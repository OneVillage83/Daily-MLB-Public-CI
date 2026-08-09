from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from html import escape
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pdf_report.production import PdfReportGameV1, ProductionPdfReportV1

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.enums import TA_CENTER, TA_LEFT  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import inch  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.pdf_report.assembly import market_display
from app.pdf_report.contracts import (
    PdfReportDecisionRowV1,
    PdfReportDocumentV1,
    PdfReportGameDossierV1,
    ReportFactV1,
)
from app.recommendation_gate.contracts import RecommendationDecision

NAVY = colors.HexColor("#102A43")
BLUE = colors.HexColor("#1F5A94")
GOLD = colors.HexColor("#D99B2B")
PALE_BLUE = colors.HexColor("#EAF2F8")
PALE_GOLD = colors.HexColor("#FFF4D6")
PALE_GREEN = colors.HexColor("#E8F5E9")
PALE_RED = colors.HexColor("#FDECEC")
PALE_GRAY = colors.HexColor("#F3F4F6")
TEXT = colors.HexColor("#172B4D")
MUTED = colors.HexColor("#5E6C84")
BORDER = colors.HexColor("#CAD3DD")
PAGE_WIDTH = letter[0]
CONTENT_WIDTH = PAGE_WIDTH - (0.40 * inch * 2)
PAIR_CARD_GAP = 0.12 * inch
PAIR_CARD_WIDTH = (CONTENT_WIDTH - PAIR_CARD_GAP) / 2
CARD_PADDING_POINTS = 5
PAIR_CARD_CONTENT_WIDTH = PAIR_CARD_WIDTH - (CARD_PADDING_POINTS * 2)
FACT_LABEL_MIN_WIDTH = 0.95 * inch
FACT_LABEL_MAX_WIDTH = 2.15 * inch
METRIC_CARD_WIDTH = CONTENT_WIDTH / 4


class _InvariantCanvas(canvas.Canvas):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)
        self.setAuthor("The Daily Edge")
        self.setCreator("Daily MLB PDF Report V1")
        self.setSubject("Customer-first MLB analytical report")


class _ReportDocTemplate(BaseDocTemplate):
    def __init__(self, buffer: BytesIO, *, title: str) -> None:
        super().__init__(
            buffer,
            pagesize=letter,
            leftMargin=0.40 * inch,
            rightMargin=0.40 * inch,
            topMargin=0.50 * inch,
            bottomMargin=0.50 * inch,
            title=title,
            author="The Daily Edge",
            subject="Daily MLB analytical report",
        )
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="normal",
        )
        self.addPageTemplates(
            [
                PageTemplate(
                    id="report",
                    frames=(frame,),
                    onPage=_draw_page_chrome,
                )
            ]
        )


def _draw_page_chrome(pdf: canvas.Canvas, doc: BaseDocTemplate) -> None:
    pdf.saveState()
    width, height = letter
    pdf.setStrokeColor(BORDER)
    pdf.setLineWidth(0.5)
    pdf.line(0.40 * inch, height - 0.36 * inch, width - 0.40 * inch, height - 0.36 * inch)
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.setFillColor(NAVY)
    pdf.drawString(0.40 * inch, height - 0.26 * inch, "THE DAILY EDGE - DAILY MLB")
    pdf.setFont("Helvetica", 7)
    pdf.setFillColor(MUTED)
    pdf.drawRightString(width - 0.40 * inch, height - 0.26 * inch, "PRE-REVIEW ANALYTICAL REPORT")
    pdf.line(0.40 * inch, 0.36 * inch, width - 0.40 * inch, 0.36 * inch)
    pdf.drawString(0.40 * inch, 0.20 * inch, "Not a guarantee of profit. Verify current price before acting.")
    pdf.drawRightString(width - 0.40 * inch, 0.20 * inch, f"Page {doc.page}")
    pdf.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=25,
            textColor=NAVY,
            alignment=TA_LEFT,
            spaceAfter=5,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=12,
            textColor=MUTED,
            spaceAfter=5,
        ),
        "h1": ParagraphStyle(
            "ReportH1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=16,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "ReportH2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=12.5,
            textColor=BLUE,
            spaceBefore=6,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "ReportBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.2,
            leading=10.4,
            textColor=TEXT,
            spaceAfter=3,
        ),
        "small": ParagraphStyle(
            "ReportSmall",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=6.6,
            leading=8,
            textColor=TEXT,
        ),
        "small_center": ParagraphStyle(
            "ReportSmallCenter",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=6.6,
            leading=8,
            textColor=TEXT,
            alignment=TA_CENTER,
        ),
        "callout": ParagraphStyle(
            "ReportCallout",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.2,
            leading=10.2,
            textColor=NAVY,
        ),
        "cover_metric": ParagraphStyle(
            "CoverMetric",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=12.5,
            leading=14,
            alignment=TA_CENTER,
            textColor=colors.whitesmoke,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.2,
            textColor=colors.whitesmoke,
            alignment=TA_CENTER,
        ),
    }


def _paragraph(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _rich(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _card(
    content: list[object],
    *,
    bg: colors.Color = PALE_GRAY,
    width: float = CONTENT_WIDTH,
) -> Table:
    return Table(
        [[content]],
        colWidths=[width],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), CARD_PADDING_POINTS),
                ("RIGHTPADDING", (0, 0), (-1, -1), CARD_PADDING_POINTS),
                ("TOPPADDING", (0, 0), (-1, -1), CARD_PADDING_POINTS),
                ("BOTTOMPADDING", (0, 0), (-1, -1), CARD_PADDING_POINTS),
            ]
        ),
    )


def _section_title(text: str, styles: dict[str, ParagraphStyle]) -> Paragraph:
    return _paragraph(text, styles["h2"])


def _header_cells(items: tuple[str, ...], styles: dict[str, ParagraphStyle]) -> list[Paragraph]:
    return [_paragraph(item.upper(), styles["table_header"]) for item in items]


def _fact_widths(width: float) -> tuple[float, float]:
    label_width = min(max(width * 0.32, FACT_LABEL_MIN_WIDTH), FACT_LABEL_MAX_WIDTH)
    return label_width, width - label_width


def _decision_fill(decision: RecommendationDecision) -> colors.Color:
    return {
        RecommendationDecision.BET: PALE_GREEN,
        RecommendationDecision.LEAN: PALE_GOLD,
        RecommendationDecision.PASS: PALE_GRAY,
        RecommendationDecision.AVOID: PALE_RED,
    }[decision]


def _ranked_table(
    title: str,
    rows: tuple[PdfReportDecisionRowV1, ...],
    styles: dict[str, ParagraphStyle],
) -> list[object]:
    story: list[object] = [_paragraph(title, styles["h2"])]
    if not rows:
        story.append(
            _card([_paragraph("No actionable candidates on this board.", styles["body"])])
        )
        return story
    header = ["Rank", "Decision", "Matchup / Market", "Price", "Model", "Edge", "EV", "Why"]
    data: list[list[object]] = [_header_cells(tuple(header), styles)]
    for row in rows:
        rank = row.top_confidence_rank if title == "Top Confidence" else row.best_value_rank
        data.append(
            [
                _paragraph(rank, styles["small_center"]),
                _paragraph(row.decision.value, styles["small_center"]),
                _rich(f"{escape(row.matchup_label)}<br/>{escape(market_display(row))}", styles["small"]),
                _paragraph(_american(row.american_price), styles["small_center"]),
                _paragraph(_percent(row.conditional_model_probability), styles["small_center"]),
                _paragraph(_percent(row.no_vig_probability_edge), styles["small_center"]),
                _paragraph(f"{row.expected_value_per_unit:+.3f}", styles["small_center"]),
                _paragraph(row.customer_explanation, styles["small"]),
            ]
        )
    table = Table(
        data,
        repeatRows=1,
        colWidths=[0.35 * inch, 0.52 * inch, 1.48 * inch, 0.47 * inch, 0.48 * inch, 0.45 * inch, 0.43 * inch, 3.32 * inch],
        hAlign="LEFT",
    )
    commands: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index, row in enumerate(rows, start=1):
        commands.append(("BACKGROUND", (0, index), (-1, index), _decision_fill(row.decision)))
    table.setStyle(TableStyle(commands))
    story.append(table)
    return story


def _full_slate_table(
    rows: tuple[PdfReportDecisionRowV1, ...],
    styles: dict[str, ParagraphStyle],
) -> Table:
    header = ["Decision", "Matchup / Market", "Price", "Model", "No-vig", "Edge", "EV", "Ranks", "Primary reason"]
    data: list[list[object]] = [_header_cells(tuple(header), styles)]
    for row in rows:
        ranks = (
            "-"
            if row.top_confidence_rank is None
            else f"C{row.top_confidence_rank} / V{row.best_value_rank}"
        )
        data.append(
            [
                _paragraph(row.decision.value, styles["small_center"]),
                _rich(f"{escape(row.matchup_label)}<br/>{escape(market_display(row))}", styles["small"]),
                _paragraph(_american(row.american_price), styles["small_center"]),
                _paragraph(_percent(row.conditional_model_probability), styles["small_center"]),
                _paragraph(_percent(row.no_vig_probability), styles["small_center"]),
                _paragraph(_percent(row.no_vig_probability_edge), styles["small_center"]),
                _paragraph(f"{row.expected_value_per_unit:+.3f}", styles["small_center"]),
                _paragraph(ranks, styles["small_center"]),
                _paragraph(row.primary_reason_code.replace("_", " "), styles["small"]),
            ]
        )
    table = Table(
        data,
        repeatRows=1,
        colWidths=[0.55 * inch, 1.47 * inch, 0.45 * inch, 0.48 * inch, 0.48 * inch, 0.42 * inch, 0.42 * inch, 0.55 * inch, 2.63 * inch],
        hAlign="LEFT",
    )
    commands: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for index, row in enumerate(rows, start=1):
        commands.append(("BACKGROUND", (0, index), (-1, index), _decision_fill(row.decision)))
    table.setStyle(TableStyle(commands))
    return table


def _facts_table(
    facts: tuple[ReportFactV1, ...],
    styles: dict[str, ParagraphStyle],
    *,
    width: float = CONTENT_WIDTH,
) -> Table:
    label_width, value_width = _fact_widths(width)
    data = [
        [_paragraph(fact.label, styles["small"]), _paragraph(fact.value, styles["small"])]
        for fact in facts
    ]
    table = Table(data, colWidths=[label_width, value_width], hAlign="LEFT")
    commands: list[tuple[object, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), PALE_BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index, fact in enumerate(facts):
        if fact.status == "degraded":
            commands.append(("BACKGROUND", (1, index), (1, index), PALE_GOLD))
        elif fact.status == "unavailable":
            commands.append(("BACKGROUND", (1, index), (1, index), PALE_RED))
    table.setStyle(TableStyle(commands))
    return table


def _fact_card(
    title: str,
    facts: tuple[ReportFactV1, ...],
    styles: dict[str, ParagraphStyle],
    *,
    width: float = PAIR_CARD_WIDTH,
) -> Table:
    return _card(
        [
            _section_title(title, styles),
            _facts_table(facts, styles, width=PAIR_CARD_CONTENT_WIDTH),
        ],
        width=width,
    )


def _decision_detail_table(
    rows: tuple[PdfReportDecisionRowV1, ...],
    styles: dict[str, ParagraphStyle],
) -> Table:
    header = ["Decision", "Market", "Price", "Model", "Edge", "EV", "Confidence", "Why"]
    data: list[list[object]] = [_header_cells(tuple(header), styles)]
    if not rows:
        data.append(
            [
                _paragraph("INFO", styles["small_center"]),
                _paragraph("No evaluable market rows", styles["small"]),
                _paragraph("-", styles["small_center"]),
                _paragraph("-", styles["small_center"]),
                _paragraph("-", styles["small_center"]),
                _paragraph("-", styles["small_center"]),
                _paragraph("-", styles["small_center"]),
                _paragraph("The game remains in the report, but no valid market-side decision was available.", styles["small"]),
            ]
        )
    else:
        for row in rows:
            data.append(
                [
                    _paragraph(row.decision.value, styles["small_center"]),
                    _paragraph(market_display(row), styles["small"]),
                    _paragraph(_american(row.american_price), styles["small_center"]),
                    _paragraph(_percent(row.conditional_model_probability), styles["small_center"]),
                    _paragraph(_percent(row.no_vig_probability_edge), styles["small_center"]),
                    _paragraph(f"{row.expected_value_per_unit:+.3f}", styles["small_center"]),
                    _paragraph(str(row.evidence_confidence_score), styles["small_center"]),
                    _paragraph(row.customer_explanation, styles["small"]),
                ]
            )
    table = Table(
        data,
        repeatRows=1,
        colWidths=[0.55 * inch, 1.16 * inch, 0.48 * inch, 0.50 * inch, 0.45 * inch, 0.42 * inch, 0.55 * inch, 3.39 * inch],
        hAlign="LEFT",
    )
    commands: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for index, row in enumerate(rows, start=1):
        commands.append(("BACKGROUND", (0, index), (-1, index), _decision_fill(row.decision)))
    table.setStyle(TableStyle(commands))
    return table


def _game_story(
    game: PdfReportGameDossierV1,
    styles: dict[str, ParagraphStyle],
) -> list[object]:
    top_block = Table(
        [
            [
                _fact_card("Game information", game.game_facts, styles),
                "",
                _fact_card("Starting pitchers", game.starter_facts, styles),
            ]
        ],
        colWidths=[PAIR_CARD_WIDTH, PAIR_CARD_GAP, PAIR_CARD_WIDTH],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]),
    )
    middle_block = Table(
        [
            [
                _fact_card("Lineups", game.lineup_facts, styles),
                "",
                _fact_card("Bullpens", game.bullpen_facts, styles),
            ]
        ],
        colWidths=[PAIR_CARD_WIDTH, PAIR_CARD_GAP, PAIR_CARD_WIDTH],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]),
    )
    lower_block = Table(
        [
            [
                _fact_card("Park and weather", game.weather_facts, styles),
                "",
                _fact_card("Odds snapshot", game.odds_facts, styles),
            ]
        ],
        colWidths=[PAIR_CARD_WIDTH, PAIR_CARD_GAP, PAIR_CARD_WIDTH],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]),
    )
    story: list[object] = [
        PageBreak(),
        _paragraph(game.title, styles["h1"]),
        _paragraph(
            f"Data Quality: {game.quality_disposition.value.upper()} | Game ID: {game.source_game_id}",
            styles["subtitle"],
        ),
        top_block,
        Spacer(1, 0.06 * inch),
        middle_block,
        Spacer(1, 0.06 * inch),
        lower_block,
        _paragraph("Model outlook", styles["h2"]),
        _facts_table(game.model_facts, styles),
        _paragraph("Market decisions and why", styles["h2"]),
        _decision_detail_table(game.decisions, styles),
        _paragraph("Game analysis", styles["h2"]),
    ]
    for item in game.analysis:
        story.append(_paragraph(f"- {item}", styles["body"]))
    story.append(_paragraph("Data limitations", styles["h2"]))
    if game.limitations:
        for item in game.limitations:
            story.append(_paragraph(f"- {item}", styles["body"]))
    else:
        story.append(_paragraph("No report-level data limitations were retained.", styles["body"]))
    return story


def _american(value: float) -> str:
    return f"+{int(value)}" if value > 0 else str(int(value))


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def render_pdf_report(document: PdfReportDocumentV1) -> bytes:
    styles = _styles()
    buffer = BytesIO()
    title = f"Daily MLB Report - {document.requested_date}"
    doc = _ReportDocTemplate(buffer, title=title)
    rows_by_checksum = {row.checksum: row for row in document.full_slate_rows}
    top_rows = tuple(rows_by_checksum[item] for item in document.top_confidence_row_checksums)
    best_rows = tuple(rows_by_checksum[item] for item in document.best_value_row_checksums)

    metrics = Table(
        [
            [
                _rich(f"{document.game_count}<br/><font size='7'>Games</font>", styles["cover_metric"]),
                _rich(f"{document.row_count}<br/><font size='7'>Market sides</font>", styles["cover_metric"]),
                _rich(f"{document.actionable_count}<br/><font size='7'>Ranked candidates</font>", styles["cover_metric"]),
                _rich("PRE-REVIEW<br/><font size='7'>Report status</font>", styles["cover_metric"]),
            ]
        ],
        colWidths=[METRIC_CARD_WIDTH] * 4,
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("BOX", (0, 0), (-1, -1), 0.6, BLUE),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        ),
    )

    story: list[object] = [
        Spacer(1, 0.12 * inch),
        _paragraph("The Daily Edge", styles["subtitle"]),
        _paragraph(f"Daily MLB Report - {document.requested_date}", styles["title"]),
        _paragraph(
            f"Data cutoff: {document.as_of_time.isoformat()} | Generated: {document.generated_at.isoformat()}",
            styles["subtitle"],
        ),
        metrics,
        Spacer(1, 0.05 * inch),
        Table(
            [[_paragraph(document.responsible_use_notice, styles["callout"])]],
            colWidths=[CONTENT_WIDTH],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
                    ("BOX", (0, 0), (-1, -1), 0.6, GOLD),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
        _paragraph("Today's Ranked Board", styles["h1"]),
    ]
    story.extend(_ranked_table("Top Confidence", top_rows, styles))
    story.extend(_ranked_table("Best Value", best_rows, styles))
    story.extend(
        [
            PageBreak(),
            _paragraph("Full Slate Recommendation Snapshot", styles["h1"]),
            _paragraph(
                "Every evaluated BET, LEAN, PASS, and AVOID is retained. A ranked candidate is not yet an approved or published wager.",
                styles["subtitle"],
            ),
        ]
    )
    if document.full_slate_rows:
        story.append(_full_slate_table(document.full_slate_rows, styles))
    else:
        story.append(_paragraph("No evaluated market rows were available.", styles["body"]))
    story.extend(
        [
            _paragraph("Data Freshness and Slate Health", styles["h1"]),
            _facts_table(document.slate_health, styles),
        ]
    )
    for game in document.games:
        story.extend(_game_story(game, styles))
    story.extend(
        [
            PageBreak(),
            _paragraph("Methodology and Responsible Use", styles["h1"]),
        ]
    )
    for statement in document.methodology:
        story.append(_paragraph(f"- {statement}", styles["body"]))
    story.extend(
        [
            _paragraph("Decision legend", styles["h2"]),
            _facts_table(
                (
                    _fact_for_renderer("BET", "Meets automated policy floors; still requires Human Review."),
                    _fact_for_renderer("LEAN", "Positive actionable evidence below one or more BET floors."),
                    _fact_for_renderer("PASS", "Prediction retained, but value or evidence does not justify action."),
                    _fact_for_renderer("AVOID", "Evidence is stale, incomplete, unsafe, or otherwise unreliable."),
                ),
                styles,
            ),
            _paragraph("Audit metadata", styles["h2"]),
            _facts_table(
                (
                    _fact_for_renderer("Report checksum", document.checksum),
                    _fact_for_renderer("Rankings checksum", document.upstream_rankings_checksum),
                    _fact_for_renderer("Recommendation Gate checksum", document.upstream_recommendation_gate_checksum),
                    _fact_for_renderer("Predictions checksum", document.upstream_predictions_checksum),
                    _fact_for_renderer("MatchupPacket checksum", document.upstream_matchup_packet_checksum),
                    _fact_for_renderer("Policy checksum", document.policy.checksum),
                ),
                styles,
            ),
        ]
    )
    doc.build(story, canvasmaker=_InvariantCanvas)
    return buffer.getvalue()


def _presentation_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _presentation_sequence(value: object) -> tuple[object, ...]:
    return tuple(value) if isinstance(value, list | tuple) else ()


def _humanize(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value.replace("_", " ").strip()
    return str(value)


def _presentation_fact(
    label: str,
    value: object,
    *,
    missing: str = "Unavailable / not retained",
    status: str | None = None,
) -> ReportFactV1:
    absent = value is None or value == "" or value == () or value == []
    return ReportFactV1(
        label=label,
        value=missing if absent else _humanize(value),
        status=("unavailable" if absent else "available") if status is None else status,
    )


def _timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _player_label(value: object) -> str | None:
    player = _presentation_mapping(value)
    for key in ("full_name", "canonical_player_id", "source_player_id"):
        candidate = player.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _team_player_index(team: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _presentation_sequence(team.get("players")):
        player = _presentation_mapping(item)
        identifier = player.get("source_player_id")
        label = _player_label(player)
        if isinstance(identifier, str) and label is not None:
            result[identifier] = label
    return result


def _indexed_player_labels(team: Mapping[str, object], key: str) -> tuple[str, ...]:
    index = _team_player_index(team)
    result: list[str] = []
    for identifier in _presentation_sequence(team.get(key)):
        text = str(identifier)
        result.append(index.get(text, text))
    return tuple(result)


def _game_information_facts(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    schedule = _presentation_mapping(context.get("schedule"))
    odds_weather = _presentation_mapping(context.get("odds_weather"))
    weather = _presentation_mapping(odds_weather.get("weather"))
    venue = _presentation_mapping(weather.get("venue_context"))
    venue_name = venue.get("venue_name") or schedule.get("source_venue_name") or schedule.get("venue_id")
    start = schedule.get("scheduled_start_time") or getattr(game, "scheduled_start_time")
    return (
        _presentation_fact(
            "Matchup",
            f"{getattr(game, 'away_team_id').upper()} at {getattr(game, 'home_team_id').upper()}",
        ),
        _presentation_fact("First pitch", _timestamp(start)),
        _presentation_fact("Game status", schedule.get("game_status")),
        _presentation_fact("Venue / park", venue_name),
        _presentation_fact("Game number", schedule.get("game_number"), missing="Not applicable"),
        _presentation_fact("Doubleheader", schedule.get("doubleheader_status"), missing="Not applicable"),
        _presentation_fact("Roof type", venue.get("roof_type"), missing="Not retained"),
        _presentation_fact("Roof status", venue.get("operational_roof_status"), missing="Not retained"),
    )


def _starter_facts_for_production(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    schedule = _presentation_mapping(context.get("schedule"))
    game_state = _presentation_mapping(context.get("game_state"))
    intelligence = _presentation_mapping(context.get("baseball_intelligence"))
    facts: list[ReportFactV1] = []
    for side in ("away", "home"):
        team_id = str(getattr(game, f"{side}_team_id")).upper()
        state_team = _presentation_mapping(game_state.get(side))
        starter = _presentation_mapping(state_team.get("starter"))
        player_name = _player_label(starter.get("player"))
        schedule_starter = _presentation_mapping(schedule.get(f"{side}_probable_starter"))
        player_name = player_name or _player_label(schedule_starter)
        certainty = starter.get("certainty") or schedule_starter.get("classification")
        intelligence_team = _presentation_mapping(intelligence.get(side))
        coverage = _presentation_mapping(intelligence_team.get("coverage"))
        starter_feature = coverage.get("starter_feature_available")
        facts.extend(
            (
                _presentation_fact(f"{team_id} starter", player_name),
                _presentation_fact(f"{team_id} status", certainty),
                _presentation_fact(
                    f"{team_id} feature evidence",
                    "Available"
                    if starter_feature is True
                    else "Unavailable"
                    if starter_feature is False
                    else None,
                ),
            )
        )
    return tuple(facts)


def _lineup_facts_for_production(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    game_state = _presentation_mapping(context.get("game_state"))
    intelligence = _presentation_mapping(context.get("baseball_intelligence"))
    facts: list[ReportFactV1] = []
    for side in ("away", "home"):
        team_id = str(getattr(game, f"{side}_team_id")).upper()
        state_team = _presentation_mapping(game_state.get(side))
        lineup = _presentation_mapping(state_team.get("lineup"))
        labels: list[str] = []
        for entry_value in _presentation_sequence(lineup.get("entries")):
            entry = _presentation_mapping(entry_value)
            player = _player_label(entry.get("player"))
            slot = entry.get("batting_order_slot")
            if player is not None:
                labels.append(f"{slot}. {player}" if slot is not None else player)
        intelligence_team = _presentation_mapping(intelligence.get(side))
        if not labels:
            labels.extend(_indexed_player_labels(intelligence_team, "lineup_source_player_ids"))
        coverage = _presentation_mapping(intelligence_team.get("coverage"))
        player_count = coverage.get("lineup_player_count")
        feature_count = coverage.get("lineup_feature_count")
        coverage_text = (
            f"{feature_count}/{player_count} players"
            if feature_count is not None and player_count is not None
            else None
        )
        facts.extend(
            (
                _presentation_fact(f"{team_id} lineup status", lineup.get("availability")),
                _presentation_fact(f"{team_id} retained lineup", "; ".join(labels) or None),
                _presentation_fact(f"{team_id} feature coverage", coverage_text),
            )
        )
    return tuple(facts)


def _bullpen_facts_for_production(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    intelligence = _presentation_mapping(context.get("baseball_intelligence"))
    facts: list[ReportFactV1] = []
    for side in ("away", "home"):
        team_id = str(getattr(game, f"{side}_team_id")).upper()
        team = _presentation_mapping(intelligence.get(side))
        labels = _indexed_player_labels(team, "bullpen_source_player_ids")
        coverage = _presentation_mapping(team.get("coverage"))
        player_count = coverage.get("bullpen_player_count")
        feature_count = coverage.get("bullpen_feature_count")
        coverage_text = (
            f"{feature_count}/{player_count} players"
            if feature_count is not None and player_count is not None
            else None
        )
        facts.extend(
            (
                _presentation_fact(f"{team_id} retained bullpen", ", ".join(labels) or None),
                _presentation_fact(f"{team_id} feature coverage", coverage_text),
            )
        )
    return tuple(facts)


def _weather_facts_for_production(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    odds_weather = _presentation_mapping(context.get("odds_weather"))
    weather = _presentation_mapping(odds_weather.get("weather"))
    venue = _presentation_mapping(weather.get("venue_context"))
    primary = weather.get("primary_source")
    evidence = _presentation_mapping(weather.get(str(primary))) if isinstance(primary, str) else {}
    if not evidence:
        evidence = _presentation_mapping(weather.get("nws")) or _presentation_mapping(weather.get("openweather"))
    forecast = _presentation_mapping(evidence.get("forecast"))
    wind = _presentation_mapping(weather.get("baseball_wind_impact"))
    temperature = forecast.get("temperature_f")
    precipitation = forecast.get("precipitation_probability_pct")
    wind_speed = forecast.get("wind_speed_mph")
    wind_direction = forecast.get("wind_direction_cardinal") or forecast.get("wind_direction_deg")
    return (
        _presentation_fact("Weather status", weather.get("status")),
        _presentation_fact("Weather relevance", weather.get("relevance")),
        _presentation_fact("Primary source", primary),
        _presentation_fact("Venue", venue.get("venue_name")),
        _presentation_fact("Temperature", None if temperature is None else f"{temperature} F"),
        _presentation_fact(
            "Precipitation",
            None if precipitation is None else f"{precipitation}%",
            missing="Not retained / not applicable",
        ),
        _presentation_fact(
            "Wind",
            None if wind_speed is None else f"{wind_speed} mph ({wind_direction or 'direction unavailable'})",
            missing="Not retained / not applicable",
        ),
        _presentation_fact(
            "Field-relative wind",
            wind.get("classification") or wind.get("reason_code"),
            missing="Not retained / not applicable",
        ),
    )


def _odds_facts_for_production(game: "PdfReportGameV1") -> tuple[ReportFactV1, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    odds_weather = _presentation_mapping(context.get("odds_weather"))
    odds = _presentation_mapping(odds_weather.get("odds"))
    freshness = _presentation_mapping(odds.get("freshness_counts"))
    freshness_text = ", ".join(f"{key}={freshness[key]}" for key in sorted(freshness))
    return (
        _presentation_fact("Market", "MLB moneyline"),
        _presentation_fact("Odds availability", odds.get("availability")),
        _presentation_fact("Odds retrieved", _timestamp(odds.get("retrieved_at"))),
        _presentation_fact("Normalized markets", odds.get("normalized_market_count")),
        _presentation_fact("Raw snapshots", odds.get("raw_snapshot_count")),
        _presentation_fact("Freshness inventory", freshness_text or None),
    )


def _quality_issue_lines(game: "PdfReportGameV1") -> tuple[str, ...]:
    context = _presentation_mapping(getattr(game, "context"))
    quality = _presentation_mapping(context.get("data_quality"))
    lines: list[str] = []
    for issue_value in _presentation_sequence(quality.get("issues")):
        issue = _presentation_mapping(issue_value)
        code = issue.get("code")
        severity = issue.get("severity")
        message = issue.get("message")
        if code is not None:
            prefix = f"{_humanize(severity).upper()}: " if severity is not None else ""
            lines.append(f"{prefix}{_humanize(code)}" + (f" - {_humanize(message)}" if message else ""))
    if not lines:
        lines.extend(_humanize(code) for code in getattr(game, "quality_issue_codes"))
    return tuple(dict.fromkeys(lines))


def _production_decision_fill(decision: str) -> colors.Color:
    return {"recommend": PALE_GREEN, "pass": PALE_GRAY, "avoid": PALE_RED}[decision]


def _production_recommendations_table(
    recommendations: tuple["PdfReportGameV1", ...], styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[object]] = [
        _header_cells(("Rank", "Matchup", "Selected side", "Prob.", "Edge", "EV", "Best price", "Books"), styles)
    ]
    for game in recommendations:
        selected = next(outcome for outcome in getattr(game, "outcomes") if outcome.side == game.selected_side)
        data.append(
            [
                _paragraph(str(game.recommendation_rank), styles["small_center"]),
                _paragraph(f"{game.away_team_id.upper()} at {game.home_team_id.upper()}", styles["small"]),
                _paragraph(str(game.selected_team_id).upper(), styles["small"]),
                _paragraph(_percent(selected.prediction_probability), styles["small_center"]),
                _paragraph(_percent(selected.edge), styles["small_center"]),
                _paragraph(
                    "-" if selected.expected_value_per_unit is None else f"{selected.expected_value_per_unit:+.3f}",
                    styles["small_center"],
                ),
                _paragraph("-" if selected.best_price is None else _american(selected.best_price), styles["small_center"]),
                _paragraph(str(selected.bookmaker_count), styles["small_center"]),
            ]
        )
    return Table(
        data,
        colWidths=[
            0.42 * inch,
            2.35 * inch,
            1.93 * inch,
            0.65 * inch,
            0.55 * inch,
            0.55 * inch,
            0.80 * inch,
            0.45 * inch,
        ],
        repeatRows=1,
        hAlign="LEFT",
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_BLUE]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        ),
    )


def _production_full_slate_table(
    games: tuple["PdfReportGameV1", ...], styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[object]] = [_header_cells(("#", "Matchup", "Start", "Decision", "Rank", "Quality"), styles)]
    for game in games:
        start = getattr(game, "scheduled_start_time")
        start_text = "Not retained" if start is None else start.strftime("%Y-%m-%d %H:%M UTC")
        data.append(
            [
                _paragraph(str(game.ordinal), styles["small_center"]),
                _paragraph(f"{game.away_team_id.upper()} at {game.home_team_id.upper()}", styles["small"]),
                _paragraph(start_text, styles["small_center"]),
                _paragraph(game.decision.upper(), styles["small_center"]),
                _paragraph("-" if game.recommendation_rank is None else str(game.recommendation_rank), styles["small_center"]),
                _paragraph(game.quality_disposition.upper(), styles["small_center"]),
            ]
        )
    commands: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_number, game in enumerate(games, start=1):
        commands.append(("BACKGROUND", (0, row_number), (-1, row_number), _production_decision_fill(game.decision)))
    return Table(
        data,
        colWidths=[0.35 * inch, 2.65 * inch, 1.35 * inch, 0.85 * inch, 0.55 * inch, 1.95 * inch],
        repeatRows=1,
        hAlign="LEFT",
        style=TableStyle(commands),
    )


def _production_outcome_table(
    game: "PdfReportGameV1", styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[object]] = [
        _header_cells(("Side", "Prob.", "Interval", "No-vig", "Edge", "EV", "Best price", "Books"), styles)
    ]
    for outcome in game.outcomes:
        data.append(
            [
                _paragraph(outcome.outcome_team_id.upper(), styles["small"]),
                _paragraph(_percent(outcome.prediction_probability), styles["small_center"]),
                _paragraph(f"{_percent(outcome.probability_lower)} - {_percent(outcome.probability_upper)}", styles["small_center"]),
                _paragraph(_percent(outcome.consensus_no_vig_probability), styles["small_center"]),
                _paragraph(_percent(outcome.edge), styles["small_center"]),
                _paragraph("-" if outcome.expected_value_per_unit is None else f"{outcome.expected_value_per_unit:+.3f}", styles["small_center"]),
                _paragraph("-" if outcome.best_price is None else _american(outcome.best_price), styles["small_center"]),
                _paragraph(str(outcome.bookmaker_count), styles["small_center"]),
            ]
        )
    return Table(
        data,
        colWidths=[2.90 * inch, 0.65 * inch, 1.10 * inch, 0.65 * inch, 0.55 * inch, 0.55 * inch, 0.85 * inch, 0.45 * inch],
        repeatRows=1,
        hAlign="LEFT",
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_BLUE]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        ),
    )


def _paired_fact_cards(
    left_title: str,
    left_facts: tuple[ReportFactV1, ...],
    right_title: str,
    right_facts: tuple[ReportFactV1, ...],
    styles: dict[str, ParagraphStyle],
) -> Table:
    return Table(
        [[_fact_card(left_title, left_facts, styles), "", _fact_card(right_title, right_facts, styles)]],
        colWidths=[PAIR_CARD_WIDTH, PAIR_CARD_GAP, PAIR_CARD_WIDTH],
        hAlign="LEFT",
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )


def _production_dossier_story(
    game: "PdfReportGameV1", styles: dict[str, ParagraphStyle]
) -> list[object]:
    issues = _quality_issue_lines(game)
    decision_facts = (
        _presentation_fact("Decision", game.decision.upper()),
        _presentation_fact(
            "Recommendation rank",
            game.recommendation_rank,
            missing="Not ranked (PASS / AVOID)",
        ),
        _presentation_fact("Selected side", None if game.selected_side is None else game.selected_side.upper(), missing="Not applicable"),
        _presentation_fact("Selected team", game.selected_team_id, missing="Not applicable"),
        _presentation_fact("Quality disposition", game.quality_disposition.upper()),
    )
    provider_facts = (
        _presentation_fact("Prediction provider", game.provider_kind),
        _presentation_fact("Provider contract", game.provider_contract),
        _presentation_fact("Provider version", game.provider_version),
        _presentation_fact("Calibration", game.calibration_state),
        _presentation_fact(
            "Market independence",
            "Attested" if game.market_independence_attested else "Not attested",
            status="available" if game.market_independence_attested else "degraded",
        ),
    )
    story: list[object] = [
        PageBreak(),
        _paragraph(f"{game.away_team_id.upper()} at {game.home_team_id.upper()}", styles["h1"]),
        _paragraph(
            f"Game {game.ordinal} | Game ID: {game.source_game_id} | {game.decision.upper()} | Quality: {game.quality_disposition.upper()}",
            styles["subtitle"],
        ),
        _card(
            [
                _paragraph(
                    f"{game.decision.upper()}" + (
                        f" - Recommendation #{game.recommendation_rank}: {str(game.selected_team_id).upper()}"
                        if game.recommendation_rank is not None
                        else " - retained analytical result; not a recommendation"
                    ),
                    styles["callout"],
                )
            ],
            bg=_production_decision_fill(game.decision),
        ),
        Spacer(1, 0.06 * inch),
        _paired_fact_cards(
            "Game information",
            _game_information_facts(game),
            "Starting pitchers",
            _starter_facts_for_production(game),
            styles,
        ),
        Spacer(1, 0.06 * inch),
        _paired_fact_cards(
            "Lineups",
            _lineup_facts_for_production(game),
            "Bullpens",
            _bullpen_facts_for_production(game),
            styles,
        ),
        Spacer(1, 0.06 * inch),
        _paired_fact_cards(
            "Park and weather",
            _weather_facts_for_production(game),
            "Odds snapshot / market context",
            _odds_facts_for_production(game),
            styles,
        ),
        _paragraph("Prediction and Value evidence", styles["h2"]),
        _production_outcome_table(game, styles),
        _paragraph("Recommendation Gate decision", styles["h2"]),
        _facts_table(decision_facts, styles),
        _paragraph("Evidence summary and limitations", styles["h2"]),
        _paired_fact_cards(
            "Prediction evidence",
            provider_facts,
            "Data Quality evidence",
            (
                _presentation_fact("Disposition", game.quality_disposition.upper()),
                _presentation_fact("Retained issue count", len(issues)),
                _presentation_fact("Retained issues", "; ".join(issues) or "No retained quality issues"),
            ),
            styles,
        ),
        _paragraph(
            "This dossier reports retained deterministic evidence only. It does not recalculate Value, rerank recommendations, or approve publication.",
            styles["body"],
        ),
    ]
    return story


def _production_pdf_story(
    document: "ProductionPdfReportV1", styles: dict[str, ParagraphStyle]
) -> tuple[list[object], tuple[str, ...]]:
    recommendations = tuple(
        sorted(
            (game for game in document.games if game.recommendation_rank is not None),
            key=lambda game: game.recommendation_rank or 0,
        )
    )
    story: list[object] = [
        Spacer(1, 0.12 * inch),
        _paragraph("The Daily Edge", styles["subtitle"]),
        _paragraph(f"Daily MLB Report - {document.requested_date}", styles["title"]),
        _paragraph(
            f"Data cutoff: {document.as_of_time.isoformat()} | Generated: {document.generated_at.isoformat()}",
            styles["subtitle"],
        ),
        Table(
            [[
                _rich(f"{len(document.games)}<br/><font size='7'>Games</font>", styles["cover_metric"]),
                _rich(f"{len(recommendations)}<br/><font size='7'>Recommendations</font>", styles["cover_metric"]),
                _rich("ML<br/><font size='7'>V1 market</font>", styles["cover_metric"]),
                _rich("PRE-REVIEW<br/><font size='7'>Report status</font>", styles["cover_metric"]),
            ]],
            colWidths=[METRIC_CARD_WIDTH] * 4,
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                    ("BOX", (0, 0), (-1, -1), 0.6, BLUE),
                    ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
        _paragraph(
            "Pre-review analytical output. RECOMMEND, PASS, and AVOID preserve the exact Recommendation Gate decision; Human Review is still required.",
            styles["callout"],
        ),
        _paragraph("Today's Ranked Recommendations", styles["h1"]),
    ]
    if recommendations:
        story.append(_production_recommendations_table(recommendations, styles))
    else:
        story.append(
            _card([_paragraph("No canonical RECOMMEND entries were retained for this slate.", styles["body"])])
        )
    story.extend([PageBreak(), _paragraph("Full Slate", styles["h1"])])
    if document.games:
        story.append(_production_full_slate_table(tuple(document.games), styles))
    else:
        story.append(
            _card([_paragraph("The sealed upstream slate contains zero games.", styles["body"])])
        )
    rendered_game_ids: list[str] = []
    for game in document.games:
        story.extend(_production_dossier_story(game, styles))
        rendered_game_ids.append(game.source_game_id)
    story.extend(
        [
            PageBreak(),
            _paragraph("Methodology and Audit", styles["h1"]),
            _paragraph(
                "Predictions are market-blind and distinct from Value, Recommendation Gate, Rankings, report display, and Human Review.",
                styles["body"],
            ),
            _paragraph("V1 covers MLB moneyline only. Spread and total predictions are not fabricated.", styles["body"]),
            _paragraph(
                "RECOMMEND is eligible for Human Review. PASS and AVOID remain retained analytical evidence.",
                styles["body"],
            ),
            _paragraph("Decision legend", styles["h2"]),
            _facts_table(
                (
                    _fact_for_renderer("RECOMMEND", "Eligible for Human Review under the retained Recommendation Gate evidence."),
                    _fact_for_renderer("PASS", "Prediction and Value evidence are retained, but the opportunity gates did not support recommendation."),
                    _fact_for_renderer("AVOID", "Prediction and Value evidence are retained, but risk or evidence-quality gates did not support recommendation."),
                ),
                styles,
            ),
            _paragraph("Audit metadata", styles["h2"]),
            _facts_table(
                tuple(
                    _fact_for_renderer(key.replace("_", " ").title(), value)
                    for key, value in document.upstream_checksums.items()
                )
                + (
                    _fact_for_renderer("Semantic report checksum", document.checksum),
                    _fact_for_renderer("Policy checksum", document.policy.checksum),
                    _fact_for_renderer("Report contract", document.contract_version),
                    _fact_for_renderer("Render version", document.render_version),
                ),
                styles,
            ),
        ]
    )
    return story, tuple(rendered_game_ids)


def render_production_pdf_report(document: "ProductionPdfReportV1") -> bytes:
    """Render every v13-native game using the accepted customer dossier system."""
    styles = _styles()
    buffer = BytesIO()
    doc = _ReportDocTemplate(buffer, title=f"Daily MLB Report - {document.requested_date}")
    story, rendered_game_ids = _production_pdf_story(document, styles)
    if rendered_game_ids != tuple(game.source_game_id for game in document.games):
        raise ValueError("production PDF dossier inventory does not match the retained slate")
    doc.build(story, canvasmaker=_InvariantCanvas)
    return buffer.getvalue()


def _fact_for_renderer(label: str, value: str) -> ReportFactV1:
    return ReportFactV1(label=label, value=value)
