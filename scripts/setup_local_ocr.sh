#!/usr/bin/env bash
# Generate the local OCR helpers this machine needs.
#
# The pipeline shells out to two programs: pdftoppm (renders one PDF page to PNG)
# and tesseract (turns that PNG into text). Windows installations usually have both
# on PATH. macOS has no bundled Tesseract, so this script writes CLI-compatible
# helpers into scripts/ocr-shims/ that prepare_grading_packet.py finds automatically.
#
# Everything in scripts/ocr-shims/ is GENERATED and disposable: delete the directory at
# any time and re-run this script. The compiled visionocr binary is platform specific
# and is built here rather than shipped.
#
# The generated `pdftoppm` and `tesseract` commands are small bash wrappers that resolve
# an interpreter at run time, so moving the skill, renaming a conda environment or
# upgrading Python does not leave a dangling shebang.
#
# Usage:
#   scripts/setup_local_ocr.sh                 # write the rapidocr-backed helpers
#   scripts/setup_local_ocr.sh --with-vision   # also build the macOS Vision binary
#   scripts/setup_local_ocr.sh --selftest PDF  # render + OCR page 1 of PDF and report
#   scripts/setup_local_ocr.sh --print-python  # show the interpreter that would be used
#
# Nothing here needs a conda environment to be activated, an environment variable, or
# network access: rapidocr ships its models inside the wheel.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM_DIR="$SCRIPT_DIR/ocr-shims"
WITH_VISION=0
SELFTEST_PDF=""
PRINT_PYTHON=0

while [ $# -gt 0 ]; do
  case "$1" in
    --with-vision) WITH_VISION=1; shift ;;
    --selftest)
      if [ $# -lt 2 ]; then echo "--selftest needs a PDF path" >&2; exit 2; fi
      SELFTEST_PDF="$2"; shift 2 ;;
    --print-python) PRINT_PYTHON=1; shift ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# --- find an interpreter that already has the OCR stack -------------------------
python_has_stack() {
  "$1" - <<'PY' >/dev/null 2>&1
import importlib
importlib.import_module("rapidocr_onnxruntime")
try:
    importlib.import_module("pymupdf")
except ImportError:
    importlib.import_module("fitz")
importlib.import_module("PIL")
PY
}

find_python() {
  local candidates=() candidate base envdir
  if [ -n "${OCR_PYTHON:-}" ]; then candidates+=("$OCR_PYTHON"); fi
  if command -v python3 >/dev/null 2>&1; then candidates+=("$(command -v python3)"); fi
  for base in /opt/anaconda3 /opt/miniconda3 /opt/homebrew/anaconda3 \
              "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" "$HOME/mambaforge"; do
    if [ -d "$base/envs" ]; then
      for envdir in "$base"/envs/*; do
        if [ -x "$envdir/bin/python" ]; then candidates+=("$envdir/bin/python"); fi
      done
    fi
  done
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then candidates+=("$candidate"); fi
  done
  for candidate in "${candidates[@]}"; do
    if python_has_stack "$candidate"; then printf '%s\n' "$candidate"; return 0; fi
  done
  return 1
}

if ! INTERP="$(find_python)"; then
  cat >&2 <<'MSG'
No Python interpreter with the OCR stack was found.
Install it into any environment, then re-run:
    python3 -m pip install rapidocr-onnxruntime pymupdf pillow
Or set OCR_PYTHON=/path/to/python and re-run.
MSG
  exit 1
fi

if [ "$PRINT_PYTHON" -eq 1 ]; then
  printf '%s\n' "$INTERP"
  exit 0
fi

mkdir -p "$SHIM_DIR"

# --- implementation bodies (regenerated on every run) ---------------------------
cat > "$SHIM_DIR/pdftoppm.py" <<'PY'
"""pdftoppm-compatible page renderer backed by PyMuPDF.

Supports the invocation the grading pipeline uses:
    pdftoppm -f <first> -l <last> -r <dpi> -png -singlefile <pdf> <prefix>
Unrelated poppler flags are ignored rather than rejected, because the caller is a
pipeline that only cares about the resulting <prefix>.png.
"""
import sys


def parse(argv):
    first = last = None
    dpi = 150
    singlefile = False
    takes_value = {"-f", "-l", "-r", "-scale-to", "-scale-dimension-before-rotation"}
    boolean = {
        "-png", "-jpeg", "-jpegcmyk", "-gray", "-mono", "-tiff", "-sep", "-q",
        "-singlefile", "-cropbox", "-freetype", "-aa", "-aaVector", "-up",
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in takes_value and index + 1 < len(argv):
            value = argv[index + 1]
            if arg == "-f":
                first = int(value)
            elif arg == "-l":
                last = int(value)
            elif arg == "-r":
                dpi = int(float(value))
            index += 2
        elif arg in boolean:
            if arg == "-singlefile":
                singlefile = True
            index += 1
        else:
            index += 1
    return first, last, dpi, singlefile


def main(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: pdftoppm [options] <pdf> <prefix>\n")
        return 2
    pdf, prefix = argv[-2], argv[-1]
    first, last, dpi, singlefile = parse(argv[1:-2])
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    try:
        document = fitz.open(pdf)
    except Exception as error:  # noqa: BLE001 - surface any open failure to the caller
        sys.stderr.write("pdftoppm shim: cannot open %s: %s\n" % (pdf, error))
        return 1
    start = first if first else 1
    end = last if last else document.page_count
    if start < 1 or end > document.page_count or start > end:
        sys.stderr.write(
            "pdftoppm shim: page range %s-%s outside 1-%s\n" % (start, end, document.page_count)
        )
        return 1
    for page_number in range(start, end + 1):
        try:
            pixmap = document[page_number - 1].get_pixmap(dpi=dpi)
        except Exception as error:  # noqa: BLE001
            sys.stderr.write("pdftoppm shim: cannot render page %s: %s\n" % (page_number, error))
            return 1
        target = "%s.png" % (prefix,) if singlefile else "%s-%d.png" % (prefix, page_number)
        pixmap.save(target)
        if singlefile:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PY

cat > "$SHIM_DIR/tesseract.py" <<'PY'
"""tesseract-compatible OCR backed by rapidocr-onnxruntime (local CPU, offline).

Supports the invocation the grading pipeline uses:
    tesseract <image> stdout -l <langs> --psm <n>
Language and page-segmentation flags are accepted and ignored: rapidocr is a detector
plus recognizer whose models already cover Chinese and English and ship inside the
wheel, so no TESSDATA directory is involved.

Exits non-zero only when recognition itself fails. A page with no recognizable text
exits 0 with empty output so the caller keeps that page in its visual queue instead of
retrying it.
"""
import sys


def main(argv):
    args = argv[1:]
    if not args:
        sys.stderr.write("usage: tesseract <image> [outputbase|stdout] [options]\n")
        return 2
    image = args[0]
    target = "stdout"
    if len(args) > 1 and not args[1].startswith("-"):
        target = args[1]
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as error:
        sys.stderr.write("tesseract shim: rapidocr-onnxruntime is missing: %s\n" % (error,))
        return 1
    try:
        result, _ = RapidOCR()(image)
    except Exception as error:  # noqa: BLE001
        sys.stderr.write("tesseract shim: recognition failed: %s\n" % (error,))
        return 1
    lines = [
        str(item[1]).strip()
        for item in (result or [])
        if len(item) > 1 and str(item[1]).strip()
    ]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    if target == "stdout":
        sys.stdout.write(payload)
    else:
        with open(target + ".txt", "w", encoding="utf-8") as handle:
            handle.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PY
chmod 644 "$SHIM_DIR/pdftoppm.py" "$SHIM_DIR/tesseract.py"

# --- interpreter-resolving wrappers --------------------------------------------
write_wrapper() {
  local target="$1" body="$2" probe="$3"
  cat > "$target" <<WRAPPER
#!/bin/bash
# Generated by scripts/setup_local_ocr.sh - delete and re-run it any time.
# The interpreter is resolved at run time, so renaming a conda environment or moving
# the skill does not leave a dangling shebang behind.
set -euo pipefail
here="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
body="\$here/$body"
candidates=()
if [ -n "\${OCR_PYTHON:-}" ]; then candidates+=("\$OCR_PYTHON"); fi
candidates+=("$INTERP" python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3)
for py in "\${candidates[@]}"; do
  [ -n "\$py" ] || continue
  command -v "\$py" >/dev/null 2>&1 || continue
  if "\$py" -c '$probe' >/dev/null 2>&1; then
    exec "\$py" "\$body" "\$@"
  fi
done
echo "$(basename "$target"): no Python interpreter with the required packages was found." >&2
echo "Set OCR_PYTHON=/path/to/python, or re-run scripts/setup_local_ocr.sh" >&2
exit 1
WRAPPER
  chmod 755 "$target"
}

write_wrapper "$SHIM_DIR/pdftoppm" "pdftoppm.py" 'import pymupdf'
write_wrapper "$SHIM_DIR/tesseract" "tesseract.py" 'import rapidocr_onnxruntime'

# --- optional macOS Vision binary (built here, never shipped) -------------------
cat > "$SHIM_DIR/visionocr.swift" <<'SWIFT'
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let image = NSImage(contentsOfFile: args[1]),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("visionocr: cannot read image\n".data(using: .utf8)!)
    exit(1)
}
let request = VNRecognizeTextRequest()
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
} catch {
    FileHandle.standardError.write("visionocr: recognition failed\n".data(using: .utf8)!)
    exit(1)
}
for observation in (request.results ?? []) {
    if let candidate = observation.topCandidates(1).first { print(candidate.string) }
}
SWIFT

cat > "$SHIM_DIR/tesseract-vision" <<'WRAPPER'
#!/bin/bash
# Generated by scripts/setup_local_ocr.sh - delete and re-run it any time.
# tesseract-compatible wrapper around the macOS Vision OCR binary, for pages whose text
# is regular prose. Dense screenshots read better with the rapidocr-backed `tesseract`.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
binary="$here/visionocr"
if [ ! -x "$binary" ]; then
  if command -v swiftc >/dev/null 2>&1 && [ -f "$here/visionocr.swift" ]; then
    echo "tesseract-vision: building the Vision binary (one time)..." >&2
    if ! swiftc -O "$here/visionocr.swift" -o "$binary"; then
      echo "tesseract-vision: build failed; run scripts/setup_local_ocr.sh --with-vision" >&2
      exit 1
    fi
  else
    echo "tesseract-vision: $binary is missing and swiftc is unavailable." >&2
    echo "Install Xcode command line tools, then run scripts/setup_local_ocr.sh --with-vision" >&2
    exit 1
  fi
fi
exec "$binary" "$1"
WRAPPER
chmod 755 "$SHIM_DIR/tesseract-vision"

cat > "$SHIM_DIR/README.md" <<'MSG'
# Generated OCR helpers

Everything in this directory is generated by `scripts/setup_local_ocr.sh` and is
disposable: delete the whole directory and re-run that script to rebuild it.

- `pdftoppm.py`, `tesseract.py` — the implementations (PyMuPDF rendering, rapidocr OCR)
- `pdftoppm`, `tesseract` — wrappers that resolve a Python interpreter at run time
- `visionocr.swift` + `tesseract-vision` — optional macOS Vision OCR. The compiled
  `visionocr` binary is platform specific and is built locally on demand, never shipped.
- `pdftoppm` and `tesseract` are the names `prepare_grading_packet.py` looks for.
MSG

echo "interpreter: $INTERP"
echo "helpers:     $SHIM_DIR/pdftoppm"
echo "             $SHIM_DIR/tesseract      (rapidocr-onnxruntime)"

if [ "$WITH_VISION" -eq 1 ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "vision:      skipped (macOS only)" >&2
  elif ! command -v swiftc >/dev/null 2>&1; then
    echo "vision:      skipped (swiftc not found; install Xcode command line tools)" >&2
  elif swiftc -O "$SHIM_DIR/visionocr.swift" -o "$SHIM_DIR/visionocr" >/dev/null 2>&1; then
    echo "vision:      $SHIM_DIR/tesseract-vision"
  else
    echo "vision:      build failed" >&2
  fi
fi

# --- smoke test: do the wrappers start? ----------------------------------------
smoke_failed=0
"$SHIM_DIR/pdftoppm" >/dev/null 2>&1 || [ $? -eq 2 ] || smoke_failed=1
"$SHIM_DIR/tesseract" >/dev/null 2>&1 || [ $? -eq 2 ] || smoke_failed=1
if [ "$smoke_failed" -eq 1 ]; then
  echo "smoke:       FAILED - a helper did not start" >&2
  exit 1
fi
echo "smoke:       both helpers start"

# --- optional end-to-end check on a real PDF -----------------------------------
if [ -n "$SELFTEST_PDF" ]; then
  if [ ! -f "$SELFTEST_PDF" ]; then echo "selftest: $SELFTEST_PDF not found" >&2; exit 1; fi
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  "$SHIM_DIR/pdftoppm" -f 1 -l 1 -r 160 -png -singlefile "$SELFTEST_PDF" "$work/page" || {
    echo "selftest: render failed" >&2; exit 1; }
  rendered="$("$INTERP" -c "from PIL import Image;import sys;im=Image.open(sys.argv[1]);print('%dx%d' % im.size)" "$work/page.png")"
  text="$("$SHIM_DIR/tesseract" "$work/page.png" stdout -l chi_sim+eng --psm 6)"
  chars="$(printf '%s' "$text" | wc -c | tr -d ' ')"
  echo "selftest:    page 1 rendered $rendered; OCR returned $chars bytes"
fi

cat <<'MSG'

prepare_grading_packet.py finds these helpers without extra flags:

    python3 scripts/batch_pipeline.py prepare \
        --report-dir <folder> --work-dir <work> \
        --inspection-mode smart --ocr-mode auto

Useful switches: --ocr-engine rapidocr|vision, --ocr-mode off,
--ocr-executable / --pdftoppm-executable to override either program.
See references/local-ocr-setup.md.
MSG
