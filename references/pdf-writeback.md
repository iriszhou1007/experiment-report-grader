# Approved PDF write-back

Prerequisites: `scripts/insert_feedback_page.py` needs `reportlab`, `pypdf` and `PyMuPDF` (`fitz`) in the Python that runs it, plus the teacher's signature asset and a handwriting font. The asset may be an annotated PDF or a tightly cropped transparent PNG. A usable signature asset is a fixed prerequisite for grading: `prepare` validates it before creating the batch. For PNGs the image is placed unchanged in the signature field, and the output verifier checks the field for overlap. `PyMuPDF` is needed during preparation; `reportlab` and the handwriting font are needed for approved write-back. Check everything with `python3 scripts/batch_pipeline.py doctor`, or install with `python3 -m pip install reportlab pypdf PyMuPDF`.

The local review service may write feedback only after explicit approval of the exact report or clearly identified batch.

1. Verify the review is still approved and its immutable source hash matches the current source before first write-back, or the stored original backup on later write-backs.
2. Create or verify one byte-for-byte original under `<batch-folder>/原始数据/`.
3. Before approval, select whether to add handwritten comments to student pages, draw page checkmarks, apply the teacher signature, and handwrite the score. The signature asset remains a fixed precondition checked during preparation; the per-report signature switch only controls whether it is applied. Select whether the feedback sheet goes first or last.
4. After the teacher reviews the score and comments, show the selected write-back settings and ask for explicit confirmation naming the student and source PDF.
5. On confirmation, generate the output from the immutable original, validate it, and replace the source PDF atomically. Do not require a separate PDF preview step.
6. Smoke-render and reopen the result. Verify original pages remain in order and exactly one current feedback page exists.
7. Regenerate Excel and HTML from approved review JSON. `index.md` is refreshed in the same step (`materialize_reviews.py --index-only`) — otherwise it is only rewritten by `finalize` and would keep reporting 待审核 while the workbook already says 已审核.

## What a write-back puts on the page

The teacher selects which marks to apply. The options are:

- optional handwritten item comments on cited student pages; placement is limited to verified blank margins, and generation stops if a comment would overlap existing page content or cannot fit
- optional red checkmarks on student-work pages before the 指导老师评议 page
- optional teacher signature from the preflight-validated fixed asset
- optional handwritten reported score in the 成绩评定 field

The selected marks are applied to the immutable original before the feedback sheet is joined at the selected end:

**Review marks** (`scripts/review_marking.py`, plus `scripts/annotate_report_pdf.py` for standalone use). Ported from [jamesz-dev/hw-worker](https://github.com/jamesz-dev/hw-worker):

- when enabled, one red checkmark on every page of student work **before** the 指导老师评议 page. That page is the signature page: it carries the score and the signature, not reviewed work, so it stays unmarked. In all 62 reports of the 实验4 batch the review page is the last page of student work, and the page after it is the 实验报告说明 boilerplate — that boilerplate is unmarked too.
- its two bezier arms vary in size, proportion, angle, stroke width and colour, seeded from `--seed`, so a batch does not look mechanically stamped
- when enabled, the teacher's signature, copied as an `Ink` annotation from the template PDF and translated to sit after the `指导教师签名：` label, on the review page
- when enabled, the graded score, written after `成绩评定：` on the same page, in a handwriting face, in the same pen colour as the template's ink

### Checkmark variation

hw-worker's routine jitters the endpoints of a fixed template, which leaves the mark's overall size and its long arm's angle nearly constant — measured over 400 draws at 4.4–4.8% and 2.3% coefficient of variation. A page of those reads as stamped.

This skill builds the mark from a bounding box plus shape parameters instead: the box is a wide fraction of the safe zone (33–65% of its width, 40–84% of its height), the vertex sits anywhere from 30% to 55% along the width, and the short arm falls at its own angle from 26° to 66°. The long arm's angle then follows from those rather than being fixed. Measured over the same 400 draws, size varies at ~19–20%, aspect at 27%, the long arm's angle at 8% and the stroke width at ~20% — roughly 2–4× the previous spread. `scripts/measure_checkmark_variation.py` reproduces those numbers.

Two constraints shape the implementation, and both were found by running hw-worker's validator rather than by inspection:

- **The control hull has to be bounded, not just the endpoints.** A control point can pull a bezier outside the box its endpoints describe. `_fit_into_zone` therefore bounds the hull of all control points, scales it down if it exceeds the safe zone, and shifts it in. It also deflates the zone by half the stroke width first, because a drawing's reported box includes that much on every side — a mark whose hull lands exactly on the edge is reported as outside it.
- **The control points have ordering constraints, not just ranges.** The validator takes a cross product of the chord with each control offset and requires a consistent bend direction, which for the short arm means `u3 + u4 > 1` and for the long arm `v1 < v2`. Sampling those independently breaks the geometry for a small fraction of draws, so the second value is drawn conditionally on the first.

Result: 400 synthetic marks across three page sizes pass `validate_checkmarks.py` cleanly, and so do all 62 reports of the 实验4 batch.

hw-worker's own `validate_checkmarks.py` marks up to and including the review page, so against this skill's output it reports exactly one failure per report — the intentionally unmarked review page, confirmed for all 62. Every other page passes it.

A form whose labels cannot be found raises instead of guessing: marking a guessed page of an official document is worse than refusing. `find_review_page` also refuses when the two labels sit on different pages.

**The score written into 成绩评定 is the reported grade** (`adjusted_total` when the course applies an offset, otherwise `total_score`), matching the 最终成绩 on the feedback sheet. The template's own handwritten score is deliberately not copied — it is a fixed `100`. The size and baseline are searched rather than fixed, because the printed rule under the label sits anywhere from 3 to 57 points away and the score must not touch existing content.

## Verification before the source is replaced

`insert_feedback_page.py` verifies the annotated body against the original and refuses to write when it fails, so a bad mark never reaches a student file. `scripts/verify_annotation.py` runs the same checks standalone. Both work at vector level, not by comparing rendered pixels:

- each promised page gained exactly one checkmark, and no unmarked page gained any — counted by grouping red bezier paths in the safe zone. A pixel diff cannot decide this: many of these reports are scans where a third of the page is already dark, so a red check drawn over that content changes no pixel classification at all. Only curves are counted, because the signature is also red ink and can land inside the safe zone.
- the score box and the signature box intersect no pre-existing text or image box on the review page. A pixel overlap test would pass trivially on a dense scan, since ink drawn over dark ink adds no new pixels.

`scripts/annotate_report_pdf.py` additionally reports `seed`, `review_page`, `checkmarked_pages`, `unmarked_pages`, either `signature_ink_annotations` (PDF) or `signature_image_rect`/`signature_image_count` (PNG), and the score placement (`score_rect`, `score_font_size`, `score_placement`, `cap_height_ratio`), so any result can be re-derived later.

## The feedback sheet

The feedback sheet can go **first** or **last**. The selected position determines whether the feedback sheet is inserted first or appended last. Original pages keep their content and relative order. When inserted first, original page numbers shift down by one; when appended last, they do not. Evidence page numbers in the review JSON always refer to the original report, which is also why `原始数据/` keeps the untouched original. The review page is reported as `review_page_in_output`.

The sheet contains only student identity, the score, item scores, and the overall comment. Exclude hashes, warnings, evidence lists, and approval metadata. Editing an approved draft returns it to draft and requires approval again.

When a course-level offset is in effect the sheet prints 「批改得分」 and 「最终成绩」 together, with the adjustment named (`课程统一调整（+5，上限 100）`) and a line noting that the item scores above sum to the graded score. Printing only the graded figure would contradict the grade the student is officially given; printing only the adjusted figure would leave the item scores adding up to a smaller number with no explanation. Without an offset the sheet still shows a single 「总分」 line. The total is always the raw `total_score` for validation purposes: item scores must sum to it.

Page-count expectations when approving: `insert_feedback_page.py` reports `source_pages`, `feedback_pages`, `feedback_page`, `feedback_position`, and `total_pages`, so a write-back is verifiable as exactly one added feedback page at the selected end.

## Options

- `--signature-file` (alias `--signature-pdf`) — annotated PDF or transparent PNG signature asset (default `scripts/writeback_paths.py: DEFAULT_SIGNATURE_PDF`)
- `--score-font` — handwriting face for the score (default Bradley Hand Bold)
- `--seed` — makes the checkmark variation reproducible; the same seed reproduces a batch exactly
- `--no-review-marks` — prepend the feedback sheet only, without checkmarks, signature or score
