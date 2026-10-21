#!/usr/bin/env python3
"""Write the human-approved result back into a report: red review marks plus a feedback sheet.

The review marks (a red checkmark on every page of student work before the 指导老师评议 page, the
teacher's signature ink on that page, and the graded score in its 成绩评定 field) are drawn first,
then the feedback sheet is prepended as page 1. Both steps read the immutable original backup and
write elsewhere, so the source is only ever replaced atomically by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from html import escape
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fitz  # noqa: E402

from review_marking import (  # noqa: E402
    DEFAULT_SCORE_FONT,
    copy_signature_ink,
    draw_review_checkmark,
    find_review_page,
    make_page_rng,
    review_label_rects,
    signature_ink_color,
    write_score,
)
from verify_annotation import verify_pair  # noqa: E402
from writeback_paths import DEFAULT_SIGNATURE_PDF  # noqa: E402


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_cjk_font() -> str:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                pdfmetrics.registerFont(TTFont("GradingCJK", str(candidate)))
                return "GradingCJK"
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def validate_review(data: dict, source: Path) -> None:
    if data.get("review_status") != "approved":
        raise ValueError("review_status must be 'approved'")
    if not data.get("approved_at"):
        raise ValueError("approved_at is required for an approved review")
    if str(data.get("source_sha256", "")).lower() != file_sha256(source):
        raise ValueError("source SHA-256 does not match the approved review")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    scores = [float(item["score"]) for item in items]
    maxima = [float(item["max_score"]) for item in items]
    if any(score < 0 or maximum < 0 or score > maximum for score, maximum in zip(scores, maxima)):
        raise ValueError("each item score must be between zero and its maximum")
    if not math.isclose(sum(scores), float(data["total_score"]), abs_tol=0.01):
        raise ValueError("item scores do not sum to total_score")
    if not math.isclose(sum(maxima), float(data["max_score"]), abs_tol=0.01):
        raise ValueError("item maxima do not sum to max_score")


def as_paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    safe = escape(str(value)).replace("\n", "<br/>") or "-"
    return Paragraph(safe, style)


def build_feedback_pdf(path: Path, data: dict, page_size: tuple[float, float]) -> None:
    font = register_cjk_font()
    width, _ = page_size
    base = getSampleStyleSheet()["BodyText"]
    body = ParagraphStyle("CJKBody", parent=base, fontName=font, fontSize=9, leading=13, spaceAfter=4)
    small = ParagraphStyle("CJKSmall", parent=body, fontSize=8, leading=11)
    heading = ParagraphStyle("CJKHeading", parent=body, fontSize=13, leading=17, spaceBefore=7, spaceAfter=5)
    title = ParagraphStyle("CJKTitle", parent=heading, fontSize=17, leading=22, alignment=TA_CENTER, spaceAfter=10)

    doc = SimpleDocTemplate(
        str(path),
        pagesize=page_size,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=13 * mm,
        bottomMargin=13 * mm,
        title="实验报告批改反馈",
        author="Experiment Report Grader",
    )
    usable = width - doc.leftMargin - doc.rightMargin
    story = [as_paragraph("实验报告批改反馈", title)]

    identity = []
    for label, key in (("姓名", "student_name"), ("学号", "student_id"), ("实验", "experiment")):
        if data.get(key):
            identity.append(f"{label}：{data[key]}")
    if identity:
        story.append(as_paragraph("　　".join(identity), body))
    graded = float(data["total_score"])
    maximum = float(data["max_score"])
    adjusted = data.get("adjusted_total")
    offset = data.get("score_offset")
    adjusted_value = float(adjusted) if isinstance(adjusted, (int, float)) else None
    shifted = adjusted_value is not None and not math.isclose(adjusted_value, graded, abs_tol=0.01)
    if shifted:
        # The dimension scores below always sum to the graded score, so both figures are
        # printed and the adjustment is named; otherwise a student adding the dimensions
        # up would find an unexplained difference.
        story.append(as_paragraph(f"批改得分：{graded:g} / {maximum:g}", heading))
        detail = "课程统一调整"
        if isinstance(offset, (int, float)) and offset:
            sign = "+" if float(offset) > 0 else ""
            detail = f"课程统一调整（{sign}{float(offset):g}，上限 {maximum:g}）"
        story.append(as_paragraph(f"{detail}后最终成绩：{adjusted_value:g} / {maximum:g}", heading))
        story.append(
            as_paragraph("上表各评分项得分合计为批改得分；最终成绩为批改得分经课程统一调整后的分数。", small)
        )
    else:
        story.append(as_paragraph(f"总分：{graded:g} / {maximum:g}", heading))

    rows = [[as_paragraph("评分项", small), as_paragraph("得分", small)]]
    for item in data["items"]:
        rows.append(
            [
                as_paragraph(item.get("criterion", "-"), small),
                as_paragraph(f"{float(item['score']):g}/{float(item['max_score']):g}", small),
            ]
        )
    table = Table(rows, colWidths=[usable * 0.72, usable * 0.28], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0F6")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#75808A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(as_paragraph("评语", heading))
    story.append(as_paragraph(data.get("overall_comment", "-"), body))
    doc.build(story)


def prepend_feedback(source: Path, feedback: Path, output: Path) -> tuple[int, int]:
    """Put the feedback sheet in front of the report so it is seen first.

    Original pages keep their own order and content, but every one of them shifts by
    one position: a page the grading packet called p.7 is page 8 of the written-back
    file. Evidence page numbers in the review JSON keep referring to the original
    report, which is why the source backup stays the reference copy.
    """
    source_reader = PdfReader(str(source))
    feedback_reader = PdfReader(str(feedback))
    writer = PdfWriter()
    for page in feedback_reader.pages:
        writer.add_page(page)
    for page in source_reader.pages:
        writer.add_page(page)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        writer.write(stream)
    return len(source_reader.pages), len(feedback_reader.pages)


def score_for_report(data: dict) -> str:
    """The score written into the report's 成绩评定 field.

    That field carries the reported grade, i.e. the graded score after any course-level
    adjustment, matching the 最终成绩 the student is officially given.
    """
    graded = float(data["total_score"])
    adjusted = data.get("adjusted_total")
    final = float(adjusted) if isinstance(adjusted, (int, float)) else graded
    return f"{final:g}"


def annotate_report(
    source: Path,
    output: Path,
    score_text: str,
    signature_pdf: Path | None,
    font_path: Path,
    seed: int | None,
) -> dict:
    """Checkmarks, signature ink and score on the original pages, saved to ``output``."""
    seed = seed if seed is not None else random.SystemRandom().randrange(1 << 30)
    summary: dict = {"seed": seed}
    with fitz.open(source) as document:
        review_page_index = find_review_page(document)
        summary["review_page"] = review_page_index + 1
        summary["total_pages"] = document.page_count

        file_rng = random.Random(random.Random(seed).randrange(1 << 30))
        marked = []
        for page in document:
            if page.number < review_page_index:
                draw_review_checkmark(page, make_page_rng(file_rng, page.number))
                marked.append(page.number + 1)
        summary["checkmarked_pages"] = marked
        summary["unmarked_pages"] = [
            number for number in range(1, document.page_count + 1) if number not in marked
        ]

        copied = 0
        color = (0.96, 0.26, 0.22)
        if signature_pdf is not None:
            copied = copy_signature_ink(
                signature_pdf, document, target_page_index=review_page_index
            )
            if copied == 0:
                raise ValueError(
                    f"no teacher-signature ink found in {signature_pdf} near 指导教师签名"
                )
            color = signature_ink_color(signature_pdf)
        summary["signature_ink_annotations"] = copied
        summary["signature_pdf"] = str(signature_pdf) if signature_pdf else None
        summary["score_included"] = bool(score_text)

        if score_text:
            label_rect, signature_rect = review_label_rects(document, review_page_index)
            placement = write_score(
                document[review_page_index], label_rect, signature_rect, score_text, font_path, color
            )
            placement["color"] = [round(value, 3) for value in color]
            placement["signature_label_x0"] = round(signature_rect.x0, 2)
            placement["fits_before_signature_label"] = (
                placement["score_x"] + placement["score_width"] < signature_rect.x0
            )
            if not placement["fits_before_signature_label"]:
                raise ValueError(
                    f"score {score_text!r} does not fit the 成绩评定 cell "
                    f"before x={signature_rect.x0:.1f}"
                )
            summary["score"] = placement

        output.parent.mkdir(parents=True, exist_ok=True)
        document.save(output, garbage=3, deflate=True)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approved", action="store_true")
    parser.add_argument(
        "--signature-pdf",
        type=Path,
        help=f"teacher signature template (default: {DEFAULT_SIGNATURE_PDF})",
    )
    parser.add_argument(
        "--no-review-marks",
        action="store_true",
        help="prepend the feedback sheet only, without checkmarks, signature or score",
    )
    parser.add_argument("--score-font", type=Path, default=Path(DEFAULT_SCORE_FONT))
    parser.add_argument("--seed", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.approved:
        raise SystemExit("Refusing to write: pass --approved only after explicit user approval")
    source = args.source.resolve()
    output = args.output.resolve()
    if source == output:
        raise SystemExit("Refusing to overwrite the source PDF")
    data = json.loads(args.review.resolve().read_text(encoding="utf-8"))
    validate_review(data, source)

    source_reader = PdfReader(str(source))
    if not source_reader.pages:
        raise ValueError("source PDF has no pages")
    box = source_reader.pages[-1].mediabox
    page_size = (float(box.width), float(box.height))
    temp_root = Path.cwd() / "tmp" / "pdfs"
    temp_root.mkdir(parents=True, exist_ok=True)
    feedback = temp_root / f"feedback-{uuid4().hex}.pdf"
    marked_pdf = temp_root / f"marked-{uuid4().hex}.pdf"
    annotation: dict | None = None
    try:
        if args.no_review_marks:
            body = source
        else:
            score_text = score_for_report(data)
            signature_pdf = (args.signature_pdf or Path(DEFAULT_SIGNATURE_PDF)).resolve()
            if not signature_pdf.is_file():
                raise SystemExit(f"signature template not found: {signature_pdf}")
            annotation = annotate_report(
                source, marked_pdf, score_text, signature_pdf, args.score_font, args.seed
            )
            verification = verify_pair(source, marked_pdf, annotation)
            if not verification["ok"]:
                raise ValueError(
                    "review marks failed verification, refusing to write: "
                    + "; ".join(verification["problems"])
                )
            annotation["verification"] = {
                "ok": verification["ok"],
                "score_obstacles_hit": verification.get("score", {}).get("obstacles_hit"),
                "signature_obstacles_hit": [
                    entry["obstacles_hit"] for entry in verification.get("signature", [])
                ],
                "checkmark_counts": sorted(
                    {
                        (entry["checkmarks_before"], entry["checkmarks_after"])
                        for entry in verification.get("page_reports", [])
                    }
                ),
            }
            body = marked_pdf
        build_feedback_pdf(feedback, data, page_size)
        source_pages, feedback_pages = prepend_feedback(body, feedback, output)
    finally:
        feedback.unlink(missing_ok=True)
        marked_pdf.unlink(missing_ok=True)

    expected_pages = source_pages + feedback_pages
    if len(PdfReader(str(output)).pages) != expected_pages:
        output.unlink(missing_ok=True)
        raise RuntimeError("output page count validation failed")
    summary = {
        "output": str(output),
        "source_pages": source_pages,
        "feedback_pages": feedback_pages,
        "feedback_page": 1,
        "total_pages": expected_pages,
        "annotation": annotation,
    }
    if annotation:
        # The review page moved down by the prepended sheet; report where it landed.
        summary["review_page_in_output"] = int(annotation["review_page"]) + feedback_pages
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
