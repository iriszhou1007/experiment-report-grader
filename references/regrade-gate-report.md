# v1.5 blind regrade gate: findings report

Status: **historical v1.5 measurement; policy decision superseded by pipeline 1.6**. The raw gate
statistics remain useful, but the pre/post runs used fresh automated judge samples and different
chunking. They show that disagreement increased in the later run; they do not prove that supplying
more evidence caused the increase. Pipeline 1.6 replaces holistic score bands with evidence
deduction facts, program-calculated scores, and approval quarantine for unresolved disputes.

- Cohort: 62 real student reports, 2025 秋 计算机网络课程设计 实验4 (ICMP/Ping/Traceroute).
- Pipeline: `pipeline_version 1.5`, rubric `provisional-default-v3-fixed-rules`,
  `text_only` + OCR (`auto` → rapidocr shim), `--max-report-chars 40000`,
  `score_increment 1.0`, `reproducibility_check true`, `--max-regrade-delta 2`.
- Grading: two independent blind passes per run, each graded from `grading-input-*.md` alone,
  under byte-identical instruction text (`/tmp/erg-v15fix/INSTRUCTIONS-pass1.md` and
  `INSTRUCTIONS-pass2.md` differ only in the pass name and which sibling file is off-limits).
- Interpreter: `/opt/anaconda3/envs/prism/bin/python3` (Python 3.11.8). OCR cache reused across runs.

---

## 1. Summary

1. The gate blocks a large fraction of any real cohort — 24/62 to 39/62 depending on packet quality.
2. A genuine packager defect was found, reproduced and fixed: conclusion/summary pages were
   truncated below their own length, so both passes had to *guess* whether a column was blank.
3. **Fixing that defect made the gate fail more, not less** (24/62 → 39/62), because the gate
   measures *disagreement*, not accuracy. Withholding evidence made both passes agree on the same
   wrong call; supplying it exposed that they count answer errors differently.
4. The dominant residual disagreement is item 2 (数据记录与计算), which changed between passes in
   66% of reports. 14 of the 39 blocks are caused by a disagreement on a *single* item.
5. The rubric's step sizes (2–5 points) are finer than measured inter-grader error-counting
   reliability (±1 error), so a hard ±2 total stop is not achievable by construction.
6. Two further defects are documented but not fixed: a line filter that deletes answer prose from
   data pages, and an instruction ambiguity about when a visual check is mandatory.

---

## 2. Run inventory

| run | work dir | packets | pass 1 | pass 2 | notes |
|---|---|---|---|---|---|
| A — as-run | `/tmp/erg-v15run/work` | pre-fix (19 chunks) | weaker prompt | stronger prompt | first attempt; prompt asymmetry |
| B — pre-fix, controlled | `/tmp/erg-v15run/work` | pre-fix (19 chunks) | re-run, same prompt as B2 | ditto | `attempt1/` holds the A pass 1 |
| C — post-fix | `/tmp/erg-v15fix/work` | fixed (22 chunks) | same prompt | same prompt | the corrected packets |

Report ids are assigned sequentially over the sorted PDF list, so `r0001…r0062` map to the same
students in every run. All cross-run comparisons below join on **student name**.

---

## 3. Gate results

| run | blocked | within | mean \|Δ\| | median | max \|Δ\| | item agreement |
|---|---|---|---|---|---|
| A — as-run (prompt asymmetry) | 30/62 | 32 | 3.08 | — | 16 | 199/310 (64%) |
| B — pre-fix, identical instructions | 24/62 | 38 | 2.31 | — | 11 | 211/310 (68%) |
| C — post-fix, identical instructions | **39/62** | 23 | **3.42** | 3 | 11 | 201/310 (65%) |

Run C \|Δ\| distribution: `{0:11, 1:4, 2:8, 3:15, 4:3, 5:10, 6:3, 7:4, 8:1, 10:2, 11:1}`.

Joining B and C per student: **18 reports disagreed less, 30 disagreed more, 14 unchanged**.
16 reports are blocked in both runs, 23 only after the fix, 8 only before it.

So the prompt asymmetry explains part of run A (30 → 24), and the packet fix moved the number the
other way by a larger margin (24 → 39).

---

## 4. Where the disagreement lives (run C, 62 reports)

Per-item disagreement — the two passes selected a different rule or score:

| item | reports changed | share | top rule pairs (P1 vs P2) |
|---|---|---|---|
| 2 数据记录与计算 | **41** | **66%** | `all_correct`30 vs `minor_details`27 (×14); `two_or_three_missing`22 vs 27 (×6); 27 vs 30 (×4); 22 vs `one_missing_or_major_error`25 (×3) |
| 4 小结质量 | 20 | 32% | `brief_personal_summary`15 vs `process_and_gain`17 (×5); 17 vs `process_gain_reflection`20 (×4); 17 vs 15 (×4) |
| 1 实验报告完整性 | 18 | 29% | `complete`20 vs `one_thin_section`18 (×8); 18 vs 20 (×6) |
| 3 结论合理性 | 15 | 24% | `correct_but_brief`13 vs `blank_or_placeholder`10 (×3) |
| 5 格式规范性与语言表达 | 15 | 24% | `minor_issues`13 vs `clear_and_standard`15 (×13) |

109 of 310 item rules changed (35%). 14 of the 39 blocks come from a disagreement on a **single**
item — 12 of them item 2, 2 of them item 4.

Two structural readings of this table:

- **Item 2 is the blocker.** Its tiers are separated by 2–3 points per detected error
  (30/27/25/22/18) while the task is "count how many of 16 questions were answered wrongly".
  Two graders disagreeing by one error is normal; that disagreement alone is a 2–3 point swing
  against a 2-point total budget.
- **Item 5 is a systematic 2-point offset, not noise.** 13 of its 15 changes are exactly
  13 vs 15. It correlates with whether the pass rendered a source page: in run C, pass 1 filled a
  non-empty `v` in 53/62 rows and pass 2 in 62/62. A grader that cannot see the page deducts.

---

## 5. The packager defect that was found and fixed

### 5.1 Mechanism (reproduced, not inferred)

`compact_report()` classifies each page into scoring sections (`priorities`) and gives a
conclusion/summary page a 2200-character ceiling through `page_ceiling()`. The over-budget branch
then multiplied **every** page's ceiling by the same `scale` and clamped to `MIN_PAGE_CHARS` (200),
ignoring `priorities`.

For `2023141460040` (陈燚):

- page 12 carries both 结论 and 小结 and is 394 characters long;
- the packer gave it a **230-character** limit (`scale ≈ 0.105` applied to the 2200 focus ceiling);
- the resulting excerpt ended at the `小 结` label with the student's paragraph missing, and the
  page was reported as `excerpt truncated`.

Both blind passes then guessed. Pass 1 chose `blank_but_work_complete` (12); pass 2 chose
`process_and_gain` (17). Rendering the real page 12 settles it — the paragraph is present:
"通过本次 Ping 与 Traceroute 的数据包捕获分析实验，我真切感受到网络协议'隐藏在背后的逻辑美感'…".

The error ran in **both directions**: `2023141460317` (宋韦瑾) and `2023141460190` (李滨瀚) have
complete 结论/小结 text on pages the *other* pass deducted as blank. In the first controlled run,
26 of the disputed items were of this "blank vs present" kind, and 25 of 30 blocks were affected.

The same excerpts spent budget on **blank report-template pages**: 41 of 62 packets included
instruction boilerplate, and for `2023141460040` pages 13–14 (empty template text) were kept while
page 12's summary was cut.

### 5.2 Fix

Terminal pages are now protected from budget scaling: a page whose `focus` includes `conclusion` or
`summary` keeps at least `TERMINAL_PAGE_LIMIT` (1200) characters, or its full length when shorter
(`scripts/prepare_grading_packet.py`, commit `061df66`).

### 5.3 Verification

Re-running `prepare` on the same cohort:

| observation | result |
|---|---|
| packets changed | 55/62 |
| reports that gained text | 55 — +33,812 chars total (mean +545) |
| reports that lost text | **0** |
| pages marked `excerpt truncated` | 622 → 535 |
| reports whose conclusion/summary page was cut before and complete after | **47** |
| reports still having a truncated conclusion/summary page | **0** |
| worst-case packet growth | +5,627 chars (`r0039`); largest packet 31,982 |
| largest terminal-page excerpt | 1,149 chars (the cap holds) |

The additive invariant ("no report ever receives less evidence") holds. `doctor`,
`doctor --self-test` (24 checks) and `test_score_reproducibility.py` pass; `package_skill.py
--verify` is clean.

---

## 6. The core finding: the gate measures disagreement, not accuracy

Fixing the packet should have reduced the blocked count. It nearly doubled it. Three traced
students show why:

| student | run B (pre-fix) P1 / P2 | run C (post-fix) P1 / P2 | items that moved |
|---|---|---|---|
| 刘卓尧 | 84 / 84 — agreed | 87 / 76 — Δ11 | items 1 and 2 |
| 曾泓瑞 | 88 / 88 — agreed | 81 / 91 — Δ10 | items 2, 3, 4 |
| 侯睿 | 87 / 88 — Δ1 | 82 / 92 — Δ10 | items 2 and 4 |

In each case the *pre-fix agreement was a shared guess about text neither pass could read*. 刘卓尧's
item 2 was `two_or_three_missing` (22) in both passes only because neither pass could see the
answers. Once the answers are visible, one pass counts `minor_details` (27) and the other counts
`many_missing` (18).

Consequences:

1. **A lossy packet can pass the gate by keeping both passes uniformly under-informed.** The gate
   rewards missing evidence with agreement, which is a perverse incentive for a tool whose purpose
   is to make grading more faithful.
2. **The blocked count is not a quality score.** It is a measure of residual inter-grader variance
   given the evidence supplied.
3. The earlier legacy measurement points the same way: on 54 byte-identical packets, scores still
   moved two-way (30 up, 13 down), and no report ever received less evidence. The variance lives in
   the judging step.

---

## 7. Other defects found

1. **Line filter deletes answer prose on data pages (not fixed).** `focused_excerpt()` reduces a page
   to lines carrying a section heading, a number, or a `SECONDARY_TERMS` match whenever the page
   exceeds its limit; ordinary answer sentences match none of those and are replaced with
   `[non-scoring text omitted by script]`. In `2023141460325` (孔子涵) the packet dropped question 2's
   answer on page 4 — exactly the line needed to score item 2 — and the grader recovered it only by
   rendering the page for the format item. Terminal pages are protected now; data pages are not.
2. **Visual-check obligation is ambiguous (not fixed).** The packet says a visually gated rule may be
   selected only after opening the page, and `v` is mandatory only for that rule. An agent that does
   not select `hard_to_read` therefore grades format blind and deducts; 13 item-5 pairs differ by
   exactly 13 vs 15. The obligation should apply to the item, not to the rule.
3. **Blank-column double counting (rubric ambiguity).** Some passes treated a blank 结论/数据记录
   column as a missing 栏目 for item 1 (`multiple_missing_sections`, 16) in addition to the item-3 /
   item-4 blank penalty; others did not. This is part of the 29% item-1 disagreement.
4. **Rule/score mismatch is possible in agent output.** Across 124 graded rows in run C the
   validator rejected exactly one: `recheck-011.jsonl` paired `generic_only` with score 10 (its
   printed score is 11). The agent's own reason said "占位文字", so it was repaired to
   `blank_or_placeholder` (10); the original is at `/tmp/erg-v15fix/recheck-011.jsonl.before`.
   Validation before `finalize` is mandatory, never optional.
5. **Blind passes must overwrite a file they may not read.** `prepare` writes stubs into both
   decision files. Hosts that enforce read-before-write need shell redirection; two agents stalled
   on this until clarified.
6. **Data anomaly.** `202314146190-李滨瀚-实验04.pdf` carries a 12-digit student id where the other
   61 files have 13. Unrelated to grading, but it will break id-based joins.

---

## 8. Score impact (run C pass 1 vs the original legacy run)

The gate blocked finalization, so no review artefacts (HTML/Excel/PDF write-back) were produced.
The graded totals themselves are available and are what would be published if the gate were
advisory:

| measurement | value |
|---|---|
| legacy baseline mean | 86.05 |
| pre-fix v1.5 mean (run B) | 88.81 |
| post-fix v1.5 mean (run C) | **90.23** |
| run C vs baseline | +4.18 mean; **48 up, 1 unchanged, 13 down**; range 0…13 |
| run C vs run B | +1.42 mean; 35 up, 10 unchanged, 17 down |
| item-level agreement vs legacy bands | 58/310 (19%) — legacy bands allowed any integer |

The movement is two-way, not inflation: 13 reports lost points against the legacy run and 17 lost
points against the immediately preceding v1.5 run.

---

## 9. Options (open decision)

| option | effect | trade-off |
|---|---|---|
| **A. Make the gate a review flag** (recommended) | `finalize` still runs both passes and writes `regrade-audit.json`, but a failed gate produces review artefacts marked as needing adjudication instead of blocking | Uses the teacher's approval step as the authority it already is; stops penalising complete evidence |
| B. Widen `--max-regrade-delta` to ≈5 | Keeps a hard stop | Arbitrary number; 4 reports still differed by 8–11 |
| C. Coarsen the rubric tiers | One borderline judgement can no longer swing 3–5 points | Requires re-authoring the rubric and re-grading; loses some discrimination |
| D. Mark absence-based rules `requires_visual_evidence` | Any blank/missing deduction must be backed by a rendered page | Adds page renders for the common blank-column case; independent of A–C |

The recommendation is **A + D**. A fixes the incentive problem and matches how the tool is actually
used (the panel already adjudicates report by report); D removes the remaining class of
unsupported deductions without touching the machine-readable score contract.

---

## 10. Artefacts

| path | contents |
|---|---|
| `references/grading-workflow.md` | the measurement, written into the shipped documentation |
| `scripts/prepare_grading_packet.py` | the terminal-page fix (commit `061df66`) |
| `/tmp/erg-v15fix/regrade-audit.json` | gate audit, fixed packets (39 blocked) |
| `/tmp/erg-v15run/attempt1-regrade-audit.json` | gate audit, original packets (30 blocked) |
| `/tmp/erg-v15run/attempt1/` | pass-1 attempt 1, preserved for the prompt-asymmetry comparison |
| `/tmp/erg-full62/scorecompare.py` | compare a run's totals against the baseline |
| `/tmp/erg-full62/classify.py` | split disagreements into blank-vs-present and content-vs-content |
| `/tmp/erg-v15fix/INSTRUCTIONS-pass1.md`, `INSTRUCTIONS-pass2.md` | the identical instruction pair |
| `/tmp/erg-v15fix/recheck-011.jsonl.before` | pre-repair row with the rule/score mismatch |

Reproduction: `/opt/anaconda3/envs/prism/bin/python3 scripts/batch_pipeline.py prepare --report-dir
<dir> --work-dir <work> --rubric assets/default-rubric.json`, fill both passes, then
`finalize --work-dir <work> --score-offset 5`.

---

## 11. Limitations

- One course, one 62-report cohort, one experiment. Treat magnitudes as indicative; the direction is
  the durable part.
- Graders were automated multi-agent passes, not human teachers. Human agreement is likely higher on
  item 2 but the tier spacing problem would remain.
- Run A's pass 1 used a weaker prompt; only runs B and C are controlled comparisons.
- The recommended policy change is argued from a single cohort and should be re-measured on the next
  batch before being treated as settled.
