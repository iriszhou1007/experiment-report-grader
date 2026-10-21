"""Draw the teacher's review marks on a report: red checkmarks, signature ink, and the score.

Vendored from https://github.com/jamesz-dev/hw-worker (``scripts/review_marking.py``, commit
18056eb) so this skill stays self-contained, with three changes:

1. Label lookup merges every search hit on the same text line instead of trusting the first
   one. On a report exported at double page size PyMuPDF splits ``成绩评定：`` into two
   overlapping rects, and the first rect alone ends before the label does.
2. Only the signature ink is copied from the signature template. The template's handwritten
   score is a fixed ``100``, so the real graded score is written instead (``write_score``).
3. The review page must be found from the labels. A form without them raises rather than
   guessing, because guessing would stamp marks onto the wrong page of an official document.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import fitz

from writeback_paths import DEFAULT_SCORE_FONT

SCORE_LABEL_VARIANTS = ("成绩评定：", "成绩评定")
SIGNATURE_LABEL_VARIANTS = ("指导教师签名：", "指导教师签名")
INK_ANNOT_TYPE = "Ink"

# Bradley Hand is a handwriting face shipped with macOS; a typed number in a text face
# would look stamped onto an otherwise handwritten page. DEFAULT_SCORE_FONT is re-exported
# from writeback_paths above.
# Cap height as a fraction of the em, measured by rendering the digits.
DIGIT_CAP_RATIO = 0.52
# Descender depth of the digits that fall below the baseline ("9", "4" in this face),
# also measured. Used to keep the ink inside the review cell.
DIGIT_DESCENDER_RATIO = 0.29


# Checkmark safe zone as a fraction of the page, matching hw-worker's validator.
SAFE_ZONE = (0.25, 0.35, 0.78, 0.82)
# Where the mark's centre may sit, as a fraction of the page.
MARK_CENTRE_X = (0.40, 0.58)
MARK_CENTRE_Y = (0.50, 0.68)
# Mark size as a fraction of the safe zone. Deliberately wide: a hand-drawn tick is never the
# same size twice, which is what makes a batch look marked rather than stamped.
MARK_WIDTH_SHARE = (0.33, 0.65)
MARK_HEIGHT_SHARE = (0.40, 0.84)


def _fit_into_zone(points: list[fitz.Point], zone: fitz.Rect, margin: float) -> list[fitz.Point]:
    """Scale and shift a mark so its control hull sits inside the zone.

    Two things make this necessary. Endpoints alone do not bound a bezier: a control point can
    pull the stroke outside the box its endpoints describe, so the hull is what gets bounded.
    And a drawing's reported box includes half the stroke width on every side, so the zone is
    deflated by that much first — otherwise a mark whose hull lands exactly on the edge is
    reported as outside it.
    """
    inner = fitz.Rect(zone.x0 + margin, zone.y0 + margin, zone.x1 - margin, zone.y1 - margin)

    def hull(items: list[fitz.Point]) -> fitz.Rect:
        return fitz.Rect(
            min(point.x for point in items),
            min(point.y for point in items),
            max(point.x for point in items),
            max(point.y for point in items),
        )

    box = hull(points)
    if box.is_empty:
        return points

    scale = min(1.0, inner.width / box.width, inner.height / box.height)
    if scale < 1.0:
        centre_x = (box.x0 + box.x1) / 2
        centre_y = (box.y0 + box.y1) / 2
        points = [
            fitz.Point(
                centre_x + (point.x - centre_x) * scale,
                centre_y + (point.y - centre_y) * scale,
            )
            for point in points
        ]
        box = hull(points)

    if box.x0 < inner.x0:
        dx = inner.x0 - box.x0
    elif box.x1 > inner.x1:
        dx = inner.x1 - box.x1
    else:
        dx = 0.0
    if box.y0 < inner.y0:
        dy = inner.y0 - box.y0
    elif box.y1 > inner.y1:
        dy = inner.y1 - box.y1
    else:
        dy = 0.0
    if dx or dy:
        points = [fitz.Point(point.x + dx, point.y + dy) for point in points]
    return points


def draw_review_checkmark(page: fitz.Page, rng: random.Random) -> None:
    """Draw one red checkmark whose size, proportions and stroke vary from page to page.

    The mark is built from a bounding box plus shape parameters rather than from jittered
    template points. Jittering the endpoints of a fixed template leaves the overall size and
    the long arm's angle nearly constant — measured at 4-5% and 2% coefficient of variation —
    which is what made a batch look machine-made. Here the box, where the vertex sits along it,
    and how steeply the short arm falls are drawn independently, so the long arm's angle
    follows from them instead of being fixed.

    The result stays inside the safe zone and inside the two-stroke open geometry that
    hw-worker's validate_checkmarks.py accepts.
    """
    rect = page.rect
    zone = fitz.Rect(
        rect.width * SAFE_ZONE[0],
        rect.height * SAFE_ZONE[1],
        rect.width * SAFE_ZONE[2],
        rect.height * SAFE_ZONE[3],
    )

    width = min(zone.width * rng.uniform(*MARK_WIDTH_SHARE), zone.width)
    height = min(zone.height * rng.uniform(*MARK_HEIGHT_SHARE), zone.height)

    # Keep the box inside the zone whatever size and centre were drawn.
    centre_x = rect.width * rng.uniform(*MARK_CENTRE_X)
    centre_y = rect.height * rng.uniform(*MARK_CENTRE_Y)
    x0 = min(max(centre_x - width / 2, zone.x0), zone.x1 - width)
    y0 = min(max(centre_y - height / 2, zone.y0), zone.y1 - height)
    x1, y1 = x0 + width, y0 + height

    # The vertex sits somewhere along the width, and the short arm falls at its own angle.
    pivot_share = rng.uniform(0.30, 0.55)
    left_span = width * pivot_share
    left_angle = min(rng.uniform(26.0, 66.0), math.degrees(math.atan2(height, left_span)))
    left_drop = left_span * math.tan(math.radians(left_angle))

    left = fitz.Point(x0, y1 - left_drop)
    pivot = fitz.Point(x0 + left_span, y1)
    right = fitz.Point(x1, y0)

    left_dx = pivot.x - left.x
    left_dy = pivot.y - left.y
    # The control points have an ordering constraint, not just ranges: the validator takes the
    # cross product of the chord with each control offset and requires a consistent bend, which
    # for the left arm means (u3 + u4) > 1. Independent ranges would violate it.
    left_u1 = rng.uniform(0.18, 0.48)
    left_u3 = rng.uniform(0.60, 0.92)
    left_u4 = rng.uniform(max(0.28, 1.06 - left_u3), 0.85)
    left_control_1 = fitz.Point(
        left.x + left_dx * left_u1,
        left.y - left_dy * rng.uniform(0.30, 0.92),
    )
    left_control_2 = fitz.Point(
        left.x + left_dx * left_u3,
        pivot.y - left_dy * left_u4,
    )

    right_dx = right.x - pivot.x
    right_dy = pivot.y - right.y
    # Same ordering constraint for the right arm, which needs v1 < v2.
    right_v1 = rng.uniform(0.10, 0.34)
    right_control_1 = fitz.Point(
        pivot.x + right_dx * right_v1,
        pivot.y - right_dy * rng.uniform(right_v1 + 0.08, 0.62),
    )
    right_control_2 = fitz.Point(
        pivot.x + right_dx * rng.uniform(0.58, 0.90),
        right.y - right_dy * rng.uniform(0.05, 0.28),
    )

    # Thicker strokes on bigger marks, plus its own variation, like pen pressure.
    size_factor = width / (0.49 * zone.width)
    stroke_width = 2.45 * size_factor**0.45 * rng.uniform(0.72, 1.35)
    color = (rng.uniform(0.88, 1.0), rng.uniform(0.10, 0.30), rng.uniform(0.10, 0.30))

    left, left_control_1, left_control_2, pivot, right_control_1, right_control_2, right = (
        _fit_into_zone(
            [left, left_control_1, left_control_2, pivot,
             right_control_1, right_control_2, right],
            zone,
            stroke_width / 2 + 0.5,
        )
    )

    page.draw_bezier(left, left_control_1, left_control_2, pivot, color=color, width=stroke_width)
    page.draw_bezier(pivot, right_control_1, right_control_2, right, color=color, width=stroke_width)


def make_page_rng(master_rng: random.Random, page_number: int) -> random.Random:
    return random.Random(master_rng.randrange(1 << 30) + page_number)


def _line_rects(page: fitz.Page, rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """Keep the hits that share a text line with the first hit and touch it horizontally."""
    if not rects:
        return []
    ordered = sorted(rects, key=lambda rect: rect.x0)
    first = ordered[0]
    tolerance = max(first.height * 0.6, 2.0)
    merged = [first]
    for rect in ordered[1:]:
        previous = merged[-1]
        same_line = abs((rect.y0 + rect.y1) / 2 - (previous.y0 + previous.y1) / 2) <= tolerance
        contiguous = rect.x0 - previous.x1 <= max(first.height * 0.8, 3.0)
        if same_line and contiguous:
            merged.append(rect)
    return merged


def find_label_rect_on_page(page: fitz.Page, labels: tuple[str, ...]) -> tuple[fitz.Rect, str] | None:
    """Return the full extent of the first label found on this page, or None."""
    for label in labels:
        hits = [fitz.Rect(rect) for rect in page.search_for(label)]
        if not hits:
            continue
        merged = _line_rects(page, hits)
        rect = merged[0]
        for extra in merged[1:]:
            rect = rect | extra
        return rect, label
    return None


def find_first_label_rect(
    document: fitz.Document,
    labels: tuple[str, ...],
    preferred_page_index: int | None = None,
) -> tuple[int, fitz.Rect, str]:
    """Find a label, looking at the preferred page first, then the rest of the document."""
    order: list[int] = []
    if preferred_page_index is not None and 0 <= preferred_page_index < document.page_count:
        order.append(preferred_page_index)
    order.extend(index for index in range(document.page_count) if index not in order)
    for page_index in order:
        found = find_label_rect_on_page(document[page_index], labels)
        if found is not None:
            rect, label = found
            return page_index, rect, label
    raise ValueError(f"label not found: {labels}")


def find_review_page(document: fitz.Document, preferred_page_index: int | None = None) -> int:
    """Index of the 指导老师评议 page — the signature page, which carries no checkmark.

    Checkmarks stop before this page: it holds the score and the signature, not reviewed work.
    Raises ValueError when the labels are missing or land on different pages, so the caller can
    leave that report alone instead of marking a guessed page.
    """
    score_page_index, _score_rect, score_label = find_first_label_rect(
        document, SCORE_LABEL_VARIANTS, preferred_page_index
    )
    signature_page_index, _signature_rect, _signature_label = find_first_label_rect(
        document, SIGNATURE_LABEL_VARIANTS, score_page_index
    )
    if score_page_index != signature_page_index:
        raise ValueError(
            f"review labels were found on different pages: "
            f"{score_label!r} on page {score_page_index + 1}, "
            f"{SIGNATURE_LABEL_VARIANTS[-1]!r} on page {signature_page_index + 1}"
        )
    return score_page_index


def review_label_rects(
    document: fitz.Document,
    page_index: int,
) -> tuple[fitz.Rect, fitz.Rect]:
    """(score label rect, signature label rect) on the review page."""
    page = document[page_index]
    score = find_label_rect_on_page(page, SCORE_LABEL_VARIANTS)
    signature = find_label_rect_on_page(page, SIGNATURE_LABEL_VARIANTS)
    if score is None:
        raise ValueError(f"missing {SCORE_LABEL_VARIANTS[0]!r} on page {page_index + 1}")
    if signature is None:
        raise ValueError(f"missing {SIGNATURE_LABEL_VARIANTS[0]!r} on page {page_index + 1}")
    return score[0], signature[0]


def _translated_vertices(
    vertices: list[list[tuple[float, float]]],
    dx: float,
    dy: float,
) -> list[list[tuple[float, float]]]:
    return [[(x + dx, y + dy) for x, y in stroke] for stroke in vertices]


def _copy_ink_annot(target_page: fitz.Page, annot: fitz.Annot, dx: float, dy: float) -> fitz.Annot:
    new_annot = target_page.add_ink_annot(_translated_vertices(annot.vertices, dx, dy))
    stroke = annot.colors.get("stroke") if annot.colors else None
    if stroke:
        new_annot.set_colors(stroke=stroke)
    border = annot.border or {}
    width = border.get("width")
    if width is not None:
        new_annot.set_border(width=width, dashes=border.get("dashes", ()))
    if annot.opacity is not None:
        new_annot.set_opacity(annot.opacity)
    new_annot.update()
    return new_annot


def signature_ink_color(signature_pdf: Path) -> tuple[float, float, float]:
    """Stroke colour of the template's handwriting, so the typed score matches the pen."""
    with fitz.open(signature_pdf) as document:
        try:
            page_index = find_review_page(document)
        except ValueError:
            page_index = document.page_count - 1
        page = document[page_index]
        annot = page.first_annot
        while annot:
            if annot.type[1] == INK_ANNOT_TYPE and annot.colors:
                stroke = annot.colors.get("stroke")
                if stroke and len(stroke) >= 3:
                    return (float(stroke[0]), float(stroke[1]), float(stroke[2]))
            annot = annot.next
    return (0.96, 0.26, 0.22)


def copy_signature_ink(
    signature_pdf: Path,
    target_document: fitz.Document,
    target_page_index: int,
    source_page_index: int | None = None,
) -> int:
    """Copy only the teacher-signature ink from the template onto the target review page.

    The template's score ink is deliberately skipped: it is the fixed ``100`` that this skill
    replaces with the graded score.
    """
    with fitz.open(signature_pdf) as source_document:
        if source_page_index is None:
            try:
                source_page_index = find_review_page(source_document)
            except ValueError:
                source_page_index = source_document.page_count - 1
        source_page = source_document[source_page_index]
        target_page = target_document[target_page_index]
        _source_score_rect, source_signature_rect = review_label_rects(
            source_document, source_page_index
        )
        _target_score_rect, target_signature_rect = review_label_rects(
            target_document, target_page_index
        )

        dx = target_signature_rect.x1 - source_signature_rect.x1
        dy = target_signature_rect.y0 - source_signature_rect.y0

        label_y0 = source_signature_rect.y0
        label_y1 = source_signature_rect.y1
        band_top = label_y0 - 18
        band_bottom = label_y1 + 22

        copied = 0
        annot = source_page.first_annot
        while annot:
            rect = fitz.Rect(annot.rect)
            if (
                annot.type[1] == INK_ANNOT_TYPE
                and rect.y1 >= band_top
                and rect.y0 <= band_bottom
            ):
                center_x = (rect.x0 + rect.x1) / 2
                # Right of the signature label: the signature. Left of it is the score.
                if center_x >= source_signature_rect.x1 - 4:
                    _copy_ink_annot(target_page, annot, dx, dy)
                    copied += 1
            annot = annot.next
    return copied


def score_font_size(label_rect: fitz.Rect) -> float:
    """Font size whose capital height is twice the label's own text height."""
    return (2.0 * max(label_rect.height, 6.0)) / DIGIT_CAP_RATIO


def score_ink_bounds(text: str, font_size: float) -> tuple[float, float]:
    """(height above the baseline, depth below it) for the digits as they actually render."""
    cap = DIGIT_CAP_RATIO * font_size
    descender = DIGIT_DESCENDER_RATIO * font_size if any(char in "679" for char in text) else 0.0
    return cap, descender


def score_ink_height(text: str, font_size: float) -> float:
    above, below = score_ink_bounds(text, font_size)
    return above + below


def _column_obstacles(
    page: fitz.Page,
    column: fitz.Rect,
    labels: tuple[fitz.Rect, ...],
) -> list[fitz.Rect]:
    """Text boxes inside the score column that the score must not run into.

    The two field labels are dropped: the score is meant to sit beside them.
    """
    obstacles: list[fitz.Rect] = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("words"):
        rect = fitz.Rect(x0, y0, x1, y1)
        if rect.x1 <= column.x0 or rect.x0 >= column.x1:
            continue
        if any(abs(rect.x0 - label.x0) < 0.5 and abs(rect.y0 - label.y0) < 0.5 for label in labels):
            continue
        if not text.strip():
            continue
        obstacles.append(rect)
    return obstacles


def fit_score(
    page: fitz.Page,
    label_rect: fitz.Rect,
    signature_rect: fitz.Rect,
    text: str,
    font_path: str | Path,
    margin: float = 1.0,
) -> tuple[float, float, float, str]:
    """Largest legible score that fits the 成绩评定 cell without touching existing text.

    Reports differ a lot here: the printed rule below the label sits anywhere from 3 to 57
    points away and the label itself can start within a point of the text above it. So the
    size and baseline are searched rather than fixed, and the outcome is reported back.
    """
    font = fitz.Font(fontfile=str(font_path))
    column = fitz.Rect(
        label_rect.x1,
        max(label_rect.y0 - 4.0 * label_rect.height, 0.0),
        signature_rect.x0,
        label_rect.y1 + 4.0 * label_rect.height,
    )
    obstacles = _column_obstacles(page, column, (label_rect, signature_rect))

    max_size = score_font_size(label_rect)
    min_size = (1.3 * label_rect.height) / DIGIT_CAP_RATIO
    x = label_rect.x1 + 1.4 * label_rect.height

    # Prefer sitting on the label's writing line, then try nudging up above nearby text.
    offsets = (0.15, 0.0, 0.45, 0.9, 1.5, 2.2)
    best: tuple[float, float, float, str] | None = None
    for factor in (1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64, 0.58, 0.52):
        size = max_size * factor
        if size < min_size:
            break
        width = font.text_length(text, size)
        if x + width >= signature_rect.x0 - margin:
            continue
        above, below = score_ink_bounds(text, size)
        for offset in offsets:
            baseline = label_rect.y1 - offset * label_rect.height
            ink = fitz.Rect(x, baseline - above, x + width, baseline + below)
            if any(ink.intersects(fitz.Rect(rect) + (-margin, -margin, margin, margin)) for rect in obstacles):
                continue
            if ink.y0 < 0 or ink.y1 > page.rect.height:
                continue
            best = (size, baseline, width, "label-line" if offset <= 0.45 else "raised")
            break
        if best is not None:
            break

    if best is None:
        size = min_size
        width = font.text_length(text, size)
        best = (size, label_rect.y1 - 0.15 * label_rect.height, width, "forced-minimum")
    return best


def write_score(
    page: fitz.Page,
    label_rect: fitz.Rect,
    signature_rect: fitz.Rect,
    text: str,
    font_path: str | Path = DEFAULT_SCORE_FONT,
    color: tuple[float, float, float] = (0.96, 0.26, 0.22),
) -> dict:
    """Write the graded score to the right of 成绩评定： in handwriting ink.

    Returns the placement so the caller can verify what landed where.
    """
    font_path = str(font_path)
    if not Path(font_path).is_file():
        raise ValueError(f"score font not found: {font_path}")

    size, baseline, width, mode = fit_score(
        page, label_rect, signature_rect, text, font_path
    )
    x = label_rect.x1 + 1.4 * label_rect.height
    above, below = score_ink_bounds(text, size)
    rect = fitz.Rect(x, baseline - above, x + width, baseline + below)

    page.insert_text(
        (x, baseline),
        text,
        fontsize=size,
        fontfile=font_path,
        fontname="graderscore",
        color=color,
    )
    return {
        "score_text": text,
        "score_x": round(x, 2),
        "score_baseline": round(baseline, 2),
        "score_font_size": round(size, 2),
        "score_width": round(width, 2),
        "score_ink_height": round(above + below, 2),
        "ink_top": round(baseline - above, 2),
        "ink_bottom": round(baseline + below, 2),
        "score_placement": mode,
        "cap_height_ratio": round(above / max(label_rect.height, 0.1), 2),
        "score_rect": [round(value, 2) for value in (rect.x0, rect.y0, rect.x1, rect.y1)],
    }
