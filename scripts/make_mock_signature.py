"""Generate a MOCK teacher-signature template for TESTING ONLY.

The skill's approved write-back copies the teacher's signature as a PDF `Ink` annotation
from a template PDF. That template is a per-machine asset belonging to a real person and is
deliberately not bundled — this script exists only so the pipeline can be exercised
end-to-end on a machine that does not have the real one yet.

It fabricates the character 张 as a set of stroke polylines (bezier-curved ink) and places
them to the RIGHT of the 指导教师签名： label, because review_marking.copy_signature_ink
only copies ink whose centre x sits at/after the label's right edge; anything to the left is
treated as the template's fixed score ink and skipped.

The template must also carry both labels — 成绩评定： and 指导教师签名： — on the same page,
because review_label_rects resolves the copy offset from the difference between the template's
label positions and the target report's.

Output: a single-page PDF, labels + one Ink annotation.

Usage:
    python scripts/make_mock_signature.py --out assets/signature/signature.pdf
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

PAGE_SIZE = (521.64, 737.10)
LABEL_FONT_SIZE = 10.5

# Label placement mirrors the 四川大学 experiment-report form closely enough that
# review_label_rects finds both labels on one page.
SCORE_LABEL = "成绩评定："
SIGNATURE_LABEL = "指导教师签名："
SCORE_LABEL_XY = (139.3, 676.9)
SIGNATURE_LABEL_XY = (317.8, 676.9)

INK_COLOR = (0.78, 0.13, 0.11)


def _stroke_outline(points: list[tuple[float, float]], width: float) -> list[list[tuple[float, float]]]:
    """Turn a polyline centre-line into a closed ribbon, so the ink reads as a brush stroke.

    PyMuPDF's add_ink_annot takes a list of subpaths and strokes them; giving it the
    centre-line alone yields a hairline. Offsetting to both sides and closing the ends makes
    the stroke carry the width a handwritten mark would.
    """
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for index, (x, y) in enumerate(points):
        if index == 0:
            nx, ny = points[1][0] - x, points[1][1] - y
        elif index == len(points) - 1:
            nx, ny = x - points[index - 1][0], y - points[index - 1][1]
        else:
            nx = points[index + 1][0] - points[index - 1][0]
            ny = points[index + 1][1] - points[index - 1][1]
        length = (nx * nx + ny * ny) ** 0.5 or 1.0
        # Normal to the local direction, scaled to half the desired stroke width.
        ox, oy = -ny / length * width / 2, nx / length * width / 2
        left.append((x + ox, y + oy))
        right.append((x - ox, y - oy))
    return [left + right[::-1] + [left[0]]]


def _bezier(p0, p1, p2, p3, steps: int = 28) -> list[tuple[float, float]]:
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
        out.append((x, y))
    return out


def zhang_strokes(origin_x: float, origin_y: float, size: float):
    """Stroke centre-lines for 张, drawn in a relaxed handwriting hand.

    Coordinates are authored in a 100x100 box then scaled. Only shape matters here: this is
    a mock, and the point is that it is a multi-stroke curved mark, not that it is a
    calligraphically faithful 张.
    """
    def s(x: float, y: float) -> tuple[float, float]:
        return (origin_x + x / 100 * size, origin_y + y / 100 * size)

    strokes: list[list[tuple[float, float]]] = []

    # 弓 radical, left side: three horizontal-ish strokes with a connecting bend.
    strokes.append(_bezier(s(8, 18), s(10, 22), s(22, 24), s(34, 26)))
    strokes.append(_bezier(s(10, 40), s(12, 43), s(24, 45), s(36, 46)))
    strokes.append(_bezier(s(12, 62), s(14, 66), s(28, 68), s(40, 70)))

    # The vertical spine of 弓, bowed.
    strokes.append(_bezier(s(34, 20), s(28, 38), s(30, 52), s(40, 72)))

    # 长 radical, right side: a long rising sweep.
    strokes.append(_bezier(s(48, 30), s(58, 24), s(70, 20), s(90, 16), steps=36))

    # The descending diagonal that crosses it.
    strokes.append(_bezier(s(60, 8), s(58, 30), s(62, 52), s(74, 78), steps=36))

    # The lower-right flick.
    strokes.append(_bezier(s(62, 56), s(66, 66), s(78, 72), s(92, 74)))

    return strokes


def build(out_path: Path, size: float = 62.0) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])

    # The two labels the write-back resolves the copy offset from.
    page.insert_text(
        SCORE_LABEL_XY, SCORE_LABEL,
        fontsize=LABEL_FONT_SIZE, fontname="china-s",
    )
    signature_label_rect_placeholder = page.insert_text(
        SIGNATURE_LABEL_XY, SIGNATURE_LABEL,
        fontsize=LABEL_FONT_SIZE, fontname="china-s",
    )

    # Locate the signature label as the skill does, so the ink lands measurably right of it.
    label_rect = page.search_for(SIGNATURE_LABEL)
    if not label_rect:
        raise SystemExit("mock template: could not locate 指导教师签名： after inserting it")
    anchor = label_rect[0]

    # Start the ink right of the label's right edge; the copy step requires the annotation's
    # centre x to be >= that edge minus a 4pt tolerance. A generous gap keeps the strokes
    # clear of the printed label instead of crowding it.
    origin_x = anchor.x1 + 22
    origin_y = anchor.y0 - 40

    for centre_line in zhang_strokes(origin_x, origin_y, size):
        page.add_ink_annot([centre_line])

    annotations = list(page.annots() or [])
    if not annotations:
        raise SystemExit("mock template: no ink annotation was created")

    # add_ink_annot takes geometry only; colour and width are set on the annotation after.
    for annot in annotations:
        annot.set_colors(stroke=INK_COLOR)
        annot.set_border(width=2.4)
        annot.update()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out_path), garbage=3, deflate=True)
    document.close()

    verify = pymupdf.open(str(out_path))
    ink_count = sum(1 for a in (verify[0].annots() or []) if a.type[1] == "Ink")
    labels_ok = bool(verify[0].search_for(SIGNATURE_LABEL)) and bool(verify[0].search_for(SCORE_LABEL))
    verify.close()

    print(f"wrote {out_path}")
    print(f"  ink annotations: {ink_count}")
    print(f"  both labels present: {labels_ok}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="assets/signature/signature.pdf")
    parser.add_argument("--size", type=float, default=62.0, help="signature glyph size in points")
    args = parser.parse_args()
    build(Path(args.out), args.size)


if __name__ == "__main__":
    main()
