from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.database import Database
from app.identifiers import generate_run_id
from app.migrations import CURRENT_SCHEMA_VERSION
from app.redaction import redact_text
from app.run_state import FailureStage, RunStatus
from app.stats.completeness import (
    CompletenessDisposition,
    CompletenessResult,
    GameAcquisitionState,
    calculate_completeness,
    classify_completeness_reason,
)
from app.stats.contracts import (
    FixtureResponse,
    RawArtifact,
    RequestParameter,
    StatcastQuery,
    StatsProvider,
    StatsRequest,
    StatsResponse,
    StatsTransport,
    StatsTransportError,
)
from app.stats.features import (
    AGGREGATE_WINDOW_KEYS,
    BattingAggregateLine,
    BattingGameLine,
    PitchingAggregateLine,
    PitchingGameLine,
    StatcastBattedBall,
    StatcastPlateAppearance,
    StatcastPitchMetric,
    StatcastPitcherAppearance,
    StatcastSwingMetric,
    StatcastPitcherPitch,
    build_aggregate_player_feature_payloads,
    build_aggregate_player_feature_payloads_v3,
    build_aggregate_team_feature_payloads,
    build_bullpen_workload,
    build_player_feature_payloads,
    derive_statcast_plate_appearances,
    derive_statcast_pitcher_appearances,
)
from app.stats.identities import (
    TEAM_SOURCE_ALIASES,
    require_active_team_identity,
    resolve_baseball_reference_aggregate_team,
    resolve_team_identity,
)
from app.stats.normalization import (
    GameStatus,
    NormalizedPitch,
    ScheduleGame,
    normalize_baseball_reference_schedule_row,
    normalize_statcast_rows,
    optional_float,
    optional_int,
    statcast_plate_appearance_classification,
)
from app.stats.player_register_reconciliation import (
    LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY,
    PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
    PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
    PLAYER_REGISTER_RECONCILIATION_PROVIDER,
    build_register_inventory,
    canonical_checksum as register_reconciliation_checksum,
    inventories_match,
)
from app.stats.providers.baseball_reference import (
    BASEBALL_REFERENCE_BASE_URL,
    BASEBALL_REFERENCE_DAILY_PATH,
    BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS,
    BaseballReferenceProvider,
)
from app.stats.providers._common import parse_csv_bytes
from app.stats.providers.pybaseball_parity import PYBASEBALL_REQUIRED_VERSION
from app.stats.providers.player_register import (
    PYBASEBALL_REGISTER_ADAPTER_VERSION,
    PYBASEBALL_REGISTER_COMMIT_SHA,
    PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
    PYBASEBALL_REGISTER_URL,
    PlayerRegisterDataset,
    PybaseballPlayerRegisterProvider,
)
from app.stats.providers.retrosheet import (
    MLB_REGULAR_SEASON_SCOPE,
    RETROSHEET_ATTRIBUTION_TEXT,
    RETROSHEET_ATTRIBUTION_VERSION,
    RETROSHEET_REGULAR_SEASON_URL,
    RETROSHEET_SEVEN_MEMBERS,
    RetrosheetCsvMember,
    RetrosheetDataset,
    RetrosheetProvider,
)
from app.stats.providers.statcast import (
    STATCAST_CSV_URL,
    StatcastDataset,
    StatcastProvider,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.run_lock import StatsRunExecutionLock, StatsRunLockError
from app.stats.reconciliation import (
    COUNTING_RECONCILIATION_CONTRACT_VERSION,
    CrossSourceCountingReconciliation,
    reconcile_baseball_reference_to_statcast,
    reconcile_schedule_pair,
)
from app.stats.retrosheet_normalization import (
    RetrosheetExcludedRow,
    RetrosheetGame,
    RetrosheetPlay,
    RetrosheetPlayerGameLine,
    RetrosheetTeamGameLine,
    normalize_retrosheet_game_row,
    normalize_retrosheet_play_row,
    normalize_retrosheet_player_game_row,
    normalize_retrosheet_player_mapping_row,
    normalize_retrosheet_team_game_row,
    parse_retrosheet_game_id,
)
from app.stats.reporting import (
    build_raw_checksum_inventory,
    merge_validation_report,
    report_checksum,
)
from app.stats.repository import StatsRepository
from app.stats.transport import FixtureStatsTransport, HttpStatsTransport


STATS_ACQUISITION_VERSION = "DSE_MLB_STATS_ACQUISITION_V1"
STATS_ADAPTER_PROVENANCE_VERSION = (
    f"{STATS_ACQUISITION_VERSION};pybaseball-parity={PYBASEBALL_REQUIRED_VERSION}"
)
RETROSHEET_ADAPTER_VERSION = "DSE_RETROSHEET_ADAPTER_V1"
BASEBALL_REFERENCE_ADAPTER_VERSION = "DSE_BASEBALL_REFERENCE_ADAPTER_V1"
STATCAST_ADAPTER_VERSION = "DSE_STATCAST_ADAPTER_V3"
STATCAST_COMPLETION_CONTRACT = "DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1"
STATCAST_COMPLETION_EVIDENCE_CONTRACT = (
    "DSE_STATCAST_GAME_COMPLETION_EVIDENCE_V1"
)
RETROSHEET_ANALYTICAL_INVENTORY_CONTRACT = (
    "DSE_RETROSHEET_ANALYTICAL_INVENTORY_V1"
)
RETROSHEET_ANALYTICAL_INVENTORY_TABLES = (
    "team_identities",
    "player_identities",
    "player_identifier_mappings",
    "games",
    "game_status_observations",
    "team_game_snapshots",
    "player_game_snapshots",
    "lineup_snapshots",
    "lineup_entries",
    "play_identities",
    "play_revisions",
)
_GID_RE = re.compile(r"^(?P<home>[A-Z]{3})(?P<date>[0-9]{8})(?P<number>[0-9]+)$")
_SAFE_STATS_RUN_RE = re.compile(r"^stats_[0-9a-f]{32}$")
_REGULAR_GAME_TYPES = frozenset({"r", "regular", "regular_season"})
_BREF_ALTERNATE_GAME_TYPE_FIELDS = (
    "season_type",
    "competition_type",
    "event_type",
    "postseason_round",
)
_BREF_NONREGULAR_GAME_TYPE_CLASSIFICATIONS = {
    "p": "postseason",
    "postseason": "postseason",
    "playoffs": "postseason",
    "e": "exhibition",
    "exhibition": "exhibition",
    "s": "spring_training",
    "spring": "spring_training",
    "spring_training": "spring_training",
    "a": "all_star",
    "all-star": "all_star",
    "all_star": "all_star",
    "futures": "futures_game",
    "futures_game": "futures_game",
    "home_run_derby": "home_run_derby",
}
_BASEBALL_REFERENCE_AGGREGATE_WINDOWS = (
    ("season_to_date", None),
    ("rolling_7_days", 7),
    ("rolling_14_days", 14),
    ("rolling_30_days", 30),
)
ScheduleRecord = tuple[ScheduleGame, str, RawArtifact]


@dataclass(frozen=True, slots=True)
class _ScheduleGameTypeClassification:
    normalized_game_type: str | None
    evidence_source: str | None
    exclusion_reason: str | None
    excluded_classification: str | None

    @property
    def is_regular_season(self) -> bool:
        return self.normalized_game_type is not None


def _classify_baseball_reference_schedule_row(
    row: Mapping[str, Any],
) -> _ScheduleGameTypeClassification:
    """Classify a BRef schedule row from positive source evidence only.

    An explicit source classification always takes precedence. When the canonical
    ``game_type`` field is absent, another explicit classification field may prove
    regular-season status. Otherwise, the structured positive regular-season game
    ordinal (``game_number`` or Baseball-Reference's observed ``team_game`` /
    ``Gm#`` key) is required. Merely appearing in a team schedule table is never
    classification evidence. If both ordinal aliases occur, both must be positive
    and equal.
    """

    explicit_game_type = str(row.get("game_type") or "").strip()
    if explicit_game_type:
        normalized = explicit_game_type.casefold()
        if normalized in _REGULAR_GAME_TYPES:
            return _ScheduleGameTypeClassification(
                normalized, "explicit_source_field:game_type", None, None
            )
        excluded = _BREF_NONREGULAR_GAME_TYPE_CLASSIFICATIONS.get(normalized)
        return _ScheduleGameTypeClassification(
            None,
            "explicit_source_field:game_type",
            (
                "explicit_non_regular_season_game_type"
                if excluded is not None
                else "ambiguous_game_type"
            ),
            excluded or "malformed",
        )

    for field in _BREF_ALTERNATE_GAME_TYPE_FIELDS:
        explicit_value = str(row.get(field) or "").strip()
        if not explicit_value:
            continue
        normalized = explicit_value.casefold()
        if normalized in _REGULAR_GAME_TYPES:
            return _ScheduleGameTypeClassification(
                normalized, f"explicit_source_field:{field}", None, None
            )
        excluded = (
            "postseason"
            if field == "postseason_round"
            else _BREF_NONREGULAR_GAME_TYPE_CLASSIFICATIONS.get(normalized)
        )
        return _ScheduleGameTypeClassification(
            None,
            f"explicit_source_field:{field}",
            (
                "explicit_non_regular_season_game_type"
                if excluded is not None
                else "ambiguous_game_type"
            ),
            excluded or "malformed",
        )

    ordinal_keys = tuple(
        key for key in ("game_number", "team_game") if key in row
    )
    if not ordinal_keys:
        return _ScheduleGameTypeClassification(
            None,
            None,
            "missing_positive_regular_season_classification",
            "malformed",
        )
    ordinal_values = {
        key: str(row.get(key) or "").strip() for key in ordinal_keys
    }
    valid_ordinals = all(
        re.fullmatch(r"[1-9][0-9]*", value) is not None
        for value in ordinal_values.values()
    )
    consistent_ordinals = len(set(ordinal_values.values())) == 1
    if valid_ordinals and consistent_ordinals:
        evidence_key = (
            ordinal_keys[0]
            if len(ordinal_keys) == 1
            else "consistent_game_number_and_team_game"
        )
        return _ScheduleGameTypeClassification(
            "regular_season",
            f"baseball_reference_structured_regular_season_{evidence_key}",
            None,
            None,
        )
    return _ScheduleGameTypeClassification(
        None,
        "baseball_reference_structured_game_ordinal",
        "ambiguous_regular_season_game_ordinal",
        "malformed",
    )


def stats_source_contract_version(command: AcquisitionCommand, season: int) -> str:
    if command is AcquisitionCommand.BOOTSTRAP_RETROSHEET:
        return f"retrosheet-regular-csv-through-{season}"
    if command is AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN:
        return PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT
    return (
        f"baseball-reference-standard-{season}+"
        f"baseball-savant-statcast-search-csv-{season}"
    )


def stats_provider_inventory(
    season: int,
    *,
    historical_through_season: int | None = None,
) -> dict[str, dict[str, object]]:
    historical_cutoff = (
        season - 1
        if historical_through_season is None
        else historical_through_season
    )
    return {
        "retrosheet": {
            "adapter_version": RETROSHEET_ADAPTER_VERSION,
            "resource_identity": RETROSHEET_REGULAR_SEASON_URL,
            "attribution_version": RETROSHEET_ATTRIBUTION_VERSION,
            "attribution_text": RETROSHEET_ATTRIBUTION_TEXT,
            "season_contract": f"regular-season-through-{historical_cutoff}",
            "transport_role": "canonical_project_controlled_raw_first",
        },
        "baseball_reference": {
            "adapter_version": BASEBALL_REFERENCE_ADAPTER_VERSION,
            "resource_identity": (
                f"{BASEBALL_REFERENCE_BASE_URL}{BASEBALL_REFERENCE_DAILY_PATH};"
                f"{BASEBALL_REFERENCE_BASE_URL}/teams/{{team}}/{season}-schedule-scores.shtml"
            ),
            "season_contract": f"bounded-standard-and-schedule-{season}",
            "transport_role": "canonical_project_controlled_raw_first",
            "request_policy": (
                "https;serial-cross-process;minimum-six-seconds-between-starts"
            ),
        },
        "statcast": {
            "adapter_version": STATCAST_ADAPTER_VERSION,
            "resource_identity": STATCAST_CSV_URL,
            "season_contract": f"one-day-chunks-{season}",
            "transport_role": "canonical_project_controlled_raw_first",
            "request_policy": (
                "https;serial-cross-process;one-live-request-at-a-time"
            ),
            "query_contract": "pybaseball-2.2.7-small-request-compatible",
            "parallel": False,
        },
        "pybaseball": {
            "adapter_version": PYBASEBALL_REGISTER_ADAPTER_VERSION,
            "pybaseball_version": PYBASEBALL_REQUIRED_VERSION,
            "resource_identity": PYBASEBALL_REGISTER_URL,
            "resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
            "raw_endpoint_category": PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
            "season_contract": "exact-retrosheet-to-mlbam-crosswalk-and-parity",
            "transport_role": (
                "canonical_project_controlled_register_raw_first;"
                "noncanonical_pybaseball_function_reference"
            ),
        },
    }


class AcquisitionCommand(StrEnum):
    BOOTSTRAP_RETROSHEET = "bootstrap-retrosheet"
    BACKFILL_CURRENT = "backfill-current"
    DAILY = "daily"
    RECONCILE_PLAYER_REGISTER_PIN = "reconcile-player-register-pin"
    VALIDATE = "validate"


class AcquisitionOutcome(StrEnum):
    """Versioned command outcome; warnings are descriptive, not dispositive."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AcquisitionOutcomeEvidence:
    """Typed outcome inputs; warnings never decide a command outcome."""

    hard_failures: tuple[str, ...] = ()
    partial_reasons: tuple[str, ...] = ()
    informational_warnings: tuple[str, ...] = ()


def classify_acquisition_outcome(
    evidence: AcquisitionOutcomeEvidence,
) -> AcquisitionOutcome:
    if evidence.hard_failures:
        return AcquisitionOutcome.FAILED
    if evidence.partial_reasons:
        return AcquisitionOutcome.PARTIAL
    return AcquisitionOutcome.SUCCESS


def build_acquisition_outcome_evidence(
    completeness: CompletenessResult,
    *,
    completeness_watermark_eligible: bool,
) -> AcquisitionOutcomeEvidence:
    hard_failures: list[str] = []
    partial_reasons: list[str] = []

    if not completeness_watermark_eligible:
        hard_failures.append(
            "unexplained_cross_source_reconciliation_difference"
        )

    reason = completeness.partial_date_reason

    # Internally inconsistent completeness evidence must also fail closed.
    if (completeness.partial_date is None) != (reason is None):
        hard_failures.append("invalid_completeness_reason_contract")
    elif reason is not None:
        disposition = classify_completeness_reason(reason)

        if disposition in {
            CompletenessDisposition.RETRYABLE_PARTIAL,
            CompletenessDisposition.REVIEW_PARTIAL,
        }:
            partial_reasons.append(reason)
        else:
            hard_failures.append(reason)

    return AcquisitionOutcomeEvidence(
        hard_failures=tuple(hard_failures),
        partial_reasons=tuple(partial_reasons),
    )


class AcquisitionMode(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"


class AcquisitionError(RuntimeError):
    pass


class AcquisitionConfigurationError(AcquisitionError):
    pass


class AcquisitionResumeError(AcquisitionError):
    pass


class AcquisitionExecutionError(AcquisitionError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        stats_run_id: str | None = None,
    ) -> None:
        super().__init__(redact_text(message))
        self.run_id = run_id
        self.stats_run_id = stats_run_id


class IdFactory(Protocol):
    def __call__(self, prefix: str) -> str: ...


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("acquisition timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_statcast_inning_half(value: object) -> str | None:
    """Map supported Statcast inning-half labels without changing raw metrics."""

    normalized = str(value or "").strip().casefold()
    if normalized == "top":
        return "top"
    if normalized in {"bot", "bottom"}:
        return "bottom"
    return None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_result_json(value: Mapping[str, Any]) -> str:
    return _canonical_json(value)


def _checksum(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _date_at_utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _stored_completeness_for_cutoff(
    details_json: object,
    requested_through_date: date,
) -> CompletenessResult | None:
    """Read immutable per-run completeness evidence for one exact cutoff."""

    try:
        details = json.loads(str(details_json))
        if not isinstance(details, Mapping):
            return None
        raw = details.get("completeness")
        if not isinstance(raw, Mapping):
            return None
        if str(raw.get("requested_through_date")) != requested_through_date.isoformat():
            return None

        def optional_date(field: str) -> date | None:
            value = raw.get(field)
            if value is None:
                return None
            parsed = date.fromisoformat(str(value))
            if parsed > requested_through_date:
                raise ValueError("stored completeness exceeds its requested cutoff")
            return parsed

        partial_date = optional_date("partial_date")
        partial_reason = raw.get("partial_date_reason")
        if (partial_date is None) != (partial_reason is None):
            return None
        return CompletenessResult(
            requested_through_date=requested_through_date,
            contiguous_regular_season_complete_through_date=optional_date(
                "contiguous_regular_season_complete_through_date"
            ),
            latest_ingested_completed_game_date=optional_date(
                "latest_ingested_completed_game_date"
            ),
            partial_date=partial_date,
            partial_date_reason=(
                str(partial_reason) if partial_reason is not None else None
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _counting_reconciliation_is_ready(details_json: object) -> bool:
    """Require the versioned summary and no unexplained cross-source differences."""

    try:
        details = json.loads(str(details_json))
        if not isinstance(details, Mapping):
            return False
        return (
            details.get("contract_version")
            == COUNTING_RECONCILIATION_CONTRACT_VERSION
            and int(details.get("entities_compared", -1)) > 0
            and int(details.get("fields_compared", -1)) >= 0
            and int(details.get("fields_matched", -1)) >= 0
            and int(details.get("explained_difference_count", -1)) >= 0
            and int(details.get("unexplained_difference_count", -1)) == 0
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def _safe_int(row: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        parsed = optional_int(row.get(key))
        if parsed is not None:
            return parsed
    return 0


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "starter", "start"}


def _nonnegative_count(row: Mapping[str, object], key: str) -> int:
    value = optional_int(row.get(key))
    if value is None or value < 0:
        raise ValueError(f"Baseball-Reference aggregate {key!r} is missing or invalid")
    return value


def _innings_to_outs(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Baseball-Reference aggregate innings are missing")
    whole, separator, fraction = text.partition(".")
    if not whole.isdigit() or (separator and fraction not in {"0", "1", "2"}):
        raise ValueError("Baseball-Reference aggregate innings are invalid")
    return (int(whole) * 3) + (int(fraction) if separator else 0)


def _aggregate_ranges(
    season_start: date,
    through_date: date,
) -> tuple[tuple[str, date, date], ...]:
    if through_date < season_start:
        return ()
    ranges: list[tuple[str, date, date]] = []
    for window_key, days in _BASEBALL_REFERENCE_AGGREGATE_WINDOWS:
        if window_key not in AGGREGATE_WINDOW_KEYS:
            raise AcquisitionExecutionError("aggregate window contract is inconsistent")
        start = (
            season_start
            if days is None
            else max(season_start, through_date - timedelta(days=days - 1))
        )
        ranges.append((window_key, start, through_date))
    return tuple(ranges)


def _aggregate_capture_ranges(
    season_start: date,
    *,
    feature_through_date: date,
    completeness_through_date: date | None,
) -> tuple[tuple[str, date, date], ...]:
    """Return D-1 feature windows plus independent source-completeness evidence."""
    ranges = list(_aggregate_ranges(season_start, feature_through_date))
    if completeness_through_date is not None:
        completeness_range = (
            "season_to_date",
            season_start,
            completeness_through_date,
        )
        if (
            completeness_through_date >= season_start
            and completeness_range not in ranges
        ):
            ranges.append(completeness_range)
    return tuple(ranges)


def _aggregate_capture_purposes(
    window_key: str,
    range_end: date,
    *,
    feature_through_date: date,
    completeness_through_date: date | None,
) -> tuple[str, ...]:
    purposes: list[str] = []
    if range_end == feature_through_date:
        purposes.append("feature_input")
    if (
        window_key == "season_to_date"
        and completeness_through_date is not None
        and range_end == completeness_through_date
    ):
        purposes.append("completeness_evidence")
    return tuple(purposes)


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    requested_through_date: date
    mode: AcquisitionMode = AcquisitionMode.OFFLINE
    resume_run_id: str | None = None
    source_stats_run_id: str | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requested_through_date, date) or isinstance(
            self.requested_through_date, datetime
        ):
            raise TypeError("requested_through_date must be a calendar date")
        if self.resume_run_id is not None and _SAFE_STATS_RUN_RE.fullmatch(
            self.resume_run_id
        ) is None:
            raise AcquisitionResumeError(
                "resume_run_id must identify an existing service-generated stats run"
            )
        if self.source_stats_run_id is not None and _SAFE_STATS_RUN_RE.fullmatch(
            self.source_stats_run_id
        ) is None:
            raise AcquisitionConfigurationError(
                "source_stats_run_id must identify a service-generated stats run"
            )


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    command: AcquisitionCommand
    status: str
    requested_through_date: date
    run_id: str | None
    stats_run_id: str | None
    counts: Mapping[str, int]
    completeness: CompletenessResult | None
    warnings: tuple[str, ...]
    report_path: Path | None
    dry_run: bool
    outcome: AcquisitionOutcome = AcquisitionOutcome.SUCCESS
    source_observed_at: datetime | None = None
    validation_details: Mapping[str, Any] | None = None
    source_stats_run_id: str | None = None

    @property
    def exit_code(self) -> int:
        return {
            AcquisitionOutcome.SUCCESS: 0,
            AcquisitionOutcome.PARTIAL: 2,
            AcquisitionOutcome.FAILED: 1,
        }[self.outcome]

    def as_dict(self) -> dict[str, Any]:
        completeness = None
        if self.completeness is not None:
            completeness = {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self.completeness).items()
            }
        return {
            "acquisition_version": STATS_ACQUISITION_VERSION,
            "acquisition_provenance": {
                "canonical_transport": "project_controlled_exact_raw_bytes",
                "canonical_adapter_version": STATS_ACQUISITION_VERSION,
                "source_contract_version": stats_source_contract_version(
                    self.command, self.requested_through_date.year
                ),
                "pybaseball_version": PYBASEBALL_REQUIRED_VERSION,
                "pybaseball_role": "noncanonical_parity_reference",
                "providers": stats_provider_inventory(
                    self.requested_through_date.year,
                    historical_through_season=(
                        self.requested_through_date.year
                        if self.command
                        in {
                            AcquisitionCommand.BOOTSTRAP_RETROSHEET,
                            AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
                        }
                        else None
                    ),
                ),
            },
            "command": self.command.value,
            "outcome_contract_version": "DSE_STATS_ACQUISITION_OUTCOME_V1",
            "outcome": self.outcome.value,
            "status": self.status,
            "exit_code": self.exit_code,
            "requested_through_date": self.requested_through_date.isoformat(),
            "source_observed_at": (
                self.source_observed_at.isoformat()
                if self.source_observed_at is not None
                else None
            ),
            "run_id": self.run_id,
            "stats_run_id": self.stats_run_id,
            "source_stats_run_id": self.source_stats_run_id,
            "counts": dict(sorted(self.counts.items())),
            "completeness": completeness,
            "effective_regular_season_complete_through_date": (
                self.completeness.contiguous_regular_season_complete_through_date.isoformat()
                if self.completeness is not None
                and self.completeness.contiguous_regular_season_complete_through_date
                is not None
                else None
            ),
            "latest_ingested_completed_game_date": (
                self.completeness.latest_ingested_completed_game_date.isoformat()
                if self.completeness is not None
                and self.completeness.latest_ingested_completed_game_date is not None
                else None
            ),
            "partial_date": (
                self.completeness.partial_date.isoformat()
                if self.completeness is not None
                and self.completeness.partial_date is not None
                else None
            ),
            "partial_date_reason": (
                self.completeness.partial_date_reason
                if self.completeness is not None
                else None
            ),
            "warnings": list(self.warnings),
            "report_path": str(self.report_path) if self.report_path is not None else None,
            "dry_run": self.dry_run,
            "validation_details": (
                dict(self.validation_details)
                if self.validation_details is not None
                else None
            ),
        }


@dataclass(slots=True)
class _RunContext:
    run_id: str
    stats_run_id: str
    command: AcquisitionCommand
    season: int
    resumed: bool = False
    source_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _BaseballReferenceAggregateCollection:
    persisted: int
    artifacts: tuple[RawArtifact, ...]
    rejected: int
    batting: tuple[BattingAggregateLine, ...]
    pitching: tuple[PitchingAggregateLine, ...]
    captures: tuple[_BaseballReferenceAggregateCapture, ...] = ()


@dataclass(frozen=True, slots=True)
class _BaseballReferenceAggregateCapture:
    raw_payload_id: str
    artifact: RawArtifact
    stat_kind: str
    window_key: str
    range_start: date
    range_end: date
    purposes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ScheduleCollection:
    records: tuple[ScheduleRecord, ...]
    raw_ids: Mapping[str, tuple[str, RawArtifact]]
    source_row_count: int
    persisted_observation_count: int
    season_start: date
    source_ids: tuple[str, ...]
    reconciled_pair_count: int
    incomplete_pair_count: int


@dataclass(frozen=True, slots=True)
class _CompletedStatcastGame:
    game_pk: int
    game_date: date
    home_team_key: str
    away_team_key: str
    home_score: int
    away_score: int
    explicit_sequence: int | None


@dataclass(frozen=True, slots=True)
class _StatcastGameReconciliation:
    schedule_to_game_pk: Mapping[str, int]
    validation_failed_schedule_ids: frozenset[str]
    completed_game_pks: frozenset[int]


_SINGLE_OUT_EVENTS = frozenset(
    {
        "field_out",
        "fielders_choice_out",
        "force_out",
        "sac_bunt",
        "sac_fly",
        "strikeout",
    }
)
_DOUBLE_OUT_EVENTS = frozenset(
    {"double_play", "grounded_into_double_play", "strikeout_double_play"}
)
_TRIPLE_OUT_EVENTS = frozenset({"triple_play"})
_BREF_GAME_ID_RE = re.compile(r"[A-Za-z0-9]{3}[0-9]{8}(?P<sequence>[0-9])")


def _pitch_ends_half_inning(pitch: NormalizedPitch) -> bool:
    event = str(pitch.metrics.get("events") or "").strip().casefold()
    outs_before = optional_int(pitch.metrics.get("outs_when_up"))
    if outs_before is None:
        return False
    return (
        (outs_before == 2 and event in _SINGLE_OUT_EVENTS)
        or (outs_before >= 1 and event in _DOUBLE_OUT_EVENTS)
        or (outs_before == 0 and event in _TRIPLE_OUT_EVENTS)
    )


def _statcast_explicit_sequence(pitches: Sequence[NormalizedPitch]) -> int | None:
    observed: set[int] = set()
    missing = False
    for pitch in pitches:
        pitch_values: set[int] = set()
        for field in ("game_number", "doubleheader_sequence"):
            value = optional_int(pitch.metrics.get(field))
            if value is not None:
                pitch_values.add(value)
        if not pitch_values:
            missing = True
        elif len(pitch_values) != 1:
            return None
        else:
            observed.update(pitch_values)
    if not observed:
        return None
    if missing or len(observed) != 1 or next(iter(observed)) not in {1, 2}:
        return None
    return next(iter(observed))


def _completed_statcast_game(
    pitches: Sequence[NormalizedPitch],
) -> _CompletedStatcastGame | None:
    if not pitches:
        return None
    first = pitches[0]
    identity = (first.game_date, first.home_team_key, first.away_team_key)
    if any(
        (pitch.game_date, pitch.home_team_key, pitch.away_team_key) != identity
        for pitch in pitches
    ):
        return None

    ordered = sorted(pitches, key=lambda pitch: (pitch.at_bat_number, pitch.pitch_number))
    at_bats = {pitch.at_bat_number for pitch in ordered}
    if not at_bats or at_bats != set(range(1, max(at_bats) + 1)):
        return None
    by_at_bat: defaultdict[int, set[int]] = defaultdict(set)
    for pitch in ordered:
        by_at_bat[pitch.at_bat_number].add(pitch.pitch_number)
    if any(
        pitch_numbers != set(range(1, max(pitch_numbers) + 1))
        for pitch_numbers in by_at_bat.values()
    ):
        return None

    half_innings: defaultdict[tuple[int, str], list[NormalizedPitch]] = defaultdict(list)
    half_order: list[tuple[int, str]] = []
    for pitch in ordered:
        inning = optional_int(pitch.metrics.get("inning"))
        half = _canonical_statcast_inning_half(
            pitch.metrics.get("inning_topbot")
        )
        if inning is None or inning < 1 or half is None:
            return None
        half_key = (inning, half)
        if not half_order or half_order[-1] != half_key:
            if half_key in half_innings:
                return None
            half_order.append(half_key)
        half_innings[half_key].append(pitch)

    terminal = ordered[-1]
    terminal_inning = optional_int(terminal.metrics.get("inning"))
    terminal_half = _canonical_statcast_inning_half(
        terminal.metrics.get("inning_topbot")
    )
    if terminal_inning is None or terminal_inning < 5 or terminal_half is None:
        return None
    expected_halves = [
        (inning, half)
        for inning in range(1, terminal_inning + 1)
        for half in ("top", "bottom")
        if not (inning == terminal_inning and terminal_half == "top" and half == "bottom")
    ]
    if half_order != expected_halves:
        return None
    if any(
        optional_int(half_innings[half_key][0].metrics.get("outs_when_up")) != 0
        for half_key in half_order
    ):
        return None
    for half_key in half_order[:-1]:
        if not _pitch_ends_half_inning(half_innings[half_key][-1]):
            return None

    home_score = optional_int(terminal.metrics.get("post_home_score"))
    away_score = optional_int(terminal.metrics.get("post_away_score"))
    if (
        home_score is None
        or away_score is None
        or home_score < 0
        or away_score < 0
        or home_score == away_score
    ):
        return None
    if terminal_half == "top":
        if home_score <= away_score or not _pitch_ends_half_inning(terminal):
            return None
    elif home_score > away_score:
        prior_home_score = optional_int(terminal.metrics.get("home_score"))
        if prior_home_score is None or home_score <= prior_home_score:
            return None
    elif not _pitch_ends_half_inning(terminal):
        return None

    return _CompletedStatcastGame(
        game_pk=first.game_pk,
        game_date=first.game_date,
        home_team_key=first.home_team_key,
        away_team_key=first.away_team_key,
        home_score=home_score,
        away_score=away_score,
        explicit_sequence=_statcast_explicit_sequence(ordered),
    )


def _schedule_confirmed_statcast_game(
    pitches: Sequence[NormalizedPitch],
) -> _CompletedStatcastGame | None:
    """Return pitch-only game evidence that still requires a final schedule match."""

    if not pitches:
        return None
    first = pitches[0]
    identity = (first.game_date, first.home_team_key, first.away_team_key)
    if any(
        (pitch.game_date, pitch.home_team_key, pitch.away_team_key) != identity
        for pitch in pitches
    ):
        return None

    ordered = sorted(
        pitches, key=lambda pitch: (pitch.at_bat_number, pitch.pitch_number)
    )
    if any(pitch.at_bat_number < 1 or pitch.pitch_number < 1 for pitch in ordered):
        return None
    by_at_bat: defaultdict[int, set[int]] = defaultdict(set)
    for pitch in ordered:
        by_at_bat[pitch.at_bat_number].add(pitch.pitch_number)
    if any(
        pitch_numbers != set(range(1, max(pitch_numbers) + 1))
        for pitch_numbers in by_at_bat.values()
    ):
        return None

    half_innings: defaultdict[
        tuple[int, str], list[NormalizedPitch]
    ] = defaultdict(list)
    half_order: list[tuple[int, str]] = []
    for pitch in ordered:
        inning = optional_int(pitch.metrics.get("inning"))
        half = _canonical_statcast_inning_half(
            pitch.metrics.get("inning_topbot")
        )
        if inning is None or inning < 1 or half is None:
            return None
        half_key = (inning, half)
        if not half_order or half_order[-1] != half_key:
            if half_key in half_innings:
                return None
            half_order.append(half_key)
        half_innings[half_key].append(pitch)

    terminal = ordered[-1]
    terminal_inning = optional_int(terminal.metrics.get("inning"))
    terminal_half = _canonical_statcast_inning_half(
        terminal.metrics.get("inning_topbot")
    )
    if terminal_inning is None or terminal_inning < 5 or terminal_half is None:
        return None
    expected_halves = [
        (inning, half)
        for inning in range(1, terminal_inning + 1)
        for half in ("top", "bottom")
        if not (
            inning == terminal_inning
            and terminal_half == "top"
            and half == "bottom"
        )
    ]
    if half_order != expected_halves:
        return None
    if any(
        optional_int(half_innings[half_key][0].metrics.get("outs_when_up")) != 0
        for half_key in half_order
    ):
        return None

    home_score = optional_int(terminal.metrics.get("post_home_score"))
    away_score = optional_int(terminal.metrics.get("post_away_score"))
    if (
        home_score is None
        or away_score is None
        or home_score < 0
        or away_score < 0
        or home_score == away_score
    ):
        return None
    return _CompletedStatcastGame(
        game_pk=first.game_pk,
        game_date=first.game_date,
        home_team_key=first.home_team_key,
        away_team_key=first.away_team_key,
        home_score=home_score,
        away_score=away_score,
        explicit_sequence=_statcast_explicit_sequence(ordered),
    )


def _baseball_reference_explicit_sequence(game: ScheduleGame) -> int | None:
    source_row = game.source_row
    if not isinstance(source_row, Mapping):
        return None
    source_game_id = str(source_row.get("game_id") or "").strip()
    match = _BREF_GAME_ID_RE.fullmatch(source_game_id)
    if match is None:
        return None
    sequence = int(match.group("sequence"))
    return sequence if sequence in {1, 2} else None


def _reconcile_completed_statcast_games(
    schedules: Sequence[ScheduleRecord],
    pitches: Sequence[NormalizedPitch],
    conflicts: Sequence[tuple[tuple[int, int, int], str, str]],
) -> _StatcastGameReconciliation:
    final_by_key: defaultdict[tuple[date, str, str], list[ScheduleGame]] = defaultdict(list)
    for game, _, _ in schedules:
        if game.status is GameStatus.FINAL:
            final_by_key[(game.official_date, game.home_team_key, game.away_team_key)].append(game)

    pitches_by_game: defaultdict[int, list[NormalizedPitch]] = defaultdict(list)
    for pitch in pitches:
        pitches_by_game[pitch.game_pk].append(pitch)
    completed_by_key: defaultdict[
        tuple[date, str, str], list[_CompletedStatcastGame]
    ] = defaultdict(list)
    evidence_by_game_pk: dict[int, _CompletedStatcastGame] = {}
    for game_pk, game_pitches in pitches_by_game.items():
        evidence = _schedule_confirmed_statcast_game(game_pitches)
        if evidence is None:
            continue
        evidence_by_game_pk[game_pk] = evidence
        completed_by_key[
            (evidence.game_date, evidence.home_team_key, evidence.away_team_key)
        ].append(evidence)

    conflicted_game_pks = {identity[0] for identity, _, _ in conflicts}
    matches: dict[str, int] = {}
    validation_failed: set[str] = set()
    for key, schedule_games in final_by_key.items():
        statcast_games = completed_by_key.get(key, [])
        for score in sorted(
            {(game.home_score, game.away_score) for game in schedule_games},
            key=str,
        ):
            score_schedules = [
                game
                for game in schedule_games
                if (game.home_score, game.away_score) == score
            ]
            score_statcast = [
                game
                for game in statcast_games
                if (game.home_score, game.away_score) == score
                and game.game_pk not in conflicted_game_pks
            ]
            if len(score_schedules) == 1 and len(score_statcast) == 1:
                matches[score_schedules[0].provider_game_id] = score_statcast[0].game_pk
                continue
            if len(score_schedules) > 1 and len(score_schedules) == len(score_statcast):
                schedules_by_sequence = {
                    sequence: game
                    for game in score_schedules
                    if (sequence := _baseball_reference_explicit_sequence(game))
                    is not None
                }
                statcast_by_sequence = {
                    game.explicit_sequence: game
                    for game in score_statcast
                    if game.explicit_sequence is not None
                }
                if (
                    len(schedules_by_sequence) == len(score_schedules)
                    and len(statcast_by_sequence) == len(score_statcast)
                    and schedules_by_sequence.keys() == statcast_by_sequence.keys()
                ):
                    for sequence, schedule_game in schedules_by_sequence.items():
                        matches[schedule_game.provider_game_id] = (
                            statcast_by_sequence[sequence].game_pk
                        )

        completed_scores = {
            (game.home_score, game.away_score)
            for game in statcast_games
            if game.game_pk not in conflicted_game_pks
        }
        schedule_scores = {(game.home_score, game.away_score) for game in schedule_games}
        if completed_scores - schedule_scores:
            validation_failed.update(game.provider_game_id for game in schedule_games)

    for game_pk in conflicted_game_pks:
        evidence = evidence_by_game_pk.get(game_pk)
        if evidence is None:
            continue
        key = (evidence.game_date, evidence.home_team_key, evidence.away_team_key)
        candidates = [
            game
            for game in final_by_key.get(key, [])
            if (game.home_score, game.away_score)
            == (evidence.home_score, evidence.away_score)
        ]
        if len(candidates) == 1:
            validation_failed.add(candidates[0].provider_game_id)

    return _StatcastGameReconciliation(
        schedule_to_game_pk=dict(sorted(matches.items())),
        validation_failed_schedule_ids=frozenset(validation_failed),
        completed_game_pks=frozenset(matches.values()),
    )


class _RecordingTransport:
    def __init__(self, transport: StatsTransport) -> None:
        self.transport = transport
        self.captures: list[RawArtifact] = []
        self.requests: list[StatsRequest] = []
        self.attempts = 0

    def fetch(self, request: StatsRequest) -> StatsResponse:
        self.requests.append(request)
        try:
            response = self.transport.fetch(request)
        except StatsTransportError as exc:
            self.attempts += exc.attempts
            self._append_captures(exc.captures)
            raise
        self.attempts += response.attempts
        self._append_captures(response.attempt_captures)
        return response

    def _append_captures(self, captures: Iterable[RawArtifact]) -> None:
        known = {item.capture_id for item in self.captures}
        for capture in captures:
            if capture.capture_id not in known:
                self.captures.append(capture)
                known.add(capture.capture_id)


class _ArtifactReplayTransport:
    """Expose one checksum-verified raw artifact to a provider parser."""

    def __init__(self, artifact: RawArtifact) -> None:
        self.artifact = artifact
        self.used = False

    def fetch(self, request: StatsRequest) -> StatsResponse:
        if self.used:
            raise AcquisitionResumeError(
                "a checkpoint raw artifact may only be replayed once"
            )
        if request.provider is not self.artifact.provider:
            raise AcquisitionResumeError(
                "checkpoint raw artifact provider does not match its parser"
            )
        self.used = True
        return StatsResponse(
            request=request,
            status_code=200,
            headers={"content-type": self.artifact.content_type},
            capture=self.artifact,
            attempt_captures=(),
            attempts=0,
            cache_hit=True,
        )


@dataclass(frozen=True, slots=True)
class _SourceRunReplayEntry:
    source_raw_payload_id: str
    expected_request: StatsRequest
    artifact: RawArtifact


class SourceRunReplayTransport:
    """Replay one prior run's immutable captures without making network calls."""

    def __init__(
        self,
        raw_store: RawArtifactStore,
        *,
        source_stats_run_id: str,
        entries: Sequence[_SourceRunReplayEntry],
    ) -> None:
        self.raw_store = raw_store
        self.source_stats_run_id = source_stats_run_id
        queues: dict[str, deque[_SourceRunReplayEntry]] = defaultdict(deque)
        for entry in entries:
            queues[entry.expected_request.fixture_key].append(entry)
        self._entries = {key: value for key, value in sorted(queues.items())}
        self.expected_capture_count = len(entries)
        self.consumed_capture_count = 0
        self.requests: list[StatsRequest] = []

    def fetch(self, request: StatsRequest) -> StatsResponse:
        queue = self._entries.get(request.fixture_key)
        if not queue:
            raise StatsTransportError(
                "retained source run has no capture for the requested replay key",
                attempts=0,
            )
        entry = queue[0]
        if request != entry.expected_request:
            raise StatsTransportError(
                "retained source capture request contract does not match replay",
                attempts=0,
            )
        self.raw_store.read_verified(entry.artifact)
        queue.popleft()
        self.requests.append(request)
        self.consumed_capture_count += 1
        return StatsResponse(
            request=request,
            status_code=200,
            headers={"content-type": entry.artifact.content_type},
            capture=entry.artifact,
            attempt_captures=(),
            attempts=0,
            cache_hit=True,
        )

    def assert_exhausted(self) -> None:
        remaining = sum(len(queue) for queue in self._entries.values())
        if remaining:
            raise AcquisitionResumeError(
                "retained source run replay did not consume its complete capture inventory"
            )
        if self.consumed_capture_count != self.expected_capture_count:
            raise AcquisitionResumeError(
                "retained source run replay capture count did not reconcile"
            )


def _source_replay_artifact(
    raw_store: RawArtifactStore,
    row: Mapping[str, Any],
) -> RawArtifact:
    try:
        relpath = Path(str(row["artifact_relpath"]))
        if relpath.is_absolute() or ".." in relpath.parts:
            raise ValueError("raw artifact path is not contained")
        metadata_path = raw_store.root.joinpath(relpath).with_suffix(".json")
        artifact = raw_store.load_verified_artifact(metadata_path)
        metadata = json.loads(str(row["metadata_json"]))
        expected_retrieved_at = datetime.fromisoformat(str(row["retrieved_at"]))
        if (
            expected_retrieved_at.tzinfo is None
            or expected_retrieved_at.utcoffset() is None
        ):
            raise ValueError("raw retrieval timestamp is not timezone-aware")
        if (
            artifact.capture_id != str(row["source_capture_id"])
            or artifact.provider.value != str(row["provider"])
            or artifact.endpoint_category != str(row["endpoint_category"])
            or artifact.retrieved_at != expected_retrieved_at.astimezone(timezone.utc)
            or artifact.content_type != str(row["content_type"])
            or artifact.checksum_sha256 != str(row["checksum_sha256"])
            or artifact.size_bytes != int(metadata["size_bytes"])
            or artifact.parent_checksum_sha256
            != metadata.get("parent_checksum_sha256")
        ):
            raise ValueError("raw metadata does not match immutable artifact metadata")
    except Exception as exc:
        raise AcquisitionResumeError(
            "retained source run raw artifact failed metadata or checksum verification"
        ) from exc
    return artifact


def _baseball_reference_replay_request(
    *,
    fixture_key: str,
    path: str,
    params: Mapping[str, RequestParameter] | None = None,
    persistent_cache: bool = True,
) -> StatsRequest:
    return StatsRequest(
        provider=StatsProvider.BASEBALL_REFERENCE,
        endpoint_category="html_table",
        fixture_key=fixture_key,
        url=BASEBALL_REFERENCE_BASE_URL.rstrip("/") + path,
        params=params or {},
        headers={
            "Accept": "text/html, application/xhtml+xml;q=0.9",
            "Accept-Language": "en-US,en;q=0.8",
        },
        timeout_seconds=30.0,
        max_attempts=3,
        minimum_interval_seconds=BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS,
        persistent_cache=persistent_cache,
    )


def _baseball_reference_daily_replay_request(
    *,
    fixture_key: str,
    stat_kind: str,
    range_start: date,
    range_end: date,
) -> StatsRequest:
    statistics_type = {"batting": "b", "pitching": "p"}.get(stat_kind)
    if statistics_type is None:
        raise AcquisitionResumeError(
            "retained aggregate capture has an unsupported statistic kind"
        )
    return _baseball_reference_replay_request(
        fixture_key=fixture_key,
        path=BASEBALL_REFERENCE_DAILY_PATH,
        params={
            "user_team": "",
            "bust_cache": "",
            "type": statistics_type,
            "lastndays": "7",
            "dates": "fromandto",
            "fromandto": f"{range_start.isoformat()}.{range_end.isoformat()}",
            "level": "mlb",
            "franch": "",
            "stat": "",
            "stat_value": "0",
        },
    )


def _statcast_replay_request(day: date) -> StatsRequest:
    return StatsRequest(
        provider=StatsProvider.STATCAST,
        endpoint_category="search_csv",
        fixture_key=f"statcast_{day.isoformat()}",
        url=STATCAST_CSV_URL,
        params=StatcastQuery(day, day).request_params(),
        headers={"Accept": "text/csv, application/csv;q=0.9"},
        timeout_seconds=120.0,
        max_attempts=3,
    )


def build_source_run_replay_transport(
    *,
    database: Database,
    raw_store: RawArtifactStore,
    source_stats_run_id: str,
    requested_through_date: date,
) -> SourceRunReplayTransport:
    """Build a fail-closed replay inventory for a terminal live backfill run."""

    if _SAFE_STATS_RUN_RE.fullmatch(source_stats_run_id) is None:
        raise AcquisitionConfigurationError(
            "source_stats_run_id must identify a service-generated stats run"
        )
    repository = StatsRepository(database)
    source_run = repository.get_ingestion_run(source_stats_run_id)
    if source_run is None:
        raise AcquisitionResumeError("source_stats_run_id does not exist")
    if (
        str(source_run["provider"]) != "current_mlb_stats"
        or str(source_run["status"]) not in {"completed", "completed_with_warnings"}
        or str(source_run["requested_through_date"])
        != requested_through_date.isoformat()
    ):
        raise AcquisitionResumeError(
            "source run is not a matching terminal current-statistics backfill"
        )

    with database.connect() as connection:
        checkpoint_rows = connection.execute(
            "SELECT * FROM stats_checkpoints WHERE stats_run_id=? "
            "ORDER BY dataset_key",
            (source_stats_run_id,),
        ).fetchall()
        source_raw_rows = connection.execute(
            "SELECT * FROM stats_raw_payload_metadata WHERE stats_run_id=? "
            "ORDER BY checkpoint_id,raw_payload_id",
            (source_stats_run_id,),
        ).fetchall()
    checkpoints = {str(row["dataset_key"]): dict(row) for row in checkpoint_rows}
    raw_by_checkpoint: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_raw_rows:
        checkpoint_id = row["checkpoint_id"]
        if checkpoint_id is None:
            raise AcquisitionResumeError(
                "source run contains raw evidence outside a checkpoint"
            )
        raw_by_checkpoint[str(checkpoint_id)].append(dict(row))
    raw_inventory_count = len(source_raw_rows)

    def checkpoint(name: str, *, statuses: frozenset[str]) -> dict[str, Any]:
        value = checkpoints.get(name)
        if value is None or str(value["status"]) not in statuses:
            raise AcquisitionResumeError(
                f"source run checkpoint {name!r} is unavailable for replay"
            )
        return value

    def raw_rows(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        rows = raw_by_checkpoint.get(str(value["checkpoint_id"]), [])
        return {str(row["raw_payload_id"]): dict(row) for row in rows}

    entries: list[_SourceRunReplayEntry] = []
    consumed_raw_ids: set[str] = set()

    schedule = checkpoint(
        "baseball_reference_schedule", statuses=frozenset({"completed"})
    )
    try:
        schedule_cursor = json.loads(str(schedule["cursor_after_json"] or "{}"))
        if (
            schedule_cursor["contract"] != "DSE_BREF_SCHEDULE_CHECKPOINT_V2"
            or schedule_cursor["command"]
            != AcquisitionCommand.BACKFILL_CURRENT.value
            or schedule_cursor["requested_through_date"]
            != requested_through_date.isoformat()
        ):
            raise ValueError("schedule replay identity changed")
        source_ids = tuple(sorted(str(value) for value in schedule_cursor["source_ids"]))
        raw_by_source = {
            str(key): str(value)
            for key, value in dict(
                schedule_cursor["raw_payloads_by_source_id"]
            ).items()
        }
        if (
            set(source_ids) != set(TEAM_SOURCE_ALIASES["baseball_reference"])
            or set(raw_by_source) != set(source_ids)
        ):
            raise ValueError("schedule source coverage changed")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcquisitionResumeError(
            "source run schedule checkpoint is not deterministic replay evidence"
        ) from exc
    schedule_raw = raw_rows(schedule)
    if set(raw_by_source.values()) != set(schedule_raw):
        raise AcquisitionResumeError(
            "source run schedule raw inventory does not reconcile"
        )
    for source_id in source_ids:
        raw_id = raw_by_source[source_id]
        artifact = _source_replay_artifact(raw_store, schedule_raw[raw_id])
        entries.append(
            _SourceRunReplayEntry(
                raw_id,
                _baseball_reference_replay_request(
                    fixture_key=f"baseball_reference_schedule_{source_id}",
                    path=(
                        f"/teams/{source_id}/{requested_through_date.year}"
                        "-schedule-scores.shtml"
                    ),
                    persistent_cache=False,
                ),
                artifact,
            )
        )
        consumed_raw_ids.add(raw_id)

    aggregates = checkpoint(
        "baseball_reference_batting_pitching",
        statuses=frozenset({"completed", "partial"}),
    )
    try:
        aggregate_cursor = json.loads(
            str(aggregates["cursor_after_json"] or "{}")
        )
        if (
            aggregate_cursor["contract"] != "DSE_BREF_AGGREGATE_CHECKPOINT_V2"
            or int(aggregate_cursor["season"]) != requested_through_date.year
            or date.fromisoformat(str(aggregate_cursor["feature_as_of"]))
            != requested_through_date
        ):
            raise ValueError("aggregate replay identity changed")
        captures = [dict(value) for value in aggregate_cursor["captures"]]
        if (
            int(aggregate_cursor["actual_capture_count"]) != len(captures)
            or int(aggregate_cursor["expected_capture_count"]) != len(captures)
        ):
            raise ValueError("aggregate replay coverage is incomplete")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcquisitionResumeError(
            "source run aggregate checkpoint is not deterministic replay evidence"
        ) from exc
    aggregate_raw = raw_rows(aggregates)
    capture_raw_ids = {str(value["raw_payload_id"]) for value in captures}
    if capture_raw_ids != set(aggregate_raw) or len(capture_raw_ids) != len(captures):
        raise AcquisitionResumeError(
            "source run aggregate raw inventory does not reconcile"
        )
    for capture in captures:
        raw_id = str(capture["raw_payload_id"])
        stat_kind = str(capture["stat_kind"])
        window_key = str(capture["window_key"])
        range_start = date.fromisoformat(str(capture["range_start"]))
        range_end = date.fromisoformat(str(capture["range_end"]))
        artifact = _source_replay_artifact(raw_store, aggregate_raw[raw_id])
        entries.append(
            _SourceRunReplayEntry(
                raw_id,
                _baseball_reference_daily_replay_request(
                    fixture_key=f"baseball_reference_{stat_kind}_{window_key}",
                    stat_kind=stat_kind,
                    range_start=range_start,
                    range_end=range_end,
                ),
                artifact,
            )
        )
        consumed_raw_ids.add(raw_id)

    statcast_checkpoints = [
        value
        for key, value in checkpoints.items()
        if key.startswith("statcast:")
    ]
    if not statcast_checkpoints:
        raise AcquisitionResumeError("source run has no retained Statcast dates")
    observed_days: set[date] = set()
    for value in statcast_checkpoints:
        if str(value["status"]) not in {"completed", "partial"}:
            raise AcquisitionResumeError(
                "source run Statcast checkpoint is not replayable"
            )
        try:
            day = date.fromisoformat(str(value["dataset_key"]).removeprefix("statcast:"))
        except ValueError as exc:
            raise AcquisitionResumeError(
                "source run Statcast checkpoint date is invalid"
            ) from exc
        if day.year != requested_through_date.year or day > requested_through_date:
            raise AcquisitionResumeError(
                "source run Statcast checkpoint escaped the requested season"
            )
        rows = raw_rows(value)
        if len(rows) != 1:
            raise AcquisitionResumeError(
                "source run Statcast checkpoint must have exactly one raw capture"
            )
        raw_id, row = next(iter(rows.items()))
        artifact = _source_replay_artifact(raw_store, row)
        entries.append(
            _SourceRunReplayEntry(raw_id, _statcast_replay_request(day), artifact)
        )
        consumed_raw_ids.add(raw_id)
        observed_days.add(day)
    expected_days = {
        date.fromordinal(value)
        for value in range(
            min(observed_days).toordinal(),
            max(observed_days).toordinal() + 1,
        )
    }
    if observed_days != expected_days:
        raise AcquisitionResumeError(
            "source run Statcast checkpoint date coverage is not contiguous"
        )
    if raw_inventory_count != len(consumed_raw_ids) or len(entries) != len(
        consumed_raw_ids
    ):
        raise AcquisitionResumeError(
            "source run contains an unclassified or duplicate raw capture"
        )
    return SourceRunReplayTransport(
        raw_store,
        source_stats_run_id=source_stats_run_id,
        entries=entries,
    )


def load_offline_fixtures(root: Path) -> dict[str, FixtureResponse]:
    fixture_root = Path(root)
    if not fixture_root.exists() or not fixture_root.is_dir() or fixture_root.is_symlink():
        raise AcquisitionConfigurationError(
            "offline mode requires a real --fixture-root directory"
        )
    fixtures: dict[str, FixtureResponse] = {}
    known = {
        "retrosheet_regular_season_archive.zip": (
            "retrosheet_regular_season_archive",
            "application/zip",
        ),
        "statcast.csv": ("statcast", "text/csv"),
        "pybaseball_player_identifier_register.zip": (
            "pybaseball_player_identifier_register",
            "application/zip",
        ),
    }
    for filename, (key, content_type) in known.items():
        path = fixture_root / filename
        if path.is_file() and not path.is_symlink():
            fixtures[key] = FixtureResponse(path, content_type)
    for path in sorted(fixture_root.glob("baseball_reference_schedule_*.html")):
        if path.is_symlink() or not path.is_file():
            raise AcquisitionConfigurationError(
                "offline schedule fixtures must be regular non-symlink files"
            )
        fixtures[path.stem] = FixtureResponse(path, "text/html")
    for pattern in (
        "baseball_reference_batting*.html",
        "baseball_reference_pitching*.html",
    ):
        for path in sorted(fixture_root.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                raise AcquisitionConfigurationError(
                    "offline aggregate fixtures must be regular non-symlink files"
                )
            fixtures[path.stem] = FixtureResponse(path, "text/html")
    for path in sorted(fixture_root.glob("statcast_*.csv")):
        if path.is_symlink() or not path.is_file():
            raise AcquisitionConfigurationError(
                "offline Statcast fixtures must be regular non-symlink files"
            )
        fixtures[path.stem] = FixtureResponse(path, "text/csv")
    return fixtures


def build_stats_transport(
    *,
    mode: AcquisitionMode,
    raw_store: RawArtifactStore,
    fixture_root: Path | None,
    user_agent: str,
    clock: Callable[[], datetime] = _utc_now,
) -> StatsTransport:
    if mode is AcquisitionMode.OFFLINE:
        if fixture_root is None:
            raise AcquisitionConfigurationError(
                "offline mode requires --fixture-root for provider execution"
            )
        return FixtureStatsTransport(
            raw_store,
            load_offline_fixtures(fixture_root),
            clock=clock,
        )
    return HttpStatsTransport(raw_store, user_agent=user_agent, clock=clock)


class StatsAcquisitionService:
    def __init__(
        self,
        *,
        database: Database,
        raw_store: RawArtifactStore,
        transport: StatsTransport,
        report_path: Path,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        self.database = database
        self.repository = StatsRepository(database)
        self.raw_store = raw_store
        self.transport = _RecordingTransport(transport)
        self.report_path = Path(report_path)
        self.clock = clock
        self.id_factory = id_factory
        self._team_identity_cache: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._player_identity_cache: dict[tuple[str, str], str] = {}
        self._baseball_reference_player_mapping_cache: dict[str, str] = {}
        self._statcast_player_mapping_cache: dict[str, str | None] = {}
        self._statcast_player_mapping_inventory_loaded = False

    def _fixture_transport(self) -> FixtureStatsTransport | None:
        candidate: object = self.transport.transport
        visited: set[int] = set()
        while id(candidate) not in visited:
            visited.add(id(candidate))
            if isinstance(candidate, FixtureStatsTransport):
                return candidate
            delegate = getattr(candidate, "delegate", None)
            if delegate is None:
                return None
            candidate = delegate
        return None

    def _source_run_replay_transport(self) -> SourceRunReplayTransport | None:
        candidate: object = self.transport.transport
        visited: set[int] = set()
        while id(candidate) not in visited:
            visited.add(id(candidate))
            if isinstance(candidate, SourceRunReplayTransport):
                return candidate
            candidate = getattr(candidate, "transport", None)
            if candidate is None:
                break
        return None

    def execute(
        self,
        command: AcquisitionCommand,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        if request.dry_run:
            return AcquisitionResult(
                command=command,
                status="dry_run",
                requested_through_date=request.requested_through_date,
                run_id=None,
                stats_run_id=None,
                counts={"network_requests": 0, "persistent_writes": 0},
                completeness=None,
                warnings=(),
                report_path=None,
                dry_run=True,
            )
        if command is AcquisitionCommand.VALIDATE:
            self.repository.reconcile_terminal_run_checkpoints(
                transitioned_at=_aware_utc(self.clock())
            )
            return self.validate(request)
        if request.resume_run_id is not None:
            try:
                with StatsRunExecutionLock(
                    self.database.path, request.resume_run_id
                ):
                    return self._execute_provider_command(command, request)
            except StatsRunLockError as exc:
                stats = self.repository.get_ingestion_run(request.resume_run_id)
                raise AcquisitionExecutionError(
                    str(exc),
                    run_id=(str(stats["run_id"]) if stats is not None else None),
                    stats_run_id=request.resume_run_id,
                ) from exc
        return self._execute_provider_command(command, request)

    def _execute_provider_command(
        self,
        command: AcquisitionCommand,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        self.repository.reconcile_terminal_run_checkpoints(
            transitioned_at=_aware_utc(self.clock())
        )
        context = self._begin_run(command, request)
        if request.resume_run_id is not None:
            return self._execute_run_context(context, request)
        try:
            with StatsRunExecutionLock(self.database.path, context.stats_run_id):
                return self._execute_run_context(context, request)
        except StatsRunLockError as exc:
            raise AcquisitionExecutionError(
                str(exc),
                run_id=context.run_id,
                stats_run_id=context.stats_run_id,
            ) from exc

    def _execute_run_context(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        try:
            if context.command is AcquisitionCommand.BOOTSTRAP_RETROSHEET:
                result = self._bootstrap_retrosheet(context, request)
            elif (
                context.command
                is AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN
            ):
                result = self._reconcile_player_register_pin(context, request)
            elif context.command in {
                AcquisitionCommand.BACKFILL_CURRENT,
                AcquisitionCommand.DAILY,
            }:
                result = self._collect_current(context, request)
            else:
                raise AcquisitionConfigurationError(
                    f"unsupported command: {context.command}"
                )
            self._write_report(result)
            return result
        except Exception as exc:
            self._fail_run(context, exc)
            try:
                self._write_failure_report(context, request, exc)
            except Exception as report_exc:
                raise AcquisitionExecutionError(
                    f"{exc}; failure report persistence failed: "
                    f"{type(report_exc).__name__}",
                    run_id=context.run_id,
                    stats_run_id=context.stats_run_id,
                ) from exc
            if isinstance(exc, AcquisitionExecutionError):
                raise AcquisitionExecutionError(
                    str(exc),
                    run_id=context.run_id,
                    stats_run_id=context.stats_run_id,
                ) from exc
            if isinstance(exc, AcquisitionError):
                raise
            raise AcquisitionExecutionError(
                str(exc), run_id=context.run_id, stats_run_id=context.stats_run_id
            ) from exc

    def _begin_run(
        self,
        command: AcquisitionCommand,
        request: AcquisitionRequest,
    ) -> _RunContext:
        now = _aware_utc(self.clock())
        expected_provider = self._command_provider(command)
        expected_scope = self._command_scope(command, request)
        expected_configuration = self._configuration_checksum(command, request)
        if request.resume_run_id is not None:
            stats = self.repository.get_ingestion_run(request.resume_run_id)
            if stats is None:
                raise AcquisitionResumeError("resume_run_id does not exist")
            if stats["requested_through_date"] != request.requested_through_date.isoformat():
                raise AcquisitionResumeError(
                    "resume_run_id requested_through_date does not match this command"
                )
            status = str(stats["status"])
            if status in {"completed", "completed_with_warnings"}:
                raise AcquisitionResumeError(
                    "completed stats runs are immutable and do not require resumption"
                )
            if status == "failed":
                raise AcquisitionResumeError(
                    "failed stats runs are terminal; start a service-generated replacement run"
                )
            if (
                stats["provider"] != expected_provider
                or stats["scope_key"] != expected_scope
                or stats["configuration_checksum"] != expected_configuration
            ):
                raise AcquisitionResumeError(
                    "resume_run_id does not match this provider, season, command, and mode"
                )
            context = _RunContext(
                run_id=str(stats["run_id"]),
                stats_run_id=str(stats["stats_run_id"]),
                command=command,
                season=request.requested_through_date.year,
                resumed=True,
            )
            outer = self.database.get_run(context.run_id)
            if outer is None:
                raise AcquisitionResumeError("resume run has no owning collection run")
            if outer["status"] == "queued":
                self.database.transition_run(
                    context.run_id, RunStatus.RUNNING, transitioned_at=now.isoformat()
                )
            if status == "queued":
                self.repository.transition_ingestion_run(
                    context.stats_run_id, "running", transitioned_at=now
                )
            return context

        run_id = generate_run_id(request.requested_through_date)
        stats_run_id = self.id_factory("stats")
        if _SAFE_STATS_RUN_RE.fullmatch(stats_run_id) is None:
            raise AcquisitionConfigurationError(
                "stats ID factory must emit stats_<32 lowercase hex>"
            )
        self.database.create_run(run_id, request.requested_through_date)
        self.database.transition_run(
            run_id, RunStatus.RUNNING, transitioned_at=now.isoformat()
        )
        self.repository.create_ingestion_run(
            stats_run_id=stats_run_id,
            run_id=run_id,
            provider=(
                expected_provider
            ),
            scope_key=expected_scope,
            requested_through_date=request.requested_through_date,
            source_version=stats_source_contract_version(
                command, request.requested_through_date.year
            ),
            adapter_version=STATS_ADAPTER_PROVENANCE_VERSION,
            configuration_checksum=expected_configuration,
            created_at=now,
        )
        self.repository.transition_ingestion_run(
            stats_run_id, "running", transitioned_at=now
        )
        return _RunContext(
            run_id=run_id,
            stats_run_id=stats_run_id,
            command=command,
            season=request.requested_through_date.year,
        )

    @staticmethod
    def _command_provider(command: AcquisitionCommand) -> str:
        if command is AcquisitionCommand.BOOTSTRAP_RETROSHEET:
            return "retrosheet"
        if command is AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN:
            return PLAYER_REGISTER_RECONCILIATION_PROVIDER
        return "current_mlb_stats"

    @staticmethod
    def _command_scope(
        command: AcquisitionCommand,
        request: AcquisitionRequest,
    ) -> str:
        if command is AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN:
            if request.source_stats_run_id is None:
                raise AcquisitionConfigurationError(
                    "reconcile-player-register-pin requires source_stats_run_id"
                )
            return f"player-register-pin:{request.source_stats_run_id}"
        if request.source_stats_run_id is not None:
            if command is not AcquisitionCommand.BACKFILL_CURRENT:
                raise AcquisitionConfigurationError(
                    "source_stats_run_id replay is valid only for backfill-current"
                )
            if request.mode is not AcquisitionMode.OFFLINE:
                raise AcquisitionConfigurationError(
                    "source_stats_run_id replay requires offline mode"
                )
        return f"regular-season:{request.requested_through_date.year}"

    @staticmethod
    def _configuration_checksum(
        command: AcquisitionCommand,
        request: AcquisitionRequest,
    ) -> str:
        values: dict[str, object] = {
            "version": STATS_ACQUISITION_VERSION,
            "source_contract_version": stats_source_contract_version(
                command, request.requested_through_date.year
            ),
            "pybaseball_parity_version": PYBASEBALL_REQUIRED_VERSION,
            "command": command.value,
            "mode": request.mode.value,
            "requested_through_date": request.requested_through_date,
        }
        if request.source_stats_run_id is not None:
            values["source_stats_run_id"] = request.source_stats_run_id
        return _checksum(values)

    def _checkpoint(self, context: _RunContext, dataset_key: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stats_checkpoints WHERE stats_run_id=? AND dataset_key=?",
                (context.stats_run_id, dataset_key),
            ).fetchone()
            parent = connection.execute(
                "SELECT scope_key FROM stats_ingestion_runs WHERE stats_run_id=?",
                (context.stats_run_id,),
            ).fetchone()
        if parent is None:
            raise AcquisitionExecutionError("checkpoint parent run disappeared")
        parent_scope = str(parent["scope_key"])
        now = _aware_utc(self.clock())
        if row is None:
            checkpoint_id = self.id_factory("checkpoint")
            self.repository.create_checkpoint(
                {
                    "checkpoint_id": checkpoint_id,
                    "stats_run_id": context.stats_run_id,
                    "dataset_key": dataset_key,
                    "scope_key": parent_scope,
                    "created_at": now,
                }
            )
            self.repository.transition_checkpoint(
                checkpoint_id, "running", transitioned_at=now
            )
            return checkpoint_id
        if str(row["scope_key"]) != parent_scope:
            raise AcquisitionResumeError(
                f"checkpoint {dataset_key!r} scope does not match its parent run"
            )
        checkpoint_id = str(row["checkpoint_id"])
        if row["status"] == "pending":
            self.repository.transition_checkpoint(
                checkpoint_id, "running", transitioned_at=now
            )
        return checkpoint_id

    def _verified_completed_checkpoint(
        self,
        context: _RunContext,
        dataset_key: str,
    ) -> tuple[dict[str, Any], dict[str, RawArtifact]] | None:
        checkpoint = self.repository.get_checkpoint(context.stats_run_id, dataset_key)
        if checkpoint is None or checkpoint["status"] in {"pending", "running"}:
            return None
        if checkpoint["status"] != "completed":
            raise AcquisitionResumeError(
                f"checkpoint {dataset_key!r} is terminal but not complete; start a new run"
            )
        raw_rows = self.repository.list_checkpoint_raw_payloads(
            str(checkpoint["checkpoint_id"])
        )
        if not raw_rows:
            raise AcquisitionResumeError(
                f"completed checkpoint {dataset_key!r} has no raw checksum evidence"
            )
        artifacts: dict[str, RawArtifact] = {}
        for row in raw_rows:
            try:
                metadata = json.loads(str(row["metadata_json"]))
                relpath = Path(str(row["artifact_relpath"]))
                artifact_path = self.raw_store.root.joinpath(relpath)
                artifact = RawArtifact(
                    capture_id=str(row["source_capture_id"]),
                    provider=StatsProvider(str(row["provider"])),
                    endpoint_category=str(row["endpoint_category"]),
                    retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
                    content_type=str(row["content_type"]),
                    checksum_sha256=str(row["checksum_sha256"]),
                    size_bytes=int(metadata["size_bytes"]),
                    path=artifact_path,
                    metadata_path=artifact_path.with_suffix(".json"),
                    parent_checksum_sha256=(
                        str(metadata["parent_checksum_sha256"])
                        if metadata.get("parent_checksum_sha256") is not None
                        else None
                    ),
                )
                self.raw_store.read_verified(artifact)
            except Exception as exc:
                raise AcquisitionResumeError(
                    f"completed checkpoint {dataset_key!r} failed raw checksum verification"
                ) from exc
            artifacts[str(row["raw_payload_id"])] = artifact
            if (
                context.source_observed_at is None
                or artifact.retrieved_at > context.source_observed_at
            ):
                context.source_observed_at = artifact.retrieved_at
        return checkpoint, artifacts

    def _complete_checkpoint(
        self,
        checkpoint_id: str,
        *,
        seen: int,
        persisted: int,
        rejected: int = 0,
        source_observed_at: datetime,
        partial: bool = False,
        cursor_after: object | None = None,
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM stats_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None or row["status"] != "running":
            return
        self.repository.transition_checkpoint(
            checkpoint_id,
            "partial" if partial else "completed",
            transitioned_at=_aware_utc(self.clock()),
            observed_through=source_observed_at,
            complete_through=None if partial else source_observed_at,
            records_seen=seen,
            records_persisted=persisted,
            records_rejected=rejected,
            cursor_after=cursor_after,
        )

    def _persist_raw(
        self,
        context: _RunContext,
        checkpoint_id: str | None,
        artifact: RawArtifact,
    ) -> str:
        verified = self.raw_store.read_verified(artifact)
        if hashlib.sha256(verified).hexdigest() != artifact.checksum_sha256:
            raise AcquisitionExecutionError("raw provider checksum verification failed")
        try:
            relpath = artifact.path.resolve(strict=True).relative_to(
                self.raw_store.root.resolve(strict=True)
            )
        except ValueError as exc:
            raise AcquisitionExecutionError(
                "raw provider artifact escaped its configured root"
            ) from exc
        raw_payload_id = f"raw:{context.stats_run_id}:{artifact.capture_id}"
        self.repository.record_raw_payload_metadata(
            {
                "raw_payload_id": raw_payload_id,
                "stats_run_id": context.stats_run_id,
                "checkpoint_id": checkpoint_id,
                "provider": artifact.provider.value,
                "endpoint_category": artifact.endpoint_category,
                "source_capture_id": artifact.capture_id,
                "retrieved_at": artifact.retrieved_at,
                "content_type": artifact.content_type,
                "checksum_sha256": artifact.checksum_sha256,
                "artifact_relpath": relpath.as_posix(),
                "metadata": {
                    "size_bytes": artifact.size_bytes,
                    "parent_checksum_sha256": artifact.parent_checksum_sha256,
                },
            }
        )
        if context.source_observed_at is None or artifact.retrieved_at > context.source_observed_at:
            context.source_observed_at = artifact.retrieved_at
        return raw_payload_id

    def _team_identity(
        self,
        provider: str,
        source_team_id: str,
        observed_at: datetime,
    ) -> tuple[str, str | None]:
        cache_key = (provider, source_team_id)
        cached = self._team_identity_cache.get(cache_key)
        if cached is not None:
            return cached
        identity = resolve_team_identity(provider, source_team_id)
        canonical = identity.canonical_team_key
        if canonical is None and provider != "retrosheet":
            raise AcquisitionExecutionError(
                f"Unknown {provider} team identity {source_team_id!r}"
            )
        team_identity_id = f"team:{provider}:{source_team_id}"
        payload = {
            "provider": provider,
            "provider_team_id": source_team_id,
            "canonical_team_key": canonical,
        }
        current_name = str(canonical or source_team_id)
        identity_checksum = _checksum(payload)
        existing = self.repository.get_team_identity(provider, source_team_id)
        if existing is not None:
            expected_facts: dict[str, object] = {
                "team_identity_id": team_identity_id,
                "canonical_team_key": canonical,
                "current_name": current_name,
                "active": 1,
            }
            conflicting = [
                field
                for field, expected in expected_facts.items()
                if existing.get(field) != expected
            ]
            accepted_checksums = {
                identity_checksum,
                _checksum(
                    {
                        **payload,
                        "current_name": current_name,
                        "active": True,
                    }
                ),
            }
            if existing.get("identity_checksum") not in accepted_checksums:
                conflicting.append("identity_checksum")
            if conflicting:
                raise AcquisitionExecutionError(
                    "provider team identity conflicts with the stable canonical mapping: "
                    + ", ".join(sorted(conflicting))
                )
            result = team_identity_id, str(canonical) if canonical is not None else None
            self._team_identity_cache[cache_key] = result
            return result
        self.repository.upsert_team_identity(
            {
                "team_identity_id": team_identity_id,
                **payload,
                "current_name": current_name,
                "active": True,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "identity_checksum": identity_checksum,
            }
        )
        result = team_identity_id, str(canonical) if canonical is not None else None
        self._team_identity_cache[cache_key] = result
        return result

    def _player_identity(
        self,
        provider: str,
        source_player_id: str,
        observed_at: datetime,
        *,
        full_name: str | None = None,
    ) -> str:
        cache_key = (provider, source_player_id)
        cached = self._player_identity_cache.get(cache_key)
        if cached is not None:
            return cached
        player_identity_id = f"player:{provider}:{source_player_id}"
        existing = self.repository.get_player_identity(provider, source_player_id)
        if existing is not None:
            if str(existing["player_identity_id"]) != player_identity_id:
                raise AcquisitionExecutionError(
                    "provider player identity resolved to an unexpected stable key"
                )
            self._player_identity_cache[cache_key] = player_identity_id
            return player_identity_id
        name = (full_name or source_player_id).strip()
        payload = {
            "provider": provider,
            "provider_player_id": source_player_id,
            "full_name": name,
        }
        self.repository.upsert_player_identity(
            {
                "player_identity_id": player_identity_id,
                **payload,
                "active": True,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "identity_checksum": _checksum(payload),
            }
        )
        self._player_identity_cache[cache_key] = player_identity_id
        return player_identity_id

    def _stable_game_identity(
        self,
        *,
        provider: str,
        provider_game_id: str,
        official_date: date,
        home_team_identity_id: str,
        away_team_identity_id: str,
        observed_at: datetime,
    ) -> str:
        game_identity_id = f"game:{provider}:{provider_game_id}"
        identity = {
            "provider": provider,
            "provider_game_id": provider_game_id,
            "official_date": official_date.isoformat(),
            "home_team_identity_id": home_team_identity_id,
            "away_team_identity_id": away_team_identity_id,
        }
        identity_checksum = _checksum(identity)
        existing = self.repository.get_game_identity(provider, provider_game_id)
        if existing is not None:
            expected_facts: dict[str, object] = {
                "game_identity_id": game_identity_id,
                "season": official_date.year,
                "game_type": "R",
                "official_date": official_date.isoformat(),
                "home_team_identity_id": home_team_identity_id,
                "away_team_identity_id": away_team_identity_id,
                "identity_checksum": identity_checksum,
            }
            conflicting = [
                field
                for field, expected in expected_facts.items()
                if existing.get(field) != expected
            ]
            if conflicting:
                raise AcquisitionExecutionError(
                    "provider game identity conflicts with the stable canonical mapping: "
                    + ", ".join(sorted(conflicting))
                )
            return game_identity_id
        self.repository.upsert_game_identity(
            {
                "game_identity_id": game_identity_id,
                **identity,
                "season": official_date.year,
                "game_type": "R",
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "identity_checksum": identity_checksum,
            }
        )
        return game_identity_id

    @staticmethod
    def _mlbam_canonical_player_id(source_player_id: str) -> str:
        normalized = source_player_id.strip()
        if not normalized.isdigit():
            raise AcquisitionExecutionError("MLBAM player identity is invalid")
        numeric = int(normalized)
        if numeric <= 0:
            raise AcquisitionExecutionError("MLBAM player identity is invalid")
        return f"mlb-player:mlbam:{numeric}"

    def _map_baseball_reference_player(
        self,
        context: _RunContext,
        *,
        source_player_id: str,
        player_identity_id: str,
        full_name: str,
        artifact: RawArtifact,
    ) -> str:
        cached = self._baseball_reference_player_mapping_cache.get(source_player_id)
        if cached is not None:
            return cached
        expected = self._mlbam_canonical_player_id(source_player_id)
        resolved = self.repository.resolve_canonical_player(
            "baseball_reference", source_player_id
        )
        if resolved is not None:
            observed = str(resolved["canonical_player_id"])
            if observed != expected:
                raise AcquisitionExecutionError(
                    "Baseball-Reference MLBAM identity conflicts with its canonical player"
                )
            self._baseball_reference_player_mapping_cache[source_player_id] = observed
            return observed
        canonical_facts = {
            "namespace": "mlbam",
            "provider_player_id": source_player_id,
            "display_name": full_name,
        }
        if self.repository.get_canonical_player(expected) is None:
            self.repository.record_canonical_player(
                {
                    "canonical_player_id": expected,
                    "created_stats_run_id": context.stats_run_id,
                    "display_name": full_name,
                    "created_at": artifact.retrieved_at,
                    "canonical_checksum": _checksum(canonical_facts),
                }
            )
        self.repository.record_player_identifier_mapping(
            {
                "mapping_id": self.id_factory("player_mapping"),
                "stats_run_id": context.stats_run_id,
                "canonical_player_id": expected,
                "player_identity_id": player_identity_id,
                "mapping_method": "exact_id",
                "verification_status": "verified",
                "source_version": "baseball-reference-daily",
                "adapter_version": STATS_ACQUISITION_VERSION,
                "observed_at": artifact.retrieved_at,
                "provenance": {
                    "identity_namespace": "mlbam",
                    "source_field": "mlbID",
                    "source_capture_id": artifact.capture_id,
                    "source_capture_checksum": artifact.checksum_sha256,
                },
                "source_checksum": _checksum(
                    {
                        "provider": "baseball_reference",
                        "provider_player_id": source_player_id,
                        "canonical_player_id": expected,
                        "source_capture_checksum": artifact.checksum_sha256,
                    }
                ),
            }
        )
        resolved = self.repository.resolve_canonical_player(
            "baseball_reference", source_player_id
        )
        if resolved is None or str(resolved["canonical_player_id"]) != expected:
            raise AcquisitionExecutionError(
                "Baseball-Reference canonical player mapping did not reconcile"
            )
        self._baseball_reference_player_mapping_cache[source_player_id] = expected
        return expected

    def _map_statcast_player(
        self,
        context: _RunContext,
        *,
        source_player_id: str,
        player_identity_id: str,
        artifact: RawArtifact,
    ) -> str | None:
        if source_player_id in self._statcast_player_mapping_cache:
            return self._statcast_player_mapping_cache[source_player_id]
        expected = self._mlbam_canonical_player_id(source_player_id)
        resolved = self.repository.resolve_canonical_player("statcast", source_player_id)
        if resolved is not None:
            observed = str(resolved["canonical_player_id"])
            if observed != expected:
                raise AcquisitionExecutionError(
                    "Statcast MLBAM identity conflicts with its canonical player"
                )
            self._statcast_player_mapping_cache[source_player_id] = observed
            return observed
        baseball_reference = self.repository.resolve_canonical_player(
            "baseball_reference", source_player_id
        )
        if baseball_reference is None:
            self._statcast_player_mapping_cache[source_player_id] = None
            return None
        if str(baseball_reference["canonical_player_id"]) != expected:
            raise AcquisitionExecutionError(
                "Cross-provider MLBAM identity conflicts with its canonical player"
            )
        self.repository.record_player_identifier_mapping(
            {
                "mapping_id": self.id_factory("player_mapping"),
                "stats_run_id": context.stats_run_id,
                "canonical_player_id": expected,
                "player_identity_id": player_identity_id,
                "mapping_method": "exact_id",
                "verification_status": "verified",
                "source_version": "statcast-search-csv",
                "adapter_version": STATS_ACQUISITION_VERSION,
                "observed_at": artifact.retrieved_at,
                "provenance": {
                    "identity_namespace": "mlbam",
                    "source_field": "batter_or_pitcher",
                    "source_capture_id": artifact.capture_id,
                    "source_capture_checksum": artifact.checksum_sha256,
                },
                "source_checksum": _checksum(
                    {
                        "provider": "statcast",
                        "provider_player_id": source_player_id,
                        "canonical_player_id": expected,
                        "source_capture_checksum": artifact.checksum_sha256,
                    }
                ),
            }
        )
        resolved = self.repository.resolve_canonical_player("statcast", source_player_id)
        if resolved is None or str(resolved["canonical_player_id"]) != expected:
            raise AcquisitionExecutionError(
                "Statcast canonical player mapping did not reconcile"
            )
        self._statcast_player_mapping_cache[source_player_id] = expected
        return expected

    def _load_statcast_player_mapping_inventory(self) -> None:
        if self._statcast_player_mapping_inventory_loaded:
            return
        mappings = self.repository.list_verified_canonical_player_mappings(
            "statcast"
        )
        for source_player_id, canonical in mappings.items():
            canonical_player_id = str(canonical["canonical_player_id"])
            cached = self._statcast_player_mapping_cache.get(source_player_id)
            if cached is not None and cached != canonical_player_id:
                raise AcquisitionExecutionError(
                    "Statcast player mapping inventory conflicts with the run cache"
                )
            self._statcast_player_mapping_cache[source_player_id] = (
                canonical_player_id
            )
        self._statcast_player_mapping_inventory_loaded = True

    def _raw_artifact_from_record(self, row: Mapping[str, Any]) -> RawArtifact:
        try:
            metadata = json.loads(str(row["metadata_json"]))
            if not isinstance(metadata, Mapping):
                raise ValueError("raw metadata_json must be an object")
            artifact_path = self.raw_store.root.joinpath(
                Path(str(row["artifact_relpath"]))
            )
            artifact = self.raw_store.load_verified_artifact(
                artifact_path.with_suffix(".json")
            )
            expected = {
                "capture_id": str(row["source_capture_id"]),
                "provider": StatsProvider(str(row["provider"])),
                "endpoint_category": str(row["endpoint_category"]),
                "retrieved_at": datetime.fromisoformat(str(row["retrieved_at"])),
                "content_type": str(row["content_type"]),
                "checksum_sha256": str(row["checksum_sha256"]),
                "size_bytes": int(metadata["size_bytes"]),
                "parent_checksum_sha256": (
                    str(metadata["parent_checksum_sha256"])
                    if metadata.get("parent_checksum_sha256") is not None
                    else None
                ),
            }
            observed = {
                "capture_id": artifact.capture_id,
                "provider": artifact.provider,
                "endpoint_category": artifact.endpoint_category,
                "retrieved_at": artifact.retrieved_at,
                "content_type": artifact.content_type,
                "checksum_sha256": artifact.checksum_sha256,
                "size_bytes": artifact.size_bytes,
                "parent_checksum_sha256": artifact.parent_checksum_sha256,
            }
            if observed != expected:
                raise ValueError("database raw metadata disagrees with artifact metadata")
            if artifact.path.relative_to(
                self.raw_store.root.resolve(strict=True)
            ).as_posix() != str(row["artifact_relpath"]).replace("\\", "/"):
                raise ValueError("database raw path disagrees with artifact metadata")
            return artifact
        except Exception as exc:
            raise AcquisitionExecutionError(
                "player register raw evidence failed immutable verification"
            ) from exc

    def _source_player_register_evidence(
        self,
        request: AcquisitionRequest,
    ) -> tuple[
        Mapping[str, Any],
        Mapping[str, Any],
        Mapping[str, Any],
        RawArtifact,
        PlayerRegisterDataset,
    ]:
        source_stats_run_id = request.source_stats_run_id
        if source_stats_run_id is None:
            raise AcquisitionConfigurationError(
                "reconcile-player-register-pin requires source_stats_run_id"
            )
        source_run = self.repository.get_ingestion_run(source_stats_run_id)
        if source_run is None:
            raise AcquisitionExecutionError(
                "source player-register stats run does not exist"
            )
        if (
            source_run["provider"] != "retrosheet"
            or source_run["status"] not in {"completed", "completed_with_warnings"}
            or str(source_run["requested_through_date"])
            != request.requested_through_date.isoformat()
            or source_run["scope_key"]
            != f"regular-season:{request.requested_through_date.year}"
            or source_run["source_version"]
            != stats_source_contract_version(
                AcquisitionCommand.BOOTSTRAP_RETROSHEET,
                request.requested_through_date.year,
            )
        ):
            raise AcquisitionExecutionError(
                "source player-register stats run is not a matching terminal "
                "Retrosheet bootstrap"
            )
        checkpoint = self.repository.get_checkpoint(
            source_stats_run_id, "pybaseball_player_identifier_register"
        )
        if checkpoint is None or checkpoint["status"] != "completed":
            raise AcquisitionExecutionError(
                "source player-register checkpoint is not completed"
            )
        raw_rows = self.repository.list_checkpoint_raw_payloads(
            str(checkpoint["checkpoint_id"])
        )
        if len(raw_rows) != 1:
            raise AcquisitionExecutionError(
                "source player-register checkpoint must link exactly one raw archive"
            )
        raw_row = raw_rows[0]
        source_artifact = self._raw_artifact_from_record(raw_row)
        if source_artifact.endpoint_category != LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY:
            raise AcquisitionExecutionError(
                "source player-register checkpoint is not the recognized legacy "
                "floating-endpoint capture"
            )
        source_dataset = PybaseballPlayerRegisterProvider(
            self.transport
        ).parse_artifact(source_artifact)
        cursor = self._checkpoint_cursor(checkpoint)
        try:
            if (
                cursor["contract"]
                != "DSE_PYBASEBALL_PLAYER_REGISTER_CHECKPOINT_V1"
                or cursor["adapter_version"]
                != PYBASEBALL_REGISTER_ADAPTER_VERSION
                or cursor["pybaseball_version"] != PYBASEBALL_REQUIRED_VERSION
                or cursor["checksum"] != source_artifact.checksum_sha256
                or int(cursor["mapping_count"]) != len(source_dataset.mappings)
                or int(cursor["source_member_count"])
                != source_dataset.source_member_count
                or int(cursor["source_row_count"])
                != source_dataset.source_row_count
                or int(checkpoint["records_seen"])
                != source_dataset.source_row_count
                or int(checkpoint["records_persisted"])
                != len(source_dataset.mappings)
                or int(checkpoint["records_rejected"]) != 0
                or cursor.get("resource_revision") not in {None, ""}
                or cursor.get("resource_identity")
                not in {
                    None,
                    "",
                    "https://codeload.github.com/chadwickbureau/register/zip/"
                    "refs/heads/master",
                }
                or cursor.get("raw_endpoint_category")
                not in {None, "", LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY}
            ):
                raise ValueError("legacy player-register checkpoint is inconsistent")
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionExecutionError(
                "source player-register checkpoint is not recognized legacy evidence"
            ) from exc
        return source_run, checkpoint, raw_row, source_artifact, source_dataset

    def _reconcile_player_register_pin(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        (
            source_run,
            source_checkpoint,
            source_raw_row,
            source_artifact,
            source_dataset,
        ) = self._source_player_register_evidence(request)
        checkpoint_id = self._checkpoint(
            context, PLAYER_REGISTER_RECONCILIATION_DATASET_KEY
        )
        existing_raw_rows = self.repository.list_checkpoint_raw_payloads(checkpoint_id)
        if len(existing_raw_rows) > 1:
            raise AcquisitionResumeError(
                "player-register reconciliation checkpoint links multiple pinned archives"
            )
        provider = PybaseballPlayerRegisterProvider(self.transport)
        pinned_raw_row: Mapping[str, Any]
        if existing_raw_rows:
            pinned_raw_row = existing_raw_rows[0]
            pinned_artifact = self._raw_artifact_from_record(pinned_raw_row)
            pinned_dataset = provider.parse_artifact(pinned_artifact)
        else:
            pinned_dataset = provider.collect()
            pinned_artifact = pinned_dataset.raw
            pinned_raw_id = self._persist_raw(
                context, checkpoint_id, pinned_artifact
            )
            persisted_raw_row = self.repository.get_raw_payload_metadata(pinned_raw_id)
            if persisted_raw_row is None:
                raise AcquisitionExecutionError(
                    "pinned player-register raw metadata disappeared"
                )
            pinned_raw_row = persisted_raw_row
        if (
            pinned_artifact.endpoint_category
            != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
        ):
            raise AcquisitionExecutionError(
                "pinned player-register capture used an unexpected endpoint category"
            )
        source_inventory = build_register_inventory(source_dataset, self.raw_store)
        pinned_inventory = build_register_inventory(pinned_dataset, self.raw_store)
        if not inventories_match(source_inventory, pinned_inventory):
            raise AcquisitionExecutionError(
                "legacy and exact-pinned player-register inventories differ"
            )
        source_cursor = self._checkpoint_cursor(source_checkpoint)
        cursor = {
            "contract": PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
            "source_stats_run_id": str(source_run["stats_run_id"]),
            "source_run_configuration_checksum": str(
                source_run["configuration_checksum"]
            ),
            "source_checkpoint_id": str(source_checkpoint["checkpoint_id"]),
            "source_checkpoint_cursor_sha256": register_reconciliation_checksum(
                source_cursor
            ),
            "source_raw_payload_id": str(source_raw_row["raw_payload_id"]),
            "source_raw_checksum_sha256": source_artifact.checksum_sha256,
            "source_raw_endpoint_category": source_artifact.endpoint_category,
            "pinned_stats_run_id": context.stats_run_id,
            "pinned_raw_payload_id": str(pinned_raw_row["raw_payload_id"]),
            "pinned_raw_checksum_sha256": pinned_artifact.checksum_sha256,
            "pinned_raw_endpoint_category": pinned_artifact.endpoint_category,
            "pinned_resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
            "pinned_resource_identity": PYBASEBALL_REGISTER_URL,
            "source_inventory": source_inventory,
            "pinned_inventory": pinned_inventory,
            "equivalent": True,
        }
        expected_seen = source_dataset.source_row_count + pinned_dataset.source_row_count
        self._complete_checkpoint(
            checkpoint_id,
            seen=expected_seen,
            persisted=1,
            source_observed_at=pinned_artifact.retrieved_at,
            cursor_after=cursor,
        )
        completed_checkpoint = self.repository.get_checkpoint(
            context.stats_run_id, PLAYER_REGISTER_RECONCILIATION_DATASET_KEY
        )
        if (
            completed_checkpoint is None
            or completed_checkpoint["status"] != "completed"
            or self._checkpoint_cursor(completed_checkpoint) != cursor
            or int(completed_checkpoint["records_seen"]) != expected_seen
            or int(completed_checkpoint["records_persisted"]) != 1
            or int(completed_checkpoint["records_rejected"]) != 0
        ):
            raise AcquisitionResumeError(
                "player-register reconciliation checkpoint failed exact completion validation"
            )
        completed_at = _aware_utc(self.clock())
        self.repository.transition_ingestion_run(
            context.stats_run_id,
            "completed",
            transitioned_at=completed_at,
            source_observed_at=pinned_artifact.retrieved_at,
        )
        self.database.transition_run(
            context.run_id,
            RunStatus.COMPLETED,
            transitioned_at=completed_at.isoformat(),
        )
        raw_member_inventory = pinned_inventory["member_inventory"]
        raw_semantic_inventory = pinned_inventory["semantic_inventory"]
        if not isinstance(raw_member_inventory, Mapping) or not isinstance(
            raw_semantic_inventory, Mapping
        ):
            raise AcquisitionExecutionError(
                "pinned player-register inventory is malformed"
            )
        member_inventory = dict(raw_member_inventory)
        semantic_inventory = dict(raw_semantic_inventory)
        return AcquisitionResult(
            command=context.command,
            status="completed",
            requested_through_date=request.requested_through_date,
            run_id=context.run_id,
            stats_run_id=context.stats_run_id,
            counts={
                "source_archive_bytes": source_artifact.size_bytes,
                "pinned_archive_bytes": pinned_artifact.size_bytes,
                "source_rows": source_dataset.source_row_count,
                "pinned_rows": pinned_dataset.source_row_count,
                "mapping_count": len(pinned_dataset.mappings),
                "member_count": int(member_inventory["member_count"]),
                "network_requests": self.transport.attempts,
            },
            completeness=None,
            warnings=(),
            report_path=self.report_path,
            dry_run=False,
            source_observed_at=pinned_artifact.retrieved_at,
            validation_details={
                "contract": PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
                "source_stats_run_id": str(source_run["stats_run_id"]),
                "source_raw_checksum_sha256": source_artifact.checksum_sha256,
                "pinned_raw_checksum_sha256": pinned_artifact.checksum_sha256,
                "pinned_resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
                "member_inventory_sha256": member_inventory["inventory_sha256"],
                "semantic_mapping_sha256": semantic_inventory["semantic_sha256"],
                "equivalent": True,
            },
        )

    def _bootstrap_retrosheet(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        register_checkpoint = self._checkpoint(
            context, "pybaseball_player_identifier_register"
        )
        completed_register = self._verified_completed_checkpoint(
            context, "pybaseball_player_identifier_register"
        )
        register_provider = PybaseballPlayerRegisterProvider(self.transport)
        if completed_register is not None:
            register_artifacts = tuple(completed_register[1].values())
            if len(register_artifacts) != 1:
                raise AcquisitionResumeError(
                    "completed player register checkpoint must reference one raw archive"
                )
            player_register = register_provider.parse_artifact(
                register_artifacts[0]
            )
            try:
                register_cursor = json.loads(
                    str(completed_register[0]["cursor_after_json"])
                )
                if (
                    register_cursor.get("contract")
                    != "DSE_PYBASEBALL_PLAYER_REGISTER_CHECKPOINT_V1"
                    or register_cursor.get("resource_revision")
                    != PYBASEBALL_REGISTER_COMMIT_SHA
                    or register_cursor.get("resource_identity")
                    != PYBASEBALL_REGISTER_URL
                    or register_cursor.get("raw_endpoint_category")
                    != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
                    or player_register.raw.endpoint_category
                    != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
                    or register_cursor.get("checksum")
                    != player_register.raw.checksum_sha256
                    or int(register_cursor.get("mapping_count", -1))
                    != len(player_register.mappings)
                ):
                    raise ValueError("player register cursor mismatch")
            except (TypeError, ValueError) as exc:
                raise AcquisitionResumeError(
                    "completed player register checkpoint does not reconcile"
                ) from exc
        else:
            player_register = register_provider.collect()
            self._persist_raw(
                context, register_checkpoint, player_register.raw
            )
            self._complete_checkpoint(
                register_checkpoint,
                seen=player_register.source_row_count,
                persisted=len(player_register.mappings),
                source_observed_at=player_register.raw.retrieved_at,
                cursor_after={
                    "contract": "DSE_PYBASEBALL_PLAYER_REGISTER_CHECKPOINT_V1",
                    "adapter_version": PYBASEBALL_REGISTER_ADAPTER_VERSION,
                    "pybaseball_version": PYBASEBALL_REQUIRED_VERSION,
                    "resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
                    "resource_identity": PYBASEBALL_REGISTER_URL,
                    "raw_endpoint_category": (
                        PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
                    ),
                    "checksum": player_register.raw.checksum_sha256,
                    "mapping_count": len(player_register.mappings),
                    "source_member_count": player_register.source_member_count,
                    "source_row_count": player_register.source_row_count,
                },
            )
        checkpoint = self._checkpoint(context, "retrosheet_regular_season")
        completed = self._verified_completed_checkpoint(
            context, "retrosheet_regular_season"
        )
        if completed is not None:
            dataset, raw_ids = self._restore_retrosheet_dataset(
                completed[0], completed[1]
            )
        else:
            dataset = RetrosheetProvider(
                self.transport, self.raw_store
            ).collect_regular_season_major_league()
            raw_ids = {
                "archive": self._persist_raw(context, checkpoint, dataset.archive)
            }
            for name, member in dataset.members.items():
                raw_ids[name] = self._persist_raw(context, checkpoint, member.raw)
            self._complete_checkpoint(
                checkpoint,
                seen=sum(member.row_count for member in dataset.members.values()),
                persisted=1 + len(dataset.members),
                source_observed_at=dataset.archive.retrieved_at,
                cursor_after=self._retrosheet_dataset_cursor(dataset, raw_ids),
            )

        (
            persisted,
            states,
            excluded_count,
            child_checkpoints,
            gid_team_role_mismatches,
        ) = self._persist_retrosheet_dataset(
            context,
            dataset,
            raw_ids,
            request.requested_through_date,
            player_register=player_register,
        )
        completeness = calculate_completeness(request.requested_through_date, states)
        counts = Counter(
            {
                "raw_payloads": 2 + len(dataset.members),
                "provider_rows": sum(
                    member.row_count for member in dataset.members.values()
                ),
                "persisted_records": persisted,
                "games": len(states),
                "excluded_source_rows": excluded_count,
                "retrosheet_child_checkpoints": child_checkpoints,
                "retrosheet_gid_team_role_mismatches": gid_team_role_mismatches,
                "player_identifier_register_rows": (
                    player_register.source_row_count
                ),
                "player_identifier_exact_mappings": len(
                    player_register.mappings
                ),
            }
        )
        warnings = (
            ("retrosheet_gid_team_role_mismatch",)
            if gid_team_role_mismatches
            else ()
        )
        return self._finalize_run(context, request, completeness, counts, warnings)

    @staticmethod
    def _retrosheet_dataset_cursor(
        dataset: RetrosheetDataset, raw_ids: Mapping[str, str]
    ) -> dict[str, object]:
        return {
            "contract": "DSE_RETROSHEET_RAW_DATASET_V2",
            "archive_checksum": dataset.archive.checksum_sha256,
            "raw_ids": dict(raw_ids),
            "members": {
                name: {
                    "columns": list(member.columns),
                    "row_count": member.row_count,
                    "normalized_row_count": member.normalized_row_count,
                    "season_row_counts": {
                        str(season): count
                        for season, count in member.season_row_counts.items()
                    },
                    "checksum": member.raw.checksum_sha256,
                }
                for name, member in sorted(dataset.members.items())
            },
        }

    @staticmethod
    def _restore_retrosheet_dataset(
        checkpoint: Mapping[str, object],
        artifacts: Mapping[str, RawArtifact],
    ) -> tuple[RetrosheetDataset, dict[str, str]]:
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"]))
            if cursor["contract"] != "DSE_RETROSHEET_RAW_DATASET_V2":
                raise ValueError("unsupported raw dataset contract")
            raw_ids = {
                str(key): str(value)
                for key, value in dict(cursor["raw_ids"]).items()
            }
            archive = artifacts[raw_ids["archive"]]
            if archive.checksum_sha256 != str(cursor["archive_checksum"]):
                raise ValueError("archive checksum changed")
            member_metadata = dict(cursor["members"])
            members: dict[str, RetrosheetCsvMember] = {}
            for name in RETROSHEET_SEVEN_MEMBERS:
                metadata = dict(member_metadata[name])
                artifact = artifacts[raw_ids[name]]
                if artifact.checksum_sha256 != str(metadata["checksum"]):
                    raise ValueError(f"{name} checksum changed")
                members[name] = RetrosheetCsvMember(
                    name=name,
                    columns=tuple(str(value) for value in metadata["columns"]),
                    row_count=int(metadata["row_count"]),
                    normalized_row_count=int(metadata["normalized_row_count"]),
                    raw=artifact,
                    season_row_counts={
                        int(season): int(count)
                        for season, count in dict(
                            metadata["season_row_counts"]
                        ).items()
                    },
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionResumeError(
                "completed Retrosheet raw checkpoint lacks verified dataset metadata"
            ) from exc
        return (
            RetrosheetDataset(
                scope=MLB_REGULAR_SEASON_SCOPE,
                archive=archive,
                members=members,
                response_headers={},
                attempts=0,
            ),
            raw_ids,
        )

    def _retrosheet_analytical_inventory(
        self, through_season: int
    ) -> dict[str, object]:
        """Fingerprint the durable Retrosheet facts used by analytics."""

        game_scope = (
            "JOIN stats_game_identities AS game "
            "ON game.game_identity_id={game_column} "
            "WHERE game.provider='retrosheet' AND game.season<=? "
        )
        queries = {
            "team_identities": (
                "SELECT team_identity_id,provider_team_id,canonical_team_key,"
                "current_name,active,identity_checksum "
                "FROM stats_team_identities WHERE provider='retrosheet' "
                "ORDER BY team_identity_id",
                False,
            ),
            "player_identities": (
                "SELECT player_identity_id,provider_player_id,full_name,"
                "primary_position,bats,throws,active,identity_checksum "
                "FROM stats_player_identities WHERE provider='retrosheet' "
                "ORDER BY player_identity_id",
                False,
            ),
            "player_identifier_mappings": (
                "SELECT mapping.player_identity_id,mapping.canonical_player_id,"
                "mapping.mapping_method,mapping.verification_status,"
                "mapping.source_version,mapping.adapter_version,"
                "mapping.provenance_json,mapping.source_checksum "
                "FROM stats_player_identifier_mappings AS mapping "
                "JOIN stats_player_identities AS player "
                "ON player.player_identity_id=mapping.player_identity_id "
                "WHERE player.provider='retrosheet' "
                "ORDER BY mapping.player_identity_id,mapping.canonical_player_id,"
                "mapping.source_checksum",
                False,
            ),
            "games": (
                "SELECT game_identity_id,provider_game_id,season,game_type,"
                "official_date,home_team_identity_id,away_team_identity_id,"
                "identity_checksum FROM stats_game_identities "
                "WHERE provider='retrosheet' AND season<=? "
                "ORDER BY game_identity_id",
                True,
            ),
            "game_status_observations": (
                "SELECT status.game_identity_id,status.revision_number,"
                "status.normalized_checksum,status.source_checksum "
                "FROM stats_game_status_observations AS status "
                + game_scope.format(game_column="status.game_identity_id")
                + "ORDER BY status.game_identity_id,status.revision_number",
                True,
            ),
            "team_game_snapshots": (
                "SELECT snapshot.game_identity_id,snapshot.team_identity_id,"
                "snapshot.side,snapshot.snapshot_kind,snapshot.revision_number,"
                "snapshot.normalized_checksum,snapshot.source_checksum "
                "FROM stats_game_team_snapshots AS snapshot "
                + game_scope.format(game_column="snapshot.game_identity_id")
                + "ORDER BY snapshot.game_identity_id,snapshot.team_identity_id,"
                "snapshot.side,snapshot.snapshot_kind,snapshot.revision_number",
                True,
            ),
            "player_game_snapshots": (
                "SELECT snapshot.game_identity_id,snapshot.team_identity_id,"
                "snapshot.player_identity_id,snapshot.role,"
                "snapshot.source_row_key,snapshot.position_code,"
                "snapshot.source_stint_key,snapshot.revision_number,"
                "snapshot.normalized_checksum,snapshot.source_checksum "
                "FROM stats_game_player_snapshots AS snapshot "
                + game_scope.format(game_column="snapshot.game_identity_id")
                + "ORDER BY snapshot.game_identity_id,snapshot.team_identity_id,"
                "snapshot.player_identity_id,snapshot.role,"
                "snapshot.source_row_key,snapshot.revision_number",
                True,
            ),
            "lineup_snapshots": (
                "SELECT lineup.lineup_snapshot_id,lineup.game_identity_id,"
                "lineup.team_identity_id,lineup.lineup_state,"
                "lineup.source_checksum FROM stats_lineup_snapshots AS lineup "
                + game_scope.format(game_column="lineup.game_identity_id")
                + "ORDER BY lineup.lineup_snapshot_id",
                True,
            ),
            "lineup_entries": (
                "SELECT entry.lineup_snapshot_id,entry.player_identity_id,"
                "entry.batting_order,entry.position_code,entry.lineup_role,"
                "entry.entry_json FROM stats_lineup_entries AS entry "
                "JOIN stats_lineup_snapshots AS lineup "
                "ON lineup.lineup_snapshot_id=entry.lineup_snapshot_id "
                + game_scope.format(game_column="lineup.game_identity_id")
                + "ORDER BY entry.lineup_snapshot_id,entry.player_identity_id,"
                "entry.lineup_role,COALESCE(entry.batting_order,0)",
                True,
            ),
            "play_identities": (
                "SELECT play.play_identity_id,play.game_identity_id,"
                "play.provider_play_id,play.at_bat_index "
                "FROM stats_play_identities AS play "
                + game_scope.format(game_column="play.game_identity_id")
                + "ORDER BY play.play_identity_id",
                True,
            ),
            "play_revisions": (
                "SELECT revision.play_identity_id,revision.revision_number,"
                "revision.revision_kind,revision.source_checksum,"
                "revision.play_json FROM stats_play_revisions AS revision "
                "JOIN stats_play_identities AS play "
                "ON play.play_identity_id=revision.play_identity_id "
                + game_scope.format(game_column="play.game_identity_id")
                + "ORDER BY revision.play_identity_id,revision.revision_number",
                True,
            ),
        }
        if tuple(queries) != RETROSHEET_ANALYTICAL_INVENTORY_TABLES:
            raise AcquisitionExecutionError(
                "Retrosheet analytical inventory table contract drifted"
            )
        tables: dict[str, dict[str, object]] = {}
        with self.database.connect() as connection:
            for table_name, (query, scoped) in queries.items():
                digest = hashlib.sha256()
                count = 0
                parameters = (through_season,) if scoped else ()
                for row in connection.execute(query, parameters):
                    encoded = json.dumps(
                        list(row),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                    digest.update(encoded)
                    digest.update(b"\n")
                    count += 1
                tables[table_name] = {
                    "row_count": count,
                    "content_sha256": digest.hexdigest(),
                }
        return {
            "contract": RETROSHEET_ANALYTICAL_INVENTORY_CONTRACT,
            "through_season": through_season,
            "tables": tables,
        }

    def _persist_retrosheet_dataset(
        self,
        context: _RunContext,
        dataset: RetrosheetDataset,
        raw_ids: Mapping[str, str],
        requested_through_date: date,
        *,
        player_register: PlayerRegisterDataset,
    ) -> tuple[int, list[GameAcquisitionState], int, int, int]:
        if dataset.scope != MLB_REGULAR_SEASON_SCOPE:
            raise AcquisitionExecutionError(
                "Retrosheet bootstrap requires the explicit major-league "
                "regular-season archive scope"
            )
        through_season = requested_through_date.year
        observed_at = dataset.archive.retrieved_at
        register_by_retrosheet = player_register.by_retrosheet_id

        def canonical_mapping(
            source_player_id: str,
        ) -> tuple[str, str, str, str, dict[str, object]]:
            exact = register_by_retrosheet.get(source_player_id.casefold())
            if exact is None:
                return (
                    f"retrosheet-player:{source_player_id}",
                    "source_declared",
                    "retrosheet-regular-player-id",
                    STATS_ACQUISITION_VERSION,
                    {
                        "identity_namespace": "retrosheet",
                        "mapping": "source_player_id",
                        "provider_player_id": source_player_id,
                    },
                )
            return (
                f"mlb-player:mlbam:{exact.mlbam_id}",
                "pybaseball_lookup",
                f"chadwick-register-sha256:{player_register.raw.checksum_sha256}",
                PYBASEBALL_REGISTER_ADAPTER_VERSION,
                {
                    "identity_namespace": "mlbam",
                    "mapping": "exact_key_retro_to_key_mlbam",
                    "key_retro": exact.retrosheet_id,
                    "key_mlbam": exact.mlbam_id,
                    "key_bbref": exact.baseball_reference_id,
                    "key_fangraphs": exact.fangraphs_id,
                    "source_member": exact.source_member,
                    "source_row_number": exact.source_row_number,
                    "source_capture_checksum": (
                        player_register.raw.checksum_sha256
                    ),
                    "source_resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
                    "source_resource_identity": PYBASEBALL_REGISTER_URL,
                    "pybaseball_version": PYBASEBALL_REQUIRED_VERSION,
                    "fuzzy_matching": False,
                },
            )
        completed_children: dict[tuple[str, int], bool] = {}
        progress: defaultdict[tuple[str, int], Counter[str]] = defaultdict(Counter)
        remaining_rows = {
            (member_name, season): count
            for member_name, member in dataset.members.items()
            for season, count in member.season_row_counts.items()
        }

        def note(
            member_name: str, season: int, *, rejected: bool = False
        ) -> bool:
            key = (member_name, season)
            if key not in completed_children:
                _, completed_child = self._retrosheet_child_checkpoint(
                    context,
                    member_name=member_name,
                    season=season,
                    raw_payload_id=raw_ids[member_name],
                    raw_checksum=dataset.members[member_name].raw.checksum_sha256,
                )
                completed_children[key] = completed_child
            remaining = remaining_rows.get(key)
            if remaining is None or remaining <= 0:
                raise AcquisitionExecutionError(
                    "Retrosheet member season row counts do not match raw input"
                )
            remaining_rows[key] = remaining - 1
            progress[key]["seen"] += 1
            progress[key]["rejected" if rejected else "persisted"] += 1
            return completed_children[key]

        def complete_season(member_name: str, season: int) -> None:
            key = (member_name, season)
            if remaining_rows.get(key) != 0:
                raise AcquisitionExecutionError(
                    "Retrosheet season checkpoint cannot complete before every "
                    "source row is processed"
                )
            counts = progress.get(key)
            if counts is None or completed_children[key]:
                return
            checkpoint_id, _ = self._retrosheet_child_checkpoint(
                context,
                member_name=member_name,
                season=season,
                raw_payload_id=raw_ids[member_name],
                raw_checksum=dataset.members[member_name].raw.checksum_sha256,
            )
            self._complete_checkpoint(
                checkpoint_id,
                seen=counts["seen"],
                persisted=counts["persisted"],
                rejected=counts["rejected"],
                source_observed_at=observed_at,
                cursor_after={
                    "contract": "DSE_RETROSHEET_MEMBER_SEASON_V2",
                    "member_name": member_name,
                    "season": season,
                    "source_row_count": dataset.members[
                        member_name
                    ].season_row_counts[season],
                    "raw_payload_id": raw_ids[member_name],
                    "raw_checksum": dataset.members[
                        member_name
                    ].raw.checksum_sha256,
                },
            )

        def complete_member(member_name: str) -> None:
            expected_seasons = dataset.members[member_name].season_row_counts
            for season in expected_seasons:
                complete_season(member_name, season)

        teams: dict[str, tuple[str, str | None]] = {}

        def team_identity(
            source_team_id: str, canonical_team_key: str | None
        ) -> tuple[str, str | None]:
            existing = teams.get(source_team_id)
            if existing is not None:
                if existing[1] != canonical_team_key:
                    raise AcquisitionExecutionError(
                        "Retrosheet team identity resolved inconsistently"
                    )
                return existing
            team_identity_id = f"team:retrosheet:{source_team_id}"
            current_name = canonical_team_key or source_team_id
            active = canonical_team_key is not None
            facts = {
                "provider": "retrosheet",
                "provider_team_id": source_team_id,
                "canonical_team_key": canonical_team_key,
                "current_name": current_name,
                "active": active,
            }
            self.repository.upsert_team_identity(
                {
                    "team_identity_id": team_identity_id,
                    **facts,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "identity_checksum": _checksum(facts),
                }
            )
            result = (team_identity_id, canonical_team_key)
            teams[source_team_id] = result
            return result

        excluded_batches: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
        excluded_count = 0

        def queue_excluded(excluded: RetrosheetExcludedRow) -> None:
            nonlocal excluded_count
            raw_id = raw_ids[excluded.member_name]
            source_row = dict(excluded.source_row)
            source_checksum = _checksum(source_row)
            source_date = self._retrosheet_row_date(source_row)
            source_row_id = self._retrosheet_source_row_id(
                excluded.member_name, excluded.source_key, source_row, source_checksum
            )
            classification = (
                "future"
                if excluded.reason == "after_through_season"
                else "non_regular_season"
            )
            excluded_batches[excluded.member_name].append(
                {
                    "excluded_row_id": self.id_factory("excluded"),
                    "stats_run_id": context.stats_run_id,
                    "raw_payload_id": raw_id,
                    "provider": "retrosheet",
                    "dataset_key": excluded.member_name,
                    "source_row_id": source_row_id,
                    "classification": classification,
                    "reason_code": excluded.reason,
                    "source_effective_date": source_date,
                    "observed_at": observed_at,
                    "source_row_checksum": source_checksum,
                    "details": {
                        "member_name": excluded.member_name,
                        "source_row": source_row,
                    },
                }
            )
            excluded_count += 1
            if len(excluded_batches[excluded.member_name]) >= 1000:
                self.repository.record_excluded_source_rows(
                    excluded_batches[excluded.member_name]
                )
                excluded_batches[excluded.member_name].clear()

        def flush_excluded_rows(member_name: str) -> None:
            if not excluded_batches[member_name]:
                return
            self.repository.record_excluded_source_rows(
                excluded_batches[member_name]
            )
            excluded_batches[member_name].clear()

        players: dict[str, str] = {}
        player_name_observations: defaultdict[
            str, defaultdict[int, Counter[str]]
        ] = defaultdict(lambda: defaultdict(Counter))
        mapping_member = dataset.members["allplayers.csv"]
        for row in mapping_member.iter_rows():
            normalized_mapping = normalize_retrosheet_player_mapping_row(
                row, through_season=through_season
            )
            if isinstance(normalized_mapping, RetrosheetExcludedRow):
                season = int(str(row.get("season", "0")))
                if not note("allplayers.csv", season, rejected=True):
                    queue_excluded(normalized_mapping)
                continue
            mapping = normalized_mapping
            completed_child = note("allplayers.csv", mapping.season)
            full_name = " ".join(
                value
                for value in (mapping.first_name, mapping.last_name)
                if value is not None
            ).strip() or mapping.provider_player_id
            player_name_observations[mapping.provider_player_id][mapping.season][
                full_name
            ] += 1
            players[mapping.provider_player_id] = (
                f"player:retrosheet:{mapping.provider_player_id}"
            )
            if mapping.team_provider_id is not None:
                team_identity(
                    mapping.team_provider_id, mapping.canonical_team_key
                )
            if completed_child:
                continue

        player_names: dict[str, str] = {}
        player_name_histories: dict[str, list[dict[str, object]]] = {}
        for source_player_id, seasons in sorted(player_name_observations.items()):
            latest_season = max(seasons)
            latest_names = seasons[latest_season]
            selected_name = min(
                latest_names,
                key=lambda name: (-latest_names[name], name),
            )
            player_names[source_player_id] = selected_name
            player_name_histories[source_player_id] = [
                {
                    "season": season,
                    "name": name,
                    "row_count": count,
                }
                for season, names in sorted(seasons.items())
                for name, count in sorted(names.items())
            ]

        with self.database.connect() as connection:
            existing_canonical_players = {
                str(row[0])
                for row in connection.execute(
                    "SELECT canonical_player_id FROM stats_canonical_players"
                ).fetchall()
            }
        player_identity_records: list[dict[str, object]] = []
        canonical_records: list[dict[str, object]] = []
        mapping_records: list[dict[str, object]] = []

        def flush_player_mappings() -> None:
            if player_identity_records:
                self.repository.upsert_player_identities(player_identity_records)
                player_identity_records.clear()
            if canonical_records:
                self.repository.record_canonical_players(canonical_records)
                existing_canonical_players.update(
                    str(record["canonical_player_id"])
                    for record in canonical_records
                )
                canonical_records.clear()
            if mapping_records:
                self.repository.record_player_identifier_mappings(mapping_records)
                mapping_records.clear()

        players_by_first_season: defaultdict[int, list[str]] = defaultdict(list)
        for source_player_id, seasons in player_name_observations.items():
            players_by_first_season[min(seasons)].append(source_player_id)
        flush_excluded_rows("allplayers.csv")
        for source_season in mapping_member.season_row_counts:
            child_key = ("allplayers.csv", source_season)
            if not completed_children[child_key]:
                for source_player_id in sorted(
                    players_by_first_season.get(source_season, [])
                ):
                    full_name = player_names[source_player_id]
                    facts = {
                        "provider": "retrosheet",
                        "provider_player_id": source_player_id,
                        "full_name": full_name,
                    }
                    player_identity_records.append(
                        {
                            "player_identity_id": players[source_player_id],
                            **facts,
                            "active": False,
                            "first_seen_at": observed_at,
                            "last_seen_at": observed_at,
                            "identity_checksum": _checksum(facts),
                        }
                    )
                    (
                        canonical_player_id,
                        mapping_method,
                        mapping_source_version,
                        mapping_adapter_version,
                        identifier_provenance,
                    ) = canonical_mapping(source_player_id)
                    name_history = player_name_histories[source_player_id]
                    mapping_facts = {
                        "provider_player_id": source_player_id,
                        "display_name": full_name,
                        "display_name_policy": (
                            "latest_season_most_frequent_then_lexicographic"
                        ),
                        "name_observations": name_history,
                        "identifier_mapping": identifier_provenance,
                    }
                    if canonical_player_id not in existing_canonical_players:
                        canonical_records.append(
                            {
                                "canonical_player_id": canonical_player_id,
                                "created_stats_run_id": context.stats_run_id,
                                "display_name": full_name,
                                "created_at": observed_at,
                                "canonical_checksum": _checksum(mapping_facts),
                            }
                        )
                    mapping_records.append(
                        {
                            "mapping_id": self.id_factory("player_mapping"),
                            "stats_run_id": context.stats_run_id,
                            "canonical_player_id": canonical_player_id,
                            "player_identity_id": players[source_player_id],
                            "mapping_method": mapping_method,
                            "verification_status": "verified",
                            "source_version": mapping_source_version,
                            "adapter_version": mapping_adapter_version,
                            "observed_at": observed_at,
                            "provenance": {
                                **mapping_facts,
                                "mapping": identifier_provenance["mapping"],
                            },
                            "source_checksum": _checksum(mapping_facts),
                        }
                    )
                    if len(player_identity_records) >= 1000:
                        flush_player_mappings()
                flush_player_mappings()
            complete_season("allplayers.csv", source_season)

        def ensure_player(source_player_id: str) -> str:
            existing = players.get(source_player_id)
            if existing is not None:
                return existing
            player_identity_id = f"player:retrosheet:{source_player_id}"
            facts = {
                "provider": "retrosheet",
                "provider_player_id": source_player_id,
                "full_name": source_player_id,
            }
            self.repository.upsert_player_identity(
                {
                    "player_identity_id": player_identity_id,
                    **facts,
                    "active": False,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "identity_checksum": _checksum(facts),
                }
            )
            (
                canonical_player_id,
                mapping_method,
                mapping_source_version,
                mapping_adapter_version,
                identifier_provenance,
            ) = canonical_mapping(source_player_id)
            canonical_facts = {
                "provider_player_id": source_player_id,
                "display_name": source_player_id,
                "identifier_mapping": identifier_provenance,
            }
            resolved = self.repository.resolve_canonical_player(
                "retrosheet", source_player_id
            )
            if resolved is None:
                self.repository.record_canonical_player(
                    {
                        "canonical_player_id": canonical_player_id,
                        "created_stats_run_id": context.stats_run_id,
                        "display_name": source_player_id,
                        "created_at": observed_at,
                        "canonical_checksum": _checksum(canonical_facts),
                    }
                )
                self.repository.record_player_identifier_mapping(
                    {
                        "mapping_id": self.id_factory("player_mapping"),
                        "stats_run_id": context.stats_run_id,
                        "canonical_player_id": canonical_player_id,
                        "player_identity_id": player_identity_id,
                        "mapping_method": mapping_method,
                        "verification_status": "verified",
                        "source_version": mapping_source_version,
                        "adapter_version": mapping_adapter_version,
                        "observed_at": observed_at,
                        "provenance": {
                            **identifier_provenance,
                        },
                        "source_checksum": _checksum(
                            {
                                "provider_player_id": source_player_id,
                                "identifier_mapping": identifier_provenance,
                            }
                        ),
                    }
                )
            elif str(resolved["canonical_player_id"]) != canonical_player_id:
                raise AcquisitionExecutionError(
                    "Retrosheet player identity has a conflicting canonical mapping"
                )
            players[source_player_id] = player_identity_id
            return player_identity_id

        games: dict[
            str, tuple[str, date, str, str, str, str]
        ] = {}
        states: list[GameAcquisitionState] = []
        game_identities: list[dict[str, object]] = []
        game_statuses: list[dict[str, object]] = []
        game_links: list[dict[str, object]] = []
        gid_team_role_mismatch_count = 0
        game_member = dataset.members["gameinfo.csv"]

        def flush_games() -> None:
            if not game_identities:
                return
            self.repository.upsert_game_identities(game_identities)
            self.repository.record_game_statuses(game_statuses)
            self.repository.link_raw_entities(game_links)
            game_identities.clear()
            game_statuses.clear()
            game_links.clear()

        for row in game_member.iter_rows():
            normalized_game = normalize_retrosheet_game_row(
                row, through_season=through_season
            )
            game_date, _, _ = parse_retrosheet_game_id(str(row.get("gid", "")))
            if isinstance(normalized_game, RetrosheetExcludedRow):
                if not note("gameinfo.csv", game_date.year, rejected=True):
                    queue_excluded(normalized_game)
                if remaining_rows[("gameinfo.csv", game_date.year)] == 0:
                    flush_games()
                    flush_excluded_rows("gameinfo.csv")
                    complete_season("gameinfo.csv", game_date.year)
                continue
            game: RetrosheetGame = normalized_game
            if not game.game_id_team_role_matches:
                gid_team_role_mismatch_count += 1
            completed_child = note("gameinfo.csv", game.game_date.year)
            home_id, _ = team_identity(
                game.home_team_provider_id, game.home_team_key
            )
            away_id, _ = team_identity(
                game.away_team_provider_id, game.away_team_key
            )
            game_identity_id = f"game:retrosheet:{game.provider_game_id}"
            game_value = (
                game_identity_id,
                game.game_date,
                home_id,
                away_id,
                game.home_team_provider_id,
                game.away_team_provider_id,
            )
            prior = games.get(game.provider_game_id)
            if prior is not None and prior != game_value:
                raise AcquisitionExecutionError(
                    "Duplicate Retrosheet game ID has conflicting facts"
                )
            games[game.provider_game_id] = game_value
            states.append(
                GameAcquisitionState(
                    game.provider_game_id,
                    game.game_date,
                    GameStatus.FINAL,
                    checksums_valid=True,
                    normalized=True,
                    validated=True,
                    source_complete=True,
                )
            )
            if completed_child:
                if remaining_rows[("gameinfo.csv", game.game_date.year)] == 0:
                    flush_games()
                    flush_excluded_rows("gameinfo.csv")
                    complete_season("gameinfo.csv", game.game_date.year)
                continue
            source_checksum = _checksum(dict(row))
            identity_payload = {
                "provider": "retrosheet",
                "provider_game_id": game.provider_game_id,
                "official_date": game.game_date,
                "home_team_identity_id": home_id,
                "away_team_identity_id": away_id,
                "season": game.game_date.year,
                "game_type": "R",
            }
            game_identities.append(
                {
                    "game_identity_id": game_identity_id,
                    **identity_payload,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "identity_checksum": _checksum(identity_payload),
                }
            )
            game_statuses.append(
                {
                    "stats_run_id": context.stats_run_id,
                    "game_identity_id": game_identity_id,
                    "raw_payload_id": raw_ids["gameinfo.csv"],
                    "provider_updated_at": None,
                    "retrieved_at": observed_at,
                    "source_checksum": source_checksum,
                    "abstract_state": "Final",
                    "detailed_state": "Retrosheet regular-season archive",
                    "status_code": "final",
                    "status": {
                        **dict(row),
                        "dse_team_role_provenance": {
                            "contract": "DSE_RETROSHEET_TEAM_ROLE_V1",
                            "away_team_field": "visteam",
                            "home_team_field": "hometeam",
                            "game_id_encoded_team_provider_id": (
                                game.game_id_encoded_team_provider_id
                            ),
                            "game_id_team_role_matches": (
                                game.game_id_team_role_matches
                            ),
                        },
                    },
                }
            )
            game_links.append(
                {
                    "raw_payload_id": raw_ids["gameinfo.csv"],
                    "link_role": "contains_game",
                    "game_identity_id": game_identity_id,
                }
            )
            if len(game_identities) >= 1000:
                flush_games()
            if remaining_rows[("gameinfo.csv", game.game_date.year)] == 0:
                flush_games()
                flush_excluded_rows("gameinfo.csv")
                complete_season("gameinfo.csv", game.game_date.year)
        flush_games()
        flush_excluded_rows("gameinfo.csv")
        complete_member("gameinfo.csv")

        regular_game_ids = set(games)
        latest_eligible_season = max(
            (game_value[1].year for game_value in games.values()), default=None
        )
        if latest_eligible_season != through_season:
            raise AcquisitionExecutionError(
                "Retrosheet archive has no eligible regular-season major-league "
                f"game in requested through-season {through_season}"
            )
        persisted = len(games)
        with self.database.connect() as connection:
            existing_lineups = {
                (
                    str(row["game_identity_id"]),
                    str(row["team_identity_id"]),
                    str(row["source_checksum"]),
                )
                for row in connection.execute(
                    "SELECT game_identity_id,team_identity_id,source_checksum "
                    "FROM stats_lineup_snapshots "
                    "WHERE game_identity_id LIKE 'game:retrosheet:%'"
                ).fetchall()
            }

        team_snapshot_batch: list[dict[str, object]] = []
        lineup_batch: list[
            tuple[dict[str, object], list[dict[str, object]]]
        ] = []

        def flush_team_rows() -> None:
            if team_snapshot_batch:
                self.repository.record_game_team_snapshots(team_snapshot_batch)
                team_snapshot_batch.clear()
            if lineup_batch:
                self.repository.record_lineup_snapshots(lineup_batch)
                lineup_batch.clear()

        for row in dataset.members["teamstats.csv"].iter_rows():
            team_game_date = self._retrosheet_row_date(row)
            if team_game_date is None:
                raise AcquisitionExecutionError(
                    "Retrosheet teamstats row has no valid game date"
                )
            normalized_line, lineup_entries = normalize_retrosheet_team_game_row(
                row, regular_game_ids=regular_game_ids
            )
            if isinstance(normalized_line, RetrosheetExcludedRow):
                if not note("teamstats.csv", team_game_date.year, rejected=True):
                    queue_excluded(normalized_line)
                if remaining_rows[("teamstats.csv", team_game_date.year)] == 0:
                    flush_team_rows()
                    flush_excluded_rows("teamstats.csv")
                    complete_season("teamstats.csv", team_game_date.year)
                continue
            team_line: RetrosheetTeamGameLine = normalized_line
            completed_child = note("teamstats.csv", team_game_date.year)
            game_id, _, home_id, _, home_source, away_source = games[
                team_line.provider_game_id
            ]
            team_id, _ = team_identity(
                team_line.team_provider_id, team_line.canonical_team_key
            )
            if team_line.team_provider_id not in {home_source, away_source}:
                raise AcquisitionExecutionError(
                    "Retrosheet team statistics referenced a team outside the game"
                )
            if completed_child:
                if remaining_rows[("teamstats.csv", team_game_date.year)] == 0:
                    flush_team_rows()
                    flush_excluded_rows("teamstats.csv")
                    complete_season("teamstats.csv", team_game_date.year)
                continue
            source_checksum = _checksum(dict(row))
            team_snapshot_batch.append(
                {
                    "stats_run_id": context.stats_run_id,
                    "game_identity_id": game_id,
                    "team_identity_id": team_id,
                    "raw_payload_id": raw_ids["teamstats.csv"],
                    "side": "home" if team_id == home_id else "away",
                    "snapshot_kind": (
                        "retrosheet_game_" + (team_line.stats_type or "total")
                    ),
                    "retrieved_at": observed_at,
                    "source_checksum": source_checksum,
                    "stats": {
                        "provider_team_id": team_line.team_provider_id,
                        "stats_type": team_line.stats_type,
                        "values": dict(team_line.stats),
                    },
                }
            )
            if lineup_entries:
                lineup_key = (game_id, team_id, source_checksum)
                if lineup_key not in existing_lineups:
                    lineup_record = {
                        "lineup_snapshot_id": (
                            "lineup:retrosheet:"
                            + _checksum(
                                {
                                    "game_identity_id": game_id,
                                    "team_identity_id": team_id,
                                    "source_checksum": source_checksum,
                                }
                            )[:32]
                        ),
                        "stats_run_id": context.stats_run_id,
                        "game_identity_id": game_id,
                        "team_identity_id": team_id,
                        "raw_payload_id": raw_ids["teamstats.csv"],
                        "lineup_state": "official",
                        "retrieved_at": observed_at,
                        "source_checksum": source_checksum,
                    }
                    entries: list[dict[str, object]] = [
                        {
                            "player_identity_id": ensure_player(
                                entry.provider_player_id
                            ),
                            "batting_order": entry.batting_order,
                            "position_code": entry.fielding_position,
                            "lineup_role": "starter",
                            "entry": {
                                "provider_player_id": entry.provider_player_id,
                                "provider_team_id": entry.team_provider_id,
                                "batting_order": entry.batting_order,
                                "fielding_position": entry.fielding_position,
                                "fielding_positions": list(
                                    entry.fielding_positions
                                ),
                                "diagnostic_codes": list(
                                    entry.diagnostic_codes
                                ),
                            },
                        }
                        for entry in sorted(
                            lineup_entries,
                            key=lambda value: value.batting_order,
                        )
                    ]
                    lineup_batch.append((lineup_record, entries))
                    existing_lineups.add(lineup_key)
            persisted += 1 + int(bool(lineup_entries))
            if len(team_snapshot_batch) >= 1000 or len(lineup_batch) >= 1000:
                flush_team_rows()
            if remaining_rows[("teamstats.csv", team_game_date.year)] == 0:
                flush_team_rows()
                flush_excluded_rows("teamstats.csv")
                complete_season("teamstats.csv", team_game_date.year)
        flush_team_rows()
        flush_excluded_rows("teamstats.csv")
        complete_member("teamstats.csv")

        source_stint_occurrences: Counter[
            tuple[str, str, str, str, str]
        ] = Counter()
        for member_name in ("batting.csv", "pitching.csv", "fielding.csv"):
            snapshot_batch: list[dict[str, object]] = []
            for row in dataset.members[member_name].iter_rows():
                player_game_date = self._retrosheet_row_date(row)
                if player_game_date is None:
                    raise AcquisitionExecutionError(
                        f"Retrosheet {member_name} row has no valid game date"
                    )
                normalized_player_line = normalize_retrosheet_player_game_row(
                    member_name, row, regular_game_ids=regular_game_ids
                )
                if isinstance(normalized_player_line, RetrosheetExcludedRow):
                    if not note(member_name, player_game_date.year, rejected=True):
                        queue_excluded(normalized_player_line)
                    if remaining_rows[(member_name, player_game_date.year)] == 0:
                        if snapshot_batch:
                            self.repository.record_game_player_snapshots(
                                snapshot_batch
                            )
                            snapshot_batch.clear()
                        flush_excluded_rows(member_name)
                        complete_season(member_name, player_game_date.year)
                    continue
                player_line: RetrosheetPlayerGameLine = normalized_player_line
                completed_child = note(member_name, player_game_date.year)
                if completed_child:
                    if remaining_rows[(member_name, player_game_date.year)] == 0:
                        if snapshot_batch:
                            self.repository.record_game_player_snapshots(
                                snapshot_batch
                            )
                            snapshot_batch.clear()
                        flush_excluded_rows(member_name)
                        complete_season(member_name, player_game_date.year)
                    continue
                game_id, _, _, _, home_source, away_source = games[
                    player_line.provider_game_id
                ]
                if player_line.team_provider_id not in {home_source, away_source}:
                    raise AcquisitionExecutionError(
                        "Retrosheet player statistics referenced a team outside the game"
                    )
                team_id, _ = team_identity(
                    player_line.team_provider_id, player_line.canonical_team_key
                )
                player_id = ensure_player(player_line.provider_player_id)
                role = player_line.record_kind
                position_code = player_line.position_code
                grain_identity = (
                    player_line.provider_game_id,
                    player_line.team_provider_id,
                    player_line.provider_player_id,
                    role,
                    position_code or "_",
                )
                source_stint_occurrences[grain_identity] += 1
                source_stint_key = (
                    f"{source_stint_occurrences[grain_identity]:06d}"
                )
                source_row_key = (
                    f"{role}:{position_code or '_'}:{source_stint_key}"
                )
                snapshot_batch.append(
                    {
                        "stats_run_id": context.stats_run_id,
                        "game_identity_id": game_id,
                        "team_identity_id": team_id,
                        "player_identity_id": player_id,
                        "raw_payload_id": raw_ids[member_name],
                        "role": role,
                        "source_row_key": source_row_key,
                        "position_code": position_code,
                        "source_stint_key": source_stint_key,
                        "retrieved_at": observed_at,
                        "source_checksum": _checksum(dict(row)),
                        "stats": {
                            "provider_player_id": player_line.provider_player_id,
                            "provider_team_id": player_line.team_provider_id,
                            "stats_type": player_line.stats_type,
                            "source_row_key": source_row_key,
                            "position_code": position_code,
                            "source_stint_key": source_stint_key,
                            "values": dict(player_line.stats),
                        },
                    }
                )
                persisted += 1
                if len(snapshot_batch) >= 10_000:
                    self.repository.record_game_player_snapshots(snapshot_batch)
                    snapshot_batch.clear()
                if remaining_rows[(member_name, player_game_date.year)] == 0:
                    if snapshot_batch:
                        self.repository.record_game_player_snapshots(snapshot_batch)
                        snapshot_batch.clear()
                    flush_excluded_rows(member_name)
                    complete_season(member_name, player_game_date.year)
            if snapshot_batch:
                self.repository.record_game_player_snapshots(snapshot_batch)
            flush_excluded_rows(member_name)
            complete_member(member_name)

        play_indexes: defaultdict[str, int] = defaultdict(int)
        play_identity_batch: list[dict[str, object]] = []
        play_payload_batch: list[
            tuple[str, str, RetrosheetPlay, Mapping[str, str]]
        ] = []

        def flush_plays() -> None:
            if not play_identity_batch:
                return
            self.repository.upsert_play_identities(play_identity_batch)
            play_ids = [str(record["play_identity_id"]) for record in play_identity_batch]
            placeholders = ",".join("?" for _ in play_ids)
            latest: dict[str, tuple[int, str]] = {}
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT revision.play_identity_id,revision.revision_number,"
                    "revision.source_checksum FROM stats_play_revisions AS revision "
                    "JOIN (SELECT play_identity_id,MAX(revision_number) AS latest "
                    "FROM stats_play_revisions WHERE play_identity_id IN ("
                    + placeholders
                    + ") GROUP BY play_identity_id) AS selected "
                    "ON selected.play_identity_id=revision.play_identity_id "
                    "AND selected.latest=revision.revision_number",
                    tuple(play_ids),
                ).fetchall()
            for row in rows:
                latest[str(row["play_identity_id"])] = (
                    int(row["revision_number"]),
                    str(row["source_checksum"]),
                )
            revisions: list[dict[str, object]] = []
            for play_id, source_checksum, play, raw_row in play_payload_batch:
                prior = latest.get(play_id)
                if prior is None:
                    revision_number = 1
                    revision_kind = "initial"
                elif prior[1] == source_checksum:
                    revision_number = prior[0]
                    revision_kind = "initial" if prior[0] == 1 else "correction"
                else:
                    revision_number = prior[0] + 1
                    revision_kind = "correction"
                revisions.append(
                    {
                        "play_identity_id": play_id,
                        "stats_run_id": context.stats_run_id,
                        "raw_payload_id": raw_ids["plays.csv"],
                        "revision_number": revision_number,
                        "revision_kind": revision_kind,
                        "inning": play.inning,
                        "half_inning": play.half_inning,
                        "event_type": play.event,
                        "retrieved_at": observed_at,
                        "source_checksum": source_checksum,
                        "play": {
                            "provider_game_id": play.provider_game_id,
                            "details": dict(play.details),
                            "source_row": dict(raw_row),
                        },
                    }
                )
            self.repository.record_play_revisions(revisions)
            play_identity_batch.clear()
            play_payload_batch.clear()

        for row in dataset.members["plays.csv"].iter_rows():
            play_game_date = self._retrosheet_row_date(row)
            if play_game_date is None:
                raise AcquisitionExecutionError(
                    "Retrosheet plays row has no valid game date"
                )
            normalized_play = normalize_retrosheet_play_row(
                row, regular_game_ids=regular_game_ids
            )
            if isinstance(normalized_play, RetrosheetExcludedRow):
                if not note("plays.csv", play_game_date.year, rejected=True):
                    queue_excluded(normalized_play)
                if remaining_rows[("plays.csv", play_game_date.year)] == 0:
                    flush_plays()
                    flush_excluded_rows("plays.csv")
                    complete_season("plays.csv", play_game_date.year)
                continue
            play: RetrosheetPlay = normalized_play
            completed_child = note("plays.csv", play_game_date.year)
            play_indexes[play.provider_game_id] += 1
            if completed_child:
                if remaining_rows[("plays.csv", play_game_date.year)] == 0:
                    flush_plays()
                    flush_excluded_rows("plays.csv")
                    complete_season("plays.csv", play_game_date.year)
                continue
            provider_play_id = (
                f"{play.provider_game_id}:{play_indexes[play.provider_game_id]}"
            )
            play_id = f"play:retrosheet:{provider_play_id}"
            source_checksum = _checksum(dict(row))
            play_identity_batch.append(
                {
                    "play_identity_id": play_id,
                    "provider": "retrosheet",
                    "game_identity_id": games[play.provider_game_id][0],
                    "provider_play_id": provider_play_id,
                    "at_bat_index": play_indexes[play.provider_game_id] - 1,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                }
            )
            play_payload_batch.append((play_id, source_checksum, play, row))
            persisted += 1
            if len(play_identity_batch) >= 5_000:
                flush_plays()
            if remaining_rows[("plays.csv", play_game_date.year)] == 0:
                flush_plays()
                flush_excluded_rows("plays.csv")
                complete_season("plays.csv", play_game_date.year)
        flush_plays()
        flush_excluded_rows("plays.csv")
        complete_member("plays.csv")

        incomplete_source_counts = sorted(
            f"{member}:{season}:{remaining}"
            for (member, season), remaining in remaining_rows.items()
            if remaining != 0
        )
        if incomplete_source_counts:
            raise AcquisitionExecutionError(
                "Retrosheet source season row counts did not reconcile: "
                + ", ".join(incomplete_source_counts)
            )
        manifest_entries = [
            {
                "dataset_key": (
                    f"retrosheet:{member_name.removesuffix('.csv')}:season:{season}"
                ),
                "member_name": member_name,
                "season": season,
                "source_row_count": count,
                "raw_payload_id": raw_ids[member_name],
                "raw_checksum": dataset.members[
                    member_name
                ].raw.checksum_sha256,
            }
            for member_name in RETROSHEET_SEVEN_MEMBERS
            for season, count in dataset.members[
                member_name
            ].season_row_counts.items()
            if season <= through_season
        ]
        manifest_cursor = {
            "contract": "DSE_RETROSHEET_MEMBER_SEASON_MANIFEST_V1",
            "through_season": through_season,
            "entries": manifest_entries,
        }
        manifest_key = "retrosheet_member_season_manifest"
        manifest_checkpoint_id = self._checkpoint(context, manifest_key)
        existing_manifest = self.repository.get_checkpoint(
            context.stats_run_id, manifest_key
        )
        if existing_manifest is not None and existing_manifest["status"] == "completed":
            try:
                existing_cursor = json.loads(
                    str(existing_manifest["cursor_after_json"])
                )
            except (TypeError, json.JSONDecodeError) as exc:
                raise AcquisitionResumeError(
                    "Retrosheet member-season manifest lacks deterministic evidence"
                ) from exc
            if existing_cursor != manifest_cursor:
                raise AcquisitionResumeError(
                    "Retrosheet member-season manifest does not match raw input"
                )
        elif existing_manifest is not None and existing_manifest["status"] == "running":
            self._complete_checkpoint(
                manifest_checkpoint_id,
                seen=len(manifest_entries),
                persisted=len(manifest_entries),
                source_observed_at=observed_at,
                cursor_after=manifest_cursor,
            )
        else:
            raise AcquisitionResumeError(
                "Retrosheet member-season manifest is terminal but incomplete"
            )

        inventory_cursor = self._retrosheet_analytical_inventory(through_season)
        inventory_key = "retrosheet_analytical_inventory"
        inventory_checkpoint_id = self._checkpoint(context, inventory_key)
        existing_inventory = self.repository.get_checkpoint(
            context.stats_run_id, inventory_key
        )
        raw_inventory_tables = inventory_cursor["tables"]
        if not isinstance(raw_inventory_tables, Mapping):
            raise AcquisitionExecutionError(
                "Retrosheet analytical inventory tables are malformed"
            )
        inventory_tables = dict(raw_inventory_tables)
        inventory_row_count = sum(
            int(value["row_count"])
            for value in inventory_tables.values()
            if isinstance(value, Mapping)
        )
        if len(inventory_tables) != sum(
            isinstance(value, Mapping) for value in inventory_tables.values()
        ):
            raise AcquisitionExecutionError(
                "Retrosheet analytical inventory table details are malformed"
            )
        if (
            existing_inventory is not None
            and existing_inventory["status"] == "completed"
        ):
            existing_cursor = self._checkpoint_cursor(existing_inventory)
            if existing_cursor != inventory_cursor:
                raise AcquisitionResumeError(
                    "Retrosheet analytical inventory does not match persisted facts"
                )
        elif (
            existing_inventory is not None
            and existing_inventory["status"] == "running"
        ):
            self._complete_checkpoint(
                inventory_checkpoint_id,
                seen=inventory_row_count,
                persisted=inventory_row_count,
                source_observed_at=observed_at,
                cursor_after=inventory_cursor,
            )
        else:
            raise AcquisitionResumeError(
                "Retrosheet analytical inventory is terminal but incomplete"
            )

        child_checkpoint_count = len(completed_children) + 2
        return (
            persisted,
            states,
            excluded_count,
            child_checkpoint_count,
            gid_team_role_mismatch_count,
        )

    def _retrosheet_child_checkpoint(
        self,
        context: _RunContext,
        *,
        member_name: str,
        season: int,
        raw_payload_id: str,
        raw_checksum: str,
    ) -> tuple[str, bool]:
        dataset_key = (
            f"retrosheet:{member_name.removesuffix('.csv')}:season:{season}"
        )
        checkpoint_id = self._checkpoint(context, dataset_key)
        checkpoint = self.repository.get_checkpoint(context.stats_run_id, dataset_key)
        if checkpoint is None or checkpoint["status"] != "completed":
            return checkpoint_id, False
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"]))
            valid = (
                cursor["contract"] == "DSE_RETROSHEET_MEMBER_SEASON_V2"
                and cursor["member_name"] == member_name
                and int(cursor["season"]) == season
                and cursor["raw_payload_id"] == raw_payload_id
                and cursor["raw_checksum"] == raw_checksum
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionResumeError(
                f"Retrosheet checkpoint {dataset_key!r} lacks deterministic evidence"
            ) from exc
        if not valid:
            raise AcquisitionResumeError(
                f"Retrosheet checkpoint {dataset_key!r} evidence does not match raw input"
            )
        return checkpoint_id, True

    @staticmethod
    def _retrosheet_row_date(row: Mapping[str, object]) -> date | None:
        game_id = str(row.get("gid", "")).strip()
        if not game_id:
            return None
        try:
            game_date, _, _ = parse_retrosheet_game_id(game_id)
        except ValueError:
            return None
        return game_date

    @staticmethod
    def _retrosheet_source_row_id(
        member_name: str,
        source_key: str | None,
        row: Mapping[str, object],
        source_checksum: str,
    ) -> str:
        components = [
            member_name,
            source_key or "row",
            str(row.get("id", "")).strip(),
            str(row.get("team", "")).strip(),
            str(row.get("stattype", "")).strip(),
        ]
        stable = ":".join(value for value in components if value)
        return f"{stable}:{source_checksum[:16]}"

    def _record_play_revision(
        self,
        context: _RunContext,
        play_id: str,
        raw_id: str,
        source_checksum: str,
        observed_at: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        with self.database.connect() as connection:
            latest = connection.execute(
                "SELECT revision_number,source_checksum FROM stats_play_revisions "
                "WHERE play_identity_id=? ORDER BY revision_number DESC LIMIT 1",
                (play_id,),
            ).fetchone()
        if latest is not None and latest["source_checksum"] == source_checksum:
            return
        revision = 1 if latest is None else int(latest["revision_number"]) + 1
        self.repository.record_play_revision(
            {
                "play_identity_id": play_id,
                "stats_run_id": context.stats_run_id,
                "raw_payload_id": raw_id,
                "revision_number": revision,
                "revision_kind": "initial" if latest is None else "correction",
                "inning": optional_int(payload.get("inning")),
                "event_type": str(payload.get("event", "")).strip() or None,
                "retrieved_at": observed_at,
                "source_checksum": source_checksum,
                "play": dict(payload),
            }
        )

    def _collect_current(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        schedule_checkpoint = self._checkpoint(context, "baseball_reference_schedule")
        completed_schedule = self._verified_completed_checkpoint(
            context, "baseball_reference_schedule"
        )
        if completed_schedule is not None:
            schedule_collection = self._restore_schedule_checkpoint(
                context, completed_schedule[0], completed_schedule[1], request
            )
            schedule_observed = max(
                (artifact.retrieved_at for artifact in completed_schedule[1].values())
            )
        else:
            schedule_collection = self._collect_schedules(
                context, schedule_checkpoint, request
            )
            schedule_observed = max(
                (
                    value[1].retrieved_at
                    for value in schedule_collection.raw_ids.values()
                ),
                default=_aware_utc(self.clock()),
            )
            self._complete_checkpoint(
                schedule_checkpoint,
                seen=schedule_collection.source_row_count,
                persisted=len(schedule_collection.records),
                source_observed_at=schedule_observed,
                cursor_after={
                    "contract": "DSE_BREF_SCHEDULE_CHECKPOINT_V2",
                    "command": context.command.value,
                    "requested_through_date": (
                        request.requested_through_date.isoformat()
                    ),
                    "season_start": schedule_collection.season_start.isoformat(),
                    "source_ids": list(schedule_collection.source_ids),
                    "expected_source_count": len(
                        TEAM_SOURCE_ALIASES["baseball_reference"]
                    ),
                    "reconciled_pair_count": (
                        schedule_collection.reconciled_pair_count
                    ),
                    "incomplete_pair_count": (
                        schedule_collection.incomplete_pair_count
                    ),
                    "persisted_observation_count": (
                        schedule_collection.persisted_observation_count
                    ),
                    "raw_payloads_by_source_id": {
                        source_id: raw_id
                        for source_id, (raw_id, _) in sorted(
                            schedule_collection.raw_ids.items()
                        )
                    },
                },
            )

        schedules = list(schedule_collection.records)
        raw_schedule_ids = dict(schedule_collection.raw_ids)
        schedule_count = schedule_collection.source_row_count
        season_start = schedule_collection.season_start

        snapshot_checkpoint = self._checkpoint(
            context, "baseball_reference_batting_pitching"
        )
        final_dates = [
            game.official_date
            for game, _, _ in schedules
            if game.status is GameStatus.FINAL
        ]
        opening_day = season_start
        prior_watermark = self.repository.get_completeness_watermark(
            provider="current_mlb_stats",
            dataset_key="regular_season_games",
            season=request.requested_through_date.year,
        )
        prior_latest_completed = (
            date.fromisoformat(
                str(prior_watermark["latest_ingested_completed_game_date"])
            )
            if prior_watermark is not None
            and prior_watermark.get("latest_ingested_completed_game_date")
            else None
        )
        if (
            prior_latest_completed is not None
            and prior_latest_completed <= request.requested_through_date
        ):
            final_dates.append(prior_latest_completed)
        effective_final_date = max(final_dates, default=None)
        feature_as_of = request.requested_through_date
        feature_through_date = feature_as_of - timedelta(days=1)
        reconciliation_through_date = effective_final_date or feature_through_date
        aggregate_capture_ranges = _aggregate_capture_ranges(
            opening_day,
            feature_through_date=feature_through_date,
            completeness_through_date=effective_final_date,
        )
        completed_snapshots = self._verified_completed_checkpoint(
            context, "baseball_reference_batting_pitching"
        )
        if completed_snapshots is not None:
            aggregate_collection = self._restore_baseball_reference_aggregates(
                context,
                completed_snapshots[0],
                completed_snapshots[1],
                feature_as_of=feature_as_of,
            )
            snapshot_persisted = aggregate_collection.persisted
            snapshot_rejected = aggregate_collection.rejected
            snapshot_raw = list(aggregate_collection.artifacts)
            snapshot_observed = max(
                artifact.retrieved_at for artifact in snapshot_raw
            )
        else:
            aggregate_collection = self._collect_baseball_reference_snapshots(
                context,
                snapshot_checkpoint,
                season_start=opening_day,
                feature_through_date=feature_through_date,
                completeness_through_date=effective_final_date,
                season=request.requested_through_date.year,
            )
            snapshot_persisted = aggregate_collection.persisted
            snapshot_rejected = aggregate_collection.rejected
            snapshot_raw = list(aggregate_collection.artifacts)
            snapshot_observed = max(
                (artifact.retrieved_at for artifact in snapshot_raw),
                default=schedule_observed,
            )
            self._complete_checkpoint(
                snapshot_checkpoint,
                seen=(
                    len(aggregate_collection.batting)
                    + len(aggregate_collection.pitching)
                    + snapshot_rejected
                ),
                persisted=snapshot_persisted,
                rejected=snapshot_rejected,
                source_observed_at=snapshot_observed,
                partial=bool(snapshot_rejected),
                cursor_after={
                    "contract": "DSE_BREF_AGGREGATE_CHECKPOINT_V2",
                    "season": request.requested_through_date.year,
                    "season_start": opening_day.isoformat(),
                    "through_date": (
                        effective_final_date.isoformat()
                        if effective_final_date is not None
                        else None
                    ),
                    "feature_as_of": feature_as_of.isoformat(),
                    "feature_through_date": feature_through_date.isoformat(),
                    "expected_capture_count": len(aggregate_capture_ranges) * 2,
                    "actual_capture_count": len(snapshot_raw),
                    "normalized_record_count": (
                        len(aggregate_collection.batting)
                        + len(aggregate_collection.pitching)
                    ),
                    "records_rejected": snapshot_rejected,
                    "captures": [
                        {
                            "raw_payload_id": capture.raw_payload_id,
                            "stat_kind": capture.stat_kind,
                            "window_key": capture.window_key,
                            "range_start": capture.range_start.isoformat(),
                            "range_end": capture.range_end.isoformat(),
                            "purposes": list(capture.purposes),
                        }
                        for capture in aggregate_collection.captures
                    ],
                },
            )

        (
            game_coverage,
            statcast_validation_failed_keys,
            _collected_feature_inputs,
            statcast_persisted,
            statcast_seen,
            statcast_excluded,
            statcast_conflicts,
            statcast_raw,
            statcast_days,
        ) = self._collect_statcast_days(
            context,
            request,
            schedules,
            opening_day=opening_day,
            effective_final_date=effective_final_date,
        )

        aggregate_complete_through = (
            effective_final_date
            if effective_final_date is not None
            and not snapshot_rejected
            and len(snapshot_raw) == len(aggregate_capture_ranges) * 2
            else None
        )
        states = self._schedule_states(
            schedules,
            game_coverage,
            aggregate_complete_through=aggregate_complete_through,
            validation_failed_keys=statcast_validation_failed_keys,
        )
        completeness = calculate_completeness(request.requested_through_date, states)
        if context.command is AcquisitionCommand.DAILY:
            completeness = self._merge_daily_completeness(
                completeness,
                aggregate_complete_through=aggregate_complete_through,
            )
        knowledge_cutoff = context.source_observed_at or _aware_utc(self.clock())
        historical_feature_inputs, statcast_feature_checksums = (
            self._load_statcast_feature_inputs(
                feature_as_of=request.requested_through_date,
                knowledge_cutoff=knowledge_cutoff,
            )
        )
        reconciliation_pitches = self._load_statcast_reconciliation_pitches(
            season_start=opening_day,
            through_date=reconciliation_through_date,
            knowledge_cutoff=knowledge_cutoff,
        )
        try:
            counting_reconciliation = reconcile_baseball_reference_to_statcast(
                through_date=reconciliation_through_date,
                batting_aggregates=aggregate_collection.batting,
                pitching_aggregates=aggregate_collection.pitching,
                statcast_pitches=reconciliation_pitches,
            )
        except ValueError as exc:
            raise AcquisitionExecutionError(
                "Cross-source counting-stat reconciliation input is ambiguous",
                run_id=context.run_id,
                stats_run_id=context.stats_run_id,
            ) from exc
        counting_reconciliation_items = self._cross_source_counting_items(
            counting_reconciliation
        )
        pitcher_appearances = derive_statcast_pitcher_appearances(
            historical_feature_inputs[2]
        )
        plate_appearances = derive_statcast_plate_appearances(
            historical_feature_inputs[2]
        )
        warnings: list[str] = []
        if completeness.partial_date_reason is not None:
            warnings.append(completeness.partial_date_reason)
        if statcast_conflicts:
            warnings.append("statcast_pitch_conflicts")
        if snapshot_rejected:
            warnings.append("baseball_reference_aggregate_rows_rejected")
        completeness_watermark_eligible = (
            counting_reconciliation.unexplained_difference_count == 0
        )
        if not completeness_watermark_eligible:
            warnings.append(
                "baseball_reference_statcast_unexplained_reconciliation_difference"
            )
        feature_count = self._persist_aggregate_features(
            context,
            feature_as_of,
            completeness,
            source_checksums=[
                *(artifact.checksum_sha256 for _, artifact in raw_schedule_ids.values()),
                *(
                    capture.artifact.checksum_sha256
                    for capture in aggregate_collection.captures
                    if capture.range_end == feature_through_date
                ),
                *(artifact.checksum_sha256 for artifact in statcast_raw),
                *statcast_feature_checksums,
            ],
            batting_aggregates=tuple(
                line
                for line in aggregate_collection.batting
                if line.through_date == feature_through_date
            ),
            pitching_aggregates=tuple(
                line
                for line in aggregate_collection.pitching
                if line.through_date == feature_through_date
            ),
            batted_balls=historical_feature_inputs[0],
            pitch_metrics=historical_feature_inputs[1],
            swing_metrics=historical_feature_inputs[3],
            pitcher_appearances=pitcher_appearances,
            statcast_plate_appearances=plate_appearances,
        )
        counts = Counter(
            {
                "raw_payloads": len(raw_schedule_ids)
                + len(snapshot_raw)
                + len(statcast_raw),
                "schedule_rows": schedule_count,
                "schedule_observations_persisted": (
                    schedule_collection.persisted_observation_count
                ),
                "games": len(schedules),
                "schedule_sources_expected": len(
                    TEAM_SOURCE_ALIASES["baseball_reference"]
                ),
                "schedule_sources_observed": len(schedule_collection.source_ids),
                "schedule_reconciled_pairs": (
                    schedule_collection.reconciled_pair_count
                ),
                "schedule_incomplete_pairs": (
                    schedule_collection.incomplete_pair_count
                ),
                "baseball_reference_snapshots": snapshot_persisted,
                "baseball_reference_snapshot_captures": len(snapshot_raw),
                "baseball_reference_snapshot_captures_expected": len(
                    aggregate_capture_ranges
                )
                * 2,
                "baseball_reference_snapshot_rejections": snapshot_rejected,
                "statcast_rows": statcast_seen,
                "statcast_pitches": (
                    statcast_seen - statcast_excluded - statcast_conflicts
                ),
                "statcast_revisions_inserted": statcast_persisted,
                "statcast_excluded": statcast_excluded,
                "statcast_conflicts": statcast_conflicts,
                "statcast_day_checkpoints": statcast_days,
                "statcast_parallel": 0,
                "persisted_records": len(schedules)
                + snapshot_persisted
                + statcast_persisted,
                "feature_snapshots": feature_count,
                "final_games": sum(
                    game.status is GameStatus.FINAL for game, _, _ in schedules
                ),
                "fully_validated_final_games": sum(
                    state.final_and_valid for state in states
                ),
                "counting_reconciliation_entities": (
                    counting_reconciliation.entities_compared
                ),
                "counting_reconciliation_fields": (
                    counting_reconciliation.fields_compared
                ),
                "counting_reconciliation_matches": (
                    counting_reconciliation.fields_matched
                ),
                "counting_reconciliation_explained_differences": (
                    counting_reconciliation.explained_difference_count
                ),
                "counting_reconciliation_unexplained_differences": (
                    counting_reconciliation.unexplained_difference_count
                ),
            }
        )
        if len(schedule_collection.source_ids) != len(
            TEAM_SOURCE_ALIASES["baseball_reference"]
        ):
            warnings.append("baseball_reference_schedule_source_coverage_incomplete")
        if schedule_collection.incomplete_pair_count:
            warnings.append("baseball_reference_schedule_pair_coverage_incomplete")
        replay_transport = self._source_run_replay_transport()
        if replay_transport is not None:
            replay_transport.assert_exhausted()
            counts["replayed_source_captures"] = (
                replay_transport.consumed_capture_count
            )
            counts["network_requests"] = 0
        return self._finalize_run(
            context,
            request,
            completeness,
            counts,
            warnings,
            additional_reconciliation_items=counting_reconciliation_items,
            completeness_watermark_eligible=completeness_watermark_eligible,
        )

    def _collect_baseball_reference_snapshots(
        self,
        context: _RunContext,
        checkpoint_id: str,
        *,
        season_start: date,
        feature_through_date: date,
        completeness_through_date: date | None,
        season: int,
    ) -> _BaseballReferenceAggregateCollection:
        provider = BaseballReferenceProvider(self.transport)
        persisted = 0
        rejected = 0
        artifacts: list[RawArtifact] = []
        captures: list[_BaseballReferenceAggregateCapture] = []
        batting: list[BattingAggregateLine] = []
        pitching: list[PitchingAggregateLine] = []
        fixture_transport = self._fixture_transport()
        for window_key, start_date, end_date in _aggregate_capture_ranges(
            season_start,
            feature_through_date=feature_through_date,
            completeness_through_date=completeness_through_date,
        ):
            for kind in ("batting", "pitching"):
                fixture_key = f"baseball_reference_{kind}_{window_key}"
                if (
                    fixture_transport is not None
                    and fixture_key not in fixture_transport.fixtures
                    and f"baseball_reference_{kind}" in fixture_transport.fixtures
                ):
                    fixture_key = f"baseball_reference_{kind}"
                table = (
                    provider.collect_batting_stats_range(
                        start_date, end_date, fixture_key=fixture_key
                    )
                    if kind == "batting"
                    else provider.collect_pitching_stats_range(
                        start_date, end_date, fixture_key=fixture_key
                    )
                )
                artifacts.append(table.raw)
                raw_id = self._persist_raw(context, checkpoint_id, table.raw)
                captures.append(
                    _BaseballReferenceAggregateCapture(
                        raw_payload_id=raw_id,
                        artifact=table.raw,
                        stat_kind=kind,
                        window_key=window_key,
                        range_start=start_date,
                        range_end=end_date,
                        purposes=_aggregate_capture_purposes(
                            window_key,
                            end_date,
                            feature_through_date=feature_through_date,
                            completeness_through_date=completeness_through_date,
                        ),
                    )
                )
                stint_occurrences: Counter[tuple[str, str]] = Counter()
                snapshot_records: list[dict[str, object]] = []
                raw_link_records: dict[str, dict[str, object]] = {}
                for row in table.rows:
                    source_player_id = str(row.get("mlbID", "")).strip()
                    full_name = str(row.get("name_display", "")).strip()
                    source_team_name = str(row.get("team_name", "")).strip()
                    source_level = str(row.get("level", "")).strip()
                    try:
                        if not source_player_id or not full_name:
                            raise ValueError(
                                "Baseball-Reference aggregate identity or team is missing"
                            )
                        team_identity = resolve_baseball_reference_aggregate_team(
                            source_team_name,
                            source_level,
                        )
                        if kind == "batting":
                            counts = {
                                "pa": _nonnegative_count(row, "plate_appearances"),
                                "ab": _nonnegative_count(row, "at_bats"),
                                "hits": _nonnegative_count(row, "hits"),
                                "doubles": _nonnegative_count(row, "doubles"),
                                "triples": _nonnegative_count(row, "triples"),
                                "home_runs": _nonnegative_count(row, "home_runs"),
                                "walks": _nonnegative_count(row, "bases_on_balls"),
                                "hit_by_pitch": _nonnegative_count(row, "hit_by_pitch"),
                                "strikeouts": _nonnegative_count(row, "strikeouts"),
                                "sacrifice_flies": _nonnegative_count(
                                    row, "sacrifice_flies"
                                ),
                            }
                        else:
                            counts = {
                                "batters_faced": _nonnegative_count(
                                    row, "batters_faced"
                                ),
                                "outs_recorded": _innings_to_outs(
                                    row.get("innings_pitched")
                                ),
                                "hits": _nonnegative_count(row, "hits"),
                                "earned_runs": _nonnegative_count(row, "earned_runs"),
                                "home_runs": _nonnegative_count(row, "home_runs"),
                                "walks": _nonnegative_count(row, "bases_on_balls"),
                                "hit_by_pitch": _nonnegative_count(row, "hit_by_pitch"),
                                "strikeouts": _nonnegative_count(row, "strikeouts"),
                                "pitches": _nonnegative_count(row, "pitches"),
                            }
                    except ValueError:
                        rejected += 1
                        continue
                    source_team_id = team_identity.source_scope_id
                    team_key = team_identity.canonical_team_key
                    stint_occurrences[(source_player_id, source_team_id)] += 1
                    source_stint_key = (
                        f"{source_team_id}:"
                        f"{stint_occurrences[(source_player_id, source_team_id)]}"
                    )
                    player_identity_id = self._player_identity(
                        "baseball_reference",
                        source_player_id,
                        table.raw.retrieved_at,
                        full_name=full_name,
                    )
                    canonical_player_id = self._map_baseball_reference_player(
                        context,
                        source_player_id=source_player_id,
                        player_identity_id=player_identity_id,
                        full_name=full_name,
                        artifact=table.raw,
                    )
                    split_key = (
                        f"{kind}:{window_key}:{start_date.isoformat()}:"
                        f"{end_date.isoformat()}"
                    )
                    snapshot_records.append(
                        {
                            "season_snapshot_id": self.id_factory("season_snapshot"),
                            "stats_run_id": context.stats_run_id,
                            "raw_payload_id": raw_id,
                            "provider": "baseball_reference",
                            "season": season,
                            "entity_kind": "player",
                            "player_identity_id": player_identity_id,
                            "source_team_id": source_team_id,
                            "source_stint_key": source_stint_key,
                            "split_key": split_key,
                            "snapshot_as_of": _date_at_utc(end_date),
                            "retrieved_at": table.raw.retrieved_at,
                            "stats": {
                                "stat_kind": kind,
                                "window_key": window_key,
                                "range_start": start_date.isoformat(),
                                "range_end": end_date.isoformat(),
                                "source_player_id": source_player_id,
                                "source_team_id": source_team_id,
                                "source_team_name": source_team_name,
                                "source_level": source_level,
                                "source_team_scope_kind": (
                                    "multi_team_aggregate"
                                    if team_identity.is_multi_team
                                    else "single_team"
                                ),
                                "source_stint_key": source_stint_key,
                                "canonical_player_id": canonical_player_id,
                                "normalized_counts": counts,
                                "source_row": dict(row),
                            },
                            "source_checksum": table.raw.checksum_sha256,
                        }
                    )
                    raw_link_records[player_identity_id] = {
                        "raw_payload_id": raw_id,
                        "link_role": f"contains_{kind}_snapshot",
                        "player_identity_id": player_identity_id,
                    }
                    if kind == "batting":
                        batting.append(
                            BattingAggregateLine(
                                end_date,
                                canonical_player_id,
                                window_key,
                                **counts,
                                source_team_id=source_team_id,
                                source_stint_key=source_stint_key,
                                team_key=team_key,
                                available_at=table.raw.retrieved_at,
                            )
                        )
                    else:
                        pitching.append(
                            PitchingAggregateLine(
                                end_date,
                                canonical_player_id,
                                window_key,
                                **counts,
                                source_team_id=source_team_id,
                                source_stint_key=source_stint_key,
                                team_key=team_key,
                                available_at=table.raw.retrieved_at,
                            )
                        )
                snapshots = self.repository.record_season_snapshots(snapshot_records)
                persisted += sum(
                    snapshot["stats_run_id"] == context.stats_run_id
                    for snapshot in snapshots
                )
                self.repository.link_raw_entities(tuple(raw_link_records.values()))
        return _BaseballReferenceAggregateCollection(
            persisted,
            tuple(artifacts),
            rejected,
            tuple(batting),
            tuple(pitching),
            tuple(captures),
        )

    def _restore_legacy_baseball_reference_aggregates(
        self,
        context: _RunContext,
        checkpoint: Mapping[str, Any],
        artifacts: Mapping[str, RawArtifact],
        *,
        feature_as_of: date,
    ) -> _BaseballReferenceAggregateCollection:
        checkpoint_id = str(checkpoint["checkpoint_id"])
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"] or "{}"))
            if cursor["contract"] != "DSE_BREF_AGGREGATE_CHECKPOINT_V1":
                raise ValueError("unsupported aggregate checkpoint contract")
            checkpoint_through = (
                date.fromisoformat(str(cursor["through_date"]))
                if cursor.get("through_date") is not None
                else None
            )
            checkpoint_feature_as_of = date.fromisoformat(
                str(cursor["feature_as_of"])
            )
            checkpoint_feature_through = date.fromisoformat(
                str(cursor["feature_through_date"])
            )
            if checkpoint_feature_as_of != feature_as_of:
                raise ValueError("aggregate feature cutoff changed")
            if checkpoint_feature_through != feature_as_of - timedelta(days=1):
                raise ValueError("aggregate D-1 feature range changed")
            if (
                checkpoint_through is not None
                and checkpoint_through > feature_as_of
            ):
                raise ValueError("aggregate checkpoint is later than its feature cutoff")
            if int(cursor["actual_capture_count"]) != len(artifacts):
                raise ValueError("aggregate raw capture count changed")
            if int(cursor["expected_capture_count"]) != len(artifacts):
                raise ValueError("aggregate source coverage is incomplete")
            if int(cursor["records_rejected"]) != 0:
                raise ValueError("aggregate checkpoint contains rejected rows")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcquisitionResumeError(
                "completed Baseball-Reference aggregate checkpoint lacks "
                "deterministic coverage evidence"
            ) from exc
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot.player_identity_id, snapshot.retrieved_at,
                       snapshot.stats_json, snapshot.raw_payload_id
                FROM stats_season_snapshots AS snapshot
                JOIN stats_raw_payload_metadata AS raw
                  ON raw.raw_payload_id=snapshot.raw_payload_id
                WHERE snapshot.stats_run_id=? AND raw.checkpoint_id=?
                ORDER BY snapshot.split_key, snapshot.player_identity_id
                """,
                (context.stats_run_id, checkpoint_id),
            ).fetchall()
        expected = int(checkpoint["records_persisted"])
        if len(rows) != expected:
            raise AcquisitionResumeError(
                "completed Baseball-Reference snapshot checkpoint does not reconcile"
            )
        batting: list[BattingAggregateLine] = []
        pitching: list[PitchingAggregateLine] = []
        try:
            for row in rows:
                raw_payload_id = str(row["raw_payload_id"])
                if raw_payload_id not in artifacts:
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference snapshot lacks raw evidence"
                    )
                payload = json.loads(str(row["stats_json"]))
                kind = str(payload["stat_kind"])
                window_key = str(payload["window_key"])
                through_date = date.fromisoformat(str(payload["range_end"]))
                if through_date > feature_as_of:
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference aggregate is later than its cutoff"
                    )
                canonical_player_id = str(payload["canonical_player_id"])
                source_team_id = str(payload["source_team_id"])
                source_stint_key = str(payload["source_stint_key"])
                source_row = dict(payload.get("source_row") or {})
                source_team_name = str(
                    payload.get("source_team_name") or source_team_id
                )
                source_level = str(
                    payload.get("source_level")
                    or source_row.get("level")
                    or "MLB"
                )
                team_key = resolve_baseball_reference_aggregate_team(
                    source_team_name,
                    source_level,
                ).canonical_team_key
                normalized = dict(payload["normalized_counts"])
                available_at = datetime.fromisoformat(str(row["retrieved_at"]))
                if kind == "batting":
                    counts = {
                        key: _nonnegative_count(normalized, key)
                        for key in (
                            "pa",
                            "ab",
                            "hits",
                            "doubles",
                            "triples",
                            "home_runs",
                            "walks",
                            "hit_by_pitch",
                            "strikeouts",
                            "sacrifice_flies",
                        )
                    }
                    batting.append(
                        BattingAggregateLine(
                            through_date,
                            canonical_player_id,
                            window_key,
                            **counts,
                            source_team_id=source_team_id,
                            source_stint_key=source_stint_key,
                            team_key=team_key,
                            available_at=available_at,
                        )
                    )
                elif kind == "pitching":
                    counts = {
                        key: _nonnegative_count(normalized, key)
                        for key in (
                            "batters_faced",
                            "outs_recorded",
                            "hits",
                            "earned_runs",
                            "home_runs",
                            "walks",
                            "hit_by_pitch",
                            "strikeouts",
                            "pitches",
                        )
                    }
                    pitching.append(
                        PitchingAggregateLine(
                            through_date,
                            canonical_player_id,
                            window_key,
                            **counts,
                            source_team_id=source_team_id,
                            source_stint_key=source_stint_key,
                            team_key=team_key,
                            available_at=available_at,
                        )
                    )
                else:
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference snapshot kind is invalid"
                    )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcquisitionResumeError(
                "completed Baseball-Reference snapshot evidence is malformed"
            ) from exc
        return _BaseballReferenceAggregateCollection(
            expected,
            tuple(artifacts.values()),
            int(checkpoint["records_rejected"]),
            tuple(batting),
            tuple(pitching),
        )

    def _restore_baseball_reference_aggregates(
        self,
        context: _RunContext,
        checkpoint: Mapping[str, Any],
        artifacts: Mapping[str, RawArtifact],
        *,
        feature_as_of: date,
    ) -> _BaseballReferenceAggregateCollection:
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"] or "{}"))
            if cursor["contract"] != "DSE_BREF_AGGREGATE_CHECKPOINT_V2":
                raise ValueError("unsupported aggregate checkpoint contract")
            season = int(cursor["season"])
            season_start = date.fromisoformat(str(cursor["season_start"]))
            checkpoint_through = (
                date.fromisoformat(str(cursor["through_date"]))
                if cursor["through_date"] is not None
                else None
            )
            checkpoint_feature_as_of = date.fromisoformat(
                str(cursor["feature_as_of"])
            )
            checkpoint_feature_through = date.fromisoformat(
                str(cursor["feature_through_date"])
            )
            if checkpoint_feature_as_of != feature_as_of:
                raise ValueError("aggregate feature cutoff changed")
            if checkpoint_feature_through != feature_as_of - timedelta(days=1):
                raise ValueError("aggregate D-1 feature range changed")
            if season != context.season or (
                checkpoint_through is not None
                and checkpoint_through > feature_as_of
            ):
                raise ValueError("aggregate season or cutoff changed")
            if int(cursor["actual_capture_count"]) != len(artifacts):
                raise ValueError("aggregate raw capture count changed")
            if int(cursor["expected_capture_count"]) != len(artifacts):
                raise ValueError("aggregate source coverage is incomplete")
            if int(cursor["records_rejected"]) != 0:
                raise ValueError("aggregate checkpoint contains rejected rows")
            expected_normalized = int(cursor["normalized_record_count"])
            capture_rows = list(cursor["captures"])
            captures: list[_BaseballReferenceAggregateCapture] = []
            seen_raw_ids: set[str] = set()
            for value in capture_rows:
                manifest = dict(value)
                raw_payload_id = str(manifest["raw_payload_id"])
                if raw_payload_id in seen_raw_ids:
                    raise ValueError("aggregate raw payload is listed twice")
                seen_raw_ids.add(raw_payload_id)
                captures.append(
                    _BaseballReferenceAggregateCapture(
                        raw_payload_id=raw_payload_id,
                        artifact=artifacts[raw_payload_id],
                        stat_kind=str(manifest["stat_kind"]),
                        window_key=str(manifest["window_key"]),
                        range_start=date.fromisoformat(str(manifest["range_start"])),
                        range_end=date.fromisoformat(str(manifest["range_end"])),
                        purposes=tuple(
                            str(item) for item in list(manifest["purposes"])
                        ),
                    )
                )
            if seen_raw_ids != set(artifacts):
                raise ValueError("aggregate raw payload manifest changed")
            expected_capture_specs = {
                (kind, window_key, start_date, end_date)
                for window_key, start_date, end_date in _aggregate_capture_ranges(
                    season_start,
                    feature_through_date=checkpoint_feature_through,
                    completeness_through_date=checkpoint_through,
                )
                for kind in ("batting", "pitching")
            }
            observed_capture_specs = {
                (
                    capture.stat_kind,
                    capture.window_key,
                    capture.range_start,
                    capture.range_end,
                )
                for capture in captures
            }
            if observed_capture_specs != expected_capture_specs:
                raise ValueError("aggregate capture range inventory changed")
            for capture in captures:
                expected_purposes = _aggregate_capture_purposes(
                    capture.window_key,
                    capture.range_end,
                    feature_through_date=checkpoint_feature_through,
                    completeness_through_date=checkpoint_through,
                )
                if capture.purposes != expected_purposes:
                    raise ValueError("aggregate capture purpose changed")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcquisitionResumeError(
                "completed Baseball-Reference aggregate checkpoint lacks "
                "deterministic raw replay evidence"
            ) from exc

        batting: list[BattingAggregateLine] = []
        pitching: list[PitchingAggregateLine] = []
        normalized_count = 0
        for capture in captures:
            provider = BaseballReferenceProvider(
                _ArtifactReplayTransport(capture.artifact)
            )
            try:
                table = (
                    provider.collect_batting_stats_range(
                        capture.range_start,
                        capture.range_end,
                        fixture_key=f"resume_batting_{capture.window_key}",
                    )
                    if capture.stat_kind == "batting"
                    else provider.collect_pitching_stats_range(
                        capture.range_start,
                        capture.range_end,
                        fixture_key=f"resume_pitching_{capture.window_key}",
                    )
                    if capture.stat_kind == "pitching"
                    else None
                )
                if table is None:
                    raise ValueError("aggregate stat kind is invalid")
            except Exception as exc:
                raise AcquisitionResumeError(
                    "completed Baseball-Reference aggregate raw payload is malformed"
                ) from exc
            stint_occurrences: Counter[tuple[str, str]] = Counter()
            for row in table.rows:
                source_player_id = str(row.get("mlbID", "")).strip()
                full_name = str(row.get("name_display", "")).strip()
                source_team_name = str(row.get("team_name", "")).strip()
                source_level = str(row.get("level", "")).strip()
                try:
                    if not source_player_id or not full_name:
                        raise ValueError(
                            "Baseball-Reference aggregate identity or team is missing"
                        )
                    team_identity = resolve_baseball_reference_aggregate_team(
                        source_team_name,
                        source_level,
                    )
                    if capture.stat_kind == "batting":
                        counts = {
                            "pa": _nonnegative_count(row, "plate_appearances"),
                            "ab": _nonnegative_count(row, "at_bats"),
                            "hits": _nonnegative_count(row, "hits"),
                            "doubles": _nonnegative_count(row, "doubles"),
                            "triples": _nonnegative_count(row, "triples"),
                            "home_runs": _nonnegative_count(row, "home_runs"),
                            "walks": _nonnegative_count(row, "bases_on_balls"),
                            "hit_by_pitch": _nonnegative_count(row, "hit_by_pitch"),
                            "strikeouts": _nonnegative_count(row, "strikeouts"),
                            "sacrifice_flies": _nonnegative_count(
                                row, "sacrifice_flies"
                            ),
                        }
                    else:
                        counts = {
                            "batters_faced": _nonnegative_count(row, "batters_faced"),
                            "outs_recorded": _innings_to_outs(
                                row.get("innings_pitched")
                            ),
                            "hits": _nonnegative_count(row, "hits"),
                            "earned_runs": _nonnegative_count(row, "earned_runs"),
                            "home_runs": _nonnegative_count(row, "home_runs"),
                            "walks": _nonnegative_count(row, "bases_on_balls"),
                            "hit_by_pitch": _nonnegative_count(row, "hit_by_pitch"),
                            "strikeouts": _nonnegative_count(row, "strikeouts"),
                            "pitches": _nonnegative_count(row, "pitches"),
                        }
                except ValueError as exc:
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference aggregate normalization changed"
                    ) from exc
                source_team_id = team_identity.source_scope_id
                stint_occurrences[(source_player_id, source_team_id)] += 1
                source_stint_key = (
                    f"{source_team_id}:"
                    f"{stint_occurrences[(source_player_id, source_team_id)]}"
                )
                player_identity_id = (
                    f"player:baseball_reference:{source_player_id}"
                )
                canonical_player_id = self._mlbam_canonical_player_id(
                    source_player_id
                )
                resolved = self.repository.resolve_canonical_player(
                    "baseball_reference", source_player_id
                )
                if (
                    resolved is None
                    or str(resolved["canonical_player_id"])
                    != canonical_player_id
                ):
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference aggregate lacks player mapping"
                    )
                split_key = (
                    f"{capture.stat_kind}:{capture.window_key}:"
                    f"{capture.range_start.isoformat()}:"
                    f"{capture.range_end.isoformat()}"
                )
                snapshot_record = {
                    "season_snapshot_id": "resume_verification_only",
                    "stats_run_id": context.stats_run_id,
                    "raw_payload_id": capture.raw_payload_id,
                    "provider": "baseball_reference",
                    "season": season,
                    "entity_kind": "player",
                    "player_identity_id": player_identity_id,
                    "source_team_id": source_team_id,
                    "source_stint_key": source_stint_key,
                    "split_key": split_key,
                    "snapshot_as_of": _date_at_utc(capture.range_end),
                    "retrieved_at": capture.artifact.retrieved_at,
                    "stats": {
                        "stat_kind": capture.stat_kind,
                        "window_key": capture.window_key,
                        "range_start": capture.range_start.isoformat(),
                        "range_end": capture.range_end.isoformat(),
                        "source_player_id": source_player_id,
                        "source_team_id": source_team_id,
                        "source_team_name": source_team_name,
                        "source_level": source_level,
                        "source_team_scope_kind": (
                            "multi_team_aggregate"
                            if team_identity.is_multi_team
                            else "single_team"
                        ),
                        "source_stint_key": source_stint_key,
                        "canonical_player_id": canonical_player_id,
                        "normalized_counts": counts,
                        "source_row": dict(row),
                    },
                    "source_checksum": capture.artifact.checksum_sha256,
                }
                if not self.repository.has_season_snapshot_evidence(snapshot_record):
                    raise AcquisitionResumeError(
                        "completed aggregate checkpoint lacks global normalized evidence"
                    )
                team_key = team_identity.canonical_team_key
                if capture.stat_kind == "batting":
                    batting.append(
                        BattingAggregateLine(
                            capture.range_end,
                            canonical_player_id,
                            capture.window_key,
                            **counts,
                            source_team_id=source_team_id,
                            source_stint_key=source_stint_key,
                            team_key=team_key,
                            available_at=capture.artifact.retrieved_at,
                        )
                    )
                else:
                    pitching.append(
                        PitchingAggregateLine(
                            capture.range_end,
                            canonical_player_id,
                            capture.window_key,
                            **counts,
                            source_team_id=source_team_id,
                            source_stint_key=source_stint_key,
                            team_key=team_key,
                            available_at=capture.artifact.retrieved_at,
                        )
                    )
                normalized_count += 1

        if (
            normalized_count != expected_normalized
            or int(checkpoint["records_seen"]) != normalized_count
            or int(checkpoint["records_rejected"]) != 0
        ):
            raise AcquisitionResumeError(
                "completed Baseball-Reference aggregate checkpoint counts do not reconcile"
            )
        return _BaseballReferenceAggregateCollection(
            int(checkpoint["records_persisted"]),
            tuple(capture.artifact for capture in captures),
            0,
            tuple(batting),
            tuple(pitching),
            tuple(captures),
        )

    def _collect_statcast_days(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
        schedules: Sequence[tuple[ScheduleGame, str, RawArtifact]],
        *,
        opening_day: date,
        effective_final_date: date | None,
    ) -> tuple[
        dict[str, int],
        set[str],
        tuple[list[StatcastBattedBall], list[StatcastPitchMetric], list[StatcastSwingMetric]],
        int,
        int,
        int,
        int,
        list[RawArtifact],
        int,
    ]:
        coverage: dict[str, int] = {}
        validation_failed_schedule_ids: set[str] = set()
        batted_balls: list[StatcastBattedBall] = []
        pitch_metrics: list[StatcastPitchMetric] = []
        swing_metrics: list[StatcastSwingMetric] = []
        if context.command is AcquisitionCommand.DAILY:
            start_date = request.requested_through_date
            effective_final_date = request.requested_through_date
        else:
            if effective_final_date is None:
                return coverage, validation_failed_schedule_ids, (batted_balls, pitch_metrics, swing_metrics), 0, 0, 0, 0, [], 0
            start_date = opening_day
        if start_date > effective_final_date:
            return coverage, validation_failed_schedule_ids, (batted_balls, pitch_metrics, swing_metrics), 0, 0, 0, 0, [], 0

        final_keys = {
            (game.official_date, game.home_team_key, game.away_team_key)
            for game, _, _ in schedules
            if game.status is GameStatus.FINAL
        }
        provider = StatcastProvider(self.transport)
        artifacts: list[RawArtifact] = []
        persisted = 0
        seen = 0
        excluded = 0
        conflict_count = 0
        day_count = 0
        current = start_date
        while current <= effective_final_date:
            dataset_key = f"statcast:{current.isoformat()}"
            checkpoint = self._checkpoint(context, dataset_key)
            completed_day = self._verified_completed_checkpoint(context, dataset_key)
            if completed_day is not None:
                (
                    day_coverage,
                    day_conflicted_game_keys,
                    inputs,
                    day_seen,
                    day_persisted,
                    day_excluded,
                    day_conflicts,
                    day_artifacts,
                ) = self._restore_statcast_checkpoint(
                    completed_day[0], completed_day[1], schedules
                )
                coverage.update(day_coverage)
                validation_failed_schedule_ids.update(day_conflicted_game_keys)
                batted_balls.extend(inputs[0])
                pitch_metrics.extend(inputs[1])
                swing_metrics.extend(inputs[2])
                persisted += day_persisted
                seen += day_seen
                excluded += day_excluded
                conflict_count += day_conflicts
                artifacts.extend(day_artifacts)
                day_count += 1
                current += timedelta(days=1)
                continue
            fixture_key = f"statcast_{current.isoformat()}"
            fixture_transport = self._fixture_transport()
            if fixture_transport is not None:
                fixtures = fixture_transport.fixtures
                if fixture_key not in fixtures:
                    if "statcast" in fixtures:
                        fixture_key = "statcast"
                    elif "statcast_empty" in fixtures:
                        fixture_key = "statcast_empty"
            dataset = provider.collect(
                StatcastQuery(current, current),
                fixture_key=fixture_key,
            )
            day_count += 1
            artifacts.append(dataset.raw)
            raw_id = self._persist_raw(context, checkpoint, dataset.raw)
            normalized = normalize_statcast_rows(dataset.rows)
            relevant_game_pks = {
                pitch.game_pk
                for pitch in normalized.pitches
                if (pitch.game_date, pitch.home_team_key, pitch.away_team_key)
                in final_keys
            }
            relevant_conflicts = tuple(
                item
                for item in normalized.conflicts
                if item[0][0] in relevant_game_pks
            )
            reconciliation = _reconcile_completed_statcast_games(
                schedules, normalized.pitches, relevant_conflicts
            )
            eligible = tuple(
                pitch
                for pitch in normalized.pitches
                if pitch.game_pk in reconciliation.completed_game_pks
            )
            nonfinal = len(normalized.pitches) - len(eligible)
            day_coverage, inputs, day_persisted = self._persist_statcast(
                context,
                dataset,
                raw_id,
                eligible,
                relevant_conflicts,
                game_matches={
                    game_pk: schedule_id
                    for schedule_id, game_pk in (
                        reconciliation.schedule_to_game_pk.items()
                    )
                },
            )
            coverage.update(day_coverage)
            validation_failed_schedule_ids.update(
                reconciliation.validation_failed_schedule_ids
            )
            batted_balls.extend(inputs[0])
            pitch_metrics.extend(inputs[1])
            swing_metrics.extend(inputs[2])
            persisted += day_persisted
            seen += len(dataset.rows)
            day_excluded = normalized.excluded_count + nonfinal
            excluded += day_excluded
            conflict_count += len(relevant_conflicts)
            final_schedule_ids = {
                game.provider_game_id
                for game, _, _ in schedules
                if game.status is GameStatus.FINAL and game.official_date == current
            }
            incomplete_final_count = len(
                final_schedule_ids - set(reconciliation.schedule_to_game_pk)
            )
            self._complete_checkpoint(
                checkpoint,
                seen=len(dataset.rows),
                persisted=day_persisted,
                rejected=day_excluded + len(relevant_conflicts),
                source_observed_at=dataset.raw.retrieved_at,
                partial=bool(relevant_conflicts or incomplete_final_count),
                cursor_after={
                    "eligible_pitch_count": len(eligible),
                    "excluded_count": day_excluded,
                    "conflict_count": len(relevant_conflicts),
                    "completed_game_count": len(
                        reconciliation.schedule_to_game_pk
                    ),
                    "incomplete_final_game_count": incomplete_final_count,
                },
            )
            current += timedelta(days=1)
        return (
            coverage,
            validation_failed_schedule_ids,
            (batted_balls, pitch_metrics, swing_metrics),
            persisted,
            seen,
            excluded,
            conflict_count,
            artifacts,
            day_count,
        )

    @staticmethod
    def _statcast_swing_feature_input(
        *,
        game_date: date,
        batter_id: str | None,
        metrics: Mapping[str, object],
        batter_is_home: bool | None,
        available_at: datetime,
    ) -> StatcastSwingMetric | None:
        if batter_id is None:
            return None

        bat_speed = optional_float(metrics.get("bat_speed"))
        swing_length = optional_float(metrics.get("swing_length"))
        attack_angle = optional_float(metrics.get("attack_angle"))
        attack_direction = optional_float(metrics.get("attack_direction"))
        swing_path_tilt = optional_float(metrics.get("swing_path_tilt"))
        miss_distance = optional_float(metrics.get("miss_distance"))
        hyper_speed = optional_float(metrics.get("hyper_speed"))

        if all(
            value is None
            for value in (
                bat_speed,
                swing_length,
                attack_angle,
                attack_direction,
                swing_path_tilt,
                miss_distance,
                hyper_speed,
            )
        ):
            return None

        return StatcastSwingMetric(
            game_date,
            batter_id,
            bat_speed=bat_speed,
            swing_length=swing_length,
            attack_angle=attack_angle,
            attack_direction=attack_direction,
            swing_path_tilt=swing_path_tilt,
            miss_distance=miss_distance,
            hyper_speed=hyper_speed,
            pitch_type=str(metrics.get("pitch_type") or "").strip() or None,
            is_home=batter_is_home,
            batter_hand=str(metrics.get("stand") or "").strip() or None,
            opposing_pitcher_hand=(
                str(metrics.get("p_throws") or "").strip() or None
            ),
            available_at=available_at,
        )

    def _statcast_feature_inputs(
        self,
        pitches: Sequence[NormalizedPitch],
        retrieved_at: datetime,
    ) -> tuple[list[StatcastBattedBall], list[StatcastPitchMetric], list[StatcastSwingMetric]]:
        self._load_statcast_player_mapping_inventory()
        batted_balls: list[StatcastBattedBall] = []
        pitch_metrics: list[StatcastPitchMetric] = []
        swing_metrics: list[StatcastSwingMetric] = []
        for pitch in pitches:
            batter_id = self._statcast_player_mapping_cache.get(
                str(pitch.batter_id)
            )
            pitcher_id = self._statcast_player_mapping_cache.get(
                str(pitch.pitcher_id)
            )
            metrics = pitch.metrics
            inning_half = _canonical_statcast_inning_half(
                metrics.get("inning_topbot")
            )
            batter_is_home = (
                False
                if inning_half == "top"
                else True if inning_half == "bottom" else None
            )
            swing_metric = self._statcast_swing_feature_input(
                game_date=pitch.game_date,
                batter_id=batter_id,
                metrics=metrics,
                batter_is_home=batter_is_home,
                available_at=retrieved_at,
            )
            if swing_metric is not None:
                swing_metrics.append(swing_metric)

            if batter_id is not None and (
                optional_float(metrics.get("launch_speed")) is not None
                or optional_float(metrics.get("launch_angle")) is not None
            ):
                batted_balls.append(
                    StatcastBattedBall(
                        pitch.game_date,
                        batter_id,
                        optional_float(metrics.get("launch_speed")),
                        optional_float(metrics.get("launch_angle")),
                        optional_int(metrics.get("launch_speed_angle")),
                        optional_float(metrics.get("estimated_ba_using_speedangle")),
                        optional_float(metrics.get("estimated_slg_using_speedangle")),
                        optional_float(metrics.get("estimated_woba_using_speedangle")),
                        hit_distance_sc=optional_float(
                            metrics.get("hit_distance_sc")
                        ),
                        hc_x=optional_float(metrics.get("hc_x")),
                        hc_y=optional_float(metrics.get("hc_y")),
                        hit_location=optional_int(
                            metrics.get("hit_location")
                        ),
                        is_home=batter_is_home,
                        batter_hand=str(metrics.get("stand") or "").strip() or None,
                        opposing_pitcher_hand=(
                            str(metrics.get("p_throws") or "").strip() or None
                        ),
                        available_at=retrieved_at,
                    )
                )
            pitching_team = (
                pitch.home_team_key
                if inning_half == "top"
                else pitch.away_team_key if inning_half == "bottom" else None
            )
            if pitching_team is not None and pitcher_id is not None:
                pitch_metrics.append(
                    StatcastPitchMetric(
                        pitch.game_date,
                        pitcher_id,
                        pitching_team,
                        str(metrics.get("pitch_type") or "").strip() or None,
                        optional_float(metrics.get("release_speed")),
                        optional_float(metrics.get("release_spin_rate")),
                        optional_float(metrics.get("release_extension")),
                        optional_float(metrics.get("pfx_x")),
                        optional_float(metrics.get("pfx_z")),
                        optional_int(metrics.get("zone")),
                        str(metrics.get("description") or "").strip() or None,
                        effective_speed=optional_float(
                            metrics.get("effective_speed")
                        ),
                        spin_axis=optional_int(metrics.get("spin_axis")),
                        release_pos_x=optional_float(
                            metrics.get("release_pos_x")
                        ),
                        release_pos_y=optional_float(
                            metrics.get("release_pos_y")
                        ),
                        release_pos_z=optional_float(
                            metrics.get("release_pos_z")
                        ),
                        vx0=optional_float(metrics.get("vx0")),
                        vy0=optional_float(metrics.get("vy0")),
                        vz0=optional_float(metrics.get("vz0")),
                        ax=optional_float(metrics.get("ax")),
                        ay=optional_float(metrics.get("ay")),
                        az=optional_float(metrics.get("az")),
                        api_break_x_arm=optional_float(
                            metrics.get("api_break_x_arm")
                        ),
                        api_break_x_batter_in=optional_float(
                            metrics.get("api_break_x_batter_in")
                        ),
                        api_break_z_with_gravity=optional_float(
                            metrics.get("api_break_z_with_gravity")
                        ),
                        arm_angle=optional_float(metrics.get("arm_angle")),
                        sz_top=optional_float(metrics.get("sz_top")),
                        sz_bot=optional_float(metrics.get("sz_bot")),
                        n_thruorder_pitcher=optional_int(
                            metrics.get("n_thruorder_pitcher")
                        ),
                        pitcher_days_since_prev_game=optional_int(
                            metrics.get("pitcher_days_since_prev_game")
                        ),
                        opposing_batter_hand=(
                            str(metrics.get("stand") or "").strip() or None
                        ),
                        available_at=retrieved_at,
                    )
                )
        return batted_balls, pitch_metrics, swing_metrics

    def _load_statcast_feature_inputs(
        self,
        *,
        feature_as_of: date,
        knowledge_cutoff: datetime,
    ) -> tuple[
        tuple[
            list[StatcastBattedBall],
            list[StatcastPitchMetric],
            list[StatcastPitcherPitch],
            list[StatcastSwingMetric],
        ],
        tuple[str, ...],
    ]:
        self._load_statcast_player_mapping_inventory()
        cutoff = _aware_utc(knowledge_cutoff)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH known_revisions AS (
                    SELECT revision.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY revision.pitch_identity_id
                               ORDER BY revision.revision_number DESC
                           ) AS recency_rank
                    FROM statcast_pitch_revisions AS revision
                    WHERE revision.retrieved_at <= ?
                )
                SELECT revision.metrics_json,
                       revision.retrieved_at,
                       revision.source_checksum,
                       identity.game_pk,
                       identity.at_bat_number,
                       identity.pitch_number
                FROM known_revisions AS revision
                JOIN statcast_pitch_identities AS identity
                  ON identity.pitch_identity_id=revision.pitch_identity_id
                WHERE recency_rank=1
                ORDER BY revision.pitch_identity_id
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        batted_balls: list[StatcastBattedBall] = []
        pitch_metrics: list[StatcastPitchMetric] = []
        swing_metrics: list[StatcastSwingMetric] = []
        pitcher_pitches: list[StatcastPitcherPitch] = []
        checksums: set[str] = set()
        for row in rows:
            payload = json.loads(str(row["metrics_json"]))
            if not isinstance(payload, dict):
                raise AcquisitionExecutionError(
                    "persisted Statcast feature metrics are malformed"
                )
            identity = payload.pop("_dse_identity", None)
            if not isinstance(identity, dict):
                continue
            try:
                game_date = date.fromisoformat(str(identity["game_date"]))
                batter_source_id = str(int(identity["batter_id"]))
                pitcher_source_id = str(int(identity["pitcher_id"]))
                home_team_key = str(identity["home_team_key"])
                away_team_key = str(identity["away_team_key"])
                available_at = datetime.fromisoformat(str(row["retrieved_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AcquisitionExecutionError(
                    "persisted Statcast feature identity is malformed"
                ) from exc
            if game_date >= feature_as_of:
                continue
            batter_id = self._statcast_player_mapping_cache.get(batter_source_id)
            pitcher_id = self._statcast_player_mapping_cache.get(pitcher_source_id)
            used = False
            inning_half = _canonical_statcast_inning_half(
                payload.get("inning_topbot")
            )
            batter_is_home = (
                False
                if inning_half == "top"
                else True if inning_half == "bottom" else None
            )
            swing_metric = self._statcast_swing_feature_input(
                game_date=game_date,
                batter_id=batter_id,
                metrics=payload,
                batter_is_home=batter_is_home,
                available_at=available_at,
            )
            if swing_metric is not None:
                swing_metrics.append(swing_metric)

            if batter_id is not None and (
                optional_float(payload.get("launch_speed")) is not None
                or optional_float(payload.get("launch_angle")) is not None
            ):
                batted_balls.append(
                    StatcastBattedBall(
                        game_date,
                        batter_id,
                        optional_float(payload.get("launch_speed")),
                        optional_float(payload.get("launch_angle")),
                        optional_int(payload.get("launch_speed_angle")),
                        optional_float(payload.get("estimated_ba_using_speedangle")),
                        optional_float(payload.get("estimated_slg_using_speedangle")),
                        optional_float(payload.get("estimated_woba_using_speedangle")),
                        hit_distance_sc=optional_float(
                            payload.get("hit_distance_sc")
                        ),
                        hc_x=optional_float(payload.get("hc_x")),
                        hc_y=optional_float(payload.get("hc_y")),
                        hit_location=optional_int(
                            payload.get("hit_location")
                        ),
                        is_home=batter_is_home,
                        batter_hand=str(payload.get("stand") or "").strip() or None,
                        opposing_pitcher_hand=(
                            str(payload.get("p_throws") or "").strip() or None
                        ),
                        available_at=available_at,
                    )
                )
                used = True
            pitching_team = (
                home_team_key
                if inning_half == "top"
                else away_team_key if inning_half == "bottom" else None
            )
            if (
                pitching_team is not None
                and pitcher_id is not None
                and inning_half is not None
            ):
                canonical_pitcher_id = pitcher_id
                pitch_metrics.append(
                    StatcastPitchMetric(
                        game_date,
                        canonical_pitcher_id,
                        pitching_team,
                        str(payload.get("pitch_type") or "").strip() or None,
                        optional_float(payload.get("release_speed")),
                        optional_float(payload.get("release_spin_rate")),
                        optional_float(payload.get("release_extension")),
                        optional_float(payload.get("pfx_x")),
                        optional_float(payload.get("pfx_z")),
                        optional_int(payload.get("zone")),
                        str(payload.get("description") or "").strip() or None,
                        effective_speed=optional_float(
                            payload.get("effective_speed")
                        ),
                        spin_axis=optional_int(payload.get("spin_axis")),
                        release_pos_x=optional_float(
                            payload.get("release_pos_x")
                        ),
                        release_pos_y=optional_float(
                            payload.get("release_pos_y")
                        ),
                        release_pos_z=optional_float(
                            payload.get("release_pos_z")
                        ),
                        vx0=optional_float(payload.get("vx0")),
                        vy0=optional_float(payload.get("vy0")),
                        vz0=optional_float(payload.get("vz0")),
                        ax=optional_float(payload.get("ax")),
                        ay=optional_float(payload.get("ay")),
                        az=optional_float(payload.get("az")),
                        api_break_x_arm=optional_float(
                            payload.get("api_break_x_arm")
                        ),
                        api_break_x_batter_in=optional_float(
                            payload.get("api_break_x_batter_in")
                        ),
                        api_break_z_with_gravity=optional_float(
                            payload.get("api_break_z_with_gravity")
                        ),
                        arm_angle=optional_float(payload.get("arm_angle")),
                        sz_top=optional_float(payload.get("sz_top")),
                        sz_bot=optional_float(payload.get("sz_bot")),
                        n_thruorder_pitcher=optional_int(
                            payload.get("n_thruorder_pitcher")
                        ),
                        pitcher_days_since_prev_game=optional_int(
                            payload.get("pitcher_days_since_prev_game")
                        ),
                        opposing_batter_hand=(
                            str(payload.get("stand") or "").strip() or None
                        ),
                        available_at=available_at,
                    )
                )
                inning = optional_int(payload.get("inning"))
                at_bat_number = optional_int(row["at_bat_number"])
                pitch_number = optional_int(row["pitch_number"])
                if inning is None or at_bat_number is None or pitch_number is None:
                    raise AcquisitionExecutionError(
                        "persisted Statcast pitcher-appearance sequence is malformed"
                    )
                pitcher_pitches.append(
                    StatcastPitcherPitch(
                        game_pk=int(row["game_pk"]),
                        game_date=game_date,
                        pitcher_id=canonical_pitcher_id,
                        team_key=pitching_team,
                        at_bat_number=at_bat_number,
                        pitch_number=pitch_number,
                        inning=inning,
                        inning_half=inning_half,
                        outs_when_up=optional_int(payload.get("outs_when_up")),
                        event=str(payload.get("events") or "").strip() or None,
                        description=(
                            str(payload.get("description") or "").strip() or None
                        ),
                        plate_appearance_classification=(
                            statcast_plate_appearance_classification(payload)
                        ),
                        batter_id=(
                            batter_id if batter_id is not None else None
                        ),
                        batter_team_key=(
                            away_team_key
                            if inning_half == "top"
                            else home_team_key
                            if inning_half == "bottom"
                            else None
                        ),
                        batter_is_home=batter_is_home,
                        batter_hand=str(payload.get("stand") or "").strip() or None,
                        pitcher_hand=(
                            str(payload.get("p_throws") or "").strip() or None
                        ),
                        game_is_final=True,
                        available_at=available_at,
                    )
                )
                used = True
            if used:
                checksums.add(str(row["source_checksum"]))
        return (
            batted_balls,
            pitch_metrics,
            pitcher_pitches,
            swing_metrics,
        ), tuple(sorted(checksums))

    def _load_statcast_reconciliation_pitches(
        self,
        *,
        season_start: date,
        through_date: date,
        knowledge_cutoff: datetime,
    ) -> tuple[NormalizedPitch, ...]:
        """Load one latest known Statcast revision per pitch at an exact date cutoff."""

        if through_date < season_start:
            return ()
        cutoff = _aware_utc(knowledge_cutoff)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH known_revisions AS (
                    SELECT revision.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY revision.pitch_identity_id
                               ORDER BY revision.revision_number DESC,
                                        revision.retrieved_at DESC,
                                        revision.source_checksum DESC
                           ) AS recency_rank
                    FROM statcast_pitch_revisions AS revision
                    WHERE revision.retrieved_at <= ?
                )
                SELECT revision.metrics_json,
                       revision.source_checksum,
                       identity.game_pk,
                       identity.at_bat_number,
                       identity.pitch_number,
                       game.official_date
                FROM known_revisions AS revision
                JOIN statcast_pitch_identities AS identity
                  ON identity.pitch_identity_id=revision.pitch_identity_id
                JOIN stats_game_identities AS game
                  ON game.game_identity_id=identity.game_identity_id
                WHERE recency_rank=1
                  AND game.provider='statcast'
                  AND game.game_type='R'
                  AND game.official_date BETWEEN ? AND ?
                ORDER BY identity.game_pk,
                         identity.at_bat_number,
                         identity.pitch_number
                """,
                (
                    cutoff.isoformat(),
                    season_start.isoformat(),
                    through_date.isoformat(),
                ),
            ).fetchall()
        pitches: list[NormalizedPitch] = []
        for row in rows:
            try:
                payload = json.loads(str(row["metrics_json"]))
                if not isinstance(payload, dict):
                    raise ValueError("metrics are not an object")
                identity = payload.pop("_dse_identity")
                if not isinstance(identity, dict):
                    raise ValueError("identity is not an object")
                game_date = date.fromisoformat(str(identity["game_date"]))
                if game_date.isoformat() != str(row["official_date"]):
                    raise ValueError("game date conflicts with persisted identity")
                pitches.append(
                    NormalizedPitch(
                        game_pk=int(row["game_pk"]),
                        game_date=game_date,
                        at_bat_number=int(row["at_bat_number"]),
                        pitch_number=int(row["pitch_number"]),
                        batter_id=int(identity["batter_id"]),
                        pitcher_id=int(identity["pitcher_id"]),
                        home_team_key=str(identity["home_team_key"]),
                        away_team_key=str(identity["away_team_key"]),
                        metrics=payload,
                        checksum_sha256=str(row["source_checksum"]),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AcquisitionExecutionError(
                    "Persisted Statcast counting-stat reconciliation evidence is malformed"
                ) from exc
        return tuple(pitches)

    def _restore_statcast_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        artifacts: Mapping[str, RawArtifact],
        schedules: Sequence[ScheduleRecord],
    ) -> tuple[
        dict[str, int],
        set[str],
        tuple[list[StatcastBattedBall], list[StatcastPitchMetric], list[StatcastSwingMetric]],
        int,
        int,
        int,
        int,
        list[RawArtifact],
    ]:
        if len(artifacts) != 1:
            raise AcquisitionResumeError(
                "completed Statcast day checkpoint must own exactly one raw payload"
            )
        artifact = next(iter(artifacts.values()))
        payload = self.raw_store.read_verified(artifact)
        columns, rows = parse_csv_bytes(
            payload, capture=artifact, member_name="resumed Statcast response"
        )
        if {"game_pk", "game_date"} - set(columns):
            raise AcquisitionResumeError(
                "completed Statcast day checkpoint raw payload is malformed"
        )
        normalized = normalize_statcast_rows(rows)
        final_keys = {
            (game.official_date, game.home_team_key, game.away_team_key)
            for game, _, _ in schedules
            if game.status is GameStatus.FINAL
        }
        relevant_game_pks = {
            pitch.game_pk
            for pitch in normalized.pitches
            if (pitch.game_date, pitch.home_team_key, pitch.away_team_key)
            in final_keys
        }
        conflicts = tuple(
            item
            for item in normalized.conflicts
            if item[0][0] in relevant_game_pks
        )
        reconciliation = _reconcile_completed_statcast_games(
            schedules, normalized.pitches, conflicts
        )
        eligible = tuple(
            pitch
            for pitch in normalized.pitches
            if pitch.game_pk in reconciliation.completed_game_pks
        )
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"]))
        except (TypeError, ValueError) as exc:
            raise AcquisitionResumeError(
                "completed Statcast checkpoint lacks reconciliation metadata"
            ) from exc
        nonfinal = len(normalized.pitches) - len(eligible)
        excluded = normalized.excluded_count + nonfinal
        current_day = date.fromisoformat(str(checkpoint["dataset_key"]).split(":", 1)[1])
        final_schedule_ids = {
            game.provider_game_id
            for game, _, _ in schedules
            if game.status is GameStatus.FINAL and game.official_date == current_day
        }
        incomplete_final_count = len(
            final_schedule_ids - set(reconciliation.schedule_to_game_pk)
        )
        if (
            int(checkpoint["records_seen"]) != len(rows)
            or int(checkpoint["records_rejected"]) != excluded + len(conflicts)
            or cursor.get("eligible_pitch_count") != len(eligible)
            or cursor.get("excluded_count") != excluded
            or cursor.get("conflict_count") != len(conflicts)
            or cursor.get("completed_game_count")
            != len(reconciliation.schedule_to_game_pk)
            or cursor.get("incomplete_final_game_count") != incomplete_final_count
        ):
            raise AcquisitionResumeError(
                "completed Statcast checkpoint counts do not reconcile"
            )
        for pitch in eligible:
            if not self.repository.has_statcast_revision(
                game_pk=pitch.game_pk,
                at_bat_number=pitch.at_bat_number,
                pitch_number=pitch.pitch_number,
                source_checksum=pitch.checksum_sha256,
            ):
                raise AcquisitionResumeError(
                    "completed Statcast checkpoint lacks normalized pitch evidence"
                )
        return (
            dict(reconciliation.schedule_to_game_pk),
            set(reconciliation.validation_failed_schedule_ids),
            self._statcast_feature_inputs(eligible, artifact.retrieved_at),
            len(rows),
            int(checkpoint["records_persisted"]),
            excluded,
            len(conflicts),
            [artifact],
        )

    def _schedule_sources(self) -> list[str]:
        fixture_transport = self._fixture_transport()
        if fixture_transport is not None:
            prefix = "baseball_reference_schedule_"
            sources = sorted(
                key.removeprefix(prefix)
                for key in fixture_transport.fixtures
                if key.startswith(prefix)
            )
            if not sources:
                raise AcquisitionConfigurationError(
                    "offline current collection requires at least one "
                    "baseball_reference_schedule_<TEAM>.html fixture"
                )
            return sources
        return sorted(TEAM_SOURCE_ALIASES["baseball_reference"])

    @staticmethod
    def _schedule_observation_subject(record: ScheduleRecord) -> str | None:
        source_row = record[0].source_row
        if not isinstance(source_row, Mapping):
            return None
        source_id = str(
            source_row.get("_dse_subject_team_source_id") or ""
        ).strip()
        if not source_id:
            return None
        identity = resolve_team_identity("baseball_reference", source_id)
        return identity.canonical_team_key

    @classmethod
    def _schedule_observation_subjects(
        cls, observations: Sequence[ScheduleRecord]
    ) -> set[str]:
        return {
            subject
            for record in observations
            if (subject := cls._schedule_observation_subject(record)) is not None
        }

    @staticmethod
    def _schedule_reconciliation_payload(game: ScheduleGame) -> dict[str, object]:
        return {
            "official_date": game.official_date.isoformat(),
            "home_team_key": game.home_team_key,
            "away_team_key": game.away_team_key,
            "status": game.status.value,
            "home_score": game.home_score,
            "away_score": game.away_score,
        }

    def _schedule_facts(self, observations: Sequence[ScheduleRecord]) -> None:
        statuses = {record[0].status for record in observations}
        if len(statuses) != 1:
            raise AcquisitionExecutionError(
                "Baseball-Reference schedule observations disagree on game status"
            )
        score_facts = {
            (record[0].home_score, record[0].away_score)
            for record in observations
            if record[0].home_score is not None or record[0].away_score is not None
        }
        if len(score_facts) > 1:
            raise AcquisitionExecutionError(
                "Baseball-Reference schedule observations disagree on final score"
            )
        if next(iter(statuses)) is GameStatus.FINAL and (
            len(score_facts) != 1 or None in next(iter(score_facts))
        ):
            raise AcquisitionExecutionError(
                "Baseball-Reference final schedule observations lack matching scores"
            )

    def _reconcile_schedule_group(
        self,
        canonical_provider_game_id: str,
        observations: Sequence[ScheduleRecord],
    ) -> tuple[ScheduleRecord, list[ScheduleRecord]]:
        if not observations:
            raise AcquisitionExecutionError("schedule reconciliation group is empty")
        self._schedule_facts(observations)
        rewritten = [
            (
                ScheduleGame(
                    provider_game_id=canonical_provider_game_id,
                    official_date=game.official_date,
                    home_team_key=game.home_team_key,
                    away_team_key=game.away_team_key,
                    status=game.status,
                    status_reason=game.status_reason,
                    home_score=game.home_score,
                    away_score=game.away_score,
                    source_row=game.source_row,
                ),
                raw_id,
                artifact,
            )
            for game, raw_id, artifact in observations
        ]
        representative = max(
            rewritten,
            key=lambda record: (
                record[2].retrieved_at,
                record[1],
            ),
        )
        return representative, rewritten

    def _restore_legacy_schedule_checkpoint(
        self,
        context: _RunContext,
        checkpoint: Mapping[str, Any],
        artifacts: Mapping[str, RawArtifact],
    ) -> _ScheduleCollection:
        rows = self.repository.list_checkpoint_schedule_observations(
            context.stats_run_id, str(checkpoint["checkpoint_id"])
        )
        grouped: defaultdict[str, list[ScheduleRecord]] = defaultdict(list)
        for row in rows:
            raw_id = str(row["raw_payload_id"])
            artifact = artifacts.get(raw_id)
            if artifact is None:
                raise AcquisitionResumeError(
                    "completed schedule checkpoint references missing raw evidence"
                )
            status_payload = json.loads(str(row["status_json"]))
            source_row = status_payload.get("source_row")
            grouped[str(row["provider_game_id"])].append(
                (
                    ScheduleGame(
                        provider_game_id=str(row["provider_game_id"]),
                        official_date=date.fromisoformat(str(row["official_date"])),
                        home_team_key=str(row["home_team_key"]),
                        away_team_key=str(row["away_team_key"]),
                        status=GameStatus(str(row["abstract_state"])),
                        status_reason=str(row["detailed_state"] or "restored_checkpoint"),
                        home_score=optional_int(status_payload.get("home_score")),
                        away_score=optional_int(status_payload.get("away_score")),
                        source_row=source_row if isinstance(source_row, dict) else None,
                    ),
                    raw_id,
                    artifact,
                )
            )
        restored: list[ScheduleRecord] = []
        for provider_game_id, observations in sorted(grouped.items()):
            representative, _ = self._reconcile_schedule_group(
                provider_game_id, observations
            )
            restored.append(representative)
        if len(restored) != int(checkpoint["records_persisted"]):
            raise AcquisitionResumeError(
                "completed schedule checkpoint record count does not reconcile"
            )
        restored.sort(
            key=lambda record: (record[0].official_date, record[0].provider_game_id)
        )
        raw_ids = {
            raw_id: (raw_id, artifact) for raw_id, artifact in artifacts.items()
        }
        source_ids: tuple[str, ...]
        reconciled_pair_count = 0
        incomplete_pair_count = 0
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"] or "{}"))
            season_start = date.fromisoformat(str(cursor["season_start"]))
            if cursor.get("contract") == "DSE_BREF_SCHEDULE_CHECKPOINT_V1":
                source_ids = tuple(sorted(str(value) for value in cursor["source_ids"]))
                reconciled_pair_count = int(cursor["reconciled_pair_count"])
                incomplete_pair_count = int(cursor["incomplete_pair_count"])
            else:
                raise KeyError("contract")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            season_start = min(
                (record[0].official_date for record in restored),
                default=date(context.season, 1, 1),
            )
            source_ids = tuple(sorted(raw_ids))
            for observations in grouped.values():
                subjects = self._schedule_observation_subjects(observations)
                expected = {
                    observations[0][0].home_team_key,
                    observations[0][0].away_team_key,
                }
                if len(observations) == 2 and subjects == expected:
                    reconciled_pair_count += 1
                else:
                    incomplete_pair_count += 1
        return _ScheduleCollection(
            tuple(restored),
            raw_ids,
            int(checkpoint["records_seen"]),
            len(rows),
            season_start,
            source_ids,
            reconciled_pair_count,
            incomplete_pair_count,
        )

    def _restore_schedule_checkpoint(
        self,
        context: _RunContext,
        checkpoint: Mapping[str, Any],
        artifacts: Mapping[str, RawArtifact],
        request: AcquisitionRequest,
    ) -> _ScheduleCollection:
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"] or "{}"))
            if cursor["contract"] != "DSE_BREF_SCHEDULE_CHECKPOINT_V2":
                raise ValueError("unsupported schedule checkpoint contract")
            if cursor["command"] != context.command.value:
                raise ValueError("schedule command changed")
            if cursor["requested_through_date"] != (
                request.requested_through_date.isoformat()
            ):
                raise ValueError("schedule requested-through date changed")
            season_start = date.fromisoformat(str(cursor["season_start"]))
            source_ids = tuple(sorted(str(value) for value in cursor["source_ids"]))
            raw_by_source = {
                str(source_id): str(raw_id)
                for source_id, raw_id in dict(
                    cursor["raw_payloads_by_source_id"]
                ).items()
            }
            if set(raw_by_source) != set(source_ids):
                raise ValueError("schedule raw-source identity changed")
            if set(raw_by_source.values()) != set(artifacts):
                raise ValueError("schedule raw payload inventory changed")
            expected_reconciled = int(cursor["reconciled_pair_count"])
            expected_incomplete = int(cursor["incomplete_pair_count"])
            expected_observations = int(cursor["persisted_observation_count"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcquisitionResumeError(
                "completed Baseball-Reference schedule checkpoint lacks "
                "deterministic raw replay evidence"
            ) from exc

        grouped: defaultdict[
            tuple[date, str, str, int], list[ScheduleRecord]
        ] = defaultdict(list)
        occurrences: defaultdict[tuple[str, date, str, str], int] = defaultdict(int)
        seen = 0
        observed_season_start: date | None = None
        raw_ids: dict[str, tuple[str, RawArtifact]] = {}
        for source_id in source_ids:
            try:
                require_active_team_identity("baseball_reference", source_id)
                raw_id = raw_by_source[source_id]
                artifact = artifacts[raw_id]
                table = BaseballReferenceProvider(
                    _ArtifactReplayTransport(artifact)
                ).collect_table(
                    f"/teams/{source_id}/{context.season}-schedule-scores.shtml",
                    table_id="team_schedule",
                    fixture_key=f"resume_schedule_{source_id}",
                    persistent_cache=False,
                )
            except Exception as exc:
                raise AcquisitionResumeError(
                    "completed Baseball-Reference schedule raw payload is malformed"
                ) from exc
            raw_ids[source_id] = (raw_id, artifact)
            regular_source_rows = 0
            for index, row in enumerate(table.rows, start=1):
                classification = _classify_baseball_reference_schedule_row(row)
                if not classification.is_regular_season:
                    continue
                regular_source_rows += 1
                normalized_row = {
                    **row,
                    "_dse_subject_team_source_id": source_id,
                    "game_type": classification.normalized_game_type,
                    "_dse_game_type_source": classification.evidence_source,
                }
                try:
                    game = normalize_baseball_reference_schedule_row(
                        normalized_row,
                        season=context.season,
                        subject_team_source_id=source_id,
                        row_index=index,
                    )
                except ValueError as exc:
                    raise AcquisitionResumeError(
                        "completed Baseball-Reference schedule normalization changed"
                    ) from exc
                if (
                    observed_season_start is None
                    or game.official_date < observed_season_start
                ):
                    observed_season_start = game.official_date
                in_scope = (
                    game.official_date == request.requested_through_date
                    if context.command is AcquisitionCommand.DAILY
                    else game.official_date <= request.requested_through_date
                )
                if not in_scope:
                    continue
                seen += 1
                occurrence_key = (
                    source_id,
                    game.official_date,
                    game.home_team_key,
                    game.away_team_key,
                )
                occurrences[occurrence_key] += 1
                grouped[
                    (
                        game.official_date,
                        game.home_team_key,
                        game.away_team_key,
                        occurrences[occurrence_key],
                    )
                ].append((game, raw_id, artifact))
            if not table.rows or regular_source_rows == 0:
                raise AcquisitionResumeError(
                    "completed Baseball-Reference schedule raw payload is empty"
                )

        restored: list[ScheduleRecord] = []
        reconciled_pair_count = 0
        incomplete_pair_count = 0
        for group_key, observations in sorted(grouped.items()):
            official_date, home_team, away_team, occurrence = group_key
            provider_ids = {
                record[0].provider_game_id
                for record in observations
                if not record[0].provider_game_id.startswith("bref:")
            }
            if len(provider_ids) > 1:
                raise AcquisitionResumeError(
                    "completed Baseball-Reference schedules disagree on identity"
                )
            canonical_provider_game_id = (
                f"bref:{official_date.isoformat()}:{away_team}:"
                f"{home_team}:{occurrence}"
            )
            representative, reconciled = self._reconcile_schedule_group(
                canonical_provider_game_id, observations
            )
            expected_subjects = {home_team, away_team}
            observed_subjects = self._schedule_observation_subjects(reconciled)
            observations_by_subject = {
                subject: self._schedule_reconciliation_payload(record[0])
                for record in reconciled
                if (subject := self._schedule_observation_subject(record)) is not None
            }
            pair_result = reconcile_schedule_pair(
                game_key=canonical_provider_game_id,
                home_observation=observations_by_subject.get(home_team),
                away_observation=observations_by_subject.get(away_team),
            )
            if (
                len(reconciled) == 2
                and observed_subjects == expected_subjects
                and pair_result.status == "passed"
            ):
                reconciled_pair_count += 1
            else:
                incomplete_pair_count += 1
            for game, raw_id, artifact in reconciled:
                if not self.repository.has_game_status_evidence(
                    {
                        "stats_run_id": context.stats_run_id,
                        "raw_payload_id": raw_id,
                        "game_identity_id": (
                            f"game:baseball_reference:{game.provider_game_id}"
                        ),
                        "retrieved_at": artifact.retrieved_at,
                        "source_checksum": artifact.checksum_sha256,
                        "abstract_state": game.status.value,
                        "detailed_state": game.status_reason,
                        "status_code": game.status.value,
                        "status": {
                            "status": game.status.value,
                            "status_reason": game.status_reason,
                            "home_score": game.home_score,
                            "away_score": game.away_score,
                            "source_row": game.source_row,
                        },
                    }
                ):
                    raise AcquisitionResumeError(
                        "completed schedule checkpoint lacks global normalized evidence"
                    )
            restored.append(representative)

        if (
            seen != int(checkpoint["records_seen"])
            or len(restored) != int(checkpoint["records_persisted"])
            or reconciled_pair_count != expected_reconciled
            or incomplete_pair_count != expected_incomplete
            or observed_season_start != season_start
        ):
            raise AcquisitionResumeError(
                "completed Baseball-Reference schedule checkpoint counts do not reconcile"
            )
        restored.sort(
            key=lambda record: (record[0].official_date, record[0].provider_game_id)
        )
        return _ScheduleCollection(
            tuple(restored),
            raw_ids,
            seen,
            expected_observations,
            season_start,
            source_ids,
            reconciled_pair_count,
            incomplete_pair_count,
        )

    def _collect_schedules(
        self,
        context: _RunContext,
        checkpoint_id: str,
        request: AcquisitionRequest,
    ) -> _ScheduleCollection:
        provider = BaseballReferenceProvider(self.transport)
        raw_ids: dict[str, tuple[str, RawArtifact]] = {}
        grouped: defaultdict[
            tuple[date, str, str, int], list[ScheduleRecord]
        ] = defaultdict(list)
        occurrences: defaultdict[tuple[str, date, str, str], int] = defaultdict(int)
        seen = 0
        season_start: date | None = None
        source_ids = tuple(self._schedule_sources())
        expected_sources = set(TEAM_SOURCE_ALIASES["baseball_reference"])
        if self._fixture_transport() is None and set(source_ids) != expected_sources:
            raise AcquisitionExecutionError(
                "live Baseball-Reference schedule collection requires all 30 active clubs"
            )
        for source_id in source_ids:
            require_active_team_identity("baseball_reference", source_id)
            table = provider.collect_table(
                f"/teams/{source_id}/{request.requested_through_date.year}-schedule-scores.shtml",
                table_id="team_schedule",
                fixture_key=f"baseball_reference_schedule_{source_id}",
                persistent_cache=False,
            )
            raw_id = self._persist_raw(context, checkpoint_id, table.raw)
            raw_ids[source_id] = (raw_id, table.raw)
            regular_source_rows = 0
            for index, row in enumerate(table.rows, start=1):
                classification = _classify_baseball_reference_schedule_row(row)
                if not classification.is_regular_season:
                    source_row = dict(row)
                    source_checksum = _checksum(source_row)
                    raw_effective_date = str(row.get("date_game") or "").strip()[:10]
                    try:
                        source_effective_date: date | None = date.fromisoformat(
                            raw_effective_date
                        )
                    except ValueError:
                        source_effective_date = None
                    self.repository.record_excluded_source_row(
                        {
                            "excluded_row_id": self.id_factory("excluded"),
                            "stats_run_id": context.stats_run_id,
                            "raw_payload_id": raw_id,
                            "provider": "baseball_reference",
                            "dataset_key": "team_schedule",
                            "source_row_id": f"{source_id}:{index}",
                            "classification": (
                                classification.excluded_classification or "malformed"
                            ),
                            "reason_code": (
                                classification.exclusion_reason
                                or "ambiguous_game_type"
                            ),
                            "source_effective_date": source_effective_date,
                            "observed_at": table.raw.retrieved_at,
                            "source_row_checksum": source_checksum,
                            "details": {
                                "source_team_id": source_id,
                                "row_index": index,
                                "classification_evidence_source": (
                                    classification.evidence_source
                                ),
                                "source_row": source_row,
                            },
                        }
                    )
                    continue
                regular_source_rows += 1
                normalized_row = {
                    **row,
                    "_dse_subject_team_source_id": source_id,
                    "game_type": classification.normalized_game_type,
                    "_dse_game_type_source": classification.evidence_source,
                }
                game = normalize_baseball_reference_schedule_row(
                    normalized_row,
                    season=request.requested_through_date.year,
                    subject_team_source_id=source_id,
                    row_index=index,
                )
                if season_start is None or game.official_date < season_start:
                    season_start = game.official_date
                if context.command is AcquisitionCommand.DAILY:
                    in_scope = game.official_date == request.requested_through_date
                else:
                    in_scope = game.official_date <= request.requested_through_date
                if not in_scope:
                    continue
                seen += 1
                occurrence_key = (
                    source_id,
                    game.official_date,
                    game.home_team_key,
                    game.away_team_key,
                )
                occurrences[occurrence_key] += 1
                group_key = (
                    game.official_date,
                    game.home_team_key,
                    game.away_team_key,
                    occurrences[occurrence_key],
                )
                grouped[group_key].append((game, raw_id, table.raw))
            if not table.rows or regular_source_rows == 0:
                raise AcquisitionExecutionError(
                    "Baseball-Reference club schedule contains no positively "
                    "classified regular-season rows"
                )
        games: list[ScheduleRecord] = []
        persisted_observations = 0
        reconciled_pair_count = 0
        incomplete_pair_count = 0
        for group_key, observations in sorted(grouped.items()):
            official_date, home_team, away_team, occurrence = group_key
            provider_ids = {
                record[0].provider_game_id
                for record in observations
                if not record[0].provider_game_id.startswith("bref:")
            }
            if len(provider_ids) > 1:
                raise AcquisitionExecutionError(
                    "Baseball-Reference club schedules disagree on box-score identity"
                )
            # The box-score identifier is absent before many games and appears only
            # after the source publishes a box score. Keep the logical schedule-game
            # identity stable across that lifecycle; the exact provider identifier
            # remains preserved in each raw source row and status observation.
            canonical_provider_game_id = (
                f"bref:{official_date.isoformat()}:{away_team}:"
                f"{home_team}:{occurrence}"
            )
            representative, reconciled = self._reconcile_schedule_group(
                canonical_provider_game_id, observations
            )
            expected_subjects = {home_team, away_team}
            observed_subjects = self._schedule_observation_subjects(reconciled)
            observations_by_subject = {
                subject: self._schedule_reconciliation_payload(record[0])
                for record in reconciled
                if (subject := self._schedule_observation_subject(record)) is not None
            }
            pair_result = reconcile_schedule_pair(
                game_key=canonical_provider_game_id,
                home_observation=observations_by_subject.get(home_team),
                away_observation=observations_by_subject.get(away_team),
            )
            if (
                len(reconciled) == 2
                and observed_subjects == expected_subjects
                and pair_result.status == "passed"
            ):
                reconciled_pair_count += 1
            else:
                incomplete_pair_count += 1
                if self._fixture_transport() is None:
                    raise AcquisitionExecutionError(
                        "Baseball-Reference game is not represented by both club schedules"
                    )
            for game, raw_id, artifact in reconciled:
                persisted_observations += self._persist_schedule_game(
                    context, game, raw_id, artifact
                )
            games.append(representative)
        games.sort(key=lambda item: (item[0].official_date, item[0].provider_game_id))
        return _ScheduleCollection(
            tuple(games),
            raw_ids,
            seen,
            persisted_observations,
            season_start or request.requested_through_date,
            source_ids,
            reconciled_pair_count,
            incomplete_pair_count,
        )

    def _source_for_canonical(self, provider: str, canonical: str) -> str:
        candidates = sorted(
            source
            for source, mapped in TEAM_SOURCE_ALIASES[provider].items()
            if mapped == canonical
        )
        if not candidates:
            raise AcquisitionExecutionError(
                f"No exact {provider} team alias exists for canonical team {canonical}"
            )
        return candidates[0]

    def _persist_schedule_game(
        self,
        context: _RunContext,
        game: ScheduleGame,
        raw_id: str,
        artifact: RawArtifact,
    ) -> int:
        home_source = self._source_for_canonical("baseball_reference", game.home_team_key)
        away_source = self._source_for_canonical("baseball_reference", game.away_team_key)
        home_id, _ = self._team_identity(
            "baseball_reference", home_source, artifact.retrieved_at
        )
        away_id, _ = self._team_identity(
            "baseball_reference", away_source, artifact.retrieved_at
        )
        game_id = self._stable_game_identity(
            provider="baseball_reference",
            provider_game_id=game.provider_game_id,
            official_date=game.official_date,
            home_team_identity_id=home_id,
            away_team_identity_id=away_id,
            observed_at=artifact.retrieved_at,
        )
        observation = self.repository.record_game_status(
            {
                "stats_run_id": context.stats_run_id,
                "game_identity_id": game_id,
                "raw_payload_id": raw_id,
                "retrieved_at": artifact.retrieved_at,
                "source_checksum": artifact.checksum_sha256,
                "abstract_state": game.status.value,
                "detailed_state": game.status_reason,
                "status_code": game.status.value,
                "status": {
                    "status": game.status.value,
                    "status_reason": game.status_reason,
                    "home_score": game.home_score,
                    "away_score": game.away_score,
                    "source_row": game.source_row,
                },
            }
        )
        self.repository.link_raw_entity(
            {
                "raw_payload_id": raw_id,
                "link_role": "contains_schedule_game",
                "game_identity_id": game_id,
            }
        )
        return int(observation["stats_run_id"] == context.stats_run_id)

    def _persist_statcast(
        self,
        context: _RunContext,
        dataset: StatcastDataset,
        raw_id: str,
        pitches: Sequence[NormalizedPitch],
        conflicts: Sequence[tuple[tuple[int, int, int], str, str]],
        *,
        game_matches: Mapping[int, str],
    ) -> tuple[dict[str, int], tuple[list[StatcastBattedBall], list[StatcastPitchMetric], list[StatcastSwingMetric]], int]:
        coverage = {schedule_id: game_pk for game_pk, schedule_id in game_matches.items()}
        batted_balls: list[StatcastBattedBall] = []
        pitch_metrics: list[StatcastPitchMetric] = []
        swing_metrics: list[StatcastSwingMetric] = []
        pitches_by_game: defaultdict[int, list[NormalizedPitch]] = defaultdict(list)
        for pitch in pitches:
            pitches_by_game[pitch.game_pk].append(pitch)
        completion_evidence = {
            game_pk: evidence
            for game_pk, game_pitches in pitches_by_game.items()
            if (
                evidence := _schedule_confirmed_statcast_game(game_pitches)
            ) is not None
        }
        if set(completion_evidence) != set(game_matches):
            raise AcquisitionExecutionError(
                "Statcast persistence requires exact completed-game reconciliation"
            )

        team_ids: dict[str, str] = {}
        for team_key in sorted(
            {
                team_key
                for pitch in pitches
                for team_key in (pitch.home_team_key, pitch.away_team_key)
            }
        ):
            source_id = self._source_for_canonical("statcast", team_key)
            team_ids[team_key], _ = self._team_identity(
                "statcast", source_id, dataset.raw.retrieved_at
            )

        game_ids: dict[int, str] = {}
        for game_pk, game_pitches in sorted(pitches_by_game.items()):
            representative = game_pitches[0]
            game_ids[game_pk] = self._stable_game_identity(
                provider="statcast",
                provider_game_id=str(game_pk),
                official_date=representative.game_date,
                home_team_identity_id=team_ids[representative.home_team_key],
                away_team_identity_id=team_ids[representative.away_team_key],
                observed_at=dataset.raw.retrieved_at,
            )

        canonical_player_ids: dict[str, str | None] = {}
        for source_player_id in sorted(
            {
                str(player_id)
                for pitch in pitches
                for player_id in (pitch.batter_id, pitch.pitcher_id)
            },
            key=int,
        ):
            player_identity_id = self._player_identity(
                "statcast", source_player_id, dataset.raw.retrieved_at
            )
            canonical_player_ids[source_player_id] = self._map_statcast_player(
                context,
                source_player_id=source_player_id,
                player_identity_id=player_identity_id,
                artifact=dataset.raw,
            )

        pitch_records: list[dict[str, object]] = []
        pitch_ids: dict[tuple[int, int, int], str] = {}
        for pitch in pitches:
            key = (pitch.game_pk, pitch.at_bat_number, pitch.pitch_number)
            pitch_id = (
                f"pitch:statcast:{pitch.game_pk}:"
                f"{pitch.at_bat_number}:{pitch.pitch_number}"
            )
            pitch_ids[key] = pitch_id
            pitch_records.append(
                {
                    "pitch_identity_id": pitch_id,
                    "provider": "statcast",
                    "game_identity_id": game_ids[pitch.game_pk],
                    "provider_pitch_id": (
                        f"{pitch.game_pk}:{pitch.at_bat_number}:"
                        f"{pitch.pitch_number}"
                    ),
                    "game_pk": pitch.game_pk,
                    "at_bat_number": pitch.at_bat_number,
                    "pitch_number": pitch.pitch_number,
                    "first_seen_at": dataset.raw.retrieved_at,
                    "last_seen_at": dataset.raw.retrieved_at,
                }
            )

        existing_pitches = self.repository.get_statcast_pitch_identities(
            tuple(pitch_ids)
        )
        identities_to_upsert: list[dict[str, object]] = []
        observed_at = dataset.raw.retrieved_at.isoformat()
        for pitch, record in zip(pitches, pitch_records, strict=True):
            key = (pitch.game_pk, pitch.at_bat_number, pitch.pitch_number)
            existing = existing_pitches.get(key)
            if existing is None:
                identities_to_upsert.append(record)
                continue
            expected_facts = {
                "pitch_identity_id": record["pitch_identity_id"],
                "provider": "statcast",
                "game_identity_id": record["game_identity_id"],
                "provider_pitch_id": record["provider_pitch_id"],
                "game_pk": record["game_pk"],
                "at_bat_number": record["at_bat_number"],
                "pitch_number": record["pitch_number"],
            }
            mismatches = [
                field
                for field, expected in expected_facts.items()
                if existing.get(field) != expected
            ]
            if mismatches:
                raise AcquisitionExecutionError(
                    "Statcast pitch identity conflicts with retained facts: "
                    + ", ".join(sorted(mismatches))
                )
            if str(existing["last_seen_at"]) < observed_at:
                identities_to_upsert.append(record)
        self.repository.upsert_pitch_identities(identities_to_upsert)

        latest_revisions = self.repository.get_latest_statcast_revisions(
            tuple(pitch_ids.values())
        )
        revision_records: list[dict[str, object]] = []
        for pitch in pitches:
            pitch_id = pitch_ids[
                (pitch.game_pk, pitch.at_bat_number, pitch.pitch_number)
            ]
            latest = latest_revisions.get(pitch_id)
            revision_records.append(
                {
                    "pitch_identity_id": pitch_id,
                    "stats_run_id": context.stats_run_id,
                    "raw_payload_id": raw_id,
                    "revision_number": (
                        1 if latest is None else int(latest["revision_number"]) + 1
                    ),
                    "revision_kind": "initial" if latest is None else "correction",
                    "retrieved_at": dataset.raw.retrieved_at,
                    "source_checksum": pitch.checksum_sha256,
                    "metrics": {
                        **dict(pitch.metrics),
                        "_dse_identity": {
                            "game_date": pitch.game_date.isoformat(),
                            "batter_id": pitch.batter_id,
                            "pitcher_id": pitch.pitcher_id,
                            "home_team_key": pitch.home_team_key,
                            "away_team_key": pitch.away_team_key,
                        },
                    },
                }
            )
        revision_results = self.repository.record_statcast_revisions(revision_records)
        persisted = sum(
            1
            for result in revision_results
            if result["stats_run_id"] == context.stats_run_id
            and result["raw_payload_id"] == raw_id
        )
        self.repository.link_raw_entities(
            tuple(
                {
                    "raw_payload_id": raw_id,
                    "link_role": "contains_statcast_pitch",
                    "pitch_identity_id": pitch_id,
                }
                for pitch_id in pitch_ids.values()
            )
        )

        for game_pk, game_id in sorted(game_ids.items()):
            evidence = completion_evidence[game_pk]
            pitch_evidence_checksum = _checksum(
                sorted(
                    pitch.checksum_sha256
                    for pitch in pitches_by_game[game_pk]
                )
            )
            completion_source_evidence = {
                "contract": STATCAST_COMPLETION_EVIDENCE_CONTRACT,
                "adapter_version": STATCAST_ADAPTER_VERSION,
                "raw_capture_checksum": dataset.raw.checksum_sha256,
                "game_pk": game_pk,
                "baseball_reference_game_id": game_matches[game_pk],
                "pitch_evidence_checksum": pitch_evidence_checksum,
                "pitch_count": len(pitches_by_game[game_pk]),
                "home_score": evidence.home_score,
                "away_score": evidence.away_score,
            }
            self.repository.record_game_status(
                {
                    "stats_run_id": context.stats_run_id,
                    "game_identity_id": game_id,
                    "raw_payload_id": raw_id,
                    "retrieved_at": dataset.raw.retrieved_at,
                    "source_checksum": _checksum(completion_source_evidence),
                    "abstract_state": "final",
                    "detailed_state": "validated complete Statcast game coverage",
                    "status_code": "final",
                    "status": {
                        "game_pk": game_pk,
                        "baseball_reference_game_id": game_matches[game_pk],
                        "completion_contract": STATCAST_COMPLETION_CONTRACT,
                        "completion_source_evidence": completion_source_evidence,
                        "pitch_count": len(pitches_by_game[game_pk]),
                        "home_score": evidence.home_score,
                        "away_score": evidence.away_score,
                        "pitch_coverage": "complete",
                    },
                }
            )

        for pitch in pitches:
            canonical_batter_id = canonical_player_ids[str(pitch.batter_id)]
            canonical_pitcher_id = canonical_player_ids[str(pitch.pitcher_id)]
            metrics = pitch.metrics
            inning_half = _canonical_statcast_inning_half(
                metrics.get("inning_topbot")
            )
            batter_is_home = (
                False
                if inning_half == "top"
                else True if inning_half == "bottom" else None
            )
            swing_metric = self._statcast_swing_feature_input(
                game_date=pitch.game_date,
                batter_id=canonical_batter_id,
                metrics=metrics,
                batter_is_home=batter_is_home,
                available_at=dataset.raw.retrieved_at,
            )
            if swing_metric is not None:
                swing_metrics.append(swing_metric)

            if canonical_batter_id is not None and (
                optional_float(metrics.get("launch_speed")) is not None
                or optional_float(metrics.get("launch_angle")) is not None
            ):
                batted_balls.append(
                    StatcastBattedBall(
                        pitch.game_date,
                        canonical_batter_id,
                        optional_float(metrics.get("launch_speed")),
                        optional_float(metrics.get("launch_angle")),
                        optional_int(metrics.get("launch_speed_angle")),
                        optional_float(metrics.get("estimated_ba_using_speedangle")),
                        optional_float(metrics.get("estimated_slg_using_speedangle")),
                        optional_float(metrics.get("estimated_woba_using_speedangle")),
                        hit_distance_sc=optional_float(
                            metrics.get("hit_distance_sc")
                        ),
                        hc_x=optional_float(metrics.get("hc_x")),
                        hc_y=optional_float(metrics.get("hc_y")),
                        hit_location=optional_int(
                            metrics.get("hit_location")
                        ),
                        is_home=batter_is_home,
                        batter_hand=str(metrics.get("stand") or "").strip() or None,
                        opposing_pitcher_hand=(
                            str(metrics.get("p_throws") or "").strip() or None
                        ),
                        available_at=dataset.raw.retrieved_at,
                    )
                )
            pitching_team = (
                pitch.home_team_key
                if inning_half == "top"
                else pitch.away_team_key if inning_half == "bottom" else None
            )
            if pitching_team is not None and canonical_pitcher_id is not None:
                pitch_metrics.append(
                    StatcastPitchMetric(
                        pitch.game_date,
                        canonical_pitcher_id,
                        pitching_team,
                        str(metrics.get("pitch_type") or "").strip() or None,
                        optional_float(metrics.get("release_speed")),
                        optional_float(metrics.get("release_spin_rate")),
                        optional_float(metrics.get("release_extension")),
                        optional_float(metrics.get("pfx_x")),
                        optional_float(metrics.get("pfx_z")),
                        optional_int(metrics.get("zone")),
                        str(metrics.get("description") or "").strip() or None,
                        effective_speed=optional_float(
                            metrics.get("effective_speed")
                        ),
                        spin_axis=optional_int(metrics.get("spin_axis")),
                        release_pos_x=optional_float(
                            metrics.get("release_pos_x")
                        ),
                        release_pos_y=optional_float(
                            metrics.get("release_pos_y")
                        ),
                        release_pos_z=optional_float(
                            metrics.get("release_pos_z")
                        ),
                        vx0=optional_float(metrics.get("vx0")),
                        vy0=optional_float(metrics.get("vy0")),
                        vz0=optional_float(metrics.get("vz0")),
                        ax=optional_float(metrics.get("ax")),
                        ay=optional_float(metrics.get("ay")),
                        az=optional_float(metrics.get("az")),
                        api_break_x_arm=optional_float(
                            metrics.get("api_break_x_arm")
                        ),
                        api_break_x_batter_in=optional_float(
                            metrics.get("api_break_x_batter_in")
                        ),
                        api_break_z_with_gravity=optional_float(
                            metrics.get("api_break_z_with_gravity")
                        ),
                        arm_angle=optional_float(metrics.get("arm_angle")),
                        sz_top=optional_float(metrics.get("sz_top")),
                        sz_bot=optional_float(metrics.get("sz_bot")),
                        n_thruorder_pitcher=optional_int(
                            metrics.get("n_thruorder_pitcher")
                        ),
                        pitcher_days_since_prev_game=optional_int(
                            metrics.get("pitcher_days_since_prev_game")
                        ),
                        opposing_batter_hand=(
                            str(metrics.get("stand") or "").strip() or None
                        ),
                        available_at=dataset.raw.retrieved_at,
                    )
                )

        for pitch_identity, prior, observed in conflicts:
            self.repository.record_conflict(
                {
                    "conflict_id": self.id_factory("conflict"),
                    "stats_run_id": context.stats_run_id,
                    "conflict_code": "statcast_pitch_revision_conflict",
                    "entity_kind": "pitch",
                    "entity_key": ":".join(str(value) for value in pitch_identity),
                    "existing_value": {"checksum": prior},
                    "observed_value": {"checksum": observed},
                    "detected_at": dataset.raw.retrieved_at,
                    "source_checksum": dataset.raw.checksum_sha256,
                }
            )
        return coverage, (batted_balls, pitch_metrics, swing_metrics), persisted

    def _schedule_states(
        self,
        schedules: Sequence[tuple[ScheduleGame, str, RawArtifact]],
        coverage: Mapping[str, int],
        *,
        aggregate_complete_through: date | None,
        validation_failed_keys: Set[str] = frozenset(),
    ) -> list[GameAcquisitionState]:
        states: list[GameAcquisitionState] = []
        for game, _, _ in schedules:
            validation_failed = game.provider_game_id in validation_failed_keys
            matched = (
                game.status is GameStatus.FINAL
                and game.provider_game_id in coverage
            )
            states.append(
                GameAcquisitionState(
                    game.provider_game_id,
                    game.official_date,
                    GameStatus.VALIDATION_FAILED if validation_failed else game.status,
                    checksums_valid=True,
                    normalized=True,
                    validated=(
                        not validation_failed
                        and
                        game.home_team_key != game.away_team_key
                        and (
                            game.status is not GameStatus.FINAL
                            or (game.home_score is not None and game.away_score is not None)
                        )
                    ),
                    source_complete=(
                        not validation_failed
                        and matched
                        and aggregate_complete_through is not None
                        and game.official_date <= aggregate_complete_through
                    ),
                )
            )
        return states

    def _merge_daily_completeness(
        self,
        current: CompletenessResult,
        *,
        aggregate_complete_through: date | None,
    ) -> CompletenessResult:
        prior = self.repository.get_completeness_watermark(
            provider="current_mlb_stats",
            dataset_key="regular_season_games",
            season=current.requested_through_date.year,
        )
        prior_requested = (
            date.fromisoformat(str(prior["requested_through_date"]))
            if prior is not None and prior.get("requested_through_date")
            else None
        )
        evaluation_through = max(
            value
            for value in (current.requested_through_date, prior_requested)
            if value is not None
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH latest_status AS (
                    SELECT status.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY status.game_identity_id
                               ORDER BY status.retrieved_at DESC,
                                        status.rowid DESC
                           ) AS recency_rank
                    FROM stats_game_status_observations AS status
                )
                SELECT game.provider_game_id, game.official_date,
                       latest.abstract_state, latest.status_json,
                       EXISTS (
                           SELECT 1
                           FROM stats_game_identities AS statcast_game
                           JOIN stats_team_identities AS statcast_home
                             ON statcast_home.team_identity_id=
                                statcast_game.home_team_identity_id
                           JOIN stats_team_identities AS statcast_away
                             ON statcast_away.team_identity_id=
                                statcast_game.away_team_identity_id
                           JOIN stats_game_status_observations AS statcast_status
                             ON statcast_status.game_identity_id=
                                statcast_game.game_identity_id
                           JOIN statcast_pitch_identities AS pitch
                             ON pitch.game_identity_id=statcast_game.game_identity_id
                           WHERE statcast_game.provider='statcast'
                             AND statcast_status.abstract_state='final'
                             AND json_extract(
                                    statcast_status.status_json,
                                    '$.completion_contract'
                                 ) IN (
                                     'DSE_STATCAST_FINAL_GAME_V1',
                                     'DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1'
                                 )
                             AND json_extract(
                                    statcast_status.status_json,
                                    '$.baseball_reference_game_id'
                                 )=game.provider_game_id
                             AND statcast_game.official_date=game.official_date
                             AND statcast_home.canonical_team_key=
                                 home.canonical_team_key
                             AND statcast_away.canonical_team_key=
                                 away.canonical_team_key
                       ) AS has_statcast
                FROM stats_game_identities AS game
                JOIN stats_team_identities AS home
                  ON home.team_identity_id=game.home_team_identity_id
                JOIN stats_team_identities AS away
                  ON away.team_identity_id=game.away_team_identity_id
                JOIN latest_status AS latest
                  ON latest.game_identity_id=game.game_identity_id
                 AND latest.recency_rank=1
                WHERE game.provider='baseball_reference' AND game.season=?
                  AND game.official_date<=?
                ORDER BY game.official_date,game.provider_game_id
                """,
                (
                    current.requested_through_date.year,
                    evaluation_through.isoformat(),
                ),
            ).fetchall()
        persisted_states: list[GameAcquisitionState] = []
        for row in rows:
            status = GameStatus(str(row["abstract_state"]))
            payload = json.loads(str(row["status_json"]))
            game_date = date.fromisoformat(str(row["official_date"]))
            final_facts_valid = (
                status is not GameStatus.FINAL
                or (
                    optional_int(payload.get("home_score")) is not None
                    and optional_int(payload.get("away_score")) is not None
                )
            )
            persisted_states.append(
                GameAcquisitionState(
                    game_id=str(row["provider_game_id"]),
                    game_date=game_date,
                    status=status,
                    checksums_valid=True,
                    normalized=True,
                    validated=final_facts_valid,
                    source_complete=(
                        bool(row["has_statcast"])
                        and aggregate_complete_through is not None
                        and game_date <= aggregate_complete_through
                    ),
                )
            )
        if persisted_states:
            current = calculate_completeness(
                evaluation_through, persisted_states
            )
        if prior is None:
            return current

        def stored_date(field: str) -> date | None:
            value = prior.get(field)
            return date.fromisoformat(str(value)) if value else None

        prior_latest = stored_date("latest_ingested_completed_game_date")
        prior_contiguous = stored_date(
            "contiguous_regular_season_complete_through_date"
        )
        latest = max(
            (
                value
                for value in (
                    prior_latest,
                    current.latest_ingested_completed_game_date,
                )
                if value is not None
            ),
            default=None,
        )

        prior_partial = stored_date("partial_date") if not persisted_states else None
        partial_candidates: list[tuple[date, str]] = []
        if (
            prior_partial is not None
            and prior_partial != current.requested_through_date
            and prior.get("partial_date_reason")
        ):
            partial_candidates.append(
                (prior_partial, str(prior["partial_date_reason"]))
            )
        if current.partial_date is not None and current.partial_date_reason is not None:
            partial_candidates.append((current.partial_date, current.partial_date_reason))
        partial, partial_reason = (
            min(partial_candidates, key=lambda candidate: candidate[0])
            if partial_candidates
            else (None, None)
        )

        contiguous_candidates = [
            value
            for value in (
                prior_contiguous,
                current.contiguous_regular_season_complete_through_date,
            )
            if value is not None and (partial is None or value < partial)
        ]
        contiguous = max(contiguous_candidates, default=None)
        return CompletenessResult(
            requested_through_date=current.requested_through_date,
            contiguous_regular_season_complete_through_date=contiguous,
            latest_ingested_completed_game_date=latest,
            partial_date=partial,
            partial_date_reason=partial_reason,
        )

    def _persist_features(
        self,
        context: _RunContext,
        feature_as_of: date,
        completeness: CompletenessResult,
        *,
        source_checksums: Sequence[str],
        batting_lines: Iterable[BattingGameLine] = (),
        pitching_lines: Iterable[PitchingGameLine] = (),
        batted_balls: Iterable[StatcastBattedBall] = (),
        pitch_metrics: Iterable[StatcastPitchMetric] = (),
    ) -> int:
        batting = tuple(batting_lines)
        pitching = tuple(pitching_lines)
        balls = tuple(batted_balls)
        pitches = tuple(pitch_metrics)
        players = build_player_feature_payloads(
            feature_as_of=feature_as_of,
            knowledge_cutoff=context.source_observed_at or _aware_utc(self.clock()),
            batting_lines=batting,
            pitching_lines=pitching,
            batted_balls=balls,
            pitch_metrics=pitches,
        )
        teams = build_bullpen_workload(
            feature_as_of=feature_as_of,
            knowledge_cutoff=context.source_observed_at or _aware_utc(self.clock()),
            pitching_lines=pitching,
        )
        state = "complete" if completeness.partial_date is None else "degraded"
        created_at = context.source_observed_at or _aware_utc(self.clock())
        count = 0
        for player_id, payload in players.items():
            input_checksum = _checksum(
                {"entity": player_id, "sources": sorted(set(source_checksums))}
            )
            self.repository.record_feature_snapshot(
                {
                    "feature_snapshot_id": self.id_factory("feature"),
                    "stats_run_id": context.stats_run_id,
                    "feature_version": str(payload["contract_version"]),
                    "entity_kind": "player",
                    "player_identity_id": player_id,
                    "feature_as_of": _date_at_utc(feature_as_of),
                    "completeness_state": state,
                    "input_checksum": input_checksum,
                    "feature_checksum": str(payload["feature_checksum"]),
                    "features": payload,
                    "created_at": created_at,
                }
            )
            count += 1
        for team_key, payload in teams.items():
            provider = "retrosheet"
            source = self._source_for_canonical(provider, team_key)
            team_id, _ = self._team_identity(provider, source, created_at)
            self.repository.record_feature_snapshot(
                {
                    "feature_snapshot_id": self.id_factory("feature"),
                    "stats_run_id": context.stats_run_id,
                    "feature_version": str(payload["contract_version"]),
                    "entity_kind": "team",
                    "team_identity_id": team_id,
                    "feature_as_of": _date_at_utc(feature_as_of),
                    "completeness_state": state,
                    "input_checksum": _checksum(
                        {"entity": team_id, "sources": sorted(set(source_checksums))}
                    ),
                    "feature_checksum": str(payload["feature_checksum"]),
                    "features": payload,
                    "created_at": created_at,
                }
            )
            count += 1
        return count

    def _persist_aggregate_features(
        self,
        context: _RunContext,
        feature_as_of: date,
        completeness: CompletenessResult,
        *,
        source_checksums: Sequence[str],
        batting_aggregates: Iterable[BattingAggregateLine],
        pitching_aggregates: Iterable[PitchingAggregateLine],
        batted_balls: Iterable[StatcastBattedBall] = (),
        pitch_metrics: Iterable[StatcastPitchMetric] = (),
        swing_metrics: Iterable[StatcastSwingMetric] = (),
        pitcher_appearances: Iterable[StatcastPitcherAppearance] = (),
        statcast_plate_appearances: Iterable[StatcastPlateAppearance] = (),
    ) -> int:
        knowledge_cutoff = context.source_observed_at or _aware_utc(self.clock())
        batting_rows = tuple(batting_aggregates)
        pitching_rows = tuple(pitching_aggregates)
        batted_ball_rows = tuple(batted_balls)
        pitch_metric_rows = tuple(pitch_metrics)
        swing_metric_rows = tuple(swing_metrics)
        appearance_rows = tuple(pitcher_appearances)
        plate_appearance_rows = tuple(statcast_plate_appearances)
        players_v2 = build_aggregate_player_feature_payloads(
            feature_as_of=feature_as_of,
            knowledge_cutoff=knowledge_cutoff,
            batting_aggregates=batting_rows,
            pitching_aggregates=pitching_rows,
            batted_balls=batted_ball_rows,
            pitch_metrics=pitch_metric_rows,
            pitcher_appearances=appearance_rows,
            statcast_plate_appearances=plate_appearance_rows,
        )
        players_v3 = build_aggregate_player_feature_payloads_v3(
            feature_as_of=feature_as_of,
            knowledge_cutoff=knowledge_cutoff,
            batting_aggregates=batting_rows,
            pitching_aggregates=pitching_rows,
            batted_balls=batted_ball_rows,
            pitch_metrics=pitch_metric_rows,
            swing_metrics=swing_metric_rows,
            pitcher_appearances=appearance_rows,
            statcast_plate_appearances=plate_appearance_rows,
        )
        standard_teams = build_aggregate_team_feature_payloads(
            feature_as_of=feature_as_of,
            knowledge_cutoff=knowledge_cutoff,
            batting_aggregates=batting_rows,
            pitching_aggregates=pitching_rows,
        )
        bullpen_teams = build_bullpen_workload(
            feature_as_of=feature_as_of,
            knowledge_cutoff=knowledge_cutoff,
            pitcher_appearances=appearance_rows,
        )
        teams: dict[str, dict[str, Any]] = {}
        for team_key in sorted(set(standard_teams) | set(bullpen_teams)):
            standard = standard_teams.get(team_key, {})
            bullpen = bullpen_teams.get(team_key, {})
            payload = {
                **standard,
                "contract_version": str(
                    standard.get("contract_version")
                    or bullpen.get("contract_version")
                ),
                "feature_as_of": feature_as_of.isoformat(),
                "knowledge_cutoff": knowledge_cutoff.isoformat(),
                "team_key": team_key,
                "bullpen_workload_source": bullpen.get(
                    "bullpen_workload_source", "unavailable"
                ),
                "bullpen_workload": bullpen.get("bullpen_workload"),
            }
            payload["feature_checksum"] = _checksum(
                {key: value for key, value in payload.items() if key != "feature_checksum"}
            )
            teams[team_key] = payload
        state = "complete" if completeness.partial_date is None else "degraded"
        created_at = knowledge_cutoff
        feature_records: list[dict[str, object]] = []
        for player_payloads in (players_v2, players_v3):
            for canonical_player_id, payload in player_payloads.items():
                feature_records.append(
                    {
                        "feature_snapshot_id": self.id_factory("feature"),
                        "stats_run_id": context.stats_run_id,
                        "feature_version": str(payload["contract_version"]),
                        "entity_kind": "player",
                        "canonical_player_id": canonical_player_id,
                        "feature_as_of": _date_at_utc(feature_as_of),
                        "completeness_state": state,
                        "input_checksum": _checksum(
                            {
                                "entity": canonical_player_id,
                                "sources": sorted(set(source_checksums)),
                            }
                        ),
                        "feature_checksum": str(payload["feature_checksum"]),
                        "features": payload,
                        "created_at": created_at,
                    }
                )
        for team_key, payload in teams.items():
            source_team_id = self._source_for_canonical(
                "baseball_reference", team_key
            )
            team_identity_id, _ = self._team_identity(
                "baseball_reference", source_team_id, created_at
            )
            feature_records.append(
                {
                    "feature_snapshot_id": self.id_factory("feature"),
                    "stats_run_id": context.stats_run_id,
                    "feature_version": str(payload["contract_version"]),
                    "entity_kind": "team",
                    "team_identity_id": team_identity_id,
                    "feature_as_of": _date_at_utc(feature_as_of),
                    "completeness_state": state,
                    "input_checksum": _checksum(
                        {
                            "entity": team_identity_id,
                            "sources": sorted(set(source_checksums)),
                        }
                    ),
                    "feature_checksum": str(payload["feature_checksum"]),
                    "features": payload,
                    "created_at": created_at,
                }
            )
        self.repository.record_feature_snapshots(feature_records)
        return len(players_v2) + len(players_v3) + len(teams)

    @staticmethod
    def _cross_source_counting_items(
        reconciliation: CrossSourceCountingReconciliation,
    ) -> list[dict[str, Any]]:
        summary_details = {
            "contract_version": COUNTING_RECONCILIATION_CONTRACT_VERSION,
            "through_date": reconciliation.through_date.isoformat(),
            "baseball_reference_fields": {
                "batting": ["PA", "AB", "H", "HR", "BB", "SO"],
                "pitching": ["BF", "H", "HR", "BB", "SO"],
            },
            "statcast_method": "terminal_completed_plate_appearance_event",
            "entities_compared": reconciliation.entities_compared,
            "fields_compared": reconciliation.fields_compared,
            "fields_matched": reconciliation.fields_matched,
            "explained_difference_count": (
                reconciliation.explained_difference_count
            ),
            "unexplained_difference_count": (
                reconciliation.unexplained_difference_count
            ),
        }
        items: list[dict[str, Any]] = [
            {
                "severity": (
                    "warning"
                    if reconciliation.unexplained_difference_count
                    else "info"
                ),
                "code": "baseball_reference_statcast_counting_reconciliation",
                "entity_kind": "dataset",
                "entity_key": "same_cutoff_player_counting_stats",
                "message": (
                    "Same-cutoff Baseball-Reference and Statcast counting-stat "
                    "reconciliation completed"
                ),
                "details": summary_details,
                "source_checksum": _checksum(summary_details),
            }
        ]
        for item in reconciliation.items:
            if item.code == "cross_source_counting_stats_match":
                continue
            details = {
                "contract_version": COUNTING_RECONCILIATION_CONTRACT_VERSION,
                "through_date": reconciliation.through_date.isoformat(),
                "field": item.field,
                "baseball_reference_value": item.expected,
                "statcast_derived_value": item.observed,
                "difference_classification": (
                    "explained_source_semantics"
                    if item.explanation is not None
                    else "unexplained"
                ),
                "explanation": item.explanation,
            }
            items.append(
                {
                    "severity": item.severity.value,
                    "code": f"baseball_reference_statcast_{item.code}",
                    "entity_kind": "player_counting_stat",
                    "entity_key": item.entity_key,
                    "message": item.message,
                    "details": details,
                    "source_checksum": _checksum(
                        {
                            "entity_key": item.entity_key,
                            **details,
                        }
                    ),
                }
            )
        return items

    def _run_reconciliation_items(
        self,
        context: _RunContext,
        counts: Mapping[str, int],
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            observed = {
                "raw_payloads": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM stats_raw_payload_metadata "
                        "WHERE stats_run_id=?",
                        (context.stats_run_id,),
                    ).fetchone()[0]
                ),
                "schedule_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM stats_game_status_observations AS status "
                        "JOIN stats_game_identities AS game "
                        "ON game.game_identity_id=status.game_identity_id "
                        "WHERE status.stats_run_id=? "
                        "AND game.provider='baseball_reference'",
                        (context.stats_run_id,),
                    ).fetchone()[0]
                ),
                "baseball_reference_snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM stats_season_snapshots "
                        "WHERE stats_run_id=?",
                        (context.stats_run_id,),
                    ).fetchone()[0]
                ),
                "statcast_revisions_inserted": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM statcast_pitch_revisions "
                        "WHERE stats_run_id=?",
                        (context.stats_run_id,),
                    ).fetchone()[0]
                ),
            }

        items: list[dict[str, Any]] = []
        reconciliation_keys = (
            ("raw_payloads", "raw_payloads"),
            ("schedule_observations_persisted", "schedule_rows"),
            ("baseball_reference_snapshots", "baseball_reference_snapshots"),
            ("statcast_revisions_inserted", "statcast_revisions_inserted"),
        )
        for expected_key, observed_key in reconciliation_keys:
            if expected_key not in counts:
                continue
            expected = int(counts[expected_key])
            actual = observed[observed_key]
            matches = expected == actual
            details = {"expected": expected, "observed": actual}
            items.append(
                {
                    "severity": "info" if matches else "error",
                    "code": (
                        f"{observed_key}_persisted_count_matches"
                        if matches
                        else f"{observed_key}_persisted_count_mismatch"
                    ),
                    "entity_kind": "dataset",
                    "entity_key": observed_key,
                    "message": (
                        f"{observed_key} persisted count reconciles"
                        if matches
                        else f"{observed_key} persisted count does not reconcile"
                    ),
                    "details": details,
                    "source_checksum": _checksum(
                        {"key": observed_key, **details}
                    ),
                }
            )

        expected_sources = counts.get("schedule_sources_expected")
        observed_sources = counts.get("schedule_sources_observed")
        if expected_sources is not None and observed_sources is not None:
            complete = expected_sources == observed_sources
            details = {
                "expected": expected_sources,
                "observed": observed_sources,
            }
            items.append(
                {
                    "severity": "info" if complete else "warning",
                    "code": (
                        "schedule_source_coverage_complete"
                        if complete
                        else "schedule_source_coverage_incomplete"
                    ),
                    "entity_kind": "dataset",
                    "entity_key": "baseball_reference_schedule",
                    "message": (
                        "All active club schedule sources were retained"
                        if complete
                        else "The schedule capture does not cover all active clubs"
                    ),
                    "details": details,
                    "source_checksum": _checksum(
                        {"key": "schedule_source_coverage", **details}
                    ),
                }
            )

        expected_captures = counts.get(
            "baseball_reference_snapshot_captures_expected"
        )
        observed_captures = counts.get("baseball_reference_snapshot_captures")
        if expected_captures is not None and observed_captures is not None:
            complete = expected_captures == observed_captures
            details = {
                "expected": expected_captures,
                "observed": observed_captures,
            }
            items.append(
                {
                    "severity": "info" if complete else "error",
                    "code": (
                        "baseball_reference_aggregate_capture_coverage_complete"
                        if complete
                        else "baseball_reference_aggregate_capture_coverage_incomplete"
                    ),
                    "entity_kind": "dataset",
                    "entity_key": "baseball_reference_batting_pitching",
                    "message": (
                        "Baseball-Reference aggregate captures reconcile"
                        if complete
                        else "Baseball-Reference aggregate capture coverage is incomplete"
                    ),
                    "details": details,
                    "source_checksum": _checksum(
                        {"key": "aggregate_capture_coverage", **details}
                    ),
                }
            )
        return items

    def _finalize_run(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
        completeness: CompletenessResult,
        counts: Mapping[str, int],
        warnings: Sequence[str],
        *,
        additional_reconciliation_items: Sequence[Mapping[str, Any]] = (),
        completeness_watermark_eligible: bool = True,
    ) -> AcquisitionResult:
        observed_at = context.source_observed_at or _aware_utc(self.clock())
        result_counts = Counter(counts)
        result_counts["provider_requests"] = len(self.transport.requests)
        result_counts["provider_attempts"] = self.transport.attempts
        result_counts["provider_retries"] = max(
            0, self.transport.attempts - len(self.transport.requests)
        )
        result_counts["raw_capture_files"] = len(self.transport.captures)
        result_counts["raw_capture_bytes"] = sum(
            artifact.size_bytes for artifact in self.transport.captures
        )
        reconciliation_id = self.id_factory("reconciliation")
        items = self._run_reconciliation_items(context, result_counts)
        items.extend(dict(item) for item in additional_reconciliation_items)
        if completeness.partial_date_reason is not None:
            items.append(
                {
                    "severity": "warning",
                    "code": completeness.partial_date_reason,
                    "entity_kind": "date",
                    "entity_key": completeness.partial_date.isoformat()
                    if completeness.partial_date
                    else None,
                    "message": "Regular-season completeness is partial",
                    "details": {
                        "partial_date": completeness.partial_date,
                        "partial_date_reason": completeness.partial_date_reason,
                    },
                    "source_checksum": _checksum(asdict(completeness)),
                }
            )
        reconciliation_failed = any(item["severity"] == "error" for item in items)
        reconciliation_status = (
            "failed"
            if reconciliation_failed
            else "warnings"
            if warnings or any(item["severity"] == "warning" for item in items)
            else "passed"
        )
        source_checksum = _checksum(
            {
                "counts": dict(result_counts),
                "completeness": asdict(completeness),
                "completeness_watermark_eligible": completeness_watermark_eligible,
                "warnings": list(warnings),
                "additional_reconciliation_item_checksums": [
                    str(item["source_checksum"])
                    for item in additional_reconciliation_items
                ],
            }
        )
        self.repository.record_reconciliation(
            {
                "reconciliation_id": reconciliation_id,
                "stats_run_id": context.stats_run_id,
                "dataset_key": "regular_season_games",
                "scope_key": f"season:{request.requested_through_date.year}",
                "status": reconciliation_status,
                "expected_count": result_counts.get(
                    "final_games", result_counts.get("games")
                ),
                "observed_count": result_counts.get(
                    "fully_validated_final_games", result_counts.get("games")
                ),
                "conflict_count": result_counts.get("statcast_conflicts", 0),
                "started_at": observed_at,
                "completed_at": _aware_utc(self.clock()),
                "details": {
                    "command": context.command.value,
                    "completeness": asdict(completeness),
                    "completeness_watermark_eligible": (
                        completeness_watermark_eligible
                    ),
                    "completeness_watermark_block_reason": (
                        None
                        if completeness_watermark_eligible
                        else "unexplained_cross_source_reconciliation_difference"
                    ),
                },
                "source_checksum": source_checksum,
            },
            items,
        )
        if reconciliation_failed:
            raise AcquisitionExecutionError(
                "Persisted statistics records failed deterministic reconciliation",
                run_id=context.run_id,
                stats_run_id=context.stats_run_id,
            )
        if completeness_watermark_eligible and (
            completeness.latest_ingested_completed_game_date is not None
            or completeness.contiguous_regular_season_complete_through_date is not None
        ):
            self.repository.advance_completeness_watermark(
                provider=(
                    "retrosheet"
                    if context.command is AcquisitionCommand.BOOTSTRAP_RETROSHEET
                    else "current_mlb_stats"
                ),
                dataset_key="regular_season_games",
                scope_key=f"season:{request.requested_through_date.year}",
                requested_through_date=completeness.requested_through_date,
                source_observed_at=observed_at,
                latest_ingested_completed_game_date=(
                    completeness.latest_ingested_completed_game_date
                ),
                contiguous_regular_season_complete_through_date=(
                    completeness.contiguous_regular_season_complete_through_date
                ),
                partial_date=completeness.partial_date,
                partial_date_reason=completeness.partial_date_reason,
                source_stats_run_id=context.stats_run_id,
                reconciliation_id=reconciliation_id,
                source_checksum=source_checksum,
                updated_at=_aware_utc(self.clock()),
            )
        status = (
            "completed_with_warnings"
            if warnings or any(item["severity"] == "warning" for item in items)
            else "completed"
        )
        self.repository.transition_ingestion_run(
            context.stats_run_id,
            status,
            transitioned_at=_aware_utc(self.clock()),
            source_observed_at=observed_at,
            latest_ingested_completed_game_date=(
                completeness.latest_ingested_completed_game_date
                if completeness.latest_ingested_completed_game_date is not None
                and completeness.latest_ingested_completed_game_date
                <= request.requested_through_date
                else None
            ),
            contiguous_regular_season_complete_through_date=(
                completeness.contiguous_regular_season_complete_through_date
                if completeness.contiguous_regular_season_complete_through_date
                is not None
                and completeness.contiguous_regular_season_complete_through_date
                <= request.requested_through_date
                else None
            ),
            partial_date=(
                completeness.partial_date
                if completeness.partial_date is not None
                and completeness.partial_date <= request.requested_through_date
                else None
            ),
            partial_date_reason=(
                completeness.partial_date_reason
                if completeness.partial_date is not None
                and completeness.partial_date <= request.requested_through_date
                else None
            ),
        )
        self.database.transition_run(
            context.run_id,
            RunStatus.COMPLETED_WITH_WARNINGS
            if status == "completed_with_warnings"
            else RunStatus.COMPLETED,
            transitioned_at=_aware_utc(self.clock()).isoformat(),
        )
        outcome = classify_acquisition_outcome(
            build_acquisition_outcome_evidence(
                completeness,
                completeness_watermark_eligible=completeness_watermark_eligible,
            )
        )
        return AcquisitionResult(
            command=context.command,
            status=status,
            requested_through_date=request.requested_through_date,
            run_id=context.run_id,
            stats_run_id=context.stats_run_id,
            counts=dict(result_counts),
            completeness=completeness,
            warnings=tuple(
                sorted(
                    set(warnings)
                    | {
                        str(item["code"])
                        for item in items
                        if item["severity"] == "warning"
                    }
                )
            ),
            report_path=self.report_path,
            dry_run=request.dry_run,
            outcome=outcome,
            source_observed_at=observed_at,
            source_stats_run_id=request.source_stats_run_id,
        )

    def _fail_run(self, context: _RunContext, exc: Exception) -> None:
        message = redact_text(str(exc))
        now = _aware_utc(self.clock())
        raw_metadata_error: str | None = None
        try:
            self._persist_unrecorded_captures(context)
        except Exception as metadata_exc:
            raw_metadata_error = redact_text(str(metadata_exc))
        try:
            self.repository.fail_active_checkpoints(
                context.stats_run_id,
                transitioned_at=now,
                reason_code="parent_run_failed",
                details={
                    "failure_stage": "acquisition",
                    "error_type": type(exc).__name__,
                    "message": message,
                },
            )
            stats = self.repository.get_ingestion_run(context.stats_run_id)
            if stats is not None and stats["status"] in {"queued", "running"}:
                self.repository.transition_ingestion_run(
                    context.stats_run_id,
                    "failed",
                    transitioned_at=now,
                    failure_stage="acquisition",
                    error={
                        "type": type(exc).__name__,
                        "message": message,
                        "raw_metadata_error": raw_metadata_error,
                    },
                )
        finally:
            outer = self.database.get_run(context.run_id)
            if outer is not None and outer["status"] in {"queued", "running"}:
                self.database.transition_run(
                    context.run_id,
                    RunStatus.FAILED,
                    failure_stage=FailureStage.COLLECTOR,
                    error_message=message,
                    transitioned_at=now.isoformat(),
                )

    def _persist_unrecorded_captures(self, context: _RunContext) -> None:
        with self.database.connect() as connection:
            recorded = {
                str(row[0])
                for row in connection.execute(
                    "SELECT source_capture_id FROM stats_raw_payload_metadata "
                    "WHERE stats_run_id=?",
                    (context.stats_run_id,),
                ).fetchall()
            }
        for artifact in self.transport.captures:
            if artifact.capture_id not in recorded:
                self._persist_raw(context, None, artifact)
                recorded.add(artifact.capture_id)

    def _write_report(self, result: AcquisitionResult) -> None:
        if result.dry_run:
            return
        payload = result.as_dict()
        payload["validation_scope"] = {
            "provider": self._command_provider(result.command),
            "season": result.requested_through_date.year,
        }
        payload["result_checksum"] = report_checksum(payload)
        section = "acquisition_run"
        if result.command is AcquisitionCommand.VALIDATE:
            section = "incremental_validation"
            existing_initial = False
            if self.report_path.exists():
                loaded = json.loads(self.report_path.read_text(encoding="utf-8"))
                existing_initial = isinstance(loaded, dict) and (
                    loaded.get("initial_validation") is not None
                )
            if (
                result.requested_through_date == date(2026, 7, 14)
                and not existing_initial
            ):
                section = "initial_validation"
        merge_validation_report(
            self.report_path,
            section=section,
            validation=payload,
        )

    def _write_failure_report(
        self,
        context: _RunContext,
        request: AcquisitionRequest,
        exc: Exception,
    ) -> None:
        observed_at = _aware_utc(self.clock())
        result = AcquisitionResult(
            command=context.command,
            status="failed",
            requested_through_date=request.requested_through_date,
            run_id=context.run_id,
            stats_run_id=context.stats_run_id,
            counts={
                "provider_attempts": self.transport.attempts,
                "raw_captures_observed": len(self.transport.captures),
            },
            completeness=None,
            warnings=(),
            report_path=self.report_path,
            dry_run=request.dry_run,
            outcome=AcquisitionOutcome.FAILED,
            source_observed_at=observed_at,
            source_stats_run_id=request.source_stats_run_id,
        )
        payload = result.as_dict()
        payload.update(
            {
                "validation_scope": {
                    "provider": self._command_provider(context.command),
                    "season": context.season,
                },
                "failure": {
                    "error_type": type(exc).__name__,
                    "failure_stage": "acquisition",
                    "message": redact_text(str(exc)),
                },
            },
        )
        payload["result_checksum"] = report_checksum(payload)
        merge_validation_report(
            self.report_path,
            section="acquisition_run",
            validation=payload,
        )

    @staticmethod
    def _checkpoint_cursor(row: Mapping[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        try:
            value = json.loads(str(row.get("cursor_after_json") or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _player_register_pin_bridge_readiness(
        self,
        source_run: Mapping[str, Any] | None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "contract": PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
            "ready": False,
            "bridge_ready": False,
            "required": True,
            "bridge_stats_run_id": None,
            "direct_pin_ready": False,
            "errors": [],
        }
        if source_run is None:
            result["errors"] = ["source_run_missing"]
            return result
        source_stats_run_id = str(source_run["stats_run_id"])
        source_checkpoint = self.repository.get_checkpoint(
            source_stats_run_id, "pybaseball_player_identifier_register"
        )
        source_cursor = self._checkpoint_cursor(source_checkpoint)
        direct_pin_declared = (
            source_checkpoint is not None
            and source_checkpoint.get("status") == "completed"
            and source_cursor.get("resource_revision")
            == PYBASEBALL_REGISTER_COMMIT_SHA
            and source_cursor.get("resource_identity") == PYBASEBALL_REGISTER_URL
            and source_cursor.get("raw_endpoint_category")
            == PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
        )
        direct_errors: list[str] = []
        if direct_pin_declared and source_checkpoint is not None:
            try:
                direct_raw_rows = self.repository.list_checkpoint_raw_payloads(
                    str(source_checkpoint["checkpoint_id"])
                )
                if len(direct_raw_rows) != 1:
                    raise ValueError("direct pin must link exactly one raw archive")
                direct_artifact = self._raw_artifact_from_record(direct_raw_rows[0])
                if (
                    direct_artifact.endpoint_category
                    != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
                    or direct_artifact.checksum_sha256 != source_cursor.get("checksum")
                ):
                    raise ValueError("direct pin raw identity does not match cursor")
                direct_dataset = PybaseballPlayerRegisterProvider(
                    self.transport
                ).parse_artifact(direct_artifact)
                if (
                    int(source_cursor.get("mapping_count", -1))
                    != len(direct_dataset.mappings)
                    or int(source_cursor.get("source_member_count", -1))
                    != direct_dataset.source_member_count
                    or int(source_cursor.get("source_row_count", -1))
                    != direct_dataset.source_row_count
                    or int(source_checkpoint["records_seen"])
                    != direct_dataset.source_row_count
                    or int(source_checkpoint["records_persisted"])
                    != len(direct_dataset.mappings)
                    or int(source_checkpoint["records_rejected"]) != 0
                ):
                    raise ValueError("direct pin checkpoint counts are inconsistent")
            except Exception as exc:
                direct_errors.append(
                    f"direct_pin_reverification_failed:{type(exc).__name__}"
                )
            else:
                result.update(
                    {
                        "ready": True,
                        "required": False,
                        "direct_pin_ready": True,
                        "errors": [],
                    }
                )
                return result
        with self.database.connect() as connection:
            bridge_run = connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE provider=? "
                "AND scope_key=? AND source_version=? "
                "AND status IN ('completed','completed_with_warnings') "
                "ORDER BY completed_at DESC,stats_run_id DESC LIMIT 1",
                (
                    PLAYER_REGISTER_RECONCILIATION_PROVIDER,
                    f"player-register-pin:{source_stats_run_id}",
                    PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
                ),
            ).fetchone()
        if bridge_run is None:
            result["errors"] = [*direct_errors, "bridge_run_missing"]
            return result
        bridge = dict(bridge_run)
        result["bridge_stats_run_id"] = str(bridge["stats_run_id"])
        checkpoint = self.repository.get_checkpoint(
            str(bridge["stats_run_id"]),
            PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
        )
        if checkpoint is None or checkpoint["status"] != "completed":
            result["errors"] = ["bridge_checkpoint_missing_or_incomplete"]
            return result
        try:
            source_request = AcquisitionRequest(
                requested_through_date=date.fromisoformat(
                    str(source_run["requested_through_date"])
                ),
                mode=AcquisitionMode.OFFLINE,
                source_stats_run_id=source_stats_run_id,
            )
            (
                verified_source_run,
                verified_source_checkpoint,
                source_raw_row,
                source_artifact,
                source_dataset,
            ) = self._source_player_register_evidence(source_request)
            pinned_raw_rows = self.repository.list_checkpoint_raw_payloads(
                str(checkpoint["checkpoint_id"])
            )
            if len(pinned_raw_rows) != 1:
                raise ValueError("bridge must link exactly one pinned raw archive")
            pinned_raw_row = pinned_raw_rows[0]
            pinned_artifact = self._raw_artifact_from_record(pinned_raw_row)
            if (
                pinned_artifact.endpoint_category
                != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
            ):
                raise ValueError("bridge pinned endpoint category is invalid")
            pinned_dataset = PybaseballPlayerRegisterProvider(
                self.transport
            ).parse_artifact(pinned_artifact)
            source_inventory = build_register_inventory(
                source_dataset, self.raw_store
            )
            pinned_inventory = build_register_inventory(
                pinned_dataset, self.raw_store
            )
            if not inventories_match(source_inventory, pinned_inventory):
                raise ValueError("bridge inventories no longer match")
            cursor = self._checkpoint_cursor(checkpoint)
            expected_cursor = {
                "contract": PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
                "source_stats_run_id": source_stats_run_id,
                "source_run_configuration_checksum": str(
                    verified_source_run["configuration_checksum"]
                ),
                "source_checkpoint_id": str(
                    verified_source_checkpoint["checkpoint_id"]
                ),
                "source_checkpoint_cursor_sha256": (
                    register_reconciliation_checksum(
                        self._checkpoint_cursor(verified_source_checkpoint)
                    )
                ),
                "source_raw_payload_id": str(source_raw_row["raw_payload_id"]),
                "source_raw_checksum_sha256": source_artifact.checksum_sha256,
                "source_raw_endpoint_category": source_artifact.endpoint_category,
                "pinned_stats_run_id": str(bridge["stats_run_id"]),
                "pinned_raw_payload_id": str(pinned_raw_row["raw_payload_id"]),
                "pinned_raw_checksum_sha256": pinned_artifact.checksum_sha256,
                "pinned_raw_endpoint_category": pinned_artifact.endpoint_category,
                "pinned_resource_revision": PYBASEBALL_REGISTER_COMMIT_SHA,
                "pinned_resource_identity": PYBASEBALL_REGISTER_URL,
                "source_inventory": source_inventory,
                "pinned_inventory": pinned_inventory,
                "equivalent": True,
            }
            if cursor != expected_cursor:
                raise ValueError("bridge checkpoint cursor failed exact revalidation")
            if (
                int(checkpoint["records_seen"])
                != source_dataset.source_row_count + pinned_dataset.source_row_count
                or int(checkpoint["records_persisted"]) != 1
                or int(checkpoint["records_rejected"]) != 0
            ):
                raise ValueError("bridge checkpoint counts are inconsistent")
        except Exception as exc:
            result["errors"] = [f"bridge_reverification_failed:{type(exc).__name__}"]
            return result
        result.update({"ready": True, "bridge_ready": True, "errors": []})
        return result

    @classmethod
    def _retrosheet_checkpoint_readiness(
        cls,
        checkpoints: Mapping[str, Mapping[str, Any]],
        *,
        through_season: int,
        actual_analytical_inventory: Mapping[str, object] | None = None,
        register_pin_bridge_ready: bool = False,
        register_artifact_verified: bool = False,
    ) -> tuple[list[str], list[str], int, int]:
        parent_key = "retrosheet_regular_season"
        manifest_key = "retrosheet_member_season_manifest"
        register_key = "pybaseball_player_identifier_register"
        inventory_key = "retrosheet_analytical_inventory"
        missing: list[str] = []
        evidence_errors: list[str] = []
        parent = checkpoints.get(parent_key)
        manifest = checkpoints.get(manifest_key)
        register = checkpoints.get(register_key)
        inventory = checkpoints.get(inventory_key)
        if parent is None or parent.get("status") != "completed":
            missing.append(parent_key)
        if manifest is None or manifest.get("status") != "completed":
            missing.append(manifest_key)
        if register is None or register.get("status") != "completed":
            missing.append(register_key)
        if inventory is None or inventory.get("status") != "completed":
            missing.append(inventory_key)
        if missing:
            return sorted(missing), evidence_errors, 0, 0

        inventory_cursor = cls._checkpoint_cursor(inventory)
        try:
            if (
                inventory_cursor["contract"]
                != RETROSHEET_ANALYTICAL_INVENTORY_CONTRACT
            ):
                raise ValueError("unsupported analytical inventory contract")
            if int(inventory_cursor["through_season"]) != through_season:
                raise ValueError(
                    "analytical inventory through-season does not match validation"
                )
            raw_table_inventory = inventory_cursor["tables"]
            if not isinstance(raw_table_inventory, Mapping):
                raise ValueError("analytical inventory tables are malformed")
            table_inventory = dict(raw_table_inventory)
            if set(table_inventory) != set(
                RETROSHEET_ANALYTICAL_INVENTORY_TABLES
            ):
                raise ValueError(
                    "analytical inventory table set is incomplete"
                )
            for table_name, raw_details in table_inventory.items():
                if not isinstance(raw_details, Mapping):
                    raise ValueError(
                        f"analytical inventory details are invalid:{table_name}"
                    )
                details = dict(raw_details)
                if int(details["row_count"]) < 0:
                    raise ValueError(
                        f"analytical inventory count is invalid:{table_name}"
                    )
                if not re.fullmatch(
                    r"[0-9a-f]{64}", str(details["content_sha256"])
                ):
                    raise ValueError(
                        f"analytical inventory checksum is invalid:{table_name}"
                    )
            games_inventory = table_inventory["games"]
            if not isinstance(games_inventory, Mapping):
                raise ValueError("analytical game inventory is malformed")
            if int(games_inventory["row_count"]) <= 0:
                raise ValueError("analytical inventory contains no games")
        except (KeyError, TypeError, ValueError) as exc:
            evidence_errors.append(f"analytical_inventory_invalid:{exc}")
        else:
            if actual_analytical_inventory is not None:
                actual = dict(actual_analytical_inventory)
                if actual != inventory_cursor:
                    expected_tables = dict(inventory_cursor["tables"])
                    raw_actual_tables = actual.get("tables", {})
                    actual_tables = (
                        dict(raw_actual_tables)
                        if isinstance(raw_actual_tables, Mapping)
                        else {}
                    )
                    mismatched = sorted(
                        table_name
                        for table_name in RETROSHEET_ANALYTICAL_INVENTORY_TABLES
                        if actual_tables.get(table_name)
                        != expected_tables.get(table_name)
                    )
                    if (
                        actual.get("contract")
                        != inventory_cursor["contract"]
                        or actual.get("through_season")
                        != inventory_cursor["through_season"]
                    ):
                        mismatched.insert(0, "inventory_contract")
                    evidence_errors.extend(
                        f"analytical_inventory_mismatch:{table_name}"
                        for table_name in mismatched
                    )

        register_cursor = cls._checkpoint_cursor(register)
        try:
            if (
                register_cursor["contract"]
                != "DSE_PYBASEBALL_PLAYER_REGISTER_CHECKPOINT_V1"
                or register_cursor["adapter_version"]
                != PYBASEBALL_REGISTER_ADAPTER_VERSION
                or register_cursor["pybaseball_version"]
                != PYBASEBALL_REQUIRED_VERSION
                or register_cursor["resource_revision"]
                != PYBASEBALL_REGISTER_COMMIT_SHA
                or register_cursor["resource_identity"]
                != PYBASEBALL_REGISTER_URL
                or register_cursor["raw_endpoint_category"]
                != PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
                or int(register_cursor["mapping_count"]) <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(register_cursor["checksum"]))
            ):
                raise ValueError("player identifier register evidence is invalid")
        except (KeyError, TypeError, ValueError) as exc:
            if not register_pin_bridge_ready:
                evidence_errors.append(f"player_identifier_register_invalid:{exc}")
        else:
            if not register_artifact_verified and not register_pin_bridge_ready:
                evidence_errors.append(
                    "player_identifier_register_invalid:raw artifact not verified"
                )

        parent_cursor = cls._checkpoint_cursor(parent)
        manifest_cursor = cls._checkpoint_cursor(manifest)
        expected_entries: dict[str, dict[str, object]] = {}
        try:
            if parent_cursor["contract"] != "DSE_RETROSHEET_RAW_DATASET_V2":
                raise ValueError("unsupported parent cursor contract")
            member_metadata = dict(parent_cursor["members"])
            raw_ids = dict(parent_cursor["raw_ids"])
            if set(member_metadata) != set(RETROSHEET_SEVEN_MEMBERS):
                raise ValueError("parent cursor does not identify all seven members")
            for member_name in RETROSHEET_SEVEN_MEMBERS:
                metadata = dict(member_metadata[member_name])
                season_counts = {
                    int(season): int(count)
                    for season, count in dict(
                        metadata["season_row_counts"]
                    ).items()
                }
                if any(count <= 0 for count in season_counts.values()):
                    raise ValueError("member season row counts must be positive")
                if not any(season <= through_season for season in season_counts):
                    raise ValueError(
                        f"{member_name} has no source season through requested history"
                    )
                for source_season, source_row_count in season_counts.items():
                    if source_season > through_season:
                        continue
                    dataset_key = (
                        "retrosheet:"
                        f"{member_name.removesuffix('.csv')}:season:{source_season}"
                    )
                    expected_entries[dataset_key] = {
                        "dataset_key": dataset_key,
                        "member_name": member_name,
                        "season": source_season,
                        "source_row_count": source_row_count,
                        "raw_payload_id": str(raw_ids[member_name]),
                        "raw_checksum": str(metadata["checksum"]),
                    }
        except (KeyError, TypeError, ValueError) as exc:
            evidence_errors.append(f"raw_dataset_inventory_invalid:{exc}")
            return sorted(missing), sorted(evidence_errors), 0, 0

        manifest_entries: dict[str, dict[str, object]] = {}
        try:
            if (
                manifest_cursor["contract"]
                != "DSE_RETROSHEET_MEMBER_SEASON_MANIFEST_V1"
            ):
                raise ValueError("unsupported manifest contract")
            if int(manifest_cursor["through_season"]) != through_season:
                raise ValueError("manifest through-season does not match validation")
            for raw_entry in list(manifest_cursor["entries"]):
                entry = dict(raw_entry)
                dataset_key = str(entry["dataset_key"])
                if dataset_key in manifest_entries:
                    raise ValueError("manifest contains duplicate dataset keys")
                manifest_entries[dataset_key] = entry
            if manifest_entries != expected_entries:
                raise ValueError("manifest does not match raw source season inventory")
        except (KeyError, TypeError, ValueError) as exc:
            evidence_errors.append(f"member_season_manifest_invalid:{exc}")

        completed_count = 0
        for dataset_key, expected in sorted(expected_entries.items()):
            checkpoint = checkpoints.get(dataset_key)
            if checkpoint is None or checkpoint.get("status") != "completed":
                missing.append(dataset_key)
                continue
            child_cursor = cls._checkpoint_cursor(checkpoint)
            expected_cursor = {
                "contract": "DSE_RETROSHEET_MEMBER_SEASON_V2",
                "member_name": expected["member_name"],
                "season": expected["season"],
                "source_row_count": expected["source_row_count"],
                "raw_payload_id": expected["raw_payload_id"],
                "raw_checksum": expected["raw_checksum"],
            }
            if child_cursor != expected_cursor:
                evidence_errors.append(
                    f"child_checkpoint_evidence_mismatch:{dataset_key}"
                )
                continue
            seen = int(checkpoint.get("records_seen") or 0)
            persisted = int(checkpoint.get("records_persisted") or 0)
            rejected = int(checkpoint.get("records_rejected") or 0)
            if (
                seen != int(str(expected["source_row_count"]))
                or persisted + rejected != seen
            ):
                evidence_errors.append(
                    f"child_checkpoint_count_mismatch:{dataset_key}"
                )
                continue
            completed_count += 1
        return (
            sorted(set(missing)),
            sorted(set(evidence_errors)),
            len(expected_entries),
            completed_count,
        )

    def _validation_source_readiness(
        self,
        *,
        season: int,
        requested_through_date: date,
    ) -> dict[str, Any]:
        terminal = ("completed", "completed_with_warnings")
        retrosheet_through = date(season - 1, 12, 31)
        with self.database.connect() as connection:
            historical = connection.execute(
                "SELECT * FROM stats_ingestion_runs "
                "WHERE provider='retrosheet' AND scope_key=? "
                "AND requested_through_date>=? AND status IN (?,?) "
                "ORDER BY requested_through_date DESC,created_at DESC LIMIT 1",
                (
                    f"regular-season:{season - 1}",
                    retrosheet_through.isoformat(),
                    *terminal,
                ),
            ).fetchone()
            current = connection.execute(
                "SELECT * FROM stats_ingestion_runs "
                "WHERE provider='current_mlb_stats' AND scope_key=? "
                "AND requested_through_date=? AND status IN (?,?) "
                "ORDER BY created_at DESC LIMIT 1",
                (
                    f"regular-season:{season}",
                    requested_through_date.isoformat(),
                    *terminal,
                ),
            ).fetchone()

            def checkpoints(stats_run_id: str | None) -> dict[str, dict[str, Any]]:
                if stats_run_id is None:
                    return {}
                rows = connection.execute(
                    "SELECT * FROM stats_checkpoints WHERE stats_run_id=?",
                    (stats_run_id,),
                ).fetchall()
                return {str(row["dataset_key"]): dict(row) for row in rows}

            historical_checkpoints = checkpoints(
                str(historical["stats_run_id"]) if historical is not None else None
            )
            current_checkpoints = checkpoints(
                str(current["stats_run_id"]) if current is not None else None
            )
            historical_reconciliation = (
                connection.execute(
                    "SELECT status FROM stats_reconciliations "
                    "WHERE stats_run_id=? AND dataset_key='regular_season_games' "
                    "ORDER BY completed_at DESC LIMIT 1",
                    (str(historical["stats_run_id"]),),
                ).fetchone()
                if historical is not None
                else None
            )
            current_reconciliation = (
                connection.execute(
                    "SELECT reconciliation_id,status,details_json "
                    "FROM stats_reconciliations "
                    "WHERE stats_run_id=? AND dataset_key='regular_season_games' "
                    "ORDER BY completed_at DESC LIMIT 1",
                    (str(current["stats_run_id"]),),
                ).fetchone()
                if current is not None
                else None
            )
            current_counting_summary = (
                connection.execute(
                    "SELECT details_json FROM stats_reconciliation_items "
                    "WHERE reconciliation_id=? AND code="
                    "'baseball_reference_statcast_counting_reconciliation' "
                    "ORDER BY reconciliation_item_id DESC LIMIT 1",
                    (str(current_reconciliation["reconciliation_id"]),),
                ).fetchone()
                if current_reconciliation is not None
                else None
            )
            completed_statcast_keys = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT checkpoint.dataset_key "
                    "FROM stats_checkpoints AS checkpoint "
                    "JOIN stats_ingestion_runs AS run "
                    "ON run.stats_run_id=checkpoint.stats_run_id "
                    "WHERE run.provider='current_mlb_stats' AND run.scope_key=? "
                    "AND run.status IN (?,?) AND checkpoint.status='completed' "
                    "AND checkpoint.dataset_key LIKE 'statcast:%'",
                    (f"regular-season:{season}", *terminal),
                ).fetchall()
            }
        register_pin_bridge = self._player_register_pin_bridge_readiness(
            dict(historical) if historical is not None else None
        )
        (
            historical_missing,
            historical_evidence_errors,
            historical_expected_count,
            historical_completed_count,
        ) = self._retrosheet_checkpoint_readiness(
            historical_checkpoints,
            through_season=season - 1,
            actual_analytical_inventory=(
                self._retrosheet_analytical_inventory(season - 1)
                if historical is not None
                else None
            ),
            register_pin_bridge_ready=bool(register_pin_bridge["bridge_ready"]),
            register_artifact_verified=bool(register_pin_bridge["ready"]),
        )
        historical_ready = (
            historical is not None
            and not historical_missing
            and not historical_evidence_errors
            and historical_expected_count > 0
            and historical_completed_count == historical_expected_count
            and historical_reconciliation is not None
            and historical_reconciliation["status"] in {"passed", "warnings"}
        )
        historical_inventory_cursor = self._checkpoint_cursor(
            historical_checkpoints.get("retrosheet_analytical_inventory")
        )

        schedule_checkpoint = current_checkpoints.get(
            "baseball_reference_schedule"
        )
        aggregate_checkpoint = current_checkpoints.get(
            "baseball_reference_batting_pitching"
        )
        schedule_cursor = self._checkpoint_cursor(schedule_checkpoint)
        aggregate_cursor = self._checkpoint_cursor(aggregate_checkpoint)
        expected_sources = set(TEAM_SOURCE_ALIASES["baseball_reference"])
        observed_sources = {
            str(value) for value in schedule_cursor.get("source_ids", [])
        }
        schedule_ready = (
            schedule_checkpoint is not None
            and schedule_checkpoint.get("status") == "completed"
            and schedule_cursor.get("contract")
            in {
                "DSE_BREF_SCHEDULE_CHECKPOINT_V1",
                "DSE_BREF_SCHEDULE_CHECKPOINT_V2",
            }
            and observed_sources == expected_sources
            and int(schedule_cursor.get("incomplete_pair_count", -1)) == 0
        )

        projected_completeness = (
            _stored_completeness_for_cutoff(
                current_reconciliation["details_json"], requested_through_date
            )
            if current_reconciliation is not None
            else None
        )
        latest_ingested = (
            projected_completeness.latest_ingested_completed_game_date
            if projected_completeness is not None
            else None
        )
        counting_reconciliation_ready = (
            current_counting_summary is not None
            and _counting_reconciliation_is_ready(
                current_counting_summary["details_json"]
            )
        )
        aggregate_through = None
        try:
            if aggregate_cursor.get("contract") in {
                "DSE_BREF_AGGREGATE_CHECKPOINT_V1",
                "DSE_BREF_AGGREGATE_CHECKPOINT_V2",
            }:
                aggregate_through = date.fromisoformat(
                    str(aggregate_cursor["through_date"])
                )
        except (KeyError, ValueError):
            aggregate_through = None
        aggregate_ready = (
            aggregate_checkpoint is not None
            and aggregate_checkpoint.get("status") == "completed"
            and aggregate_through is not None
            and (latest_ingested is None or aggregate_through >= latest_ingested)
            and int(aggregate_cursor.get("actual_capture_count", -1))
            == int(aggregate_cursor.get("expected_capture_count", -2))
            and int(aggregate_cursor.get("records_rejected", -1)) == 0
        )

        season_start = None
        try:
            season_start = date.fromisoformat(str(schedule_cursor["season_start"]))
        except (KeyError, ValueError):
            season_start = None
        required_statcast_keys: set[str] = set()
        if season_start is not None and latest_ingested is not None:
            cursor_date = season_start
            while cursor_date <= latest_ingested:
                required_statcast_keys.add(f"statcast:{cursor_date.isoformat()}")
                cursor_date += timedelta(days=1)
        missing_statcast = sorted(required_statcast_keys - completed_statcast_keys)
        statcast_ready = (
            latest_ingested is not None
            and season_start is not None
            and not missing_statcast
        )
        current_ready = (
            current is not None
            and current_reconciliation is not None
            and current_reconciliation["status"] in {"passed", "warnings"}
            and projected_completeness is not None
            and counting_reconciliation_ready
            and schedule_ready
            and aggregate_ready
            and statcast_ready
        )
        return {
            "contract_version": "DSE_STATS_SOURCE_READINESS_V1",
            "ready": historical_ready and current_ready,
            "retrosheet_through_prior_season": {
                "ready": historical_ready,
                "required_through_season": season - 1,
                "stats_run_id": (
                    str(historical["stats_run_id"])
                    if historical is not None
                    else None
                ),
                "missing_or_incomplete_checkpoints": historical_missing,
                "checkpoint_evidence_errors": historical_evidence_errors,
                "expected_member_season_checkpoint_count": (
                    historical_expected_count
                ),
                "completed_member_season_checkpoint_count": (
                    historical_completed_count
                ),
                "analytical_inventory": {
                    "contract": historical_inventory_cursor.get("contract"),
                    "through_season": historical_inventory_cursor.get(
                        "through_season"
                    ),
                    "tables": historical_inventory_cursor.get("tables", {}),
                    "ready": not any(
                        error.startswith("analytical_inventory_")
                        for error in historical_evidence_errors
                    )
                    and "retrosheet_analytical_inventory"
                    not in historical_missing,
                },
                "reconciliation_status": (
                    str(historical_reconciliation["status"])
                    if historical_reconciliation is not None
                    else None
                ),
                "player_register_pin_reconciliation": register_pin_bridge,
            },
            "current_season": {
                "ready": current_ready,
                "stats_run_id": (
                    str(current["stats_run_id"]) if current is not None else None
                ),
                "source_observed_at": (
                    str(current["source_observed_at"])
                    if current is not None and current["source_observed_at"]
                    else None
                ),
                "projected_completeness": (
                    {
                        key: value.isoformat() if isinstance(value, date) else value
                        for key, value in asdict(projected_completeness).items()
                    }
                    if projected_completeness is not None
                    else None
                ),
                "schedule": {
                    "ready": schedule_ready,
                    "expected_source_count": len(expected_sources),
                    "observed_source_count": len(observed_sources),
                    "incomplete_pair_count": schedule_cursor.get(
                        "incomplete_pair_count"
                    ),
                },
                "baseball_reference_aggregates": {
                    "ready": aggregate_ready,
                    "through_date": (
                        aggregate_through.isoformat()
                        if aggregate_through is not None
                        else None
                    ),
                    "required_through_date": (
                        latest_ingested.isoformat()
                        if latest_ingested is not None
                        else None
                    ),
                },
                "statcast": {
                    "ready": statcast_ready,
                    "required_day_count": len(required_statcast_keys),
                    "missing_or_incomplete_day_checkpoints": missing_statcast,
                },
                "reconciliation_status": (
                    str(current_reconciliation["status"])
                    if current_reconciliation is not None
                    else None
                ),
                "counting_reconciliation": {
                    "ready": counting_reconciliation_ready,
                    "unexplained_differences_allowed": False,
                },
            },
        }

    def _validation_details(
        self,
        *,
        season: int,
        requested_through_date: date,
        integrity: Mapping[str, Any],
        raw_checksum_inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        schema = self.database.schema_info()
        source_readiness = self._validation_source_readiness(
            season=season,
            requested_through_date=requested_through_date,
        )
        current_stats_run_id = source_readiness["current_season"]["stats_run_id"]
        with self.database.connect() as connection:
            run_rows = connection.execute(
                "SELECT provider,status,COUNT(*) AS count "
                "FROM stats_ingestion_runs GROUP BY provider,status "
                "ORDER BY provider,status"
            ).fetchall()
            raw_rows = connection.execute(
                "SELECT provider,endpoint_category,COUNT(*) AS file_count,"
                "COALESCE(SUM(CAST(json_extract(metadata_json,'$.size_bytes') "
                "AS INTEGER)),0) AS byte_count,MIN(retrieved_at) AS first_retrieved_at,"
                "MAX(retrieved_at) AS latest_retrieved_at "
                "FROM stats_raw_payload_metadata "
                "GROUP BY provider,endpoint_category "
                "ORDER BY provider,endpoint_category"
            ).fetchall()
            raw_totals = connection.execute(
                "SELECT COUNT(*) AS file_count,"
                "COALESCE(SUM(CAST(json_extract(metadata_json,'$.size_bytes') "
                "AS INTEGER)),0) AS byte_count FROM stats_raw_payload_metadata"
            ).fetchone()
            retrosheet_seasons = connection.execute(
                "SELECT season,COUNT(*) AS game_count FROM stats_game_identities "
                "WHERE provider='retrosheet' GROUP BY season ORDER BY season"
            ).fetchall()
            retrosheet_roles = connection.execute(
                "SELECT snapshot.role,COUNT(*) AS count "
                "FROM stats_game_player_snapshots AS snapshot "
                "JOIN stats_game_identities AS game "
                "ON game.game_identity_id=snapshot.game_identity_id "
                "WHERE game.provider='retrosheet' GROUP BY snapshot.role "
                "ORDER BY snapshot.role"
            ).fetchall()
            retrosheet_excluded = connection.execute(
                "SELECT dataset_key,reason_code,COUNT(*) AS count "
                "FROM stats_excluded_source_rows WHERE provider='retrosheet' "
                "GROUP BY dataset_key,reason_code ORDER BY dataset_key,reason_code"
            ).fetchall()
            retrosheet_counts = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM stats_game_identities "
                " WHERE provider='retrosheet') AS games,"
                "(SELECT COUNT(*) FROM stats_game_team_snapshots AS snapshot "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=snapshot.game_identity_id "
                " WHERE game.provider='retrosheet') AS team_game_lines,"
                "(SELECT COUNT(*) FROM stats_game_player_snapshots AS snapshot "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=snapshot.game_identity_id "
                " WHERE game.provider='retrosheet') AS player_game_lines,"
                "(SELECT COUNT(*) FROM stats_lineup_snapshots AS lineup "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=lineup.game_identity_id "
                " WHERE game.provider='retrosheet') AS lineup_snapshots,"
                "(SELECT COUNT(*) FROM stats_lineup_entries AS entry "
                " JOIN stats_lineup_snapshots AS lineup "
                " ON lineup.lineup_snapshot_id=entry.lineup_snapshot_id "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=lineup.game_identity_id "
                " WHERE game.provider='retrosheet') AS lineup_entries,"
                "(SELECT COUNT(*) FROM stats_play_revisions AS revision "
                " JOIN stats_play_identities AS play "
                " ON play.play_identity_id=revision.play_identity_id "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=play.game_identity_id "
                " WHERE game.provider='retrosheet') AS play_revisions,"
                "(SELECT COUNT(*) FROM stats_player_identities "
                " WHERE provider='retrosheet') AS player_identities"
            ).fetchone()
            bref_counts = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM stats_game_identities "
                " WHERE provider='baseball_reference' AND season=?) AS games,"
                "(SELECT COUNT(*) FROM stats_game_status_observations AS status "
                " JOIN stats_game_identities AS game "
                " ON game.game_identity_id=status.game_identity_id "
                " WHERE game.provider='baseball_reference' AND game.season=?) "
                "AS schedule_observations,"
                "(SELECT COALESCE(SUM(checkpoint.records_seen),0) "
                " FROM stats_checkpoints AS checkpoint "
                " JOIN stats_ingestion_runs AS run "
                " ON run.stats_run_id=checkpoint.stats_run_id "
                " WHERE run.provider='current_mlb_stats' "
                " AND run.scope_key=? AND checkpoint.dataset_key="
                "'baseball_reference_schedule') AS source_schedule_rows,"
                "(SELECT COUNT(*) FROM stats_season_snapshots "
                " WHERE provider='baseball_reference' AND season=? "
                " AND split_key LIKE 'batting:%') AS batting_rows,"
                "(SELECT COUNT(*) FROM stats_season_snapshots "
                " WHERE provider='baseball_reference' AND season=? "
                " AND split_key LIKE 'pitching:%') AS pitching_rows",
                (season, season, f"regular-season:{season}", season, season),
            ).fetchone()
            statcast_counts = connection.execute(
                "SELECT COUNT(DISTINCT identity.pitch_identity_id) AS pitches,"
                "COUNT(revision.statcast_revision_id) AS revisions,"
                "MIN(game.official_date) AS first_game_date,"
                "MAX(game.official_date) AS latest_game_date,"
                "COALESCE(SUM(CASE WHEN revision.revision_number>1 THEN 1 ELSE 0 END),0) "
                "AS correction_revisions "
                "FROM statcast_pitch_identities AS identity "
                "JOIN stats_game_identities AS game "
                "ON game.game_identity_id=identity.game_identity_id "
                "LEFT JOIN statcast_pitch_revisions AS revision "
                "ON revision.pitch_identity_id=identity.pitch_identity_id "
                "WHERE game.season=?",
                (season,),
            ).fetchone()
            duplicate_games = connection.execute(
                "SELECT COALESCE(SUM(count-1),0) FROM ("
                "SELECT COUNT(*) AS count FROM stats_game_identities "
                "GROUP BY provider,provider_game_id HAVING COUNT(*)>1)"
            ).fetchone()[0]
            duplicate_pitches = connection.execute(
                "SELECT COALESCE(SUM(count-1),0) FROM ("
                "SELECT COUNT(*) AS count FROM statcast_pitch_identities "
                "GROUP BY game_pk,at_bat_number,pitch_number HAVING COUNT(*)>1)"
            ).fetchone()[0]
            player_game_inventory = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM stats_game_player_snapshots) AS snapshot_rows,"
                "(SELECT COUNT(*) FROM (SELECT 1 FROM stats_game_player_snapshots "
                " GROUP BY game_identity_id,team_identity_id,player_identity_id,role,source_row_key)) "
                "AS logical_identities,"
                "(SELECT COUNT(*) FROM stats_game_player_snapshots "
                " WHERE revision_number>1) AS correction_revisions,"
                "(SELECT COALESCE(SUM(count-1),0) FROM (SELECT COUNT(*) AS count "
                " FROM stats_game_player_snapshots GROUP BY game_identity_id,"
                " team_identity_id,player_identity_id,role,revision_number "
                " HAVING COUNT(*)>1)) AS duplicate_revision_rows,"
                "(SELECT COALESCE(SUM(count-1),0) FROM (SELECT COUNT(*) AS count "
                " FROM stats_game_player_snapshots GROUP BY game_identity_id,"
                " team_identity_id,player_identity_id,role,normalized_checksum "
                " HAVING COUNT(*)>1)) AS duplicate_normalized_rows,"
                "(SELECT COALESCE(SUM(count-1),0) FROM ("
                " SELECT latest.game_identity_id,latest.team_identity_id,"
                " latest.player_identity_id,latest.role,COUNT(*) AS count "
                " FROM stats_game_player_snapshots AS latest "
                " WHERE latest.revision_number=(SELECT MAX(candidate.revision_number) "
                " FROM stats_game_player_snapshots AS candidate "
                " WHERE candidate.game_identity_id=latest.game_identity_id "
                " AND candidate.team_identity_id=latest.team_identity_id "
                " AND candidate.player_identity_id=latest.player_identity_id "
                " AND candidate.role=latest.role) "
                " GROUP BY latest.game_identity_id,latest.team_identity_id,"
                " latest.player_identity_id,latest.role HAVING COUNT(*)>1)) "
                "AS latest_logical_duplicates"
            ).fetchone()
            repeated_raw = connection.execute(
                "SELECT COALESCE(SUM(count-1),0) FROM ("
                "SELECT COUNT(*) AS count FROM stats_raw_payload_metadata "
                "GROUP BY provider,endpoint_category,checksum_sha256 "
                "HAVING COUNT(*)>1)"
            ).fetchone()[0]
            reconciliation_rows = connection.execute(
                "SELECT status,COUNT(*) AS count,"
                "COALESCE(SUM(conflict_count),0) AS conflicts "
                "FROM stats_reconciliations GROUP BY status ORDER BY status"
            ).fetchall()
            reconciliation_items = connection.execute(
                "SELECT severity,code,COUNT(*) AS count "
                "FROM stats_reconciliation_items GROUP BY severity,code "
                "ORDER BY severity,code"
            ).fetchall()
            failed_checkpoints = int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_checkpoints WHERE status='failed'"
                ).fetchone()[0]
            )
            failed_runs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_ingestion_runs WHERE status='failed'"
                ).fetchone()[0]
            )
            conflicts = int(
                connection.execute("SELECT COUNT(*) FROM stats_conflicts").fetchone()[0]
            )
            feature_inventory_rows: list[Any] = []
            feature_entity_rows: list[Any] = []
            if current_stats_run_id is not None:
                feature_inventory_rows = connection.execute(
                    "SELECT entity_kind,feature_version,feature_as_of,COUNT(*) "
                    "AS snapshot_count,COUNT(DISTINCT CASE entity_kind "
                    "WHEN 'game' THEN game_identity_id WHEN 'team' THEN "
                    "team_identity_id ELSE canonical_player_id END) AS entity_count,"
                    "SUM(CASE WHEN completeness_state='complete' THEN 1 ELSE 0 END) "
                    "AS complete_count,SUM(CASE WHEN completeness_state='degraded' "
                    "THEN 1 ELSE 0 END) AS degraded_count,SUM(CASE WHEN "
                    "completeness_state='blocked' THEN 1 ELSE 0 END) AS blocked_count,"
                    "MIN(observed_through) AS earliest_observed_through,"
                    "MAX(observed_through) AS latest_observed_through,"
                    "MIN(complete_through) AS earliest_complete_through,"
                    "MAX(complete_through) AS latest_complete_through "
                    "FROM stats_feature_snapshots WHERE stats_run_id=? "
                    "GROUP BY entity_kind,feature_version,feature_as_of "
                    "ORDER BY feature_as_of,entity_kind,feature_version",
                    (current_stats_run_id,),
                ).fetchall()
                feature_entity_rows = connection.execute(
                    "SELECT entity_kind,COUNT(*) AS snapshot_count,"
                    "COUNT(DISTINCT feature_as_of) AS feature_as_of_count,"
                    "MIN(feature_as_of) AS earliest_feature_as_of,"
                    "MAX(feature_as_of) AS latest_feature_as_of "
                    "FROM stats_feature_snapshots WHERE stats_run_id=? "
                    "GROUP BY entity_kind ORDER BY entity_kind",
                    (current_stats_run_id,),
                ).fetchall()

        schema_matches = (
            schema.get("version") == CURRENT_SCHEMA_VERSION
            and schema.get("user_version") == CURRENT_SCHEMA_VERSION
            and schema.get("checksum") == schema.get("expected_checksum")
            and schema.get("fingerprint") == schema.get("expected_fingerprint")
        )
        return {
            "contract_version": "DSE_STATS_VALIDATION_DETAILS_V1",
            "provider_inventory": stats_provider_inventory(season),
            "source_readiness": source_readiness,
            "database": {
                "schema": schema,
                "schema_matches_application": schema_matches,
                "integrity_check": list(integrity.get("integrity_check", [])),
                "foreign_key_violations": [
                    list(value) for value in integrity.get("foreign_key_violations", [])
                ],
            },
            "ingestion_runs": [dict(row) for row in run_rows],
            "raw_retention": {
                "file_count": int(raw_totals["file_count"]),
                "byte_count": int(raw_totals["byte_count"]),
                "by_provider_endpoint": [dict(row) for row in raw_rows],
                "checksum_inventory": dict(raw_checksum_inventory),
            },
            "retrosheet": {
                "imported_seasons": [dict(row) for row in retrosheet_seasons],
                "row_counts": dict(retrosheet_counts),
                "player_rows_by_role": [dict(row) for row in retrosheet_roles],
                "excluded_rows": [dict(row) for row in retrosheet_excluded],
            },
            "baseball_reference": {
                "season": season,
                "row_counts": dict(bref_counts),
            },
            "statcast": {
                "season": season,
                **dict(statcast_counts),
                "conflicts": conflicts,
            },
            "duplicates": {
                "game_identity_duplicates": int(duplicate_games),
                "statcast_pitch_tuple_duplicates": int(duplicate_pitches),
                "repeated_raw_observations": int(repeated_raw),
                "player_game_logical_duplicates": int(
                    player_game_inventory["latest_logical_duplicates"]
                ),
                "player_game_duplicate_revision_rows": int(
                    player_game_inventory["duplicate_revision_rows"]
                ),
                "player_game_duplicate_normalized_rows": int(
                    player_game_inventory["duplicate_normalized_rows"]
                ),
            },
            "player_game_line_inventory": dict(player_game_inventory),
            "feature_inventory": {
                "contract_version": "DSE_STATS_FEATURE_INVENTORY_V1",
                "stats_run_id": current_stats_run_id,
                "snapshot_count": sum(
                    int(row["snapshot_count"]) for row in feature_entity_rows
                ),
                "by_entity_type": [dict(row) for row in feature_entity_rows],
                "by_entity_type_and_feature_as_of": [
                    dict(row) for row in feature_inventory_rows
                ],
            },
            "reconciliation": {
                "statuses": [dict(row) for row in reconciliation_rows],
                "items": [dict(row) for row in reconciliation_items],
            },
            "schema_drift": {
                "database_schema_drift_detected": not schema_matches,
                "failed_ingestion_runs": failed_runs,
                "failed_checkpoints": failed_checkpoints,
                "provider_contract_status": (
                    "no_failed_checkpoints_observed"
                    if failed_checkpoints == 0
                    else "failed_checkpoints_require_review"
                ),
            },
        }

    def validate(self, request: AcquisitionRequest) -> AcquisitionResult:
        integrity = self.database.integrity_check()
        counts: dict[str, int] = {}
        warnings: list[str] = []
        provider = "current_mlb_stats"
        season = request.requested_through_date.year
        run_ids = self.repository.list_scope_run_ids(provider, season)
        with self.database.connect() as connection:
            counts["stats_ingestion_runs"] = len(run_ids)
            raw_rows: list[Any] = []
            scoped_tables = (
                "stats_raw_payload_metadata",
                "stats_game_status_observations",
                "statcast_pitch_revisions",
                "stats_feature_snapshots",
                "stats_conflicts",
                "stats_reconciliations",
            )
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                for table in scoped_tables:
                    counts[table] = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE stats_run_id IN ({placeholders})",
                            tuple(run_ids),
                        ).fetchone()[0]
                    )
                counts["stats_game_identities"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ("
                        "SELECT game_identity_id "
                        "FROM stats_game_status_observations "
                        f"WHERE stats_run_id IN ({placeholders}) "
                        "UNION "
                        "SELECT identity.game_identity_id "
                        "FROM statcast_pitch_revisions AS revision "
                        "JOIN statcast_pitch_identities AS identity "
                        "ON identity.pitch_identity_id=revision.pitch_identity_id "
                        f"WHERE revision.stats_run_id IN ({placeholders})"
                        ")",
                        (*run_ids, *run_ids),
                    ).fetchone()[0]
                )
            else:
                for table in scoped_tables:
                    counts[table] = 0
                counts["stats_game_identities"] = 0
            raw_rows = connection.execute(
                "SELECT raw_payload_id,provider,endpoint_category,artifact_relpath,"
                "checksum_sha256,CAST(json_extract(metadata_json,'$.size_bytes') "
                "AS INTEGER) AS size_bytes "
                "FROM stats_raw_payload_metadata ORDER BY raw_payload_id"
            ).fetchall()
        if not integrity["ok"]:
            warnings.append("sqlite_integrity_failed")
        if not run_ids:
            warnings.append("validation_scope_has_no_ingestion_runs")
        raw_checksum_inventory, raw_warnings = build_raw_checksum_inventory(
            self.raw_store.root,
            tuple(dict(row) for row in raw_rows),
        )
        warnings.extend(raw_warnings)
        validation_details = self._validation_details(
            season=season,
            requested_through_date=request.requested_through_date,
            integrity=integrity,
            raw_checksum_inventory=raw_checksum_inventory,
        )
        source_readiness = validation_details["source_readiness"]
        current_readiness = source_readiness["current_season"]
        projected = current_readiness["projected_completeness"]
        completeness = (
            _stored_completeness_for_cutoff(
                json.dumps({"completeness": projected}, sort_keys=True),
                request.requested_through_date,
            )
            if projected is not None
            else None
        )
        if completeness is not None and completeness.partial_date_reason is not None:
            warnings.append(str(completeness.partial_date_reason))
        if not source_readiness["retrosheet_through_prior_season"]["ready"]:
            warnings.append("retrosheet_through_prior_season_not_validated")
        if not current_readiness["schedule"]["ready"]:
            warnings.append("baseball_reference_schedule_coverage_not_validated")
        if not current_readiness["baseball_reference_aggregates"]["ready"]:
            warnings.append("baseball_reference_aggregate_coverage_not_validated")
        if not current_readiness["statcast"]["ready"]:
            warnings.append("statcast_completed_date_coverage_not_validated")
        if current_readiness["reconciliation_status"] not in {"passed", "warnings"}:
            warnings.append("current_source_reconciliation_not_validated")
        if not current_readiness["counting_reconciliation"]["ready"]:
            warnings.append(
                "baseball_reference_statcast_counting_reconciliation_not_validated"
            )
        completeness_outcome_evidence = (
            build_acquisition_outcome_evidence(
                completeness,
                completeness_watermark_eligible=True,
            )
            if completeness is not None
            else AcquisitionOutcomeEvidence()
        )
        proven_partial = bool(completeness_outcome_evidence.partial_reasons)
        hard_failures: list[str] = list(
            completeness_outcome_evidence.hard_failures
        )
        if not integrity["ok"]:
            hard_failures.append("sqlite_integrity_failed")
        if not run_ids:
            hard_failures.append("validation_scope_has_no_ingestion_runs")
        if raw_warnings:
            hard_failures.append("raw_evidence_validation_failed")
        if not source_readiness["retrosheet_through_prior_season"]["ready"]:
            hard_failures.append("retrosheet_through_prior_season_not_validated")
        if not current_readiness["schedule"]["ready"]:
            hard_failures.append("baseball_reference_schedule_coverage_not_validated")
        if not proven_partial:
            if not current_readiness["baseball_reference_aggregates"]["ready"]:
                hard_failures.append("baseball_reference_aggregate_coverage_not_validated")
            if not current_readiness["statcast"]["ready"]:
                hard_failures.append("statcast_completed_date_coverage_not_validated")
            if current_readiness["reconciliation_status"] not in {"passed", "warnings"}:
                hard_failures.append("current_source_reconciliation_not_validated")
            if not current_readiness["counting_reconciliation"]["ready"]:
                hard_failures.append(
                    "baseball_reference_statcast_counting_reconciliation_not_validated"
                )
        outcome = classify_acquisition_outcome(
            AcquisitionOutcomeEvidence(
                hard_failures=tuple(hard_failures),
                partial_reasons=completeness_outcome_evidence.partial_reasons,
                informational_warnings=tuple(sorted(set(warnings))),
            )
        )
        status = "completed_with_warnings" if warnings else "completed"
        result = AcquisitionResult(
            command=AcquisitionCommand.VALIDATE,
            status=status,
            requested_through_date=request.requested_through_date,
            run_id=None,
            stats_run_id=None,
            counts=counts,
            completeness=completeness,
            warnings=tuple(sorted(set(warnings))),
            report_path=self.report_path,
            dry_run=request.dry_run,
            outcome=outcome,
            source_observed_at=(
                datetime.fromisoformat(str(current_readiness["source_observed_at"]))
                if current_readiness["source_observed_at"]
                else None
            ),
            validation_details=validation_details,
        )
        self._write_report(result)
        return result


def default_stats_user_agent() -> str:
    return os.getenv(
        "STATS_USER_AGENT",
        "Daily-Sports-Edge-MLB/1.0 (statistics acquisition)",
    )
