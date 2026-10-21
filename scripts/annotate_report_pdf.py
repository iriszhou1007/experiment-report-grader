#!/usr/bin/env python3
"""Annotate one graded report: red checkmarks, teacher signature ink, and the graded score.

Writes to ``--output`` and never touches ``--source``. Emits a JSON summary on the last line
so the caller can verify what landed where.

The checkmark rule follows hw-worker's detection but stops one page earlier: mark every page of
student work *before* the 指导老师评议 page, and leave that page unmarked because it carries the
score and the signature rather than reviewed work. In these reports the review page is the last
page of student work and the page after it is the 实验报告说明 boilerplate, so neither is marked.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import fitz

from review_marking import (
    DEFAULT_SCORE_FONT,
    copy_signature_ink,
    draw_review_checkmark,
    find_review_page,
    make_page_rng,
    review_label_rects,
    signature_ink_color,
    write_score,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="report PDF to annotate")
    parser.add_argument("--output", type=Path, required=True, help="annotated PDF to write")
    parser.add_argument(
        "--score",
        required=True,
        help="score text to write into 成绩评定, e.g. 94",
    )
    parser.add_argument(
        "--signature-pdf",
        type=Path,
        help="template PDF whose teacher-signature ink is copied onto the review page",
    )
    parser.add_argument(
        "--signature-page",
        type=int,
        help="1-based page in the template holding the signature (default: detected)",
    )
    parser.add_argument("--seed", type=int, help="seed for reproducible checkmark variation")
    parser.add_argument("--font", type=Path, default=Path(DEFAULT_SCORE_FONT))
    parser.add_argument("--no-checkmarks", action="store_true")
    parser.add_argument("--no-signature", action="store_true")
    parser.add_argument("--no-score", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if source == output:
        raise SystemExit("Refusing to overwrite the source PDF")
    if not args.no_signature and not args.signature_pdf:
        raise SystemExit("Pass --signature-pdf or --no-signature")

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 30)
    summary: dict = {"output": str(output), "source": str(source), "seed": seed}

    with fitz.open(source) as document:
        review_page_index = find_review_page(document)
        summary["review_page"] = review_page_index + 1
        summary["total_pages"] = document.page_count

        master_rng = random.Random(seed)
        file_rng = random.Random(master_rng.randrange(1 << 30))
        marked: list[int] = []
        if not args.no_checkmarks:
            for page in document:
                if page.number < review_page_index:
                    draw_review_checkmark(page, make_page_rng(file_rng, page.number))
                    marked.append(page.number + 1)
        summary["checkmarked_pages"] = marked
        summary["unmarked_pages"] = [
            number
            for number in range(1, document.page_count + 1)
            if number not in marked
        ]

        copied_signature = 0
        if not args.no_signature:
            copied_signature = copy_signature_ink(
                args.signature_pdf.resolve(),
                document,
                target_page_index=review_page_index,
                source_page_index=(args.signature_page - 1) if args.signature_page else None,
            )
            if copied_signature == 0:
                raise SystemExit(
                    f"No teacher-signature ink found in {args.signature_pdf} "
                    "near the 指导教师签名 label"
                )
        summary["signature_ink_annotations"] = copied_signature

        if not args.no_score:
            review_page = document[review_page_index]
            label_rect, signature_rect = review_label_rects(document, review_page_index)
            color = (0.96, 0.26, 0.22)
            if not args.no_signature:
                color = signature_ink_color(args.signature_pdf.resolve())
            placement = write_score(
                review_page, label_rect, signature_rect, args.score, args.font, color
            )
            # The score must sit between the label it belongs to and the signature label.
            summary["score"] = {
                **placement,
                "color": [round(value, 3) for value in color],
                "signature_label_x0": round(signature_rect.x0, 2),
                "fits_before_signature_label": placement["score_x"] + placement["score_width"]
                < signature_rect.x0,
            }
            if not summary["score"]["fits_before_signature_label"]:
                raise SystemExit(
                    f"score {args.score!r} is too wide for the 成绩评定 cell "
                    f"(ends at {placement['score_x'] + placement['score_width']:.1f}, "
                    f"signature label starts at {signature_rect.x0:.1f})"
                )

        output.parent.mkdir(parents=True, exist_ok=True)
        document.save(output, garbage=3, deflate=True)

    with fitz.open(output) as check:
        summary["output_pages"] = check.page_count
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
