from __future__ import annotations

from dataclasses import replace

from reportlab.platypus import Paragraph, Table  # type: ignore[import-untyped]

from app.pdf_report.artifact import pdf_preflight
from app.pdf_report.renderer import (
    CONTENT_WIDTH,
    _production_full_slate_table,
    _production_outcome_table,
    _production_pdf_story,
    _production_recommendations_table,
    _styles,
    render_production_pdf_report,
)
from scripts.generate_final_output_fixture import _game, _report


def _flowable_text(value: object) -> tuple[str, ...]:
    if isinstance(value, Paragraph):
        return (value.getPlainText(),)
    if isinstance(value, Table):
        return tuple(
            text
            for row in value._cellvalues  # noqa: SLF001 - focused renderer regression
            for cell in row
            for text in _flowable_text(cell)
        )
    if isinstance(value, list | tuple):
        return tuple(text for item in value for text in _flowable_text(item))
    return ()


def test_three_game_production_report_restores_every_dossier_before_methodology() -> None:
    report = _report()
    styles = _styles()
    story, rendered_ids = _production_pdf_story(report, styles)
    content = render_production_pdf_report(report)
    preflight = pdf_preflight(content)
    top_level_text = [item.getPlainText() for item in story if isinstance(item, Paragraph)]
    all_text = "\n".join(text for item in story for text in _flowable_text(item))

    assert rendered_ids == tuple(game.source_game_id for game in report.games)
    assert len(rendered_ids) == 3
    for game in report.games:
        assert sum(game.source_game_id in text for text in top_level_text) == 1
    assert top_level_text.index("Methodology and Audit") > max(
        index for index, text in enumerate(top_level_text) if "Game ID:" in text
    )
    assert {game.decision for game in report.games} == {"recommend", "pass", "avoid"}
    assert " BET " not in f" {all_text} "
    assert " LEAN " not in f" {all_text} "
    assert "Projected spread" not in all_text
    assert "Projected total" not in all_text
    assert preflight.page_count > 0


def test_sixteen_game_full_slate_has_no_game_or_page_cap() -> None:
    small_report = _report()
    large_report = _report(16)
    story, rendered_ids = _production_pdf_story(large_report, _styles())
    small_preflight = pdf_preflight(render_production_pdf_report(small_report))
    large_preflight = pdf_preflight(render_production_pdf_report(large_report))

    assert rendered_ids == tuple(f"fixture-game-{ordinal}" for ordinal in range(1, 17))
    assert len(rendered_ids) == 16
    assert large_preflight.page_count > small_preflight.page_count
    ranks = [game.recommendation_rank for game in large_report.games if game.decision == "recommend"]
    assert ranks == list(range(1, len(ranks) + 1))
    assert all(
        game.recommendation_rank is None
        for game in large_report.games
        if game.decision in {"pass", "avoid"}
    )
    text = [item.getPlainText() for item in story if isinstance(item, Paragraph)]
    assert text.index("Methodology and Audit") > max(
        index for index, value in enumerate(text) if "Game ID:" in value
    )


def test_zero_game_production_report_is_valid() -> None:
    report = _report(0)
    content = render_production_pdf_report(report)
    preflight = pdf_preflight(content)
    story, rendered_ids = _production_pdf_story(report, _styles())
    text = "\n".join(value for item in story for value in _flowable_text(item))

    assert report.games == ()
    assert rendered_ids == ()
    assert preflight.page_count > 0
    assert "0Games" in text
    assert "0Recommendations" in text
    assert "zero games" in text.lower()


def test_zero_recommendation_report_retains_pass_and_avoid_dossiers() -> None:
    report = replace(
        _report(),
        games=(_game(1, "pass"), _game(2, "avoid"), _game(3, "pass")),
    )
    story, rendered_ids = _production_pdf_story(report, _styles())
    text = "\n".join(value for item in story for value in _flowable_text(item))
    preflight = pdf_preflight(render_production_pdf_report(report))

    assert rendered_ids == tuple(game.source_game_id for game in report.games)
    assert all(game.decision in {"pass", "avoid"} for game in report.games)
    assert "No canonical RECOMMEND entries were retained for this slate." in text
    assert preflight.page_count > 0


def test_long_labels_fit_all_production_tables() -> None:
    report = _report()
    game = report.games[0]
    long_away = "fixture-away-team-with-an-intentionally-long-customer-facing-identifier"
    long_home = "fixture-home-team-with-an-intentionally-long-customer-facing-identifier"
    long_selected = "fixture-selected-team-with-an-intentionally-long-customer-facing-identifier"
    outcomes = (
        replace(game.outcomes[0], outcome_team_id=long_selected),
        replace(game.outcomes[1], outcome_team_id=long_away),
    )
    long_game = replace(
        game,
        away_team_id=long_away,
        home_team_id=long_home,
        selected_team_id=long_selected,
        outcomes=outcomes,
    )
    long_report = replace(report, games=(long_game, report.games[1], report.games[2]))
    styles = _styles()
    tables = (
        _production_recommendations_table((long_game,), styles),
        _production_full_slate_table(tuple(long_report.games), styles),
        _production_outcome_table(long_game, styles),
    )

    for table in tables:
        width, _ = table.wrap(CONTENT_WIDTH, 4000)
        assert width <= CONTENT_WIDTH + 1e-6
    assert pdf_preflight(render_production_pdf_report(long_report)).page_count > 0
