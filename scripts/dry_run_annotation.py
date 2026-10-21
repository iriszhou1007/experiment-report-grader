"""Dry-run the annotation over a whole batch and report what landed where.

Writes only into --out, never into the report folder, so it is safe to run before approval.
Cross-checks every output with hw-worker's own validate_checkmarks.py; that validator marks up
to and including the review page, so the intentionally unmarked review page is its only
expected complaint.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from pathlib import Path

# Resolved rather than pinned: this script drives the write-back on whatever machine it runs on,
# and a hardcoded interpreter is the same migration trap as a hardcoded signature path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_env import resolve_python  # noqa: E402

PY_BIN = str(resolve_python().value or sys.executable)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--signature-pdf", "--signature-file", dest="signature_pdf", type=Path, required=True)
    parser.add_argument("--validator", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260407)
    return parser.parse_args()


def source_for(reports: Path, backup_dir: Path, name: str) -> Path:
    """The file this report should be annotated from.

    An approved report has already been written back, so its source file now carries the
    feedback sheet and the earlier marks. The write-back always regenerates from the immutable
    original under 原始数据/, and a dry run has to read the same file or every check on an
    already-approved report reports marks that were there to begin with.
    """
    backup = backup_dir / f"原始备份_{name}"
    return backup if backup.is_file() else reports / name


def main() -> int:
    args = parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    backup_dir = args.review_dir.parent / "原始数据"
    env = dict(os.environ, PYTHONPATH=str(args.skill / "scripts"))
    rows: list[dict] = []
    fails: list[tuple[str, str, object]] = []
    validator_lines: collections.Counter = collections.Counter()

    for review_path in sorted(args.review_dir.glob("*.review.json")):
        data = json.loads(review_path.read_text(encoding="utf-8"))
        name = Path(data["source_file"]).name
        source = source_for(args.reports, backup_dir, name)
        marked = out / f"{source.stem}.marked.pdf"
        summary_path = out / f"{source.stem}.json"
        score = data.get("adjusted_total", data["total_score"])

        result = subprocess.run(
            [
                PY_BIN, str(args.skill / "scripts" / "annotate_report_pdf.py"),
                "--source", str(source), "--output", str(marked),
                "--score", f"{score:g}", "--signature-pdf", str(args.signature_pdf),
                "--seed", str(args.seed),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        if result.returncode != 0:
            fails.append((name, "annotate", result.stderr.strip()[-200:]))
            continue
        summary_path.write_text(result.stdout.strip().splitlines()[-1], encoding="utf-8")

        verified = subprocess.run(
            [
                PY_BIN, str(args.skill / "scripts" / "verify_annotation.py"),
                "--original", str(source), "--annotated", str(marked),
                "--summary", str(summary_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        try:
            report = json.loads(verified.stdout.strip().splitlines()[-1])
        except Exception:
            report = {"ok": False, "problems": [verified.stderr.strip()[-200:]]}
        if not report["ok"]:
            fails.append((name, "verify", report["problems"][:3]))
            continue

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        placement = summary["score"]
        rows.append(
            {
                "name": name,
                "pages": summary["total_pages"],
                "review_page": summary["review_page"],
                "checkmarked_pages": summary["checkmarked_pages"],
                "unmarked_pages": summary["unmarked_pages"],
                "score": placement["score_text"],
                "score_font_size": placement["score_font_size"],
                "score_placement": placement["score_placement"],
                "score_obstacles_hit": report.get("score", {}).get("obstacles_hit"),
                "signature_obstacles_hit": [
                    entry["obstacles_hit"] for entry in report.get("signature", [])
                ],
                "signature_image_obstacles_hit": report.get("signature_image", {}).get("obstacles_hit"),
                "signature_ink_annotations": summary["signature_ink_annotations"],
                "signature_image_count": summary.get("signature_image_count", 0),
            }
        )

        if args.validator:
            checked = subprocess.run(
                [PY_BIN, str(args.validator), str(marked)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in checked.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and "PASS" not in stripped:
                    validator_lines[stripped[:78]] += 1

        print("OK", name, flush=True)

    print(f"=== 自检通过 {len(rows)} / 失败 {len(fails)} ===")
    for failure in fails:
        print("  FAIL", failure[0], failure[1], failure[2])
    print()
    # Every counter below is derived from `rows`. An all-failed run left `rows` empty and used to
    # raise on min() here, killing the summary and -- worse -- the batch_summary.json write, i.e.
    # precisely the run whose record matters most. The failure list above is the payload in that
    # case, so the per-item breakdown is skipped rather than faked.
    if not rows:
        print("没有任何报告通过校验，逐项汇总无数据可算；失败明细见上。")
    else:
        print("评议页位置分布:", dict(collections.Counter(row["review_page"] for row in rows)))
        print(
            "打勾页 = 1..评议页-1 全部成立:",
            all(row["checkmarked_pages"] == list(range(1, row["review_page"])) for row in rows),
        )
        print("评议页均在未打勾列表:", all(row["review_page"] in row["unmarked_pages"] for row in rows))
        print(
            "评议页后一页未被勾:",
            all(
                row["review_page"] + 1 in row["unmarked_pages"] or row["review_page"] == row["pages"]
                for row in rows
            ),
        )
        print(
            "字号范围: %.1f - %.1f"
            % (min(row["score_font_size"] for row in rows), max(row["score_font_size"] for row in rows))
        )
        print("落位方式:", dict(collections.Counter(row["score_placement"] for row in rows)))
        print("分数障碍命中总和:", sum(row["score_obstacles_hit"] for row in rows))
        print(
            "签名障碍命中总和:",
            sum(
                sum(row["signature_obstacles_hit"])
                + (row["signature_image_obstacles_hit"] or 0)
                for row in rows
            ),
        )
        print("签名墨迹数分布:", dict(collections.Counter(row["signature_ink_annotations"] for row in rows)))
        print("签名图片数分布:", dict(collections.Counter(row["signature_image_count"] for row in rows)))
    print()
    print("--- 第三方验收器非 PASS 行 ---")
    for line, count in validator_lines.most_common(10):
        print(f"  {count:>3}  {line}")

    # Written whether or not anything passed: the failures are the reason to read this file later.
    (out.parent / "batch_summary.json").write_text(
        json.dumps(
            {"passed": rows, "failures": [[f[0], f[1], f[2]] for f in fails]},
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
