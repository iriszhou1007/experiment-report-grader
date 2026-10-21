"""Audit a written-back batch: page structure, marks, score placement and artifacts.

    audit_written_batch.py --batch /path/to/<batch folder> [--review-dir .../reviewjson]
"""

from __future__ import annotations

import argparse
import collections
import json
import hashlib
import statistics as st
import sys
import tempfile
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_marking import SAFE_ZONE  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch",
        type=Path,
        required=True,
        help="folder holding the source PDFs of the batch",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        help="review JSON folder (default: <batch>/<batch>_批改结果/reviewjson)",
    )
    return parser.parse_args()


def checkmarks(page: fitz.Page) -> int:
    zone = fitz.Rect(
        page.rect.width * SAFE_ZONE[0], page.rect.height * SAFE_ZONE[1],
        page.rect.width * SAFE_ZONE[2], page.rect.height * SAFE_ZONE[3],
    )
    boxes = []
    for drawing in page.get_drawings():
        items = drawing.get("items", [])
        if not items or any(item[0] != "c" for item in items):
            continue
        colour = drawing.get("color")
        if not colour or colour[0] < 0.8 or colour[1] > 0.35 or colour[2] > 0.35:
            continue
        rect = fitz.Rect(drawing["rect"])
        centre = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        if zone.contains(centre):
            boxes.append(rect)
    clusters = []
    for box in boxes:
        for index, cluster in enumerate(clusters):
            if (cluster + (-12, -12, 12, 12)).intersects(box):
                clusters[index] = cluster | box
                break
        else:
            clusters.append(fitz.Rect(box))
    return len(clusters)


def find_review_dir(batch: Path) -> Path:
    """Locate the active result folder, ignoring archived copies named ``*_批改结果_旧-*``."""
    candidates = [
        path for path in batch.glob("*_批改结果") if path.is_dir() and (path / "reviewjson").is_dir()
    ]
    if not candidates:
        raise SystemExit(
            f"no result folder with a reviewjson folder under {batch}; pass --review-dir"
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(path.name for path in candidates))
        raise SystemExit(f"more than one result folder under {batch}: {names}; pass --review-dir")
    return candidates[0] / "reviewjson"


def main() -> int:
    args = parse_args()
    result = args.batch.resolve()
    review_dir = (args.review_dir or find_review_dir(result)).resolve()
    if not review_dir.is_dir():
        raise SystemExit(f"review folder not found: {review_dir}")
    reviews = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(review_dir.glob("*.review.json"))]
    problems = []
    stats = collections.Counter()
    rows = []

    for review in reviews:
        name = review["student_name"]
        source = Path(review["source_file"])
        doc = fitz.open(source)
        feedback = review.get("writeback", {}).get("feedback_page")
        review_page = review.get("writeback", {}).get("review_page_in_output")
        expected = review.get("writeback", {}).get("total_pages")

        if expected != doc.page_count:
            problems.append(f"{name}: 页数 {doc.page_count} != 记录 {expected}")
        first = doc[0].get_text()
        if "批改反馈" not in first:
            problems.append(f"{name}: 第 1 页不是反馈页")
        # The feedback sheet carries the adjusted total when the course applied a score offset and
        # the graded total otherwise. A batch finalized with the default offset has no
        # adjusted_total key at all, which used to crash this audit with a TypeError instead of
        # reporting anything.
        want_score = review.get("adjusted_total")
        if want_score is None:
            want_score = review.get("total_score")
        if want_score is None:
            problems.append(f"{name}: 评审记录里既没有 adjusted_total 也没有 total_score")
        elif f"{float(want_score):g}" not in first:
            problems.append(f"{name}: 反馈页未出现最终成绩 {want_score}")

        marked = [i + 1 for i in range(doc.page_count) if checkmarks(doc[i]) > 0]
        expect_marked = list(range(2, (review_page or 0)))
        if marked != expect_marked:
            problems.append(f"{name}: 打勾页 {marked[:3]}... 与预期 {expect_marked[:3]}... 不符")
        if review_page and checkmarks(doc[review_page - 1]) > 0:
            problems.append(f"{name}: 签名页 p{review_page} 仍有勾")
        if doc.page_count > (review_page or 0) and checkmarks(doc[doc.page_count - 1]) > 0:
            problems.append(f"{name}: 末页(说明页)仍有勾")

        # Hold the Page: annotations from a temporary page object become unbound once it is
        # collected, which shows up as "annotation not bound to any page".
        inks = []
        if review_page:
            review_sheet = doc[review_page - 1]
            for annot in review_sheet.annots() or []:
                if annot.type[1] == "Ink":
                    inks.append(annot)
        # Presence, not an exact count: the write-back copies the template's signature strokes as
        # separate Ink annotations, so a multi-stroke signature (the bundled mock has seven)
        # legitimately lands as more than one. The meaningful assertions are that signature ink
        # arrived, and -- checked just below -- that the review page carries no checkmark.
        if not inks:
            problems.append(f"{name}: 签名页没有签名墨迹")
        if review_page:
            page = doc[review_page - 1]
            label = page.search_for("成绩评定：")
            sign = page.search_for("指导教师签名：")
            hit = page.search_for(f"{want_score:g}")
            near = [r for r in hit if label and sign and r.x0 > label[0].x1 and r.x1 < sign[0].x0]
            if not near:
                problems.append(f"{name}: 成绩评定栏未找到 {want_score}")

        row = {"name": name, "raw": review["total_score"], "final": want_score,
               "pages": doc.page_count, "marked": len(marked), "review_page": review_page}
        rows.append(row)
        stats["ink"] += len(inks)
        stats["checked"] += len(marked)
        doc.close()

    raw = [r["raw"] for r in rows]
    final = [r["final"] for r in rows]
    print(f"报告数: {len(rows)}")
    print(f"原始分: 均值 {st.mean(raw):.1f} 中位 {st.median(raw):g} 区间 {min(raw)}-{max(raw)}")
    print(f"最终分: 均值 {st.mean(final):.1f} 中位 {st.median(final):g} 区间 {min(final)}-{max(final)}")
    print("最终分分布:", dict(collections.Counter(
        "95+" if a >= 95 else "90-94" if a >= 90 else "85-89" if a >= 85 else "<85" for a in final)))
    print(f"勾总数: {stats['checked']}  签名墨迹总数: {stats['ink']}")
    print()

    # backups
    backups = list((review_dir.parent / "原始数据").glob("原始备份_*.pdf"))
    bad_backup = [b.name for b in backups
                  if not any(hashlib.sha256(b.read_bytes()).hexdigest() == r["source_sha256"].lower()
                             for r in reviews if r["source_file"].endswith(b.name.replace("原始备份_", "")))]
    print(f"原始备份: {len(backups)} 个, 与批改哈希不符: {len(bad_backup)}")

    # artifacts
    index = (review_dir.parent / "index.md").read_text(encoding="utf-8")
    print("index.md 已审核:", index.count("| 已审核 |"), "待审核:", index.count("| 待审核 |"))
    # Read the workbook through openpyxl instead of regexing sheet1.xml. openpyxl emits non-ASCII
    # as XML character references -- 已审核 arrives as &#24050;&#23457;&#26680; -- so a literal
    # search over the XML never matches, and pinning exact formula text tied this audit to one
    # batch's size, layout and score offset. Everything below is derived from the workbook's own
    # headers and data range: a score offset adds a column, which shifts the status column, so a
    # hardcoded column index is just as wrong as a hardcoded row count.
    workbook_path = review_dir.parent / "实验报告成绩汇总.xlsx"
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter

        sheet = openpyxl.load_workbook(workbook_path, read_only=True)["成绩汇总"]
        header_row = next(
            (r for r in range(1, sheet.max_row + 1) if sheet.cell(r, 1).value == "姓名"), None
        )
        if header_row is None:
            problems.append("Excel: 未找到表头行（姓名）")
        else:
            headers = {
                str(sheet.cell(header_row, c).value or "").strip(): c
                for c in range(1, sheet.max_column + 1)
            }
            first, last = header_row + 1, header_row + len(rows)
            status_col, total_col = headers.get("审核状态"), headers.get("总分")
            if status_col is None or total_col is None:
                problems.append(
                    "Excel: 表头缺少 审核状态/总分，实际为 "
                    f"{sorted(name for name in headers if name)}"
                )
            else:
                status_letter = get_column_letter(status_col)
                total_letter = get_column_letter(total_col)
                approved_rows = sum(
                    1 for r in range(first, last + 1)
                    if str(sheet.cell(r, status_col).value or "").strip() == "已审核"
                )
                summary_cells = [
                    str(sheet.cell(r, c).value or "")
                    for r in range(1, header_row)
                    for c in range(1, sheet.max_column + 1)
                ]
                expected_formulas = (
                    f"COUNTA(A{first}:A{last})",
                    f"COUNTIF({status_letter}{first}:{status_letter}{last}",
                    f"AVERAGE({total_letter}{first}:{total_letter}{last})",
                )
                missing = [
                    want for want in expected_formulas
                    if not any(want in cell for cell in summary_cells)
                ]
                print(
                    f"Excel 已审核行: {approved_rows}/{len(rows)}  数据区: 第 {first}-{last} 行"
                    f"  状态列 {status_letter}  总分列 {total_letter}"
                    + (f"  汇总公式缺失: {missing}" if missing else "  汇总公式正确")
                )
                if missing:
                    problems.append(f"Excel: 汇总公式未引用数据区 {first}-{last}: {missing}")
                expected_approved = sum(
                    1 for r in reviews if r.get("review_status") == "approved"
                )
                if approved_rows != expected_approved:
                    problems.append(
                        f"Excel: 已审核行 {approved_rows} != 评审记录中已批准 {expected_approved}"
                    )
    except Exception as error:  # noqa: BLE001 - an unreadable workbook is itself the finding
        print(f"Excel 读取失败: {error}")
        problems.append(f"Excel: 工作簿无法读取: {error}")
    html = (review_dir.parent / "实验报告审核面板.html").read_text(encoding="utf-8")
    print("面板含最终成绩:", "最终成绩" in html, "| 内嵌报告数:", html.count(".review.json"))

    print()
    print(f"问题数: {len(problems)}")
    for problem in problems[:15]:
        print("  -", problem)
    # A hardcoded /tmp resolves to <drive>:\\tmp on Windows and may not exist there.
    audit_path = Path(tempfile.gettempdir()) / "audit_batch.json"
    audit_path.write_text(json.dumps({"rows": rows, "problems": problems}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
