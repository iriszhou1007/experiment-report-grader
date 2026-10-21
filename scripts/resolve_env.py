"""Resolve every external path this skill needs, in one place and in one order.

Why this module exists: the write-back template, the handwriting face, the interpreter, Node and
the OCR helpers used to be absolute paths to one developer's machine, so a migrated copy failed
late — often after a whole batch had been reviewed. Everything here resolves through the same
order and reports *which layer answered*, because a path that resolves by luck today is a support
question tomorrow.

Resolution order, first hit wins:

1. an explicit CLI flag
2. an ``EXPERIMENT_REPORT_GRADER_*`` environment variable
3. ``config/local.json`` inside the skill (or ``$EXPERIMENT_REPORT_GRADER_CONFIG``)
4. a bundled asset under ``assets/``
5. platform probes (common install locations)
6. nothing — callers decide whether that is fatal or merely a degradation

Standard library only: ``batch_pipeline doctor`` imports this before it knows whether the heavy
third-party dependencies are importable, so importing them here would make the diagnosis fail
for the reason it is trying to diagnose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

ENV_PREFIX = "EXPERIMENT_REPORT_GRADER_"

# The teacher's signature template is *not* bundled. It is the teacher's own ink, so shipping it
# inside a distributable archive is a privacy decision nobody made, and a signature belongs to a
# person rather than to a skill. An earlier revision kept the author's absolute path here as a
# last-resort default, which made a freshly unpacked copy look healthy on the author's machine
# while resolving to a file outside the package — a green light that meant nothing on a new
# machine. It is now resolved only from the documented layers and reported unresolved otherwise.
# Configure it with any of:
#
#   --signature-pdf /path/to/signature.pdf  (or a transparent PNG)
#   EXPERIMENT_REPORT_GRADER_SIGNATURE_FILE=/path/to/signature.png
#   EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF=/path/to/signature.pdf  (legacy variable; also accepts PNG)
#   python3 scripts/bootstrap.py --write-config       # records the current value in config/local.json
#   <skill>/assets/signature/signature.pdf            # drop it in the bundle for a one-machine setup
#
# A config/local.json entry looks like this (documentation, not a default):
#   "signature_file": "~/Documents/教学/签名.png"
SIGNATURE_BUNDLE_RELATIVE = "assets/signature/signature.pdf"
SIGNATURE_BUNDLE_DIR = "assets/signature"

# Handwriting faces, in preference order per platform. Bradley Hand Bold is the face the
# existing write-backs used, so it wins wherever it exists.
SCORE_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf"),
    Path("/Library/Fonts/Bradley Hand Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Bradley Hand.ttf"),
    Path("C:/Windows/Fonts/BRADHITC.TTF"),
    Path("C:/Windows/Fonts/segoepr.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)


@dataclass(frozen=True)
class Resolution:
    """One resolved path plus the layer that produced it."""

    value: Path | None
    source: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict:
        return {
            "value": None if self.value is None else str(self.value),
            "source": self.source,
            "detail": self.detail,
            "ok": self.ok,
        }


def env_name(key: str) -> str:
    return f"{ENV_PREFIX}{key.upper()}"


def config_path() -> Path:
    override = os.environ.get(env_name("config"))
    return Path(override).expanduser() if override else SKILL_ROOT / "config" / "local.json"


def load_local_config() -> dict:
    """Read ``config/local.json``; a malformed file is reported, never silently ignored."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"config/local.json is not readable JSON: {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"config/local.json must contain a JSON object: {path}")
    return data


def _from_config(config: dict, key: str) -> Path | None:
    value = config.get(key)
    if not value:
        return None
    return Path(str(value)).expanduser()


def resolve(
    *,
    explicit: str | Path | None,
    key: str,
    bundle_relative: str | None = None,
    candidates: tuple[Path, ...] = (),
    config: dict | None = None,
    require_exists: bool = True,
) -> Resolution:
    """Apply the documented order for one logical path."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists() or not require_exists:
            return Resolution(path, "flag")
        return Resolution(None, "flag", f"{path} does not exist")

    variable = env_name(key)
    from_env = os.environ.get(variable)
    if from_env:
        path = Path(from_env).expanduser()
        if path.exists() or not require_exists:
            return Resolution(path, f"env:{variable}")
        return Resolution(None, f"env:{variable}", f"{path} does not exist")

    settings = load_local_config() if config is None else config
    from_config = _from_config(settings, key)
    from_config_note = ""
    if from_config is not None:
        if from_config.exists() or not require_exists:
            return Resolution(from_config, "config:local.json")
        # A config entry pointing at a missing path must not disable a working probe. Observed on
        # this very skill: a config/local.json carried over from a Windows machine pinned
        # pdftoppm_executable to an I:\ path, which made doctor report "not found" while a
        # perfectly good poppler sat in the Codex runtime. The reason is kept for the report and
        # resolution continues; flags and environment variables stay terminal because those are
        # deliberate per-invocation instructions rather than persisted machine state.
        from_config_note = f"config:local.json \u2192 {from_config} does not exist"
        from_config = None

    if bundle_relative:
        bundled = SKILL_ROOT / bundle_relative
        if bundled.exists():
            return Resolution(bundled, f"bundle:{bundle_relative}", from_config_note)

    for candidate in candidates:
        if candidate.exists():
            return Resolution(candidate, "probe", from_config_note)

    return Resolution(None, "none", from_config_note or "not found in any layer")


def signature_candidates() -> tuple[Path, ...]:
    """Bundle-local signature files.

    Only the bundle is probed. A machine-specific signature is a *configuration* item, not a
    guess: pointing at a path outside the package by default is what made the previous revision
    report a healthy new machine that could not actually write a report back.
    """
    found = [SKILL_ROOT / SIGNATURE_BUNDLE_RELATIVE]
    bundle_dir = SKILL_ROOT / SIGNATURE_BUNDLE_DIR
    if bundle_dir.is_dir():
        found.extend(sorted(path for path in bundle_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"))
        found.extend(sorted(path for path in bundle_dir.iterdir() if path.is_file() and path.suffix.lower() == ".png"))
    return tuple(found)


def resolve_signature_pdf(explicit: str | Path | None = None, config: dict | None = None) -> Resolution:
    """Resolve the teacher's PDF or transparent PNG signature asset (PDF config remains supported)."""
    image_asset = resolve(
        explicit=explicit,
        key="signature_file",
        config=config,
    )
    if image_asset.ok or image_asset.source in {"flag", f"env:{ENV_PREFIX}SIGNATURE_FILE"}:
        return image_asset
    settings = load_local_config() if config is None else config
    if settings.get("signature_file"):
        return image_asset
    return resolve(
        explicit=None,
        key="signature_pdf",
        bundle_relative=SIGNATURE_BUNDLE_RELATIVE,
        candidates=signature_candidates(),
        config=config,
    )


def resolve_score_font(explicit: str | Path | None = None, config: dict | None = None) -> Resolution:
    """Handwriting face for the score stamped into 成绩评定."""
    return resolve(
        explicit=explicit,
        key="score_font",
        bundle_relative="assets/fonts/score.ttf",
        candidates=SCORE_FONT_CANDIDATES,
        config=config,
    )


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found).resolve() if found else None


def windows_fixed_drives() -> tuple[Path, ...]:
    """Enumerate fixed local volumes, never mapped/network or removable drives.

    API contracts: https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getlogicaldrives
    and https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getdrivetypew.
    """
    if sys.platform != "win32":
        return ()
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLogicalDrives.restype = ctypes.c_uint32
    kernel.GetLogicalDrives.argtypes = []
    kernel.GetDriveTypeW.restype = ctypes.c_uint
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise OSError(ctypes.get_last_error(), "GetLogicalDrives failed")
    return tuple(Path(f"{chr(65 + index)}:/") for index in range(26)
                 if mask & (1 << index) and kernel.GetDriveTypeW(f"{chr(65 + index)}:\\") == 3)


def search_windows_ocr(
    name: str, roots: tuple[Path, ...], *, max_depth: int = 4,
    max_entries: int = 6000, seconds_per_drive: float = 2.0,
) -> tuple[list[Path], str]:
    """Bounded, read-only search of installation trees; roots are injectable for tests.

    Each drive has its own budget so a large C: cannot starve D:/E:/I:. No root-wide
    recursion, directory links/junctions, network shares, or user-document search.
    Time is a cooperative budget; an individual filesystem call can still block.
    """
    filename = f"{name}.exe"
    bases = ("Program Files", "Program Files (x86)", "Programs", "tools", "apps",
             "software", "软件", "软件安装", "Anaconda3", "Miniconda3",
             "ProgramData/chocolatey", "scoop/apps")
    found: list[Path] = []
    notes = []
    for root in roots:
        started = time.monotonic()
        entries = 0
        limited = False
        denied = False
        queue = deque((root / base, 0) for base in bases)
        # Home-relative install trees only when they lie on this fixed volume.
        for relative in ("AppData/Local/Programs", "scoop/apps", "anaconda3", "miniconda3"):
            home_base = Path.home() / relative
            if home_base.is_relative_to(root):
                queue.append((home_base, 0))
        # Portable releases are often extracted directly to D:\poppler-<version>.
        queue.append((root, -1))
        seen: set[Path] = set()
        while queue:
            if entries >= max_entries or time.monotonic() - started >= seconds_per_drive:
                limited = True
                break
            directory, depth = queue.popleft()
            if directory in seen:
                continue
            seen.add(directory)
            try:
                # Do not follow symlinks or junctions out of an installation tree.
                info = directory.lstat()
                if directory.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                with os.scandir(directory) as children:
                    for child in children:
                        entries += 1
                        if entries > max_entries or time.monotonic() - started >= seconds_per_drive:
                            limited = True
                            break
                        if child.name.casefold() == filename and child.is_file(follow_symlinks=False):
                            found.append(Path(child.path))
                        if depth < max_depth and child.is_dir(follow_symlinks=False):
                            if depth >= 0 or child.name.casefold().startswith(("tesseract", "poppler")):
                                queue.append((Path(child.path), depth + 1))
            except FileNotFoundError:
                continue
            except OSError:
                denied = True
        notes.append(f"{root} ({entries} entries"
                     + (", budget reached" if limited else "")
                     + (", some directories inaccessible" if denied else "") + ")")
    return sorted(set(found), key=lambda path: str(path).casefold()), (
        "bounded common-install-directory search: " + ("; ".join(notes) or "no fixed volumes reported")
    )


@lru_cache(maxsize=2)
def windows_ocr_search(name: str) -> tuple[list[Path], str]:
    try:
        return search_windows_ocr(name, windows_fixed_drives())
    except (OSError, AttributeError) as error:
        return [], f"local drive enumeration failed: {error}"


@lru_cache(maxsize=64)
def _windows_ocr_probe(name: str, path: str, stamp: tuple, tessdata_prefix: str) -> str:
    """Return an actionable error or empty string; cache only within this process/file version."""
    def run(arguments: list[str]) -> str:
        result = subprocess.run(
            [path, *arguments], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        output = result.stdout + "\n" + result.stderr
        if result.returncode:
            raise ValueError(f"exit {result.returncode}: {output.strip()[:250]}")
        return output
    try:
        version = run(["--version"] if name == "tesseract" else ["-v"])
        if name not in version.casefold():
            return f"{path}: version output does not identify {name}"
        if name == "tesseract":
            languages = {line.strip() for line in run(["--list-langs"]).splitlines()}
            missing = {"chi_sim", "eng"} - languages
            if missing:
                return (f"{path}: missing language data {', '.join(sorted(missing))}; "
                        "check tessdata and TESSDATA_PREFIX")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return f"{path}: cannot run OCR helper ({error})"
    return ""


def validate_windows_ocr(name: str, path: Path) -> str:
    try:
        info = path.stat()
        if not path.is_file():
            return f"{path}: not a file"
    except OSError as error:
        return f"{path}: {error}"
    # Include language-file timestamps: adding chi_sim must invalidate a failed probe.
    data_dirs = [path.parent / "tessdata"]
    if os.environ.get("TESSDATA_PREFIX"):
        data_dirs.append(Path(os.environ["TESSDATA_PREFIX"]))
    stamps = [(info.st_size, info.st_mtime_ns)]
    for directory in data_dirs:
        for language in ("chi_sim", "eng"):
            try:
                data = (directory / f"{language}.traineddata").stat()
                stamps.append((data.st_size, data.st_mtime_ns))
            except OSError:
                stamps.append(None)
    return _windows_ocr_probe(name, str(path), tuple(stamps), os.environ.get("TESSDATA_PREFIX", ""))


def _resolve_windows_ocr(name: str, explicit, config, candidates: list[Path]) -> Resolution:
    key = "ocr_executable" if name == "tesseract" else "pdftoppm_executable"
    variable = env_name(key)
    # Explicit choices are terminal even when invalid: do not silently use another install.
    for source, value in (("flag", explicit), (f"env:{variable}", os.environ.get(variable))):
        if value:
            path = Path(value).expanduser()
            error = validate_windows_ocr(name, path)
            return Resolution(None if error else path, source, error)
    settings = load_local_config() if config is None else config
    saved = _from_config(settings, key)
    options = ([(saved, "config:local.json")] if saved else []) + [(path, "probe") for path in candidates]
    errors: list[str] = []
    seen: set[Path] = set()

    def check(options):
        for path, source in options:
            if path in seen:
                continue
            seen.add(path)
            try:
                exists = path.is_file()
            except OSError as error:
                errors.append(f"{path}: {error}")
                continue
            if not exists:
                if source == "config:local.json":
                    errors.append(f"config:local.json → {path} does not exist or is not a file")
                continue
            error = validate_windows_ocr(name, path)
            if not error:
                return Resolution(path, source, "; ".join(errors))
            errors.append(error)
        return None

    resolved = check(options)
    if resolved:
        return resolved
    paths, scope = windows_ocr_search(name)
    resolved = check((path, "probe:local-drives") for path in paths)
    if resolved:
        return Resolution(resolved.value, resolved.source, "; ".join(filter(None, (resolved.detail, scope))))
    return Resolution(None, "none", "; ".join([*errors, scope,
        "not located/usable within this scope, not proof of no installation; provide the full executable path"]))


def resolve_node(explicit: str | Path | None = None, config: dict | None = None) -> Resolution:
    """Node executable. PATH comes first so a user's own install wins over any cached runtime."""
    candidates: list[Path] = []
    on_path = _which("node")
    if on_path is not None:
        candidates.append(on_path)
    codex_node = (
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        / "dependencies" / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
    )
    candidates.extend([
        codex_node,
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
        Path.home() / ".nvm" / "current" / "bin" / "node",
    ])
    return resolve(explicit=explicit, key="node", candidates=tuple(candidates), config=config)


def resolve_ocr_program(
    name: str, explicit: str | Path | None = None, config: dict | None = None
) -> Resolution:
    """External OCR helper (``tesseract``) or page renderer (``pdftoppm``)."""
    key = "ocr_executable" if name == "tesseract" else "pdftoppm_executable"

    on_path = _which(name)
    codex_native = (
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        / "dependencies" / "native"
    )
    candidates: list[Path] = []
    if on_path is not None:
        candidates.append(on_path)
    if name == "tesseract" and sys.platform == "win32":
        candidates.append(Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
    if sys.platform == "win32":
        candidates.append(codex_native / "poppler" / "Library" / "bin" / f"{name}.exe")
    else:
        candidates.extend([
            codex_native / "poppler" / "bin" / name,
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            Path("/usr/bin") / name,
        ])
    # Shims generated by scripts/setup_local_ocr.sh are the last resort: a real program wins.
    if sys.platform == "win32":
        return _resolve_windows_ocr(name, explicit, config, candidates)
    candidates.append(SKILL_ROOT / "scripts" / "ocr-shims" / name)
    return resolve(explicit=explicit, key=key, candidates=tuple(candidates), config=config)


def resolve_python(explicit: str | Path | None = None, config: dict | None = None) -> Resolution:
    """Interpreter used to run this skill's own scripts."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return Resolution(path, "flag")
        return Resolution(None, "flag", f"{path} does not exist")
    variable = env_name("python")
    from_env = os.environ.get(variable)
    if from_env:
        path = Path(from_env).expanduser()
        if path.exists():
            return Resolution(path, f"env:{variable}")
        return Resolution(None, f"env:{variable}", f"{path} does not exist")
    settings = load_local_config() if config is None else config
    from_config = _from_config(settings, "python")
    if from_config is not None and from_config.exists():
        return Resolution(from_config, "config:local.json")
    current = Path(sys.executable)
    if current.exists():
        return Resolution(current, "running interpreter")
    return Resolution(None, "none", "no interpreter resolved")


def ocr_helper_candidates(name: str) -> list[Path]:
    """Candidate locations for an OCR helper, in preference order.

    Kept as a list because the caller's lookup is explicit flag -> PATH -> these candidates,
    so a program the user put on PATH still wins over anything guessed here. An environment
    variable or ``config/local.json`` entry is more explicit than PATH, though, so those are
    returned as the effective explicit value by ``ocr_program_explicit``.
    """
    if sys.platform == "win32":
        resolved = resolve_ocr_program(name)
        return [resolved.value] if resolved.ok else []
    config = load_local_config()
    key = "ocr_executable" if name == "tesseract" else "pdftoppm_executable"
    from_env = os.environ.get(env_name(key))
    from_config = _from_config(config, key)
    candidates: list[Path] = []
    for value in (from_env, from_config):
        if value:
            path = Path(value).expanduser()
            if path.is_file():
                candidates.append(path)
    if name == "tesseract" and os.name == "nt":
        candidates.append(Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
    codex_native = (
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        / "dependencies" / "native"
    )
    if os.name == "nt":
        candidates.append(codex_native / "poppler" / "Library" / "bin" / f"{name}.exe")
    else:
        candidates.extend([
            codex_native / "poppler" / "bin" / name,
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            Path("/usr/bin") / name,
        ])
    candidates.append(SKILL_ROOT / "scripts" / "ocr-shims" / name)
    return candidates


# --- OCR discovery -------------------------------------------------------------------------
# The pipeline speaks two command-line contracts: a poppler-style renderer (`pdftoppm`) and a
# tesseract-style recognizer (`tesseract`). Any local engine can be routed behind those names, so
# "is OCR available?" is really "which of this machine's own engines can we wire up?".
#
# Discovery lives here rather than only inside setup_local_ocr.sh because the answer has to reach
# the agent on every platform (Windows has no bash), because doctor must be able to say "present
# but not wired" instead of just "not found", and because a teacher cannot be expected to know
# which conda environment happens to hold the engine.

# import name -> engine name. Ordered so the better Chinese-screenshot recall is reported first.
OCR_MODULE_ENGINES: tuple[tuple[str, str], ...] = (
    ("rapidocr_onnxruntime", "rapidocr"),
    ("rapidocr", "rapidocr"),
    ("paddleocr", "paddleocr"),
    ("easyocr", "easyocr"),
    ("tesserocr", "tesserocr"),
    ("pytesseract", "pytesseract"),
)

# The engine `--ocr-engine auto` reaches: the rapidocr-backed shim. Kept as the default because
# lab reports are dominated by screenshots and dense interface captures, which the shim reads
# better than on-device recognition does. That makes the alternatives explicit choices rather
# than defaults.
OCR_DEFAULT_ENGINE = "rapidocr"

# What each platform has natively, reported so the agent can name it to the teacher. This does not
# change the default: macOS has Vision (faster, weaker on dense screenshots) and Windows has no
# equivalent, so a Tesseract install is the route there -- there is no shim generator on Windows
# because the helper script is bash.
NATIVE_OCR_FOR_PLATFORM = {"darwin": "vision", "win32": "tesseract"}

# Which generated shim corresponds to which engine, so the report can say whether the engine is
# already reachable by the pipeline or merely present on disk.
OCR_SHIM_FOR_ENGINE = {"vision": "tesseract-vision", "rapidocr": "tesseract"}

_INTERPRETER_PROBE = """
import importlib, json, sys
found = {}
for name in %r:
    try:
        module = importlib.import_module(name)
    except Exception:
        continue
    found[name] = str(getattr(module, "__version__", "") or "")
print(json.dumps({"python": sys.executable, "modules": found}))
"""


def candidate_interpreters() -> list[Path]:
    """Interpreters worth asking about OCR, de-duplicated, the skill's own first."""
    seen: list[Path] = []

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        try:
            real = candidate.resolve()
        except OSError:
            return
        if real.is_file() and real not in seen:
            seen.append(real)

    add(resolve_python().value)
    for name in ("python3", "python"):
        add(_which(name))
    bases = [
        Path.home() / "anaconda3", Path.home() / "miniconda3",
        Path.home() / "miniforge3", Path.home() / "mambaforge",
        Path("/opt/anaconda3"), Path("/opt/miniconda3"), Path("/opt/homebrew/anaconda3"),
    ]
    for base in bases:
        envs = base / "envs"
        if not envs.is_dir():
            continue
        for env in sorted(envs.iterdir()):
            add(env / "bin" / "python")   # POSIX layout
            add(env / "python.exe")       # Windows layout
    for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        add(Path(candidate))
    return seen


def probe_ocr_modules(python: Path) -> dict[str, str]:
    """Import name -> version for the OCR engines this interpreter has. Never raises."""
    try:
        completed = subprocess.run(
            [str(python), "-c", _INTERPRETER_PROBE % ([name for name, _ in OCR_MODULE_ENGINES],)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {str(key): str(value) for key, value in (payload.get("modules") or {}).items()}


def ocr_inventory() -> list[dict]:
    """Every OCR route this machine could offer, preferred first. Report-only; wires nothing."""
    entries: list[dict] = []
    shim_dir = SKILL_ROOT / "scripts" / "ocr-shims"

    def shim_present(engine: str) -> bool:
        name = OCR_SHIM_FOR_ENGINE.get(engine)
        return bool(name) and (shim_dir / name).is_file()

    if sys.platform == "darwin":
        compiled = shim_dir / "visionocr"
        swiftc = _which("swiftc")
        entries.append({
            "engine": "vision",
            "kind": "native",
            "usable": bool(compiled.is_file() or swiftc),
            "location": str(compiled) if compiled.is_file() else (str(swiftc) if swiftc else ""),
            "version": "",
            "detail": "" if compiled.is_file() else (
                f"buildable with {swiftc} (one time)" if swiftc
                else "needs Xcode command line tools for swiftc"
            ),
            "wired": shim_present("vision"),
        })

    # One interpreter can hold several engines; report the first import name that matched so the
    # list stays readable, and never repeat an engine already found in an earlier interpreter.
    reported: set[str] = set()
    for python in candidate_interpreters():
        for import_name, version in sorted(probe_ocr_modules(python).items()):
            engine = dict(OCR_MODULE_ENGINES).get(import_name, import_name)
            if engine in reported:
                continue
            reported.add(engine)
            entries.append({
                "engine": engine,
                "kind": "python-module",
                "usable": True,
                "location": str(python),
                "version": version,
                "detail": import_name,
                "wired": shim_present(engine),
            })

    tesseract = resolve_ocr_program("tesseract")
    entries.append({
        "engine": "tesseract",
        "kind": "binary",
        "usable": tesseract.ok,
        "location": str(tesseract.value) if tesseract.ok else "",
        "version": "",
        "detail": "" if tesseract.ok else tesseract.detail,
        "wired": tesseract.ok,
    })

    native = NATIVE_OCR_FOR_PLATFORM.get(sys.platform, "tesseract")
    for entry in entries:
        entry["native"] = entry["engine"] == native
    # Usable-and-native first, then anything else usable, then the unavailable engines.
    entries.sort(key=lambda item: (not (item["native"] and item["usable"]), not item["usable"]))
    return entries


def ocr_report() -> dict:
    """What this machine has for OCR, what the pipeline will actually use, and how to enable it."""
    inventory = ocr_inventory()
    usable = [item for item in inventory if item["usable"]]
    renderer = resolve_ocr_program("pdftoppm")

    # The engine the pipeline reaches with --ocr-engine auto. That is decided by what the
    # tesseract lookup resolves to, not by "which shims exist": the vision shim is selectable but
    # is only used when asked for explicitly, so counting it as the engine in use would misreport
    # every default run. A resolved path inside scripts/ocr-shims/ means the rapidocr-backed shim;
    # anywhere else means a real tesseract binary.
    shim_dir = SKILL_ROOT / "scripts" / "ocr-shims"
    tesseract = resolve_ocr_program("tesseract")
    active = ""
    if tesseract.ok and tesseract.value is not None:
        active = "rapidocr" if shim_dir in tesseract.value.parents else "tesseract"
    vision_wired = (shim_dir / "tesseract-vision").is_file()

    return {
        # Auto uses the rapidocr-backed shim; the on-device recognizer is an explicit alternative.
        "default_engine": OCR_DEFAULT_ENGINE,
        "native_engine": NATIVE_OCR_FOR_PLATFORM.get(sys.platform, "tesseract"),
        "engine": active,
        "wired": tesseract.ok,
        "enable_command": "" if tesseract.ok else (
            "provide ocr_executable (any local drive) with chi_sim+eng; rerun doctor"
            if sys.platform == "win32" else "python3 scripts/bootstrap.py --ocr"
        ),
        "vision_wired": vision_wired,
        "vision_command": "python3 scripts/bootstrap.py --ocr --with-vision",
        "available_usable": [item["engine"] for item in usable],
        "renderer": None if renderer.value is None else str(renderer.value),
        "renderer_ok": renderer.ok,
        "renderer_detail": renderer.detail,
        "available": inventory,
    }


def portability_report() -> list[dict]:
    """Every external path plus the layer that answered, for ``doctor``."""
    items = [
        ("python", "every phase", resolve_python()),
        ("node", "review panel service", resolve_node()),
        ("teacher signature", "prepare (fixed grading precondition)", resolve_signature_pdf()),
        ("score font", "PDF write-back after approval", resolve_score_font()),
        ("tesseract", "prepare (local OCR)", resolve_ocr_program("tesseract")),
        ("pdftoppm", "prepare (local OCR)", resolve_ocr_program("pdftoppm")),
    ]
    return [
        {
            "check": name,
            "phase": phase,
            "ok": resolution.ok,
            "value": None if resolution.value is None else str(resolution.value),
            "detail": resolution.value if resolution.value is not None else resolution.detail,
            "source": resolution.source,
        }
        for name, phase, resolution in items
    ]


if __name__ == "__main__":
    print(json.dumps(portability_report(), ensure_ascii=False, indent=2, default=str))
