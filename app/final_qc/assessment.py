from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.artifacts import resolve_contained_path
from app.daily_slate.contracts import canonical_sha256
from app.final_qc.contracts import FINAL_QC_CHECK_CODES, FinalQcCheckV1, FinalQcPolicyV1, FinalQcV1
from app.infographic.artifact import verify_infographic_artifacts
from app.infographic.repository import PersistedInfographicV1
from app.pdf_report.artifact import pdf_preflight, verify_production_pdf_report_artifacts
from app.pdf_report.repository import PersistedPdfReportV1
from app.pre_model_evidence import PreModelArtifactV1
from app.redaction import redact_value


def _physical_check(operation: Callable[[], Any]) -> tuple[bool, object]:
    try:
        observation = operation()
    except Exception as exc:
        return False, {"error_type": type(exc).__name__, "status": "failed"}
    return True, observation


def _actual_size(artifact_root: Path, artifact: PreModelArtifactV1) -> dict[str, object]:
    path = resolve_contained_path(artifact_root, artifact.relpath)
    actual = path.stat().st_size
    if actual != artifact.byte_count:
        raise ValueError("artifact byte-count evidence mismatch")
    return {"actual": actual, "expected": artifact.byte_count}


def _contained_relpath(artifact_root: Path, artifact: PreModelArtifactV1) -> str:
    resolve_contained_path(artifact_root, artifact.relpath)
    return artifact.relpath


def _actual_pdf_preflight(
    artifact_root: Path,
    pdf: PersistedPdfReportV1,
) -> dict[str, object]:
    path = resolve_contained_path(artifact_root, pdf.artifacts.pdf.relpath)
    result = pdf_preflight(path.read_bytes())
    if (
        result.sha256 != pdf.artifacts.pdf.checksum
        or result.byte_count != pdf.artifacts.pdf.byte_count
        or result.page_count != pdf.artifacts.page_count
    ):
        raise ValueError("PDF preflight evidence mismatch")
    return result.as_dict()


def _verify_pdf_artifacts(
    artifact_root: Path,
    pdf: PersistedPdfReportV1,
) -> dict[str, object]:
    verify_production_pdf_report_artifacts(pdf.document, pdf.artifacts, artifact_root)
    return {"checksum": pdf.artifacts.pdf.checksum, "status": "verified"}


def _verify_infographic_artifact_set(
    artifact_root: Path,
    infographic: PersistedInfographicV1,
) -> dict[str, object]:
    verify_infographic_artifacts(infographic.document, infographic.artifacts, artifact_root)
    return {
        "feed_checksum": infographic.artifacts.feed.checksum,
        "status": "verified",
        "story_checksum": infographic.artifacts.story.checksum,
    }


def assess_final_qc(
    *,
    pdf: PersistedPdfReportV1,
    infographic: PersistedInfographicV1,
    evaluated_at: datetime,
    artifact_root: Path,
    policy: FinalQcPolicyV1 = FinalQcPolicyV1(),
    secret_values: Iterable[str] = (),
) -> tuple[tuple[FinalQcCheckV1, ...], FinalQcV1 | None]:
    doc = pdf.document
    info = infographic.document
    pdf_games = {g.source_game_id: g for g in doc.games}
    recommendations = [g for g in doc.games if g.decision == "recommend"]
    variant_cards = tuple(
        (() if variant.pick_of_day is None else (variant.pick_of_day,)) + variant.recommendations
        for variant in info.variants
    )
    cards = tuple(card for inventory in variant_cards for card in inventory)
    card_ids = [c.source_game_id for c in cards]
    variant_card_ids = [[card.source_game_id for card in inventory] for inventory in variant_cards]
    recommend_ids = [g.source_game_id for g in sorted(recommendations, key=lambda g: g.recommendation_rank or 0)]
    all_artifacts = (
        pdf.artifacts.document,
        pdf.artifacts.pdf,
        pdf.artifacts.manifest,
        infographic.artifacts.document,
        infographic.artifacts.feed,
        infographic.artifacts.story,
        infographic.artifacts.manifest,
    )
    pdf_artifact_verified = _physical_check(lambda: _verify_pdf_artifacts(artifact_root, pdf))
    pdf_size_verified = _physical_check(lambda: _actual_size(artifact_root, pdf.artifacts.pdf))
    pdf_preflight_verified = _physical_check(lambda: _actual_pdf_preflight(artifact_root, pdf))
    infographic_artifacts_verified = _physical_check(
        lambda: _verify_infographic_artifact_set(artifact_root, infographic)
    )
    contained_paths = _physical_check(
        lambda: [_contained_relpath(artifact_root, artifact) for artifact in all_artifacts]
    )
    artifact_sizes = _physical_check(
        lambda: [_actual_size(artifact_root, artifact) for artifact in all_artifacts]
    )
    observations = {
        "pdf_snapshot_exists": (True, pdf.snapshot_id),
        "pdf_artifact_checksum": pdf_artifact_verified,
        "pdf_byte_count": pdf_size_verified,
        "pdf_preflight": pdf_preflight_verified,
        "infographic_snapshot_exists": (True, infographic.snapshot_id),
        "infographic_artifact_checksums": infographic_artifacts_verified,
        "infographic_dimensions": (
            tuple((v.width, v.height) for v in info.variants) == ((1080, 1350), (1080, 1920)),
            [(v.width, v.height) for v in info.variants],
        ),
        "run_identity": (
            doc.run_id == info.run_id == pdf.run_id == infographic.run_id and doc.requested_date == info.requested_date,
            [doc.run_id, info.run_id, doc.requested_date, info.requested_date],
        ),
        "game_inventory": (
            info.full_report_game_count == len(doc.games),
            [len(doc.games), info.full_report_game_count],
        ),
        "recommendation_identity": (
            all(ids == recommend_ids[: len(ids)] for ids in variant_card_ids),
            {"cards_by_variant": variant_card_ids, "recommendations": recommend_ids},
        ),
        "recommendation_ranks": (
            all(
                [c.recommendation_rank for c in inventory] == list(range(1, len(inventory) + 1))
                for inventory in variant_cards
            ),
            [[c.recommendation_rank for c in inventory] for inventory in variant_cards],
        ),
        "selected_identity": (
            all(
                pdf_games[c.source_game_id].selected_team_id == c.selected_team_id
                and pdf_games[c.source_game_id].selected_side == c.selected_side
                for c in cards
            ),
            card_ids,
        ),
        "displayed_value_metrics": (
            all(
                next(o for o in pdf_games[c.source_game_id].outcomes if o.side == c.selected_side).edge == c.edge
                and next(
                    o for o in pdf_games[c.source_game_id].outcomes if o.side == c.selected_side
                ).expected_value_per_unit
                == c.expected_value_per_unit
                for c in cards
            ),
            card_ids,
        ),
        "displayed_price_bookmakers": (
            all(
                next(o for o in pdf_games[c.source_game_id].outcomes if o.side == c.selected_side).best_price
                == c.best_price
                and next(o for o in pdf_games[c.source_game_id].outcomes if o.side == c.selected_side).bookmaker_count
                == c.bookmaker_count
                for c in cards
            ),
            card_ids,
        ),
        "infographic_recommend_subset": (
            all(pdf_games[c.source_game_id].decision == "recommend" for c in cards),
            card_ids,
        ),
        "infographic_rank_order": (
            all(ids == recommend_ids[: len(ids)] for ids in variant_card_ids),
            variant_card_ids,
        ),
        "pick_of_day_rank_one": (
            all(variant.pick_of_day is None or variant.pick_of_day.recommendation_rank == 1 for variant in info.variants),
            [None if variant.pick_of_day is None else variant.pick_of_day.source_game_id for variant in info.variants],
        ),
        "pass_not_promoted": (all(pdf_games[c.source_game_id].decision != "pass" for c in cards), card_ids),
        "avoid_not_promoted": (all(pdf_games[c.source_game_id].decision != "avoid" for c in cards), card_ids),
        "no_unknown_recommendation": (all(c.source_game_id in pdf_games for c in cards), card_ids),
        "lineage_reconciled": (
            info.upstream_pdf_report_snapshot_id == pdf.snapshot_id
            and info.upstream_pdf_report_checksum == doc.checksum,
            [info.upstream_pdf_report_snapshot_id, pdf.snapshot_id],
        ),
        "timestamps_valid": (
            doc.as_of_time <= doc.generated_at <= info.generated_at <= evaluated_at,
            [
                doc.as_of_time.isoformat(),
                doc.generated_at.isoformat(),
                info.generated_at.isoformat(),
                evaluated_at.isoformat(),
            ],
        ),
        "paths_contained": contained_paths,
        "artifact_sizes": artifact_sizes,
        "secret_free": (
            redact_value(
                {"pdf": doc.as_dict(), "infographic": info.as_dict()},
                tuple(str(v) for v in secret_values if str(v)),
                preserve_field_names=("bookmaker_key", "market_key"),
            )
            == {"pdf": doc.as_dict(), "infographic": info.as_dict()},
            True,
        ),
        "no_generation_exception": (
            doc.report_status == info.report_status == "pre_review",
            [doc.report_status, info.report_status],
        ),
    }
    source = canonical_sha256({"pdf": doc.checksum, "infographic": info.checksum})
    checks = tuple(
        FinalQcCheckV1(i, code, bool(observations[code][0]), observations[code][1], source)
        for i, code in enumerate(FINAL_QC_CHECK_CODES, 1)
    )
    snapshot = None
    if all(check.passed for check in checks):
        snapshot = FinalQcV1(
            doc.run_id,
            doc.requested_date,
            doc.as_of_time,
            evaluated_at,
            policy,
            pdf.snapshot_id,
            doc.checksum,
            infographic.snapshot_id,
            info.checksum,
            checks,
            secret_values=secret_values,
        )
    return checks, snapshot
