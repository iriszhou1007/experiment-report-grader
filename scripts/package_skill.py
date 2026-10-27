#!/usr/bin/env python3
"""Pack the skill into a movable archive, and verify the archive it produced.

What makes this more than a zip command: the exclusions are the difference between a
distributable skill and a leak. Student reports, the scratch directory, generated OCR binaries
and the machine-local ``config/local.json`` must not travel, and ``--verify`` extracts the
archive into a temporary directory and proves the result still passes ``doctor`` — so a broken
package is caught here instead of on the machine it was copied to.

    python3 scripts/package_skill.py --out dist/
    python3 scripts/package_skill.py --out dist/ --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

# Excluded because they are either somebody's private data, machine-specific, or regenerated.
# `实验报告_测试样本` is real student work kept for regression runs; `config/local.json` holds the
# host's own absolute paths; `scripts/ocr-shims/` is generated per machine and platform-specific.
#
# `.workbuddy/` is a host's own workspace. It was being shipped, and it carried three run logs
# holding 61 students' names and ID numbers -- the exclusion list, not .gitignore, is what decides
# this, because packaging walks the working tree and those logs are gitignored but still on disk.
# The whole directory goes rather than a list of its current files: a whitelist would leak again
# the first time somebody drops another log or note in there.
EXCLUDE_DIRS = {
    ".git", "tmp", "__pycache__", "dist", "node_modules", "ocr-shims",
    "实验报告_测试样本", ".workbuddy", "output",
}
EXCLUDE_FILES = {".DS_Store", "local.json"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
# The teacher's signature is per-machine configuration, so a bundle-local one stays behind too:
# preferring it locally is convenient, distributing somebody's signature is not a decision a
# packaging script gets to make on their behalf.
EXCLUDE_PATTERNS = ("assets/signature/*.pdf",)

# Kept explicit rather than derived from the file listing: a packaging change that silently drops
# a script is exactly the failure this list is here to catch (it caught bootstrap.py being absent
# from the first archive built in the same session that created it).
REQUIRED_IN_ARCHIVE = (
    "SKILL.md",
    "skill.json",
    "README.md",
    "scripts/batch_pipeline.py",
    "scripts/check_stable_version.py",
    "scripts/submission_preflight.py",
    "scripts/bootstrap.py",
    "scripts/export_scores.py",
    "scripts/build_review_html.py",
    "scripts/resolve_env.py",
    "scripts/install_skill.py",
    "scripts/package_skill.py",
    "scripts/verify_host_registration.mjs",
    "scripts/review_server.mjs",
    "scripts/materialize_reviews.py",
    "scripts/scoring_policy.py",
    "scripts/test_score_reproducibility.py",
    "scripts/test_windows_ocr.py",
    "scripts/insert_feedback_page.py",
    "scripts/review_marking.py",
    "scripts/approve_batch.py",
    "scripts/setup_local_ocr.sh",
    "assets/default-rubric.json",
    "assets/rubrics/experiment-1-web-http.json",
    "assets/rubrics/experiment-2-dns.json",
    "assets/guides/experiment-1-web-http.pdf",
    "assets/guides/experiment-2-dns.html",
    "assets/review-panel-template.html",
    "config/defaults.example.json",
    "references/portability.md",
    "references/stable-version-check.md",
    "references/experiment-1-web-http.md",
    "references/experiment-2-dns.md",
    "agents/openai.yaml",
)


def manifest() -> dict:
    return json.loads((SKILL_ROOT / "skill.json").read_text(encoding="utf-8"))


def included_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(SKILL_ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(SKILL_ROOT)
        if any(part in EXCLUDE_DIRS for part in relative.parts[:-1]):
            continue
        if relative.name in EXCLUDE_FILES or relative.name.endswith(EXCLUDE_SUFFIXES):
            continue
        if any(relative.match(pattern) for pattern in EXCLUDE_PATTERNS):
            continue
        files.append(relative)
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_tar(out_dir: Path, prefix: str, files: list[Path]) -> Path:
    target = out_dir / f"{prefix}.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for relative in files:
            archive.add(SKILL_ROOT / relative, arcname=f"{prefix}/{relative.as_posix()}")
    return target


def write_zip(out_dir: Path, prefix: str, files: list[Path]) -> Path:
    target = out_dir / f"{prefix}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(SKILL_ROOT / relative, arcname=f"{prefix}/{relative.as_posix()}")
    return target


def verify_archive(archive: Path, expect_absent: list[str]) -> dict:
    """Extract into a temporary directory and check the package can stand on its own.

    The extracted copy is exercised with an empty ``HOME`` and no ``config/local.json``, because
    otherwise the machine that *built* the archive answers on its behalf: an earlier revision ran
    this check with the real home and reported a healthy package, while the signature template was
    in fact being resolved from a path outside the archive that only exists on the build machine.
    A verification that can be satisfied by the builder's own filesystem verifies nothing.
    """
    result: dict = {"archive": str(archive), "checks": []}

    def check(name: str, ok: bool, detail: str) -> None:
        result["checks"].append({"check": name, "ok": ok, "detail": detail})

    with tempfile.TemporaryDirectory(prefix="experiment-report-grader-verify-") as temp:
        temp_root = Path(temp)
        target = temp_root / "extracted"
        target.mkdir()
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(target)
        else:
            with tarfile.open(archive) as handle:
                handle.extractall(target)
        roots = [entry for entry in target.iterdir() if entry.is_dir()]
        check("single top-level directory", len(roots) == 1, ", ".join(entry.name for entry in roots))
        root = roots[0] if roots else target

        missing = [name for name in REQUIRED_IN_ARCHIVE if not (root / name).exists()]
        check("required files present", not missing, "all present" if not missing else f"missing: {missing}")

        leaked = [name for name in expect_absent if (root / name).exists()]
        check("excluded content absent", not leaked, "nothing excluded travelled" if not leaked else f"leaked: {leaked}")

        extras = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and (path.name.endswith(EXCLUDE_SUFFIXES) or path.name in EXCLUDE_FILES)
        ]
        check("no generated or private files", not extras, "clean" if not extras else f"found: {extras[:5]}")

        # A path from the build machine is not a crash, it is silent misdirection: the previous
        # revision shipped an absolute personal path as a resolution fallback, which pointed an
        # unpacked copy on someone else's machine at a directory that does not exist there.
        home_pattern = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+")
        home_hits: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() in {".pdf", ".png", ".ttf", ".otf", ".xlsx", ".wasm"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if home_pattern.search(line):
                    home_hits.append(f"{path.relative_to(root)}:{number}")
        check(
            "no machine-specific home paths shipped",
            not home_hits,
            "clean" if not home_hits else f"found: {home_hits[:6]}",
        )

        # A neutral machine: empty home, no config file, no EXPERIMENT_REPORT_GRADER_* overrides.
        home = temp_root / "neutral-home"
        home.mkdir()
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("EXPERIMENT_REPORT_GRADER_")
        }
        # HOME alone is not enough isolation: desktop hosts commonly prepend their own cached
        # runtimes under /Users/<builder>/... to PATH.  Leaving those entries in place lets
        # shutil.which() answer the extracted package from the build machine and produces the
        # exact false green/false red this check is meant to prevent.
        personal_path = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:\\Users\\)")
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry and not personal_path.match(entry)
        )
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "EXPERIMENT_REPORT_GRADER_CONFIG": str(home / "absent-local.json"),
        })

        resolved = subprocess.run(
            [sys.executable, str(root / "scripts" / "resolve_env.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            env=env,
        )
        try:
            report = {item["check"]: item for item in json.loads(resolved.stdout)}
        except (ValueError, KeyError, TypeError):
            report = {}
        result["neutral_resolution"] = report
        # The signature must not resolve on a machine that has nothing configured: it is the item
        # that leaked last time, and a package that answers it here is answering from outside.
        signature = report.get("signature template", {})
        check(
            "signature unresolved on a neutral machine",
            not signature.get("ok"),
            "correctly unresolved" if not signature.get("ok")
            else f"resolved to {signature.get('value')} from outside the package",
        )
        # Fonts and OCR helpers may legitimately come from the operating system, so they are
        # allowed to resolve — but not out of a personal home directory, which is the shape the
        # shipped-fallback bug took.
        personal = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:\\Users\\)")
        from_home = {
            name: report[name]["value"]
            for name in ("score font", "tesseract", "pdftoppm")
            if report.get(name, {}).get("ok") and personal.match(str(report[name].get("value") or ""))
        }
        check(
            "no per-machine asset resolved from a personal home path",
            not from_home,
            "clean" if not from_home else f"resolved from a home directory: {from_home}",
        )

        doctor = subprocess.run(
            [sys.executable, str(root / "scripts" / "batch_pipeline.py"), "doctor"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            env=env,
        )
        payload = doctor.stdout.strip().splitlines()[-1] if doctor.stdout.strip() else ""
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = {"ok": False, "missing": ["doctor did not report"]}
        result["extracted_doctor"] = parsed
        result["required_missing"] = missing

        # Everything doctor calls missing has to be a documented per-machine item; anything else
        # means the archive is incomplete and would fail later, in the middle of a batch.
        allowed = {
            "teacher signature", "score font", "tesseract", "pdftoppm", "node", "ocr engine",
        }
        unexpected = sorted(set(parsed.get("missing", [])) - allowed)
        check(
            "fresh machine reports only per-machine gaps",
            not unexpected,
            f"doctor missing: {parsed.get('missing')}" if not unexpected
            else f"unexpected gaps: {unexpected}",
        )
        # Doctor exits non-zero on a fresh machine by design (per-machine assets are unresolved),
        # so the check is that it ran and reported, not that it was happy.
        check(
            "extracted skill runs doctor",
            bool(parsed.get("ok") is not None and "missing" in parsed),
            f"exit {doctor.returncode}; reported: {parsed.get('missing')}",
        )
    result["ok"] = all(item["ok"] for item in result["checks"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(SKILL_ROOT / "dist"), help="output directory (default: dist/)")
    parser.add_argument("--formats", nargs="*", default=["tar.gz", "zip"], choices=["tar.gz", "zip"])
    parser.add_argument("--verify", action="store_true", help="extract and check the archive")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = manifest()
    prefix = f"{config['name']}-{config.get('skill_version', '0')}"
    files = included_files()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    archives: list[dict] = []
    for fmt in args.formats:
        writer = write_tar if fmt == "tar.gz" else write_zip
        target = writer(out_dir, prefix, files)
        archives.append({"path": str(target), "bytes": target.stat().st_size, "sha256": sha256(target)})

    # A recipient who installs from a zip has no git history to compare against, so the release
    # carries its own checksums: the file to compare against after copying, and the one line to
    # quote when reporting what was actually installed.
    checksums = out_dir / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{archive['sha256']}  {Path(archive['path']).name}\n" for archive in archives),
        encoding="utf-8",
    )

    report: dict = {
        "ok": True,
        "skill": config["name"],
        "skill_version": config.get("skill_version"),
        "pipeline_version": config.get("pipeline_version"),
        "files": len(files),
        "archives": archives,
        "checksums": str(checksums),
    }
    if args.verify:
        report["verification"] = verify_archive(
            Path(archives[0]["path"]), sorted(EXCLUDE_DIRS | EXCLUDE_FILES)
        )
        report["ok"] = report["verification"]["ok"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['skill']} {report['skill_version']} (pipeline {report['pipeline_version']}), {len(files)} files")
        for archive in archives:
            print(f"  {archive['path']}  {archive['bytes'] / 1024:.0f} KiB  sha256 {archive['sha256'][:16]}…")
        print(f"  {checksums}")
        if args.verify:
            for item in report["verification"]["checks"]:
                print(f"  [{'ok ' if item['ok'] else 'FAIL'}] {item['check']}: {item['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
