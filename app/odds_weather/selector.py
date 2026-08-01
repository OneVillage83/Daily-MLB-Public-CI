"""Deterministic, offline retained-evidence loading for Odds + Weather V1."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.artifacts import (
    UnsafeArtifactPath,
    resolve_contained_path,
    validate_artifact_relpath,
)
from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.odds_weather.contracts import (
    OddsProviderEventV1,
    OddsWeatherContractError,
    OddsWeatherWarningV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
)
from app.raw_payloads import (
    EndpointCategory,
    ProviderName,
    RawPayloadCapture,
    sanitized_json_bytes,
)
from app.redaction import redact_value


class OddsWeatherSelectorError(RuntimeError):
    """Raised when retained Phase 4 rows cannot be reconstructed exactly."""


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise OddsWeatherSelectorError(f"retained {field} is not ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsWeatherSelectorError(f"retained {field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OddsWeatherSelectorError(f"retained {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _checksum(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise OddsWeatherSelectorError(f"retained {field} is not a SHA-256 checksum")
    return value


def _mapping_json(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise OddsWeatherSelectorError(f"retained {field} is not JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OddsWeatherSelectorError(f"retained {field} is malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise OddsWeatherSelectorError(f"retained {field} must be a JSON object")
    return parsed


def _canonical_text(value: Mapping[str, object]) -> str:
    return canonical_json_bytes(dict(value)).decode("utf-8")


def _warning_key(warning: OddsWeatherWarningV1) -> tuple[str, ...]:
    return (
        warning.source_game_id or "",
        warning.domain.value,
        warning.code,
        warning.provider or "",
        warning.provider_event_id or "",
        warning.message,
    )


def _warnings(values: Iterable[OddsWeatherWarningV1]) -> tuple[OddsWeatherWarningV1, ...]:
    result = tuple(values)
    if any(not isinstance(item, OddsWeatherWarningV1) for item in result):
        raise ValueError("warning inventories must contain OddsWeatherWarningV1 values")
    return tuple(sorted(result, key=_warning_key))


@dataclass(frozen=True, slots=True)
class OddsWeatherRawCaptureV1:
    """One immutable sanitized provider response retained outside SQLite."""

    ordinal: int
    provider: str
    endpoint_category: str
    source_game_id: str | None
    provider_event_id: str | None
    retrieved_at: datetime
    provider_timestamp: datetime | None
    raw_relpath: str
    checksum: str
    byte_count: int

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise ValueError("raw capture ordinal must be a positive integer")
        allowed = {
            "the_odds_api": {"mlb_odds"},
            "nws": {"point_lookup", "hourly_forecast"},
            "openweather": {"one_call"},
        }
        if self.endpoint_category not in allowed.get(self.provider, set()):
            raise ValueError("raw capture provider/endpoint combination is invalid")
        for field in ("source_game_id", "provider_event_id"):
            value = getattr(self, field)
            if value is not None and (
                not isinstance(value, str) or not value or value != value.strip()
            ):
                raise ValueError(f"{field} must be a trimmed string when present")
        object.__setattr__(self, "retrieved_at", _coerce_aware(self.retrieved_at, "retrieved_at"))
        if self.provider_timestamp is not None:
            object.__setattr__(
                self,
                "provider_timestamp",
                _coerce_aware(self.provider_timestamp, "provider_timestamp"),
            )
        object.__setattr__(self, "raw_relpath", validate_artifact_relpath(self.raw_relpath))
        object.__setattr__(self, "checksum", _plain_checksum(self.checksum, "checksum"))
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int) or self.byte_count <= 0:
            raise ValueError("raw capture byte_count must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "checksum": self.checksum,
            "endpoint_category": self.endpoint_category,
            "ordinal": self.ordinal,
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "provider_timestamp": (
                None if self.provider_timestamp is None else self.provider_timestamp.isoformat()
            ),
            "raw_relpath": self.raw_relpath,
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_game_id": self.source_game_id,
        }

    @property
    def row_checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def _coerce_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _plain_checksum(value: str, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 checksum")
    return value


@dataclass(frozen=True, slots=True)
class RetainedOddsProviderEventV1:
    revision_ordinal: int
    event: OddsProviderEventV1

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision_ordinal, bool)
            or not isinstance(self.revision_ordinal, int)
            or self.revision_ordinal < 1
        ):
            raise ValueError("provider event revision_ordinal must be positive")
        if not isinstance(self.event, OddsProviderEventV1):
            raise ValueError("retained provider event must use OddsProviderEventV1")

    @property
    def event_checksum(self) -> str:
        return canonical_sha256(self.event.mutable_event())

    @property
    def row_checksum(self) -> str:
        return canonical_sha256(self.event.as_dict())

    def identity_dict(self) -> dict[str, object]:
        return {
            "event_checksum": self.event_checksum,
            "provider_event_id": self.event.provider_event_id,
            "retrieved_at": self.event.retrieved_at.isoformat(),
            "revision_ordinal": self.revision_ordinal,
            "row_checksum": self.row_checksum,
        }


@dataclass(frozen=True, slots=True)
class RetainedWeatherRevisionV1:
    revision_ordinal: int
    evidence: WeatherForecastEvidenceV1

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision_ordinal, bool)
            or not isinstance(self.revision_ordinal, int)
            or self.revision_ordinal < 1
        ):
            raise ValueError("weather revision_ordinal must be positive")
        if not isinstance(self.evidence, WeatherForecastEvidenceV1):
            raise ValueError("retained weather revision must use WeatherForecastEvidenceV1")

    @property
    def forecast_checksum(self) -> str:
        return canonical_sha256(dict(self.evidence.forecast))

    @property
    def row_checksum(self) -> str:
        return canonical_sha256(self.evidence.as_dict())

    def identity_dict(self) -> dict[str, object]:
        return {
            "forecast_checksum": self.forecast_checksum,
            "provider": self.evidence.provider.value,
            "retrieved_at": self.evidence.retrieved_at.isoformat(),
            "revision_ordinal": self.revision_ordinal,
            "row_checksum": self.row_checksum,
            "source_game_id": self.evidence.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class OddsWeatherRetainedEvidenceInventoryV1:
    """Complete immutable input and selection evidence for one Phase 4 attempt."""

    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    upstream_daily_slate_snapshot_id: str
    upstream_daily_slate_checksum: str
    upstream_game_state_snapshot_id: str
    upstream_game_state_checksum: str
    upstream_baseball_intelligence_snapshot_id: str
    upstream_baseball_intelligence_checksum: str
    raw_captures: tuple[OddsWeatherRawCaptureV1, ...]
    provider_events: tuple[RetainedOddsProviderEventV1, ...]
    weather_revisions: tuple[RetainedWeatherRevisionV1, ...]
    source_warnings: tuple[OddsWeatherWarningV1, ...] = ()
    final_warnings: tuple[OddsWeatherWarningV1, ...] = ()
    selected_raw_capture_checksums: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", validate_run_id(self.run_id))
        if (
            isinstance(self.phase_attempt, bool)
            or not isinstance(self.phase_attempt, int)
            or self.phase_attempt < 1
        ):
            raise ValueError("phase_attempt must be a positive integer")
        object.__setattr__(self, "requested_date", parse_requested_date(self.requested_date).isoformat())
        object.__setattr__(self, "as_of_time", _coerce_aware(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", _coerce_aware(self.observed_at, "observed_at"))
        for field in (
            "phase_input_checksum",
            "upstream_daily_slate_checksum",
            "upstream_game_state_checksum",
            "upstream_baseball_intelligence_checksum",
        ):
            object.__setattr__(self, field, _plain_checksum(getattr(self, field), field))
        for field in (
            "upstream_daily_slate_snapshot_id",
            "upstream_game_state_snapshot_id",
            "upstream_baseball_intelligence_snapshot_id",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be a non-empty trimmed string")
        raw = tuple(sorted(tuple(self.raw_captures), key=lambda item: item.ordinal))
        if tuple(item.ordinal for item in raw) != tuple(range(1, len(raw) + 1)):
            raise ValueError("raw capture ordinals must be contiguous")
        if len({item.checksum for item in raw}) != len(raw):
            raise ValueError("raw capture inventory contains duplicate checksums")
        object.__setattr__(self, "raw_captures", raw)
        events = tuple(
            sorted(
                tuple(self.provider_events),
                key=lambda item: (
                    item.event.provider_event_id,
                    item.revision_ordinal,
                    item.event.retrieved_at,
                ),
            )
        )
        _require_group_ordinals(
            ((item.event.provider_event_id, item.revision_ordinal) for item in events),
            "provider event",
        )
        if len({(item.event.provider_event_id, item.event.retrieved_at) for item in events}) != len(events):
            raise ValueError("provider event inventory contains duplicate revision identities")
        object.__setattr__(self, "provider_events", events)
        weather = tuple(
            sorted(
                tuple(self.weather_revisions),
                key=lambda item: (
                    item.evidence.source_game_id,
                    item.evidence.provider.value,
                    item.revision_ordinal,
                    item.evidence.retrieved_at,
                ),
            )
        )
        _require_group_ordinals(
            (
                (
                    f"{item.evidence.source_game_id}\0{item.evidence.provider.value}",
                    item.revision_ordinal,
                )
                for item in weather
            ),
            "weather revision",
        )
        if len(
            {
                (
                    item.evidence.source_game_id,
                    item.evidence.provider,
                    item.evidence.retrieved_at,
                )
                for item in weather
            }
        ) != len(weather):
            raise ValueError("weather inventory contains duplicate revision identities")
        object.__setattr__(self, "weather_revisions", weather)
        object.__setattr__(self, "source_warnings", _warnings(self.source_warnings))
        final = tuple(self.final_warnings)
        if any(not isinstance(item, OddsWeatherWarningV1) for item in final):
            raise ValueError("final warnings must contain OddsWeatherWarningV1 values")
        object.__setattr__(self, "final_warnings", final)
        selected = tuple(sorted({_plain_checksum(item, "selected raw checksum") for item in self.selected_raw_capture_checksums}))
        if not set(selected).issubset({item.checksum for item in raw}):
            raise ValueError("selected raw capture inventory is not retained")
        object.__setattr__(self, "selected_raw_capture_checksums", selected)
        retained_checksums = {item.checksum for item in raw}
        for event in events:
            if event.event.raw_capture_checksum not in retained_checksums:
                raise ValueError("provider event references an unretained raw capture")
        for revision in weather:
            if not set(revision.evidence.raw_capture_checksums).issubset(retained_checksums):
                raise ValueError("weather revision references an unretained raw capture")

    @property
    def odds_events(self) -> tuple[OddsProviderEventV1, ...]:
        return tuple(item.event for item in self.provider_events)

    @property
    def weather_evidence(self) -> tuple[WeatherForecastEvidenceV1, ...]:
        return tuple(item.evidence for item in self.weather_revisions)

    @property
    def eligible_provider_event_revisions(self) -> tuple[RetainedOddsProviderEventV1, ...]:
        return tuple(item for item in self.provider_events if item.event.retrieved_at <= self.observed_at)

    @property
    def future_provider_event_revisions(self) -> tuple[RetainedOddsProviderEventV1, ...]:
        return tuple(item for item in self.provider_events if item.event.retrieved_at > self.observed_at)

    @property
    def eligible_weather_revisions(self) -> tuple[RetainedWeatherRevisionV1, ...]:
        return tuple(item for item in self.weather_revisions if item.evidence.retrieved_at <= self.observed_at)

    @property
    def future_weather_revisions(self) -> tuple[RetainedWeatherRevisionV1, ...]:
        return tuple(item for item in self.weather_revisions if item.evidence.retrieved_at > self.observed_at)

    @property
    def odds_revision_inventory(self) -> tuple[dict[str, object], ...]:
        rows: list[dict[str, object]] = []
        for retained in self.provider_events:
            for ordinal, row in enumerate(retained.event.mutable_history_rows(), start=1):
                rows.append(
                    {
                        "bookmaker_key": row.get("bookmaker_key"),
                        "event_retrieved_at": retained.event.retrieved_at.isoformat(),
                        "market_key": row.get("market_key"),
                        "ordinal": ordinal,
                        "outcome_name": row.get("outcome_name"),
                        "point": row.get("point"),
                        "provider_event_id": retained.event.provider_event_id,
                        "retrieved_at": row.get("retrieved_at"),
                        "row_checksum": canonical_sha256(row),
                    }
                )
        return tuple(rows)

    def identity_dict(self) -> dict[str, object]:
        return {
            "final_warnings": [item.as_dict() for item in self.final_warnings],
            "observed_at": self.observed_at.isoformat(),
            "odds_revision_inventory": list(self.odds_revision_inventory),
            "phase_attempt": self.phase_attempt,
            "phase_input_checksum": self.phase_input_checksum,
            "provider_event_inventory": [item.identity_dict() for item in self.provider_events],
            "raw_capture_inventory": [item.as_dict() for item in self.raw_captures],
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "selected_raw_capture_checksums": list(self.selected_raw_capture_checksums),
            "source_warnings": [item.as_dict() for item in self.source_warnings],
            "upstream_baseball_intelligence_checksum": self.upstream_baseball_intelligence_checksum,
            "upstream_baseball_intelligence_snapshot_id": self.upstream_baseball_intelligence_snapshot_id,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_daily_slate_snapshot_id": self.upstream_daily_slate_snapshot_id,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
            "upstream_game_state_snapshot_id": self.upstream_game_state_snapshot_id,
            "weather_revision_inventory": [item.identity_dict() for item in self.weather_revisions],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())


def _require_group_ordinals(values: Iterable[tuple[str, int]], label: str) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for key, ordinal in values:
        groups[key].append(ordinal)
    for ordinals in groups.values():
        if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
            raise ValueError(f"{label} ordinals must be contiguous per identity")


class OddsWeatherRetainedEvidenceSelector:
    """Reconstruct exact retained Phase 4 evidence without making PIT decisions."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))

    def verify_raw_capture(
        self,
        capture: OddsWeatherRawCaptureV1,
        *,
        run_id: str,
        requested_date: str,
    ) -> None:
        expected_relpath = (
            f"{requested_date}/{run_id}/raw/{capture.provider}/"
            f"{capture.endpoint_category}/{capture.checksum}.json"
        )
        if capture.raw_relpath != validate_artifact_relpath(expected_relpath):
            raise OddsWeatherSelectorError("raw capture path does not match its attempt identity")
        try:
            path = resolve_contained_path(self.artifact_root, capture.raw_relpath)
            metadata = path.stat()
            content = path.read_bytes()
        except (FileNotFoundError, OSError, UnsafeArtifactPath) as exc:
            raise OddsWeatherSelectorError("raw capture is missing or unsafe") from exc
        if getattr(metadata, "st_nlink", 1) != 1:
            raise OddsWeatherSelectorError("raw capture uses an unsafe hard link")
        if len(content) != capture.byte_count or hashlib.sha256(content).hexdigest() != capture.checksum:
            raise OddsWeatherSelectorError("raw capture bytes, checksum, or byte count disagree")
        try:
            payload: Any = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OddsWeatherSelectorError("raw capture is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict | list):
            raise OddsWeatherSelectorError("raw capture must be a JSON object or array")
        try:
            rebuilt = sanitized_json_bytes(
                RawPayloadCapture(
                    provider=cast(ProviderName, capture.provider),
                    endpoint_category=cast(
                        EndpointCategory,
                        capture.endpoint_category,
                    ),
                    payload=payload,
                    retrieved_at=capture.retrieved_at.isoformat(),
                    provider_timestamp=(
                        None
                        if capture.provider_timestamp is None
                        else capture.provider_timestamp.isoformat()
                    ),
                    content_type="application/json",
                    event_id=capture.provider_event_id or capture.source_game_id,
                ),
                secret_values=self.secret_values,
            )
        except (TypeError, ValueError) as exc:
            raise OddsWeatherSelectorError("raw capture violates its sanitized contract") from exc
        if rebuilt != content or redact_value(capture.as_dict(), self.secret_values) != capture.as_dict():
            raise OddsWeatherSelectorError("raw capture is noncanonical or credential-bearing")

    def load_inventory(
        self,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        phase_attempt: int,
        source_warnings: Iterable[OddsWeatherWarningV1] = (),
        final_warnings: Iterable[OddsWeatherWarningV1] = (),
        selected_raw_capture_checksums: Iterable[str] = (),
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        safe_run = validate_run_id(run_id)
        attempt = connection.execute(
            "SELECT * FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
            (safe_run, phase_attempt),
        ).fetchone()
        if attempt is None:
            raise OddsWeatherSelectorError("Odds Weather attempt evidence does not exist")
        raw = self._load_raw_captures(connection, safe_run, phase_attempt, str(attempt["requested_date"]))
        events = self._load_provider_events(connection, safe_run, phase_attempt)
        weather = self._load_weather_revisions(connection, safe_run, phase_attempt)
        return OddsWeatherRetainedEvidenceInventoryV1(
            run_id=safe_run,
            phase_attempt=phase_attempt,
            requested_date=str(attempt["requested_date"]),
            as_of_time=_aware(attempt["as_of_time"], "as_of_time"),
            observed_at=_aware(attempt["observed_at"], "observed_at"),
            phase_input_checksum=_checksum(attempt["phase_input_checksum"], "phase_input_checksum"),
            upstream_daily_slate_snapshot_id=str(attempt["upstream_daily_slate_snapshot_id"]),
            upstream_daily_slate_checksum=_checksum(attempt["upstream_daily_slate_checksum"], "DailySlate checksum"),
            upstream_game_state_snapshot_id=str(attempt["upstream_game_state_snapshot_id"]),
            upstream_game_state_checksum=_checksum(attempt["upstream_game_state_checksum"], "GameState checksum"),
            upstream_baseball_intelligence_snapshot_id=str(attempt["upstream_baseball_intelligence_snapshot_id"]),
            upstream_baseball_intelligence_checksum=_checksum(attempt["upstream_baseball_intelligence_checksum"], "BIA checksum"),
            raw_captures=raw,
            provider_events=events,
            weather_revisions=weather,
            source_warnings=tuple(source_warnings),
            final_warnings=tuple(final_warnings),
            selected_raw_capture_checksums=tuple(selected_raw_capture_checksums),
        )

    def _load_raw_captures(self, connection: sqlite3.Connection, run_id: str, attempt: int, requested_date: str) -> tuple[OddsWeatherRawCaptureV1, ...]:
        rows = connection.execute(
            "SELECT * FROM odds_weather_raw_captures WHERE run_id=? AND phase_attempt=? ORDER BY ordinal",
            (run_id, attempt),
        ).fetchall()
        result: list[OddsWeatherRawCaptureV1] = []
        for row in rows:
            capture = OddsWeatherRawCaptureV1(
                ordinal=int(row["ordinal"]),
                provider=str(row["provider"]),
                endpoint_category=str(row["endpoint_category"]),
                source_game_id=None if row["source_game_id"] is None else str(row["source_game_id"]),
                provider_event_id=None if row["provider_event_id"] is None else str(row["provider_event_id"]),
                retrieved_at=_aware(row["retrieved_at"], "raw retrieved_at"),
                provider_timestamp=None if row["provider_timestamp"] is None else _aware(row["provider_timestamp"], "provider_timestamp"),
                raw_relpath=str(row["raw_relpath"]),
                checksum=_checksum(row["raw_capture_checksum"], "raw_capture_checksum"),
                byte_count=int(row["raw_byte_count"]),
            )
            metadata = _mapping_json(row["canonical_metadata_json"], "raw metadata")
            if (
                metadata != capture.as_dict()
                or str(row["canonical_metadata_json"]) != _canonical_text(capture.as_dict())
                or str(row["row_checksum"]) != capture.row_checksum
            ):
                raise OddsWeatherSelectorError("raw capture relational metadata does not reconcile")
            self.verify_raw_capture(capture, run_id=run_id, requested_date=requested_date)
            result.append(capture)
        return tuple(result)

    def _load_provider_events(self, connection: sqlite3.Connection, run_id: str, attempt: int) -> tuple[RetainedOddsProviderEventV1, ...]:
        rows = connection.execute(
            "SELECT * FROM odds_weather_provider_events WHERE run_id=? AND phase_attempt=? ORDER BY provider_event_id,revision_ordinal,retrieved_at",
            (run_id, attempt),
        ).fetchall()
        result: list[RetainedOddsProviderEventV1] = []
        for row in rows:
            event_payload = _mapping_json(row["canonical_event_json"], "provider event")
            key = (run_id, attempt, str(row["provider_event_id"]), str(row["retrieved_at"]))
            self._verify_event_children(connection, key, event_payload)
            history_rows = connection.execute(
                """SELECT * FROM odds_weather_odds_revisions
                   WHERE run_id=? AND phase_attempt=? AND provider_event_id=? AND event_retrieved_at=?
                   ORDER BY ordinal""",
                key,
            ).fetchall()
            history: list[dict[str, Any]] = []
            for ordinal, revision in enumerate(history_rows, start=1):
                payload = _mapping_json(revision["canonical_json"], "odds revision")
                expected = (
                    ordinal,
                    payload.get("bookmaker_key"),
                    payload.get("market_key"),
                    payload.get("outcome_name"),
                    payload.get("price_american", payload.get("price")),
                    payload.get("point"),
                    payload.get("provider_last_update"),
                    payload.get("bookmaker_last_update"),
                    payload.get("market_last_update"),
                    payload.get("retrieved_at"),
                    canonical_sha256(payload),
                )
                actual = (
                    int(revision["ordinal"]),
                    str(revision["bookmaker_key"]),
                    str(revision["market_key"]),
                    str(revision["outcome_name"]),
                    float(revision["price_american"]),
                    None if revision["point"] is None else float(revision["point"]),
                    None if revision["provider_last_update"] is None else str(revision["provider_last_update"]),
                    None if revision["bookmaker_last_update"] is None else str(revision["bookmaker_last_update"]),
                    None if revision["market_last_update"] is None else str(revision["market_last_update"]),
                    str(revision["retrieved_at"]),
                    str(revision["row_checksum"]),
                )
                if actual != expected or str(revision["canonical_json"]) != _canonical_text(payload):
                    raise OddsWeatherSelectorError("odds revision relational evidence does not reconcile")
                history.append(payload)
            try:
                event = OddsProviderEventV1(
                    provider_event_id=str(row["provider_event_id"]),
                    retrieved_at=_aware(row["retrieved_at"], "event retrieved_at"),
                    raw_capture_checksum=str(row["raw_capture_checksum"]),
                    event=event_payload,
                    history_rows=tuple(history),
                    contract_version=str(row["contract_version"]),
                    secret_values=self.secret_values,
                )
            except (OddsWeatherContractError, ValueError) as exc:
                raise OddsWeatherSelectorError("retained provider event violates its contract") from exc
            retained = RetainedOddsProviderEventV1(int(row["revision_ordinal"]), event)
            if (
                str(row["sport_key"]) != "baseball_mlb"
                or _aware(row["commence_time"], "commence_time") != event.commence_time
                or str(row["away_team_id"]) != event.away_team_id
                or str(row["home_team_id"]) != event.home_team_id
                or str(row["event_checksum"]) != retained.event_checksum
                or str(row["row_checksum"]) != retained.row_checksum
            ):
                raise OddsWeatherSelectorError("provider event relational evidence does not reconcile")
            result.append(retained)
        return tuple(result)

    @staticmethod
    def _verify_event_children(connection: sqlite3.Connection, key: tuple[object, ...], event_payload: Mapping[str, Any]) -> None:
        bookmakers = event_payload.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise OddsWeatherSelectorError("provider event bookmaker payload is invalid")
        rows = connection.execute(
            "SELECT * FROM odds_weather_bookmakers WHERE run_id=? AND phase_attempt=? AND provider_event_id=? AND event_retrieved_at=? ORDER BY ordinal",
            key,
        ).fetchall()
        if len(rows) != len(bookmakers):
            raise OddsWeatherSelectorError("provider event bookmaker rows are incomplete")
        for ordinal, (row, bookmaker) in enumerate(zip(rows, bookmakers, strict=True), start=1):
            if not isinstance(bookmaker, dict):
                raise OddsWeatherSelectorError("provider event bookmaker is invalid")
            expected = (ordinal, bookmaker.get("key"), bookmaker.get("title"), bookmaker.get("last_update"), _canonical_text(bookmaker), canonical_sha256(bookmaker))
            actual = (int(row["ordinal"]), str(row["bookmaker_key"]), str(row["title"]), None if row["bookmaker_last_update"] is None else str(row["bookmaker_last_update"]), str(row["canonical_json"]), str(row["row_checksum"]))
            if actual != expected:
                raise OddsWeatherSelectorError("provider event bookmaker row disagrees")
            markets = bookmaker.get("markets")
            if not isinstance(markets, list):
                raise OddsWeatherSelectorError("provider event markets are invalid")
            market_rows = connection.execute(
                "SELECT * FROM odds_weather_markets WHERE run_id=? AND phase_attempt=? AND provider_event_id=? AND event_retrieved_at=? AND bookmaker_key=? ORDER BY ordinal",
                (*key, str(bookmaker["key"])),
            ).fetchall()
            if len(market_rows) != len(markets):
                raise OddsWeatherSelectorError("provider event market rows are incomplete")
            for market_ordinal, (market_row, market) in enumerate(zip(market_rows, markets, strict=True), start=1):
                if not isinstance(market, dict):
                    raise OddsWeatherSelectorError("provider event market is invalid")
                expected_market = (market_ordinal, market.get("key"), market.get("last_update"), _canonical_text(market), canonical_sha256(market))
                actual_market = (int(market_row["ordinal"]), str(market_row["market_key"]), None if market_row["market_last_update"] is None else str(market_row["market_last_update"]), str(market_row["canonical_json"]), str(market_row["row_checksum"]))
                if actual_market != expected_market:
                    raise OddsWeatherSelectorError("provider event market row disagrees")
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    raise OddsWeatherSelectorError("provider event outcomes are invalid")
                outcome_rows = connection.execute(
                    "SELECT * FROM odds_weather_outcomes WHERE run_id=? AND phase_attempt=? AND provider_event_id=? AND event_retrieved_at=? AND bookmaker_key=? AND market_key=? ORDER BY ordinal",
                    (*key, str(bookmaker["key"]), str(market["key"])),
                ).fetchall()
                if len(outcome_rows) != len(outcomes):
                    raise OddsWeatherSelectorError("provider event outcome rows are incomplete")
                for outcome_ordinal, (outcome_row, outcome) in enumerate(zip(outcome_rows, outcomes, strict=True), start=1):
                    if not isinstance(outcome, dict):
                        raise OddsWeatherSelectorError("provider event outcome is invalid")
                    expected_outcome = (outcome_ordinal, outcome.get("name"), float(outcome["price"]), outcome.get("point"), _canonical_text(outcome), canonical_sha256(outcome))
                    actual_outcome = (int(outcome_row["ordinal"]), str(outcome_row["outcome_name"]), float(outcome_row["price_american"]), None if outcome_row["point"] is None else float(outcome_row["point"]), str(outcome_row["canonical_json"]), str(outcome_row["row_checksum"]))
                    if actual_outcome != expected_outcome:
                        raise OddsWeatherSelectorError("provider event outcome row disagrees")

    def _load_weather_revisions(self, connection: sqlite3.Connection, run_id: str, attempt: int) -> tuple[RetainedWeatherRevisionV1, ...]:
        rows = connection.execute(
            "SELECT * FROM odds_weather_weather_revisions WHERE run_id=? AND phase_attempt=? ORDER BY source_game_id,provider,revision_ordinal,retrieved_at",
            (run_id, attempt),
        ).fetchall()
        result: list[RetainedWeatherRevisionV1] = []
        for row in rows:
            payload = _mapping_json(row["canonical_json"], "weather revision")
            links = connection.execute(
                """SELECT ordinal,raw_capture_checksum FROM odds_weather_weather_raw_captures
                   WHERE run_id=? AND phase_attempt=? AND source_game_id=? AND provider=? AND weather_retrieved_at=? ORDER BY ordinal""",
                (run_id, attempt, str(row["source_game_id"]), str(row["provider"]), str(row["retrieved_at"])),
            ).fetchall()
            if tuple(int(link["ordinal"]) for link in links) != tuple(range(1, len(links) + 1)):
                raise OddsWeatherSelectorError("weather raw-capture ordinals are not contiguous")
            raw_checksums = tuple(str(link["raw_capture_checksum"]) for link in links)
            try:
                evidence = WeatherForecastEvidenceV1(
                    source_game_id=str(row["source_game_id"]),
                    provider=WeatherProvider(str(row["provider"])),
                    retrieved_at=_aware(row["retrieved_at"], "weather retrieved_at"),
                    raw_capture_checksums=raw_checksums,
                    forecast=_mapping_json(_canonical_text(payload.get("forecast", {})), "weather forecast"),
                    contract_version=str(row["contract_version"]),
                    secret_values=self.secret_values,
                )
            except (OddsWeatherContractError, ValueError) as exc:
                raise OddsWeatherSelectorError("retained weather revision violates its contract") from exc
            retained = RetainedWeatherRevisionV1(int(row["revision_ordinal"]), evidence)
            if (
                payload != evidence.as_dict()
                or str(row["canonical_json"]) != _canonical_text(evidence.as_dict())
                or _aware(row["forecast_time"], "forecast_time") != evidence.forecast_time
                or (None if row["forecast_offset_minutes"] is None else float(row["forecast_offset_minutes"])) != evidence.forecast.get("forecast_offset_minutes")
                or str(row["forecast_checksum"]) != retained.forecast_checksum
                or str(row["row_checksum"]) != retained.row_checksum
            ):
                raise OddsWeatherSelectorError("weather revision relational evidence does not reconcile")
            result.append(retained)
        return tuple(result)
