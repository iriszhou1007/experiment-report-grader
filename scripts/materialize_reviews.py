#!/usr/bin/env python3
"""Expand compact model decisions into complete review JSON files."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from scoring_policy import calculate_deduction_scores, uses_deduction_policy


WARNING_LABELS = {
    "low_text_pages": "部分页面几乎没有可提取文字，可能包含扫描件、截图或图片内容。",
    "unreadable_text_layer": "该报告的文本层无法正常解码，脚本已剔除其中的乱码；正文可能因此缺失，扣分前请打开源报告核对。",
    "priority_headings_not_detected": "脚本未定位到评分重点章节标题，扣分前需核对源报告。",
    "compact_text_omitted": "评分材料包省略了非重点文字；需要时应补读对应源页面。",
    "visual_content_detected": "脚本检测到可能影响评分的图片、截图或扫描内容。",
    "visual_review_not_requested": "本批次未启用视觉复核；脚本标记的图片候选页尚未查看。",
    "packet_over_budget": "评分材料包达到了文字预算上限，各页摘录相应缩短；页面没有缺失，但单页内容可能更少。",
    "full_evidence_packet": "本批次使用完整证据模式：每页正文均完整送达，未做摘录截断，仅去除了共用模板文字。",
    "visual_review_sampled": "评分相关图片所在页较多，本批次只抽取了风险最高的若干页做视觉复核，其余候选页未查看。",
}

# Warnings that describe the *batch's* settings rather than this report's own risk. They fire on
# every report in a batch (full_evidence_packet was 62/62 in the first measured batch; the visual
# keys likewise whenever a screenshot appears anywhere), so leaving them in a per-report "需要教师
# 复核" list made every report look unvetted and buried the two hints that actually distinguish one
# report from another. They are collected once, per batch, and displayed above the report list.
# ``visual_review_not_requested`` / ``visual_review_sampled`` stay per-report on purpose: they only
# fire when the scan actually found candidate image pages, so they carry per-case information.
BATCH_LEVEL_WARNINGS = frozenset({
    "full_evidence_packet",
    "packet_over_budget",
    "visual_content_detected",
})
PAGE_REFERENCE = re.compile(r"(?:第\s*|\bp(?:age)?\.?\s*)(\d+)\s*(?:页)?", re.IGNORECASE)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_stem(value: str) -> str:
    stem = Path(value).stem
    return re.sub(r'[<>:"/\\|?*]+', "_", stem).strip(" .") or "report"


def normalized_rubric(data: dict) -> tuple[str, list[dict], float]:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Rubric items are required")
    result = []
    for item in items:
        criterion = str(item.get("criterion", "")).strip()
        maximum = float(item.get("max_score"))
        if not criterion or maximum <= 0:
            raise ValueError("Every rubric item needs a criterion and positive max_score")
        entry = {"criterion": criterion, "max_score": maximum}
        notes = item.get("grading_notes", [])
        if isinstance(notes, str):
            notes = [notes]
        if not isinstance(notes, list):
            raise ValueError(f"grading_notes in {criterion} must be a string or list")
        cleaned_notes = [str(note).strip() for note in notes if str(note).strip()]
        if cleaned_notes:
            entry["grading_notes"] = cleaned_notes
        # Optional score bands. They travel with the rubric into the grading packet so a
        # deduction has a stated anchor instead of whatever the grader felt was typical.
        bands = item.get("bands")
        if isinstance(bands, list):
            cleaned = []
            seen_ids = set()
            for band in bands:
                if isinstance(band, dict):
                    band_id = str(band.get("id", "")).strip()
                    description = str(band.get("description", band.get("label", ""))).strip()
                    try:
                        score = float(band.get("score"))
                    except (TypeError, ValueError):
                        raise ValueError(f"Rubric band in {criterion} needs a numeric score")
                    if not band_id or not description:
                        raise ValueError(f"Rubric band in {criterion} needs id and description")
                    if band_id in seen_ids:
                        raise ValueError(f"Duplicate rubric band id {band_id!r} in {criterion}")
                    if not math.isfinite(score) or score < 0 or score > maximum:
                        raise ValueError(f"Rubric band {band_id!r} score is outside {criterion}")
                    seen_ids.add(band_id)
                    normalized_band = {
                        "id": band_id,
                        "score": int(score) if score.is_integer() else score,
                        "description": description,
                    }
                    if bool(band.get("requires_visual_evidence", False)):
                        normalized_band["requires_visual_evidence"] = True
                    cleaned.append(normalized_band)
                else:
                    text = str(band).strip()
                    if text:
                        cleaned.append(text)
            if cleaned:
                if any(isinstance(band, dict) for band in cleaned) and not all(
                    isinstance(band, dict) for band in cleaned
                ):
                    raise ValueError(f"Rubric bands in {criterion} cannot mix fixed rules and text ranges")
                entry["bands"] = cleaned
        deduction_rules = item.get("deduction_rules")
        if isinstance(deduction_rules, list) and deduction_rules:
            if entry.get("bands"):
                raise ValueError(f"Rubric item {criterion} cannot mix bands and deduction_rules")
            cleaned_rules = []
            seen_rule_ids = set()
            for rule in deduction_rules:
                if not isinstance(rule, dict):
                    raise ValueError(f"Deduction rules in {criterion} must be objects")
                rule_id = str(rule.get("id", "")).strip()
                description = str(rule.get("description", "")).strip()
                try:
                    points = float(rule.get("points"))
                    max_occurrences = int(rule.get("max_occurrences", 1))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"Deduction rule in {criterion} needs numeric points and cap") from error
                if not rule_id or not description or rule_id in seen_rule_ids:
                    raise ValueError(f"Deduction rules in {criterion} need unique ids and descriptions")
                if not math.isfinite(points) or points <= 0 or points > maximum or max_occurrences < 1:
                    raise ValueError(f"Deduction rule {rule_id!r} is outside {criterion}")
                seen_rule_ids.add(rule_id)
                cleaned_rule = {
                    "id": rule_id,
                    "points": int(points) if points.is_integer() else points,
                    "description": description,
                    "max_occurrences": max_occurrences,
                }
                group = str(rule.get("group", "")).strip()
                if group:
                    cleaned_rule["group"] = group
                if bool(rule.get("requires_visual_evidence", False)):
                    cleaned_rule["requires_visual_evidence"] = True
                only_if_no_others = rule.get("only_if_no_other_deductions", False)
                if not isinstance(only_if_no_others, bool):
                    raise ValueError(f"only_if_no_other_deductions for {rule_id!r} must be boolean")
                if only_if_no_others:
                    cleaned_rule["only_if_no_other_deductions"] = True
                count_by = rule.get("count_by")
                if count_by is not None:
                    if count_by != "figure":
                        raise ValueError(f"Unsupported count_by for deduction rule {rule_id!r}")
                    cleaned_rule["count_by"] = count_by
                requirement_ids = rule.get("requirement_ids", [])
                if not isinstance(requirement_ids, list) or any(
                    not isinstance(value, str) or not value.strip() for value in requirement_ids
                ):
                    raise ValueError(f"requirement_ids for {rule_id!r} must be non-empty strings")
                if requirement_ids:
                    cleaned_rule["requirement_ids"] = sorted(set(value.strip() for value in requirement_ids))
                exclusive_with = rule.get("exclusive_with", [])
                if isinstance(exclusive_with, str):
                    exclusive_with = [exclusive_with]
                if not isinstance(exclusive_with, list):
                    raise ValueError(f"exclusive_with for {rule_id!r} in {criterion} must be a list")
                cleaned_exclusions = list(dict.fromkeys(
                    str(value).strip() for value in exclusive_with if str(value).strip()
                ))
                if rule_id in cleaned_exclusions:
                    raise ValueError(f"Deduction rule {rule_id!r} cannot exclude itself")
                if cleaned_exclusions:
                    cleaned_rule["exclusive_with"] = cleaned_exclusions
                cleaned_rules.append(cleaned_rule)
            known_rule_ids = {rule["id"] for rule in cleaned_rules}
            for rule in cleaned_rules:
                unknown = set(rule.get("exclusive_with", [])) - known_rule_ids
                if unknown:
                    raise ValueError(
                        f"Deduction rule {rule['id']!r} in {criterion} excludes unknown rules: "
                        + ", ".join(sorted(unknown))
                    )
            entry["deduction_rules"] = cleaned_rules
            caps = item.get("deduction_group_caps") or {}
            if not isinstance(caps, dict):
                raise ValueError(f"deduction_group_caps in {criterion} must be an object")
            normalized_caps = {}
            for group, cap in caps.items():
                value = float(cap)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f"Deduction cap {group!r} in {criterion} must be positive")
                normalized_caps[str(group)] = value
            if normalized_caps:
                entry["deduction_group_caps"] = normalized_caps
        result.append(entry)
    deduction_items = [bool(item.get("deduction_rules")) for item in result]
    if any(deduction_items) and not all(deduction_items):
        raise ValueError("A deduction rubric must define deduction_rules for every item")
    if all(deduction_items):
        policy = {}
        for key in ("minimum_report_deduction", "minimum_minor_deduction", "maximum_minor_items"):
            raw = data.get(key, 0)
            try:
                value = int(raw) if key == "maximum_minor_items" else float(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{key} must be numeric") from error
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{key} must be a finite non-negative number")
            if value:
                policy[key] = value
        if policy.get("minimum_minor_deduction", 0) > policy.get("minimum_report_deduction", math.inf):
            raise ValueError("minimum_minor_deduction cannot exceed minimum_report_deduction")
        for key in ("require_confirmed_deductions", "require_rule_fact_ids", "recheck_fact_consistency"):
            value = data.get(key, False)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
            if value:
                policy[key] = True
        if policy:
            result[0]["report_deduction_policy"] = policy
    maximum = sum(item["max_score"] for item in result)
    declared = float(data.get("max_score", maximum))
    if abs(maximum - declared) > 0.01:
        raise ValueError("Rubric item max_score does not equal the item maxima")
    target = data.get("distribution_target")
    if target is not None:
        if not isinstance(target, dict):
            raise ValueError("distribution_target must be an object")
        lower, upper = float(target["lower"]), float(target["upper"])
        fraction = float(target["minimum_fraction"])
        if not all(math.isfinite(value) for value in (lower, upper, fraction)) or not (
            0 <= lower <= upper <= maximum and 0 <= fraction < 1
        ):
            raise ValueError("Invalid distribution_target interval or fraction")
        result[0]["report_distribution_target"] = {
            "lower": lower, "upper": upper, "minimum_fraction": fraction,
        }
    return str(data.get("rubric_id", "confirmed-rubric")), result, maximum


def lookup_report(packet_reports: list[dict], decision: dict) -> dict:
    sha = str(decision.get("source_sha256", "")).lower()
    name = Path(str(decision.get("source_name") or decision.get("source_file") or "")).name.lower()
    matches = [
        report for report in packet_reports
        if (sha and report.get("source_sha256", "").lower() == sha)
        or (name and report.get("source_name", "").lower() == name)
    ]
    if len(matches) != 1:
        raise ValueError(f"Decision must match exactly one report, got {len(matches)} matches")
    return matches[0]


def sequence_or_default(value, length: int, default):
    if value is None:
        return [default() for _ in range(length)]
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"Expected a list with {length} entries")
    return value


def tidy_number(value: float):
    value = float(value)
    return int(value) if value.is_integer() else value


def normalized_comment(value) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value).replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalized_identity(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def validate_optional_identity(decision: dict, report: dict, field: str) -> None:
    supplied = str(decision.get(field, "")).strip()
    expected = str(report.get(field, "")).strip()
    if supplied and expected and normalized_identity(supplied) != normalized_identity(expected):
        raise ValueError(f"{field} does not match filename identity for {report['source_name']}")


def validate_evidence_pages(entries: list, page_count: int, source_name: str) -> list[str]:
    result = [str(value).strip() for value in entries if str(value).strip()]
    for entry in result:
        for matched in PAGE_REFERENCE.findall(entry):
            page = int(matched)
            if page < 1 or page > page_count:
                raise ValueError(f"Evidence page {page} is outside {source_name} (1-{page_count})")
    return result


def referenced_pages(entries: list[str]) -> set[int]:
    return {
        int(matched)
        for entry in entries
        for matched in PAGE_REFERENCE.findall(entry)
    }


def format_mean(value: float) -> str:
    """One decimal so the headline figure can be quoted without re-rounding."""
    return f"{value:.1f}"


def write_index(output_dir: Path, out_path: Path | None = None) -> Path:
    rows = []
    graded_totals = []
    adjusted_totals = []
    offsets = set()
    for path in sorted(output_dir.glob("*.review.json")):
        review = load_json(path)
        status = (
            "已审核" if review.get("review_status") == "approved"
            else "待裁决" if review.get("adjudication_status") == "required"
            else "待审核"
        )
        warnings = "；".join(str(value) for value in review.get("warnings", []))
        total = review.get("total_score")
        adjusted = review.get("adjusted_total")
        offset = review.get("score_offset")
        if isinstance(total, (int, float)):
            graded_totals.append(float(total))
        if isinstance(adjusted, (int, float)):
            adjusted_totals.append(float(adjusted))
        if isinstance(offset, (int, float)) and offset > 0:
            offsets.add(float(offset))
        rows.append([
            Path(str(review.get("source_file", ""))).name,
            str(review.get("student_name", "")),
            str(review.get("student_id", "")),
            str(tidy_number(total) if isinstance(total, (int, float)) else ""),
            str(tidy_number(adjusted) if isinstance(adjusted, (int, float)) else ""),
            status,
            warnings,
        ])

    # The graded mean and the reported mean are different numbers whenever the
    # course applies an offset, so both are stated by name instead of leaving one
    # of them to be inferred from the table.
    has_adjustment = bool(adjusted_totals) and not all(
        abs(adjusted - graded) < 1e-9 for adjusted, graded in zip(adjusted_totals, graded_totals)
    )
    summary = [f"- 报告数：{len(rows)}"]
    if graded_totals:
        summary.append(f"- 原始平均分（批改得分）：{format_mean(sum(graded_totals) / len(graded_totals))}")
    if has_adjustment:
        description = "课程整体调整"
        if len(offsets) == 1:
            offset = tidy_number(next(iter(offsets)))
            description = f"每份 +{offset}，上限 100"
        summary.append(
            f"- 调整后平均分（上报成绩）：{format_mean(sum(adjusted_totals) / len(adjusted_totals))}"
            f"（{description}）"
        )
        summary.append(
            "- 调整只改变上报成绩，评分维度得分与总分保持批改原值，扣分理由不变；"
            "「调整后成绩」由「总分」加偏移量算出，故改动总分时该列同步变化。"
        )

    headers = ["源文件", "姓名", "学号", "总分"]
    aligns = ["---", "---", "---", "---:"]
    if has_adjustment:
        headers.append("调整后成绩")
        aligns.append("---:")
    headers += ["状态", "复核提示"]
    aligns += ["---", "---"]

    lines = ["# 实验报告批改索引", ""]
    lines += summary
    lines += ["", "| " + " | ".join(headers) + " |", "|" + "|".join(aligns) + "|"]
    for row in rows:
        cells = row[:4]
        if has_adjustment:
            cells = cells + row[4:5]
        cells = cells + row[5:]
        escaped = [value.replace("|", "\\|").replace("\n", " ") for value in cells]
        lines.append("| " + " | ".join(escaped) + " |")
    index_path = out_path if out_path is not None else output_dir.parent / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=False)
    parser.add_argument("--rubric", required=False)
    parser.add_argument("--decisions", required=False)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="refresh index.md from the review JSON already in --output-dir and exit; used "
             "after an in-panel approval so the index does not keep saying 待审核",
    )
    parser.add_argument("--index-out", type=Path, help="write index.md here instead")
    parser.add_argument("--score-increment", type=float, default=1.0)
    parser.add_argument("--min-comment-chars", type=int, default=8)
    parser.add_argument("--max-comment-chars", type=int, default=800)
    parser.add_argument(
        "--score-offset",
        type=float,
        default=0.0,
        help="course-level adjustment added on top of the graded total (0 = report the graded "
             "score as-is). The graded item scores and total are always kept, and the adjusted "
             "figure is written alongside them so a raised grade stays defensible.",
    )
    args = parser.parse_args()

    if args.index_only:
        index_path = write_index(Path(args.output_dir).resolve(), args.index_out)
        print(json.dumps({"ok": True, "index": str(index_path)}, ensure_ascii=False))
        return

    for required in ("packet", "rubric", "decisions"):
        if not getattr(args, required):
            raise SystemExit(f"--{required} is required unless --index-only is used")

    packet = load_json(Path(args.packet))
    rubric_id, rubric_items, max_score = normalized_rubric(load_json(Path(args.rubric)))
    decision_data = load_json(Path(args.decisions))
    decisions = decision_data.get("reports") if isinstance(decision_data, dict) else decision_data
    if not isinstance(decisions, list) or not decisions:
        raise SystemExit("Decisions must contain a non-empty reports list")

    packet_reports = packet.get("reports")
    if not isinstance(packet_reports, list) or not packet_reports:
        raise SystemExit("Packet contains no reports")
    output_dir = Path(args.output_dir).resolve()
    if args.score_increment <= 0:
        raise SystemExit("score-increment must be positive")
    if not math.isfinite(args.score_offset):
        raise SystemExit("score-offset must be finite")
    if args.min_comment_chars < 1 or args.max_comment_chars < args.min_comment_chars:
        raise SystemExit("Invalid comment length limits")
    output_dir.mkdir(parents=True, exist_ok=True)
    pending_writes: list[tuple[Path, dict]] = []
    seen_reports: set[str] = set()

    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("Every decision must be an object")
        report = lookup_report(packet_reports, decision)
        report_key = str(report["source_sha256"]).lower()
        if report_key in seen_reports:
            raise ValueError(f"Duplicate decision for {report['source_name']}")
        seen_reports.add(report_key)
        validate_optional_identity(decision, report, "student_id")
        validate_optional_identity(decision, report, "student_name")
        deduction_mode = uses_deduction_policy(rubric_items)
        normalized_deductions = []
        if deduction_mode:
            calculated_scores, normalized_deductions = calculate_deduction_scores(decision, rubric_items)
            decision["scores"] = calculated_scores
            decision["deductions"] = normalized_deductions
        scores = sequence_or_default(decision.get("scores"), len(rubric_items), lambda: None)
        comments = sequence_or_default(decision.get("item_comments"), len(rubric_items), str)
        evidence = sequence_or_default(decision.get("evidence"), len(rubric_items), list)
        visual_evidence = sequence_or_default(decision.get("visual_evidence"), len(rubric_items), list)
        score_rules = sequence_or_default(decision.get("score_rules"), len(rubric_items), str)
        try:
            numeric_scores = [float(score) for score in scores]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Scores must be numeric for {report['source_name']}") from exc
        for index, score in enumerate(numeric_scores):
            if not math.isfinite(score):
                raise ValueError(f"Score {index + 1} is not finite")
            if score < 0 or score > rubric_items[index]["max_score"]:
                raise ValueError(f"Score {index + 1} is outside its rubric range")
            steps = score / args.score_increment
            if abs(steps - round(steps)) > 1e-9:
                raise ValueError(f"Score {index + 1} does not follow the configured increment")
        calculated_total = sum(numeric_scores)
        if decision.get("total_score") is not None:
            declared_total = float(decision["total_score"])
            if not math.isfinite(declared_total) or abs(declared_total - calculated_total) > 1e-8:
                raise ValueError(f"Declared total_score does not equal item scores for {report['source_name']}")
        overall_comment = normalized_comment(decision.get("overall_comment", ""))
        if not overall_comment:
            raise ValueError(f"Missing overall_comment for {report['source_name']}")
        comment_length = len(re.sub(r"\s+", "", overall_comment))
        if comment_length < args.min_comment_chars or comment_length > args.max_comment_chars:
            raise ValueError(
                f"overall_comment for {report['source_name']} must contain "
                f"{args.min_comment_chars}-{args.max_comment_chars} non-space characters"
            )

        review_items = []
        for index, rubric_item in enumerate(rubric_items):
            fixed_bands = rubric_item.get("bands", [])
            fixed_bands = fixed_bands if fixed_bands and all(isinstance(band, dict) for band in fixed_bands) else []
            selected_rule = str(score_rules[index]).strip()
            if fixed_bands:
                rules_by_id = {str(band["id"]): band for band in fixed_bands}
                if selected_rule not in rules_by_id:
                    raise ValueError(
                        f"score_rules item {index + 1} for {report['source_name']} must be one of "
                        + ", ".join(rules_by_id)
                    )
                expected_score = float(rules_by_id[selected_rule]["score"])
                if abs(numeric_scores[index] - expected_score) > 1e-9:
                    raise ValueError(
                        f"Score {index + 1} for {report['source_name']} must be {expected_score:g} "
                        f"when score rule {selected_rule!r} is selected"
                    )
                if not normalized_comment(comments[index]):
                    raise ValueError(
                        f"Item comment {index + 1} is required for reproducible scoring of "
                        f"{report['source_name']}"
                    )
            elif deduction_mode and not normalized_comment(comments[index]):
                raise ValueError(
                    f"Item comment {index + 1} is required for deduction scoring of "
                    f"{report['source_name']}"
                )
            item_evidence = evidence[index]
            if not isinstance(item_evidence, list):
                raise ValueError("Each evidence entry must be a list")
            validated_evidence = validate_evidence_pages(
                item_evidence,
                int(report.get("page_count", 0)),
                report["source_name"],
            )
            if (fixed_bands or deduction_mode) and not validated_evidence:
                raise ValueError(
                    f"Evidence item {index + 1} is required for reproducible scoring of "
                    f"{report['source_name']}"
                )
            item_visual_evidence = visual_evidence[index]
            if not isinstance(item_visual_evidence, list):
                raise ValueError("Each visual_evidence entry must be a list")
            validated_visual_evidence = validate_evidence_pages(
                item_visual_evidence,
                int(report.get("page_count", 0)),
                report["source_name"],
            )
            selected_band = rules_by_id.get(selected_rule, {}) if fixed_bands else {}
            if bool(selected_band.get("requires_visual_evidence", False)):
                visual_pages = referenced_pages(validated_visual_evidence)
                if not validated_visual_evidence or not visual_pages:
                    raise ValueError(
                        f"Rule {selected_rule!r} for item {index + 1} of {report['source_name']} "
                        "requires at least one visually inspected page in visual_evidence"
                    )
                evidence_pages = referenced_pages(validated_evidence)
                if not visual_pages.issubset(evidence_pages):
                    raise ValueError(
                        f"Visual evidence pages for item {index + 1} of {report['source_name']} "
                        "must also appear in evidence"
                    )
            review_item = {
                "criterion": rubric_item["criterion"],
                "score": tidy_number(numeric_scores[index]),
                "max_score": tidy_number(rubric_item["max_score"]),
                "comment": normalized_comment(comments[index]),
                "evidence": validated_evidence,
            }
            if validated_visual_evidence:
                review_item["visual_evidence"] = validated_visual_evidence
            if fixed_bands and selected_rule:
                review_item["score_rule"] = selected_rule
                review_item["score_rule_description"] = str(
                    rules_by_id[selected_rule].get("description", "")
                )
            if deduction_mode:
                item_deductions = []
                for deduction in normalized_deductions:
                    if int(deduction["item"]) != index + 1:
                        continue
                    deduction_evidence = validate_evidence_pages(
                        deduction["evidence"],
                        int(report.get("page_count", 0)),
                        report["source_name"],
                    )
                    deduction_visual = validate_evidence_pages(
                        deduction.get("visual_evidence", []),
                        int(report.get("page_count", 0)),
                        report["source_name"],
                    )
                    if deduction_visual and not referenced_pages(deduction_visual).issubset(
                        referenced_pages(deduction_evidence)
                    ):
                        raise ValueError(
                            f"Visual evidence for deduction {deduction['fact']!r} of "
                            f"{report['source_name']} must also appear in evidence"
                        )
                    item_deductions.append({
                        **deduction,
                        "evidence": deduction_evidence,
                        "visual_evidence": deduction_visual,
                    })
                review_item["deductions"] = item_deductions
            review_items.append(review_item)

        warnings = [
            WARNING_LABELS.get(str(value), str(value))
            for value in report.get("warnings", [])
            if str(value) not in BATCH_LEVEL_WARNINGS
        ]
        decision_warnings = decision.get("warnings", [])
        if not isinstance(decision_warnings, list):
            raise ValueError(f"warnings must be a list for {report['source_name']}")
        warnings.extend(normalized_comment(value) for value in decision_warnings if normalized_comment(value))
        if packet.get("inspection_mode") == "text_only":
            warnings.append("本草稿采用仅文字检查；依赖截图、图表或扫描内容的细节尚未进行视觉核验。")
        warnings = list(dict.fromkeys(warnings))
        review = {
            "schema_version": "1.0",
            "source_file": report["source_file"],
            "source_sha256": report["source_sha256"],
            "student_id": report.get("student_id", ""),
            "student_name": report.get("student_name", ""),
            "experiment": decision.get("experiment") or report.get("experiment", ""),
            "rubric_id": rubric_id,
            "review_status": "draft",
            "approved_at": None,
            "total_score": tidy_number(calculated_total),
            "max_score": tidy_number(max_score),
        }
        if args.score_offset:
            review["score_offset"] = tidy_number(args.score_offset)
            review["adjusted_total"] = tidy_number(min(max_score, calculated_total + args.score_offset))
        review["items"] = review_items
        review["overall_comment"] = overall_comment
        review["warnings"] = warnings

        output = output_dir / f"{safe_stem(report['source_name'])}.review.json"
        if output.exists():
            existing = load_json(output)
            if existing.get("review_status") == "approved":
                raise ValueError(f"Refusing to replace approved review: {output.name}")
        pending_writes.append((output, review))

    written = []
    for output, review in pending_writes:
        output.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(output.name)

    index_path = write_index(output_dir)
    print(json.dumps({
        "output_dir": str(output_dir),
        "validated": len(pending_writes),
        "reviews": len(written),
        "files": written,
        "index": str(index_path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
