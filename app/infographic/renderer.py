from __future__ import annotations

from html import escape
from textwrap import wrap

from app.infographic.contracts import InfographicDocumentV1, InfographicSelectionV1, InfographicVariantType


def _text(x: int, y: int, value: str, *, size: int, fill: str, weight: int = 400, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(value)}</text>'


def _multiline(
    x: int, y: int, value: str, *, size: int, fill: str, max_chars: int, max_lines: int, line_height: int
) -> str:
    lines = wrap(value, width=max_chars, break_long_words=False, break_on_hyphens=False) or [""]
    clipped = lines[:max_lines]
    if len(lines) > max_lines:
        clipped[-1] = clipped[-1].rstrip(" .") + "…"
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else line_height}">{escape(line)}</tspan>'
        for index, line in enumerate(clipped)
    )
    return f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" fill="{fill}">{spans}</text>'


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"+{int(value)}" if value > 0 else str(int(value))


def _card(
    document: InfographicDocumentV1,
    card: InfographicSelectionV1,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    label: str,
    prominent: bool,
) -> str:
    policy = document.policy
    title_size = 34 if prominent else 24
    parts = [
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="24" fill="{policy.panel_hex}" stroke="#22344D" stroke-width="2"/>',
        f'<rect x="{x}" y="{y}" width="12" height="{height}" rx="6" fill="{policy.recommend_hex}"/>',
        _text(x + 30, y + 39, label.upper(), size=16, fill=policy.accent_hex, weight=700),
        _text(x + width - 28, y + 39, "RECOMMEND", size=16, fill=policy.recommend_hex, weight=700, anchor="end"),
        _text(x + 30, y + 84, card.matchup_label, size=title_size, fill=policy.primary_text_hex, weight=700),
        _text(
            x + 30,
            y + 124,
            f"{card.selected_team_id.upper()} MONEYLINE",
            size=24,
            fill=policy.secondary_text_hex,
            weight=600,
        ),
        _text(
            x + width - 28,
            y + 124,
            _price(card.best_price),
            size=26,
            fill=policy.primary_text_hex,
            weight=700,
            anchor="end",
        ),
    ]
    metrics = (
        ("PRED", _pct(card.prediction_probability)),
        ("MARKET", _pct(card.market_probability)),
        ("EDGE", _pct(card.edge)),
        ("EV/$1", "—" if card.expected_value_per_unit is None else f"{card.expected_value_per_unit:+.3f}"),
    )
    metric_width = (width - 60) // 4
    for index, (label_text, value) in enumerate(metrics):
        mx = x + 30 + index * metric_width
        parts.extend(
            (
                _text(mx, y + 174, label_text, size=13, fill=policy.secondary_text_hex, weight=700),
                _text(mx, y + 202, value, size=21, fill=policy.primary_text_hex, weight=700),
            )
        )
    parts.append(
        _text(
            x + 30,
            y + height - 28,
            f"Rank #{card.recommendation_rank} | {card.bookmaker_count} eligible books | {card.quality_disposition.upper()}",
            size=16,
            fill=policy.secondary_text_hex,
            weight=600,
        )
    )
    return "".join(parts)


def render_infographic_svg(document: InfographicDocumentV1, variant: InfographicVariantType) -> bytes:
    selected = next(item for item in document.variants if item.variant is variant)
    p = document.policy
    width = selected.width
    height = selected.height
    margin = 54
    content = width - 2 * margin
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f"<!-- infographic_checksum={document.checksum} variant={variant.value} -->",
        f'<rect width="{width}" height="{height}" fill="{p.background_hex}"/>',
        f'<circle cx="{width - 90}" cy="72" r="140" fill="#0D2D4E" opacity="0.55"/>',
        _text(margin, 66, "THE DAILY EDGE", size=20, fill=p.accent_hex, weight=700),
        _text(margin, 115, "TODAY'S MLB RECOMMENDATIONS", size=39, fill=p.primary_text_hex, weight=800),
        _text(margin, 150, document.requested_date, size=20, fill=p.secondary_text_hex, weight=600),
        _text(width - margin, 66, "DAILY MLB", size=20, fill=p.primary_text_hex, weight=700, anchor="end"),
        _text(width - margin, 150, "PRE-REVIEW", size=16, fill="#F59E0B", weight=700, anchor="end"),
    ]
    cursor = 190
    if selected.pick_of_day is None:
        parts.extend(
            (
                f'<rect x="{margin}" y="{cursor}" width="{content}" height="180" rx="24" fill="{p.panel_hex}"/>',
                _text(margin + 30, cursor + 50, "PICK OF THE DAY", size=17, fill=p.accent_hex, weight=700),
                _text(
                    margin + 30,
                    cursor + 105,
                    "No canonical RECOMMEND entry",
                    size=29,
                    fill=p.primary_text_hex,
                    weight=700,
                ),
                _text(
                    margin + 30,
                    cursor + 145,
                    "PASS and AVOID remain in the full report.",
                    size=17,
                    fill=p.secondary_text_hex,
                ),
            )
        )
        cursor += 210
    else:
        parts.append(
            _card(
                document,
                selected.pick_of_day,
                x=margin,
                y=cursor,
                width=content,
                height=290,
                label="Pick of the Day",
                prominent=True,
            )
        )
        cursor += 320
    for card in selected.recommendations:
        parts.append(
            _card(
                document,
                card,
                x=margin,
                y=cursor,
                width=content,
                height=235,
                label=f"Recommendation #{card.recommendation_rank}",
                prominent=False,
            )
        )
        cursor += 255
    weather_y = min(cursor, height - 270)
    parts.extend(
        (
            f'<rect x="{margin}" y="{weather_y}" width="{content}" height="118" rx="20" fill="#0D2D4E"/>',
            _text(margin + 24, weather_y + 34, "WEATHER WATCH", size=16, fill=p.accent_hex, weight=700),
            _text(margin + 24, weather_y + 68, selected.weather_headline, size=21, fill=p.primary_text_hex, weight=700),
            _multiline(
                margin + 24,
                weather_y + 95,
                selected.weather_detail,
                size=15,
                fill=p.secondary_text_hex,
                max_chars=94,
                max_lines=1,
                line_height=20,
            ),
        )
    )
    footer = height - 116
    parts.extend(
        (
            f'<rect x="0" y="{footer}" width="{width}" height="116" fill="#0B1320"/>',
            _text(margin, footer + 38, "FULL RESEARCH", size=15, fill=p.accent_hex, weight=700),
            _text(margin, footer + 70, selected.report_cta, size=16, fill=p.primary_text_hex, weight=600),
            _text(
                margin,
                footer + 97,
                "Human Review is still required. No publication approval is implied.",
                size=14,
                fill=p.secondary_text_hex,
            ),
            _text(
                width - margin,
                footer + 72,
                "THE DAILY EDGE",
                size=20,
                fill=p.primary_text_hex,
                weight=800,
                anchor="end",
            ),
            "</svg>",
        )
    )
    return "".join(parts).encode("utf-8")
