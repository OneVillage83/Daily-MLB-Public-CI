from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, settings  # noqa: E402
from app.baseball_intelligence import (  # noqa: E402
    BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION,
    BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_CONTRACT,
    BaseballIntelligencePhaseHandler,
)
from app.daily_slate.handler import DailySlatePhaseHandler  # noqa: E402
from app.game_state.handler import GameStatePhaseHandler  # noqa: E402
from app.data_quality import (  # noqa: E402
    DATA_QUALITY_ATTEMPT_MANIFEST_CONTRACT,
    DATA_QUALITY_CONTRACT_VERSION,
    DATA_QUALITY_PHASE_INPUT_CONTRACT,
    DataQualityPhaseHandler,
    DataQualityPolicyV1,
)
from app.matchup_packet import (  # noqa: E402
    MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION,
    MATCHUP_PACKET_ATTEMPT_MANIFEST_CONTRACT,
    MATCHUP_PACKET_CONTRACT_VERSION,
    MATCHUP_PACKET_PHASE_INPUT_CONTRACT,
    MatchupPacketPhaseHandler,
)
from app.model_feature_set import (  # noqa: E402
    MODEL_FEATURE_ENCODING_POLICY_VERSION,
    MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
    MODEL_FEATURE_SET_ATTEMPT_MANIFEST_CONTRACT,
    MODEL_FEATURE_SET_CONTRACT_VERSION,
    MODEL_FEATURE_SET_PHASE_INPUT_CONTRACT,
    MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
    ModelFeatureSetPhaseHandler,
)
from app.odds_weather import (  # noqa: E402
    ODDS_WEATHER_ATTEMPT_MANIFEST_CONTRACT,
    ODDS_WEATHER_CONTRACT_VERSION,
    ODDS_WEATHER_PHASE_INPUT_CONTRACT,
    WEATHER_FORECAST_CONTRACT_VERSION,
    OddsWeatherPhaseHandler,
)
from app.database import Database  # noqa: E402
from app.redaction import redact_text  # noqa: E402
from app.run_controller.contracts import PipelinePhaseKey  # noqa: E402
from app.run_controller.repository import (  # noqa: E402
    DuplicatePipelineRunError,
    PipelineRunNotFoundError,
    PipelineRunRepository,
)
from app.run_controller.service import (  # noqa: E402
    ManualRunController,
    ManualRunExecutionBlocked,
    ManualRunExecutionConflict,
    ManualRunExecutionError,
    ManualRunRecoveryRequired,
    ManualRunSummaryV1,
    PhaseHandler,
)
from app.stats.features import FEATURE_VERSION_V3  # noqa: E402


EXIT_SUCCESS = 0
EXIT_INVALID_INPUT = 2
EXIT_CONFLICT = 3
EXIT_BLOCKED = 4
EXIT_EXECUTION_FAILED = 5


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _safe_configuration_metadata(configured_settings: Settings) -> dict[str, Any]:
    return {
        "execution": {
            "mode": "manual",
            "network_enabled": True,
        },
        "daily_slate": {
            "authoritative_provider": "mlb",
            "endpoint_category": "daily_slate_schedule",
            "source_version": "statsapi-v1",
        },
        "game_state": {
            "authoritative_provider": "mlb",
            "endpoint_category": "game_state_feed",
            "source_version": "statsapi-game-feed-v1.1",
        },
        "baseball_intelligence": {
            "attempt_manifest_version": (
                BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_CONTRACT
            ),
            "contract_version": BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION,
            "feature_version": FEATURE_VERSION_V3,
            "network_enabled": False,
            "source_mode": "retained_sqlite",
        },
        "odds_weather": {
            "attempt_manifest_version": ODDS_WEATHER_ATTEMPT_MANIFEST_CONTRACT,
            "contract_version": ODDS_WEATHER_CONTRACT_VERSION,
            "input_contract_version": ODDS_WEATHER_PHASE_INPUT_CONTRACT,
            "network_enabled": True,
            "nws_mode": "primary",
            "odds_mode": "the_odds_api_mlb",
            "openweather_enabled": configured_settings.openweather_enabled,
            "openweather_mode": (
                "comparison"
                if configured_settings.openweather_enabled
                and configured_settings.weather_compare_enabled
                else "fallback"
                if configured_settings.openweather_enabled
                else "disabled"
            ),
            "source_mode": "provider_acquisition_and_retained_sqlite",
            "weather_contract_version": WEATHER_FORECAST_CONTRACT_VERSION,
        },
        "data_quality": {
            "attempt_manifest_version": DATA_QUALITY_ATTEMPT_MANIFEST_CONTRACT,
            "contract_version": DATA_QUALITY_CONTRACT_VERSION,
            "input_contract_version": DATA_QUALITY_PHASE_INPUT_CONTRACT,
            "network_enabled": False,
            "policy_version": DataQualityPolicyV1().policy_version,
            "source_mode": "retained_sqlite",
        },
        "matchup_packet": {
            "assembly_policy_version": MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION,
            "attempt_manifest_version": MATCHUP_PACKET_ATTEMPT_MANIFEST_CONTRACT,
            "contract_version": MATCHUP_PACKET_CONTRACT_VERSION,
            "input_contract_version": MATCHUP_PACKET_PHASE_INPUT_CONTRACT,
            "network_enabled": False,
            "source_mode": "retained_sqlite",
        },
        "model_feature_set": {
            "attempt_manifest_version": MODEL_FEATURE_SET_ATTEMPT_MANIFEST_CONTRACT,
            "contract_version": MODEL_FEATURE_SET_CONTRACT_VERSION,
            "encoding_policy_version": MODEL_FEATURE_ENCODING_POLICY_VERSION,
            "feature_version": FEATURE_VERSION_V3,
            "input_contract_version": MODEL_FEATURE_SET_PHASE_INPUT_CONTRACT,
            "missing_value_policy_version": MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
            "network_enabled": False,
            "source_mode": "retained_sqlite",
            "transformation_policy_version": MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
        },
        "odds": {
            "enabled": bool(configured_settings.odds_api_key),
            "format": configured_settings.odds_format,
            "markets": configured_settings.odds_markets,
            "regions": configured_settings.odds_regions,
            "retry_max_seconds": configured_settings.odds_retry_max_seconds,
        },
        "weather": {
            "nws_enabled": True,
            "openweather_api_version": configured_settings.openweather_api_version,
            "openweather_enabled": configured_settings.openweather_enabled,
            "weather_compare_enabled": configured_settings.weather_compare_enabled,
        },
        "request_timeout_seconds": configured_settings.request_timeout_seconds,
    }


def _daily_slate_user_agent(configured_settings: Settings) -> str:
    return f"Daily-MLB/{configured_settings.app_version}"


def build_controller(
    database_path: Path,
    *,
    configured_settings: Settings = settings,
    clock: Callable[[], datetime] | None = None,
    odds_weather_handler: PhaseHandler | None = None,
    data_quality_handler: PhaseHandler | None = None,
    matchup_packet_handler: PhaseHandler | None = None,
    model_feature_set_handler: PhaseHandler | None = None,
) -> ManualRunController:
    database = Database(
        database_path,
        busy_timeout_ms=configured_settings.sqlite_busy_timeout_ms,
    )
    secret_values = configured_settings.credential_values()
    repository = PipelineRunRepository(
        database,
        secret_values=secret_values,
        repository_root=ROOT,
    )
    daily_slate_handler = DailySlatePhaseHandler(
        database,
        artifact_root=configured_settings.artifact_dir,
        request_timeout_seconds=configured_settings.request_timeout_seconds,
        user_agent=_daily_slate_user_agent(configured_settings),
        secret_values=secret_values,
    )
    game_state_handler = GameStatePhaseHandler(
        database,
        artifact_root=configured_settings.artifact_dir,
        request_timeout_seconds=configured_settings.request_timeout_seconds,
        user_agent=_daily_slate_user_agent(configured_settings),
        secret_values=secret_values,
    )
    if clock is None:
        baseball_intelligence_handler = BaseballIntelligencePhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            secret_values=secret_values,
        )
    else:
        baseball_intelligence_handler = BaseballIntelligencePhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            secret_values=secret_values,
            clock=clock,
        )
    if odds_weather_handler is None:
        odds_weather_options: dict[str, Any] = {}
        if clock is not None:
            odds_weather_options["clock"] = clock
        odds_weather_handler = OddsWeatherPhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            configured_settings=configured_settings,
            secret_values=secret_values,
            **odds_weather_options,
        )
    retained_phase_options: dict[str, Any] = {}
    if clock is not None:
        retained_phase_options["clock"] = clock
    if data_quality_handler is None:
        data_quality_handler = DataQualityPhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            secret_values=secret_values,
            **retained_phase_options,
        )
    if matchup_packet_handler is None:
        matchup_packet_handler = MatchupPacketPhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            secret_values=secret_values,
            **retained_phase_options,
        )
    if model_feature_set_handler is None:
        model_feature_set_handler = ModelFeatureSetPhaseHandler(
            database,
            artifact_root=configured_settings.artifact_dir,
            secret_values=secret_values,
            **retained_phase_options,
        )
    controller_options: dict[str, Any] = {}
    if clock is not None:
        controller_options["clock"] = clock
    return ManualRunController(
        repository,
        timezone_name=configured_settings.report_timezone,
        configuration_metadata=_safe_configuration_metadata(configured_settings),
        handlers={
            PipelinePhaseKey.DAILY_SLATE: daily_slate_handler,
            PipelinePhaseKey.GAME_STATE: game_state_handler,
            PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY: (
                baseball_intelligence_handler
            ),
            PipelinePhaseKey.ODDS_WEATHER: odds_weather_handler,
            PipelinePhaseKey.DATA_QUALITY: data_quality_handler,
            PipelinePhaseKey.MATCHUP_PACKET: matchup_packet_handler,
            PipelinePhaseKey.MODEL_FEATURE_SET: model_feature_set_handler,
        },
        **controller_options,
    )


def _add_common_database_argument(
    parser: argparse.ArgumentParser,
    configured_settings: Settings,
) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=configured_settings.database_path,
        help="schema-v12 SQLite database path",
    )


def build_parser(configured_settings: Settings = settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_controller",
        description=(
            "Initialize, inspect, and manually resume Daily MLB pipeline runs; "
            "phases 1-7 can execute with immutable retained evidence"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser(
        "start",
        help="initialize a pending manual pipeline run without executing phases",
    )
    _add_common_database_argument(start, configured_settings)
    start.add_argument("--date", required=True, help="requested slate date YYYY-MM-DD")
    start.add_argument("--force-refresh", action="store_true")
    start.add_argument("--json", action="store_true", dest="json_output")

    show = commands.add_parser("show", help="show persisted pipeline run state")
    _add_common_database_argument(show, configured_settings)
    show.add_argument("--run-id", required=True)
    show.add_argument("--json", action="store_true", dest="json_output")

    resume = commands.add_parser(
        "resume",
        help=(
            "resume persisted work through MODEL_FEATURE_SET; the controller "
            "blocks safely at PREDICTIONS"
        ),
    )
    _add_common_database_argument(resume, configured_settings)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _human_summary(summary: ManualRunSummaryV1) -> str:
    payload = summary.as_dict()
    lines = [
        f"run_id: {payload['run_id']}",
        f"status: {payload['status']}",
        f"requested_date: {payload['requested_date']}",
        f"as_of_time: {payload['as_of_time']}",
        f"timezone: {payload['timezone']}",
        f"pipeline_version: {payload['pipeline_version']}",
        f"configuration_version: {payload['configuration_version']}",
        f"configuration_fingerprint: {payload['configuration_fingerprint']}",
        f"code_revision: {payload['code_revision']}",
        f"database_schema_version: {payload['database_schema_version']}",
        f"force_refresh: {str(payload['force_refresh']).lower()}",
        f"phase_status_counts: {_canonical_json(payload['phase_status_counts'])}",
        f"current_phase: {payload['current_phase'] or '-'}",
        f"next_actionable_phase: {payload['next_actionable_phase'] or '-'}",
        "phases:",
    ]
    phases = payload["phases"]
    if not isinstance(phases, list):
        raise TypeError("summary phases must be a list")
    for phase in phases:
        if not isinstance(phase, Mapping):
            raise TypeError("summary phase must be a mapping")
        lines.append(
            "  "
            f"{phase['ordinal']:02d} {phase['phase_key']} "
            f"status={phase['status']} attempts={phase['attempt_count']} "
            f"artifact={phase['artifact_relpath'] or '-'}"
        )
    return "\n".join(lines)


def _print_summary(summary: ManualRunSummaryV1, *, json_output: bool) -> None:
    print(
        _canonical_json(summary.as_dict())
        if json_output
        else _human_summary(summary),
        flush=True,
    )


def main(
    argv: list[str] | None = None,
    *,
    configured_settings: Settings = settings,
) -> int:
    args = build_parser(configured_settings).parse_args(argv)
    try:
        controller = build_controller(
            args.database,
            configured_settings=configured_settings,
        )
        if args.command == "start":
            summary = controller.start(
                args.date,
                force_refresh=args.force_refresh,
            )
        elif args.command == "show":
            summary = controller.show(args.run_id)
        elif args.command == "resume":
            summary = controller.resume(args.run_id)
        else:
            raise ValueError("unsupported run-controller command")
        _print_summary(summary, json_output=args.json_output)
        return EXIT_SUCCESS
    except DuplicatePipelineRunError as exc:
        print(
            f"CONFLICT: {redact_text(exc, configured_settings.credential_values())}",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    except (ManualRunExecutionBlocked, ManualRunRecoveryRequired) as exc:
        print(
            f"EXECUTION BLOCKED: {redact_text(exc, configured_settings.credential_values())}",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    except ManualRunExecutionError as exc:
        print(f"EXECUTION FAILED: {exc}", file=sys.stderr)
        return EXIT_EXECUTION_FAILED
    except (
        PipelineRunNotFoundError,
        ManualRunExecutionConflict,
        ValueError,
        TypeError,
    ) as exc:
        print(
            f"ERROR: {redact_text(exc, configured_settings.credential_values())}",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT
    except Exception:
        print("EXECUTION FAILED: unexpected manual run controller failure", file=sys.stderr)
        return EXIT_EXECUTION_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
