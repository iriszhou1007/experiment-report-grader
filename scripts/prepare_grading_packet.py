#!/usr/bin/env python3
"""Prepare a compact, page-linked grading packet from a PDF batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import resolve_env

from pypdf import PdfReader

try:
    from PIL import Image
except ImportError:  # Visual extraction remains optional.
    Image = None


PRIORITY_TERMS = (
    "\u6570\u636e", "\u8bb0\u5f55", "\u8ba1\u7b97", "\u7ed3\u679c",
    "\u7ed3\u8bba", "\u5c0f\u7ed3", "\u603b\u7ed3", "\u5206\u6790",
    "\u6536\u83b7", "\u5fc3\u5f97", "\u95ee\u9898", "\u6539\u8fdb",
)
HEADING_TERMS = ("\u5b9e\u9a8c\u76ee\u7684", "\u5b9e\u9a8c\u73af\u5883", "\u5b9e\u9a8c\u5185\u5bb9", "\u5b9e\u9a8c\u6b65\u9aa4", "\u5b9e\u9a8c\u8981\u6c42")
SECTION_TERMS = {
    "data_and_calculation": ("\u6570\u636e\u8bb0\u5f55", "\u6570\u636e\u4e0e\u8ba1\u7b97", "\u6570\u636e\u8ba1\u7b97", "\u8ba1\u7b97\u7ed3\u679c", "\u5b9e\u9a8c\u7ed3\u679c", "\u7ed3\u679c\u5206\u6790"),
    "conclusion": ("\u5b9e\u9a8c\u7ed3\u8bba", "\u7ed3\u8bba"),
    "summary": ("\u5b9e\u9a8c\u5c0f\u7ed3", "\u5c0f\u7ed3", "\u5b9e\u9a8c\u603b\u7ed3", "\u603b\u7ed3", "\u5fc3\u5f97", "\u6536\u83b7", "\u6539\u8fdb\u5efa\u8bae"),
}
SECONDARY_TERMS = ("\u95ee\u9898", "\u539f\u56e0", "\u6539\u8fdb", "\u8bef\u5dee", "\u5206\u6790")
VISUAL_ONLY_TERMS = ("\u66f2\u7ebf", "\u6298\u7ebf\u56fe", "\u67f1\u72b6\u56fe", "\u6563\u70b9\u56fe", "\u62d3\u6251\u56fe", "\u5750\u6807\u8f74", "\u6ce2\u5f62\u56fe")
# Per-page excerpt ceilings. Pages are never dropped outright: MIN_PAGE_CHARS is a
# presence floor, so every page of every report reaches the model at least once.
FOCUS_PAGE_LIMIT = 2200
EDGE_PAGE_LIMIT = 650
TERTIARY_PAGE_LIMIT = 320
MIN_PAGE_CHARS = 200
# The conclusion and summary cells decide every absence-based rule ("is the column blank?"),
# and they are short. Shrinking them to the plain-page floor is what makes a packet end at the
# column label while the student's paragraph sits on the next line: the two remedies a grader
# then has - assume blank, or assume present - are both guesses, and they disagree. So these
# pages keep at least TERMINAL_PAGE_LIMIT characters through budget scaling.
TERMINAL_SECTIONS = ("conclusion", "summary")
TERMINAL_PAGE_LIMIT = 1200
# OCR text is the only text some pages have, so it gets a larger share than a
# plain low-priority page, but stays bounded.
OCR_PAGE_LIMIT = 1600
OCR_EXCERPT_CHARS = 1600
# Prepended to OCR text so the model never treats recognition errors as student errors.
OCR_MARKER = "[OCR-\u81ea\u52a8\u8bc6\u522b\uff0c\u53ef\u80fd\u6709\u9519\u5b57\uff0c\u4e0d\u8981\u636e\u6b64\u5224\u5b9a\u672f\u8bed/\u62fc\u5199/\u6570\u503c\u9519\u8bef]"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# C0/C1 controls other than tab and newline. A PDF whose text layer is not decoded properly --
# pypdf, for instance, hands back the raw bytes of a CMap it cannot read ("Advanced encoding
# /UniGB-UTF16-H not implemented yet") -- would otherwise put NULs and escape bytes straight into
# the evidence packet the model reads, where they also count against the text budget. Measured on
# a deliberately broken layer: 40.6% of extracted characters were control bytes; a healthy layer
# measured 0.0%.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Past this share of control characters the text layer is treated as undecodable and the report is
# flagged, rather than silently graded on whatever fragments happened to survive.
UNREADABLE_TEXT_RATIO = 0.05


def clean_text(value: str) -> str:
    value = CONTROL_CHARS.sub("", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r", "").split("\n")]
    result: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        result.append(line)
        previous_blank = blank
    return "\n".join(result).strip()


def canonical_line(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


EXPERIMENT_TAG = re.compile(r"^(?:实验|第)", re.IGNORECASE)


def parse_identity(path: Path) -> tuple[str, str, str]:
    """Pull student id, name and experiment label out of a filename.

    Accepts `01_实验1_学号_姓名.pdf`, `学号-姓名-实验04.pdf`, and
    `06_实验1_姓名_学号(1).pdf`: fields may be separated by underscore, hyphen or
    whitespace, and a trailing Windows duplicate marker such as `(1)` is ignored.
    The identifier is an 8-20 digit field; the name is collected from the other
    nonnumeric, non-topic fields, whether it appears before or after the identifier.
    """
    # Windows Explorer appends a duplicate marker before the extension, e.g. `(1)`.
    # Remove that marker first so `2024141460545(1)` is recognized as the same student ID.
    stem = re.sub(r"\s*\(\d+\)$", "", path.stem)
    parts = [part.strip() for part in re.split(r"[_\-\s]+", stem) if part.strip()]
    id_pattern = re.compile(r"\d{8,20}")
    index = next((i for i, part in enumerate(parts) if id_pattern.fullmatch(part)), -1)
    student_id = parts[index] if index >= 0 else ""
    experiments = [part for part in parts if EXPERIMENT_TAG.match(part)]
    experiment = experiments[-1] if experiments else ""
    name_parts = [
        part for part in parts
        if not id_pattern.fullmatch(part)
        and not part.isdecimal()
        and not EXPERIMENT_TAG.match(part)
    ]
    return student_id, "_".join(name_parts), experiment


def extract_pages(path: Path) -> list[dict]:
    reader = PdfReader(str(path))
    result = []
    for page in reader.pages:
        raw = page.extract_text() or ""
        text = clean_text(raw)
        try:
            image_count = len(page.images)
        except Exception:
            image_count = 0
        result.append({
            "text": text,
            "image_count": image_count,
            # Kept only so the report can be flagged as having an undecodable text layer; it is
            # never serialized (the packet stores the selected excerpt, not these page dicts).
            "control_ratio": (len(CONTROL_CHARS.findall(raw)) / len(raw)) if raw else 0.0,
        })
    return result


def page_text(page: str | dict) -> str:
    return str(page.get("text", "")) if isinstance(page, dict) else str(page)


def page_image_count(page: str | dict) -> int:
    if not isinstance(page, dict):
        return 0
    try:
        return max(0, int(page.get("image_count", 0)))
    except (TypeError, ValueError):
        return 0


def dynamic_text_budget(pages: list[str | dict], ceiling: int) -> tuple[int, dict]:
    texts = [page_text(page) for page in pages]
    characters = sum(len(re.sub(r"\s+", "", text)) for text in texts)
    page_count = max(1, len(pages))
    image_pages = sum(1 for page in pages if page_image_count(page))
    low_text_pages = sum(1 for text in texts if len(re.sub(r"\s+", "", text)) < 30)
    sections = {section for focus in locate_page_sections(texts) for section in focus}
    image_ratio = image_pages / page_count
    low_text_ratio = low_text_pages / page_count
    if characters <= 1800:
        target = min(ceiling, max(1200, characters + 300))
        profile = "short"
    elif image_ratio >= 0.4:
        target = min(ceiling, 3000)
        profile = "image_heavy"
    elif len(sections) >= 3:
        target = min(ceiling, 3400)
        profile = "well_structured"
    else:
        target = ceiling
        profile = "complex_or_unstructured"
    return max(1000, int(target)), {
        "profile": profile,
        "ceiling": int(ceiling),
        "selected": max(1000, int(target)),
        "source_characters": characters,
        "image_page_ratio": round(image_ratio, 3),
        "low_text_page_ratio": round(low_text_ratio, 3),
        "detected_section_count": len(sections),
    }


def find_program(explicit: str | None, name: str, candidates: list[Path]) -> Path | None:
    if explicit:
        path = Path(explicit).resolve()
        return path if path.is_file() else None
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered).resolve()
    return next((path for path in candidates if path.is_file()), None)


SKILL_ROOT = Path(__file__).resolve().parent.parent


def ocr_helper_candidates(name: str) -> list[Path]:
    """Fallback locations for an external OCR helper, in the order we prefer them.

    Delegates to ``resolve_env`` so an environment variable or a ``config/local.json`` entry
    applies here too, and so there is one list to keep current. Lookup stays explicit flag ->
    PATH -> these candidates, so anything the caller puts on PATH still wins. Among candidates
    a real program beats a shim: the Codex runtime ships poppler for both platforms but under
    different layouts, and the Windows-only path silently hid a usable pdftoppm on macOS.
    Shims produced by ``scripts/setup_local_ocr.sh`` are the last resort.
    """
    return resolve_env.ocr_helper_candidates(name)


def visual_candidates(pages: list[str | dict], removable: set[str]) -> tuple[list[int], list[dict]]:
    texts = [remove_template_lines(page_text(page), removable)[0] for page in pages]
    focuses = locate_page_sections(texts)
    details = []
    for index, (page, text, focus) in enumerate(zip(pages, texts, focuses)):
        image_count = page_image_count(page)
        reasons = visual_page_assessment(text, image_count, focus)
        if reasons:
            details.append({
                "page": index + 1,
                "image_count": image_count,
                "text_characters": len(re.sub(r"\s+", "", text)),
                "reasons": reasons,
            })
    return [entry["page"] for entry in details], details


HEX_TOKEN = re.compile(r"^[0-9a-fA-F]{1,4}$")


def is_low_signal_ocr_line(line: str) -> bool:
    """True for lines that are almost certainly OCR noise rather than report content.

    Dense screenshots (hex viewers, memory dumps, log panes) make OCR emit grids of
    short hex tokens. Those lines carry no grading evidence and crowd out real text.
    """
    tokens = line.split()
    if len(tokens) >= 4:
        hexish = sum(1 for token in tokens if HEX_TOKEN.match(token))
        if hexish / len(tokens) >= 0.6:
            return True
    compact = re.sub(r"\s+", "", line)
    if len(compact) >= 8:
        signal = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", compact))
        if signal / len(compact) < 0.4:
            return True
    return False


def filter_ocr_text(ocr_text: str, native_text: str) -> tuple[str, dict]:
    """Drop OCR lines that merely repeat the page's text layer or carry no signal.

    OCR renders the whole page, so it re-reads text the PDF already exposed. Keeping
    both copies spends budget twice on the same sentence.
    """
    native_compact = re.sub(r"\s+", "", native_text)
    kept: list[str] = []
    duplicates = 0
    noise = 0
    for raw in ocr_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = re.sub(r"\s+", "", line)
        if len(key) >= 6 and key[:12] in native_compact:
            duplicates += 1
            continue
        if is_low_signal_ocr_line(line):
            noise += 1
            continue
        kept.append(line)
    return "\n".join(kept), {"duplicate_lines": duplicates, "noise_lines": noise}


def ocr_text_is_sufficient(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 100 or len([line for line in text.splitlines() if line.strip()]) < 3:
        return False
    if any(term in text for term in VISUAL_ONLY_TERMS):
        return False
    signal = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", compact))
    return bool(compact) and signal / len(compact) >= 0.55


def ocr_candidate_pages(
    path: Path,
    pages: list[str | dict],
    removable: set[str],
    mode: str = "auto",
    cache_dir: Path | None = None,
    ocr_executable: str | None = None,
    pdftoppm_executable: str | None = None,
) -> dict:
    candidates, details = visual_candidates(pages, removable)
    result = {
        "mode": mode,
        "status": "off" if mode == "off" else "unavailable",
        "candidate_pages": candidates,
        "details": details,
        "texts": {},
        "resolved_pages": [],
        "engine": None,
    }
    if mode == "off" or not candidates:
        result["status"] = "not_needed" if not candidates else "off"
        return result
    tesseract = find_program(
        ocr_executable,
        "tesseract",
        ocr_helper_candidates("tesseract"),
    )
    pdftoppm = find_program(
        pdftoppm_executable,
        "pdftoppm",
        ocr_helper_candidates("pdftoppm"),
    )
    if not tesseract or not pdftoppm:
        return result
    cache_root = (cache_dir or path.parent / ".ocr-cache").resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = cache_root / f"{sha256_file(path)[:20]}.ocr.json"
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # The engine is part of the key: two OCR engines produce different text for
            # the same page, so reusing a cached result across engines would silently
            # keep the old engine's reading. That matters whenever a batch is re-run
            # with a different --ocr-engine or --ocr-executable.
            if (
                cached.get("candidate_pages") == candidates
                and str(cached.get("engine")) == str(tesseract)
            ):
                return cached
        except Exception:
            pass
    texts: dict[str, str] = {}
    failures = []
    digest = sha256_file(path)[:12]
    for page_number in candidates:
        prefix = cache_root / f"{digest}-p{page_number}"
        image_path = prefix.with_suffix(".png")
        try:
            rendered = subprocess.run(
                [str(pdftoppm), "-f", str(page_number), "-l", str(page_number), "-r", "160", "-png", "-singlefile", str(path), str(prefix)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
            if rendered.returncode != 0 or not image_path.is_file():
                failures.append(page_number)
                continue
            recognized = subprocess.run(
                [str(tesseract), str(image_path), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if recognized.returncode != 0:
                recognized = subprocess.run(
                    [str(tesseract), str(image_path), "stdout", "-l", "eng", "--psm", "6"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
            text = clean_text(recognized.stdout) if recognized.returncode == 0 else ""
            if text:
                texts[str(page_number)] = text
        except (OSError, subprocess.SubprocessError):
            failures.append(page_number)
        finally:
            if image_path.exists():
                image_path.unlink()
    resolved = [int(page) for page, text in texts.items() if ocr_text_is_sufficient(text)]
    result.update({
        "status": "completed" if not failures else "partial",
        "texts": texts,
        "resolved_pages": sorted(resolved),
        "engine": str(tesseract),
        "failed_pages": sorted(set(failures)),
    })
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def build_template_profile(all_pages: list[list[str | dict]]) -> dict:
    if len(all_pages) < 2:
        return {"version": 1, "report_count": len(all_pages), "lines": [], "fingerprint": ""}
    frequency: Counter[str] = Counter()
    original: dict[str, str] = {}
    for report_pages in all_pages:
        seen: set[str] = set()
        for page in report_pages:
            for line in page_text(page).splitlines():
                key = canonical_line(line)
                instruction_like = bool(re.search(r"(?:请|要求|需要|完成|观察|记录|按照|本实验|填写|说明)", line))
                if len(key) < 16 or (any(term in line for term in PRIORITY_TERMS) and not instruction_like):
                    continue
                if re.search(r"\d{8,20}", line) or re.search(r"[=<>]", line):
                    continue
                template_heading = any(term in line for term in HEADING_TERMS)
                if not template_heading and not instruction_like:
                    continue
                seen.add(key)
                original.setdefault(key, line)
        frequency.update(seen)
    threshold = max(2, math.ceil(len(all_pages) * 0.6))
    lines = sorted(
        key
        for key, count in frequency.items()
        if count >= threshold
    )
    fingerprint = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest() if lines else ""
    return {
        "version": 1,
        "report_count": len(all_pages),
        "threshold": threshold,
        "lines": lines,
        "samples": [original[key] for key in lines[:30]],
        "fingerprint": fingerprint,
    }


def removable_common_lines(all_pages: list[list[str | dict]]) -> set[str]:
    return set(build_template_profile(all_pages)["lines"])


def classify_grading_route(
    pages: list[str | dict],
    removable: set[str],
    inspection_mode: str,
    ocr_result: dict,
) -> dict:
    cleaned = [remove_template_lines(page_text(page), removable)[0] for page in pages]
    sections = {section for focus in locate_page_sections(cleaned) for section in focus}
    page_count = max(1, len(cleaned))
    low_text_pages = sum(1 for text in cleaned if len(re.sub(r"\s+", "", text)) < 30)
    unresolved_visual = sorted(
        set(int(page) for page in ocr_result.get("candidate_pages", []))
        - set(int(page) for page in ocr_result.get("resolved_pages", []))
    )
    reasons: list[str] = []
    if not sections:
        reasons.append("scoring_sections_not_detected")
    if low_text_pages / page_count >= 0.3:
        reasons.append("many_low_text_pages")
    if inspection_mode in {"smart", "full_visual"} and len(unresolved_visual) > max(8, math.ceil(page_count * 0.6)):
        reasons.append("visual_evidence_requires_review")
    if reasons:
        tier = "detailed"
    elif len(sections) == len(SECTION_TERMS) and (not unresolved_visual or inspection_mode == "text_only"):
        tier = "fast"
    else:
        tier = "standard"
        if len(sections) < len(SECTION_TERMS):
            reasons.append("some_scoring_sections_not_detected")
        if unresolved_visual:
            reasons.append("visual_evidence_not_in_text")
    return {
        "tier": tier,
        "reasons": reasons,
        "detected_sections": sorted(sections),
        "unresolved_visual_pages": unresolved_visual,
    }


def select_visual_review_pages(ocr_result: dict, maximum: int = 8) -> list[int]:
    unresolved = sorted(
        set(int(page) for page in ocr_result.get("candidate_pages", []))
        - set(int(page) for page in ocr_result.get("resolved_pages", []))
    )
    details = {int(item.get("page", 0)): item for item in ocr_result.get("details", [])}

    def priority(page: int) -> tuple[int, int]:
        reasons = set(details.get(page, {}).get("reasons", []))
        score = 0
        score += 5 if "scoring_section_contains_image" in reasons else 0
        score += 4 if "no_extractable_text" in reasons else 0
        score += 3 if "very_low_text" in reasons else 0
        score += 2 if "image_with_limited_text" in reasons else 0
        score += 1 if "multiple_images" in reasons else 0
        return (-score, page)

    return sorted(sorted(unresolved, key=priority)[:max(1, maximum)])


# Tier ceilings exist to stop a generous ceiling from spending budget on reports that do not
# need it. They must never override an explicitly raised ceiling: when the caller asks for more
# text than the tier would allow, that request is the authority, not the tier's default.
TIER_DEFAULT_CEILING = {"fast": 2800, "detailed": None, "standard": 3400}


def apply_route_budget(budget: int, ceiling: int, route: dict) -> tuple[int, dict]:
    tier = route.get("tier", "standard")
    tier_limit = TIER_DEFAULT_CEILING.get(tier, 3400)
    if tier_limit is None:
        selected = ceiling
    else:
        # Honour whichever is larger: the tier's own limit, or the ceiling the caller set.
        selected = min(budget, ceiling, max(tier_limit, ceiling))
    return max(1000, int(selected)), {
        "grading_tier": tier,
        "tier_reasons": route.get("reasons", []),
        "tier_ceiling": tier_limit,
        "ceiling_applied": selected,
    }


def extract_visual_assets(
    path: Path,
    page_numbers: list[int],
    output_dir: Path,
    max_assets_per_page: int = 3,
    max_assets_per_report: int = 8,
) -> list[dict]:
    if not page_numbers or Image is None:
        return []
    reader = PdfReader(str(path))
    report_dir = output_dir / sha256_file(path)[:12]
    assets: list[dict] = []
    for page_number in sorted(set(page_numbers)):
        if not 1 <= page_number <= len(reader.pages):
            continue
        candidates = []
        try:
            for image in reader.pages[page_number - 1].images:
                pil_image = image.image.convert("RGB")
                width, height = pil_image.size
                if width * height < 80_000 or min(width, height) < 120:
                    continue
                candidates.append((width * height, image.name, pil_image))
        except Exception:
            continue
        for asset_index, (_, source_name, pil_image) in enumerate(
            sorted(candidates, key=lambda item: item[0], reverse=True)[:max_assets_per_page],
            start=1,
        ):
            if len(assets) >= max_assets_per_report:
                return assets
            report_dir.mkdir(parents=True, exist_ok=True)
            if max(pil_image.size) > 1600:
                pil_image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            target = report_dir / f"p{page_number:03d}-{asset_index}.jpg"
            pil_image.save(target, format="JPEG", quality=82, optimize=True)
            assets.append({
                "page": page_number,
                "kind": "embedded_image",
                "source_image": source_name,
                "path": str(target.resolve()),
                "width": pil_image.width,
                "height": pil_image.height,
            })
    return assets


def remove_template_lines(page: str, removable: set[str]) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    for line in page.splitlines():
        if canonical_line(line) in removable:
            removed += len(line)
        else:
            kept.append(line)
    return clean_text("\n".join(kept)), removed


def excerpt(text: str, limit: int) -> tuple[str, int]:
    if len(text) <= limit:
        return text, 0
    head = max(1, int(limit * 0.72))
    tail = max(1, limit - head)
    marker = "\n...[middle omitted by script]...\n"
    return text[:head] + marker + text[-tail:], len(text) - limit


def section_for_heading(line: str) -> str | None:
    compact = canonical_line(line).strip("0123456789.、（）()：:")
    if not compact or len(compact) > 28 or re.search(r"[，。；！？!?]", compact):
        return None
    for section, terms in SECTION_TERMS.items():
        for term in terms:
            if compact == term or (compact.startswith(term) and len(compact) <= len(term) + 6):
                return section
    return None


def locate_page_sections(pages: list[str]) -> list[list[str]]:
    result: list[list[str]] = []
    active: str | None = None
    for text in pages:
        found: list[str] = []
        stopped = False
        lines = [line for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            combined = line
            if index + 1 < len(lines) and len(canonical_line(line)) <= 8:
                combined += lines[index + 1]
            section = section_for_heading(line) or section_for_heading(combined)
            if section:
                active = section
                if section not in found:
                    found.append(section)
            elif len(canonical_line(combined)) <= 28 and any(term in canonical_line(combined) for term in HEADING_TERMS):
                active = None
                stopped = True
        if active and active not in found and not stopped:
            found.append(active)
        result.append(found)
    return result


def is_numeric_evidence(line: str) -> bool:
    numbers = re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?%?", line)
    return len(numbers) >= 2 or (numbers and bool(re.search(r"[=≈<>±/%]", line)))


def focused_excerpt(text: str, limit: int, focus: list[str]) -> tuple[str, int]:
    if len(text) <= limit:
        return text, 0
    lines = text.splitlines()
    wanted: set[int] = set()
    for index, line in enumerate(lines):
        section = section_for_heading(line)
        if section:
            wanted.update(range(index, min(len(lines), index + 10)))
        if is_numeric_evidence(line):
            wanted.update(range(max(0, index - 1), min(len(lines), index + 2)))
        if any(term in line for term in SECONDARY_TERMS):
            wanted.update(range(max(0, index - 1), min(len(lines), index + 3)))
    if focus:
        wanted.update(range(max(0, len(lines) - 5), len(lines)))
    if not wanted:
        return excerpt(text, limit)
    selected: list[str] = []
    previous = -2
    for index in sorted(wanted):
        if index > previous + 1 and selected:
            selected.append("...[non-scoring text omitted by script]...")
        selected.append(lines[index])
        previous = index
    compact = clean_text("\n".join(selected))
    if len(compact) > limit:
        compact, _ = excerpt(compact, limit)
    return compact, max(0, len(text) - len(compact))


def visual_page_assessment(text: str, image_count: int, focus: list[str]) -> list[str]:
    characters = len(re.sub(r"\s+", "", text))
    reasons: list[str] = []
    if characters == 0:
        reasons.append("no_extractable_text")
    elif characters < 30:
        reasons.append("very_low_text")
    if image_count and characters < 250:
        reasons.append("image_with_limited_text")
    if image_count >= 2:
        reasons.append("multiple_images")
    if image_count and focus:
        reasons.append("scoring_section_contains_image")
    return reasons


def compact_report(
    path: Path,
    pages: list[str | dict],
    removable: set[str],
    budget: int,
    inspection_mode: str = "text_only",
    auto_upgrade_visual: bool = False,
    ocr_result: dict | None = None,
    budget_profile: dict | None = None,
    grading_route: dict | None = None,
    visual_assets: list[dict] | None = None,
    full_evidence: bool = False,
) -> dict:
    cleaned_pages: list[str] = []
    image_counts: list[int] = []
    template_chars_removed = 0
    for page in pages:
        cleaned, removed = remove_template_lines(page_text(page), removable)
        cleaned_pages.append(cleaned)
        image_counts.append(page_image_count(page))
        template_chars_removed += removed

    priorities = locate_page_sections(cleaned_pages)
    detected_candidates, detected_details = visual_candidates(pages, removable)
    ocr_result = ocr_result or {
        "mode": "off",
        "status": "off",
        "candidate_pages": detected_candidates,
        "details": detected_details,
        "texts": {},
        "resolved_pages": [],
        "engine": None,
    }
    visual_details = ocr_result.get("details") or detected_details
    visual_candidate_pages = ocr_result.get("candidate_pages") or detected_candidates
    ocr_resolved_pages = sorted({int(page) for page in ocr_result.get("resolved_pages", [])})
    ocr_filtered = {"duplicate_lines": 0, "noise_lines": 0}
    for page_number in ocr_resolved_pages:
        ocr_text = str(ocr_result.get("texts", {}).get(str(page_number), "")).strip()
        if ocr_text and 1 <= page_number <= len(cleaned_pages):
            native = cleaned_pages[page_number - 1]
            filtered, stats = filter_ocr_text(ocr_text, native)
            ocr_filtered["duplicate_lines"] += stats["duplicate_lines"]
            ocr_filtered["noise_lines"] += stats["noise_lines"]
            if not filtered.strip():
                continue
            compact_ocr, _ = excerpt(filtered, OCR_EXCERPT_CHARS)
            cleaned_pages[page_number - 1] = clean_text(
                native + "\n" + OCR_MARKER + "\n" + compact_ocr
            )
    priorities = locate_page_sections(cleaned_pages)
    if inspection_mode == "full_visual":
        visual_review_pages = list(range(1, len(cleaned_pages) + 1))
    elif inspection_mode == "smart" or auto_upgrade_visual:
        visual_review_pages = select_visual_review_pages(ocr_result)
    else:
        visual_review_pages = []

    # Every page enters the evidence. Scoring-section, visual and edge signals only decide
    # how much budget a page receives, never whether the page exists at all: a silently
    # absent page is indistinguishable from a page the student left blank.
    def page_ceiling(index: int) -> int:
        if full_evidence:
            # Full-evidence mode: a page may use all of its own text. The per-page ceilings
            # below encode a ranking of *importance*, and ranking is exactly what a whole
            # report can afford to skip when the caller has already accepted the cost.
            return len(cleaned_pages[index])
        if index + 1 in ocr_resolved_pages:
            return OCR_PAGE_LIMIT
        if priorities[index]:
            return FOCUS_PAGE_LIMIT
        if index in {0, len(cleaned_pages) - 1}:
            return EDGE_PAGE_LIMIT
        return TERTIARY_PAGE_LIMIT

    order = sorted(
        range(len(cleaned_pages)),
        key=lambda i: (
            0 if i + 1 in ocr_resolved_pages else 1,
            0 if priorities[i] else 1,
            0 if i in {0, len(cleaned_pages) - 1} else 1,
            i,
        ),
    )
    page_limits: dict[int, int] = {
        index: min(page_ceiling(index), len(cleaned_pages[index]))
        for index in range(len(cleaned_pages))
    }
    ideal_total = sum(page_limits.values())
    if full_evidence:
        # In full-evidence mode the budget is a reporting figure, not a cap: shrinking pages to
        # fit it would reintroduce exactly the truncation this mode exists to remove. The
        # packet reports the overrun instead (packet_over_budget), which is the honest outcome.
        pass
    elif ideal_total > budget:
        # Scale toward the budget, but never below the presence floor. When the floor
        # itself exceeds the budget, presence wins and the packet reports the overrun.
        scale = budget / ideal_total
        for index in list(page_limits):
            floor = min(MIN_PAGE_CHARS, len(cleaned_pages[index]))
            page_limits[index] = max(floor, int(page_limits[index] * scale))
            if any(section in TERMINAL_SECTIONS for section in priorities[index]):
                # Never let the global scale cut a conclusion/summary page down to the
                # plain-page floor: that is exactly the truncation that turns a real
                # paragraph into an unanswerable "is this column blank?" question.
                page_limits[index] = min(
                    len(cleaned_pages[index]),
                    max(page_limits[index], TERMINAL_PAGE_LIMIT),
                )
    else:
        # Leftover budget un-truncates pages in the same priority order. Without this the
        # per-page ceilings stay binding even when the budget is generous, so a long report
        # loses content it could have afforded — a plain non-scoring page is capped at 320
        # characters however much budget remains.
        leftover = budget - ideal_total
        for index in order:
            if leftover <= 0:
                break
            full = len(cleaned_pages[index])
            if page_limits[index] < full:
                extra = min(full - page_limits[index], leftover)
                page_limits[index] += extra
                leftover -= extra

    selected: dict[int, dict] = {}
    omitted_chars = 0
    truncated_pages: list[int] = []
    for index in order:
        text, omitted = focused_excerpt(cleaned_pages[index], page_limits[index], priorities[index])
        if omitted:
            truncated_pages.append(index + 1)
        selected[index] = {
            "page": index + 1,
            "focus": priorities[index],
            "image_count": image_counts[index],
            "excerpt_limit": page_limits[index],
            "ocr": index + 1 in ocr_resolved_pages,
            "text": text,
        }
        omitted_chars += omitted
    budget_used = sum(len(page["text"]) for page in selected.values())

    nonblank = [len(re.sub(r"\s+", "", page)) for page in cleaned_pages]
    low_text_pages = [index + 1 for index, size in enumerate(nonblank) if size < 30]
    detected_terms = [term for term in PRIORITY_TERMS if any(term in page for page in cleaned_pages)]
    detected_sections = [section for section in SECTION_TERMS if any(section in focus for focus in priorities)]
    warnings: list[str] = []
    if low_text_pages:
        warnings.append("low_text_pages")
    # A page whose text layer could not be decoded has had its garbage stripped by clean_text, so
    # what reaches the model is a fragment of the real content rather than noise. That is safer
    # but still lossy, and without a flag the report would look like an ordinary thin one.
    undecodable_pages = [
        index + 1
        for index, page in enumerate(pages)
        if isinstance(page, dict)
        and float(page.get("control_ratio") or 0.0) >= UNREADABLE_TEXT_RATIO
    ]
    if undecodable_pages:
        warnings.append("unreadable_text_layer")
    if not any(priorities):
        warnings.append("priority_headings_not_detected")
    if omitted_chars:
        warnings.append("compact_text_omitted")
    elif full_evidence:
        warnings.append("full_evidence_packet")
    if budget_used > budget:
        warnings.append("packet_over_budget")
    if visual_candidate_pages:
        warnings.append("visual_content_detected")
    if visual_candidate_pages and not visual_review_pages:
        warnings.append("visual_review_not_requested")
    if len(set(visual_candidate_pages) - set(ocr_resolved_pages)) > len(visual_review_pages) and visual_review_pages:
        warnings.append("visual_review_sampled")

    student_id, student_name, experiment = parse_identity(path)
    return {
        "source_file": str(path.resolve()),
        "source_name": path.name,
        "source_sha256": sha256_file(path),
        "student_id": student_id,
        "student_name": student_name,
        "experiment": experiment,
        "page_count": len(pages),
        "extracted_characters": sum(len(page) for page in cleaned_pages),
        "budget_characters": budget,
        "budget_used": budget_used,
        "budget_exceeded": budget_used > budget,
        "pages_included": len(selected),
        "pages_omitted": [index + 1 for index in range(len(cleaned_pages)) if index not in selected],
        "pages_truncated": sorted(truncated_pages),
        "ocr_pages": list(ocr_resolved_pages),
        "ocr_filtered_lines": ocr_filtered,
        "budget_profile": budget_profile or {"profile": "fixed", "selected": budget, "ceiling": budget},
        "grading_route": grading_route or {"tier": "standard", "reasons": []},
        "template_characters_removed": template_chars_removed,
        "packet_characters_omitted": omitted_chars,
        "image_pages": [index + 1 for index, count in enumerate(image_counts) if count],
        "visual_candidate_pages": visual_candidate_pages,
        "visual_review_pages": visual_review_pages,
        "visual_page_details": visual_details,
        "visual_assets": visual_assets or [],
        "ocr_status": ocr_result.get("status", "off"),
        "ocr_engine": ocr_result.get("engine"),
        "ocr_resolved_pages": ocr_resolved_pages,
        "detected_sections": detected_sections,
        "detected_priority_terms": detected_terms,
        "warnings": warnings,
        "pages": [selected[index] for index in sorted(selected)],
    }


def rubric_header(rubric: dict | None) -> str | None:
    """Render the confirmed rubric so the positional score array is never ambiguous."""
    if not isinstance(rubric, dict):
        return None
    items = rubric.get("items")
    if not isinstance(items, list) or not items:
        return None
    parts = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return None
        parts.append(f"{index}.{item.get('criterion', '?')}/{item.get('max_score', '?')}")
    maximum = rubric.get("max_score")
    if maximum in (None, ""):
        maximum = sum(item.get("max_score", 0) for item in items if isinstance(item, dict))
    deduction_mode = all(
        isinstance(item, dict) and item.get("deduction_rules") for item in items
    )
    score_note = (
        ' Output "d" deduction facts; the program calculates "s".'
        if deduction_mode else
        f' "s" must be exactly {len(items)} integers in this order, each within its own maximum.'
    )
    lines = [f"rubric ({len(items)} items, total {maximum}): " + " | ".join(parts) + score_note]
    band_lines = []
    deduction_lines = []
    for index, item in enumerate(items, start=1):
        bands = item.get("bands") if isinstance(item, dict) else None
        if isinstance(bands, list) and bands:
            rendered = []
            for band in bands:
                if isinstance(band, dict):
                    visual_suffix = " [必须有视觉证据页 v]" if band.get("requires_visual_evidence") else ""
                    rendered.append(
                        f"{band.get('id', '?')}={band.get('score', '?')}分: "
                        f"{band.get('description', band.get('label', ''))}{visual_suffix}"
                    )
                else:
                    rendered.append(str(band))
            band_lines.append(f"  {index}.{item.get('criterion', '?')}: " + "; ".join(rendered))
        rules = item.get("deduction_rules") if isinstance(item, dict) else None
        if isinstance(rules, list) and rules:
            rendered = []
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                visual_suffix = " [必须有视觉证据页 v]" if rule.get("requires_visual_evidence") else ""
                exclusion_suffix = ""
                if rule.get("exclusive_with"):
                    exclusion_suffix = " [不得同时使用: " + ",".join(rule["exclusive_with"]) + "]"
                conditional_suffix = ""
                if rule.get("only_if_no_other_deductions"):
                    conditional_suffix = " [仅当整份报告没有任何其他扣分时启用；不得与任何分项的其他规则叠加]"
                requirement_suffix = ""
                if rule.get("requirement_ids"):
                    requirement_suffix = " [requirements可选: " + ",".join(rule["requirement_ids"]) + "]"
                counting_suffix = ""
                if rule.get("count_by") == "figure":
                    counting_suffix = (
                        f" [每张问题截图扣{rule.get('points')}分，最多计{rule.get('max_occurrences', 1)}张；"
                        "同图多种问题仅计一张。一个d条目附figures:[{id:'p.N#imageK',requirements:[...],reason:'...'}]；"
                        "K是原页截图自上而下、同高从左到右的序号，不使用缺失的图号；每张所处页均须已视觉核验。"
                        "顶层requirements等于各图缺失要素的并集，程序按图数计算并封顶]"
                    )
                rendered.append(
                    f"{rule.get('id', '?')}=-{rule.get('points', '?')}分: "
                    f"{rule.get('description', '')}{visual_suffix}{exclusion_suffix}{conditional_suffix}{requirement_suffix}{counting_suffix}"
                )
            caps = item.get("deduction_group_caps") or {}
            cap_text = ""
            if caps:
                cap_text = "; 分组上限 " + ", ".join(f"{key}={value}分" for key, value in caps.items())
            deduction_lines.append(
                f"  {index}.{item.get('criterion', '?')}: " + "; ".join(rendered) + cap_text
            )
        notes = item.get("grading_notes") if isinstance(item, dict) else None
        if isinstance(notes, list) and notes:
            deduction_lines.append(
                f"  {index}.{item.get('criterion', '?')}判定要点: " + " ".join(map(str, notes))
            )
    if band_lines:
        has_fixed_rules = any(
            isinstance(band, dict)
            for item in items if isinstance(item, dict)
            for band in (item.get("bands") or [])
        )
        if has_fixed_rules:
            lines.append(
                "fixed score rules (select exactly one rule id per item; its printed score is mandatory):"
            )
        else:
            lines.append("score bands (pick the band that matches the evidence, then place the score inside it):")
        lines.extend(band_lines)
    if deduction_lines:
        lines.append(
            "evidence deductions (start each item at its maximum; output d facts and let the program calculate s):"
        )
        lines.append(
            "  d entry: {item,rule,fact,reason,certainty,evidence,visual_evidence}; "
            "fact must be unique across the report. More than 2 points requires certainty=confirmed."
        )
        lines.extend(deduction_lines)
        policy = items[0].get("report_deduction_policy", {})
        if rubric.get("require_confirmed_deductions") or policy.get("require_confirmed_deductions"):
            lines.append("All deductions, including 2 points, require explicit certainty=confirmed; uncertainty is a review flag, not a deduction.")
        if rubric.get("require_rule_fact_ids") or policy.get("require_rule_fact_ids"):
            lines.append("Use fact=rule exactly (canonical fact id). For a rule with requirement_ids, list the affected ids in requirements; group all affected requirements into one entry.")
        if rubric.get("recheck_fact_consistency") or policy.get("recheck_fact_consistency"):
            lines.append("Blind passes must agree on rule facts AND affected requirements, not just the total. Differences require adjudication; never copy the first pass.")
        minimum_total = float(rubric.get("minimum_report_deduction", 0) or 0)
        minimum_minor = float(rubric.get("minimum_minor_deduction", 0) or 0)
        maximum_minor_items = int(rubric.get("maximum_minor_items", 0) or 0)
        if minimum_total or minimum_minor or maximum_minor_items:
            lines.append(
                "report-level strictness: "
                f"at least {minimum_total:g} total deduction points; "
                f"at least {minimum_minor:g} ordinary deduction points; "
                f"ordinary deductions may use at most {maximum_minor_items} rubric items."
            )
    return "\n".join(lines)


def coverage_header(report: dict) -> str:
    """State page coverage explicitly so an omitted page can never read as a blank page."""
    total = int(report.get("page_count") or 0)
    included = report.get("pages_included")
    if included is None:
        included = len(report.get("pages") or [])
    omitted = report.get("pages_omitted") or []
    truncated = report.get("pages_truncated") or []
    ocr_pages = report.get("ocr_pages") or report.get("ocr_resolved_pages") or []
    parts = [f"coverage: {included}/{total} pages included"]
    parts.append("omitted pages: " + (",".join(map(str, omitted)) if omitted else "none"))
    parts.append("excerpt truncated on: " + (",".join(map(str, truncated)) if truncated else "none"))
    if ocr_pages:
        parts.append("OCR text merged on: " + ",".join(map(str, ocr_pages)))
        filtered = report.get("ocr_filtered_lines") or {}
        dropped = int(filtered.get("duplicate_lines", 0)) + int(filtered.get("noise_lines", 0))
        if dropped:
            parts.append(f"filtered {dropped} OCR lines repeating the text layer or carrying no signal")
    if report.get("budget_exceeded"):
        parts.append(f"presence floor overran the text budget ({report.get('budget_used')} chars used)")
    return "; ".join(parts)


def anchor_header(anchors: list[dict] | None) -> str | None:
    """Render teacher-confirmed reference scores so calibration is a recorded input.

    Anchors used to live only in whichever prompt happened to carry them, which made a
    graded batch impossible to reproduce or audit. Passing them here puts the same
    standard into every chunk.
    """
    if not anchors:
        return None
    lines = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        name = str(anchor.get("student_name") or anchor.get("label") or "").strip()
        score = anchor.get("total_score", anchor.get("score"))
        reason = str(anchor.get("reason") or anchor.get("comment") or "").strip()
        if not name or score in (None, ""):
            continue
        details = []
        item_scores = anchor.get("item_scores")
        score_rules = anchor.get("score_rules")
        item_reasons = anchor.get("item_reasons")
        evidence = anchor.get("evidence")
        visual_evidence = anchor.get("visual_evidence")
        deductions = anchor.get("deductions")
        if isinstance(item_scores, list) and item_scores:
            details.append("items=" + json.dumps(item_scores, ensure_ascii=False, separators=(",", ":")))
        if isinstance(score_rules, list) and score_rules:
            details.append("rules=" + json.dumps(score_rules, ensure_ascii=False, separators=(",", ":")))
        if isinstance(item_reasons, list) and item_reasons:
            details.append("reasons=" + json.dumps(item_reasons, ensure_ascii=False, separators=(",", ":")))
        if isinstance(evidence, list) and evidence:
            details.append("evidence=" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
        if isinstance(visual_evidence, list) and visual_evidence:
            details.append(
                "visual=" + json.dumps(visual_evidence, ensure_ascii=False, separators=(",", ":"))
            )
        if isinstance(deductions, list) and deductions:
            details.append(
                "deductions=" + json.dumps(deductions, ensure_ascii=False, separators=(",", ":"))
            )
        suffix = " — " + "; ".join(([reason] if reason else []) + details) if reason or details else ""
        lines.append(f"  {name} = {score}{suffix}")
    if not lines:
        return None
    return (
        "calibration anchors (already graded in this course; hold the same standard):\n"
        + "\n".join(lines)
    )


def render_model_input(
    reports: list[dict],
    rubric: dict | None = None,
    anchors: list[dict] | None = None,
) -> str:
    has_deductions = bool(rubric) and all(
        isinstance(item, dict) and item.get("deduction_rules") for item in rubric.get("items", [])
    )
    output_instruction = (
        'Fill each JSONL row as {"id":"r0001","d":[deduction facts],"c":"overall comment",'
        '"i":[item reasons],"e":[evidence lists],"v":[visual evidence lists]}. Do not invent scores: '
        'the program calculates s from d. An empty d list is valid when no printed rule is confirmed.'
        if has_deductions else
        'Fill the paired JSONL rows as {"id":"r0001","r":[rule ids],"s":[scores],'
        '"c":"overall comment","i":[item reasons],"e":[evidence lists]}. Optional: v, t,w.'
    )
    lines = [
        "# Compact grading evidence",
        output_instruction,
        "Fill \"e\" with the page numbers each item score rests on, e.g. [\"p.3\",\"p.7\"], and \"i\" with one short sentence per item saying why it scored that, e.g. [\"结构完整\",\"分片分析正确\",\"结论过简\",\"小结偏短\",\"表述规范\"]. Without \"i\" the instructor sees a deduction but not its reason.",
        "Always fill \"e\" and \"i\" for every item, in rubric order.",
        "OCR and extracted text do not preserve page layout. Never infer alignment, readability, table placement or screenshot presentation from their serialized order. A rule marked 必须有视觉证据页 may be selected only after opening the cited source PDF page; record those pages in rubric order in \"v\" (use [] for other items), and also include them in that item's \"e\".",
        "When fixed score rules are printed, select one rule id per item in \"r\" and copy that rule's exact score into \"s\". Never improvise a score between rules. Judge each item independently from cited facts, not from the desired total or the scores of nearby reports.",
        "When evidence deductions are printed, start every item at its maximum and apply only a printed rule. Record one unique fact per deduction in d. If no listed defect is clearly confirmed, use d=[] and do not invent a shortfall merely to avoid a high score. A deduction above 2 points is allowed only for a confirmed, explicit problem with page evidence. Never charge the same fact under two items or combine rules marked mutually exclusive. If evidence is truncated or uncertain, do not record a major deduction.",
        "Every page of every report appears below. Read the \"coverage:\" line before deducting for anything missing: an omitted or truncated page is not evidence that the student left it blank.",
        "Text after an OCR marker was recognized from an image and may contain recognition errors - never deduct for a term, spelling or number that only looks wrong there.",
    ]
    rubric_line = rubric_header(rubric)
    if rubric_line:
        lines.append(rubric_line)
    anchor_block = anchor_header(anchors)
    if anchor_block:
        lines.extend(anchor_block.split("\n"))
    for report in reports:
        report_id = report.get("report_id")
        heading = f"{report_id} | {report['source_name']}" if report_id else report["source_name"]
        lines.extend([
            "",
            f"## {heading}",
            f"student: {report['student_name'] or 'unknown'} / {report['student_id'] or 'unknown'}",
            f"pages: {report['page_count']}; sections: {','.join(report.get('detected_sections', [])) or 'none'}",
            coverage_header(report),
            f"text_budget: {report.get('budget_characters', 'fixed')}; profile: {report.get('budget_profile', {}).get('profile', 'fixed')}",
            f"grading_tier: {report.get('grading_route', {}).get('tier', 'standard')}; reasons: {','.join(report.get('grading_route', {}).get('reasons', [])) or 'none'}",
            f"visual_candidates: {','.join(map(str, report['visual_candidate_pages'])) or 'none'}; visual_review: {','.join(map(str, report.get('visual_review_pages', []))) or 'none'}",
            f"ocr_resolved: {','.join(map(str, report.get('ocr_resolved_pages', []))) or 'none'}",
        ])
        if report.get("visual_assets"):
            lines.append("visual_assets: " + "; ".join(
                f"p.{asset['page']}={asset['path']}" for asset in report["visual_assets"]
            ))
        for page in report["pages"]:
            focus = ",".join(page["focus"])
            suffix = f"; focus={focus}" if focus else ""
            if page.get("ocr"):
                # The inline marker sits mid-page and can be cut by excerpt truncation,
                # so the warning is repeated where truncation can never reach it.
                suffix += "; OCR text merged (image recognition, may contain errors)"
            lines.extend(["", f"[p.{page['page']}{suffix}]", page["text"]])
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-input")
    parser.add_argument("--inspection-mode", choices=("text_only", "smart", "full_visual"), default="text_only")
    parser.add_argument("--auto-upgrade-visual", action="store_true")
    parser.add_argument("--max-chars-per-report", type=int, default=4000)
    parser.add_argument("--fixed-report-budget", action="store_true")
    # Full evidence: send every page in full. Trades a larger packet for the guarantee that no
    # conclusion or summary is cut mid-sentence, which is the one thing a text budget can get
    # wrong about a student's work.
    parser.add_argument(
        "--full-evidence",
        action="store_true",
        help="Keep every page intact instead of excerpting it; template de-duplication still applies.",
    )
    parser.add_argument("--ocr-mode", choices=("auto", "off"), default="auto")
    parser.add_argument("--ocr-executable")
    parser.add_argument("--pdftoppm-executable")
    parser.add_argument("--ocr-cache-dir")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise SystemExit(f"Report directory does not exist: {report_dir}")
    pdfs = sorted(path for path in report_dir.glob("*.pdf") if path.is_file())
    if not pdfs:
        raise SystemExit("No PDF files found directly inside the report directory")

    extracted = [extract_pages(path) for path in pdfs]
    template_profile = build_template_profile(extracted)
    removable = set(template_profile["lines"])
    output = Path(args.output).resolve()
    ocr_cache_dir = Path(args.ocr_cache_dir).resolve() if args.ocr_cache_dir else output.parent / "ocr-cache"
    visual_assets_dir = output.parent / "visual-assets"
    if visual_assets_dir.exists():
        # Extracted crops are a derived cache: extract_visual_assets() overwrites each file it
        # needs, so a stale directory is only wasted space. A hostile delete is worse than a
        # stale one, and on Windows rmtree may be wrapped by a fail-closed trash shim that
        # raises when the drive has no usable Recycle Bin. Try to clear it, then carry on.
        try:
            shutil.rmtree(visual_assets_dir)
        except OSError as error:
            print(json.dumps({
                "warning": "visual_assets_not_cleared",
                "path": str(visual_assets_dir),
                "detail": str(error),
                "impact": "stale page crops may remain; the run continues and overwrites what it needs",
            }, ensure_ascii=False))
    reports = []
    for path, pages in zip(pdfs, extracted):
        ocr_result = ocr_candidate_pages(
            path,
            pages,
            removable,
            args.ocr_mode,
            ocr_cache_dir,
            args.ocr_executable,
            args.pdftoppm_executable,
        )
        grading_route = classify_grading_route(pages, removable, args.inspection_mode, ocr_result)
        if args.full_evidence:
            # No budget arithmetic: the ceiling is the report's own length, so every page
            # reaches the model in full and the profile records why nothing was shortened.
            report_budget = sum(len(page) for page in (page_text(p) for p in pages))
            budget_profile = {
                "profile": "full_evidence",
                "ceiling": report_budget,
                "selected": report_budget,
                "truncated": False,
            }
        elif args.fixed_report_budget:
            report_budget = max(1000, args.max_chars_per_report)
            budget_profile = {"profile": "fixed", "selected": report_budget, "ceiling": report_budget}
        else:
            report_budget, budget_profile = dynamic_text_budget(pages, max(1000, args.max_chars_per_report))
            report_budget, route_budget = apply_route_budget(
                report_budget,
                max(1000, args.max_chars_per_report),
                grading_route,
            )
            budget_profile.update(route_budget)
            budget_profile["selected"] = report_budget
        if args.inspection_mode == "full_visual":
            review_pages = list(range(1, len(pages) + 1))
        elif args.inspection_mode == "smart" or args.auto_upgrade_visual:
            review_pages = select_visual_review_pages(ocr_result)
        else:
            review_pages = []
        visual_assets = extract_visual_assets(path, review_pages, visual_assets_dir)
        reports.append(compact_report(
            path,
            pages,
            removable,
            report_budget,
            args.inspection_mode,
            args.auto_upgrade_visual,
            ocr_result,
            budget_profile,
            grading_route,
            visual_assets,
            args.full_evidence,
        ))
    for index, report in enumerate(reports, start=1):
        report["report_id"] = f"r{index:04d}"
    model_input = render_model_input(reports)
    stored_reports = reports
    if args.model_input:
        model_input_path = Path(args.model_input).resolve()
        model_input_path.parent.mkdir(parents=True, exist_ok=True)
        model_input_path.write_text(model_input, encoding="utf-8")
        stored_reports = [
            {key: value for key, value in report.items() if key != "pages"}
            for report in reports
        ]
    packet = {
        "packet_version": "1.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_dir": str(report_dir),
        "inspection_mode": args.inspection_mode,
        "auto_upgrade_visual": bool(args.auto_upgrade_visual),
        "template_fingerprint": template_profile.get("fingerprint", ""),
        "reports": stored_reports,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        packet,
        ensure_ascii=False,
        indent=2 if args.pretty else None,
        separators=None if args.pretty else (",", ":"),
    )
    output.write_text(serialized, encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "reports": len(reports),
        "source_characters": sum(report["extracted_characters"] for report in reports),
        "packet_characters": len(serialized),
        "model_input_characters": len(model_input),
        "visual_candidates": sum(len(report["visual_candidate_pages"]) for report in reports),
        "visual_review_pages": sum(len(report["visual_review_pages"]) for report in reports),
        "ocr_resolved_pages": sum(len(report["ocr_resolved_pages"]) for report in reports),
        "visual_assets": sum(len(report.get("visual_assets", [])) for report in reports),
        "grading_tiers": {
            tier: sum(1 for report in reports if report.get("grading_route", {}).get("tier") == tier)
            for tier in ("fast", "standard", "detailed")
        },
        "template_lines": len(template_profile.get("lines", [])),
        "selected_budgets": [report["budget_characters"] for report in reports],
    }))


if __name__ == "__main__":
    main()
