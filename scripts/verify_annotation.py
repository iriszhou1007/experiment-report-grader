#!/usr/bin/env python3
"""Verify an annotated report against its original before anything is written back.

Two independent checks, both at vector level rather than by comparing rendered pixels:

* Checkmarks: every promised page must carry a red check path inside the safe zone, and no
  unmarked page may carry one. Rendered-pixel diffs cannot decide this — many of these reports
  are scans where a third of the page is already dark, so a red check drawn over that content
  changes no pixel classification at all.
* Score and signature: their boxes must not intersect any pre-existing text box. The same
  density problem would make a pixel overlap test pass trivially, because ink drawn over dark
  ink adds no new pixels.

Usage:
    verify_annotation.py --original OLD.pdf --annotated NEW.pdf --summary summary.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz

# Matches the acceptance thresholds of jamesz-dev/hw-worker's validate_checkmarks.py.
SAFE_ZONE = (0.25, 0.35, 0.78, 0.82)
RED_MIN = 0.80
GREEN_MAX = 0.35
BLUE_MAX = 0.35


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--annotated", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--pixel-dpi", type=int, default=150, help="dpi for the diagnostic diff")
    return parser.parse_args()


def is_red(color) -> bool:
    if not color or len(color) < 3:
        return False
    return color[0] >= RED_MIN and color[1] <= GREEN_MAX and color[2] <= BLUE_MAX


def red_stroke_boxes(page: fitz.Page) -> list[fitz.Rect]:
    """Red bezier paths whose centre sits inside the checkmark safe zone.

    Only curves count. A checkmark is drawn as two beziers, while the teacher's signature is
    red ink made of straight segments — and it can land inside the safe zone too, where colour
    alone cannot tell the two apart.
    """
    width, height = page.rect.width, page.rect.height
    zone = fitz.Rect(
        width * SAFE_ZONE[0],
        height * SAFE_ZONE[1],
        width * SAFE_ZONE[2],
        height * SAFE_ZONE[3],
    )
    boxes: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        items = drawing.get("items", [])
        if not items or any(item[0] != "c" for item in items):
            continue
        if not is_red(drawing.get("color")):
            continue
        rect = fitz.Rect(drawing["rect"])
        if rect.is_empty:
            continue
        centre = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        if not zone.contains(centre):
            continue
        boxes.append(rect)
    return boxes


def _merge_clusters(boxes: list[fitz.Rect], join_distance: float) -> list[fitz.Rect]:
    """Group boxes that touch, grown by join_distance, into single boxes."""
    slack = fitz.Rect(-join_distance, -join_distance, join_distance, join_distance)
    clusters: list[fitz.Rect] = []
    for box in boxes:
        clusters.append(fitz.Rect(box))
        while True:
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    if (clusters[i] + slack).intersects(clusters[j]):
                        clusters[i] = clusters[i] | clusters[j]
                        del clusters[j]
                        break
                else:
                    continue
                break
            else:
                break
    return clusters


def count_checkmarks(page: fitz.Page, join_distance: float = 12.0) -> int:
    """Red stroke paths in the safe zone, grouped into marks by proximity.

    Each checkmark is two curves (left arm, right arm), so counting raw paths would report
    two marks per page.
    """
    return len(_merge_clusters(red_stroke_boxes(page), join_distance))


def text_boxes(page: fitz.Page, skip: tuple[fitz.Rect, ...] = ()) -> list[fitz.Rect]:
    """Text-layer boxes on the page, minus the field labels the marks are meant to sit beside."""
    boxes: list[fitz.Rect] = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("words"):
        if not text.strip():
            continue
        rect = fitz.Rect(x0, y0, x1, y1)
        if any(_same_place(rect, other) for other in skip):
            continue
        boxes.append(rect)
    return boxes


def image_boxes(page: fitz.Page) -> list[fitz.Rect]:
    """Placed raster images: scanned or photographed page content has no text layer at all."""
    boxes: list[fitz.Rect] = []
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"])
        if not rect.is_empty:
            boxes.append(rect)
    return boxes


def _same_place(rect: fitz.Rect, other: fitz.Rect) -> bool:
    return abs(rect.x0 - other.x0) < 0.5 and abs(rect.y0 - other.y0) < 0.5


def pixel_diff(
    original: fitz.Document,
    annotated: fitz.Document,
    page_index: int,
    dpi: int,
) -> dict:
    """Diagnostic only: how much ink changed on this page, ignoring colour."""
    import numpy as np

    def ink(document: fitz.Document) -> "np.ndarray":
        pixmap = document[page_index].get_pixmap(dpi=dpi)
        samples = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )
        return samples[:, :, :3].min(axis=2) < 200

    old = ink(original)
    new = ink(annotated)
    return {
        "changed_pixels": int((old ^ new).sum()),
        "new_pixels_over_blank": int((new & ~old).sum()),
    }


def _label_rects(page: fitz.Page) -> tuple[fitz.Rect, ...]:
    """The two field labels, which the score and signature are placed beside, not over."""
    from review_marking import find_label_rect_on_page, SCORE_LABEL_VARIANTS, SIGNATURE_LABEL_VARIANTS

    found: list[fitz.Rect] = []
    for variants in (SCORE_LABEL_VARIANTS, SIGNATURE_LABEL_VARIANTS):
        hit = find_label_rect_on_page(page, variants)
        if hit is not None:
            found.append(hit[0])
    return tuple(found)


def verify_pair(
    original_path: Path,
    annotated_path: Path,
    summary: dict,
    pixel_dpi: int = 150,
) -> dict:
    """Check that the annotated report carries exactly the promised review marks."""
    problems: list[str] = []
    with fitz.open(original_path) as original, fitz.open(annotated_path) as annotated:
        if original.page_count != annotated.page_count:
            return {
                "ok": False,
                "problems": [
                    f"page count changed: {original.page_count} -> {annotated.page_count}"
                ],
            }

        review_page_index = int(summary["review_page"]) - 1
        marked = set(summary.get("checkmarked_pages") or [])
        result: dict = {
            "original": str(original_path),
            "annotated": str(annotated_path),
            "pages": original.page_count,
            "review_page": review_page_index + 1,
        }

        page_reports = []
        for index in range(original.page_count):
            before = count_checkmarks(original[index])
            after = count_checkmarks(annotated[index])
            entry = {
                "page": index + 1,
                "checkmarked": index + 1 in marked,
                "checkmarks_before": before,
                "checkmarks_after": after,
            }
            if index + 1 in marked:
                if after != before + 1:
                    problems.append(
                        f"page {index + 1}: expected one added checkmark, "
                        f"{before} before and {after} after"
                    )
            elif after != before:
                problems.append(
                    f"page {index + 1}: not marked but checkmarks went {before} -> {after}"
                )
            page_reports.append(entry)
        result["page_reports"] = page_reports

        # Obstacles come from the ORIGINAL page: the annotated one already contains the score
        # this is meant to check, and would otherwise be flagged as its own obstacle.
        original_review = original[review_page_index]
        skip = _label_rects(original_review)
        existing = text_boxes(original_review, skip) + image_boxes(original_review)
        result["obstacle_count"] = len(existing)

        score = summary.get("score")
        if score:
            box = fitz.Rect(score["score_rect"])
            hits = [rect for rect in existing if box.intersects(rect)]
            result["score"] = {
                "text": score["score_text"],
                "placement": score["score_placement"],
                "font_size": score["score_font_size"],
                "cap_height_ratio": score["cap_height_ratio"],
                "rect": score["score_rect"],
                "obstacles_hit": len(hits),
                "hit_text": [original_review.get_textbox(rect)[:20] for rect in hits],
            }
            if hits:
                problems.append(
                    f"score {score['score_text']!r} overlaps {len(hits)} existing box(es): "
                    f"{[original_review.get_textbox(rect)[:20] for rect in hits]}"
                )

        if summary.get("signature_ink_annotations"):
            review_page = annotated[review_page_index]
            signature_annots = []
            annot = review_page.first_annot
            while annot:
                if annot.type[1] == "Ink":
                    signature_annots.append(annot)
                annot = annot.next
            if not signature_annots:
                problems.append("signature: no Ink annotation on the review page")
            result["signature"] = []
            for annot in signature_annots:
                box = fitz.Rect(annot.rect)
                hits = [rect for rect in existing if box.intersects(rect)]
                result["signature"].append(
                    {
                        "rect": [round(value, 2) for value in box],
                        "obstacles_hit": len(hits),
                        "hit_text": [original_review.get_textbox(rect)[:20] for rect in hits],
                    }
                )
                if hits:
                    problems.append(
                        f"signature ink overlaps {len(hits)} existing box(es): "
                        f"{[original_review.get_textbox(rect)[:20] for rect in hits]}"
                    )

        if pixel_dpi:
            result["pixel_diff"] = pixel_diff(original, annotated, review_page_index, pixel_dpi)

    result["problems"] = problems
    result["ok"] = not problems
    return result


def main() -> None:
    args = parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    result = verify_pair(args.original, args.annotated, summary, pixel_dpi=args.pixel_dpi)
    print(json.dumps(result, ensure_ascii=False))
    if result["problems"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
