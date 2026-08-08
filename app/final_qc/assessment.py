from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from app.artifacts import validate_artifact_relpath
from app.daily_slate.contracts import canonical_sha256
from app.final_qc.contracts import FINAL_QC_CHECK_CODES, FinalQcCheckV1, FinalQcPolicyV1, FinalQcV1
from app.infographic.repository import PersistedInfographicV1
from app.pdf_report.repository import PersistedPdfReportV1
from app.redaction import redact_value


def assess_final_qc(
    *,
    pdf: PersistedPdfReportV1,
    infographic: PersistedInfographicV1,
    evaluated_at: datetime,
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
    observations = {
        "pdf_snapshot_exists": (True, pdf.snapshot_id),
        "pdf_artifact_checksum": (len(pdf.artifacts.pdf.checksum) == 64, pdf.artifacts.pdf.checksum),
        "pdf_byte_count": (pdf.artifacts.pdf.byte_count > 0, pdf.artifacts.pdf.byte_count),
        "pdf_preflight": (pdf.artifacts.page_count > 0, pdf.artifacts.page_count),
        "infographic_snapshot_exists": (True, infographic.snapshot_id),
        "infographic_artifact_checksums": (
            all(len(a.checksum) == 64 for a in (infographic.artifacts.feed, infographic.artifacts.story)),
            [infographic.artifacts.feed.checksum, infographic.artifacts.story.checksum],
        ),
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
        "paths_contained": (
            all(
                validate_artifact_relpath(a.relpath) == a.relpath
                for a in (
                    pdf.artifacts.document,
                    pdf.artifacts.pdf,
                    pdf.artifacts.manifest,
                    infographic.artifacts.document,
                    infographic.artifacts.feed,
                    infographic.artifacts.story,
                    infographic.artifacts.manifest,
                )
            ),
            True,
        ),
        "artifact_sizes": (
            all(
                a.byte_count > 0
                for a in (
                    pdf.artifacts.document,
                    pdf.artifacts.pdf,
                    pdf.artifacts.manifest,
                    infographic.artifacts.document,
                    infographic.artifacts.feed,
                    infographic.artifacts.story,
                    infographic.artifacts.manifest,
                )
            ),
            True,
        ),
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
