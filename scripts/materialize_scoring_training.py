from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.identifiers import parse_requested_date  # noqa: E402
from app.model_feature_set.repository import ModelFeatureSetRepository  # noqa: E402
from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1  # noqa: E402
from app.predictions.historical_materialization import (  # noqa: E402
    HistoricalScoringMaterializationArtifactV1,
    HistoricalScoringMaterializationError,
    build_historical_scoring_materialization,
    default_historical_scoring_materialization_path,
    write_historical_scoring_materialization_artifact,
)
from app.predictions.historical_sources import HistoricalTrainingSourceError  # noqa: E402
from app.predictions.training_data import ScoringTrainingMaterializationError  # noqa: E402
from app.redaction import redact_text  # noqa: E402

EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_USAGE = 64


class CliUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def build_parser(
    configured_settings: Settings = settings,
) -> argparse.ArgumentParser:
    parser = _Parser(
        prog="materialize_scoring_training",
        description=(
            "Build a deterministic historical MLB scoring-training artifact from "
            "strictly pregame ModelFeatureSet evidence and validated final scores"
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=configured_settings.database_path,
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=configured_settings.artifact_dir,
        help="pipeline artifact root containing retained ModelFeatureSet artifacts",
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--feature-name",
        action="append",
        dest="feature_names",
        help=(
            "repeat to materialize a subset; omitted means the complete "
            "ModelFeatureSet V1 schema"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "optional immutable output path; default is under "
            "ARTIFACT_DIR/training/scoring"
        ),
    )
    return parser


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(_canonical_json(payload) + "\n")
    stream.flush()


def _selected_features(args: argparse.Namespace) -> tuple[str, ...]:
    if args.feature_names is None:
        return MODEL_FEATURE_NAMES_V1
    feature_names = tuple(str(value).strip() for value in args.feature_names)
    if any(not value for value in feature_names):
        raise CliUsageError("--feature-name values must not be empty")
    if len(feature_names) != len(set(feature_names)):
        raise CliUsageError("--feature-name values must not contain duplicates")
    unsupported = sorted(set(feature_names) - set(MODEL_FEATURE_NAMES_V1))
    if unsupported:
        raise CliUsageError(
            "unsupported --feature-name value(s): " + ", ".join(unsupported)
        )
    return tuple(sorted(feature_names))


def _compact_summary(
    artifact: HistoricalScoringMaterializationArtifactV1,
) -> dict[str, object]:
    payload = artifact.summary_as_dict()
    raw_coverage = payload.get("coverage")
    if not isinstance(raw_coverage, dict):
        raise HistoricalScoringMaterializationError(
            "historical materialization coverage summary is malformed"
        )
    coverage = dict(raw_coverage)
    feature_coverage = coverage.pop("feature_coverage", None)
    if not isinstance(feature_coverage, list):
        raise HistoricalScoringMaterializationError(
            "historical materialization feature coverage is malformed"
        )
    observed_counts: list[int] = []
    missing_counts: list[int] = []
    distinct_counts: list[int] = []
    for item in feature_coverage:
        if not isinstance(item, dict):
            raise HistoricalScoringMaterializationError(
                "historical feature coverage row is malformed"
            )
        observed_counts.append(int(item["observed_game_count"]))
        missing_counts.append(int(item["missing_game_count"]))
        distinct_counts.append(int(item["distinct_observed_value_count"]))
    coverage.update(
        {
            "fully_observed_feature_count": sum(
                value == 0 for value in missing_counts
            ),
            "never_observed_feature_count": sum(
                value == 0 for value in observed_counts
            ),
            "variable_observed_feature_count": sum(
                value >= 2 for value in distinct_counts
            ),
            "constant_observed_feature_count": sum(
                observed > 0 and distinct <= 1
                for observed, distinct in zip(
                    observed_counts,
                    distinct_counts,
                    strict=True,
                )
            ),
        }
    )
    payload["coverage"] = coverage
    return payload


def _run(
    args: argparse.Namespace,
    *,
    configured_settings: Settings,
) -> tuple[int, dict[str, object]]:
    start_date = parse_requested_date(args.start_date)
    end_date = parse_requested_date(args.end_date)
    if end_date < start_date:
        raise CliUsageError("--end-date must not precede --start-date")
    feature_names = _selected_features(args)

    database = Database(args.database)
    repository = ModelFeatureSetRepository(
        database,
        artifact_root=args.artifact_root,
        secret_values=configured_settings.credential_values(),
    )
    artifact = build_historical_scoring_materialization(
        database,
        repository,
        start_date=start_date,
        end_date=end_date,
        feature_names=feature_names,
    )
    output = (
        args.output
        if args.output is not None
        else default_historical_scoring_materialization_path(
            args.artifact_root,
            artifact,
        )
    )
    written = write_historical_scoring_materialization_artifact(
        artifact,
        output,
    )
    payload = _compact_summary(artifact)
    payload.update(
        {
            "output_path": str(written),
            "status": "materialized",
        }
    )
    return EXIT_SUCCESS, payload


def main(
    argv: list[str] | None = None,
    *,
    configured_settings: Settings = settings,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        args = build_parser(configured_settings).parse_args(argv)
        exit_code, payload = _run(
            args,
            configured_settings=configured_settings,
        )
        _emit(stdout, payload)
        return exit_code
    except (
        HistoricalTrainingSourceError,
        ScoringTrainingMaterializationError,
        HistoricalScoringMaterializationError,
    ) as exc:
        payload = {
            "status": "failed",
            "exit_code": EXIT_OPERATIONAL_FAILURE,
            "error_type": type(exc).__name__,
            "message": redact_text(
                str(exc),
                configured_settings.credential_values(),
            ),
        }
        _emit(stderr, payload)
        return EXIT_OPERATIONAL_FAILURE
    except (CliUsageError, ValueError, TypeError) as exc:
        payload = {
            "status": "usage_error",
            "exit_code": EXIT_USAGE,
            "error_type": type(exc).__name__,
            "message": redact_text(
                str(exc),
                configured_settings.credential_values(),
            ),
        }
        _emit(stderr, payload)
        return EXIT_USAGE
    except Exception:
        payload = {
            "status": "failed",
            "exit_code": EXIT_OPERATIONAL_FAILURE,
            "error_type": "UnexpectedMaterializationFailure",
            "message": "unexpected historical scoring materialization failure",
        }
        _emit(stderr, payload)
        return EXIT_OPERATIONAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
