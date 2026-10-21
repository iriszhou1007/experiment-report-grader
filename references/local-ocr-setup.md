# Local OCR setup

OCR is optional. Without it the pipeline still grades, but pages whose content lives in
screenshots stay in the visual queue and cost image tokens — or get graded blind if the
model is not allowed to look at images. With it, those pages become text.

## What the pipeline needs

Two programs, both invoked as ordinary subprocesses:

| Program | Invocation | Contract |
|---|---|---|
| `pdftoppm` | `pdftoppm -f <first> -l <last> -r 160 -png -singlefile <pdf> <prefix>` | writes `<prefix>.png`; non-zero exit on failure |
| `tesseract` | `tesseract <png> stdout -l chi_sim+eng --psm 6` | recognized text on stdout; non-zero exit on failure |

`ocr_candidate_pages` needs **both**. On macOS and Linux a missing one is a silent, non-fatal
fallback: OCR reports `unavailable` and pages stay in the visual queue. On Windows it is fatal —
see [Windows](#windows). `--ocr-executable` and `--pdftoppm-executable` override either program;
`--ocr-mode off` disables the step entirely.

Lookup order for each program, applied by `resolve_env.py`:

1. the explicit `--ocr-executable` / `--pdftoppm-executable` flag
2. the `EXPERIMENT_REPORT_GRADER_OCR_EXECUTABLE` / `..._PDFTOPPM_EXECUTABLE` variable
3. `config/local.json`
4. platform candidates: `PATH` first, then the Codex runtime poppler, Homebrew and system
   prefixes, then the shims this skill generates under `scripts/ocr-shims/`

A flag or variable naming a path that does not exist stops resolution — those are deliberate
per-invocation instructions. A `config/local.json` entry that does not exist does **not**: the
reason is recorded and resolution continues, because a config file carried over from another
machine must not be able to disable a program that is sitting right there. `doctor` prints the
recorded reason, e.g.
`[ok] pdftoppm ... (note: config:local.json → I:\...\pdftoppm.exe does not exist)`.

## Seeing what this machine has

Every machine installs OCR differently — macOS ships Vision, Windows expects Tesseract, a conda
environment may hold rapidocr — so the skill names what is present rather than only reporting
what is missing:

```bash
python3 scripts/bootstrap.py               # the "local OCR (this machine)" section
python3 scripts/batch_pipeline.py doctor   # the "ocr engine" row
```

```text
  local OCR (this machine):
    default engine:  rapidocr (the tesseract-compatible shim)
    platform-native: vision
    vision     native, usable, wired    /…/scripts/ocr-shims/visionocr
    rapidocr   usable, wired            /opt/anaconda3/envs/prism/bin/python3
    tesseract  usable, wired            /…/scripts/ocr-shims/tesseract
    in use: rapidocr   (Vision is also wired: --ocr-engine vision)
```

`default engine` is what `--ocr-engine auto` uses; `platform-native` is what the platform
itself provides. Building the inventory probes every interpreter it can find — the skill's own,
`python3` on `PATH`, conda environments, the usual Homebrew and system prefixes — for
`rapidocr_onnxruntime`, `rapidocr`, `paddleocr`, `easyocr`, `tesserocr` and `pytesseract`, so an
engine installed in some environment is reported instead of missed. Looking changes nothing;
`--ocr` is what generates the shims.

## What turning OCR on costs

OCR runs on local CPU, so it costs no API tokens, but it is not free. Budget the time
before starting a large batch, and expect `prepare` to print nothing but its progress
lines while it works:

| | OCR off | OCR on (first run) | OCR on (re-run, cache hit) |
|---|---:|---:|---:|
| 4 reports, 27 candidate pages | 2.5 s | **80 s** | 0.5 s |

That is roughly 2.5–3 s per candidate page, single-machine. A 70-report batch with
screenshot-heavy reports has on the order of 400–650 candidate pages, so a first
`prepare` can take **20–30 minutes**; it prints
`[ocr] <report>: N candidate page(s) [...] ...` per report so it does not look frozen.

The recognition cache lives in `<work-dir>/ocr-cache` and is keyed by page hash,
candidate page set and engine, so re-running `prepare` is fast. Note that `clean`
deletes the work directory — and therefore that cache — so a cleaned batch re-runs OCR
from scratch. New batches keep the work directory at `<report-dir>/<batch-id>_评分暂存`, beside the submitted reports. Arbitrary `--work-dir` paths are rejected; a matching legacy batch under the same report directory can still be resumed and cleaned.

Evidence grows as well: with `--inspection-mode smart`, the same 4 reports go from
12,987 to 18,640 characters of packet (~+44%), because 27 pages that previously stayed
in the visual queue now contribute text. The money is still small (low single-digit
fen per report); the wall-clock is the real cost.

OCR re-reads the page, including text the PDF already exposed, so the merge drops lines
that only repeat the page's own text layer and lines that carry no signal — hex viewer
grids, memory-dump columns and similar noise from dense screenshots. The report's
`coverage:` line records how many lines were dropped, e.g.
`filtered 41 OCR lines repeating the text layer or carrying no signal`. Measured on one
4-report batch: 148 of 1,986 OCR lines duplicated the text layer (~7.5%) and a further
block was hex noise.

## macOS

Nothing needs to be activated: shims carry an absolute interpreter shebang, need no
environment variable, and rapidocr ships its models inside the wheel, so recognition
works offline. The default engine is the rapidocr shim:

```bash
python3 scripts/bootstrap.py --ocr                    # set up the default engine
python3 scripts/bootstrap.py --ocr --with-vision      # ... and macOS Vision as well
# or directly, with a smoke test on a real report:
scripts/setup_local_ocr.sh --selftest path/to/any/report.pdf
```

`--with-vision` is opt-in. `auto` uses rapidocr, and Vision is chosen per run with
`--ocr-engine vision`; `--all` does **not** build it. Building it is still worth doing when
some batches are prose-heavy, because it is roughly three times faster (measurements below).
It needs `swiftc` from the Xcode command line tools — without it the script warns and carries
on rather than failing.

The script finds a Python interpreter that already has the OCR stack (it also accepts
`OCR_PYTHON=/path/to/python`) and writes into `scripts/ocr-shims/`:

| File | Backed by | Notes |
|---|---|---|
| `pdftoppm` + `pdftoppm.py` | PyMuPDF | renders the same geometry as poppler |
| `tesseract` + `tesseract.py` | rapidocr-onnxruntime (PP-OCRv4) | **default**; best recall on dense screenshots |
| `tesseract-vision` (+ `visionocr.swift`) | macOS Vision | built on first use; faster, weaker on dense UI |

Every file in that directory is **generated and disposable**: delete the directory and
re-run the script to rebuild it. `pdftoppm` and `tesseract` are bash wrappers that try
`OCR_PYTHON`, then the interpreter recorded at setup time, then `python3` from the usual
prefixes, and fail with an actionable message when none of them has the packages. That
keeps a renamed conda environment or a moved skill directory from turning into
`bad interpreter`. The compiled `visionocr` binary is platform specific and is built
locally — by the script, or automatically on first use of `tesseract-vision` — never
shipped.

If an interpreter with the stack is missing:

```bash
python3 -m pip install rapidocr-onnxruntime pymupdf pillow
```

Measured on one 160 DPI page of a screenshot-heavy report (`1160×1638`):

| Engine | Output | Time |
|---|---|---|
| rapidocr | 135 lines | 2.4 s |
| macOS Vision | 83 lines | 0.7 s |

Vision is about three times faster but drops roughly a third of the content here, and
mangles dense UI text badly (`Iienes Irfoy masice Steviees` for `Internet Information
Services`). Prefer rapidocr for screenshot-heavy reports; reach for Vision only on prose
pages where speed matters.

macOS also already has a usable renderer, because the Codex runtime ships poppler:

```text
~/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/bin/pdftoppm
```

Note the layout: `poppler/bin/pdftoppm`, with no `Library/` layer and no `.exe`. The
earlier Windows-only candidate hid this file, which is why OCR reported "unavailable"
on a machine that had a perfectly good renderer.

## Choosing an engine

```bash
--ocr-engine auto      # default: the ordinary `tesseract` lookup — usually the rapidocr
                       # shim, or a real Tesseract where one is installed
--ocr-engine rapidocr  # force scripts/ocr-shims/tesseract
--ocr-engine vision    # force scripts/ocr-shims/tesseract-vision (macOS only)
```

`--ocr-engine` is not saved as a batch setting, so pass it on every run that needs it.
`--ocr-executable` still wins over both and accepts any path. The OCR cache is keyed by
page hash, candidate page set **and engine**, so switching engines re-runs recognition
instead of silently reusing the previous engine's text.

## Windows

Windows has no shim generator — the helper script is bash — so its OCR route is a real Tesseract
install. Import/registration alone runs no health checks: run `bootstrap.py` or `doctor` afterward.
Do not infer "not installed" from a miss on C:. Resolution checks explicit CLI paths, environment,
`config/local.json`, PATH and known runtime paths first. When no usable automatic candidate is
found, it enumerates **all fixed local volumes**, including D:, E: and I: when present, and searches
common installation trees: `Program Files`, `Program Files (x86)`, `Programs`, `tools`, `apps`,
`software`, `软件`, `软件安装`, Anaconda/Miniconda, Chocolatey and Scoop. Portable
`Tesseract*` / `poppler*` directories directly under a drive are included, as are applicable
home-local Programs/Scoop/conda trees. Examples:

```text
D:\Program Files\Tesseract-OCR\tesseract.exe
I:\tools\poppler-26.07\Library\bin\pdftoppm.exe
%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe
```

This is **not a full-disk search**. Each volume gets its own maximum 4 descendant-directory levels
per common base, 6,000 directory entries and 2-second cooperative budget. An individual filesystem
call can outlast that budget. Network/removable volumes, directory symlinks/junctions and unrelated
root directories such as `Users` are not recursively traversed. Permission failures and budget
limits are recorded; they do not stop searching later drives. A miss means **not located/usable
within this scope**, not proof of no installation. Ask for an existing executable's full path before
suggesting an install. Automatic search results are cached only for the current command/process;
after installing/moving software, rerun the command.

An existing filename is not enough: Tesseract must successfully identify itself with `--version`
and list both `chi_sim` and `eng` with `--list-langs`; Poppler's `pdftoppm -v` must start and identify
itself (stdout or stderr). Each subprocess has a 5-second timeout. This checks startup/language
availability, not OCR recognition accuracy. A broken automatic/config candidate is reported while
other candidates are tried; an invalid explicit flag/environment choice is terminal and must be
corrected, not silently replaced. The validated paths are pinned for extraction.

If **either** helper is unresolved/unusable, `prepare` **stops before creating a batch** while OCR
is enabled, and reports all failed prerequisites together. For example:

```text
Windows OCR prerequisites are not ready; no batch work was started.
  pdftoppm: bounded common-install-directory search: ...
  ... not proof of no installation; provide the full executable path
```

Paths on any drive may be configured in `config/local.json`:

```json
{
  "ocr_executable": "D:/Program Files/Tesseract-OCR/tesseract.exe",
  "pdftoppm_executable": "I:/tools/poppler-26.07/Library/bin/pdftoppm.exe"
}
```

Or, in PowerShell for the current session:

```powershell
$env:EXPERIMENT_REPORT_GRADER_OCR_EXECUTABLE = 'D:\Program Files\Tesseract-OCR\tesseract.exe'
$env:EXPERIMENT_REPORT_GRADER_PDFTOPPM_EXECUTABLE = 'I:\tools\poppler-26.07\Library\bin\pdftoppm.exe'
python scripts\batch_pipeline.py doctor
```

Do not replace other existing config keys. After confirming paths, `bootstrap.py --write-config`
can record them. Missing language data must be supplied in the selected Tesseract's tessdata
location (check `TESSDATA_PREFIX` too), not solved by pretending the binary itself is absent.

`--ocr-mode off` is the deliberate way to grade text-only reports where no OCR exists; it is
the only way past that check. Never run `scripts/setup_local_ocr.sh` on Windows — there is no
bash, and `bootstrap.py --ocr` reports what the machine has instead of pretending to set up.

## Linux

```bash
sudo apt install poppler-utils tesseract-ocr tesseract-ocr-chi-sim
```

## Two traps worth knowing

**A real Tesseract without Chinese data fails silently.** If `chi_sim` is missing, the
pipeline retries the page with `-l eng`; that call succeeds with exit code 0 and returns
garbage for Chinese screenshots, which then enters the evidence. Install
`tesseract-ocr-chi-sim` (or `tesseract-lang` from Homebrew) or use the rapidocr shim,
which does not depend on TESSDATA at all.

**The sufficiency test cannot tell good recognition from bad.** `ocr_text_is_sufficient`
measures length, line count and character density only. Garbled output passes, and a page
that passes is removed from the visual queue — so the model never sees the original.
The pipeline compensates by marking OCR text where truncation cannot reach it:

```text
[p.3; OCR text merged (image recognition, may contain errors)]
```

plus an `OCR text merged on: ...` note in the report's `coverage:` line. If a score turns
on what an OCR page says, open the source page.

## Verifying a setup

```bash
python3 scripts/bootstrap.py               # names the engine, where it is, and whether it is wired
python3 scripts/batch_pipeline.py doctor   # adds an "ocr engine" row beside the two program lookups
```

`doctor` gained that third row because the two program rows can only report "no tesseract
binary", which reads as "no OCR on this machine" even when Vision or rapidocr is present and
merely unwired.

To see just the two programs, without running a batch:

```bash
python3 -c "import sys;sys.path.insert(0,'scripts');from prepare_grading_packet import ocr_helper_candidates,find_program;[print(n,'->',find_program(None,n,ocr_helper_candidates(n))) for n in ('tesseract','pdftoppm')]"
```

After a `prepare`, the batch's `visual-review-plan.json` records
`ocr_status` (`completed` / `partial` / `unavailable` / `off`), `ocr_engine`, and
`ocr_resolved_pages`. On macOS and Linux, `unavailable` with no explicit `--ocr-mode off`
means a helper was not found.

The shims are generated per machine and are not portable: re-run
`python3 scripts/bootstrap.py --ocr` (macOS/Linux) after moving the skill, changing Python
environments, or rebuilding the machine. The generated directory is excluded from the
distribution archive, so an unpacked copy starts with none.
