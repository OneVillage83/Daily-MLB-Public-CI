from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.predictions.production import ReviewedPredictionInputV1  # noqa: E402
from app.predictions.repository import PredictionsRepository  # noqa: E402


def _repository(database: Path) -> PredictionsRepository:
    return PredictionsRepository(
        Database(database),
        artifact_root=settings.artifact_dir,
        secret_values=settings.credential_values(),
    )


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _scheduled_start(value: datetime | None) -> str:
    if value is None:
        raise ValueError("prediction authoring requires a scheduled start time")
    return value.isoformat()


def _templates(repository: PredictionsRepository, run_id: str) -> list[dict[str, object]]:
    upstream = repository.resolve_upstream(run_id)
    packet = {game.source_game_id: game for game in upstream.matchup_packet.packet.games}
    return [
        {
            "authoring_evidence": {
                "method_note": "Complete using market-blind reviewed baseball evidence only",
                "quality_disposition": game.quality_disposition.value,
                "quality_issue_codes": list(game.quality_issue_codes),
            },
            "away_team_id": game.away_team_id,
            "generated_at": None,
            "home_lower": None,
            "home_probability": None,
            "home_team_id": game.home_team_id,
            "home_upper": None,
            "market_context_exposed": False,
            "market_independence_attested": False,
            "predictive_feature_checksum": game.predictive_feature_checksum,
            "predictive_features": game.feature_map(),
            "provider_policy": repository.provider_policy.as_dict(),
            "run_id": run_id,
            "sealed_at": None,
            "source_game_id": game.source_game_id,
            "upstream_model_feature_game_checksum": game.checksum,
            "upstream_model_feature_set_checksum": upstream.model_feature_set.feature_set.checksum,
            "upstream_model_feature_set_snapshot_id": upstream.model_feature_set.snapshot_id,
            "scheduled_start_time": _scheduled_start(packet[game.source_game_id].schedule.scheduled_start_time),
        }
        for game in upstream.model_feature_set.feature_set.games
    ]


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("prediction input must be a JSON object")
    return value


def _value(payload: dict[str, object], repository: PredictionsRepository) -> ReviewedPredictionInputV1:
    raw_evidence = payload.get("authoring_evidence", {})
    evidence: dict[str, object] = dict(raw_evidence) if isinstance(raw_evidence, dict) else {}
    if payload.get("provider_policy") != repository.provider_policy.as_dict():
        raise ValueError("prediction input provider policy does not match the active reviewed policy")
    return ReviewedPredictionInputV1(
        run_id=str(payload["run_id"]),
        source_game_id=str(payload["source_game_id"]),
        upstream_model_feature_set_snapshot_id=str(payload["upstream_model_feature_set_snapshot_id"]),
        upstream_model_feature_set_checksum=str(payload["upstream_model_feature_set_checksum"]),
        upstream_model_feature_game_checksum=str(payload["upstream_model_feature_game_checksum"]),
        predictive_feature_checksum=str(payload["predictive_feature_checksum"]),
        provider_policy=repository.provider_policy,
        home_probability=_number(payload["home_probability"], "home_probability"),
        home_lower=_number(payload["home_lower"], "home_lower"),
        home_upper=_number(payload["home_upper"], "home_upper"),
        generated_at=datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00")),
        sealed_at=datetime.fromisoformat(str(payload["sealed_at"]).replace("Z", "+00:00")),
        authoring_evidence=evidence,
        market_independence_attested=cast(bool, payload.get("market_independence_attested")),
        secret_values=repository.secret_values,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and seal market-blind reviewed MLB prediction inputs")
    parser.add_argument("--database", type=Path, default=settings.database_path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("template-run", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
    one = commands.add_parser("template-game")
    one.add_argument("--run-id", required=True)
    one.add_argument("--source-game-id", required=True)
    for name in ("validate", "seal"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = _repository(args.database)
    if args.command == "template-run":
        payload: object = _templates(repository, args.run_id)
    elif args.command == "template-game":
        payload = next(
            value for value in _templates(repository, args.run_id) if value["source_game_id"] == args.source_game_id
        )
    elif args.command == "inspect":
        upstream = repository.resolve_upstream(args.run_id)
        values, missing = repository.load_input_inventory(upstream)
        payload = {"missing_game_ids": list(missing), "sealed_inputs": [value.as_dict() for value in values]}
    else:
        value = _value(_load(args.input), repository)
        payload = value.as_dict() if args.command == "validate" else repository.seal_authoring_input(value).as_dict()
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
