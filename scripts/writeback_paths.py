"""Paths the PDF write-back depends on, kept free of third-party imports.

``batch_pipeline doctor`` has to report on these before the heavy dependencies are known to
be importable, so they live here rather than in the modules that actually use them.

Both defaults used to be absolute paths to one developer's machine. They are now produced by
``resolve_env`` (flag -> environment -> ``config/local.json`` -> bundled asset -> platform
probe), and the constants below stay plain strings so existing callers can keep doing
``Path(DEFAULT_...)`` and checking ``.is_file()``. ``SIGNATURE_PDF_RESOLUTION`` and
``SCORE_FONT_RESOLUTION`` carry the layer that answered, which is what ``doctor`` reports.

The signature has no outside-the-bundle fallback on purpose: it is the teacher's own ink and a
per-machine configuration item. When it is unresolved the constant names the bundle location the
user is being asked to fill, so the failure message points at the fix.
"""

from __future__ import annotations

from resolve_env import SKILL_ROOT, SIGNATURE_BUNDLE_RELATIVE, resolve_score_font, resolve_signature_pdf

SIGNATURE_PDF_RESOLUTION = resolve_signature_pdf()
SCORE_FONT_RESOLUTION = resolve_score_font()

# Teacher-signature template: a PDF carrying the signature as an Ink annotation next to the
# 指导教师签名 label. Override with --signature-pdf, EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF, or
# config/local.json ("signature_pdf").
DEFAULT_SIGNATURE_PDF = str(
    SIGNATURE_PDF_RESOLUTION.value or (SKILL_ROOT / SIGNATURE_BUNDLE_RELATIVE)
)

# Handwriting face for the score written into the 成绩评定 field. Override with --score-font,
# EXPERIMENT_REPORT_GRADER_SCORE_FONT, or config/local.json ("score_font"). As with the signature,
# an unresolved value falls back to the bundle path the user is being asked to fill, so
# review_marking's "score font not found: <path>" names something actionable instead of "".
DEFAULT_SCORE_FONT = str(
    SCORE_FONT_RESOLUTION.value or (SKILL_ROOT / "assets" / "fonts" / "score.ttf")
)
