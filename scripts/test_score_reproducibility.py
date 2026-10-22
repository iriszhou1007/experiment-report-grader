#!/usr/bin/env python3
"""Regression tests for deterministic deductions and blind-regrade quarantine."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

import batch_pipeline
from materialize_reviews import normalized_rubric
from scoring_policy import calculate_deduction_scores, minimum_offset_for_average, score_distribution


ROOT = Path(__file__).resolve().parent.parent
RUBRIC_PATH = ROOT / "assets" / "default-rubric.json"
EXPERIMENT_ONE_RUBRIC_PATH = ROOT / "assets" / "rubrics" / "experiment-1-web-http.json"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def deduction(
    item: int,
    rule: str,
    fact: str,
    reason: str,
    evidence: list[str] | None = None,
    visual: list[str] | None = None,
    certainty: str = "confirmed",
    requirements: list[str] | None = None,
) -> dict:
    return {
        "item": item,
        "rule": rule,
        "fact": fact,
        "reason": reason,
        "certainty": certainty,
        "evidence": evidence or ["p.1"],
        "visual_evidence": visual or [],
        **({"requirements": requirements} if requirements is not None else {}),
    }


def decision(name: str, deductions: list[dict], scores: list[int] | None = None, item_count: int = 5) -> dict:
    result = {
        "source_name": name,
        "deductions": deductions,
        "overall_comment": "证据完整，程序根据逐项扣分事实计算分数。",
        "item_comments": ["依据对应页面核对，未发现其他明确问题。"] * item_count,
        "evidence": [["p.1"] for _ in range(item_count)],
        "visual_evidence": [[] for _ in range(item_count)],
    }
    if scores is not None:
        result["scores"] = scores
    return result


def materialize(temp: Path, rubric: dict, value: dict, name: str) -> subprocess.CompletedProcess:
    packet_path = temp / f"packet-{name}.json"
    rubric_path = temp / f"rubric-{name}.json"
    decisions_path = temp / f"decisions-{name}.json"
    output_dir = temp / f"reviews-{name}"
    write_json(packet_path, {
        "inspection_mode": "smart",
        "reports": [{
            "source_name": "student.pdf",
            "source_file": str(temp / "student.pdf"),
            "source_sha256": "abc123",
            "student_id": "1",
            "student_name": "测试学生",
            "page_count": 2,
            "warnings": [],
        }],
    })
    write_json(rubric_path, rubric)
    write_json(decisions_path, {"reports": [value]})
    return subprocess.run([
        sys.executable,
        str(ROOT / "scripts" / "materialize_reviews.py"),
        "--packet", str(packet_path),
        "--rubric", str(rubric_path),
        "--decisions", str(decisions_path),
        "--output-dir", str(output_dir),
    ], capture_output=True, text=True, encoding="utf-8")


def main() -> None:
    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    _, items, maximum = normalized_rubric(rubric)
    assert maximum == 100
    assert rubric["minimum_published_average"] == 90
    assert "minimum_report_deduction" not in rubric
    assert "minimum_minor_deduction" not in rubric
    assert items[1]["deduction_rules"][0]["id"] == "required_question_unanswered"
    assert items[4]["deduction_rules"][0]["requires_visual_evidence"] is True
    assert items[0]["grading_notes"]
    header = batch_pipeline.packet_tools.rubric_header(rubric)
    assert 'program calculates "s"' in header
    assert '"s" must be exactly' not in header
    assert "不要因篇幅短" in header

    legacy_items = [{
        "criterion": "legacy",
        "max_score": 10,
        "bands": [{"id": "ok", "score": 10, "description": "legacy fixed rule"}],
    }]
    legacy = {
        "source_name": "legacy.pdf",
        "scores": [10],
        "score_rules": ["ok"],
        "overall_comment": "legacy",
        "item_comments": ["rule matched"],
        "evidence": [["p.1"]],
    }
    batch_pipeline.validate_scoring_protocol([legacy], legacy_items, 1)

    perfect = decision("student.pdf", [])
    batch_pipeline.validate_scoring_protocol([perfect], items, 1)
    assert perfect["scores"] == [20, 30, 15, 20, 15]

    major = decision("student.pdf", [
        deduction(2, "required_question_unanswered", "q7_unanswered", "第7题完全未回答"),
    ])
    batch_pipeline.validate_scoring_protocol([major], items, 1)
    assert major["scores"] == [20, 27, 15, 20, 15]

    duplicate = decision("student.pdf", [
        deduction(1, "important_section_blank", "conclusion_blank", "结论栏空白", visual=["p.1"]),
        deduction(3, "conclusion_blank_or_placeholder", "conclusion_blank", "结论栏空白"),
    ])
    try:
        calculate_deduction_scores(duplicate, items)
    except ValueError as error:
        assert "charged more than once" in str(error)
    else:
        raise AssertionError("The same fact must not be deducted twice")

    uncertain_major = decision("student.pdf", [
        deduction(
            2,
            "required_question_unanswered",
            "q7_uncertain",
            "摘录中未看到第7题",
            certainty="uncertain",
        ),
    ])
    try:
        calculate_deduction_scores(uncertain_major, items)
    except ValueError as error:
        assert "without confirmed evidence" in str(error)
    else:
        raise AssertionError("An uncertain observation must not deduct more than two points")

    visual_missing = decision("student.pdf", [
        deduction(5, "hard_to_read", "layout_unreadable", "页面明显难读"),
    ])
    try:
        calculate_deduction_scores(visual_missing, items)
    except ValueError as error:
        assert "requires visual evidence" in str(error)
    else:
        raise AssertionError("A visual deduction needs a visually inspected page")

    visual_ok = decision("student.pdf", [
        deduction(5, "hard_to_read", "layout_unreadable", "页面明显难读", visual=["p.1"]),
    ])
    batch_pipeline.validate_scoring_protocol([visual_ok], items, 1)
    assert visual_ok["scores"][-1] == 12

    experiment_one = json.loads(EXPERIMENT_ONE_RUBRIC_PATH.read_text(encoding="utf-8"))
    experiment_id, experiment_items, experiment_maximum = normalized_rubric(experiment_one)
    assert experiment_id == "experiment-1-web-http-v6-counted-figures"
    assert experiment_maximum == 100
    assert [item["max_score"] for item in experiment_items] == [30, 35, 25, 10]
    guide = ROOT / experiment_one["source_requirements"]["path"]
    assert hashlib.sha256(guide.read_bytes()).hexdigest() == experiment_one["source_requirements"]["sha256"]
    assert set(experiment_items[0]["deduction_rules"][1]["requirement_ids"]) == {
        "S-choice", "S-origin", "S-root", "S-index", "S-binding", "S-url", "S-mapping",
    }
    environment_ids = experiment_items[3]["deduction_rules"][0]["requirement_ids"]
    assert set(environment_ids) == {"ENV-OS", "ENV-server", "ENV-browser", "ENV-Wireshark"}
    experiment_header = batch_pipeline.packet_tools.rubric_header(experiment_one)
    assert "HTTP-Q10" in experiment_header and "CACHE-Q4" in experiment_header
    assert "不得同时使用" in experiment_header
    assert "fact=rule" in experiment_header
    normalized_header = batch_pipeline.packet_tools.rubric_header({"items": experiment_items, "max_score": 100})
    assert "fact=rule" in normalized_header
    assert "explicit certainty=confirmed" in normalized_header
    indentation_rule = next(rule for rule in experiment_items[3]["deduction_rules"]
                            if rule["id"] == "body_first_line_indent_missing")
    assert indentation_rule["only_if_no_other_deductions"] is True
    assert indentation_rule["requires_visual_evidence"] is True
    assert "仅当整份报告没有任何其他扣分时启用" in normalized_header
    assert "screenshot_documentation_issue" in normalized_header
    bad_flag = json.loads(json.dumps(experiment_one))
    bad_flag["items"][3]["deduction_rules"][-1]["only_if_no_other_deductions"] = "true"
    try:
        normalized_rubric(bad_flag)
    except ValueError as error:
        assert "must be boolean" in str(error)
    else:
        raise AssertionError("A conditional deduction flag must not accept truthy strings")
    # Every executable rule must retain a report-requirement reference in its packet text.
    report_refs = set(experiment_one["report_requirements"])
    for item in experiment_items:
        for rule in item["deduction_rules"]:
            ref = rule["description"].split("]", 1)[0].removeprefix("[")
            assert ref in report_refs
    assert "[RPT-1]" in normalized_header and "[RPT-4]" in normalized_header

    def exp_decision(deductions):
        return decision("student.pdf", deductions, item_count=len(experiment_items))

    def exp_d(item, rule, requirements=None, visual=None):
        return deduction(item, rule, rule, "完整报告及相关原页核对后确认该项缺失。", requirements=requirements, visual=visual)

    # One selected server has the full 30-point weight; no second-server deduction exists.
    exp_full = exp_decision([])
    batch_pipeline.validate_scoring_protocol([exp_full], experiment_items, 1)
    assert exp_full["scores"] == [30, 35, 25, 10]
    scores, _ = calculate_deduction_scores(exp_decision([
        exp_d(1, "server_configuration_issue", ["S-url", "S-mapping"]),
    ]), experiment_items)
    assert scores == [28, 35, 25, 10]

    # One image with three missing elements is still one 2-point occurrence.
    caption_fact = {
        **exp_d(4, "screenshot_documentation_issue",
                ["FIG-numbering", "FIG-reference", "FIG-description"], visual=["p.1"]),
        "figures": [{"id": "p.1#image1", "requirements": ["FIG-numbering", "FIG-reference", "FIG-description"],
                     "reason": "该截图缺少图号、正文引用和说明。"}],
    }
    caption_missing = exp_decision([caption_fact])
    caption_missing["visual_evidence"][3] = ["p.1"]
    scores, ledger = calculate_deduction_scores(caption_missing, experiment_items)
    assert scores == [30, 35, 25, 8] and len(ledger) == 1
    caption_rule = next(rule for rule in experiment_items[3]["deduction_rules"]
                        if rule["id"] == "screenshot_documentation_issue")
    assert caption_rule["max_occurrences"] == 3 and caption_rule["count_by"] == "figure"
    assert "最多计3张" in normalized_header and "figures:" in normalized_header
    for count, expected_total in [(1, 98), (2, 96), (3, 94), (4, 94), (5, 94)]:
        counted = {**caption_fact, "figures": [
            {**caption_fact["figures"][0], "id": f"p.1#image{number}"} for number in range(1, count+1)
        ]}
        counted_scores, normalized_ledger = calculate_deduction_scores(exp_decision([counted]), experiment_items)
        assert sum(counted_scores) == expected_total
        assert len(normalized_ledger[0]["figures"]) == min(count, 3)
    bad_figures = [
        [],
        caption_fact["figures"] * 2,
        [{**caption_fact["figures"][0], "id": "图1"}],
        [{**caption_fact["figures"][0], "id": "p.0#image1"}],
        [{**caption_fact["figures"][0], "id": "p.2#image1"}],  # Own page not visually checked.
        [{**caption_fact["figures"][0], "requirements": []}],
        [{**caption_fact["figures"][0], "requirements": ["not-a-figure-element"]}],
        [{**caption_fact["figures"][0], "requirements": ["FIG-numbering"]}],  # Parent union mismatch.
        [{**caption_fact["figures"][0], "reason": ""}],
    ]
    for figures in bad_figures:
        try:
            calculate_deduction_scores(exp_decision([{**caption_fact, "figures": figures}]), experiment_items)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid/duplicate/unverified screenshot evidence was accepted")

    indent_fact = exp_d(4, "body_first_line_indent_missing", ["BODY-first-line-indent"], visual=["p.1"])
    indent_only = exp_decision([indent_fact])
    indent_only["visual_evidence"][3] = ["p.1"]
    scores, _ = calculate_deduction_scores(indent_only, experiment_items)
    assert scores == [30, 35, 25, 7]  # Sole confirmed indentation defect: 97, not a score ceiling.
    for other in [
        exp_d(1, "server_configuration_issue", ["S-url"]),
        exp_d(2, "http_answer_issue", ["HTTP-Q1"]),
        exp_d(3, "conditional_answer_issue", ["CACHE-Q3"]),
        exp_d(4, "source_and_help_missing", ["SOURCE"]),
        caption_fact,
    ]:
        ordinary_scores, _ = calculate_deduction_scores(exp_decision([other]), experiment_items)
        assert sum(ordinary_scores) == 98  # Existing 2-point defect disables the indentation rule.
        for pair in ([indent_fact, other], [other, indent_fact]):
            try:
                calculate_deduction_scores(exp_decision(pair), experiment_items)
            except ValueError as error:
                assert "only if no other deductions exist" in str(error)
            else:
                raise AssertionError("Conditional indentation deduction stacked with another item/rule")

    exclusive = exp_decision([
        exp_d(2, "http_analysis_absent", visual=["p.1"]),
        exp_d(2, "http_required_capture_absent", visual=["p.1"]),
    ])
    try:
        calculate_deduction_scores(exclusive, experiment_items)
    except ValueError as error:
        assert "mutually exclusive" in str(error)
    else:
        raise AssertionError("Task-level and component-level deductions must not stack")

    for ledger in (exclusive["deductions"], list(reversed(exclusive["deductions"]))):
        try:
            calculate_deduction_scores(exp_decision(ledger), experiment_items)
        except ValueError as error:
            assert "mutually exclusive" in str(error)
        else:
            raise AssertionError("Exclusion must work in both orders")

    ordinary = [
        exp_d(4, "source_and_help_missing", ["SOURCE"]),
        exp_d(4, "environment_record_missing", ["ENV-browser"]),
        exp_d(4, "reflection_answers_missing", ["R1", "R2"]),
        exp_d(4, "verification_process_missing", ["VERIFY"]),
    ]
    for count, expected in enumerate([100, 98, 96, 94, 92]):
        scores, _ = calculate_deduction_scores(exp_decision(ordinary[:count]), experiment_items)
        assert sum(scores) == expected
    scores, _ = calculate_deduction_scores(exp_decision([
        exp_d(2, "http_analysis_absent", visual=["p.1"]),
        exp_d(3, "conditional_analysis_absent", visual=["p.1"]),
    ]), experiment_items)
    assert sum(scores) == 84  # Real substantial omissions are not lifted into the target interval.
    invalid = [
        {**ordinary[0], "certainty": "uncertain"},
        {key: value for key, value in ordinary[0].items() if key != "certainty"},
        {**ordinary[0], "fact": "freely_named_fact"},
        {**ordinary[0], "requirements": ["not_a_requirement"]},
        {**ordinary[0], "requirements": []},
        exp_d(1, "iis_required_answers_missing"),  # Optional thinking questions are not a rule.
        exp_d(1, "personal_page_issue", ["T1-title"], visual=["p.1"]),
        exp_d(1, "personal_page_issue", ["T1-description"], visual=["p.1"]),
        exp_d(1, "iis_configuration_issue", ["T1-http"]),
        exp_d(2, "apache_configuration_issue", ["T2-config"]),
        exp_d(4, "hard_to_read", visual=["p.1"]),
        exp_d(2, "http_evidence_link_missing", ["HTTP-figure-link"]),
        exp_d(3, "conditional_second_visit_missing"),
        exp_d(3, "conditional_evidence_link_missing", ["CACHE-figure-link"]),
        exp_d(2, "http_analysis_absent"),  # Even whole-absence claims need a source-page check.
        exp_d(4, "screenshot_documentation_issue", ["FIG-numbering"]),  # Requires original-page viewing.
        exp_d(4, "body_first_line_indent_missing", ["BODY-first-line-indent"]),
        exp_d(4, "body_first_line_indent_missing", ["not_a_body_requirement"], visual=["p.1"]),
    ]
    for entry in invalid:
        try:
            calculate_deduction_scores(exp_decision([entry]), experiment_items)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid experiment-1 deduction was accepted")

    # Several explicit ordinary problems within a protocol task still cost only 2 points.
    grouped = exp_decision([exp_d(
        2, "http_answer_issue", ["HTTP-Q9", "HTTP-Q10", "HTTP-figure-link"]
    )])
    scores, ledger = calculate_deduction_scores(grouped, experiment_items)
    assert sum(scores) == 98
    assert len(ledger) == 1
    # Partly missing second-visit analysis is one ordinary cache issue, not an extra 4 points.
    scores, _ = calculate_deduction_scores(exp_decision([exp_d(
        3, "conditional_answer_issue", ["CACHE-Q3", "CACHE-Q4"]
    )]), experiment_items)
    assert sum(scores) == 98

    target = experiment_one["distribution_target"]
    assert score_distribution([98] * 8 + [100, 84], target)["target_met"] is False
    assert score_distribution([98] * 9 + [100], target)["target_met"] is True
    assert score_distribution([92, 98, 99, 91], target)["in_range"] == 2
    assert score_distribution([], target)["target_met"] is None

    assert minimum_offset_for_average([80, 85, 100], 100, 90, 1) == 3
    assert minimum_offset_for_average([90, 91], 100, 90, 1) == 0

    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        # finalize also refreshes the service UI; keep that scratch output isolated from
        # any real batch currently being reviewed on the developer's machine.
        batch_pipeline.WORKSPACE_DIR = temp / "workspace"
        # Both artifact materialization and the batch scorer enforce the new condition.
        caption_output = materialize(temp, experiment_one, caption_missing, "exp-captions")
        assert caption_output.returncode == 0, caption_output.stderr
        saved_caption = json.loads(next((temp / "reviews-exp-captions").glob("*.review.json")).read_text())
        assert saved_caption["items"][3]["deductions"][0]["figures"][0]["id"] == "p.1#image1"
        two_figures = exp_decision([{**caption_fact, "figures": [
            caption_fact["figures"][0], {**caption_fact["figures"][0], "id": "p.1#image2"},
        ]}])
        two_figures["visual_evidence"][3] = ["p.1"]
        counted_output = materialize(temp, experiment_one, two_figures, "exp-two-figures")
        assert counted_output.returncode == 0, counted_output.stderr
        counted_review = json.loads(next((temp / "reviews-exp-two-figures").glob("*.review.json")).read_text())
        assert counted_review["total_score"] == 96
        indent_output = materialize(temp, experiment_one, indent_only, "exp-indent")
        assert indent_output.returncode == 0, indent_output.stderr
        indent_review = json.loads(next((temp / "reviews-exp-indent").glob("*.review.json")).read_text())
        assert indent_review["total_score"] == 97
        stacked = materialize(temp, experiment_one, exp_decision([caption_fact, indent_fact]), "exp-stacked")
        assert stacked.returncode != 0 and "only if no other deductions exist" in stacked.stderr
        # The condition must survive normalization and change the resume signature.
        with_flag = temp / "with-condition.json"
        without_flag = temp / "without-condition.json"
        write_json(with_flag, experiment_one)
        changed = json.loads(json.dumps(experiment_one))
        changed["items"][3]["deduction_rules"][-1]["only_if_no_other_deductions"] = False
        write_json(without_flag, changed)
        assert batch_pipeline.rubric_signature(with_flag)[3] != batch_pipeline.rubric_signature(without_flag)[3]
        for value in (indent_only, caption_missing):
            batch_pipeline.validate_scoring_protocol([value], experiment_items, 1)
        conditional_dispute = batch_pipeline.compare_regrade_passes(
            [indent_only], [caption_missing], 2, temp, experiment_items,
        )
        assert conditional_dispute["reports"][0]["absolute_delta"] == 1
        assert conditional_dispute["review_required"] == 1  # Different facts still require adjudication.
        batch_pipeline.validate_scoring_protocol([two_figures], experiment_items, 1)
        counted_dispute = batch_pipeline.compare_regrade_passes([caption_missing], [two_figures], 2, temp, experiment_items)
        assert counted_dispute["reports"][0]["absolute_delta"] == 2
        assert counted_dispute["review_required"] == 1  # Same rule/requirements but different image count.
        reordered = exp_decision([{**two_figures["deductions"][0], "figures": [
            {**figure, "reason": "同一问题换个说法"} for figure in reversed(two_figures["deductions"][0]["figures"])
        ]}])
        batch_pipeline.validate_scoring_protocol([reordered], experiment_items, 1)
        assert batch_pipeline.compare_regrade_passes([two_figures], [reordered], 2, temp, experiment_items)["review_required"] == 0
        other_image = exp_decision([{**caption_fact, "figures": [
            {**caption_fact["figures"][0], "id": "p.1#image2"},
        ]}])
        batch_pipeline.validate_scoring_protocol([other_image], experiment_items, 1)
        image_dispute = batch_pipeline.compare_regrade_passes([caption_missing], [other_image], 2, temp, experiment_items)
        assert image_dispute["reports"][0]["absolute_delta"] == 0 and image_dispute["review_required"] == 1
        three_images = exp_decision([{**caption_fact, "figures": [
            {**caption_fact["figures"][0], "id": f"p.1#image{number}"} for number in range(1,4)
        ]}])
        five_images = exp_decision([{**caption_fact, "figures": [
            {**caption_fact["figures"][0], "id": f"p.1#image{number}"} for number in range(1,6)
        ]}])
        for value in (three_images, five_images):
            batch_pipeline.validate_scoring_protocol([value], experiment_items, 1)
        assert batch_pipeline.compare_regrade_passes(
            [three_images], [five_images], 2, temp, experiment_items,
        )["review_required"] == 0  # Uncharged later images do not create disagreement.
        first_exp = exp_decision([exp_d(2, "http_answer_issue", ["HTTP-Q9"])])
        second_exp = exp_decision([exp_d(2, "http_answer_issue", ["HTTP-Q10"])])
        for value in (first_exp, second_exp):
            batch_pipeline.validate_scoring_protocol([value], experiment_items, 1)
        same_score_dispute = batch_pipeline.compare_regrade_passes([first_exp], [second_exp], 2, temp, experiment_items)
        assert same_score_dispute["review_required"] == 1
        assert same_score_dispute["reports"][0]["absolute_delta"] == 0
        assert same_score_dispute["reports"][0]["dispute_reasons"] == ["deduction_facts"]
        assert same_score_dispute["reports"][0]["recheck_deductions"][0]["requirements"] == ["HTTP-Q10"]
        reworded = exp_decision([{**first_exp["deductions"][0], "reason": "同一问题的不同表述", "evidence": ["p.2"]}])
        batch_pipeline.validate_scoring_protocol([reworded], experiment_items, 1)
        assert batch_pipeline.compare_regrade_passes([first_exp], [reworded], 2, temp, experiment_items)["review_required"] == 0
        exp_perfect = exp_decision([])
        batch_pipeline.validate_scoring_protocol([exp_perfect], experiment_items, 1)
        assert batch_pipeline.compare_regrade_passes([first_exp], [exp_perfect], 2, temp, experiment_items)["review_required"] == 1
        other_major = decision("student.pdf", [
            deduction(2, "required_question_unanswered", "q8_unanswered", "第8题完全未回答"),
        ])
        batch_pipeline.validate_scoring_protocol([other_major], items, 1)
        audit = batch_pipeline.compare_regrade_passes([major], [other_major], 2, temp)
        assert audit["review_required"] == 0
        assert audit["reports"][0]["changed_deduction_facts"] == ["q7_unanswered", "q8_unanswered"]
        stronger = decision("student.pdf", [
            deduction(2, "required_question_unanswered", "q7_unanswered", "第7题完全未回答"),
            deduction(2, "key_answer_direct_contradiction", "q8_wrong", "第8题与实测数据矛盾"),
        ])
        batch_pipeline.validate_scoring_protocol([stronger], items, 1)
        blocked = batch_pipeline.compare_regrade_passes([perfect], [stronger], 2, temp)
        assert blocked["review_required"] == 1
        assert blocked["reports"][0]["changed_deduction_facts"] == [
            "q7_unanswered", "q8_wrong"
        ]

        review_dir = temp / "marked"
        review_dir.mkdir()
        write_json(review_dir / "student.review.json", {
            "source_file": str(temp / "student.pdf"),
            "warnings": [],
        })
        assert batch_pipeline.mark_regrade_adjudication(review_dir, blocked) == 1
        marked = json.loads((review_dir / "student.review.json").read_text(encoding="utf-8"))
        assert marked["adjudication_status"] == "required"

        completed = materialize(temp, rubric, perfect, "lenient")
        assert completed.returncode == 0, completed.stderr
        review = json.loads(next((temp / "reviews-lenient").glob("*.review.json")).read_text(encoding="utf-8"))
        assert review["total_score"] == 100
        assert all(not item["deductions"] for item in review["items"])

        mismatch = decision("student.pdf", major["deductions"], [20, 30, 15, 20, 15])
        rejected = materialize(temp, rubric, mismatch, "mismatch")
        assert rejected.returncode != 0
        assert "do not equal the program-calculated" in rejected.stderr

        bad_page = decision("student.pdf", [
            deduction(2, "required_question_unanswered", "q7_unanswered", "第7题未回答", ["p.3"]),
        ])
        out_of_range = materialize(temp, rubric, bad_page, "bad-page")
        assert out_of_range.returncode != 0
        assert "outside student.pdf (1-2)" in out_of_range.stderr

        work = temp / "finalize-work"
        result_dir = temp / "result"
        review_dir = result_dir / "reviewjson"
        work.mkdir()
        result_dir.mkdir()
        write_json(result_dir / "批次信息.json", {"submission_preflight": []})
        report = {
            "report_id": "r0001",
            "source_name": "student.pdf",
            "source_file": str(temp / "student.pdf"),
            "source_sha256": "abc123",
            "student_id": "1",
            "student_name": "测试学生",
            "experiment": "实验",
            "page_count": 2,
            "warnings": [],
        }
        manifest = {
            "pipeline_version": "1.9",
            "batch_id": "self-test",
            "result_dir": str(result_dir),
            "review_dir": str(review_dir),
            "inspection_mode": "smart",
            "rubric": rubric,
            "score_increment": 1,
            "reproducibility_check": True,
            "max_regrade_delta": 2,
            "pending_reports": [report],
            "reused_reports": [],
            "blocked_reports": [],
            "chunks": [{
                "chunk": 1,
                "decisions_file": "decisions-001.jsonl",
                "recheck_file": "recheck-001.jsonl",
            }],
        }
        write_json(work / "grading-manifest.json", manifest)
        low = decision("student.pdf", [
            deduction(2, "required_question_unanswered", f"q{number}_unanswered", f"第{number}题完全未回答")
            for number in range(1, 6)
        ])
        lower = decision("student.pdf", [
            deduction(2, "required_question_unanswered", f"q{number}_unanswered", f"第{number}题完全未回答")
            for number in range(1, 7)
        ])
        primary_row = {
            "id": "r0001", "d": low["deductions"], "c": low["overall_comment"],
            "i": low["item_comments"], "e": low["evidence"], "v": low["visual_evidence"],
        }
        recheck_row = {
            "id": "r0001", "d": lower["deductions"], "c": lower["overall_comment"],
            "i": lower["item_comments"], "e": lower["evidence"], "v": lower["visual_evidence"],
        }
        (work / "decisions-001.jsonl").write_text(json.dumps(primary_row, ensure_ascii=False) + "\n")
        (work / "recheck-001.jsonl").write_text(json.dumps(recheck_row, ensure_ascii=False) + "\n")
        finalized = batch_pipeline.finalize_batch(SimpleNamespace(work_dir=str(work), score_offset=0))
        assert finalized["adjudication_required"] == 1
        assert finalized["score_offset"] == 5
        quarantined = json.loads(next(review_dir.glob("*.review.json")).read_text(encoding="utf-8"))
        assert quarantined["adjudication_status"] == "required"
        assert quarantined["total_score"] == 85
        assert quarantined["adjusted_total"] == 90
        approval = subprocess.run([
            sys.executable,
            str(ROOT / "scripts" / "approve_batch.py"),
            "--review-dir", str(review_dir),
            "--dry-run",
        ], capture_output=True, text=True, encoding="utf-8")
        assert approval.returncode != 0
        assert "must be adjudicated" in approval.stderr

        # Exercise the full experiment-specific finalize path, not just the comparison helper.
        exp_work = temp / "experiment-work"
        exp_result = temp / "experiment-result"
        exp_work.mkdir()
        exp_result.mkdir()
        write_json(exp_result / "批次信息.json", {"submission_preflight": []})
        exp_manifest = {
            **manifest, "rubric": experiment_one, "result_dir": str(exp_result),
            "review_dir": str(exp_result / "reviewjson"),
        }
        write_json(exp_work / "grading-manifest.json", exp_manifest)
        for filename, value in (("decisions-001.jsonl", first_exp), ("recheck-001.jsonl", second_exp)):
            row = {"id": "r0001", "d": value["deductions"], "c": value["overall_comment"],
                   "i": value["item_comments"], "e": value["evidence"], "v": value["visual_evidence"]}
            (exp_work / filename).write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        exp_final = batch_pipeline.finalize_batch(SimpleNamespace(work_dir=str(exp_work), score_offset=0))
        assert exp_final["score_offset"] == 0
        assert exp_final["adjudication_required"] == 1
        assert exp_final["distribution_audit"]["unresolved_reports"] == 1
        assert exp_final["distribution_audit"]["raw"]["in_range"] == 1
        assert exp_final["distribution_audit"]["published"]["in_range"] == 1
        exp_review = json.loads(next((exp_result / "reviewjson").glob("*.review.json")).read_text(encoding="utf-8"))
        assert exp_review["items"][1]["deductions"][0]["requirements"] == ["HTTP-Q9"]
        html = (exp_result / "实验报告审核面板.html").read_text(encoding="utf-8")
        assert "renderRegradeDispute" in html
        exp_approval = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "approve_batch.py"),
            "--review-dir", str(exp_result / "reviewjson"), "--dry-run",
        ], capture_output=True, text=True, encoding="utf-8")
        assert exp_approval.returncode != 0
        assert "must be adjudicated" in exp_approval.stderr

    print("score reproducibility tests passed")


if __name__ == "__main__":
    main()
