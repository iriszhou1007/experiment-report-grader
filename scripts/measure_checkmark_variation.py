"""Measure how much a checkmark drawing routine varies, so 'too uniform' can be quantified."""

from __future__ import annotations

import math
import random
import statistics as st
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_marking import draw_review_checkmark, make_page_rng  # noqa: E402

W, H = 521.7, 737.1


def sample(seed: int) -> dict | None:
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    draw_review_checkmark(page, make_page_rng(random.Random(seed), 3))
    strokes = [
        (item[1:], drawing.get("width"))
        for drawing in page.get_drawings()
        for item in drawing.get("items", [])
        if item[0] == "c"
    ]
    doc.close()
    if len(strokes) != 2:
        return None

    (p0, a1, a2, p3), stroke_width = strokes[0]
    (q0, b1, b2, q3), _ = strokes[1]

    def key(point):
        return (round(point.x, 3), round(point.y, 3))

    if key(p3) == key(q0):
        pivot, left_outer, right_outer = p3, p0, q3
    elif key(p0) == key(q3):
        pivot, left_outer, right_outer = p0, p3, q0
    elif key(p0) == key(q0):
        pivot, left_outer, right_outer = p0, p3, q3
    else:
        return None

    xs = [p.x for p in (p0, a1, a2, p3, q0, b1, b2, q3)]
    ys = [p.y for p in (p0, a1, a2, p3, q0, b1, b2, q3)]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    left_span, right_span = pivot.x - left_outer.x, right_outer.x - pivot.x
    left_drop, right_rise = pivot.y - left_outer.y, pivot.y - right_outer.y
    return {
        "width": width,
        "height": height,
        "aspect": width / height,
        "left_span": left_span,
        "right_span": right_span,
        "arm_ratio": left_span / right_span,
        "left_angle": math.degrees(math.atan2(left_drop, max(left_span, 1e-6))),
        "right_angle": math.degrees(math.atan2(right_rise, max(right_span, 1e-6))),
        "included": math.degrees(
            math.atan2(left_drop, max(left_span, 1e-6))
            + math.atan2(right_rise, max(right_span, 1e-6))
        ),
        "pivot_share": left_span / (left_span + right_span),
        "stroke_width": stroke_width,
        "width_ratio": width / W,
        "height_ratio": height / H,
    }


def report(rows: list[dict], keys: tuple[str, ...], label: str) -> None:
    print(f"样本 {len(rows)} 个（{label}）")
    for key in keys:
        values = [row[key] for row in rows]
        mean = st.mean(values)
        sd = st.pstdev(values)
        print(
            f"  {key:13} 均值{mean:8.2f}  标准差{sd:6.2f}  变异系数{sd / mean * 100:5.1f}%"
            f"  范围[{min(values):.2f}, {max(values):.2f}]"
            f"  极差/均值{(max(values) - min(values)) / mean * 100:5.1f}%"
        )


def main() -> None:
    rows = [row for row in (sample(seed) for seed in range(400)) if row]
    report(
        rows,
        (
            "width",
            "height",
            "aspect",
            "left_span",
            "right_span",
            "arm_ratio",
            "left_angle",
            "right_angle",
            "included",
            "pivot_share",
            "stroke_width",
        ),
        f"页 {W}x{H}",
    )


if __name__ == "__main__":
    main()
