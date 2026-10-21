#!/usr/bin/env python3
"""Register this skill with the agent hosts installed on this machine.

Why it is a script and not instructions: each host reads its own directory
(``~/.agents/skills`` for DSH and Codex, ``~/.claude/skills`` for Claude Code, and so on), the
list grows, and a skill installed into the wrong one is simply invisible. The roots live in
``skill.json`` so this script, ``verify_host_registration.mjs`` and the documentation cannot
disagree about them.

The link is a symlink by default, so the checked-out skill stays the single source of truth;
``--copy`` exists for machines where a link is not wanted or not possible. Nothing is
overwritten: an existing entry is only replaced when it is a link to this same skill, or a copy
carrying this skill's marker file.

    python3 scripts/install_skill.py --dry-run            # what would happen
    python3 scripts/install_skill.py --host auto          # register with what is installed
    python3 scripts/install_skill.py --host dsh --copy     # copy instead of link
    python3 scripts/install_skill.py --uninstall          # remove the entries it created
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# One source of truth for what must never travel. This list used to be written out again here and
# had already drifted from the packaging one; a --copy install is the same distribution decision as
# building an archive, so it has to exclude the same things -- including a host's .workbuddy
# workspace, which holds run logs with real student names and ID numbers.
from package_skill import EXCLUDE_DIRS, EXCLUDE_FILES, EXCLUDE_SUFFIXES  # noqa: E402

COPY_IGNORE = sorted(EXCLUDE_DIRS | EXCLUDE_FILES) + [f"*{suffix}" for suffix in EXCLUDE_SUFFIXES]

SKILL_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = SKILL_ROOT / "skill.json"
MARKER_NAME = ".experiment-report-grader-install.json"

# A shared root that more than one host reads; registering here is the single cheapest action
# that makes the skill visible to both DSH and Codex.
DEFAULT_HOSTS = ("agents",)


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"skill.json not found at {path}")
    except ValueError as error:
        raise SystemExit(f"skill.json is not valid JSON: {error}")


def expand(path: str, home: Path) -> Path:
    """Expand a leading ``~`` against an explicit home, so tests can use a throwaway one."""
    if path in ("~", ""):
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path).expanduser()


def host_entry(manifest: dict, host: str) -> dict:
    hosts = manifest.get("hosts", {})
    if host not in hosts:
        raise SystemExit(f"Unknown host '{host}'. Known hosts: {', '.join(sorted(hosts))}")
    return hosts[host]


def detect_hosts(manifest: dict, home: Path) -> list[str]:
    """Hosts whose own directory already exists, plus the shared root which is always tried."""
    detected = list(DEFAULT_HOSTS)
    for host, entry in sorted(manifest.get("hosts", {}).items()):
        if host in detected:
            continue
        for hint in entry.get("detect", []):
            if expand(hint, home).exists():
                detected.append(host)
                break
    return detected


def target_roots(
    manifest: dict,
    hosts: list[str],
    home: Path,
    project: Path | None,
    scope: str,
) -> list[tuple[str, Path, str]]:
    """Resolve (host, root, scope) triples, de-duplicated by path, order preserved."""
    seen: set[Path] = set()
    result: list[tuple[str, Path, str]] = []
    for host in hosts:
        entry = host_entry(manifest, host)
        if scope == "project":
            keys = ("project",)
        elif scope == "both":
            keys = ("user", "project")
        else:
            keys = ("user",)
        for key in keys:
            for relative in entry.get(key, []):
                if key == "project":
                    if project is None:
                        continue
                    root = project / relative.lstrip("./")
                else:
                    root = expand(relative, home)
                if root in seen:
                    continue
                seen.add(root)
                result.append((host, root, key))
    return result


def marker_payload() -> dict:
    return {"skill": "experiment-report-grader", "version": _manifest_version(), "source": str(SKILL_ROOT)}


def _manifest_version() -> str:
    try:
        return str(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("skill_version", ""))
    except (OSError, ValueError):
        return ""


def describe_existing(entry: Path) -> tuple[str, str]:
    """Classify an existing entry: 'same', 'copy-of-this-skill', 'other-link', 'other-dir'."""
    if entry.is_symlink():
        target = Path(os.path.realpath(entry))
        return ("same" if target == SKILL_ROOT.resolve() else "other-link", str(target))
    if entry.is_dir():
        marker = entry / MARKER_NAME
        if marker.is_file():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                return "other-dir", "unreadable marker"
            if payload.get("skill") == "experiment-report-grader":
                return "copy-of-this-skill", str(entry)
        return "other-dir", str(entry)
    return "other-dir", str(entry)


def link_or_copy(root: Path, entry: Path, mode: str, dry_run: bool) -> str:
    """Create the entry. Returns a short description of what was done."""
    if dry_run:
        return f"would create {'copy' if mode == 'copy' else 'link'} -> {entry}"
    root.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        shutil.copytree(
            SKILL_ROOT,
            entry,
            dirs_exist_ok=False,
            ignore=shutil.ignore_patterns(*COPY_IGNORE),
        )
        (entry / MARKER_NAME).write_text(
            json.dumps(marker_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"copied -> {entry}"
    try:
        entry.symlink_to(SKILL_ROOT, target_is_directory=True)
        return f"linked -> {entry}"
    except OSError as error:
        if os.name != "nt":
            raise SystemExit(f"Cannot create symlink at {entry}: {error}. Re-run with --copy.") from error
        # Windows without developer mode: a junction needs no elevation.
        # `mklink` answers in the console's OEM code page (cp936 on a Chinese Windows), so
        # decoding its bytes as UTF-8 — which is what `text=True` does when PYTHONUTF8=1,
        # exactly the setting SKILL.md asks for so Chinese output is not mangled — raises
        # UnicodeDecodeError inside subprocess' reader thread and hides the real failure.
        # Capture bytes and decode with the locale encoding, tolerating anything unexpected.
        import locale
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(entry), str(SKILL_ROOT)],
            capture_output=True,
        )
        if completed.returncode != 0:
            encoding = locale.getpreferredencoding(False) or "utf-8"
            output = (completed.stderr or completed.stdout or b"").decode(
                encoding, errors="replace"
            ).strip()
            raise SystemExit(f"Cannot create junction at {entry}: {output}")
        return f"junction -> {entry}"


def install(args: argparse.Namespace) -> dict:
    manifest = load_manifest()
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    hosts = list(args.host) if args.host else []
    if not hosts or "auto" in hosts:
        hosts = detect_hosts(manifest, home)
    project = Path(args.project).expanduser().resolve() if args.project else None
    roots = target_roots(manifest, hosts, home, project, args.scope)

    actions: list[dict] = []
    for host, root, scope in roots:
        entry = root / manifest["name"]
        if entry.exists() or entry.is_symlink():
            kind, detail = describe_existing(entry)
            if kind in ("same", "copy-of-this-skill"):
                actions.append({"host": host, "scope": scope, "root": str(root), "status": "already", "detail": detail})
                continue
            if not args.force:
                actions.append({
                    "host": host,
                    "scope": scope,
                    "root": str(root),
                    "status": "skipped",
                    "detail": f"{entry} exists and is not this skill ({kind}: {detail}); pass --force to replace",
                })
                continue
            if not args.dry_run:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
        actions.append({
            "host": host,
            "scope": scope,
            "root": str(root),
            "status": "dry-run" if args.dry_run else "created",
            "detail": link_or_copy(root, entry, args.mode, args.dry_run),
        })
    return {"ok": True, "skill": manifest["name"], "version": manifest.get("skill_version"),
            "home": str(home), "hosts": hosts, "dry_run": bool(args.dry_run), "actions": actions}


def uninstall(args: argparse.Namespace) -> dict:
    manifest = load_manifest()
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    hosts = list(args.host) if args.host else []
    if not hosts or "auto" in hosts:
        hosts = detect_hosts(manifest, home)
    project = Path(args.project).expanduser().resolve() if args.project else None
    roots = target_roots(manifest, hosts, home, project, args.scope)

    actions: list[dict] = []
    for host, root, scope in roots:
        entry = root / manifest["name"]
        if not entry.exists() and not entry.is_symlink():
            actions.append({"host": host, "scope": scope, "root": str(root), "status": "absent", "detail": ""})
            continue
        kind, detail = describe_existing(entry)
        if kind not in ("same", "copy-of-this-skill") and not args.force:
            actions.append({
                "host": host,
                "scope": scope,
                "root": str(root),
                "status": "skipped",
                "detail": f"{entry} is not this skill ({kind}: {detail}); pass --force to remove anyway",
            })
            continue
        if not args.dry_run:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        actions.append({
            "host": host,
            "scope": scope,
            "root": str(root),
            "status": "dry-run" if args.dry_run else "removed",
            "detail": str(entry),
        })
    return {"ok": True, "skill": manifest["name"], "home": str(home), "dry_run": bool(args.dry_run), "actions": actions}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", nargs="*", metavar="HOST",
                        help="agents | dsh | codex | workbuddy | codebuddy | claude | auto (default: auto)")
    parser.add_argument("--scope", choices=("user", "project", "both"), default="user")
    parser.add_argument("--project", help="project directory for --scope project/both")
    parser.add_argument("--home", help="home directory to install into (default: the real one)")
    parser.add_argument("--copy", dest="mode", action="store_const", const="copy", default="link",
                        help="copy the skill instead of symlinking it")
    parser.add_argument("--force", action="store_true", help="replace an entry that is not this skill")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen")
    parser.add_argument("--uninstall", action="store_true", help="remove the entries instead")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = uninstall(args) if args.uninstall else install(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['skill']} {result.get('version', '')} -> home {result['home']}")
        for action in result["actions"]:
            print(f"  [{action['status']:<8}] {action['host']:<10} {action['scope']:<8} {action['detail']}")
    if not result["actions"]:
        print("  (nothing to do: no host roots matched)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
