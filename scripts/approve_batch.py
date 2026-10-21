#!/usr/bin/env python3
"""Approve a reviewed batch and write the result back into every source PDF.

This is the batch counterpart of the review panel's per-report approval. It deliberately does
no PDF work of its own: each report goes through the same ``insert_feedback_page.py`` the panel
calls, so the feedback sheet, the checkmarks, the signature, the score and the built-in
verification cannot drift between the two paths.

``finalize`` refuses to run once a review is approved, so the review artifacts (HTML, Excel,
index.md) are regenerated here at the end, mirroring what the panel does after a save.

Safety properties:
* every write is generated from the immutable original under 原始数据/, never from a PDF that
  has already been written back
* the original is copied to 原始数据/ before the first write, and that backup is verified
  against the hash recorded at grading time
* the output replaces the source atomically, via a same-directory rename
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WRITE_BACK = SCRIPT_DIR / "insert_feedback_page.py"
# Both artifacts come from this skill's own Python writers. The Node pair that used to be called
# here needed @oai/artifact-tool (which ships only inside the Codex runtime) for the workbook and
# Node itself for the panel, so "approve a whole batch without the panel" was impossible on every
# other host: the panel step failed outright without Node, and the workbook was skipped silently
# unless --spreadsheet-modules was passed.
HTML_BUILDER = SCRIPT_DIR / "build_review_html.py"
SPREADSHEET_EXPORTER = SCRIPT_DIR / "export_scores.py"
MATERIALIZER = SCRIPT_DIR / "materialize_reviews.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument(
        "--signature-pdf",
        type=Path,
        help="teacher signature template; defaults to the write-back script's own default",
    )
    parser.add_argument("--score-font", type=Path)
    parser.add_argument("--seed", type=int, help="seed for reproducible checkmark variation")
    parser.add_argument("--limit", type=int, help="approve at most this many reports")
    parser.add_argument("--only", action="append", default=[], help="student name or id to limit to")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    # Resolve these against the caller's working directory now. The write-back runs as a child
    # process with its cwd set to the scratch directory, so a relative --signature-pdf would
    # otherwise be looked up inside that scratch tree and the write-back would refuse.
    for name in ("signature_pdf", "score_font", "review_dir"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replace_atomically(temp: Path, target: Path) -> None:
    """Rename into place from the same directory, so the target is never half-written."""
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.staging")
    shutil.copyfile(temp, staging)
    if staging.stat().st_size != temp.stat().st_size:
        staging.unlink(missing_ok=True)
        raise RuntimeError(f"staged copy is truncated: {target.name}")
    os.replace(staging, target)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_back_report(
    args: argparse.Namespace,
    review_path: Path,
    review: dict,
    backup_dir: Path,
    temp_root: Path,
) -> dict:
    source = Path(review["source_file"])
    if not source.is_file():
        raise RuntimeError(f"source PDF is missing: {source}")

    backup = backup_dir / f"原始备份_{source.name}"

    if args.dry_run:
        # Report the plan without touching the filesystem: a dry run that copies backups
        # leaves the batch looking partly written back.
        return {
            "dry_run": True,
            "student": f"{review.get('student_name')}({review.get('student_id')})",
            "source_pages": None,
            "needs_backup": not backup.is_file(),
            "source_hash_matches_graded": sha256_file(source) == review["source_sha256"].lower(),
        }

    backup_dir.mkdir(parents=True, exist_ok=True)

    current_hash = sha256_file(source)
    if backup.is_file():
        if sha256_file(backup) != review["source_sha256"].lower():
            raise RuntimeError(
                f"original backup does not match the graded hash: {backup.name}"
            )
    else:
        if current_hash != review["source_sha256"].lower():
            raise RuntimeError(
                f"source has changed since grading and there is no original backup to fall back "
                f"on: {source.name}"
            )
        shutil.copyfile(source, backup)
        if sha256_file(backup) != review["source_sha256"].lower():
            raise RuntimeError(f"copying the original backup failed: {source.name}")

    temp_pdf = temp_root / f"{uuid.uuid4().hex}.pdf"
    command = [
        args.python,
        str(WRITE_BACK),
        "--source", str(backup),
        "--review", str(review_path),
        "--output", str(temp_pdf),
        "--approved",
    ]
    if args.signature_pdf:
        command += ["--signature-pdf", str(args.signature_pdf)]
    if args.score_font:
        command += ["--score-font", str(args.score_font)]
    if args.seed is not None:
        command += ["--seed", str(args.seed)]

    # The write-back refuses to run unless the review it reads is already marked approved, so
    # the approval is recorded first and rolled back if the PDF step fails. This mirrors the
    # order the review panel uses.
    draft = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
    review["review_status"] = "approved"
    review["approved_at"] = utc_now()
    review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(temp_root),
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"write-back refused ({source.name}): "
                f"{result.stderr.strip()[-400:] or result.stdout.strip()[-400:]}"
            )
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        if not temp_pdf.is_file():
            raise RuntimeError(f"write-back produced no file: {source.name}")

        annotation = summary.get("annotation") or {}
        verification = annotation.get("verification") or {}
        if not verification.get("ok"):
            raise RuntimeError(f"annotation verification did not pass: {source.name}")

        replace_atomically(temp_pdf, source)
    except Exception:
        temp_pdf.unlink(missing_ok=True)
        review_path.write_text(draft, encoding="utf-8")
        raise
    temp_pdf.unlink(missing_ok=True)

    review["source_modified_at"] = utc_now()
    # Absolute on purpose: resolve_backup() reads a relative value against WORKSPACE_DIR, which is
    # not necessarily the folder this batch lives under, so a relative path can resolve somewhere
    # else entirely. Its fallback candidate still covers a result folder that gets moved.
    review["original_backup"] = str(backup.resolve())
    review["writeback"] = {
        "feedback_page": summary.get("feedback_page"),
        "review_page_in_output": summary.get("review_page_in_output"),
        "source_pages": summary.get("source_pages"),
        "total_pages": summary.get("total_pages"),
        "score_written": (annotation.get("score") or {}).get("score_text"),
        "signature_ink_annotations": annotation.get("signature_ink_annotations"),
        "checkmarked_pages": len(annotation.get("checkmarked_pages") or []),
        "unmarked_pages": annotation.get("unmarked_pages"),
        "seed": annotation.get("seed"),
    }
    review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def rebuild_artifacts(args: argparse.Namespace, review_dir: Path) -> dict:
    output_dir = review_dir.parent
    html_path = output_dir / "实验报告审核面板.html"
    workbook_path = output_dir / "实验报告成绩汇总.xlsx"
    index_path = output_dir / "index.md"

    # Explicit utf-8 on every child: these scripts report Chinese paths, and the default decoding
    # would be the console code page on Windows.
    for command, timeout in (
        ([args.python, str(HTML_BUILDER), str(review_dir), str(html_path)], 600),
        ([args.python, str(SPREADSHEET_EXPORTER), str(review_dir), str(workbook_path)], 600),
        ([args.python, str(MATERIALIZER), "--index-only", "--output-dir", str(review_dir)], 300),
    ):
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    return {
        "html": str(html_path),
        "workbook": str(workbook_path),
        "index": str(index_path),
    }


def main() -> int:
    args = parse_args()
    review_dir = args.review_dir.resolve()
    output_dir = review_dir.parent
    # Keep scratch inside the skill's own tmp namespace, alongside the batch work directory.
    workspace = output_dir.parent.parent
    batch = output_dir.name.removesuffix("_批改结果")
    scratch = workspace / "tmp" / "experiment-report-grader" / batch
    temp_root = scratch / "approve-batch"
    temp_root.mkdir(parents=True, exist_ok=True)
    backup_dir = output_dir / "原始数据"

    paths = sorted(review_dir.glob("*.review.json"))
    targets = []
    already = 0
    for path in paths:
        review = json.loads(path.read_text(encoding="utf-8"))
        if review.get("review_status") == "approved":
            already += 1
            continue
        if args.only and not any(
            token in (review.get("student_name", ""), str(review.get("student_id", "")))
            for token in args.only
        ):
            continue
        targets.append((path, review))
    if args.limit:
        targets = targets[: args.limit]
    unresolved = [
        f"{review.get('student_name')}({review.get('student_id')})"
        for _, review in targets
        if review.get("adjudication_status") == "required"
    ]
    if unresolved:
        raise RuntimeError(
            "Blind-regrade disputes must be adjudicated in the review panel before batch approval: "
            + ", ".join(unresolved)
        )

    print(json.dumps({
        "review_dir": str(review_dir),
        "reviews": len(paths),
        "already_approved": already,
        "to_approve": len(targets),
        "dry_run": args.dry_run,
    }, ensure_ascii=False), flush=True)

    approved = []
    planned = []
    failures = []
    for path, review in targets:
        label = f"{review.get('student_name')}({review.get('student_id')})"
        try:
            summary = write_back_report(args, path, review, backup_dir, temp_root)
            if summary.get("dry_run"):
                record = {
                    "student": label,
                    "plan": "needs backup" if summary["needs_backup"] else "reuse existing backup",
                    "source_matches_graded_hash": summary["source_hash_matches_graded"],
                }
                planned.append(record)
                print("PLAN " + json.dumps(record, ensure_ascii=False), flush=True)
                continue
            annotation = summary.get("annotation") or {}
            record = {
                "student": label,
                "pages": summary.get("total_pages"),
                "feedback_page": summary.get("feedback_page"),
                "review_page": summary.get("review_page_in_output"),
                "score": (annotation.get("score") or {}).get("score_text"),
                "checkmarked_pages": len(annotation.get("checkmarked_pages") or []),
            }
            approved.append(record)
            print("OK " + json.dumps(record, ensure_ascii=False), flush=True)
        except Exception as error:  # noqa: BLE001 - report every failure and keep going
            failures.append({"student": label, "error": str(error)[:400]})
            print("FAIL " + json.dumps({"student": label, "error": str(error)[:400]}, ensure_ascii=False), flush=True)

    artifacts = None
    if not args.dry_run and not args.skip_artifacts:
        artifacts = rebuild_artifacts(args, review_dir)

    print(json.dumps({
        "approved": len(approved),
        "planned": len(planned) if args.dry_run else None,
        "failed": len(failures),
        "failures": failures,
        "artifacts": artifacts,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
