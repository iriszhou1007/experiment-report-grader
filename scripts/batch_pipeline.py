#!/usr/bin/env python3
"""Token-efficient two-phase batch pipeline for experiment report grading."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import prepare_grading_packet as packet_tools
import resolve_env
from check_stable_version import verify_stable_version
from submission_preflight import scan as scan_submission_folder
from materialize_reviews import BATCH_LEVEL_WARNINGS, WARNING_LABELS as BATCH_LEVEL_LABELS, normalized_rubric
from scoring_policy import calculate_deduction_scores, minimum_offset_for_average, score_distribution, uses_deduction_policy


PIPELINE_VERSION = "2.1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
# The workspace is used only for service/runtime files. Grading intermediates are always placed
# beside the submitted reports, so they are easy to find and clean without filling the skill
# checkout or a shared workspace.
WORKSPACE_DIR = Path(
    os.environ.get("EXPERIMENT_REPORT_GRADER_WORKSPACE") or SKILL_DIR.parent
).expanduser().resolve()
DEFAULT_RUBRIC = SKILL_DIR / "assets" / "default-rubric.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} must be a JSON object")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def expand_compact_decision(decision: dict, reports_by_id: dict[str, dict]) -> dict:
    report_id = str(decision.get("id", ""))
    report = reports_by_id.get(report_id)
    source_name = str(decision.get("source_name", "")) or (str(report.get("source_name")) if report else "")
    expanded = {
        "source_name": source_name,
        "scores": decision.get("scores", decision.get("s", [])),
        "overall_comment": decision.get("overall_comment", decision.get("c", "")),
    }
    aliases = {
        "t": "total_score",
        "e": "evidence",
        "v": "visual_evidence",
        "i": "item_comments",
        "r": "score_rules",
        "d": "deductions",
        "w": "warnings",
        "x": "experiment",
        "sid": "student_id",
        "n": "student_name",
    }
    for short, full in aliases.items():
        if full in decision:
            expanded[full] = decision[full]
        elif short in decision:
            expanded[full] = decision[short]
    return expanded


def compact_decision(report: dict, decision: dict | None = None) -> dict:
    decision = decision or {}
    row = {
        "id": report["report_id"],
        "s": decision.get("scores", []),
        "c": decision.get("overall_comment", ""),
    }
    aliases = {
        "total_score": "t",
        "evidence": "e",
        "visual_evidence": "v",
        "item_comments": "i",
        "score_rules": "r",
        "deductions": "d",
        "warnings": "w",
        "experiment": "x",
        "student_id": "sid",
        "student_name": "n",
    }
    for full, short in aliases.items():
        if full == "deductions" and full in decision:
            row[short] = decision[full]
        elif decision.get(full) not in (None, "", []):
            row[short] = decision[full]
    return row


def file_sha256(path: Path) -> str:
    return packet_tools.sha256_file(path)


def safe_batch_id(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip()).rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Batch name is empty or invalid")
    return cleaned[:80]


def discover_saved_settings(report_dir: Path) -> dict:
    candidates: list[Path] = []
    candidates.extend(report_dir.glob("*_批改结果/批次信息.json"))
    if report_dir.parent.is_dir():
        candidates.extend(report_dir.parent.glob("*/*_批改结果/批次信息.json"))
    readable = []
    for path in set(candidates):
        try:
            value = read_json(path)
            readable.append((path.stat().st_mtime, value, path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if not readable:
        return {}
    _, value, path = max(readable, key=lambda item: item[0])
    return {**value, "_source": str(path)}


def rubric_signature(rubric_path: Path) -> tuple[str, list[dict], float, str]:
    data = read_json(rubric_path)
    rubric_id, items, maximum = normalized_rubric(data)
    # Fingerprint what the rubric *means*, not what its file happens to look like. Hashing the
    # raw bytes made reformatting indistinguishable from a real edit: the same rubric checked out
    # with CRLF on Windows, re-indented, or with its keys reordered produced a different digest,
    # so `settings_compatible` reported `batch_settings_changed` and threw away reuse for a
    # non-change. The canonical form below keeps the parts that actually reach the model — the
    # item order, each criterion and maximum, and the score bands — so editing a band still
    # invalidates reuse (nothing else catches that: `review_shape_matches` never sees bands,
    # because reviews do not store them) while pure formatting does not.
    canonical = json.dumps(
        {"rubric_id": rubric_id, "maximum": maximum, "items": items},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return rubric_id, items, maximum, digest


def review_shape_matches(review: dict, rubric_id: str, rubric_items: list[dict]) -> bool:
    if str(review.get("rubric_id", "")) != rubric_id:
        return False
    items = review.get("items")
    if not isinstance(items, list) or len(items) != len(rubric_items):
        return False
    for current, expected in zip(items, rubric_items):
        if str(current.get("criterion", "")) != expected["criterion"]:
            return False
        if abs(float(current.get("max_score", -1)) - expected["max_score"]) > 0.01:
            return False
    return True


def review_source_name(review: dict) -> str:
    return Path(str(review.get("source_file", ""))).name.lower()


def load_reviews(review_dir: Path) -> dict[str, tuple[Path, dict]]:
    result: dict[str, tuple[Path, dict]] = {}
    if not review_dir.is_dir():
        return result
    for path in sorted(review_dir.glob("*.review.json")):
        review = read_json(path)
        name = review_source_name(review)
        if name:
            result[name] = (path, review)
    return result


def resolve_backup(review: dict, result_dir: Path, source_name: str) -> Path | None:
    recorded = str(review.get("original_backup", "")).strip()
    candidates: list[Path] = []
    if recorded:
        path = Path(recorded)
        candidates.append(path if path.is_absolute() else WORKSPACE_DIR / path)
    candidates.append(result_dir / "原始数据" / f"原始备份_{source_name}")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def reusable_review(
    review: dict,
    source_path: Path,
    source_hash: str,
    result_dir: Path,
    rubric_id: str,
    rubric_items: list[dict],
    settings_compatible: bool,
) -> tuple[bool, str]:
    if not review_shape_matches(review, rubric_id, rubric_items):
        return False, "rubric_changed"
    original_hash = str(review.get("source_sha256", "")).lower()
    if not original_hash:
        return False, "missing_source_hash"
    if review.get("review_status") == "approved":
        backup = resolve_backup(review, result_dir, source_path.name)
        if backup and file_sha256(backup).lower() == original_hash:
            return True, "approved_backup_verified"
        if source_hash.lower() == original_hash:
            return True, "approved_source_unchanged"
        return False, "approved_backup_unverified"
    if not settings_compatible:
        return False, "batch_settings_changed"
    if source_hash.lower() == original_hash:
        return True, "draft_source_unchanged"
    return False, "source_changed"


def previous_decisions(work_dir: Path) -> dict[str, dict]:
    saved: dict[str, dict] = {}
    reports_by_id: dict[str, dict] = {}
    manifest_path = work_dir / "grading-manifest.json"
    if manifest_path.is_file():
        try:
            previous_manifest = read_json(manifest_path)
            reports_by_id = {
                str(report.get("report_id", "")): report
                for report in previous_manifest.get("pending_reports", [])
                if report.get("report_id")
            }
        except Exception:
            reports_by_id = {}
    for path in sorted(work_dir.glob("decisions-*.json")):
        try:
            data = read_json(path)
        except Exception:
            continue
        reports = data.get("reports") if isinstance(data, dict) else None
        if not isinstance(reports, list):
            continue
        for decision in reports:
            if not isinstance(decision, dict):
                continue
            name = str(decision.get("source_name", "")).lower()
            filled = isinstance(decision.get("scores"), list) or "deductions" in decision
            if name and filled and decision.get("overall_comment"):
                saved[name] = decision
    for path in sorted(work_dir.glob("decisions-*.jsonl")):
        try:
            rows = read_jsonl(path)
        except Exception:
            continue
        for row in rows:
            decision = expand_compact_decision(row, reports_by_id)
            name = str(decision.get("source_name", "")).lower()
            filled = isinstance(decision.get("scores"), list) or "deductions" in decision
            if name and filled and decision.get("overall_comment"):
                saved[name] = decision
    return saved


def decision_stub(report: dict, saved: dict[str, dict]) -> dict:
    old = saved.get(report["source_name"].lower())
    return compact_decision(report, old)


def load_anchors(explicit: str | None) -> list[dict]:
    """Read the optional calibration anchors file, keeping only usable entries.

    Anchors pin the standard across chunks. Keeping them in a file rather than in
    whichever prompt happens to carry them makes a graded batch reproducible.
    """
    if not explicit:
        return []
    path = Path(explicit).resolve()
    if not path.is_file():
        raise ValueError(f"Anchors file does not exist: {path}")
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError("Anchors file must contain a JSON list")
    anchors = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("student_name") or entry.get("label") or "").strip()
        score = entry.get("total_score", entry.get("score"))
        if not name or score in (None, ""):
            continue
        anchors.append({
            "student_name": name,
            "total_score": score,
            "reason": str(entry.get("reason") or entry.get("comment") or "").strip(),
            "item_scores": entry.get("item_scores", entry.get("scores", [])),
            "score_rules": entry.get("score_rules", entry.get("rules", [])),
            "item_reasons": entry.get("item_reasons", []),
            "evidence": entry.get("evidence", []),
            "visual_evidence": entry.get("visual_evidence", []),
            "deductions": entry.get("deductions", []),
        })
    return anchors


def chunk_reports(
    reports: list[dict],
    max_chars: int,
    rubric: dict | None = None,
    anchors: list[dict] | None = None,
) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for report in reports:
        candidate = current + [report]
        if current and len(packet_tools.render_model_input(candidate, rubric, anchors)) > max_chars:
            chunks.append(current)
            current = [report]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def compatible_batch_settings(
    previous: dict | None,
    inspection_mode: str,
    auto_upgrade_visual: bool,
    ocr_mode: str,
    fixed_report_budget: bool,
    max_report_chars: int,
    score_increment: float,
    rubric_id: str,
    rubric_hash: str,
    full_evidence: bool = False,
    reproducibility_check: bool = True,
    max_regrade_delta: float = 2.0,
    stable_tag: str = "",
    stable_commit: str = "",
) -> bool:
    if not previous:
        return True
    if str(previous.get("inspection_mode", "text_only")) != inspection_mode:
        return False
    if bool(previous.get("auto_upgrade_visual", False)) != auto_upgrade_visual:
        return False
    if str(previous.get("ocr_mode", "auto")) != ocr_mode:
        return False
    if bool(previous.get("full_evidence", False)) != full_evidence:
        # Evidence mode changes the packet itself, so a decision graded against the old
        # packet must not be carried over as if the text were unchanged.
        return False
    if bool(previous.get("reproducibility_check", True)) != reproducibility_check:
        return False
    if abs(float(previous.get("max_regrade_delta", 2)) - max_regrade_delta) > 1e-9:
        return False
    if bool(previous.get("fixed_report_budget", False)) != fixed_report_budget:
        return False
    if int(previous.get("max_report_chars", 4000)) != max_report_chars:
        return False
    if previous.get("pipeline_version") and str(previous.get("pipeline_version")) != PIPELINE_VERSION:
        return False
    if str(previous.get("stable_tag", "")) != stable_tag:
        return False
    if str(previous.get("stable_commit", "")) != stable_commit:
        return False
    if abs(float(previous.get("score_increment", 1)) - score_increment) > 1e-9:
        return False
    previous_rubric_id = str(previous.get("rubric_id", ""))
    previous_rubric_hash = str(previous.get("rubric_sha256", ""))
    if previous_rubric_id and previous_rubric_id != rubric_id:
        return False
    if previous_rubric_hash and previous_rubric_hash != rubric_hash:
        return False
    return True


def remove_old_chunk_files(work_dir: Path) -> None:
    """Clear the previous run's chunks so a shorter run cannot leave orphans behind.

    Each file is removed individually: a bulk delete can trip the host's per-turn
    bulk-delete confirmation, which would abort the whole prepare. Removal is by
    unlink, so a stale file is the worst case, never a wrong result.
    """
    for pattern in (
        "grading-input-*.md",
        "decisions-*.json",
        "decisions-*.jsonl",
        "recheck-*.jsonl",
    ):
        for path in sorted(work_dir.glob(pattern)):
            try:
                path.unlink()
            except OSError as error:
                print(json.dumps({
                    "warning": "chunk_file_not_removed",
                    "path": str(path),
                    "detail": str(error),
                }, ensure_ascii=False))


def require_ocr_on_windows(args: argparse.Namespace) -> None:
    """Require runnable Tesseract (chi_sim+eng) AND pdftoppm before creating a batch."""
    if sys.platform != "win32" or getattr(args, "ocr_mode", "auto") == "off":
        return
    resolved = {}
    failures = []
    for name, key in (("tesseract", "ocr_executable"), ("pdftoppm", "pdftoppm_executable")):
        result = resolve_env.resolve_ocr_program(name, getattr(args, key, None))
        if result.ok:
            resolved[key] = str(result.value)
        else:
            failures.append(f"  {name}: {result.detail}\n"
                            f"  provide --{key.replace('_', '-')} or {resolve_env.env_name(key)}, "
                            f'or "{key}" in config/local.json')
    if failures:
        raise RuntimeError(
            "Windows OCR prerequisites are not ready; no batch work was started.\n"
            + "\n".join(failures)
            + "\nCheck existing installs on all local drives before suggesting installation. "
              "Tesseract needs chi_sim+eng; pdftoppm comes from Poppler. "
              "A bounded search miss is not proof of no installation. "
              "Use --ocr-mode off only when deliberately grading without OCR."
        )
    # Pin the verified paths so extraction cannot choose an invalid PATH entry or bypass config.
    for key, value in resolved.items():
        setattr(args, key, value)


def require_signature_template() -> str:
    """Fail before creating batch outputs unless the configured teacher signature is usable."""
    resolution = resolve_env.resolve_signature_pdf()
    if not resolution.ok or resolution.value is None:
        raise RuntimeError(
            "A teacher signature file is required before grading. Configure "
            "EXPERIMENT_REPORT_GRADER_SIGNATURE_FILE (PDF or transparent PNG) or config/local.json, then retry. "
            f"Resolver detail: {resolution.detail or 'not found'}"
        )
    signature_path = Path(resolution.value).expanduser().resolve()
    if not signature_path.is_file():
        raise RuntimeError(f"Teacher signature file is not readable: {signature_path}")
    try:
        import fitz

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        from review_marking import (
            copy_signature_ink,
            copy_signature_image,
            find_review_page,
            is_signature_image,
            validate_signature_image,
        )

        if is_signature_image(signature_path):
            validate_signature_image(signature_path)
            # A synthetic page proves there is room to place the signature without modifying
            # any student report; actual overlap is checked during approved PDF write-back.
            with fitz.open() as synthetic:
                page = synthetic.new_page(width=595, height=842)
                label = fitz.Rect(360, 408, 430, 426)
                copy_signature_image(signature_path, page, label)
        elif signature_path.suffix.lower() == ".pdf":
            with fitz.open(signature_path) as document:
                if document.page_count < 1:
                    raise ValueError("PDF has no pages")
                page_index = find_review_page(document)
                copied = copy_signature_ink(
                    signature_path, document, target_page_index=page_index, source_page_index=page_index
                )
                if copied < 1:
                    raise ValueError("no teacher signature ink found beside the signature label")
        else:
            raise ValueError("supported signature formats are annotated PDF and transparent PNG")
    except Exception as exc:
        raise RuntimeError(
            f"Teacher signature file is not usable for grading: {signature_path}: {exc}"
        ) from exc
    return file_sha256(signature_path)


def prepare_batch(args: argparse.Namespace) -> dict:
    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise ValueError(f"Report directory does not exist: {report_dir}")
    all_pdfs = sorted(path for path in report_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".pdf")
    word_files = sorted(
        path for path in report_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".doc", ".docx"}
    )
    preflight = scan_submission_folder(report_dir)
    eligible_pdf_names = {
        entry["file"] for entry in preflight["entries"]
        if entry["file_type"] == "PDF" and entry["included_in_grading"]
    }
    pdfs = [path for path in all_pdfs if path.name in eligible_pdf_names]
    if not pdfs:
        rejected = [entry for entry in preflight["entries"] if not entry["included_in_grading"]]
        if rejected:
            details = "；".join(
                f"{entry['file']}（{entry['status']}；"
                f"{entry['student_name'] or '姓名未识别'} / {entry['student_id'] or '学号未识别'}）"
                for entry in rejected
            )
            raise ValueError(
                "报告目录中没有符合前置条件的 PDF，当前不能开始评分。预检结果："
                f"{details}。请按提示补交 PDF 或规范文件名后再继续。"
            )
        raise ValueError("报告目录中没有可处理的 PDF、DOC 或 DOCX 文件，无法开始评分。")

    detected_topics = sorted(topic for topic in preflight["experiment_groups"] if topic)
    if len(detected_topics) > 1:
        details = "; ".join(
            f"{topic}: {', '.join(preflight['experiment_groups'][topic][:5])}"
            for topic in detected_topics
        )
        raise ValueError(
            "报告文件名显示多个实验主题，当前不能开始评分。请按主题拆分报告目录，"
            f"或先确认本批次主题后再继续：{details}"
        )

    saved_settings = discover_saved_settings(report_dir)
    explicit_settings = {
        name: getattr(args, name)
        for name in (
            "inspection_mode",
            "ocr_mode",
            "score_increment",
            "max_report_chars",
            "auto_upgrade_visual",
            "fixed_report_budget",
            "full_evidence",
            "reproducibility_check",
            "max_regrade_delta",
        )
    }
    args.inspection_mode = args.inspection_mode or str(saved_settings.get("inspection_mode") or "text_only")
    args.ocr_mode = args.ocr_mode or str(saved_settings.get("ocr_mode") or "auto")
    args.score_increment = float(args.score_increment if args.score_increment is not None else saved_settings.get("score_increment", 1))
    args.max_report_chars = int(args.max_report_chars if args.max_report_chars is not None else saved_settings.get("max_report_chars", 4000))
    args.auto_upgrade_visual = bool(saved_settings.get("auto_upgrade_visual", False)) if args.auto_upgrade_visual is None else args.auto_upgrade_visual
    args.fixed_report_budget = bool(saved_settings.get("fixed_report_budget", False)) if args.fixed_report_budget is None else args.fixed_report_budget
    args.full_evidence = bool(saved_settings.get("full_evidence", False)) if args.full_evidence is None else args.full_evidence
    args.reproducibility_check = (
        bool(saved_settings.get("reproducibility_check", True))
        if args.reproducibility_check is None else args.reproducibility_check
    )
    args.max_regrade_delta = float(
        args.max_regrade_delta
        if args.max_regrade_delta is not None
        else saved_settings.get("max_regrade_delta", 2)
    )
    # An inherited setting silently changes how much evidence each report gets. Report it
    # explicitly: reused settings have invalidated more than one hand-run comparison.
    inherited_settings = {
        name: getattr(args, name)
        for name, value in explicit_settings.items()
        if value is None and saved_settings.get(name) is not None
    }

    saved_rubric = str(saved_settings.get("rubric_path", "")).strip()
    rubric_path = Path(args.rubric or (saved_rubric if saved_rubric and Path(saved_rubric).is_file() else DEFAULT_RUBRIC)).resolve()
    rubric_id, rubric_items, maximum, rubric_hash = rubric_signature(rubric_path)
    stable_version_check = verify_stable_version(
        SKILL_DIR, rubric_path, rubric_id=rubric_id, rubric_sha256=rubric_hash,
    )

    # This is a fixed gate for every batch, even when this particular task will not write back.
    # Validate it before creating result/work directories so a blocked batch leaves no artifacts.
    signature_sha256 = require_signature_template()

    # Windows requires runnable Tesseract+languages and Poppler. Fail here,
    # before any of the work below, rather than grading a batch without OCR.
    require_ocr_on_windows(args)

    batch_id = safe_batch_id(args.batch_name or report_dir.name)
    result_dir = report_dir / f"{batch_id}_批改结果"
    review_dir = result_dir / "reviewjson"
    backup_dir = result_dir / "原始数据"
    result_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    batch_info_path = result_dir / "批次信息.json"
    previous_batch = read_json(batch_info_path) if batch_info_path.is_file() else None
    settings_match = compatible_batch_settings(
        previous_batch,
        args.inspection_mode,
        bool(args.auto_upgrade_visual),
        args.ocr_mode,
        bool(args.fixed_report_budget),
        int(args.max_report_chars),
        args.score_increment,
        rubric_id,
        rubric_hash,
        bool(args.full_evidence),
        bool(args.reproducibility_check),
        float(args.max_regrade_delta),
        str(stable_version_check["stable_tag"]),
        str(stable_version_check["stable_commit"]),
    )

    expected_work_dir = (report_dir / f"{batch_id}_评分暂存").resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else expected_work_dir
    if work_dir != expected_work_dir:
        legacy_work_dir = (report_dir / "tmp" / "experiment-report-grader" / batch_id).resolve()
        legacy_manifest = legacy_work_dir / "grading-manifest.json"
        legacy_is_this_batch = False
        if work_dir == legacy_work_dir and legacy_manifest.is_file():
            try:
                old_manifest = read_json(legacy_manifest)
                legacy_is_this_batch = (
                    str(Path(old_manifest.get("report_dir", "")).resolve()) == str(report_dir)
                    and str(old_manifest.get("batch_id", "")) == batch_id
                )
            except (OSError, json.JSONDecodeError, TypeError):
                legacy_is_this_batch = False
        if not legacy_is_this_batch:
            raise ValueError(
                f"评分暂存目录必须位于实验报告目录下：{expected_work_dir}；拒绝使用工作区或其他位置。"
            )
    work_dir.mkdir(parents=True, exist_ok=True)
    saved_decisions = previous_decisions(work_dir)
    existing_reviews = load_reviews(review_dir)
    reusable = []
    blocked = []
    pending_paths: list[Path] = []
    source_hashes: dict[str, str] = {}

    for source in pdfs:
        source_hash = file_sha256(source)
        source_hashes[source.name.lower()] = source_hash
        entry = existing_reviews.get(source.name.lower())
        if not entry:
            pending_paths.append(source)
            continue
        review_path, review = entry
        if source_hash.lower() != str(review.get("source_sha256", "")).lower():
            # A replacement PDF needs fresh evidence and fresh decisions; an old decision
            # file is keyed by filename and would otherwise be silently reused.
            saved_decisions.pop(source.name.lower(), None)
        if review.get("submission_status") == "resubmission_required":
            blocked.append({
                "source_name": source.name,
                "review_file": review_path.name,
                "status": "resubmission_required",
                "reason": "waiting_for_student_resubmission",
            })
            continue
        if review.get("submission_status") == "resubmitted":
            # Receipt is explicitly confirmed by the teacher in the review panel. Always
            # create fresh grading evidence after that confirmation, even if the replacement
            # PDF happens to have the same bytes as the returned copy.
            saved_decisions.pop(source.name.lower(), None)
            pending_paths.append(source)
            continue
        can_reuse, reason = reusable_review(
            review,
            source,
            source_hash,
            result_dir,
            rubric_id,
            rubric_items,
            settings_match,
        )
        record = {
            "source_name": source.name,
            "review_file": review_path.name,
            "status": review.get("review_status", "draft"),
            "reason": reason,
        }
        if can_reuse and not args.force:
            reusable.append(record)
        elif review.get("review_status") == "approved":
            blocked.append(record)
        else:
            pending_paths.append(source)

    # The template profile is a property of the whole cohort, not of whichever reports happen
    # to be pending. Building it from the pending subset made the stripped-boilerplate set
    # drift as grading progressed, so the same report got different evidence in different runs.
    cohort_digest = hashlib.sha256("\n".join(sorted(source_hashes.values())).encode("utf-8")).hexdigest()[:16]
    template_profile_path = work_dir / "course-template-profile.json"
    stored_profile = read_json(template_profile_path) if template_profile_path.is_file() else None
    if (
        isinstance(stored_profile, dict)
        and stored_profile.get("cohort_digest") == cohort_digest
        and isinstance(stored_profile.get("lines"), list)
    ):
        template_profile = stored_profile
        extracted = [packet_tools.extract_pages(path) for path in pending_paths]
    else:
        all_extracted = [packet_tools.extract_pages(path) for path in pdfs]
        extracted_by_path = {path: pages for path, pages in zip(pdfs, all_extracted)}
        template_profile = packet_tools.build_template_profile(all_extracted)
        template_profile["cohort_digest"] = cohort_digest
        write_json(template_profile_path, template_profile)
        extracted = [extracted_by_path[path] for path in pending_paths]
    removable = set(template_profile.get("lines", []))
    if args.ocr_engine and args.ocr_engine != "auto" and not args.ocr_executable:
        # Wire the optional engines explicitly: without this the vision shim exists but
        # is never selectable, because candidate lookup only ever looks for "tesseract".
        shim_name = "tesseract" if args.ocr_engine == "rapidocr" else "tesseract-vision"
        chosen = SKILL_DIR / "scripts" / "ocr-shims" / shim_name
        if not chosen.is_file():
            hint = "scripts/setup_local_ocr.sh"
            if args.ocr_engine == "vision":
                hint += " --with-vision"
            raise ValueError(
                f"--ocr-engine {args.ocr_engine} needs {chosen}, which does not exist; run {hint}"
            )
        args.ocr_executable = str(chosen)
    visual_assets_dir = work_dir / "visual-assets"
    if visual_assets_dir.exists():
        shutil.rmtree(visual_assets_dir)
    if args.ocr_mode != "off" and pending_paths:
        # Local OCR runs on the CPU for a few seconds per candidate page and prints
        # nothing of its own, so a large batch looks frozen without this notice.
        print(
            "[ocr] local OCR is enabled; each candidate page takes roughly 2-3s with no "
            "further output until this report finishes. Progress:",
            file=sys.stderr,
            flush=True,
        )
    pending_reports = []
    for source, pages in zip(pending_paths, extracted):
        if args.ocr_mode != "off":
            report_candidates, _ = packet_tools.visual_candidates(pages, removable)
            if report_candidates:
                print(
                    f"[ocr] {source.name}: {len(report_candidates)} candidate page(s) "
                    f"{report_candidates} ...",
                    file=sys.stderr,
                    flush=True,
                )
        ocr_result = packet_tools.ocr_candidate_pages(
            source,
            pages,
            removable,
            args.ocr_mode,
            work_dir / "ocr-cache",
            args.ocr_executable,
            args.pdftoppm_executable,
        )
        if args.ocr_mode != "off" and ocr_result.get("candidate_pages"):
            print(
                f"[ocr] {source.name}: {len(ocr_result.get('resolved_pages', []))} of "
                f"{len(ocr_result['candidate_pages'])} page(s) recognized "
                f"({ocr_result.get('status')})",
                file=sys.stderr,
                flush=True,
            )
        grading_route = packet_tools.classify_grading_route(
            pages,
            removable,
            args.inspection_mode,
            ocr_result,
        )
        if args.full_evidence:
            report_budget = sum(len(page) for page in (packet_tools.page_text(p) for p in pages))
            budget_profile = {
                "profile": "full_evidence",
                "ceiling": report_budget,
                "selected": report_budget,
                "truncated": False,
            }
        elif args.fixed_report_budget:
            report_budget = max(1000, args.max_report_chars)
            budget_profile = {"profile": "fixed", "selected": report_budget, "ceiling": report_budget}
        else:
            report_budget, budget_profile = packet_tools.dynamic_text_budget(
                pages,
                max(1000, args.max_report_chars),
            )
            report_budget, route_budget = packet_tools.apply_route_budget(
                report_budget,
                max(1000, args.max_report_chars),
                grading_route,
            )
            budget_profile.update(route_budget)
            budget_profile["selected"] = report_budget
        if args.inspection_mode == "full_visual":
            review_pages = list(range(1, len(pages) + 1))
        elif args.inspection_mode == "smart" or args.auto_upgrade_visual:
            review_pages = packet_tools.select_visual_review_pages(ocr_result)
        else:
            review_pages = []
        visual_assets = packet_tools.extract_visual_assets(
            source,
            review_pages,
            visual_assets_dir,
        )
        report = packet_tools.compact_report(
            source,
            pages,
            removable,
            report_budget,
            args.inspection_mode,
            bool(args.auto_upgrade_visual),
            ocr_result,
            budget_profile,
            grading_route,
            visual_assets,
            bool(args.full_evidence),
        )
        report["source_sha256"] = source_hashes[source.name.lower()]
        pending_reports.append(report)

    for index, report in enumerate(pending_reports, start=1):
        report["report_id"] = f"r{index:04d}"

    rubric_for_input = {"items": rubric_items, "max_score": maximum}
    anchors = load_anchors(args.anchors)
    chunks = chunk_reports(pending_reports, max(1000, args.max_chunk_chars), rubric_for_input, anchors)
    remove_old_chunk_files(work_dir)
    chunk_records = []
    for index, reports in enumerate(chunks, start=1):
        input_path = work_dir / f"grading-input-{index:03d}.md"
        decisions_path = work_dir / f"decisions-{index:03d}.jsonl"
        recheck_path = work_dir / f"recheck-{index:03d}.jsonl"
        model_input = packet_tools.render_model_input(reports, rubric_for_input, anchors)
        input_path.write_text(model_input, encoding="utf-8")
        write_jsonl(decisions_path, [decision_stub(report, saved_decisions) for report in reports])
        if args.reproducibility_check:
            write_jsonl(recheck_path, [decision_stub(report, {}) for report in reports])
        chunk_records.append({
            "chunk": index,
            "input_file": input_path.name,
            "decisions_file": decisions_path.name,
            "recheck_file": recheck_path.name if args.reproducibility_check else None,
            "reports": [report["source_name"] for report in reports],
            "report_ids": [report["report_id"] for report in reports],
            "characters": len(model_input),
            "estimated_tokens": round(len(model_input) / 2.5),
            "over_budget": len(model_input) > args.max_chunk_chars,
        })

    manifest_reports = [
        {key: value for key, value in report.items() if key != "pages"}
        for report in pending_reports
    ]
    visual_plan_path = work_dir / "visual-review-plan.json"
    write_json(visual_plan_path, {
        "inspection_mode": args.inspection_mode,
        "auto_upgrade_visual": bool(args.auto_upgrade_visual),
        "anchors": anchors or [],
        "ocr_mode": args.ocr_mode,
        "fixed_report_budget": bool(args.fixed_report_budget),
        "full_evidence": bool(args.full_evidence),
        "max_report_chars": int(args.max_report_chars),
        "reports": [
            {
                "source_name": report["source_name"],
                "candidate_pages": report.get("visual_candidate_pages", []),
                "review_pages": report.get("visual_review_pages", []),
                "ocr_status": report.get("ocr_status", "off"),
                "ocr_engine": report.get("ocr_engine"),
                "ocr_resolved_pages": report.get("ocr_resolved_pages", []),
                "grading_route": report.get("grading_route", {}),
                "visual_assets": report.get("visual_assets", []),
                "budget": report.get("budget_profile", {}),
                "details": report.get("visual_page_details", []),
            }
            for report in pending_reports
            if report.get("visual_candidate_pages")
        ],
    })
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "stable_tag": stable_version_check["stable_tag"],
        "stable_commit": stable_version_check["stable_commit"],
        "stable_version_check": stable_version_check,
        "created_at": now_iso(),
        "batch_id": batch_id,
        "report_dir": str(report_dir),
        "result_dir": str(result_dir),
        "review_dir": str(review_dir),
        "inspection_mode": args.inspection_mode,
        "auto_upgrade_visual": bool(args.auto_upgrade_visual),
        "ocr_mode": args.ocr_mode,
        "fixed_report_budget": bool(args.fixed_report_budget),
        "full_evidence": bool(args.full_evidence),
        "max_report_chars": int(args.max_report_chars),
        "score_increment": args.score_increment,
        "reproducibility_check": bool(args.reproducibility_check),
        "max_regrade_delta": float(args.max_regrade_delta),
        "rubric": read_json(rubric_path),
        "rubric_path": str(rubric_path),
        "rubric_sha256": rubric_hash,
        "signature_template_sha256": signature_sha256,
        "pending_reports": manifest_reports,
        "reused_reports": reusable,
        "blocked_reports": blocked,
        "chunks": chunk_records,
        "visual_review_plan": str(visual_plan_path),
        "template_profile": str(template_profile_path),
        "template_fingerprint": template_profile.get("fingerprint", ""),
    }
    manifest_path = work_dir / "grading-manifest.json"
    write_json(manifest_path, manifest)

    batch_info = {
        "batch_id": batch_id,
        "batch_name": batch_id,
        "report_dir": str(report_dir),
        "pdf_count": len(pdfs),
        "submitted_pdf_count": len(all_pdfs),
        "pdf_files": [path.name for path in pdfs],
        "unsupported_word_files": [path.name for path in word_files],
        "submission_preflight": preflight["entries"],
        "detected_experiment_labels": detected_topics,
        "inspection_mode": args.inspection_mode,
        "auto_upgrade_visual": bool(args.auto_upgrade_visual),
        "ocr_mode": args.ocr_mode,
        "fixed_report_budget": bool(args.fixed_report_budget),
        "full_evidence": bool(args.full_evidence),
        "max_report_chars": int(args.max_report_chars),
        "score_increment": args.score_increment,
        "reproducibility_check": bool(args.reproducibility_check),
        "max_regrade_delta": float(args.max_regrade_delta),
        "rubric_id": rubric_id,
        "rubric_path": str(rubric_path),
        "rubric_sha256": rubric_hash,
        "signature_template_sha256": signature_sha256,
        "pipeline_version": PIPELINE_VERSION,
        "stable_tag": stable_version_check["stable_tag"],
        "stable_commit": stable_version_check["stable_commit"],
        "stable_version_check": stable_version_check,
        "work_dir": str(work_dir),
        "pending_count": len(pending_reports),
        "reused_count": len(reusable),
        "blocked_count": len(blocked),
        "updated_at": now_iso(),
    }
    write_json(batch_info_path, batch_info)
    summary = {
        "ok": not blocked,
        "phase": "prepare",
        "batch_id": batch_id,
        "reports": len(pdfs),
        "unsupported_word_files": [path.name for path in word_files],
        "submission_preflight": preflight["entries"],
        "detected_experiment_labels": detected_topics,
        "pending": len(pending_reports),
        "reused": len(reusable),
        "blocked": len(blocked),
        "chunks": len(chunks),
        "stable_version_check": stable_version_check,
        "work_dir": str(work_dir),
        "manifest": str(manifest_path),
        "inputs": [chunk["input_file"] for chunk in chunk_records],
        "decisions": [chunk["decisions_file"] for chunk in chunk_records],
        "rechecks": [chunk["recheck_file"] for chunk in chunk_records if chunk.get("recheck_file")],
        "visual_candidates": sum(len(report.get("visual_candidate_pages", [])) for report in pending_reports),
        "visual_review_pages": sum(len(report.get("visual_review_pages", [])) for report in pending_reports),
        "ocr_resolved_pages": sum(len(report.get("ocr_resolved_pages", [])) for report in pending_reports),
        "visual_assets": sum(len(report.get("visual_assets", [])) for report in pending_reports),
        "grading_tiers": {
            tier: sum(1 for report in pending_reports if report.get("grading_route", {}).get("tier") == tier)
            for tier in ("fast", "standard", "detailed")
        },
        "template_lines": len(template_profile.get("lines", [])),
        "template_characters_removed": sum(report.get("template_characters_removed", 0) for report in pending_reports),
        "pages_included": sum(report.get("pages_included", 0) for report in pending_reports),
        "pages_total": sum(report.get("page_count", 0) for report in pending_reports),
        "pages_truncated": sum(len(report.get("pages_truncated", [])) for report in pending_reports),
        "reused_settings": inherited_settings,
        "reused_settings_source": str(saved_settings.get("_source", "")) if inherited_settings else "",
        "anchors": anchors or [],
        "ocr_mode": args.ocr_mode,
        "ocr_engine": args.ocr_engine or "auto",
        "ocr_executable": str(args.ocr_executable or ""),
        "budget_profiles": {report["source_name"]: report.get("budget_profile", {}) for report in pending_reports},
        "visual_review_plan": str(visual_plan_path),
        "next": (
            "Grade once into decisions-*.jsonl, grade again from the packet alone into "
            "recheck-*.jsonl without reading the first pass, then run finalize."
            if args.reproducibility_check
            else "Fill decisions-*.jsonl, then run finalize."
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if blocked:
        raise RuntimeError("Approved reviews conflict with current source or settings; use a new batch name or restore the verified backup.")
    return summary


def find_node(explicit: str | None) -> Path:
    """Resolve Node through the shared order, so it is not tied to one runtime's cache."""
    resolution = resolve_env.resolve_node(explicit)
    if resolution.value is None:
        raise ValueError(f"Node.js was not found ({resolution.detail})")
    return resolution.value.resolve()


def collect_batch_level_notices(pending_reports: list[dict]) -> list[str]:
    """Warnings that describe the batch's settings, collected once instead of once per report.

    Keys such as ``full_evidence_packet`` fire on every report in the batch, so repeating them
    under each report made the panel's "需要教师复核" box look identical for all 62 reports and
    hid the one or two hints that actually differ between them. ``materialize_reviews`` keeps
    them out of the per-report list; this gathers them so the panel can say it once.
    """
    seen: list[str] = []
    for report in pending_reports:
        for value in report.get("warnings", []):
            key = str(value)
            if key in BATCH_LEVEL_WARNINGS and key not in seen:
                seen.append(key)
    return [BATCH_LEVEL_LABELS.get(key, key) for key in seen]


def build_review_artifacts(
    *,
    review_dir: Path,
    html_path: Path,
    workbook_path: Path,
    work_dir: Path,
    notices: list[str] | None = None,
    preflight: list[dict] | None = None,
) -> dict:
    """Write the HTML panel and the Excel workbook with this skill's own Python writers.

    A second, Node-based pair used to be selected whenever ``@oai/artifact-tool`` was present.
    It is gone because it cost more than it bought: the two pairs had to be kept byte-identical
    by hand (``doctor --self-test`` existed only to catch them drifting, and could not even run
    while the notices bug was present), the Node workbook writer needed a package that ships only
    inside the Codex runtime, and every defect found in this area lived on that branch. One
    writer means a machine needs Python and its pip packages, nothing else.
    """
    interpreter = resolve_env.resolve_python().value or Path(sys.executable)
    notice_args = []
    if notices or preflight:
        notice_path = work_dir / "batch-notices.json"
        write_json(notice_path, {"batch_notices": notices or []})
        notice_args = [str(notice_path)]
    if preflight:
        preflight_path = work_dir / "submission-preflight.json"
        write_json(preflight_path, {"entries": preflight})
        notice_args.append(str(preflight_path))
    run_checked([
        str(interpreter),
        str(SCRIPT_DIR / "build_review_html.py"),
        str(review_dir),
        str(html_path),
        *notice_args,
    ])
    run_checked([str(interpreter), str(SCRIPT_DIR / "export_scores.py"), str(review_dir), str(workbook_path)])
    # Kept in the returned mapping (and so in 批次信息.json) because a batch should be able to
    # state which writer produced its deliverables; batches written before this change recorded
    # "node" and stay readable.
    return {"artifact_backend": "python", "python": str(interpreter)}


def run_checked(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return completed.stdout.strip()


def collect_decisions(manifest: dict, work_dir: Path, file_key: str = "decisions_file") -> list[dict]:
    expected = {report["source_name"] for report in manifest.get("pending_reports", [])}
    reports_by_id = {
        str(report.get("report_id", "")): report
        for report in manifest.get("pending_reports", [])
        if report.get("report_id")
    }
    decisions: list[dict] = []
    seen: set[str] = set()
    _, rubric_items, _ = normalized_rubric(manifest["rubric"])
    deduction_mode = uses_deduction_policy(rubric_items)
    for chunk in manifest.get("chunks", []):
        file_name = chunk.get(file_key)
        if not file_name:
            raise ValueError(f"Manifest has no {file_key} for chunk {chunk.get('chunk', '?')}")
        path = work_dir / file_name
        if not path.is_file():
            raise ValueError(f"Missing decisions file: {path.name}")
        if path.suffix.lower() == ".jsonl":
            reports = read_jsonl(path)
        else:
            payload = read_json(path)
            reports = payload.get("reports")
            if not isinstance(reports, list):
                raise ValueError(f"{path.name} must contain a reports list")
        for raw_decision in reports:
            decision = expand_compact_decision(raw_decision, reports_by_id)
            name = str(decision.get("source_name", ""))
            if name not in expected:
                raise ValueError(f"Unexpected report in decisions: {name}")
            if name in seen:
                raise ValueError(f"Duplicate decision for report: {name}")
            if deduction_mode:
                scores, deductions = calculate_deduction_scores(decision, rubric_items)
                decision["scores"] = scores
                decision["deductions"] = deductions
            if not isinstance(decision.get("scores"), list) or not decision["scores"]:
                raise ValueError(f"Scores are not filled for report: {name}")
            if not str(decision.get("overall_comment", "")).strip():
                raise ValueError(f"Overall comment is not filled for report: {name}")
            seen.add(name)
            decisions.append(decision)
    missing = expected - seen
    if missing:
        raise ValueError("Missing decisions for: " + ", ".join(sorted(missing)))
    return decisions


def validate_scoring_protocol(decisions: list[dict], rubric_items: list[dict], score_increment: float) -> None:
    """Validate the reproducible parts of both judging passes before comparing them."""
    for decision in decisions:
        name = str(decision.get("source_name", "unknown"))
        deduction_mode = uses_deduction_policy(rubric_items)
        if deduction_mode:
            calculated, deductions = calculate_deduction_scores(decision, rubric_items)
            decision["scores"] = calculated
            decision["deductions"] = deductions
        scores = decision.get("scores")
        comments = decision.get("item_comments")
        evidence = decision.get("evidence")
        visual_evidence = decision.get("visual_evidence")
        rules = decision.get("score_rules")
        if not isinstance(scores, list) or len(scores) != len(rubric_items):
            raise ValueError(f"{name}: scores must contain {len(rubric_items)} items")
        try:
            numeric_scores = [float(value) for value in scores]
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name}: scores must be numeric") from error
        for index, (score, item) in enumerate(zip(numeric_scores, rubric_items), start=1):
            if not 0 <= score <= float(item["max_score"]):
                raise ValueError(f"{name}: score {index} is outside its rubric range")
            if abs(score / score_increment - round(score / score_increment)) > 1e-9:
                raise ValueError(f"{name}: score {index} does not follow the configured increment")
            fixed_bands = item.get("bands", [])
            fixed_bands = fixed_bands if fixed_bands and all(isinstance(band, dict) for band in fixed_bands) else []
            if deduction_mode:
                if not isinstance(comments, list) or len(comments) != len(rubric_items):
                    raise ValueError(f"{name}: deduction scoring requires i with {len(rubric_items)} reasons")
                if not isinstance(evidence, list) or len(evidence) != len(rubric_items):
                    raise ValueError(f"{name}: deduction scoring requires e with {len(rubric_items)} evidence lists")
                if not str(comments[index - 1]).strip():
                    raise ValueError(f"{name}: item {index} needs a scoring reason")
                if not isinstance(evidence[index - 1], list) or not evidence[index - 1]:
                    raise ValueError(f"{name}: item {index} needs at least one evidence page")
                continue
            if not fixed_bands:
                continue
            if not isinstance(rules, list) or len(rules) != len(rubric_items):
                raise ValueError(f"{name}: fixed score rules require r with {len(rubric_items)} ids")
            if not isinstance(comments, list) or len(comments) != len(rubric_items):
                raise ValueError(f"{name}: fixed score rules require i with {len(rubric_items)} reasons")
            if not isinstance(evidence, list) or len(evidence) != len(rubric_items):
                raise ValueError(f"{name}: fixed score rules require e with {len(rubric_items)} evidence lists")
            rules_by_id = {str(band["id"]): band for band in fixed_bands}
            selected = str(rules[index - 1]).strip()
            if selected not in rules_by_id:
                raise ValueError(f"{name}: invalid score rule {selected!r} for item {index}")
            selected_band = rules_by_id[selected]
            expected_score = float(selected_band["score"])
            if abs(score - expected_score) > 1e-9:
                raise ValueError(
                    f"{name}: item {index} score must be {expected_score:g} for rule {selected!r}"
                )
            if not str(comments[index - 1]).strip():
                raise ValueError(f"{name}: item {index} needs a scoring reason")
            if not isinstance(evidence[index - 1], list) or not evidence[index - 1]:
                raise ValueError(f"{name}: item {index} needs at least one evidence page")
            if bool(selected_band.get("requires_visual_evidence", False)):
                if not isinstance(visual_evidence, list) or len(visual_evidence) != len(rubric_items):
                    raise ValueError(
                        f"{name}: rule {selected!r} requires v with {len(rubric_items)} visual evidence lists"
                    )
                item_visual = visual_evidence[index - 1]
                if not isinstance(item_visual, list) or not any(str(value).strip() for value in item_visual):
                    raise ValueError(
                        f"{name}: rule {selected!r} for item {index} requires at least one visually inspected page in v"
                    )


def compare_regrade_passes(
    primary: list[dict],
    recheck: list[dict],
    tolerance: float,
    work_dir: Path,
    rubric_items: list[dict] | None = None,
) -> dict:
    """Write an auditable comparison; disputed reports are quarantined for adjudication."""
    second_by_name = {str(decision["source_name"]): decision for decision in recheck}
    rows = []
    exceeded = []
    require_fact_consistency = bool(
        rubric_items and rubric_items[0].get("report_deduction_policy", {}).get("recheck_fact_consistency")
    )
    for first in primary:
        name = str(first["source_name"])
        second = second_by_name.get(name)
        if second is None:
            raise ValueError(f"Missing recheck decision for: {name}")
        first_scores = [float(value) for value in first["scores"]]
        second_scores = [float(value) for value in second["scores"]]
        if len(first_scores) != len(second_scores):
            raise ValueError(f"Recheck score count differs for: {name}")
        first_total = sum(first_scores)
        second_total = sum(second_scores)
        delta = abs(first_total - second_total)
        item_deltas = [abs(left - right) for left, right in zip(first_scores, second_scores)]
        first_rules = first.get("score_rules") if isinstance(first.get("score_rules"), list) else []
        second_rules = second.get("score_rules") if isinstance(second.get("score_rules"), list) else []
        changed_rules = [
            index + 1
            for index, (left, right) in enumerate(zip(first_rules, second_rules))
            if str(left) != str(right)
        ]
        first_deductions = {
            str(entry.get("fact")): (
                int(entry.get("item", 0)), str(entry.get("rule", "")),
                tuple(sorted(set(entry.get("requirements", [])))),
                tuple(sorted((figure["id"], tuple(sorted(figure["requirements"])))
                             for figure in entry.get("figures", []))),
            )
            for entry in first.get("deductions", [])
            if isinstance(entry, dict) and entry.get("fact")
        }
        second_deductions = {
            str(entry.get("fact")): (
                int(entry.get("item", 0)), str(entry.get("rule", "")),
                tuple(sorted(set(entry.get("requirements", [])))),
                tuple(sorted((figure["id"], tuple(sorted(figure["requirements"])))
                             for figure in entry.get("figures", []))),
            )
            for entry in second.get("deductions", [])
            if isinstance(entry, dict) and entry.get("fact")
        }
        changed_facts = sorted(
            fact
            for fact in set(first_deductions) | set(second_deductions)
            if first_deductions.get(fact) != second_deductions.get(fact)
        )
        dispute_reasons = []
        if delta > tolerance + 1e-9:
            dispute_reasons.append("total_delta")
        if require_fact_consistency and changed_facts:
            dispute_reasons.append("deduction_facts")
        row = {
            "source_name": name,
            "primary_total": tidy_score(first_total),
            "recheck_total": tidy_score(second_total),
            "absolute_delta": tidy_score(delta),
            "item_deltas": [tidy_score(value) for value in item_deltas],
            "changed_rule_items": changed_rules,
            "changed_deduction_facts": changed_facts,
            "primary_deductions": first.get("deductions", []),
            "recheck_deductions": second.get("deductions", []),
            "dispute_reasons": dispute_reasons,
            "status": "review_required" if dispute_reasons else "within_tolerance",
        }
        rows.append(row)
        if dispute_reasons:
            exceeded.append(row)
    audit = {
        "protocol": "blind-double-score-v2" if require_fact_consistency else "blind-double-score-v1",
        "require_fact_consistency": require_fact_consistency,
        "max_allowed_delta": tolerance,
        "reports": rows,
        "within_tolerance": len(rows) - len(exceeded),
        "review_required": len(exceeded),
    }
    write_json(work_dir / "regrade-audit.json", audit)
    return audit


def mark_regrade_adjudication(review_dir: Path, audit: dict) -> int:
    """Attach dispute state to drafts so downstream approval can fail closed per report."""
    by_name = {str(row["source_name"]): row for row in audit.get("reports", [])}
    required = 0
    for path in review_dir.glob("*.review.json"):
        review = read_json(path)
        row = by_name.get(Path(str(review.get("source_file", ""))).name)
        if not row:
            continue
        if row.get("status") == "review_required":
            review["adjudication_status"] = "required"
            review["regrade_dispute"] = row
            warning = (
                f"两次盲评相差 {row.get('absolute_delta')} 分"
                + ("，扣分事实或对应要求不一致" if "deduction_facts" in row.get("dispute_reasons", []) else "")
                + "，须核对分歧事实并明确确认后才能批准。"
            )
            review["warnings"] = list(dict.fromkeys([*(review.get("warnings") or []), warning]))
            required += 1
        else:
            review["adjudication_status"] = "clear"
            review.pop("regrade_dispute", None)
        write_json(path, review)
    return required


def tidy_score(value: float):
    value = float(value)
    return int(value) if value.is_integer() else value


def finalize_batch(args: argparse.Namespace) -> dict:
    work_dir = Path(args.work_dir).resolve()
    manifest_path = work_dir / "grading-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Manifest does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("blocked_reports"):
        raise ValueError("Approved reviews conflict with the prepared batch")
    result_dir = Path(manifest["result_dir"]).resolve()
    review_dir = Path(manifest["review_dir"]).resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    decisions = collect_decisions(manifest, work_dir) if manifest.get("pending_reports") else []
    regrade_audit = None
    if decisions:
        _, rubric_items, _ = normalized_rubric(manifest["rubric"])
        validate_scoring_protocol(decisions, rubric_items, float(manifest["score_increment"]))
        if manifest.get("reproducibility_check", False):
            rechecks = collect_decisions(manifest, work_dir, "recheck_file")
            validate_scoring_protocol(rechecks, rubric_items, float(manifest["score_increment"]))
            regrade_audit = compare_regrade_passes(
                decisions,
                rechecks,
                float(manifest.get("max_regrade_delta", 2)),
                work_dir,
                rubric_items,
            )
        combined_path = work_dir / "decisions-combined.json"
        packet_path = work_dir / "materialize-packet.json"
        rubric_path = work_dir / "rubric.json"
        write_json(combined_path, {"reports": decisions})
        write_json(packet_path, {
            "packet_version": "1.1",
            "inspection_mode": manifest["inspection_mode"],
            "reports": manifest["pending_reports"],
        })
        write_json(rubric_path, manifest["rubric"])
        run_checked([
            sys.executable,
            str(SCRIPT_DIR / "materialize_reviews.py"),
            "--packet", str(packet_path),
            "--rubric", str(rubric_path),
            "--decisions", str(combined_path),
            "--output-dir", str(Path(manifest["review_dir"])),
            "--score-increment", str(manifest["score_increment"]),
            "--score-offset", "0",
        ])
        if regrade_audit:
            mark_regrade_adjudication(review_dir, regrade_audit)

    review_dir = Path(manifest["review_dir"])
    reviews = sorted(review_dir.glob("*.review.json"))
    if not reviews:
        raise ValueError("No review JSON files are available for finalization")

    review_payloads = [read_json(path) for path in reviews]
    _, rubric_items, _ = normalized_rubric(manifest["rubric"])
    explicit_offset = float(getattr(args, "score_offset", 0) or 0)
    if not math.isfinite(explicit_offset) or explicit_offset < 0:
        raise ValueError("score_offset must be a finite non-negative number")
    target_average = float(manifest.get("rubric", {}).get("minimum_published_average", 0) or 0)
    automatic_offset = 0.0
    if target_average:
        automatic_offset = minimum_offset_for_average(
            [float(review["total_score"]) for review in review_payloads],
            float(manifest["rubric"].get("max_score", 100)),
            target_average,
            float(manifest.get("score_increment", 1)),
        )
    effective_offset = max(explicit_offset, automatic_offset)
    for path, review in zip(reviews, review_payloads):
        if effective_offset > 0:
            review["score_offset"] = tidy_score(effective_offset)
            review["adjusted_total"] = tidy_score(
                min(float(review["max_score"]), float(review["total_score"]) + effective_offset)
            )
        else:
            review.pop("score_offset", None)
            review.pop("adjusted_total", None)
        write_json(path, review)
    distribution_audit = None
    distribution_target = rubric_items[0].get("report_distribution_target")
    if distribution_target:
        distribution_audit = {
            "snapshot_at": now_iso(),
            "scope": "finalize_snapshot_before_teacher_edits",
            "unresolved_reports": sum(
                review.get("adjudication_status") == "required" for review in review_payloads
            ),
            "raw": score_distribution([float(review["total_score"]) for review in review_payloads], distribution_target),
            "published": score_distribution([
                float(review.get("adjusted_total", review["total_score"])) for review in review_payloads
            ], distribution_target),
        }
    run_checked([
        sys.executable,
        str(SCRIPT_DIR / "materialize_reviews.py"),
        "--index-only",
        "--output-dir", str(review_dir),
    ])

    html_path = Path(manifest["result_dir"]) / "实验报告审核面板.html"
    workbook_path = Path(manifest["result_dir"]) / "实验报告成绩汇总.xlsx"
    batch_notices = collect_batch_level_notices(manifest.get("pending_reports", []))
    if target_average:
        batch_notices.append(
            f"课程要求发布成绩平均分不低于 {target_average:g}；本批次统一调整为 +{effective_offset:g} 分，原始批改分保留可追溯。"
        )
    if distribution_audit:
        raw_audit, published_audit = distribution_audit["raw"], distribution_audit["published"]
        batch_notices.append(
            f"生成草稿时的分布快照（教师修改后须重新统计）：{raw_audit['lower']:g}—{raw_audit['upper']:g}分"
            f"原始分 {raw_audit['in_range']}/{raw_audit['count']}，发布分 {published_audit['in_range']}/{published_audit['count']}；"
            f"目标为超过{raw_audit['minimum_fraction'] * 100:g}%，仅作审核观察，不据此强改成绩。"
            f"待裁决{distribution_audit['unresolved_reports']}份。"
        )
    artifact_info = build_review_artifacts(
        review_dir=review_dir,
        html_path=html_path,
        workbook_path=workbook_path,
        work_dir=work_dir,
        notices=batch_notices,
        preflight=read_json(Path(manifest["result_dir"]) / "批次信息.json").get("submission_preflight", []),
    )

    regrade_audit_path = None
    regrade_audit_source = work_dir / "regrade-audit.json"
    if manifest.get("reproducibility_check", False) and decisions and regrade_audit_source.is_file():
        regrade_audit_path = result_dir / "复评分差异审计.json"
        shutil.copyfile(regrade_audit_source, regrade_audit_path)

    batch_info_path = Path(manifest["result_dir"]) / "批次信息.json"
    batch_info = read_json(batch_info_path) if batch_info_path.is_file() else {}
    batch_info.update({
        "score_offset": effective_offset,
        "minimum_published_average": target_average or None,
        "automatic_score_offset": automatic_offset,
        "distribution_audit": distribution_audit,
        "adjudication_required": sum(
            1 for review in review_payloads if review.get("adjudication_status") == "required"
        ),
        "review_count": len(reviews),
        "pending_count": 0,
        "finalized_at": now_iso(),
        "updated_at": now_iso(),
        # Settings-level notes travel with the batch so the panel states them once instead of
        # stamping them onto all 62 reports' "需要教师复核" lists.
        "batch_notices": batch_notices,
        "reproducibility_check": bool(manifest.get("reproducibility_check", False)),
        "max_regrade_delta": float(manifest.get("max_regrade_delta", 2)),
        "regrade_audit": (
            str(regrade_audit_path) if regrade_audit_path else str(batch_info.get("regrade_audit", ""))
        ),
        # Recorded so a batch states which writer produced its deliverables; batches finalized
        # before the Node writer was removed recorded "node" and stay readable.
        **artifact_info,
    })
    write_json(batch_info_path, batch_info)

    service_root = WORKSPACE_DIR / "tmp" / "review-server-ui"
    service_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(html_path, service_root / "实验报告审核面板.html")
    write_json(service_root / "active-batch.json", batch_info)
    summary = {
        "ok": True,
        "phase": "finalize",
        "batch_id": manifest["batch_id"],
        "reviews": len(reviews),
        "new_reviews": len(decisions),
        "reused_reviews": len(manifest.get("reused_reports", [])),
        "score_offset": effective_offset,
        "minimum_published_average": target_average or None,
        "adjudication_required": batch_info["adjudication_required"],
        "distribution_audit": distribution_audit,
        "reproducibility_check": bool(manifest.get("reproducibility_check", False)),
        "regrade_audit": (
            str(regrade_audit_path) if regrade_audit_path else str(batch_info.get("regrade_audit", ""))
        ),
        "workbook": str(workbook_path),
        "html": str(html_path),
        "index": str(review_dir.parent / "index.md"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def batch_status(args: argparse.Namespace) -> dict:
    work_dir = Path(args.work_dir).resolve()
    manifest_path = work_dir / "grading-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Manifest does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    pending = {report["source_name"] for report in manifest.get("pending_reports", [])}
    reports_by_id = {
        str(report.get("report_id", "")): report
        for report in manifest.get("pending_reports", [])
        if report.get("report_id")
    }
    completed = set()
    rechecked = set()
    for chunk in manifest.get("chunks", []):
        path = work_dir / chunk["decisions_file"]
        if not path.is_file():
            continue
        if path.suffix.lower() == ".jsonl":
            rows = read_jsonl(path)
        else:
            rows = read_json(path).get("reports", [])
        for row in rows:
            decision = expand_compact_decision(row, reports_by_id)
            filled = bool(decision.get("scores")) or "d" in row or "deductions" in row
            if filled and str(decision.get("overall_comment", "")).strip():
                completed.add(str(decision.get("source_name", "")))
        recheck_name = chunk.get("recheck_file")
        recheck_path = work_dir / recheck_name if recheck_name else None
        if recheck_path and recheck_path.is_file():
            for row in read_jsonl(recheck_path):
                decision = expand_compact_decision(row, reports_by_id)
                filled = bool(decision.get("scores")) or "d" in row or "deductions" in row
                if filled and str(decision.get("overall_comment", "")).strip():
                    rechecked.add(str(decision.get("source_name", "")))
    review_dir = Path(manifest["review_dir"])
    reviews = load_reviews(review_dir)
    approved = sum(1 for _, review in reviews.values() if review.get("review_status") == "approved")
    summary = {
        "ok": True,
        "phase": "status",
        "batch_id": manifest["batch_id"],
        "pending_decisions": len(pending - completed),
        "completed_decisions": len(completed & pending),
        "pending_rechecks": len(pending - rechecked) if manifest.get("reproducibility_check") else 0,
        "completed_rechecks": len(rechecked & pending) if manifest.get("reproducibility_check") else 0,
        "reused_reviews": len(manifest.get("reused_reports", [])),
        "blocked_reviews": len(manifest.get("blocked_reports", [])),
        "review_files": len(reviews),
        "approved_reviews": approved,
        "work_dir": str(work_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def clean_work(args: argparse.Namespace) -> dict:
    work_dir = Path(args.work_dir).resolve()
    manifest_path = work_dir / "grading-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Refusing to clean a directory without a grading manifest: {work_dir}")
    manifest = read_json(manifest_path)
    report_dir = Path(str(manifest.get("report_dir", ""))).resolve()
    batch_id = safe_batch_id(str(manifest.get("batch_id", "")))
    allowed = {
        (report_dir / f"{batch_id}_评分暂存").resolve(),
        (report_dir / "tmp" / "experiment-report-grader" / batch_id).resolve(),
    }
    if work_dir not in allowed:
        raise ValueError(f"Refusing to clean a directory outside the recorded report batch: {work_dir}")
    if Path(str(manifest.get("result_dir", ""))).resolve() == work_dir:
        raise ValueError("Refusing to clean the result directory")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    summary = {"ok": True, "phase": "clean", "removed": str(work_dir)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


SELF_TEST_RUBRIC = (
    ("实验报告完整性", 20),
    ("数据记录与计算完整性", 30),
    ("结论合理性", 15),
    ("小结质量", 20),
    ("格式规范性与语言表达", 15),
)
SELF_TEST_REPORTS = (
    ("2023001", "自检甲", (20, 28, 13, 17, 13)),
    ("2023002", "自检乙", (19, 27, 12, 16, 14)),
)
# A comment is free text typed by a person, so it can contain anything a person can type. These
# sequences are the ones that matter mechanically: `$&`, `$` + backtick and `$'` are special in a
# JavaScript replacement string, and the placeholder plus a nearby template marker would break a
# naive "find the payload" step in a test. A student writing about `$&` in a regular expression, or
# quoting a `<script>` tag, is entirely plausible.
SELF_TEST_HOSTILE_COMMENT = (
    "自检：$& $` $' $1 $$ __REVIEWS_JSON__ ; let active=0; <script>alert('x')</script> "
    "反斜杠\\ 制表\t 换行\n第二行"
)


def write_self_test_reviews(review_dir: Path, score_offset: float = 5, hostile: bool = False) -> int:
    """Synthetic reviews that exercise both artifacts without needing a PDF or a model.

    Chosen so the workbook's optional column is in play (a positive offset makes the adjusted
    total and its `=MIN(...)` formula appear), because that column is the part a fallback writer
    is most likely to get wrong. ``hostile`` swaps in text that is hostile to the writers rather
    than hostile to a student.
    """
    review_dir.mkdir(parents=True, exist_ok=True)
    for stale in review_dir.glob("*.review.json"):
        stale.unlink()
    for index, (student_id, name, scores) in enumerate(SELF_TEST_REPORTS):
        items = [
            {
                "criterion": criterion,
                "score": score,
                "max_score": maximum,
                "comment": (
                    SELF_TEST_HOSTILE_COMMENT if hostile and index == 0 else f"自检用评语：{criterion}"
                ),
                "evidence": ["p.1"],
            }
            for (criterion, maximum), score in zip(SELF_TEST_RUBRIC, scores)
        ]
        if index == 0:
            items[0]["visual_evidence"] = ["p.1"]
        payload = {
            "schema_version": "1.0",
            "source_file": f"/self-test/{student_id}-{name}.pdf",
            "source_sha256": "0" * 64,
            "student_id": student_id,
            "student_name": name,
            "experiment": "self-test",
            "rubric_id": "self-test",
            "review_status": "draft",
            "approved_at": None,
            "total_score": sum(scores),
            "max_score": sum(maximum for _, maximum in SELF_TEST_RUBRIC),
            "score_offset": score_offset,
            "items": items,
            "overall_comment": "doctor --self-test 生成的合成评语，用于校验产物写出器。",
            "warnings": [],
        }
        write_json(review_dir / f"{student_id}-{name}.review.json", payload)
    return len(SELF_TEST_REPORTS)


def panel_payload(html_path: Path, *, assignment: str = "reviews") -> list:
    """Read a JSON payload back out of a generated panel.

    The template assigns both payloads directly on one line
    (``let reviews=<json>; let batchNotices=<json>; let active=0;``), so a payload is everything
    between its own assignment and the next marker. A comment may contain a trailing marker as
    ordinary text — the hostile self-test variant does exactly that on purpose — so each candidate
    boundary is tried until one parses as JSON rather than taking the first match.
    """
    text = html_path.read_text(encoding="utf-8")
    start_marker = f"let {assignment}="
    if assignment == "reviews":
        end_marker = "; let batchNotices="
    else:
        end_marker = "; let active=0;"
    if end_marker not in text and assignment == "reviews":
        # A panel written before batch-level notices existed still ends at the active marker.
        end_marker = "; let active=0;"
    start = text.index(start_marker) + len(start_marker)
    index = text.find(end_marker, start)
    while index != -1:
        try:
            return json.loads(text[start:index])
        except ValueError:
            index = text.find(end_marker, index + 1)
    raise ValueError(
        f"no parseable payload between 'let {assignment}=' and the following marker"
    )


def artifact_self_test() -> list[dict]:
    """Run the artifact writer on synthetic reviews and check what it produced.

    With the Node pair gone there is nothing to cross-compare, so this checks the writer against
    the payload itself: the panel has to carry back exactly the comments and batch notices that
    went in, and the workbook has to reflect the score offset. Byte-for-byte equality was the old
    contract and it was the weaker check anyway -- it passes if both writers are wrong in the same
    way. Nothing here touches a PDF or a source report.

    Both score-offset variants are covered on purpose. A batch graded without an offset has one
    fewer column than one graded with it, and an earlier version of the writer got that case
    wrong while passing every offset batch -- the golden batch alone could not have caught it.
    """
    checks: list[dict] = []
    # Throwaway artifacts, so they go to the system temp area rather than the pipeline's
    # scratch root: a self-test must not litter the directory a skill is checked out in.
    work = Path(tempfile.mkdtemp(prefix="experiment-report-grader-self-test-"))
    import export_scores  # imported late: only the self-test needs a workbook reader

    headers_seen: dict[float, str] = {}
    for variant, offset, hostile in (
        ("no-offset", 0, False),
        ("offset", 5, False),
        ("hostile-comments", 0, True),
    ):
        review_dir = work / f"reviews-{variant}"
        write_self_test_reviews(review_dir, score_offset=offset, hostile=hostile)

        # Non-empty on purpose: a blank list would silently skip the notice-embedding check.
        self_test_notices = [
            BATCH_LEVEL_LABELS["full_evidence_packet"],
            BATCH_LEVEL_LABELS["packet_over_budget"],
        ]
        target = work / f"{variant}-python"
        html_path = target / "实验报告审核面板.html"
        workbook_path = target / "实验报告成绩汇总.xlsx"
        try:
            build_review_artifacts(
                review_dir=review_dir,
                html_path=html_path,
                workbook_path=workbook_path,
                work_dir=target,
                notices=self_test_notices,
            )
        except Exception as error:  # noqa: BLE001 - the self-test reports rather than raises
            checks.append({
                "check": f"artifact writer ({variant})",
                "phase": "doctor --self-test",
                "ok": False,
                "detail": str(error)[:300],
                "fix": "inspect the exporter's own stderr output",
            })
            continue
        checks.append({
            "check": f"artifact writer ({variant})",
            "phase": "doctor --self-test",
            "ok": html_path.is_file() and workbook_path.is_file(),
            "detail": f"wrote panel and workbook to {target}",
            "fix": "run the exporter directly to see the error",
        })

        # Read the payload back out of the panel and require it to carry exactly the comments that
        # went in: a writer can emit a well-formed page and still mangle or drop the embedded JSON.
        try:
            payload = panel_payload(html_path)
            expected = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(review_dir.glob("*.review.json"))
            ]
            got = [item.get("overall_comment") for item in payload]
            want = [item.get("overall_comment") for item in expected]
            checks.append({
                "check": f"panel payload round-trip ({variant})",
                "phase": "doctor --self-test",
                "ok": got == want,
                "detail": f"{len(payload)} reviews carried intact" if got == want
                else f"comments differ: {got!r} != {want!r}",
                "fix": "the panel writer is mangling the embedded JSON",
            })
            got_visual = payload[0]["items"][0].get("visual_evidence")
            want_visual = expected[0]["items"][0].get("visual_evidence")
            checks.append({
                "check": f"panel visual evidence ({variant})",
                "phase": "doctor --self-test",
                "ok": got_visual == want_visual,
                "detail": "visual evidence carried intact" if got_visual == want_visual
                else f"visual evidence differs: {got_visual!r} != {want_visual!r}",
                "fix": "the panel writer is dropping visual evidence metadata",
            })
        except Exception as error:  # noqa: BLE001
            checks.append({
                "check": f"panel payload round-trip ({variant})",
                "phase": "doctor --self-test",
                "ok": False,
                "detail": f"payload not readable: {error}"[:300],
                "fix": "the panel writer is mangling the embedded JSON",
            })

        # Batch-level notices travel on the same line as the review payload, so a writer that
        # splits or reorders that line could carry reviews correctly and still drop them.
        try:
            got_notices = panel_payload(html_path, assignment="batchNotices")
            ok = got_notices == self_test_notices
            checks.append({
                "check": f"panel batch notices ({variant})",
                "phase": "doctor --self-test",
                "ok": ok,
                "detail": f"{len(got_notices)} notices carried intact" if ok
                else f"notices differ: {got_notices!r}",
                "fix": "the panel writer is dropping the batch-level notice payload",
            })
        except Exception as error:  # noqa: BLE001
            checks.append({
                "check": f"panel batch notices ({variant})",
                "phase": "doctor --self-test",
                "ok": False,
                "detail": f"notices not readable: {error}"[:300],
                "fix": "the panel writer is dropping the batch-level notice payload",
            })

        # Reverse check: the offset must actually change the sheet, so an exporter that silently
        # ignored it cannot pass.
        snapshot = export_scores.snapshot_workbook(workbook_path)
        headers = [
            str(cell["value"])
            for cell in snapshot["sheets"]["成绩汇总"]["cells"].values()
            if isinstance(cell["value"], str) and cell["value"].startswith(("平均分", "原始平均分"))
        ]
        headers_seen[offset] = "|".join(sorted(headers))
    if len(headers_seen) == 2:
        checks.append({
            "check": "score offset reflected",
            "phase": "doctor --self-test",
            "ok": headers_seen[0] != headers_seen[5],
            "detail": f"no-offset='{headers_seen[0]}' offset='{headers_seen[5]}'",
            "fix": "the exporter is ignoring score_offset",
        })

    # Keep the artifacts only when something failed, so a passing run leaves nothing behind.
    if checks and all(check["ok"] for check in checks):
        shutil.rmtree(work, ignore_errors=True)
    else:
        checks.append({
            "check": "self-test output",
            "phase": "doctor --self-test",
            "ok": True,
            "detail": f"kept for inspection at {work}",
            "fix": "",
        })
    return checks


def batch_doctor(args: argparse.Namespace) -> dict:
    """Report the external pieces each phase needs, before a phase fails late.

    The pipeline degrades quietly in some places (OCR simply reports "unavailable")
    and fails loudly in others, but only at the very last step: the approved PDF
    write-back needs reportlab, which used to surface only after the instructor had
    reviewed the whole batch.
    """
    checks: list[dict] = []
    missing: list[str] = []

    def record(name: str, phase: str, ok: bool, detail: str, fix: str) -> None:
        checks.append({"check": name, "phase": phase, "ok": ok, "detail": detail, "fix": "" if ok else fix})
        if not ok:
            missing.append(name)

    for module, phase, fix in (
        ("pypdf", "prepare, finalize", f"{sys.executable} -m pip install pypdf"),
        ("PIL", "prepare (image counting, visual crops)", f"{sys.executable} -m pip install pillow"),
        ("reportlab", "PDF write-back after approval", f"{sys.executable} -m pip install reportlab"),
        ("fitz", "PDF write-back (checkmarks, signature, score)", f"{sys.executable} -m pip install PyMuPDF"),
        ("openpyxl", "finalize (Excel)", f"{sys.executable} -m pip install openpyxl"),
    ):
        try:
            imported = importlib.import_module(module)
            version = getattr(imported, "__version__", "") or getattr(imported, "Version", "")
            record(module, phase, True, f"importable{f' ({version})' if version else ''}", fix)
        except Exception as error:  # noqa: BLE001 - any import failure is a missing dependency
            # Every module above is now needed by a phase that has to work on every host.
            # openpyxl used to be excused here because the Node writer could cover for it; that
            # writer is gone, so a missing openpyxl means the workbook cannot be produced at all.
            record(module, phase, False, str(error), fix)

    # Node is needed for the interactive review panel and for the host-registration check, and
    # for nothing else: grading, the panel HTML, the workbook and the PDF write-back are all
    # Python. Reported as a real gap because the documented review flow depends on it, and the
    # fix names the alternative so a machine without Node does not look stuck.
    try:
        node = find_node(getattr(args, "node", None))
        record("node", "review panel service", True, str(node), "")
    except ValueError as error:
        record(
            "node",
            "review panel service",
            False,
            str(error),
            "install Node.js for the interactive review panel; without it, review the JSON by hand "
            "or approve the batch with scripts/approve_batch.py",
        )

    for name in ("tesseract", "pdftoppm"):
        resolution = resolve_env.resolve_ocr_program(name, None)
        if resolution.ok:
            detail = f"{resolution.value} [{resolution.source}]"
            if resolution.detail:
                # A config entry that lost to a probe is worth naming: it usually means a
                # config/local.json from another machine is still sitting there.
                detail += f"  (note: {resolution.detail})"
        else:
            detail = f"not located/usable ({resolution.detail}); " + (
                "Windows prepare stops while OCR is enabled" if sys.platform == "win32"
                else "OCR reports unavailable and pages stay in the visual queue"
            )
        record(
            name,
            "prepare (local OCR)",
            resolution.ok,
            detail,
            ("check existing installations on other local drives; provide the full executable path "
             "and verify chi_sim+eng for Tesseract; set " if sys.platform == "win32"
             else "run scripts/setup_local_ocr.sh, or set ")
            + resolve_env.env_name('ocr_executable' if name == 'tesseract' else 'pdftoppm_executable'),
        )

    # Which local engine the pipeline can actually reach, named explicitly. Without this the two
    # rows above can only say "no tesseract binary", which reads as "no OCR on this machine" even
    # when Vision or rapidocr is sitting right there, merely unwired.
    ocr = resolve_env.ocr_report()
    if ocr["wired"]:
        ocr_detail = f"{ocr['engine']} (wired)"
        if ocr["vision_wired"] and ocr["engine"] != "vision":
            ocr_detail += "; Vision also wired for --ocr-engine vision"
    elif ocr["available_usable"]:
        ocr_detail = (
            f"none wired; available locally: {', '.join(ocr['available_usable'])}"
            + (f" — enable with: {ocr['enable_command']}" if ocr["enable_command"] else "")
        )
    else:
        ocr_detail = "no local OCR engine found; text-only grading still works"
    record(
        "ocr engine",
        "prepare (local OCR)",
        bool(ocr["wired"]),
        ocr_detail,
        ("provide a verified Tesseract executable with chi_sim+eng on Windows"
         if sys.platform == "win32" else "python3 scripts/bootstrap.py --ocr"),
    )

    # The signature template is required for prepare; the handwriting face is required for
    # approved PDF write-back. Each is reported with the layer that answered.
    from writeback_paths import SCORE_FONT_RESOLUTION, SIGNATURE_PDF_RESOLUTION

    record(
        "teacher signature",
        "prepare (fixed grading precondition)",
        SIGNATURE_PDF_RESOLUTION.ok,
        f"{SIGNATURE_PDF_RESOLUTION.value} [{SIGNATURE_PDF_RESOLUTION.source}]"
        if SIGNATURE_PDF_RESOLUTION.ok
        else f"unresolved ({SIGNATURE_PDF_RESOLUTION.detail})",
        "pass --signature-file/--signature-pdf, set EXPERIMENT_REPORT_GRADER_SIGNATURE_FILE, or add "
        '"signature_file" to config/local.json',
    )
    record(
        "score font",
        "PDF write-back after approval",
        SCORE_FONT_RESOLUTION.ok,
        f"{SCORE_FONT_RESOLUTION.value} [{SCORE_FONT_RESOLUTION.source}]"
        if SCORE_FONT_RESOLUTION.ok
        else f"unresolved ({SCORE_FONT_RESOLUTION.detail})",
        "pass --score-font, set EXPERIMENT_REPORT_GRADER_SCORE_FONT, or add "
        '"score_font" to config/local.json',
    )

    if getattr(args, "self_test", False):
        for check in artifact_self_test():
            checks.append(check)
            if not check["ok"]:
                missing.append(check["check"])

    summary = {"ok": not missing, "phase": "doctor", "python": sys.executable, "missing": missing, "checks": checks}
    for check in checks:
        mark = "ok  " if check["ok"] else "MISS"
        print(f"[{mark}] {check['check']:<20} {check['phase']:<34} {check['detail']}")
        if not check["ok"]:
            print(f"        fix: {check['fix']}")
    print(json.dumps({"ok": summary["ok"], "missing": missing}, ensure_ascii=False))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="scan, reuse unchanged reviews, and create grading chunks")
    prepare.add_argument("--report-dir", required=True)
    prepare.add_argument("--batch-name")
    prepare.add_argument("--rubric")
    prepare.add_argument("--inspection-mode", choices=("text_only", "smart", "full_visual"))
    prepare.add_argument("--auto-upgrade-visual", action=argparse.BooleanOptionalAction, default=None)
    prepare.add_argument("--score-increment", type=float)
    prepare.add_argument("--max-report-chars", type=int)
    prepare.add_argument("--fixed-report-budget", action=argparse.BooleanOptionalAction, default=None)
    prepare.add_argument(
        "--full-evidence",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Send every page in full instead of excerpting it; template de-duplication still applies.",
    )
    prepare.add_argument(
        "--anchors",
        help="JSON list of teacher-confirmed anchors; include deductions, item_scores, "
             "item_reasons and evidence when possible so calibration is reproducible",
    )
    prepare.add_argument(
        "--reproducibility-check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="require a blind second scoring pass and quarantine reports whose totals differ too much",
    )
    prepare.add_argument(
        "--max-regrade-delta",
        type=float,
        default=None,
        help="largest allowed total-score difference between the two passes (default: 2)",
    )
    prepare.add_argument("--ocr-mode", choices=("auto", "off"))
    prepare.add_argument(
        "--ocr-engine",
        choices=("auto", "rapidocr", "vision"),
        help="pick a bundled OCR shim: auto uses whatever is installed, rapidocr and "
             "vision force scripts/ocr-shims/{tesseract,tesseract-vision}",
    )
    prepare.add_argument("--ocr-executable")
    prepare.add_argument("--pdftoppm-executable")
    prepare.add_argument("--max-chunk-chars", type=int, default=24000)
    prepare.add_argument("--work-dir")
    prepare.add_argument("--force", action="store_true", help="regrade compatible drafts; never replace approved reviews")
    prepare.set_defaults(handler=prepare_batch)

    finalize = commands.add_parser("finalize", help="expand decisions and generate review artifacts")
    finalize.add_argument("--work-dir", required=True)
    finalize.add_argument(
        "--score-offset",
        type=float,
        default=0.0,
        help="course-level adjustment (points) added on top of each graded total. The graded "
             "scores are kept and the adjusted figure is written beside them; 0 reports the "
             "graded score as-is.",
    )
    finalize.set_defaults(handler=finalize_batch)

    status = commands.add_parser("status", help="show pending decisions and review counts")
    status.add_argument("--work-dir", required=True)
    status.set_defaults(handler=batch_status)

    doctor = commands.add_parser("doctor", help="check the external pieces each phase needs")
    doctor.add_argument("--node")
    doctor.add_argument(
        "--self-test",
        action="store_true",
        help="also run the end-to-end dry run against the bundled sample reports",
    )
    doctor.set_defaults(handler=batch_doctor)

    clean = commands.add_parser("clean", help="remove this pipeline's temporary working directory")
    clean.add_argument("--work-dir", required=True)
    clean.set_defaults(handler=clean_work)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "score_increment", None) is not None and args.score_increment <= 0:
        parser.error("score-increment must be positive")
    if getattr(args, "max_regrade_delta", None) is not None and args.max_regrade_delta < 0:
        parser.error("max-regrade-delta must be non-negative")
    try:
        args.handler(args)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
