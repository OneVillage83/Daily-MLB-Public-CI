from __future__ import annotations

import math
from datetime import datetime, timezone

from app.daily_slate.contracts import canonical_sha256
from app.model_feature_set.contracts import ModelFeatureGameV1, ModelFeatureSetV1
from app.model_feature_set.schema import (
    MODEL_FEATURE_SCHEMA_CHECKSUM,
    MODEL_FEATURE_SCHEMA_VERSION,
)
from app.predictions.contracts import (
    GamePredictionV1,
    ModelCalibrationStatus,
    ModelDeploymentStatus,
    PredictionFeatureContributionV1,
    PredictionFeatureTermV1,
    PredictionInputState,
    PredictionModelManifestV1,
    PredictionsContractError,
    PredictionsV1,
)
from app.predictions.probability import (
    build_run_distribution,
    decisive_moneyline_probabilities,
)


def _term(
    feature_name: str,
    *,
    center: float,
    scale: float,
    home: float = 0.0,
    away: float = 0.0,
) -> PredictionFeatureTermV1:
    return PredictionFeatureTermV1(
        feature_name=feature_name,
        center=center,
        scale=scale,
        missing_value=center,
        home_coefficient=home,
        away_coefficient=away,
    )


REFERENCE_HEURISTIC_POISSON_MANIFEST_CHECKSUM_V1 = (
    "386904476579911615d23d76e8f64500419995e9114ce382e7f9b8783edcb18e"
)


REFERENCE_HEURISTIC_POISSON_V1 = PredictionModelManifestV1(
    model_id="dse_mlb_reference_heuristic_poisson",
    model_version="1.0.0",
    model_kind="independent_poisson_log_rate",
    deployment_status=ModelDeploymentStatus.REFERENCE_ONLY,
    calibration_status=ModelCalibrationStatus.UNCALIBRATED_REFERENCE,
    recommendation_eligible=False,
    parameter_origin="transparent_expert_heuristic_not_empirically_fitted",
    home_log_rate_intercept=math.log(4.55),
    away_log_rate_intercept=math.log(4.35),
    minimum_run_rate=1.25,
    maximum_run_rate=9.0,
    terms=(
        _term(
            "home.lineup.season_to_date.hitting.ops",
            center=0.720,
            scale=0.100,
            home=0.18,
        ),
        _term(
            "away.lineup.season_to_date.hitting.ops",
            center=0.720,
            scale=0.100,
            away=0.18,
        ),
        _term(
            "home.lineup.rolling_14_days.hitting.ops",
            center=0.720,
            scale=0.100,
            home=0.06,
        ),
        _term(
            "away.lineup.rolling_14_days.hitting.ops",
            center=0.720,
            scale=0.100,
            away=0.06,
        ),
        _term(
            "away.starter.season_to_date.pitching.era",
            center=4.20,
            scale=1.50,
            home=0.12,
        ),
        _term(
            "home.starter.season_to_date.pitching.era",
            center=4.20,
            scale=1.50,
            away=0.12,
        ),
        _term(
            "away.starter.season_to_date.pitching.k_minus_bb_rate",
            center=0.140,
            scale=0.080,
            home=-0.10,
        ),
        _term(
            "home.starter.season_to_date.pitching.k_minus_bb_rate",
            center=0.140,
            scale=0.080,
            away=-0.10,
        ),
        _term(
            "away.bullpen.season_to_date.pitching.era",
            center=4.20,
            scale=1.50,
            home=0.06,
        ),
        _term(
            "home.bullpen.season_to_date.pitching.era",
            center=4.20,
            scale=1.50,
            away=0.06,
        ),
        _term(
            "away.bullpen.workload.previous_3_days.pitch_count_sum",
            center=120.0,
            scale=80.0,
            home=0.04,
        ),
        _term(
            "home.bullpen.workload.previous_3_days.pitch_count_sum",
            center=120.0,
            scale=80.0,
            away=0.04,
        ),
        _term(
            "weather.temperature_f",
            center=72.0,
            scale=15.0,
            home=0.02,
            away=0.02,
        ),
        _term(
            "weather.wind_outfield_component_mph",
            center=0.0,
            scale=10.0,
            home=0.02,
            away=0.02,
        ),
    ),
    tail_tolerance=1e-12,
    maximum_run_support=60,
)

if (
    REFERENCE_HEURISTIC_POISSON_V1.checksum
    != REFERENCE_HEURISTIC_POISSON_MANIFEST_CHECKSUM_V1
):
    raise RuntimeError("reference prediction manifest changed without a version bump")


def _run_rate(log_rate: float, manifest: PredictionModelManifestV1) -> float:
    if not math.isfinite(log_rate):
        raise PredictionsContractError("model log run rate must be finite")
    rate = math.exp(log_rate)
    return min(manifest.maximum_run_rate, max(manifest.minimum_run_rate, rate))


def predict_game(
    game: ModelFeatureGameV1,
    manifest: PredictionModelManifestV1,
) -> GamePredictionV1:
    if manifest.feature_schema_version != game.schema_version:
        raise PredictionsContractError(
            "prediction manifest and game feature schema versions disagree"
        )
    if manifest.feature_schema_checksum != game.schema_checksum:
        raise PredictionsContractError(
            "prediction manifest and game feature schema checksums disagree"
        )
    feature_map = game.feature_map()
    contributions: list[PredictionFeatureContributionV1] = []
    home_log_rate = manifest.home_log_rate_intercept
    away_log_rate = manifest.away_log_rate_intercept
    for term in manifest.terms:
        raw_value = feature_map[term.feature_name]
        effective_value = term.missing_value if raw_value is None else raw_value
        standardized_value = (effective_value - term.center) / term.scale
        home_contribution = standardized_value * term.home_coefficient
        away_contribution = standardized_value * term.away_coefficient
        home_log_rate += home_contribution
        away_log_rate += away_contribution
        contributions.append(
            PredictionFeatureContributionV1(
                feature_name=term.feature_name,
                raw_value=raw_value,
                effective_value=effective_value,
                standardized_value=standardized_value,
                home_log_rate_contribution=home_contribution,
                away_log_rate_contribution=away_contribution,
                was_imputed=raw_value is None,
            )
        )
    home_run_rate = _run_rate(home_log_rate, manifest)
    away_run_rate = _run_rate(away_log_rate, manifest)
    distribution = build_run_distribution(
        home_run_rate=home_run_rate,
        away_run_rate=away_run_rate,
        maximum_run_support=manifest.maximum_run_support,
    )
    if distribution.approximation_tail_bound > manifest.tail_tolerance:
        raise PredictionsContractError(
            "run-distribution tail exceeds manifest tolerance"
        )
    home_win, away_win, tie = decisive_moneyline_probabilities(distribution)
    used_feature_names = tuple(term.feature_name for term in manifest.terms)
    imputed_feature_names = tuple(
        item.feature_name for item in contributions if item.was_imputed
    )
    input_state = (
        PredictionInputState.PRIOR_ONLY
        if not used_feature_names or len(imputed_feature_names) == len(used_feature_names)
        else PredictionInputState.IMPUTED
        if imputed_feature_names
        else PredictionInputState.COMPLETE
    )
    model_input_checksum = canonical_sha256(
        {
            "effective_terms": [
                {
                    "effective_value": item.effective_value,
                    "feature_name": item.feature_name,
                    "standardized_value": item.standardized_value,
                    "was_imputed": item.was_imputed,
                }
                for item in contributions
            ],
            "feature_schema_checksum": game.schema_checksum,
            "model_manifest_checksum": manifest.checksum,
        }
    )
    return GamePredictionV1(
        edge_event_id=game.edge_event_id,
        daily_mlb_game_id=game.daily_mlb_game_id,
        source_game_id=game.source_game_id,
        away_team_id=game.away_team_id,
        home_team_id=game.home_team_id,
        upstream_model_feature_game_checksum=game.checksum,
        model_manifest_checksum=manifest.checksum,
        model_id=manifest.model_id,
        model_version=manifest.model_version,
        deployment_status=manifest.deployment_status,
        calibration_status=manifest.calibration_status,
        recommendation_eligible=manifest.recommendation_eligible,
        quality_disposition=game.quality_disposition,
        quality_issue_codes=game.quality_issue_codes,
        market_reference_checksum=game.market_reference_checksum,
        input_state=input_state,
        used_feature_names=used_feature_names,
        imputed_feature_names=imputed_feature_names,
        model_input_checksum=model_input_checksum,
        contributions=tuple(contributions),
        distribution=distribution,
        home_win_probability=home_win,
        away_win_probability=away_win,
        tied_after_regulation_probability=tie,
    )


def predict_model_feature_set(
    feature_set: ModelFeatureSetV1,
    *,
    manifest: PredictionModelManifestV1 = REFERENCE_HEURISTIC_POISSON_V1,
    observed_at: datetime | None = None,
) -> PredictionsV1:
    if feature_set.schema_version != MODEL_FEATURE_SCHEMA_VERSION:
        raise PredictionsContractError("unsupported ModelFeatureSet schema version")
    if feature_set.schema_checksum != MODEL_FEATURE_SCHEMA_CHECKSUM:
        raise PredictionsContractError("ModelFeatureSet schema checksum mismatch")
    if manifest.feature_schema_version != feature_set.schema_version:
        raise PredictionsContractError(
            "model manifest and ModelFeatureSet schema versions disagree"
        )
    if manifest.feature_schema_checksum != feature_set.schema_checksum:
        raise PredictionsContractError(
            "model manifest and ModelFeatureSet schema checksums disagree"
        )
    upstream_observed = feature_set.observed_at.astimezone(timezone.utc)
    selected_observed = upstream_observed if observed_at is None else observed_at
    if selected_observed.tzinfo is None or selected_observed.utcoffset() is None:
        raise PredictionsContractError("Predictions observed_at must be timezone-aware")
    selected_observed = selected_observed.astimezone(timezone.utc)
    if selected_observed < upstream_observed:
        raise PredictionsContractError(
            "Predictions observed_at cannot precede ModelFeatureSet evidence"
        )
    games = tuple(predict_game(game, manifest) for game in feature_set.games)
    if len(games) != len(feature_set.games):
        raise PredictionsContractError("every ModelFeatureSet game must be predicted")
    return PredictionsV1(
        requested_date=feature_set.requested_date,
        as_of_time=feature_set.as_of_time,
        observed_at=selected_observed,
        upstream_model_feature_set_checksum=feature_set.checksum,
        model_manifest=manifest,
        games=games,
    )
