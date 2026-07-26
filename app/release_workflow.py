from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.analysis import (
    CANDIDATE_POLICY_VERSION,
    REVIEWED_ANALYST_VERSION,
    AnalystEvidenceBundle,
    CandidateDecision,
    CanonicalGame,
    DataQualityState,
    EvidenceAssessment,
    EvidenceState,
    LineupInformationState,
    MlbFeatures,
    ReviewedPredictionInput,
    SealedPrediction,
    SourceReference,
    assemble_canonical_game,
    assemble_features,
    evaluate_moneyline_candidate,
    seal_prediction,
)
from app.analysis.models import QualityIssue, checksum_payload, utc_datetime
from app.artifacts import (
    ANALYSIS_FEATURES_FILENAME,
    CANDIDATE_POLICY_RESULTS_FILENAME,
    CANONICAL_GAMES_FILENAME,
    DAILY_CARD_JSON_FILENAME,
    DAILY_CARD_MARKDOWN_FILENAME,
    ODDS_CONSENSUS_FILENAME,
    PREDICTION_EVALUATIONS_FILENAME,
    PUBLICATION_MANIFEST_FILENAME,
    RESULTS_LEDGER_FILENAME,
    SEALED_PREDICTIONS_FILENAME,
    WEATHER_FILENAME,
    ArtifactPaths,
)
from app.config import Settings
from app.database import Database
from app.exporter import create_zip, write_json, write_text
from app.identifiers import parse_requested_date, validate_run_id
from app.publication import (
    approve_daily_card,
    build_publication_manifest,
    create_daily_card_draft,
    payload_checksum,
    render_analysis_features,
    render_candidate_policy_results,
    render_canonical_games,
    render_daily_card,
    render_daily_card_markdown,
    render_prediction_evaluations,
    render_results_ledger,
    render_sealed_predictions,
)
from app.redaction import redact_value
from app.stadiums import stadium_for_team


class ReleaseWorkflowError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseWorkflowError(f"Unable to read required artifact {path.name}") from exc


def _records(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        payload = payload["records"]
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ReleaseWorkflowError(f"Artifact {path.name} must contain a record list")
    return payload


def _write_records(paths: ArtifactPaths, filename: str, payload: Mapping[str, Any]) -> None:
    write_json(paths.json_path(filename), payload)


def _quality_issue(value: Mapping[str, Any]) -> QualityIssue:
    return QualityIssue(
        code=str(value["code"]),
        message=str(value["message"]),
        blocking=bool(value["blocking"]),
    )


def _canonical_from_mapping(value: Mapping[str, Any]) -> CanonicalGame:
    return CanonicalGame(
        contract_version=str(value["contract_version"]),
        run_id=str(value["run_id"]),
        event_id=str(value["event_id"]),
        requested_date=parse_requested_date(str(value["requested_date"])),
        commence_time=utc_datetime(value["commence_time"], field="commence_time"),
        raw_home_team=str(value["raw_home_team"]),
        raw_away_team=str(value["raw_away_team"]),
        home_team_key=str(value["home_team_key"]),
        away_team_key=str(value["away_team_key"]),
        venue_context=dict(value.get("venue_context", {})),
        roof_context=str(value["roof_context"]),
        odds_consensus=dict(value.get("odds_consensus", {})),
        weather_context=dict(value.get("weather_context", {})),
        weather_gate_clear=bool(value["weather_gate_clear"]),
        quality_state=DataQualityState(str(value["quality_state"])),
        quality_issues=tuple(
            _quality_issue(item)
            for item in value.get("quality_issues", [])
            if isinstance(item, Mapping)
        ),
        source_checksums=dict(value.get("source_checksums", {})),
        assembled_at=utc_datetime(value["assembled_at"], field="assembled_at"),
        checksum=str(value["checksum"]),
    )


def _feature_from_mapping(value: Mapping[str, Any]) -> MlbFeatures:
    return MlbFeatures(
        contract_version=str(value["contract_version"]),
        event_id=str(value["event_id"]),
        canonical_game_checksum=str(value["canonical_game_checksum"]),
        schedule_context=dict(value.get("schedule_context", {})),
        team_context=dict(value.get("team_context", {})),
        venue_context=dict(value.get("venue_context", {})),
        weather_context=dict(value.get("weather_context", {})),
        market_context=dict(value.get("market_context", {})),
        data_quality_state=DataQualityState(str(value["data_quality_state"])),
        uncertainty_flags=tuple(str(item) for item in value.get("uncertainty_flags", [])),
        generated_at=utc_datetime(value["generated_at"], field="generated_at"),
        checksum=str(value["checksum"]),
    )


def _assessment(value: Mapping[str, Any]) -> EvidenceAssessment:
    return EvidenceAssessment(
        state=EvidenceState(str(value["state"])),
        assessment=(str(value["assessment"]) if value.get("assessment") is not None else None),
        source_ids=tuple(str(item) for item in value.get("source_ids", [])),
    )


def _evidence(value: Mapping[str, Any]) -> AnalystEvidenceBundle:
    return AnalystEvidenceBundle(
        analyst_identity=str(value["analyst_identity"]),
        method_version=str(value["method_version"]),
        starting_pitching=_assessment(value["starting_pitching"]),
        bullpen=_assessment(value["bullpen"]),
        offensive_matchup=_assessment(value["offensive_matchup"]),
        lineup_information_state=LineupInformationState(str(value["lineup_information_state"])),
        lineup_notes=(str(value["lineup_notes"]) if value.get("lineup_notes") is not None else None),
        lineup_source_ids=tuple(str(item) for item in value.get("lineup_source_ids", [])),
        venue_context=_assessment(value["venue_context"]),
        weather_context=_assessment(value["weather_context"]),
        schedule_rest_context=_assessment(value["schedule_rest_context"]),
        material_unknowns=tuple(str(item) for item in value.get("material_unknowns", [])),
        source_provenance=tuple(
            SourceReference(
                source_id=str(item["source_id"]),
                source_name=str(item["source_name"]),
                reference=str(item["reference"]),
                observed_at=utc_datetime(item["observed_at"], field="source observed_at"),
            )
            for item in value.get("source_provenance", [])
        ),
    )


def _sealed_from_mapping(value: Mapping[str, Any]) -> SealedPrediction:
    return SealedPrediction(
        contract_version=str(value["contract_version"]),
        prediction_id=str(value["prediction_id"]),
        run_id=str(value["run_id"]),
        event_id=str(value["event_id"]),
        home_team_key=str(value["home_team_key"]),
        away_team_key=str(value["away_team_key"]),
        home_probability=float(value["home_probability"]),
        away_probability=float(value["away_probability"]),
        home_probability_lower=float(value.get("home_probability_lower", value["lower_bound"])),
        home_probability_upper=float(value.get("home_probability_upper", value["upper_bound"])),
        away_probability_lower=float(value["away_probability_lower"]),
        away_probability_upper=float(value["away_probability_upper"]),
        generated_at=utc_datetime(value["generated_at"], field="generated_at"),
        sealed_at=utc_datetime(value["sealed_at"], field="sealed_at"),
        feature_checksum=str(value["feature_checksum"]),
        market_independence_attested=bool(value["market_independence_attested"]),
        evidence=_evidence(value["evidence"]),
        evidence_checksum=str(value["evidence_checksum"]),
        checksum=str(value.get("checksum", value.get("prediction_checksum"))),
    )


class ReleaseWorkflow:
    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.database = database or Database(
            settings.database_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms
        )

    def _paths(self, run_id: str) -> ArtifactPaths:
        row = self.database.get_run(validate_run_id(run_id))
        if row is None:
            raise ReleaseWorkflowError("Collection run does not exist")
        return ArtifactPaths(
            self.settings.artifact_dir,
            parse_requested_date(str(row["requested_date"])),
            run_id,
        )

    def assemble_run(self, run_id: str, *, assembled_at: datetime | None = None) -> tuple[list[CanonicalGame], list[MlbFeatures]]:
        paths = self._paths(run_id)
        timestamp = assembled_at or _now()
        odds_records = _records(paths.json_path(ODDS_CONSENSUS_FILENAME))
        weather_records = _records(paths.json_path(WEATHER_FILENAME))
        weather_by_event = {
            str(item.get("event_id")): item for item in weather_records if item.get("event_id")
        }
        persisted_games = {
            str(item["event_id"]): item for item in self.database.list_run_games(run_id)
        }
        artifact_event_ids = {str(item.get("event_id") or "") for item in odds_records}
        if artifact_event_ids != set(persisted_games):
            raise ReleaseWorkflowError(
                "Odds artifact and persisted run-game associations do not reconcile"
            )
        canonical: list[CanonicalGame] = []
        features: list[MlbFeatures] = []
        for odds in odds_records:
            event_id = str(odds.get("event_id") or "")
            persisted = persisted_games[event_id]
            persisted_time = utc_datetime(
                persisted["commence_time"], field="persisted commence_time"
            )
            artifact_commence = odds.get("commence_time")
            if not isinstance(artifact_commence, (str, datetime)):
                raise ReleaseWorkflowError("Odds artifact game time is missing")
            artifact_time = utc_datetime(
                artifact_commence, field="artifact commence_time"
            )
            identities = (
                (str(odds.get("raw_home_team") or odds.get("home_team") or ""), str(persisted["home_team"])),
                (str(odds.get("raw_away_team") or odds.get("away_team") or ""), str(persisted["away_team"])),
                (str(odds.get("home_team_key") or ""), str(persisted["home_team_key"] or "")),
                (str(odds.get("away_team_key") or ""), str(persisted["away_team_key"] or "")),
            )
            if artifact_time != persisted_time or any(left != right for left, right in identities):
                raise ReleaseWorkflowError(
                    "Odds artifact game identity does not match persisted collection evidence"
                )
            game = assemble_canonical_game(
                run_id=run_id,
                requested_date=paths.requested_date,
                odds_summary=odds,
                weather_packet=weather_by_event.get(event_id),
                venue=stadium_for_team(str(odds.get("home_team_key") or "")),
                assembled_at=timestamp,
            )
            canonical.append(game)
            features.append(assemble_features(game, generated_at=timestamp))
        _write_records(paths, CANONICAL_GAMES_FILENAME, render_canonical_games(canonical, generated_at=timestamp))
        _write_records(paths, ANALYSIS_FEATURES_FILENAME, render_analysis_features(features, generated_at=timestamp))
        create_zip(paths.run_dir)
        return canonical, features

    def prediction_template(self, run_id: str, event_id: str) -> dict[str, Any]:
        paths = self._paths(run_id)
        feature_rows = _records(paths.json_path(ANALYSIS_FEATURES_FILENAME))
        feature = next((_feature_from_mapping(item) for item in feature_rows if item.get("event_id") == event_id), None)
        if feature is None:
            raise ReleaseWorkflowError("Feature record was not found")
        context = feature.prediction_input_view()
        teams = context["team_context"]
        unknown: dict[str, Any] = {
            "state": "unknown",
            "assessment": None,
            "source_ids": [],
        }
        return {
            "contract_version": REVIEWED_ANALYST_VERSION,
            "market_blind_context": context,
            "prediction": {
                "run_id": run_id,
                "event_id": event_id,
                "home_team_key": teams["home_team_key"],
                "away_team_key": teams["away_team_key"],
                "feature_checksum": feature.checksum,
                "home_probability": None,
                "home_probability_lower": None,
                "home_probability_upper": None,
                "generated_at": None,
                "market_independence_attested": False,
                "evidence": {
                    "analyst_identity": "",
                    "method_version": REVIEWED_ANALYST_VERSION,
                    "starting_pitching": dict(unknown),
                    "bullpen": dict(unknown),
                    "offensive_matchup": dict(unknown),
                    "lineup_information_state": "unknown",
                    "lineup_notes": None,
                    "lineup_source_ids": [],
                    "venue_context": dict(unknown),
                    "weather_context": dict(unknown),
                    "schedule_rest_context": dict(unknown),
                    "material_unknowns": [],
                    "source_provenance": [],
                },
            },
        }

    def seal_reviewed_prediction(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        sealed_at: datetime | None = None,
    ) -> SealedPrediction:
        paths = self._paths(run_id)
        raw = payload.get("prediction", payload)
        if not isinstance(raw, Mapping) or str(raw.get("run_id")) != run_id:
            raise ReleaseWorkflowError("Prediction input does not match the collection run")
        features = _records(paths.json_path(ANALYSIS_FEATURES_FILENAME))
        feature = next((item for item in features if item.get("event_id") == raw.get("event_id")), None)
        if feature is None or feature.get("checksum") != raw.get("feature_checksum"):
            raise ReleaseWorkflowError("Prediction input is not bound to the current feature record")
        evidence = _evidence(raw["evidence"])
        prediction = seal_prediction(
            ReviewedPredictionInput(
                run_id=run_id,
                event_id=str(raw["event_id"]),
                home_team_key=str(raw["home_team_key"]),
                away_team_key=str(raw["away_team_key"]),
                home_probability=float(raw["home_probability"]),
                home_probability_lower=float(raw["home_probability_lower"]),
                home_probability_upper=float(raw["home_probability_upper"]),
                generated_at=utc_datetime(raw["generated_at"], field="generated_at"),
                feature_checksum=str(raw["feature_checksum"]),
                market_independence_attested=bool(raw["market_independence_attested"]),
                evidence=evidence,
            ),
            sealed_at=sealed_at or _now(),
        )
        prediction_payload = prediction.as_dict()
        safe = redact_value(
            prediction_payload, self.settings.credential_values()
        )
        if safe != prediction_payload:
            raise ReleaseWorkflowError(
                "Prediction evidence contains credential-bearing material"
            )
        evidence_id = f"evidence_{prediction.evidence_checksum[:24]}"
        self.database.insert_sealed_prediction(
            evidence={
                "evidence_id": evidence_id,
                "run_id": run_id,
                "event_id": prediction.event_id,
                "contract_version": prediction.contract_version,
                "analyst_id": evidence.analyst_identity,
                "method_version": evidence.method_version,
                "evidence": safe["evidence"],
                "evidence_checksum": prediction.evidence_checksum,
                "source_checksum": prediction.evidence_checksum,
                "created_at": prediction.sealed_at.isoformat(),
            },
            prediction={
                **safe,
                "evidence_id": evidence_id,
                "run_id": run_id,
                "analyst_id": evidence.analyst_identity,
                "method_version": evidence.method_version,
                "lower_bound": prediction.home_probability_lower,
                "upper_bound": prediction.home_probability_upper,
                "prediction_checksum": prediction.checksum,
                "payload": safe,
            },
        )
        existing = []
        path = paths.json_path(SEALED_PREDICTIONS_FILENAME)
        if path.exists():
            existing = _records(path)
        if any(item.get("prediction_id") == prediction.prediction_id for item in existing):
            raise ReleaseWorkflowError("Sealed prediction already exists")
        _write_records(paths, SEALED_PREDICTIONS_FILENAME, render_sealed_predictions([*existing, safe]))
        create_zip(paths.run_dir)
        return prediction

    def evaluate_prediction(
        self,
        run_id: str,
        prediction_id: str,
        *,
        evaluated_at: datetime | None = None,
    ) -> dict[str, Any]:
        paths = self._paths(run_id)
        canonical_rows = _records(paths.json_path(CANONICAL_GAMES_FILENAME))
        prediction_rows = _records(paths.json_path(SEALED_PREDICTIONS_FILENAME))
        prediction_row = next(
            (item for item in prediction_rows if item.get("prediction_id") == prediction_id),
            None,
        )
        if prediction_row is None:
            raise ReleaseWorkflowError("Sealed prediction was not found")
        prediction = _sealed_from_mapping(prediction_row)
        game_row = next(
            (item for item in canonical_rows if item.get("event_id") == prediction.event_id),
            None,
        )
        if game_row is None:
            raise ReleaseWorkflowError("Canonical game was not found")
        game = _canonical_from_mapping(game_row)
        evaluation = evaluate_moneyline_candidate(
            game,
            prediction,
            evaluated_at=evaluated_at or _now(),
            phase2_live_weather_accepted=self.settings.phase2_weather_gate_clear(),
        )
        evaluation_payload = redact_value(
            evaluation.as_dict(), self.settings.credential_values()
        )
        market_id = f"market_{evaluation.checksum[:24]}"
        self.database.insert_market_evaluation(
            {
                "evaluation_id": market_id,
                "prediction_id": prediction.prediction_id,
                "run_id": run_id,
                "event_id": game.event_id,
                "policy_version": evaluation.policy_version,
                "market_snapshot_checksum": game.source_checksums["odds"],
                "market_no_vig_probability": evaluation.market_no_vig_probability,
                "best_price": evaluation.best_price,
                "best_price_books": list(evaluation.best_price_books),
                "break_even_probability": evaluation.break_even_probability,
                "edge_percentage_points": evaluation.edge_percentage_points,
                "expected_value_per_unit_risk": evaluation.expected_value_per_unit_risk,
                "bookmaker_count": evaluation.eligible_bookmaker_count,
                "source_checksum": evaluation.checksum,
                "payload": evaluation_payload,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
            }
        )
        gate_rows: list[dict[str, Any]] = []
        enriched_gates: list[dict[str, Any]] = []
        for gate in evaluation_payload["gate_results"]:
            observed = gate.get("observed", gate.get("observed_value"))
            gate_source = payload_checksum(
                {
                    "policy_version": evaluation.policy_version,
                    "evaluation_checksum": evaluation.checksum,
                    "gate": gate,
                }
            )
            enriched = {
                **gate,
                "observed": observed,
                "policy_version": evaluation.policy_version,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
                "source_checksum": gate_source,
            }
            enriched_gates.append(enriched)
            gate_rows.append(
                {
                    "gate_result_id": f"gate_{gate_source[:24]}",
                    "gate_code": gate["code"],
                    "threshold": gate.get("threshold"),
                    "observed": observed,
                    "passed": gate["passed"],
                    "reason": gate["reason"],
                    "source_checksum": gate_source,
                    "evaluated_at": evaluation.evaluated_at.isoformat(),
                }
            )
        evaluation_payload["gate_results"] = enriched_gates
        evaluation_payload["evaluation_id"] = evaluation.evaluation_id
        evaluation_payload["policy_evaluation_id"] = evaluation.evaluation_id
        evaluation_payload["checksum"] = evaluation.checksum
        all_passed = all(item["passed"] for item in enriched_gates)
        self.database.insert_policy_evaluation(
            {
                "policy_evaluation_id": evaluation.evaluation_id,
                "evaluation_id": market_id,
                "prediction_id": prediction.prediction_id,
                "policy_version": evaluation.policy_version,
                "outcome": evaluation.decision.value,
                "all_gates_passed": all_passed,
                "source_checksum": evaluation.checksum,
                "payload": evaluation_payload,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
            },
            gate_rows,
        )
        existing: list[dict[str, Any]] = []
        policy_path = paths.json_path(CANDIDATE_POLICY_RESULTS_FILENAME)
        if policy_path.exists():
            existing = _records(policy_path)
        _write_records(
            paths,
            PREDICTION_EVALUATIONS_FILENAME,
            render_prediction_evaluations([*existing, evaluation_payload]),
        )
        _write_records(
            paths,
            CANDIDATE_POLICY_RESULTS_FILENAME,
            render_candidate_policy_results([*existing, evaluation_payload]),
        )
        create_zip(paths.run_dir)
        return evaluation_payload

    def create_draft(self, run_id: str, *, generated_at: datetime | None = None) -> dict[str, Any]:
        paths = self._paths(run_id)
        timestamp = generated_at or _now()
        canonical = _records(paths.json_path(CANONICAL_GAMES_FILENAME))
        features = _records(paths.json_path(ANALYSIS_FEATURES_FILENAME))
        predictions = _records(paths.json_path(SEALED_PREDICTIONS_FILENAME))
        evaluation_history = _records(
            paths.json_path(CANDIDATE_POLICY_RESULTS_FILENAME)
        )
        latest_by_event: dict[str, dict[str, Any]] = {}
        for evaluation in sorted(
            evaluation_history,
            key=lambda item: (
                str(item.get("evaluated_at") or ""),
                str(item.get("evaluation_id") or ""),
            ),
        ):
            latest_by_event[str(evaluation.get("event_id") or "")] = evaluation
        evaluations = list(latest_by_event.values())
        draft = create_daily_card_draft(
            run_id=run_id,
            requested_date=paths.requested_date,
            policy_version=CANDIDATE_POLICY_VERSION,
            canonical_games=canonical,
            features=features,
            sealed_predictions=predictions,
            policy_evaluations=evaluations,
            generated_at=timestamp,
        )
        self.database.insert_card_draft(
            {
                **draft,
                "created_at": draft["generated_at"],
                "payload": draft,
            },
            draft["policy_evaluation_ids"],
        )
        write_json(paths.json_path(DAILY_CARD_JSON_FILENAME), render_daily_card(draft))
        write_text(paths.text_path(DAILY_CARD_MARKDOWN_FILENAME), render_daily_card_markdown(draft))
        create_zip(paths.run_dir)
        return draft

    def approve_draft(
        self,
        run_id: str,
        draft: Mapping[str, Any],
        *,
        reviewer_id: str,
        decisions: Mapping[str, Any],
        batch_review: Mapping[str, Any],
        approved_at: datetime | None = None,
    ) -> dict[str, Any]:
        paths = self._paths(run_id)
        timestamp = approved_at or _now()
        canonical_rows = _records(paths.json_path(CANONICAL_GAMES_FILENAME))
        feature_rows = _records(paths.json_path(ANALYSIS_FEATURES_FILENAME))
        for record in [*canonical_rows, *feature_rows]:
            expected = record.get("checksum")
            unsigned = {key: value for key, value in record.items() if key != "checksum"}
            if not isinstance(expected, str) or checksum_payload(unsigned) != expected:
                raise ReleaseWorkflowError("Release source artifact checksum is invalid")
        canonical_by_event = {
            str(item["event_id"]): _canonical_from_mapping(item) for item in canonical_rows
        }
        feature_by_event = {str(item["event_id"]): item for item in feature_rows}
        predictions_by_id = {
            str(item["prediction_id"]): _sealed_from_mapping(item)
            for item in _records(paths.json_path(SEALED_PREDICTIONS_FILENAME))
        }
        current_states: dict[str, dict[str, Any]] = {}
        for item in draft.get("items", []):
            if not isinstance(item, Mapping) or item.get("automated_status") != CandidateDecision.CANDIDATE_REQUIRES_REVIEW.value:
                continue
            event_id = str(item["event_id"])
            game = canonical_by_event[event_id]
            prediction = predictions_by_id[str(item["prediction_id"])]
            current = evaluate_moneyline_candidate(
                game,
                prediction,
                evaluated_at=timestamp,
                phase2_live_weather_accepted=self.settings.phase2_weather_gate_clear(),
            )
            current_gate = {gate.code: gate.passed for gate in current.gate_results}
            market = item["market_evaluation"]
            feature = feature_by_event[event_id]
            current_sources = {
                **{
                    f"canonical_source:{key}": value
                    for key, value in game.source_checksums.items()
                },
                "canonical_game": game.checksum,
                "features": feature["checksum"],
            }
            current_states[event_id] = {
                "scheduled_first_pitch_utc": game.commence_time.isoformat(),
                "event_status": "pregame" if timestamp < game.commence_time else "started",
                "best_price_fresh": current_gate.get("odds_fresh", False),
                "best_price": current.best_price,
                "best_price_books": list(current.best_price_books),
                "policy_version": CANDIDATE_POLICY_VERSION,
                "prediction_checksum": prediction.checksum,
                "evidence_checksum": prediction.evidence_checksum,
                "policy_evaluation_checksum": item["policy_evaluation_checksum"],
                "required_source_checksums": current_sources,
                "draft_best_price": market.get("best_price"),
                "current_policy_evaluation": current.as_dict(),
            }
        package = approve_daily_card(
            draft,
            reviewer_id=reviewer_id,
            decisions=decisions,
            batch_review=batch_review,
            current_event_states=current_states,
            current_policy_version=CANDIDATE_POLICY_VERSION,
            approved_at=timestamp,
        )
        package_payload = package.as_dict()
        payload = redact_value(package_payload, self.settings.credential_values())
        if payload != package_payload:
            raise ReleaseWorkflowError(
                "Review evidence contains credential-bearing material"
            )
        for decision in payload["review_decisions"]:
            self.database.insert_review_decision({**decision, "draft_id": payload["draft_id"]})
        self.database.insert_publication_batch(
            {
                **payload,
                "published_at": payload["approved_at"],
                "payload": payload,
            },
            payload["published_plays"],
        )
        write_json(paths.json_path(DAILY_CARD_JSON_FILENAME), render_daily_card(payload))
        write_text(paths.text_path(DAILY_CARD_MARKDOWN_FILENAME), render_daily_card_markdown(payload))
        ledger = render_results_ledger([payload], [], generated_at=timestamp)
        write_json(paths.json_path(RESULTS_LEDGER_FILENAME), ledger)
        artifact_payloads = {
            DAILY_CARD_JSON_FILENAME: render_daily_card(payload),
            DAILY_CARD_MARKDOWN_FILENAME: render_daily_card_markdown(payload),
            RESULTS_LEDGER_FILENAME: ledger,
        }
        manifest = build_publication_manifest(payload, artifact_payloads, generated_at=timestamp)
        write_json(paths.json_path(PUBLICATION_MANIFEST_FILENAME), manifest)
        create_zip(paths.run_dir)
        return payload

    def settle(
        self,
        run_id: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        paths = self._paths(run_id)
        stored = self.database.insert_settlement_event(record)
        batch_id = str(record["batch_id"])
        batch = self.database.get_publication_batch(batch_id)
        if batch is None:
            raise ReleaseWorkflowError("Publication batch was not found")
        raw_payload = json.loads(str(batch["payload_json"]))
        ledger = render_results_ledger(
            [raw_payload],
            self.database.list_settlement_ledger(batch_id=batch_id),
        )
        write_json(paths.json_path(RESULTS_LEDGER_FILENAME), ledger)
        create_zip(paths.run_dir)
        return stored
