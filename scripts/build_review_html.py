#!/usr/bin/env python3
"""Build a private, self-contained browser review panel from review JSON files.

The only panel writer. It replaced a Node version that existed for no functional reason -- the
panel is a static template with one ``__REVIEWS_JSON__`` placeholder, so the substitution was the
entire job. Everything the skill writes is Python now.

The embedded payload has to be shaped exactly as the panel's own JavaScript expects, so these
details are load-bearing rather than stylistic:
  * JSON is emitted compactly (separators without spaces), like JSON.stringify
  * non-ASCII is emitted raw (ensure_ascii=False), like JSON.stringify
  * every "<" becomes the literal escape \\u003c, like replaceAll("<", "\\\\u003c")
  * reviews are read from a name-sorted listing and carry __review_file last

Usage: build_review_html.py <review-dir> <output.html> [batch-notices.json] [preflight.json]

The optional third argument is a JSON file holding ``{"batch_notices": [...]}``. Those are the
batch's settings-level notes (see ``BATCH_LEVEL_WARNINGS`` in materialize_reviews.py), which the
panel shows once above the report list instead of repeating on every report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLACEHOLDER = "__REVIEWS_JSON__"
NOTICES_PLACEHOLDER = "__BATCH_NOTICES_JSON__"
PREFLIGHT_PLACEHOLDER = "__PREFLIGHT_JSON__"


def build(
    review_dir: Path,
    output_path: Path,
    template_path: Path,
    notices_path: Path | None = None,
    preflight_path: Path | None = None,
) -> dict:
    names = sorted(
        name for name in (entry.name for entry in review_dir.iterdir())
        if name.lower().endswith(".review.json")
    )
    if not names:
        raise ValueError("No review JSON files found")

    reviews = []
    for name in names:
        review = json.loads((review_dir / name).read_text(encoding="utf-8"))
        review["__review_file"] = name
        reviews.append(review)

    notices: list[str] = []
    if notices_path is not None:
        loaded = json.loads(notices_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            loaded = loaded.get("batch_notices", [])
        if not isinstance(loaded, list):
            raise ValueError("batch notices must be a JSON list")
        notices = [str(item) for item in loaded if str(item).strip()]
    preflight: list[dict] = []
    if preflight_path is not None and preflight_path.is_file():
        loaded = json.loads(preflight_path.read_text(encoding="utf-8"))
        preflight = loaded.get("entries", []) if isinstance(loaded, dict) else loaded
        if not isinstance(preflight, list):
            raise ValueError("preflight inventory must be a JSON list")
    else:
        batch_info_path = review_dir.parent / "批次信息.json"
        if batch_info_path.is_file():
            batch_info = json.loads(batch_info_path.read_text(encoding="utf-8"))
            preflight = batch_info.get("submission_preflight", [])

    # Same escaping contract as the reviews payload: raw non-ASCII, "<" as \u003c so a notice
    # that quotes the template cannot terminate the surrounding <script> element.
    def encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    template = template_path.read_text(encoding="utf-8")
    for placeholder in (PLACEHOLDER, NOTICES_PLACEHOLDER, PREFLIGHT_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise ValueError(f"Review template must contain exactly one {placeholder} placeholder")
    # str.replace takes the plain string path, so a "$&" inside a student's comment is literal.
    html = template.replace(PLACEHOLDER, encode(reviews)).replace(
        NOTICES_PLACEHOLDER, encode(notices)
    ).replace(PREFLIGHT_PLACEHOLDER, encode(preflight))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" disables newline translation. Without it, Windows turns every LF in the template
    # into CRLF, so the Python panel stops being byte-identical to the Node one — which is exactly
    # what the doctor self-test compares, and it went unnoticed only because a host with no Node
    # has just one writer to compare against.
    output_path.write_text(html, encoding="utf-8", newline="")
    return {
        "output": str(output_path),
        "reports": len(reviews),
        "batch_notices": len(notices),
        "template": str(template_path),
    }


def main() -> int:
    if len(sys.argv) not in {3, 4, 5}:
        print(
            "Usage: build_review_html.py <review-dir> <output.html> [batch-notices.json] [preflight.json]",
            file=sys.stderr,
        )
        return 2
    skill_dir = Path(__file__).resolve().parents[1]
    try:
        result = build(
            Path(sys.argv[1]).resolve(),
            Path(sys.argv[2]).resolve(),
            skill_dir / "assets" / "review-panel-template.html",
            Path(sys.argv[3]).resolve() if len(sys.argv) == 4 else None,
            Path(sys.argv[4]).resolve() if len(sys.argv) == 5 else None,
        )
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({**result, "ok": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
