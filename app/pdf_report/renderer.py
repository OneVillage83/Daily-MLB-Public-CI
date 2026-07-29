from __future__ import annotations

from html import escape
from io import BytesIO

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


def _fact_for_renderer(label: str, value: str) -> ReportFactV1:
    return ReportFactV1(label=label, value=value)
