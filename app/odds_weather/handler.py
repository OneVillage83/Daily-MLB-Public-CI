"""Production Manual Run Controller handler for Odds + Weather V1."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol

from app.artifacts import ArtifactPaths
from app.baseball_intelligence.repository import PersistedBaseballIntelligenceV1
from app.collectors.nws_weather_collector import NwsWeatherCollector
from app.collectors.odds_collector import (
    COLLECTOR_VERSION,
    OddsCollectionResult,
    OddsCollector,
    OddsPayloadError,
)
from app.collectors.openweather_collector import OpenWeatherCollector
from app.config import Settings, settings
from app.daily_slate.contracts import canonical_sha256
from app.daily_slate.repository import PersistedDailySlateV1
from app.database import Database
from app.exporter import atomic_create_bytes
from app.game_state.repository import PersistedGameStateV1
from app.http import HttpClient
from app.identifiers import parse_requested_date, validate_run_id
from app.odds_weather.acquisition import (
    OddsWeatherAcquisitionPlanV1,
    WeatherAcquisitionDisposition,
    plan_odds_weather_acquisition,
)
from app.odds_weather.adapters import (
    OddsWeatherAdapterError,
    nws_forecast_to_phase4,
    odds_collection_to_phase4,
    openweather_forecast_to_phase4,
)
from app.odds_weather.assembly import OddsWeatherAssemblyError, OddsWeatherAssemblyResultV1
from app.odds_weather.attempt_manifest import OddsWeatherAttemptOutcome
from app.odds_weather.contracts import (
    ODDS_PROVIDER_EVENT_CONTRACT_VERSION,
    ODDS_WEATHER_CONTRACT_VERSION,
    WEATHER_FORECAST_CONTRACT_VERSION,
    OddsProviderEventV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    WeatherForecastEvidenceV1,
)
from app.odds_weather.repository import (
    OddsWeatherAttemptEvidenceV1,
    OddsWeatherNotFoundError,
    OddsWeatherRepository,
    PersistedOddsWeatherV1,
)
from app.odds_weather.selector import (
    OddsWeatherRawCaptureV1,
    OddsWeatherRetainedEvidenceInventoryV1,
)
from app.raw_payloads import RawPayloadCapture, sanitized_json_bytes
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.stadiums import CATALOG_VERSION, POLICY_VERSION, stadium_for_team

ODDS_WEATHER_PHASE_INPUT_CONTRACT = "DSE_ODDS_WEATHER_PHASE_INPUT_V1"
ODDS_WEATHER_ACQUISITION_POLICY_VERSION = "DSE_ODDS_WEATHER_ACQUISITION_POLICY_V1"
ODDS_WEATHER_WEATHER_POLICY_VERSION = "DSE_ODDS_WEATHER_PROVIDER_POLICY_V1"


class OddsWeatherAcquisitionError(RuntimeError):
    """Raised when required Phase 4 provider evidence cannot be acquired."""


class OddsAcquirer(Protocol):
    def __call__(self) -> OddsCollectionResult: ...


class NwsAcquirer(Protocol):
    def __call__(
        self,
        latitude: float,
        longitude: float,
        game_time: datetime,
        *,
        event_id: str | None = None,
    ) -> tuple[dict[str, Any], RawPayloadCapture, RawPayloadCapture]: ...


class OpenWeatherAcquirer(Protocol):
    def __call__(
        self,
        latitude: float,
        longitude: float,
        game_time: datetime,
        *,
        event_id: str | None = None,
    ) -> tuple[dict[str, Any], RawPayloadCapture]: ...


StadiumAuthority = Callable[[str], Mapping[str, object] | None]
AcquisitionPlanner = Callable[[object], OddsWeatherAcquisitionPlanV1]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_aware_utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    return _aware_utc(parsed, field)


@dataclass(frozen=True, slots=True)
class OddsWeatherPhasePolicyV1:
    odds_weather_contract_version: str
    odds_provider_event_contract_version: str
    weather_forecast_contract_version: str
    odds_collector_version: str
    odds_regions: tuple[str, ...]
    odds_markets: tuple[str, ...]
    odds_format: str
    weather_policy_version: str
    openweather_enabled: bool
    weather_compare_enabled: bool
    openweather_api_version: str
    stadium_metadata_policy_version: str
    stadium_catalog_version: int
    acquisition_policy_version: str = ODDS_WEATHER_ACQUISITION_POLICY_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "acquisition_policy_version": self.acquisition_policy_version,
            "odds_collector_version": self.odds_collector_version,
            "odds_format": self.odds_format,
            "odds_markets": list(self.odds_markets),
            "odds_provider_event_contract_version": (
                self.odds_provider_event_contract_version
            ),
            "odds_regions": list(self.odds_regions),
            "odds_weather_contract_version": self.odds_weather_contract_version,
            "openweather_api_version": self.openweather_api_version,
            "openweather_enabled": self.openweather_enabled,
            "stadium_catalog_version": self.stadium_catalog_version,
            "stadium_metadata_policy_version": self.stadium_metadata_policy_version,
            "weather_compare_enabled": self.weather_compare_enabled,
            "weather_forecast_contract_version": self.weather_forecast_contract_version,
            "weather_policy_version": self.weather_policy_version,
        }


def odds_weather_phase_input_checksum(
    *,
    requested_date: str,
    as_of_time: datetime,
    observed_at: datetime,
    upstream_daily_slate_checksum: str,
    upstream_game_state_checksum: str,
    upstream_baseball_intelligence_checksum: str,
    policy: OddsWeatherPhasePolicyV1,
) -> str:
    """Bind the complete nonsecret configuration and upstream Phase 4 input."""

    return canonical_sha256(
        {
            "as_of_time": _aware_utc(as_of_time, "as_of_time").isoformat(),
            "contract_version": ODDS_WEATHER_PHASE_INPUT_CONTRACT,
            "observed_at": _aware_utc(observed_at, "observed_at").isoformat(),
            "policy": policy.as_dict(),
            "requested_date": parse_requested_date(requested_date).isoformat(),
            "upstream_baseball_intelligence_checksum": (
                upstream_baseball_intelligence_checksum
            ),
            "upstream_daily_slate_checksum": upstream_daily_slate_checksum,
            "upstream_game_state_checksum": upstream_game_state_checksum,
        }
    )


class OddsWeatherPhaseHandler:
    """Acquire, assemble, persist, and reverify Phase 4 evidence."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        configured_settings: Settings = settings,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        odds_acquirer: OddsAcquirer | None = None,
        nws_acquirer: NwsAcquirer | None = None,
        openweather_acquirer: OpenWeatherAcquirer | None = None,
        repository: OddsWeatherRepository | None = None,
        stadium_authority: StadiumAuthority = stadium_for_team,
        acquisition_planner: Callable[[Any], OddsWeatherAcquisitionPlanV1] = (
            plan_odds_weather_acquisition
        ),
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.settings = configured_settings
        combined_secrets = (*configured_settings.credential_values(), *secret_values)
        self.secret_values = tuple(dict.fromkeys(str(value) for value in combined_secrets if str(value)))
        self.clock = clock
        self.repository = repository or OddsWeatherRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
        )
        http = HttpClient(
            configured_settings.request_timeout_seconds,
            max_attempts=configured_settings.odds_max_attempts,
            retry_max_seconds=configured_settings.odds_retry_max_seconds,
        )
        self.odds_acquirer: OddsAcquirer = odds_acquirer or OddsCollector(
            configured_settings,
            http,
        ).collect
        self.nws_acquirer: NwsAcquirer = nws_acquirer or NwsWeatherCollector(
            http,
            configured_settings.nws_user_agent,
        ).collect
        if openweather_acquirer is not None:
            self.openweather_acquirer: OpenWeatherAcquirer | None = openweather_acquirer
        elif configured_settings.openweather_enabled and configured_settings.openweather_api_key:
            self.openweather_acquirer = OpenWeatherCollector(
                http,
                configured_settings.openweather_api_key,
                configured_settings.openweather_api_version,
            ).collect
        else:
            self.openweather_acquirer = None
        self.stadium_authority = stadium_authority
        self.acquisition_planner = acquisition_planner
        self.policy = OddsWeatherPhasePolicyV1(
            odds_weather_contract_version=ODDS_WEATHER_CONTRACT_VERSION,
            odds_provider_event_contract_version=ODDS_PROVIDER_EVENT_CONTRACT_VERSION,
            weather_forecast_contract_version=WEATHER_FORECAST_CONTRACT_VERSION,
            odds_collector_version=COLLECTOR_VERSION,
            odds_regions=tuple(part.strip() for part in configured_settings.odds_regions.split(",")),
            odds_markets=tuple(part.strip() for part in configured_settings.odds_markets.split(",")),
            odds_format=configured_settings.odds_format,
            weather_policy_version=ODDS_WEATHER_WEATHER_POLICY_VERSION,
            openweather_enabled=configured_settings.openweather_enabled,
            weather_compare_enabled=configured_settings.weather_compare_enabled,
            openweather_api_version=configured_settings.openweather_api_version,
            stadium_metadata_policy_version=POLICY_VERSION,
            stadium_catalog_version=CATALOG_VERSION,
        )

    def _validate_context(
        self,
        context: PhaseExecutionContext,
    ) -> tuple[
        str,
        PersistedDailySlateV1,
        PersistedGameStateV1,
        PersistedBaseballIntelligenceV1,
        datetime,
    ]:
        if context.phase_key is not PipelinePhaseKey.ODDS_WEATHER:
            raise ValueError("OddsWeatherPhaseHandler may only execute ODDS_WEATHER")
        if (
            isinstance(context.attempt_number, bool)
            or not isinstance(context.attempt_number, int)
            or context.attempt_number < 1
        ):
            raise ValueError("ODDS_WEATHER attempt must be positive")
        run_id = validate_run_id(context.run_id)
        requested_date = parse_requested_date(context.requested_date).isoformat()
        context_as_of = _parse_aware_utc(context.as_of_time, "context as_of_time")
        slate, state, bia = self.repository._verify_upstream(run_id)
        if (
            slate.slate.requested_date != requested_date
            or state.state.requested_date != requested_date
            or bia.assembly.requested_date != requested_date
            or slate.slate.as_of_time != context_as_of
            or state.state.as_of_time != context_as_of
            or bia.assembly.as_of_time != context_as_of
        ):
            raise ValueError("Phase 4 context does not match the sealed upstream chain")
        with self.database.connect() as connection:
            self.repository._active_phase(
                connection,
                run_id,
                context.attempt_number,
                requested_date,
                context_as_of,
            )
        return run_id, slate, state, bia, context_as_of

    def _observation_boundary(
        self,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
    ) -> datetime:
        observed_at = _aware_utc(self.clock(), "Odds Weather observation clock")
        if observed_at < max(
            slate.slate.observed_at,
            state.state.observed_at,
            bia.assembly.observed_at,
        ):
            raise ValueError("Odds Weather observation cutoff precedes upstream evidence")
        return observed_at

    def _input_checksum(
        self,
        *,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
        observed_at: datetime,
    ) -> str:
        return odds_weather_phase_input_checksum(
            requested_date=slate.slate.requested_date,
            as_of_time=slate.slate.as_of_time,
            observed_at=observed_at,
            upstream_daily_slate_checksum=slate.slate.checksum,
            upstream_game_state_checksum=state.state.checksum,
            upstream_baseball_intelligence_checksum=bia.assembly.checksum,
            policy=self.policy,
        )

    def _warning(
        self,
        *,
        code: str,
        domain: OddsWeatherWarningDomain,
        message: str,
        source_game_id: str | None = None,
        provider: str | None = None,
    ) -> OddsWeatherWarningV1:
        safe = redact_text(message, self.secret_values).strip()
        return OddsWeatherWarningV1(
            code=code,
            domain=domain,
            message=safe or "Odds Weather provider evidence was unavailable",
            source_game_id=source_game_id,
            provider=provider,
        )

    def _publish_raw_capture(
        self,
        *,
        capture: RawPayloadCapture,
        run_id: str,
        requested_date: str,
        ordinal: int,
        source_game_id: str | None = None,
        provider_event_id: str | None = None,
    ) -> OddsWeatherRawCaptureV1:
        created = False
        path: Path | None = None
        content: bytes | None = None
        checksum: str | None = None
        try:
            content = sanitized_json_bytes(capture, secret_values=self.secret_values)
            checksum = hashlib.sha256(content).hexdigest()
            provider_timestamp = (
                None
                if capture.provider_timestamp is None
                else _parse_aware_utc(capture.provider_timestamp, "provider timestamp")
            )
            retrieved_at = _parse_aware_utc(
                capture.retrieved_at,
                "raw capture retrieved_at",
            )
            path = ArtifactPaths(
                self.artifact_root,
                parse_requested_date(requested_date),
                run_id,
            ).raw_json_path(capture.provider, capture.endpoint_category, checksum)
            relpath = path.relative_to(self.artifact_root.resolve()).as_posix()
            descriptor = OddsWeatherRawCaptureV1(
                ordinal=ordinal,
                provider=capture.provider,
                endpoint_category=capture.endpoint_category,
                source_game_id=source_game_id,
                provider_event_id=provider_event_id,
                retrieved_at=retrieved_at,
                provider_timestamp=provider_timestamp,
                raw_relpath=relpath,
                checksum=checksum,
                byte_count=len(content),
            )
            created = atomic_create_bytes(path, content)
            if not created and path.read_bytes() != content:
                raise OddsWeatherAcquisitionError(
                    "raw capture conflicts with immutable evidence"
                )
            if path.read_bytes() != content:
                raise OddsWeatherAcquisitionError(
                    "raw capture publication did not preserve exact bytes"
                )
            return descriptor
        except Exception as exc:
            if created and path is not None and content is not None and checksum is not None:
                try:
                    retained = path.read_bytes()
                    if (
                        len(retained) == len(content)
                        and hashlib.sha256(retained).hexdigest() == checksum
                        and retained == content
                    ):
                        path.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    exc.add_note(
                        "new raw-capture artifact cleanup could not be verified "
                        f"({type(cleanup_error).__name__})"
                    )
            if isinstance(exc, OddsWeatherAcquisitionError):
                raise
            raise OddsWeatherAcquisitionError(
                "raw capture could not be published as immutable evidence"
            ) from exc

    def _failed_inventory(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        observed_at: datetime,
        input_checksum: str,
        raw_captures: Iterable[OddsWeatherRawCaptureV1],
        odds_events: Iterable[OddsProviderEventV1],
        weather_evidence: Iterable[WeatherForecastEvidenceV1],
        source_warnings: Iterable[OddsWeatherWarningV1],
        failure_warning: OddsWeatherWarningV1,
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        inventory = self.repository.build_inventory(
            run_id=run_id,
            phase_attempt=phase_attempt,
            observed_at=observed_at,
            phase_input_checksum=input_checksum,
            raw_captures=raw_captures,
            odds_events=odds_events,
            weather_evidence=weather_evidence,
            source_warnings=source_warnings,
        )
        return replace(inventory, final_warnings=(failure_warning,))

    def _retain_failure_without_masking(
        self,
        *,
        original: Exception,
        outcome: OddsWeatherAttemptOutcome,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> None:
        try:
            existing = self.repository.get_attempt_evidence(
                inventory.run_id,
                inventory.phase_attempt,
            )
        except OddsWeatherNotFoundError:
            existing = None
        if existing is not None and existing.outcome is OddsWeatherAttemptOutcome.ASSEMBLED:
            return
        try:
            self.repository.persist_failed_attempt(
                run_id=inventory.run_id,
                phase_attempt=inventory.phase_attempt,
                outcome=outcome,
                inventory=inventory,
            )
        except Exception as evidence_error:
            original.add_note(
                "failed-attempt evidence could not be retained "
                f"({type(evidence_error).__name__})"
            )

    def _raise_acquisition_failure(
        self,
        *,
        original: Exception,
        run_id: str,
        phase_attempt: int,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
        raw_captures: Iterable[OddsWeatherRawCaptureV1],
        odds_events: Iterable[OddsProviderEventV1],
        weather_evidence: Iterable[WeatherForecastEvidenceV1],
        source_warnings: Iterable[OddsWeatherWarningV1],
        domain: OddsWeatherWarningDomain,
        provider: str,
        source_game_id: str | None = None,
    ) -> NoReturn:
        observed_at = self._observation_boundary(slate, state, bia)
        input_checksum = self._input_checksum(
            slate=slate,
            state=state,
            bia=bia,
            observed_at=observed_at,
        )
        warning = self._warning(
            code="odds_weather_acquisition_failed",
            domain=domain,
            message=str(original),
            source_game_id=source_game_id,
            provider=provider,
        )
        inventory = self._failed_inventory(
            run_id=run_id,
            phase_attempt=phase_attempt,
            observed_at=observed_at,
            input_checksum=input_checksum,
            raw_captures=raw_captures,
            odds_events=odds_events,
            weather_evidence=weather_evidence,
            source_warnings=source_warnings,
            failure_warning=warning,
        )
        self._retain_failure_without_masking(
            original=original,
            outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
            inventory=inventory,
        )
        raise original

    def _verify_success(
        self,
        *,
        result: OddsWeatherAssemblyResultV1,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        persisted: PersistedOddsWeatherV1,
        reconstructed: PersistedOddsWeatherV1,
        evidence: OddsWeatherAttemptEvidenceV1,
    ) -> None:
        manifest = self.repository.get_attempt_manifest(inventory.run_id, inventory.phase_attempt)
        retained = self.repository.load_retained_inventory(
            inventory.run_id,
            inventory.phase_attempt,
        )
        expected_bytes = result.snapshot.canonical_json_bytes()
        if (
            persisted.snapshot_id != f"odds-weather:{result.snapshot.checksum}"
            or reconstructed.snapshot_id != persisted.snapshot_id
            or persisted.snapshot.canonical_json_bytes() != expected_bytes
            or reconstructed.snapshot.canonical_json_bytes() != expected_bytes
            or evidence.outcome is not OddsWeatherAttemptOutcome.ASSEMBLED
            or evidence.snapshot_checksum != result.snapshot.checksum
            or evidence.retained_inventory_checksum != inventory.checksum
            or manifest.retained_inventory_checksum != inventory.checksum
            or manifest.phase_input_checksum != inventory.phase_input_checksum
            or retained != inventory
        ):
            raise RuntimeError("persisted Odds Weather evidence does not match assembly")

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        run_id, slate, state, bia, _context_as_of = self._validate_context(context)
        plan = self.acquisition_planner(slate.slate)
        raw_captures: list[OddsWeatherRawCaptureV1] = []
        odds_events: list[OddsProviderEventV1] = []
        weather_evidence: list[WeatherForecastEvidenceV1] = []
        source_warnings: list[OddsWeatherWarningV1] = []
        next_ordinal = 1

        if not slate.slate.games:
            observed_at = self._observation_boundary(slate, state, bia)
            input_checksum = self._input_checksum(
                slate=slate,
                state=state,
                bia=bia,
                observed_at=observed_at,
            )
        else:
            try:
                if any(
                    item.disposition is WeatherAcquisitionDisposition.COLLECT
                    for item in plan.weather_games
                ):
                    self.settings.validate_for_weather()
                if not self.settings.odds_api_key and isinstance(
                    getattr(self.odds_acquirer, "__self__", None),
                    OddsCollector,
                ):
                    raise OddsWeatherAcquisitionError("Odds provider credential is not configured")
                collection = self.odds_acquirer()
                raw_captures.append(
                    self._publish_raw_capture(
                        capture=collection.capture,
                        run_id=run_id,
                        requested_date=slate.slate.requested_date,
                        ordinal=next_ordinal,
                    )
                )
                next_ordinal += 1
            except OddsPayloadError as exc:
                raw_captures.append(
                    self._publish_raw_capture(
                        capture=exc.capture,
                        run_id=run_id,
                        requested_date=slate.slate.requested_date,
                        ordinal=next_ordinal,
                    )
                )
                observed_at = self._observation_boundary(slate, state, bia)
                input_checksum = self._input_checksum(
                    slate=slate,
                    state=state,
                    bia=bia,
                    observed_at=observed_at,
                )
                warning = self._warning(
                    code="odds_weather_normalization_failed",
                    domain=OddsWeatherWarningDomain.ODDS,
                    message=str(exc),
                    provider="the_odds_api",
                )
                inventory = self._failed_inventory(
                    run_id=run_id,
                    phase_attempt=context.attempt_number,
                    observed_at=observed_at,
                    input_checksum=input_checksum,
                    raw_captures=raw_captures,
                    odds_events=(),
                    weather_evidence=(),
                    source_warnings=(),
                    failure_warning=warning,
                )
                self._retain_failure_without_masking(
                    original=exc,
                    outcome=OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
                    inventory=inventory,
                )
                raise
            except Exception as exc:
                observed_at = self._observation_boundary(slate, state, bia)
                input_checksum = self._input_checksum(
                    slate=slate,
                    state=state,
                    bia=bia,
                    observed_at=observed_at,
                )
                warning = self._warning(
                    code="odds_weather_acquisition_failed",
                    domain=OddsWeatherWarningDomain.ODDS,
                    message=str(exc),
                    provider="the_odds_api",
                )
                inventory = self._failed_inventory(
                    run_id=run_id,
                    phase_attempt=context.attempt_number,
                    observed_at=observed_at,
                    input_checksum=input_checksum,
                    raw_captures=raw_captures,
                    odds_events=(),
                    weather_evidence=(),
                    source_warnings=(),
                    failure_warning=warning,
                )
                self._retain_failure_without_masking(
                    original=exc,
                    outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
                    inventory=inventory,
                )
                raise

            try:
                adapted = odds_collection_to_phase4(
                    collection,
                    secret_values=self.secret_values,
                )
                odds_events.extend(adapted.events)
                source_warnings.extend(adapted.warnings)
            except OddsWeatherAdapterError as exc:
                observed_at = self._observation_boundary(slate, state, bia)
                input_checksum = self._input_checksum(
                    slate=slate,
                    state=state,
                    bia=bia,
                    observed_at=observed_at,
                )
                warning = self._warning(
                    code="odds_weather_normalization_failed",
                    domain=OddsWeatherWarningDomain.ODDS,
                    message=str(exc),
                    provider="the_odds_api",
                )
                inventory = self._failed_inventory(
                    run_id=run_id,
                    phase_attempt=context.attempt_number,
                    observed_at=observed_at,
                    input_checksum=input_checksum,
                    raw_captures=raw_captures,
                    odds_events=(),
                    weather_evidence=(),
                    source_warnings=source_warnings,
                    failure_warning=warning,
                )
                self._retain_failure_without_masking(
                    original=exc,
                    outcome=OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
                    inventory=inventory,
                )
                raise

            game_by_id = {game.source_game_id: game for game in slate.slate.games}
            for weather_plan in plan.weather_games:
                if weather_plan.disposition is not WeatherAcquisitionDisposition.COLLECT:
                    continue
                game = game_by_id[weather_plan.source_game_id]
                stadium = self.stadium_authority(game.home_team_id)
                if stadium is None or game.scheduled_start_time is None:
                    continue
                latitude_value = stadium["latitude"]
                longitude_value = stadium["longitude"]
                if (
                    isinstance(latitude_value, bool)
                    or not isinstance(latitude_value, int | float)
                    or isinstance(longitude_value, bool)
                    or not isinstance(longitude_value, int | float)
                ):
                    raise OddsWeatherAcquisitionError(
                        "weather acquisition plan contains invalid stadium coordinates"
                    )
                latitude = float(latitude_value)
                longitude = float(longitude_value)
                nws_succeeded = False
                nws_error: Exception | None = None
                try:
                    forecast, point_capture, forecast_capture = self.nws_acquirer(
                        latitude,
                        longitude,
                        game.scheduled_start_time,
                        event_id=game.source_game_id,
                    )
                    point = self._publish_raw_capture(
                        capture=point_capture,
                        run_id=run_id,
                        requested_date=slate.slate.requested_date,
                        ordinal=next_ordinal,
                        source_game_id=game.source_game_id,
                    )
                    raw_captures.append(point)
                    next_ordinal += 1
                    hourly = self._publish_raw_capture(
                        capture=forecast_capture,
                        run_id=run_id,
                        requested_date=slate.slate.requested_date,
                        ordinal=next_ordinal,
                        source_game_id=game.source_game_id,
                    )
                    raw_captures.append(hourly)
                    next_ordinal += 1
                    weather_evidence.append(
                        nws_forecast_to_phase4(
                            source_game_id=game.source_game_id,
                            forecast=forecast,
                            point_capture=point_capture,
                            forecast_capture=forecast_capture,
                            secret_values=self.secret_values,
                        )
                    )
                    nws_succeeded = True
                except OddsWeatherAcquisitionError as exc:
                    self._raise_acquisition_failure(
                        original=exc,
                        run_id=run_id,
                        phase_attempt=context.attempt_number,
                        slate=slate,
                        state=state,
                        bia=bia,
                        raw_captures=raw_captures,
                        odds_events=odds_events,
                        weather_evidence=weather_evidence,
                        source_warnings=source_warnings,
                        domain=OddsWeatherWarningDomain.WEATHER,
                        provider="nws",
                        source_game_id=game.source_game_id,
                    )
                except OddsWeatherAdapterError as exc:
                    observed_at = self._observation_boundary(slate, state, bia)
                    input_checksum = self._input_checksum(
                        slate=slate,
                        state=state,
                        bia=bia,
                        observed_at=observed_at,
                    )
                    warning = self._warning(
                        code="odds_weather_normalization_failed",
                        domain=OddsWeatherWarningDomain.WEATHER,
                        message=str(exc),
                        source_game_id=game.source_game_id,
                        provider="nws",
                    )
                    inventory = self._failed_inventory(
                        run_id=run_id,
                        phase_attempt=context.attempt_number,
                        observed_at=observed_at,
                        input_checksum=input_checksum,
                        raw_captures=raw_captures,
                        odds_events=odds_events,
                        weather_evidence=weather_evidence,
                        source_warnings=source_warnings,
                        failure_warning=warning,
                    )
                    self._retain_failure_without_masking(
                        original=exc,
                        outcome=OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
                        inventory=inventory,
                    )
                    raise
                except Exception as exc:
                    nws_error = exc
                    source_warnings.append(
                        self._warning(
                            code="nws_acquisition_failed",
                            domain=OddsWeatherWarningDomain.WEATHER,
                            message=f"NWS acquisition failed: {exc}",
                            source_game_id=game.source_game_id,
                            provider="nws",
                        )
                    )

                collect_openweather = self.settings.openweather_enabled and (
                    self.settings.weather_compare_enabled or not nws_succeeded
                )
                if not collect_openweather:
                    if nws_error is not None:
                        self._raise_acquisition_failure(
                            original=nws_error,
                            run_id=run_id,
                            phase_attempt=context.attempt_number,
                            slate=slate,
                            state=state,
                            bia=bia,
                            raw_captures=raw_captures,
                            odds_events=odds_events,
                            weather_evidence=weather_evidence,
                            source_warnings=source_warnings,
                            domain=OddsWeatherWarningDomain.WEATHER,
                            provider="nws",
                            source_game_id=game.source_game_id,
                        )
                    continue
                if self.openweather_acquirer is None:
                    source_warnings.append(
                        self._warning(
                            code="openweather_acquisition_failed",
                            domain=OddsWeatherWarningDomain.WEATHER,
                            message="OpenWeather fallback is enabled but unavailable",
                            source_game_id=game.source_game_id,
                            provider="openweather",
                        )
                    )
                    if nws_error is not None:
                        self._raise_acquisition_failure(
                            original=nws_error,
                            run_id=run_id,
                            phase_attempt=context.attempt_number,
                            slate=slate,
                            state=state,
                            bia=bia,
                            raw_captures=raw_captures,
                            odds_events=odds_events,
                            weather_evidence=weather_evidence,
                            source_warnings=source_warnings,
                            domain=OddsWeatherWarningDomain.WEATHER,
                            provider="nws",
                            source_game_id=game.source_game_id,
                        )
                    continue
                openweather_succeeded = False
                try:
                    forecast, capture = self.openweather_acquirer(
                        latitude,
                        longitude,
                        game.scheduled_start_time,
                        event_id=game.source_game_id,
                    )
                    retained = self._publish_raw_capture(
                        capture=capture,
                        run_id=run_id,
                        requested_date=slate.slate.requested_date,
                        ordinal=next_ordinal,
                        source_game_id=game.source_game_id,
                    )
                    next_ordinal += 1
                    raw_captures.append(retained)
                    weather_evidence.append(
                        openweather_forecast_to_phase4(
                            source_game_id=game.source_game_id,
                            forecast=forecast,
                            capture=capture,
                            secret_values=self.secret_values,
                        )
                    )
                    openweather_succeeded = True
                except OddsWeatherAcquisitionError as exc:
                    self._raise_acquisition_failure(
                        original=exc,
                        run_id=run_id,
                        phase_attempt=context.attempt_number,
                        slate=slate,
                        state=state,
                        bia=bia,
                        raw_captures=raw_captures,
                        odds_events=odds_events,
                        weather_evidence=weather_evidence,
                        source_warnings=source_warnings,
                        domain=OddsWeatherWarningDomain.WEATHER,
                        provider="openweather",
                        source_game_id=game.source_game_id,
                    )
                except OddsWeatherAdapterError as exc:
                    observed_at = self._observation_boundary(slate, state, bia)
                    input_checksum = self._input_checksum(
                        slate=slate,
                        state=state,
                        bia=bia,
                        observed_at=observed_at,
                    )
                    warning = self._warning(
                        code="odds_weather_normalization_failed",
                        domain=OddsWeatherWarningDomain.WEATHER,
                        message=str(exc),
                        source_game_id=game.source_game_id,
                        provider="openweather",
                    )
                    inventory = self._failed_inventory(
                        run_id=run_id,
                        phase_attempt=context.attempt_number,
                        observed_at=observed_at,
                        input_checksum=input_checksum,
                        raw_captures=raw_captures,
                        odds_events=odds_events,
                        weather_evidence=weather_evidence,
                        source_warnings=source_warnings,
                        failure_warning=warning,
                    )
                    self._retain_failure_without_masking(
                        original=exc,
                        outcome=OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
                        inventory=inventory,
                    )
                    raise
                except Exception as exc:
                    source_warnings.append(
                        self._warning(
                            code="openweather_acquisition_failed",
                            domain=OddsWeatherWarningDomain.WEATHER,
                            message=f"OpenWeather acquisition failed: {exc}",
                            source_game_id=game.source_game_id,
                            provider="openweather",
                        )
                    )
                if nws_error is not None and not openweather_succeeded:
                    self._raise_acquisition_failure(
                        original=nws_error,
                        run_id=run_id,
                        phase_attempt=context.attempt_number,
                        slate=slate,
                        state=state,
                        bia=bia,
                        raw_captures=raw_captures,
                        odds_events=odds_events,
                        weather_evidence=weather_evidence,
                        source_warnings=source_warnings,
                        domain=OddsWeatherWarningDomain.WEATHER,
                        provider="nws",
                        source_game_id=game.source_game_id,
                    )

            observed_at = self._observation_boundary(slate, state, bia)
            input_checksum = self._input_checksum(
                slate=slate,
                state=state,
                bia=bia,
                observed_at=observed_at,
            )

        inventory = self.repository.build_inventory(
            run_id=run_id,
            phase_attempt=context.attempt_number,
            observed_at=observed_at,
            phase_input_checksum=input_checksum,
            raw_captures=raw_captures,
            odds_events=odds_events,
            weather_evidence=weather_evidence,
            source_warnings=source_warnings,
        )
        try:
            result, completed_inventory = self.repository.assemble_inventory(inventory)
        except OddsWeatherAssemblyError as exc:
            failed = replace(
                inventory,
                final_warnings=(
                    self._warning(
                        code="odds_weather_assembly_failed",
                        domain=OddsWeatherWarningDomain.ODDS,
                        message=str(exc),
                    ),
                ),
            )
            self._retain_failure_without_masking(
                original=exc,
                outcome=OddsWeatherAttemptOutcome.ASSEMBLY_FAILED,
                inventory=failed,
            )
            raise

        try:
            persisted = self.repository.persist_assembly(
                run_id=run_id,
                phase_attempt=context.attempt_number,
                result=result,
                inventory=completed_inventory,
            )
            reconstructed = self.repository.get_for_run_attempt(
                run_id,
                context.attempt_number,
            )
            evidence = self.repository.get_attempt_evidence(
                run_id,
                context.attempt_number,
            )
            self._verify_success(
                result=result,
                inventory=completed_inventory,
                persisted=persisted,
                reconstructed=reconstructed,
                evidence=evidence,
            )
        except Exception as exc:
            failed = replace(
                inventory,
                final_warnings=(
                    self._warning(
                        code="odds_weather_assembly_failed",
                        domain=OddsWeatherWarningDomain.ODDS,
                        message=str(exc),
                    ),
                ),
            )
            self._retain_failure_without_masking(
                original=exc,
                outcome=OddsWeatherAttemptOutcome.ASSEMBLY_FAILED,
                inventory=failed,
            )
            raise

        warnings = tuple(warning.as_dict() for warning in result.snapshot.warnings)
        return PhaseExecutionResult(
            status=(
                PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
                if warnings
                else PipelinePhaseStatus.SUCCEEDED
            ),
            input_checksum=input_checksum,
            output_checksum=result.snapshot.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )
