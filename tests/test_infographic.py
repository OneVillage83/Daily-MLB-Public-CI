from __future__ import annotations

from dataclasses import replace

import pytest

from app.infographic.assembly import assemble_infographic_document
from app.infographic.contracts import InfographicContractError, InfographicVariantType
from app.infographic.policy import InfographicPolicyError, InfographicPolicyV1
from app.infographic.renderer import render_infographic_svg
from tests.test_final_output_pipeline import NOW, _semantic_report


def _document():
    return assemble_infographic_document(
        _semantic_report(),
        upstream_pdf_report_snapshot_id="pdf-report:fixture",
        generated_at=NOW,
    )


def test_infographic_references_exact_pdf_and_retains_current_rank_semantics() -> None:
    report = _semantic_report()
    document = assemble_infographic_document(
        report,
        upstream_pdf_report_snapshot_id="pdf-report:fixture",
        generated_at=NOW,
    )
    assert document.upstream_pdf_report_checksum == report.checksum
    assert document.full_report_game_count == len(report.games)
    for variant in document.variants:
        assert variant.pick_of_day is not None
        assert variant.pick_of_day.recommendation_rank == 1
        cards = (variant.pick_of_day,) + variant.recommendations
        assert [card.recommendation_rank for card in cards] == list(range(1, len(cards) + 1))
        assert {card.source_game_id for card in cards} == set(report.recommendation_game_ids)
        assert all(report.games[card.recommendation_rank - 1].decision == "recommend" for card in cards)


def test_feed_story_render_is_deterministic_fixed_size_and_has_no_external_assets() -> None:
    document = _document()
    expected = {
        InfographicVariantType.FEED_4X5: (1080, 1350),
        InfographicVariantType.STORY_9X16: (1080, 1920),
    }
    for variant in InfographicVariantType:
        first = render_infographic_svg(document, variant)
        assert first == render_infographic_svg(document, variant)
        assert first.startswith(b"<svg") and first.endswith(b"</svg>")
        assert f'width="{expected[variant][0]}" height="{expected[variant][1]}"'.encode() in first
        assert b"<image" not in first and b" href=" not in first and b"xlink:href" not in first
        assert document.checksum.encode() in first


def test_infographic_policy_prohibits_recalculation_rewrite_approval_and_staking() -> None:
    for field in (
        "allow_value_recalculation",
        "allow_decision_rewrite",
        "allow_rank_rewrite",
        "allow_missing_data_fabrication",
        "allow_publication_approval",
        "allow_stake_sizing",
    ):
        with pytest.raises(InfographicPolicyError, match="prohibited"):
            InfographicPolicyV1(**{field: True})


def test_pick_of_day_and_rank_inventory_cannot_be_rewritten() -> None:
    document = _document()
    feed = document.variants[0]
    assert feed.pick_of_day is not None
    with pytest.raises(InfographicContractError, match="rank order"):
        replace(feed, recommendations=(replace(feed.pick_of_day, recommendation_rank=3),))

