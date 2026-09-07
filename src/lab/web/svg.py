"""Кривая капитала как inline-SVG без внешних библиотек (R13.2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

W, H = 640, 220
PAD_L, PAD_R, PAD_T, PAD_B = 56, 12, 12, 24


def equity_svg(points: list[tuple[datetime, Decimal]], *, width: int = W, height: int = H) -> str:
    """Полилиния накопленного P&L; нулевая линия; подписи min/max/последнего значения.

    Пустой список → пустая строка (шаблон покажет текст, а не пустую картинку).
    """
    if not points:
        return ""
    values = [Decimal(0), *[v for _, v in points]]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + Decimal(1)
    inner_w = width - PAD_L - PAD_R
    inner_h = height - PAD_T - PAD_B
    n = len(points)

    def x(i: int) -> float:
        return PAD_L + (inner_w * i / max(1, n))

    def y(v: Decimal) -> float:
        return PAD_T + float((hi - v) / (hi - lo)) * inner_h

    coords = [(x(0), y(Decimal(0)))] + [(x(i + 1), y(v)) for i, (_, v) in enumerate(points)]
    path = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
    zero_y = y(Decimal(0))
    last = points[-1][1]
    trend = "up" if last >= 0 else "down"
    first_ts, last_ts = points[0][0], points[-1][0]
    out = [
        f'<svg class="equity {trend}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Кривая капитала: {n} сделок, итог {last:.2f} USD" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<line class="zero" x1="{PAD_L}" y1="{zero_y:.1f}" x2="{width - PAD_R}" '
        f'y2="{zero_y:.1f}"/>',
        f'<polyline class="curve" fill="none" points="{path}"/>',
        f'<text class="tick" x="4" y="{PAD_T + 10}">{hi:.0f}</text>',
        f'<text class="tick" x="4" y="{height - PAD_B}">{lo:.0f}</text>',
        f'<text class="tick" x="{PAD_L}" y="{height - 6}">{escape(first_ts.strftime("%d.%m.%y"))}'
        "</text>",
        f'<text class="tick" text-anchor="end" x="{width - PAD_R}" y="{height - 6}">'
        f'{escape(last_ts.strftime("%d.%m.%y"))}</text>',
        f'<text class="last" text-anchor="end" x="{width - PAD_R}" y="{PAD_T + 10}">'
        f"{last:+.2f} USD</text>",
        "</svg>",
    ]
    return "".join(out)
