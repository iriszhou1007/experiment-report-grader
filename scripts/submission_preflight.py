"""Inventory report submissions before grading; never treat non-PDF files as score records."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from prepare_grading_packet import parse_identity


def scan(report_dir: Path) -> dict:
    entries = []
    experiments: dict[str, list[str]] = {}
    for path in sorted((p for p in report_dir.iterdir() if p.is_file()), key=lambda p: p.name.casefold()):
        suffix = path.suffix.casefold()
        if suffix not in {".pdf", ".doc", ".docx"}:
            continue
        student_id, student_name, experiment = parse_identity(path)
        fields = [part for part in path.stem.replace("-", "_").split("_") if part.strip()]
        experiment_tokens = [part for part in fields if part.lower().startswith(("实验", "第"))]
        if experiment_tokens:
            experiment = experiment_tokens[-1]
        elif experiment and not experiment.lower().startswith(("实验", "第")):
            experiment = ""
        reasons = []
        identity_ok = bool(student_id and student_name)
        if suffix in {".doc", ".docx"}:
            status = "需重新提交 PDF"
            reasons.append("收到 Word 文件；本批次只评分 PDF")
        else:
            status = "可进入评分" if identity_ok else "需重新提交：文件名缺少姓名或学号"
        if not student_id:
            reasons.append("文件名未包含或无法识别学号，需重新提交并规范文件名")
        if not student_name:
            reasons.append("文件名未包含或无法识别姓名，需重新提交并规范文件名")
        if experiment and suffix == ".pdf":
            experiments.setdefault(experiment, []).append(path.name)
        entries.append({
            "file": path.name,
            "file_type": suffix[1:].upper(),
            "status": status,
            "student_id": student_id,
            "student_name": student_name,
            "experiment": experiment,
            "reasons": reasons,
            "included_in_grading": suffix == ".pdf" and identity_ok,
        })
    eligible_pdfs = [entry for entry in entries if entry["file_type"] == "PDF" and entry["included_in_grading"]]
    pdf_keys = {
        (entry["student_id"], entry["experiment"].casefold())
        for entry in eligible_pdfs
    }
    pdf_stems = {Path(entry["file"]).stem.casefold() for entry in eligible_pdfs}
    for entry in entries:
        if entry["file_type"] in {"DOC", "DOCX"} and (
            Path(entry["file"]).stem.casefold() in pdf_stems
            or (entry["student_id"] and (entry["student_id"], entry["experiment"].casefold()) in pdf_keys)
        ):
            entry["status"] = "未纳入评分（已收到 PDF）"
            entry["reasons"] = ["同一目录已收到 PDF；Word 文件仅作旁件，不参与评分"]
    return {"entries": entries, "experiment_groups": experiments, "pdf_count": sum(e["file_type"] == "PDF" for e in entries)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: submission_preflight.py <report-directory>")
    print(json.dumps(scan(Path(sys.argv[1]).resolve()), ensure_ascii=False, separators=(",", ":")))
