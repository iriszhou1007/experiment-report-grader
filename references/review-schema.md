# Review artifact schema

The normal grading path should not ask the model to reproduce this full shape. With the default rubric, fill `id`, deduction facts `d`, overall comment `c`, item reasons `i`, evidence `e`, and visual evidence `v`; the program calculates `s`. Every deduction records `item`, `rule`, unique `fact`, `reason`, `certainty`, `evidence`, and `visual_evidence`. Legacy fixed-rule rubrics still accept `r` plus `s`.

Create one `*.review.json` file for each source PDF under `<report-folder>/<batch-id>_批改结果/reviewjson/`. Use UTF-8 and the following shape:

```json
{
  "schema_version": "1.0",
  "source_file": "实验报告样本/example.pdf",
  "source_sha256": "lowercase hex digest",
  "student_id": "optional",
  "student_name": "optional",
  "experiment": "optional",
  "rubric_id": "course rubric name or provisional-default-v6-obvious-defects-only",
  "review_status": "draft",
  "approved_at": null,
  "total_score": 97,
  "max_score": 100,
  "items": [
    {"criterion": "实验报告完整性", "score": 20, "max_score": 20, "comment": "必需栏目完整。", "evidence": ["p.1"], "deductions": []},
    {
      "criterion": "数据记录与计算完整性",
      "score": 27,
      "max_score": 30,
      "comment": "第4题完全未回答，核对报告后确认。",
      "evidence": ["第 4 页", "第 6 页"],
      "visual_evidence": ["第 6 页"],
      "deductions": [
        {"rule": "required_question_unanswered", "fact": "q04_unanswered", "points": 3, "reason": "第4题完全未回答", "certainty": "confirmed", "evidence": ["p.6"]}
      ]
    },
    {"criterion": "结论", "score": 15, "max_score": 15, "comment": "结论有实际依据。", "evidence": ["p.6"], "deductions": []},
    {"criterion": "小结", "score": 20, "max_score": 20, "comment": "小结完整。", "evidence": ["p.6"], "deductions": []},
    {"criterion": "报告格式", "score": 15, "max_score": 15, "comment": "正常可读。", "evidence": ["p.1"], "deductions": []}
  ],
  "overall_comment": "Concise student-facing summary.",
  "warnings": ["Anything requiring instructor judgment."]
}
```

## Validation rules

- `review_status` is `draft` until the user explicitly approves this exact review. The insertion script accepts only `approved` plus its command-line approval flag.
- `adjudication_status: required` means the blind passes exceeded tolerance, or (when `recheck_fact_consistency` is enabled) their deduction facts/affected requirements differ. Approval and PDF write-back require explicit adjudication acknowledgement; batch approval refuses it. `regrade_dispute` retains both ledgers and `dispute_reasons` for the panel's comparison.
- `source_sha256` identifies the immutable original. It must match the current source before the first approval and `<batch-folder>/原始数据/原始备份_<source-name>` on every approval thereafter.
- `items[*].score` must be between zero and its maximum. Item maxima must sum to `max_score`; item scores must sum to `total_score`.
- `scripts/materialize_reviews.py` validates the complete batch before writing reviews: identity, score range/increment, rule/score consistency, duplicate facts, required confirmation, visual evidence, affected `requirements` within printed `requirement_ids`, reasons, comments and page bounds. The default and experiment-1 rubrics do not require minimum deductions; optional custom floors remain supported.
- For a selected rule with `requires_visual_evidence: true`, visual evidence must name at least one valid PDF page and every visually inspected page must also appear in the item's ordinary evidence. OCR text and a `visual_candidate_pages` flag do not satisfy this requirement.
- A rule with `only_if_no_other_deductions: true` is permitted only as the sole deduction in the entire report, across all items. Selecting it with any other deduction is a validation error, regardless of ledger order. It does not mandate a deduction when the visual defect is absent. Experiment-1 v6 uses this for the 3-point no-first-line-indent rule; screenshot numbering/reference/description defects are a separate visually verified rule charging 2 points per distinct image, capped at 6.
- `count_by: "figure"` on a rubric rule makes `points` the per-image rate and `max_occurrences` the charged-image cap (experiment 1: 2 and 3). The single canonical deduction entry must include a nonempty `figures` array of `{id: "p.N#imageK", requirements: [...], reason: "..."}`. N is the original page and K is the page's experiment-screenshot order; it is not the student's possibly missing figure number. IDs must be unique, every figure's page must be in both evidence and visual evidence, and parent `requirements` must equal the union of per-figure requirements. The program charges `points * min(len(figures), max_occurrences)`; splitting the same counted rule across entries is rejected. The blind audit compares per-figure IDs and missing elements, ignoring wording and array order.
- After validation, only the earliest charged figures (by original page and image order) and their requirement union enter the normalized ledger. Extra observations beyond the cap neither increase the score deduction nor create an artificial blind-pass dispute.
- `finalize` compares the primary and blind recheck totals and fact ids. It writes `regrade-audit.json`; reports above the configured delta receive `adjudication_status: required` and remain non-approvable until teacher confirmation.
- Fixed-rule reviews carry `items[*].score_rule` and `score_rule_description` so the instructor can see the exact rule that produced the score. If the instructor changes that score in the panel, the service removes the model rule metadata and records `teacher_override: true` instead.
- Evidence uses one-based PDF page numbers and may include a short locator such as `p. 5, HTTP response screenshot`.
- Keep unsupported or unresolved concerns in `warnings`; do not convert them into deductions without a confirmed rubric basis.
- For batch work, create `<batch-folder>/index.md` with source filename, student name, student ID, draft score, review status, and warnings. The index is for review convenience and is not approval.
- Generate the Excel roster from the same JSON files so PDF feedback and spreadsheet scores cannot diverge. Put every `items[*].score` in a separate rubric column, calculate the total in Excel, and include student name, student ID, maximum score, review status, and source filename.
- Generate the HTML review panel from the same JSON files. In local-server mode, saving updates JSON, Excel, and HTML directly; approval additionally backs up the original once and writes the verified feedback-appended PDF back to the source path.
