# Batch grading workflow

`scripts/batch_pipeline.py prepare` hashes every PDF, reuses compatible unchanged reviews, extracts only pending reports, fingerprints repeated course-template instructions, assigns a dynamic text budget, detects scoring sections and image pages, optionally runs local OCR, extracts useful embedded images, routes reports by review depth, and creates automatically sized grading chunks.

The template profile is stored in the temporary work directory, keyed to a digest of the cohort's source hashes, and rebuilt only when that cohort changes. It is never derived from the pending subset alone: doing so made the stripped-boilerplate set drift as grading progressed, so the same report received different evidence in different runs. It removes only repeated headings and instruction-like lines; warnings or missing headings never count as proof that student work is absent.

## Grading routes

- `fast`: all core scoring sections are detected and the available text is sufficient. The normal input budget is capped at 2,800 characters.
- `standard`: some evidence needs ordinary judgment but does not justify reading the complete report. The normal cap is 3,400 characters.
- `detailed`: many pages contain little extractable text, scoring sections are not detected, or a visual-heavy report needs closer review. It may use the configured per-report ceiling.

Routes control how much evidence is prepared; they do not prescribe a score. The model still applies the confirmed rubric.

The budget governs how long each page's excerpt may be, never whether the page appears. Every page of every report is included: scoring sections, image flags and edge pages only raise a page's ceiling (2,200 for scoring-section pages, 1,600 for OCR text, 650 for the first and last page, 320 otherwise). When the ideal total exceeds the budget, limits scale toward it but never below a 200-character presence floor, so a page can be short but cannot vanish. A packet that overruns its budget to honour that floor says so in its `coverage:` line and carries a `packet_over_budget` warning.

## Which warnings the instructor actually sees per report

`prepare` emits warning keys per report; `materialize_reviews.py` turns them into the panel's "需要教师复核" list. Some of those keys describe the *batch's settings* rather than that report's own risk, and they fire on every report in the batch — in the first measured cohort `full_evidence_packet` fired on 62/62 and `visual_content_detected` on every report whose pages contained any screenshot. Repeating them per report made all 62 reports show an identical 3–4 item review box, which is the same as showing none: the two hints that genuinely differ between reports were buried.

Those keys are listed in `BATCH_LEVEL_WARNINGS` and are pulled out of each report's list. `finalize` collects them once into `batch_notices` (in `批次信息.json`, and embedded in the panel as `__BATCH_NOTICES_JSON__`), where the panel shows them once above the report list, labelled as applying to the whole batch.

- Batch-level: `full_evidence_packet`, `packet_over_budget`, `visual_content_detected`.
- Per-report: `visual_review_not_requested`, `visual_review_sampled` (only fire when the scan actually found candidate image pages), `low_text_pages`, `priority_headings_not_detected`, `compact_text_omitted`.

Keep the two groups separate when adding a warning: a key belongs at batch level only if it would read identically on every report. On the measured cohort this took the per-report list from 3–4 items on all 62 reports to 1 item on 58 and 0 on 4.

## Inspection modes

- `text_only`: use extracted and OCR text. Record image candidates but do not inspect images.
- `smart`: inspect only prioritized unresolved pages in `visual-review-plan.json`. The script keeps at most eight pages and eight extracted images per report, preferring images inside scoring sections and low-text pages.
- `full_visual`: inspect every page where layout, images, tables, plots, screenshots, calculations, or legibility affect scoring.
- `--auto-upgrade-visual` permits text_only to inspect unresolved candidate pages. Do not upgrade without this stored user choice.

OCR is text evidence, not visual evidence. It does not preserve reliable row/column geometry, alignment, font size, image placement, or rendered legibility. A rubric rule with `requires_visual_evidence: true` may be selected in any inspection mode only after the grader opens the smallest relevant set of source PDF pages and records those pages in decision key `v`. This gate is deduction-triggered: ordinary `text_only` reports do not become visual reviews merely because they contain screenshots.

OCR mode defaults to `auto`. If an OCR helper is available, the script OCRs candidate pages and removes pages with sufficient non-chart text from the visual queue. On macOS and Linux, missing OCR support is a normal fallback: keep those pages in the visual plan. On Windows it is not — there is no shim generator there (the helper script is bash), so `prepare` stops and asks for Tesseract instead of grading a batch without OCR. Use `--ocr-mode off` to disable OCR explicitly. `auto` uses the rapidocr-backed shim; `--ocr-engine vision` selects the macOS on-device recognizer instead. See [local-ocr-setup.md](local-ocr-setup.md) for how the helpers are located on each platform, how to see which engine a given machine will use, and how to generate the shims.

The sufficiency test measures only length, line count and character density, so it cannot tell good recognition from bad. OCR text is therefore merged in behind an explicit marker telling the model not to deduct for anything that only looks wrong there, and it keeps a larger share of its page than ordinary low-priority text. Treat a merged OCR page as a hint rather than ground truth: if a score turns on what that page says, open the source page.

Because OCR renders and reads the whole page, it re-reads text the PDF already exposed. Before merging, lines that merely repeat the page's own text layer and lines that carry no signal (hex-viewer grids, memory-dump columns) are dropped, and the report's `coverage:` line states how many. The recognition cache is keyed by page hash, candidate page set and engine, so changing `--ocr-engine` or `--ocr-executable` re-runs recognition instead of silently reusing the previous engine's text. OCR costs roughly 2.5–3s per candidate page on CPU, which on a large first batch means tens of minutes; `prepare` prints `[ocr] …` progress lines while it works.

## Model input and decision format

Grade from `grading-input-*.md` alone. Its header carries the confirmed rubric — dimension names, maxima and the required score order — so the positional score array is never ambiguous, and it asks for evidence page numbers. Each report then carries a `coverage:` line naming the pages that were included, omitted, or truncated, followed by every page of that report.

`prepare --anchors <file>` renders teacher-confirmed reference scores into that same header, and records them in the run summary. Prefer item-level anchors: total-only anchors do not tell the grader which dimension caused the total.

```json
[{"student_name":"朱明月","score":95,"item_scores":[20,27,15,18,15],"deductions":[{"item":2,"rule":"required_question_unanswered","fact":"q07_unanswered","reason":"第7题完全未回答","certainty":"confirmed","evidence":["p.6"],"visual_evidence":[]},{"item":4,"rule":"minor_summary_issue","fact":"summary_no_reflection","reason":"小结说明了收获，但没有记录实验困难或改进反思","certainty":"confirmed","evidence":["p.14"],"visual_evidence":[]}],"item_reasons":["栏目齐全","第7题未回答","结论成立","小结缺少反思","页面清楚"],"evidence":[["p.1"],["p.6"],["p.13"],["p.14"],["p.2"]]}]
```

Anchors pin one standard across chunks. They belong in a file rather than in whichever prompt happens to carry them: a batch graded with anchors that exist only in a conversation cannot be reproduced, and the anchors are the control variable when comparing two runs.

```text
rubric (5 items, total 100.0): 1.实验报告完整性/20.0 | 2.数据记录与计算完整性/30.0 | ...
coverage: 15/15 pages included; omitted pages: none; excerpt truncated on: 12,13; 
```

Missing headings, low text, omitted excerpts and image candidates are flags rather than proof that student work is missing. A page named as omitted or truncated is not a blank page.

## The measurement unit is the lab requirement, not the capture value

Score by looking for each lab requirement's answer, and give credit as soon as it is found. A Wireshark screenshot carries hundreds of values; no student is expected to transcribe them, and **redundant capture data is not a defect**. The current deduction rules count an answer wherever it is legible in the report rather than only in its template column; only a confirmed unanswered requirement supports the corresponding major deduction.

This matters because it is easy to build a metric that violates it. Counting "values visible in screenshots but absent from the prose" looks like evidence about review cost, and on the first cohort it produced 1,192 such values across 59 of 62 reports. Every one of them was irrelevant: they were extra frame rows, extra field readouts, the other 4 replies of a 5-packet sequence. A large number there says nothing about whether any requirement went unanswered.

The defensible unit is the requirement. On that same cohort, matched as "is there evidence of this answer anywhere in the report — prose or screenshot":

| result | reports |
|---|---:|
| all 16 requirements evidenced | 40 |
| 15 of 16 | 13 |
| 14 of 16 | 9 |
| fewer than 14 | 0 |

Two measurement traps to avoid when checking this kind of batch:

- **Do not require the question text to be located first.** Many reports answer questions whose text sits inside a screenshot, so the extracted layer begins mid-sentence (`果是请求报文则为 Echo (ping) request。`). A "find the question, then look for its answer" pass finds nothing at all on those reports and reports them as completely unanswered. Match the answer directly.
- **Accept every valid form of an answer.** "第一个 Ping 包返回的准确时间" is answered by a round-trip time (`2.222ms`) as legitimately as by a wall-clock stamp (`17:00:22.024262000`). A pattern demanding `HH:MM:SS.mmm` scored 30 of 62 reports as missing this one, and all 30 had answered it.

Both traps make a report look emptier than it is, which inflates both the apparent need for visual review and the apparent deductions.

Fill one JSON object per line in `decisions-*.jsonl`. The default rubric records evidence-backed deductions in `d`; the program, not the grader, calculates every item score:

```json
{"id":"r0001","d":[{"item":2,"rule":"required_question_unanswered","fact":"q07_unanswered","reason":"第7题完全未回答","certainty":"confirmed","evidence":["p.6"],"visual_evidence":[]},{"item":4,"rule":"minor_summary_issue","fact":"summary_no_reflection","reason":"小结说明了收获，但没有记录实验困难或改进反思","certainty":"confirmed","evidence":["p.14"],"visual_evidence":[]}],"c":"综合评语","i":["栏目齐全","第7题未回答","结论成立","小结缺少反思","页面清楚"],"e":[["p.1"],["p.6"],["p.13"],["p.14"],["p.2"]],"v":[[],[],[],[],[]]}
```

Required keys are `id`, deductions `d`, overall comment `c`, evidence lists `e`, and item reasons `i`. Every deduction fact must be unique across the report. Under the default rubric, apply only a printed rule and use an empty `d` list when no listed defect is clearly confirmed; there is no mandatory minimum deduction. Deductions above two points require `certainty=confirmed` plus explicit page evidence. Visual rules additionally require `visual_evidence`, and rules linked by `exclusive_with` cannot be combined. Supplying `s` is optional and only acts as a checksum: the pipeline rejects it unless it equals the program-calculated result. Legacy fixed-rule rubrics continue to use `r` and `s`.

By default `prepare` also creates `recheck-*.jsonl`. Complete it as a blind second scoring pass from `grading-input-*.md`, without consulting the first decisions. `finalize` validates both ledgers, writes `regrade-audit.json`, and materializes disagreements as `adjudication_status: required`. These drafts are visible for efficient review, but the panel and batch approval refuse write-back until the teacher explicitly confirms the disputed report's current score. Averaging is forbidden because it creates a number with no matching facts.

Run `finalize` with the work directory printed by `prepare`. The default rubric declares `minimum_published_average: 90`; if necessary, finalization computes the smallest uniform increment-aligned offset for which `average(min(100, raw + offset)) >= 90`. The raw evidence score stays untouched and appears beside the adjusted score. An explicit larger course adjustment may still be supplied with `--score-offset`.

Once an offset is in play the graded mean and the reported mean are different numbers, so every artifact names which one it shows instead of printing a bare 「平均分」: the workbook labels them 「原始平均分（批改得分）」 and 「调整后平均分（上报成绩）」, `index.md` states both in its header, and the write-back feedback sheet prints 「批改得分」 beside 「最终成绩」. The adjusted figure is never stored as a frozen copy — the workbook computes it with `=MIN(<满分>,<总分>+<偏移>)` and the review page recomputes `adjusted_total` on every save, so editing a score in the panel moves both figures together. Before writing any draft it checks identity, score count/range/increment, optional declared total, comment length, evidence page bounds, duplicates, and approved-file conflicts. It then creates review JSON and rebuilds all review artifacts from that single source of truth.

Use `status` after interruption. Existing review JSON plus source hash, rubric, mode, OCR setting, budget setting, score increment, and pipeline version form the resume record. Approved conflicts stop the batch rather than overwriting work.

## Score reproducibility: fact ledgers, deterministic scoring and adjudication quarantine

The grader still judges evidence, but the current default rubric no longer asks it to choose a broad holistic tier. Each pass records atomic deduction facts; the program validates uniqueness, evidence, visual obligations and caps, then calculates the score. A mandatory blind second pass compares both totals and fact ids. A total delta above 2 creates a quarantined draft: it may be inspected and edited, but cannot be approved or written back until the teacher explicitly acknowledges the adjudication.

The [experiment-1 v6 checklist](experiment-1-web-http.md) additionally enables `require_confirmed_deductions`, `require_rule_fact_ids` and `recheck_fact_consistency`: even 2-point deductions need explicit confirmation, `fact` equals its rule id, and scoped rules require a nonempty `requirements` list drawn from `requirement_ids`. These settings travel in the normalized rubric and its signature. Blind comparison checks categories, affected requirements and the identities/missing elements of charged screenshots; ordinary evidence-page wording changes alone do not create a dispute. Screenshot observations beyond the 6-point cap are not charged facts. Any changed deduction fact is quarantined even when totals agree. Audit JSON and the panel retain both ledgers so the teacher can resolve the basis, not just the number. This is stricter protection, not evidence that model disagreement has disappeared.

A rule may declare `only_if_no_other_deductions: true`. Normalization preserves this boolean in the rubric signature and the packet prints its report-wide condition. Both batch and materialization scoring reject that rule when any other deduction is selected, including one in another item, in either ledger order. It does not impose a minimum deduction. Experiment-1 v6 uses it for a visually confirmed 3-point unindented prose paragraph, checked only after all other rules; figure numbering/body reference/description defects instead cost 2 points per distinct screenshot, capped at 6 and disable the indentation fallback. These are explicit teacher supplements, not assumed universal report requirements.

An optional root `distribution_target` records an inclusive interval with a strict `> minimum_fraction` target. Finalization writes raw/published counts to `批次信息.json` and its summary; notices label them as a pre-teacher-edit snapshot, with unresolved report counts. This never rescales grades or blocks a report for being outside the interval. For experiment 1 the interval is 92–98 and the target is >80%; small synthetic cases verify counting only, not an actual classroom distribution.

The v1.5 measurements below are retained as historical evidence for why this design changed. They demonstrate high judging variance, but the pre/post packet runs used fresh automated judge samples and therefore do **not** establish that the packet fix caused the higher blocked count. Treat causal language in the archived [regrade-gate report](regrade-gate-report.md) as superseded by this qualification.

The following measurement describes the legacy broad-band workflow and explains why fact-ledger scoring and regrade controls were added:

Measured on one real cohort of 62 reports (ICMP 实验4, rubric `provisional-default-v2`, `text_only` with OCR on), comparing an older build of this skill against the current one:

| observation | result |
|---|---|
| packets byte-identical between the two builds | 54/62 |
| of those 54 — **code provably changed nothing** — score movement | 30 up, 13 down, 11 unchanged; mean +1.1; range −5 … +7 |
| whole cohort | 35 up, 15 down, 12 unchanged; mean +1.2; largest move +9 |
| item scores identical | 131/310 (42%) |
| totals identical | 12/62 |
| same packet, same rubric, two graders, 5 reports | Δ −7 … +5, sd 4.1 |

Three things follow from that table.

1. **The variance is two-way, not inflation.** 15 of 62 reports lost points in the second run. A build that simply "got more generous" would not deduct from a quarter of the cohort.
2. **None of it is attributable to the pipeline.** The 13 deductions on byte-identical packets cannot come from code that produced identical bytes. Of the 8 packets that did differ, 7 differ because one build capped the text budget below the caller's `--max-report-chars` (a tier ceiling overriding the caller's ceiling) and all 7 gained text; the eighth had the same budget and differs by a single character. **No report ever received less evidence**, so no deduction came from extraction or OCR.
3. **The drift tracks the grader.** On 5 reports, two graders working from the same packet, same rubric, same bands landed between −7 and +5 apart (sd 4.1). One of them reproduced the original run's mean exactly (+0.00); the other was +0.20 on those same 5 — the noise is per-report, not a stable bias either way.

### Comparing two runs without fooling yourself

1. **Diff the packets first.** Extract each report's section from `grading-input-*.md` in both runs and compare bytes. If they are identical, every score difference is judging variance, full stop — stop looking for a code cause.
2. **Hold every setting constant**: the rubric *file* (not just its id), `--inspection-mode`, `--ocr-mode` and engine, `--max-report-chars`, and `--score-offset`. Two rubrics can share an id and differ in band wording; see the anchors note above.
3. **Hold the grading protocol constant**: use fixed-rule rubrics, complete both passes, and prefer item-level `--anchors` so the standard travels with the run instead of living in whichever conversation happened to carry it. Total-only anchors are weaker because different item combinations can produce the same total.

### Where the drift comes from, and what to do

Qualitative descriptions still require judgment, and graders can draw a boundary in different places. Fixed deduction values remove free score choice after a fact is selected; the blind pass and 2-point gate expose cross-fact drift. Grading by requirement rather than by capture value (above) removes the largest evidence-counting error, but it does not make a boundary call deterministic.

- **The teacher's approval gate remains the authority.** Use `regrade-audit.json` to find changed rules, then use `e` (evidence pages) so adjudication takes seconds rather than a full re-read. A teacher edit is an explicit override; it need not impersonate a model rule.
- **Do not chase a target mean by suppressing deductions.** A course-wide adjustment belongs in the rubric's `minimum_published_average` or an explicit `--score-offset`, where the graded score stays traceable.
- **When auditing a version change**, test the evidence, not the scores: identical packets mean the build changed nothing that matters to grading. Scores will still move a little, and that is the judging step, not the release.

### The gate measures disagreement, not accuracy

A full v1.5 run on the same 62-report cohort (fixed rules, blind regrade, `text_only` + OCR, `--max-regrade-delta 2`) failed the gate on **30 of 62** reports, with deltas up to 16. Two blind passes were then compared under **byte-identical instructions** to separate grader noise from packet defects:

| measurement | blocked | mean \|Δ\| | item agreement |
|---|---|---|---|
| as-run (pass 1 with a weaker prompt) | 30/62 | 3.08 | 199/310 (64%) |
| controlled (both passes, identical instructions) | 24/62 | 2.31 | 211/310 (68%) |

Equalizing the instructions helped, so part of the first number was prompt asymmetry — a measurement artefact, not grader volatility. A genuine packet defect was also found and reproduced exactly:

- The packager classifies each page into scoring sections (`priorities`) and gives a conclusion/summary page a 2200-character ceiling through `page_ceiling()`. The budget-scaling branch then multiplies **every** page's ceiling by the same `scale` and clamps to `MIN_PAGE_CHARS` (200) — ignoring `priorities`.
- For report `2023141460040` (陈燚) that left page 12 — which carries both 结论 and 小结 — with a **230-character** limit while the page holds **394**. The packet therefore ended at the `小 结` label with the student's paragraph missing, and marked the page `excerpt truncated`.
- Both blind passes then had to guess whether the missing column was blank. Pass 1 answered "blank" (`blank_but_work_complete`, 12); pass 2 answered "present" (`process_and_gain`, 17). Rendering the real page 12 settles it: the paragraph is there. The same defect ran in **both directions** — `2023141460317` (宋韦瑾) and `2023141460190` (李滨瀚) have full 结论/小结 text on pages the other pass deducted blank.
- The excerpt was simultaneously spending budget on **blank report-template pages**: 41 of 62 packets included instruction boilerplate, and for `2023141460040` pages 13–14 (empty template text) were kept while page 12's summary was cut.

The fix protects terminal pages from budget scaling: a page whose `focus` includes `conclusion` or `summary` keeps at least `TERMINAL_PAGE_LIMIT` (1200) characters, or its full length when shorter. Re-running `prepare` on the same cohort:

| observation | result |
|---|---|
| packets changed | 55/62 |
| reports that **gained** text | 55 — total +33,812 chars (mean +545/report) |
| reports that **lost** text | **0** |
| pages marked `excerpt truncated` | 622 → 535 |
| reports whose conclusion/summary page was cut before and complete after | **47** |
| reports still having a truncated conclusion/summary page | **0** |

The additive invariant ("no report ever receives less evidence") still holds, so this is safe to apply to an existing batch; it changes the packets, so re-grade rather than reuse old decisions.

The later post-fix run measured more disagreement:

| run | blocked | mean \|Δ\| | item agreement |
|---|---|---|---|
| pre-fix packets, identical instructions | 24/62 | 2.31 | 211/310 (68%) |
| post-fix packets, identical instructions | **39/62** | **3.42** | 201/310 (65%) |

Joining the two runs per student: 18 reports disagreed less, **30 disagreed more**, 14 were unchanged. Post-fix, item 2 (数据记录与计算) changed between the passes in **41 of 62 reports (66%)**, and 12 of the 14 blocks caused by a single item were item 2 — pairs like `all_correct`=30 vs `minor_details`=27 (14 times) and `two_or_three_missing`=22 vs `minor_details`=27 (6 times).

This is a useful operational measurement, not a controlled causal estimate. Seven per-report packet sections were byte-identical across the two runs, yet their blocked count changed from 1/7 to 4/7 and mean |Δ| from 1.29 to 3.00. The three highlighted students were also fresh judge samples; for 刘卓尧 and 曾泓瑞, the item-2 answer evidence cited by the graders did not change. The data therefore cannot assign the higher disagreement to the packet fix.

The defensible conclusion is narrower: holistic tier selection remained too noisy for a ±2 batch-wide hard stop. Pipeline 1.6 addresses that engineering problem without claiming the packet caused the noise: it records atomic deduction facts, calculates scores deterministically, and quarantines unresolved reports at approval rather than suppressing review artifacts for the whole batch.

Two v1.5 findings directly informed the replacement policy:

- Ordinary ambiguity should move a score by only one or two points. Three-point deductions now require a confirmed fact and evidence.
- Absence-based deductions need positive evidence. Truncated or uncertain text cannot support them; visually dependent absence rules require rendered-page evidence.

One more evidence gap is worth closing in the packager: `focused_excerpt()` filters a page down to lines carrying a section heading, a number or a `SECONDARY_TERMS` match whenever the page exceeds its limit, and ordinary answer prose matches none of those. In `2023141460325` (孔子涵) the packet replaced question 2's answer on page 4 with `[non-scoring text omitted by script]`, and the grader recovered it only by rendering the page for the format item. Terminal pages are protected now; data pages are not.

The full measurement — run inventory, item-level attribution, the traced students, the score impact and the open policy options — is in [regrade-gate-report.md](regrade-gate-report.md).

One operational detail when running the blind passes: `prepare` writes stub rows into both `decisions-*.jsonl` and `recheck-*.jsonl`, so each pass must *overwrite* a file it is forbidden to read. Some hosts refuse a write to a file that has not been read in the same session. Write the pass with shell redirection (or a writable temp file plus a move) rather than a tool that enforces read-before-write — reading the stub to satisfy the tool would mean reading the other pass's file, which defeats the blind requirement. Reading the stub itself is harmless, but the tool cannot know that.

Scope of the measurement: one course, one 62-report cohort, one experiment, and graders that included an automated multi-agent pass. Treat the magnitudes as an order of magnitude — the direction of the conclusion (judging dominates) is the durable part.
