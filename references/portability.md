# Portability and migration

How this skill moves to another machine, another user, or another agent host (DSH, Codex,
WorkBuddy/CodeBuddy, Claude Code) without the grading pipeline behaving differently.

This is a plan, not a description of shipped behavior. It records what has been verified, what
breaks today, and the target design. Evidence is pinned to commit `ffb2ca9` (pipeline 1.3); line
numbers drift, so re-check before acting on any single row.

"Portable" here means three separate properties, and a host can satisfy one without the others:

1. **Discoverable** — the host finds the bundle and lists the skill by name.
2. **Runnable** — every external path the pipeline needs resolves on the target machine.
3. **Degradable** — when an optional capability is absent, the failure is reported up front
   instead of after the instructor has reviewed a whole batch.

## 1. Host discovery (verified)

| Host | User scope | Project scope | Notes |
|---|---|---|---|
| DSH | `~/.dsh/skills` (rank 400), `~/.agents/skills` (rank 500) | `<projectRoot>/.dsh/skills` (100), `<projectRoot>/.agents/skills` (200) | Rank ascending = higher precedence; `<projectRoot>` is the nearest `.git` ancestor. Discovery is exactly one level: `<root>/<name>/SKILL.md` or `<root>/<name>.md`; nested `**/SKILL.md` is deliberately unsupported. Frontmatter requires `name` + `description`; `name` must be kebab-case. `~/.dsh/skills` skips its `.system` subdirectory. `customSkillDirs` (rank 300) and a bundled root also exist. |
| Codex | `~/.agents/skills` (documented), `~/.codex/skills` (`CODEX_HOME`, present and read in practice) | `$CWD/.agents/skills`, each parent up to the repo root, `$REPO_ROOT/.agents/skills`; `/etc/codex/skills` for admin scope | `agents/openai.yaml` in the bundle carries UI metadata, invocation policy and tool dependencies. `[[skills.config]]` in `~/.codex/config.toml` can disable a skill by `path`. |
| WorkBuddy | `~/.workbuddy/skills` | `.workbuddy/skills` | Same product family as CodeBuddy, different directory name. |
| CodeBuddy | `~/.codebuddy/skills` | `.codebuddy/skills` | Frontmatter additionally supports `allowed-tools`, `disable-model-invocation`, `user-invocable`, `context: fork`, `agent`, `model`, `hooks`. |
| Claude Code | `~/.claude/skills` | `.claude/skills` (and parents up to the repo root) | Documented as accepting a **symlink** at `<skill-name>`; also plugin-scoped as `<plugin>/skills/<name>/SKILL.md` with `.claude-plugin/plugin.json`. |

### 1.1 The shared landing point

`~/.agents/skills/<name>/` is read by **both DSH (rank 500) and Codex (user scope)**. One
registration therefore covers two hosts, which is why it should be the default install target
rather than either vendor's private directory.

`~/.dsh/skills` is still worth a second entry when DSH is the primary host: it ranks above
`~/.agents/skills`, so it wins a name collision.

### 1.2 Frontmatter subset

Keep `SKILL.md` frontmatter at the intersection every host accepts — `name` + `description` — and
push host-specific keys into per-host files under `agents/`. Rationale, not preference: DSH
*excludes* a skill whose optional invocation values are malformed, so a key that is valid for
CodeBuddy but misspelled for DSH can remove the skill from DSH's catalog entirely.

| Key | DSH | Codex | WorkBuddy/CodeBuddy | Claude Code |
|---|---|---|---|---|
| `name` | required, kebab-case | required | optional (dir name fallback) | optional (dir name becomes the command) |
| `description` | required | required | optional | recommended |
| `whenToUse` | read | ignored | ignored | ignored |
| `disable-model-invocation`, `user-invocable` | read | via `agents/openai.yaml` | read | read |
| `allowed-tools`, `context`, `agent`, `model`, `hooks` | ignored | ignored | read | read |

Host-specific files: `agents/openai.yaml` (Codex, already present), `agents/dsh.yaml`,
`agents/workbuddy.yaml`. Records of who reads what belong in this table; the files themselves
stay declarative.

### 1.3 Registration roots as measured on this machine

| Root | State | Meaning |
|---|---|---|
| `~/.agents/skills` | absent (parent writable) | The shared DSH + Codex target — absent, so `install_skill.py` must create it |
| `~/.dsh/skills` | absent | DSH-native, higher precedence on a name clash |
| `~/.codex/skills` | **present, 6 skills** | Codex works today through `CODEX_HOME`, not through `~/.agents` |
| `~/.claude/skills` | absent | Claude Code personal scope |
| `~/.workbuddy/skills`, `~/.codebuddy/skills` | absent | Neither vendor CLI is installed here |
| `/etc/codex/skills` | absent | Codex admin scope, not applicable to a single-user machine |

Consequence for the installer: on this machine the only *occupied* user root is Codex's own, so
"install into `~/.agents/skills`" is a creation, not an edit — and it is the one action that makes
the skill visible to DSH and Codex at once. A `--host auto` dry run has to report "would create"
rather than assuming any of these exist.

## 2. What blocks a move today

Status after P1 (see §4.4) and the writer unification: items **1–6 are implemented and verified**;
7 and 8 remain. Items 3 and 5 went further than planned: rather than keeping the Node writers as an
available alternative, both were retired and deleted, so the artifact writers are Python only and
`finalize` needs no Node at all.

| # | Evidence | Failure mode on a new machine | Target fix |
|---|---|---|---|
| 1 | `scripts/writeback_paths.py` shipped `DEFAULT_SIGNATURE_PDF` as an absolute path into a second repository of the author's (`<home>/git/hw-worker/demo-signature.pdf`) | Write-back depends on a directory that only exists on the author's machine, and it surfaces only when the instructor approves a report — after the whole batch has been reviewed. Worse, because that fallback travelled *inside the package*, a freshly unpacked copy looked healthy on the build machine while resolving to a file outside the archive. | Resolve: explicit `--signature-pdf` → `$EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF` → `config/local.json` → `assets/signature/` in the bundle → **unresolved**, with a message naming those four options and no out-of-bundle fallback. **Fixed — see §4.6.** |
| 2 | `scripts/writeback_paths.py:14` — macOS-only `Bradley Hand Bold.ttf` | No handwriting face off macOS; the score is the last thing written. | Probe macOS/Windows/Linux faces, allow `--score-font` or `$EXPERIMENT_REPORT_GRADER_SCORE_FONT`, and fall back to a bundled or standard face with a recorded warning. |
| 3 | `scripts/batch_pipeline.py:749`, `scripts/review_server.mjs:29-53` — `@oai/artifact-tool` only under the Codex runtime cache | **The one hard Codex lock-in.** No Excel workbook on any other host; `finalize` fails after all decisions are filled in. `export_scores.mjs:4` imports it directly, and `:212-231` also uses it for sheet PNG previews and formula-error inspection. | Add an equivalent **openpyxl** exporter (formulas `=SUM(...)`/`=MIN(满分,总分+偏移)`, number formats, freeze panes, column widths, both sheets), used automatically when the JS runtime is absent. Keep the JS path when available. **Measured equivalent — see §4.1. Resolved by deleting the Node writer: `@oai/artifact-tool` is no longer referenced anywhere in the skill.** |
| 4 | `scripts/batch_pipeline.py:738` — node fallback is `.../codex-runtimes/.../node.exe` | Node not found on a machine without the Codex cache. | Resolve `--node` → `$EXPERIMENT_REPORT_GRADER_NODE` → `PATH` → platform candidates. **Done.** With items 3 and 5 retired, Node is no longer needed by `finalize` at all; it remains required only by the local review panel service (`scripts/review_server.mjs`), which is where `doctor` now reports it. |
| 5 | `scripts/build_review_html.mjs:31-35` — one `__REVIEWS_JSON__` placeholder substitution, nothing else | Node is required for HTML for no functional reason. | Port to ~15 lines of Python; keep the `.mjs` for parity or retire it. Removes node from `finalize` entirely. **Measured byte-identical — see §4.1. Resolved by retiring the `.mjs`.** |
| 6 | `scripts/prepare_grading_packet.py:173-201` — `ocr_helper_candidates` lists the Codex runtime, Homebrew, `/usr/local/bin`, `/usr/bin`, then the shims | OCR reports `unavailable` on a new machine and pages silently stay in the visual queue; the batch still "succeeds". Already documented as a per-machine step in `references/local-ocr-setup.md`. | Keep codex-runtimes as one candidate among many, add `$EXPERIMENT_REPORT_GRADER_OCR_EXECUTABLE` / `..._PDFTOPPM_EXECUTABLE`, and have `setup_local_ocr.sh` run as part of bootstrap. **Done — see §5. Windows has no shim generator (the script is bash), so there a missing Tesseract stops `prepare` rather than degrading silently.** |
| 7 | `SKILL.md:31-40`, `README.md:107-111` — `/opt/anaconda3/envs/prism/bin/python3`, `~/git/experiment-report-grader` | Documentation cannot be copied onto another machine verbatim. | Document `--skill-dir` + `EXPERIMENT_REPORT_GRADER_WORKSPACE` as the normal case, not as an escape hatch, and show a resolved-interpreter form. |
| 8 | `scripts/ocr-shims/` is gitignored and generated per machine, including a compiled `visionocr` | A fresh clone has no OCR helpers until setup runs. | bootstrap step, then `doctor` reports which engine ended up active. **Done — `bootstrap.py --ocr` generates them, `doctor` names the active engine, and the directory is excluded from the distribution archive.** |

One thing already works in the skill's favor and should not be regressed: every pipeline script
locates its own directory via `Path(__file__).resolve().parent` (no self-path hardcoding). The
`ensure_modules_link` junction helper this paragraph used to cite is gone with the Node writer it
served, and so is the Windows junction defect that had grown in a second, hand-rolled copy of that
logic inside `review_server.mjs`.

## 3. Target design

### 3.1 Bundle layout

```
experiment-report-grader/
├─ SKILL.md                  # common frontmatter only
├─ skill.json                # package manifest: version, host roots, dependency classes
├─ agents/
│   └─ openai.yaml           # Codex-only UI metadata and invocation policy
├─ config/
│   ├─ defaults.example.json # committed template
│   └─ local.json            # machine-local, gitignored
├─ assets/ references/ scripts/
└─ scripts/
    ├─ resolve_env.py                 # single path-resolution point
    ├─ bootstrap.py                   # dependency check, OCR helpers, config/local.json
    ├─ install_skill.py               # register into the roots skill.json declares
    ├─ verify_host_registration.mjs   # prove it by running the host's own discovery code
    ├─ package_skill.py               # archive + self-verification of the archive
    ├─ build_review_html.py           # the panel writer (template substitution)
    └─ export_scores.py               # the workbook writer, plus a workbook comparator
```

`skill.json` fields: `skill_version`, `pipeline_version`, `hosts` (supported host → roots),
`requires` (python modules, node, external programs), `optional` (OCR engines, handwriting faces),
`external_paths` (logical name → resolution order). `batch_pipeline.py doctor` should read it so the
manifest and the checks cannot drift apart.

### 3.2 Path resolution contract

One order, applied by `resolve_env.py` for every external path:

1. explicit CLI flag
2. environment variable (`EXPERIMENT_REPORT_GRADER_*`)
3. `config/local.json` (written by bootstrap)
4. bundle-local `assets/`
5. platform candidate probe
6. hard error naming the flag, the variable and the fix

Requirement: `doctor` prints **which** layer answered for each path. A path that resolves by luck
today becomes a support question tomorrow; recording the layer is what makes a migrated setup
debuggable.

### 3.3 Environment variables

`EXPERIMENT_REPORT_GRADER_WORKSPACE` already exists (scratch root). Add, consistent with it:
`_PYTHON`, `_NODE`, `_SIGNATURE_PDF`, `_SCORE_FONT`, `_OCR_EXECUTABLE`,
`_PDFTOPPM_EXECUTABLE`, `_CONFIG`. (`_SPREADSHEET_MODULES` was here too; it went away with the
Node workbook writer.)

### 3.4 Removing host lock-in

Priority order, because it is also the dependency order:

1. **Excel** (blocker 3) — **done, by retiring the Node writer rather than adding a fallback.**
   `openpyxl` is now the only workbook writer, so it is required on every host; `bootstrap.py
   --install` covers it.
2. **HTML** (blocker 5) — **done**: the Python writer is the only one, so node is gone from
   `finalize` entirely.
3. **OCR** (blocker 6) — **done**: `bootstrap.py --ocr` generates the shims, `bootstrap.py` and
   `doctor` both name the engine a machine will use, and on Windows a missing Tesseract stops
   `prepare` instead of degrading silently. See `references/local-ocr-setup.md`.
4. **Write-back assets** (blockers 1, 2) — the last step of the whole workflow, so the most
   expensive place to discover a missing file.
5. **Node and the review service** — the Web panel stays the interactive path. Something has to
   serve HTTP; if node is absent, the static HTML panel remains the archival fallback, and
   `doctor` should say which mode is available rather than implying the panel always works.

### 3.5 Install, package, bootstrap

- `install_skill.py --host auto|agents|dsh|codex|workbuddy|codebuddy|claude [--scope user|project|both] [--copy] [--dry-run] [--uninstall] [--home DIR]`
  — detects which hosts exist, then symlinks (POSIX) or junctions (Windows) the bundle into each
  root. `auto` always includes the shared `~/.agents/skills` and adds a host's own root when that
  host is present. `--home` exists so the whole path can be exercised against a throwaway home
  instead of the real one.
- `package_skill.py --out dist/ [--verify]` — versioned `tar.gz` + `zip` plus a `SHA256SUMS` file,
  excluding student reports, `tmp/`, `__pycache__`, `scripts/ocr-shims/`, `config/local.json`,
  `assets/signature/*.pdf` and `dist/`. `--verify` extracts the archive somewhere temporary and
  requires the required-file list, the exclusions, the neutral-machine checks, and a working
  `doctor` inside the extracted copy.
- **The archive is the delivery channel.** There is no git remote and none is needed: the receiving
  machine unpacks the zip and needs no network, no history and no build step. That is why the
  first-run sequence lives inside the package (README.md for a person, `SKILL.md` →
  "First run on a new machine" for an agent) instead of in a conversation, and why the release
  carries `SHA256SUMS`: a recipient with no git history has nothing else to compare a copied file
  against. The four things that cannot be packaged — the signature template, the handwriting face,
  the OCR helpers and the interpreter's dependencies — are exactly the four that `bootstrap.py`
  reports by name.
- `bootstrap.py [--install] [--ocr] [--write-config] [--all] [--dry-run]` — reports what is missing
  on this machine and, on request, installs the Python dependencies, generates the OCR helpers, and
  records the resolved paths in `config/local.json`. Report-only by default: provisioning a machine
  is an explicit act.

Analogy worth keeping: `local-ocr-setup.md` states the shims are per machine and not portable. The
same sentence applies to the signature template, the handwriting face, the interpreter, and the OCR
helpers — bootstrap exists to make that list finite and explicit.

## 4. Verification

### 4.1 What has been measured

The two capabilities that decide whether P1 is feasible were first prototyped against the completed
实验4 batch (62 reports, already written back) and compared against the artifacts the Codex runtime
produced. The prototypes were untracked scratch and have since been replaced by the tracked writers
(`scripts/build_review_html.py`, `scripts/export_scores.py`). The measurements below are what
justified deleting the Node writers, so they are now the record of *why* only one writer exists;
`doctor --self-test` no longer compares two writers (there is nothing to compare) and instead checks
the single writer against the payload it was given.

| Writer | Test | Result |
|---|---|---|
| `build_review_html.py` (port of `build_review_html.mjs`) | Build the panel from the batch's `reviewjson/` and compare bytes with the shipped `实验报告审核面板.html` | **Byte-identical** — both `dc34af6690bfb75cf495daf381aeb0250d6fac14e9ae43d7475b93f0c437f629` |
| `export_scores.py` (openpyxl replacement for `export_scores.mjs`) | Build the workbook from the same `reviewjson/` and diff it cell by cell against the artifact-tool workbook | **0 differences** across sheet names, freeze panes, grid lines, merged ranges, column widths, row heights, table definitions, and every non-empty cell's value, formula text, number format, font (name/size/bold/colour), fill, borders, alignment and wrap |

Both comparisons were run against one real batch, which is a weak corpus: it turned out to contain
neither a batch without a score offset nor a comment with JavaScript replacement patterns, and each
of those gaps hid a real defect (§4.6). `doctor --self-test` now covers no-offset, offset and
hostile-comment fixtures, the payload round-trip, the batch notices, and the offset actually changing
the sheet.

Two byte-level details mattered for the HTML port and are easy to get wrong: `JSON.stringify`
emits compact JSON with raw non-ASCII, and the `.mjs` escapes every `<` to `\u003c`. Python needs
`separators=(",", ":")` plus `ensure_ascii=False` to match.

Two fidelity details mattered for the workbook: the summary formulas carry no `$` anchors
(`=COUNTIF(K7:K68,"已审核")`, not `=COUNTIF($K$7:$K$68,…)`), and the student ID is written with a
leading zero-width space under an `@` number format so Excel keeps it as text. Both were compared,
not normalised.

### 4.2 What the fallback gives up

Measured, not assumed:

- **Sheet PNG previews** — `review_server.mjs:227,232` renders them into a temp directory and
  deletes it immediately; `batch_pipeline.py:885-895` renders into the work directory, which
  `clean` removes. Previews never reach the deliverable, so dropping them costs nothing.
- **`workbook.inspect` formula scan** — the exporter prints its ndjson to stdout, but
  `finalize` discards `run_checked`'s return value, and the sidecar dumps the tool writes next to
  the workbook are moved out of the result folder (`batch_pipeline.py:895-900`) precisely because
  the result folder is specified to hold only deliverables. Nothing parses them. The fallback
  should instead validate the same arithmetic in Python before export, which is strictly stronger:
  it can fail before a file exists rather than after.

### 4.3 Standing verification

`doctor` gains a **portability** section reporting, per item: resolved value, resolving layer,
ok/missing, and the exact fix command. New self-test: `doctor --self-test` runs prepare → decisions
stub → finalize → `dry_run_annotation.py` against `实验报告_测试样本/实验3`, writes nothing into
source PDFs, and asserts one workbook, one HTML panel and N review JSON files.

### 4.4 Implemented in P1, with its evidence

| Change | Where | Evidence |
|---|---|---|
| One resolution order for every external path | `scripts/resolve_env.py` (new) | `python3 scripts/resolve_env.py` prints the same values as before plus the resolving layer; `doctor` shows `[probe]`/`[env:…]`/`[config:local.json]` |
| Node-free panel and workbook writers | `scripts/build_review_html.py`, `scripts/export_scores.py` (new) | byte-identical panel and 0 workbook differences against the shipped 实验4 artifacts |
| Backend selection, later retired | `batch_pipeline.py` (`--artifact-backend`, `choose_artifact_backend`) | Superseded: keeping two writers meant keeping them byte-identical by hand, so the Node writers were deleted instead. `批次信息.json` still records `artifact_backend` (`python`), and batches finalized earlier that recorded `node` stay readable |
| Write-back assets resolvable, not hardcoded | `writeback_paths.py` gains env/config/bundle/probe resolution | `doctor` reports the same signature and font paths as before, now with their source |
| OCR helpers resolvable | `prepare_grading_packet.ocr_helper_candidates` delegates to `resolve_env` | same `pdftoppm`/`tesseract` resolved as before; shims remain last |
| Panel needs no Node package | `review_server.mjs` (runs the Python writers for both artifacts) | server started on a scratch port, `/api/health` reports `artifact_backend: python`, and a real save regenerated both artifacts |
| Artifact-writer drift guard | `doctor --self-test` | Superseded: with one writer there is nothing to diff, so it checks the writer against its own payload — comments and batch notices read back out of the panel, and the offset actually changing the sheet — across both score-offset variants |
| Newline translation cannot break the panel | `build_review_html.py` writes with `newline=""`; `.gitattributes` pins the template to LF | no longer an equivalence question, since there is one writer, but pinning still keeps the template identical on every checkout — which is what the byte comparison used to police |

The self-test earned its place immediately: end-to-end verification on a batch **without**
`--score-offset` failed with one difference — the Python writer had hardcoded the status/source
column *letters* (`K`/`L`), which are only correct when the adjusted column exists. Every check run
before that used the 实验4 batch, which has `+5` offset, so it passed. Both variants are now covered,
plus a reverse assertion that the offset actually changes the sheet, which a byte comparison between
two writers could never have caught.

Acceptance for "migrated to a new host":

- `bootstrap.py` then `install_skill.py --host auto` then `doctor --self-test` all pass;
- the skill is listed by name in the host's own catalog;
- Excel, HTML, review JSON and a dry-run write-back are produced without the Codex runtime;
- no path in the resolved set points outside the bundle, `config/local.json`, or documented system
  locations.

## 5. Execution plan

**P1 — remove hardcoding and host lock-in** (blockers 1, 2, 3, 4, 5, 6): **done**, see §4.4.
`resolve_env.py` exists, candidate probes are centralized, and the panel and workbook are written by
this skill's own Python scripts. The Node writers were not kept as an alternative path — they were
deleted, along with `--artifact-backend`, `--spreadsheet-modules`, and the runtime setup that linked
`@oai/artifact-tool` into place. `doctor` is green with the Codex runtime present; the half that
matters on a fresh teacher machine — green with the runtime *absent* — is now the only path there is,
so every run exercises it instead of a flag somebody has to remember to pass.

Feasibility was established for the two hardest parts before implementation (§4.1), and the wiring is
in place. The residual risk that used to head this section — two writers drifting apart as the rubric
or the layout changed — went away with the second writer. What remains is the ordinary risk of one
writer being wrong, which `doctor --self-test` now checks against the payload rather than against a
sibling implementation.

**P2 — package and register** (blockers 7, 8 partly): **done**, see §4.5 and the corrections in
§4.6. The definition of done is met with one honest qualification: `bootstrap.py` now exits
non-zero on a fresh machine until the per-machine signature template is configured, so "the
package installs and runs" does not mean "a new machine is ready to write reports back" — that
requires the teacher's own asset, by design.

**P3 — documentation and self-check**: **done** for the `doctor` portability section,
`doctor --self-test`, the rewritten `SKILL.md` invocation example, and the new
`references/portability.md` link; `README.md` carries the same guidance in Chinese.

**P4 — optional**: ship host metadata under `agents/`, and consider plugin-form distribution
(Codex plugin cache, Claude Code `.claude-plugin/plugin.json`) so dependencies travel with the skill.

### 4.5 Implemented in P2, with its evidence

The whole move was rehearsed end to end on this machine, using a throwaway home and a throwaway
extraction directory rather than the real ones:

| Step | Command | Result |
|---|---|---|
| Pack | `package_skill.py --out dist/ --verify` | single top-level directory, required files present, exclusions absent, **no machine-specific home paths**, and the extracted copy checked against a *neutral* home |
| Unpack as another machine | `tar xzf … -C /tmp/newmachine` | `doctor` reports exactly the per-machine gaps: `['tesseract', 'pdftoppm', 'signature template']` — see the correction in §4.6, the first run of this drill reported `ok` because the archive carried a fallback pointing outside itself |
| Provision | `bootstrap.py --all` | generates the OCR helpers and writes `config/local.json`; the signature stays `BLOCK` until it is configured, and `bootstrap` exits non-zero to say so |
| Configure the write-back asset | `EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF=… bootstrap.py` | exits 0 once the signature resolves |
| Prove it works | `doctor --self-test` from the unpacked copy | both writers available and in agreement, including the hostile-comment fixture |
| Register | `verify_host_registration.mjs --home /tmp/newhome --install` | symlink created in the throwaway home; **DSH's own discovery code then returned the skill** |

The discovery check drives the real `@deepseek-ai/dsh-skill-filesystem` provider (registered on a
stub context, no model, no session, no network) rather than trusting the documentation, and it
carries two negative controls so a green result means something: a nested `…/other/nested/SKILL.md`
is **not** discovered, and a bundle whose frontmatter lacks `name` is dropped with the host's own
warning (`skill file … ignored: frontmatter requires name and description`). Discovery returned the
skill twice — `user-dsh` rank 400 and `user-agents` rank 500 — which is the rank order the plan
describes, observed rather than quoted.

Hosts with no local implementation to drive (Codex, WorkBuddy, CodeBuddy, Claude Code here) are
reported as "root layout only". Their roots come from vendor documentation and are recorded in
`skill.json`; `install_skill.py` reads that manifest, so the installer and the documentation cannot
disagree about them.

Note on `agents/openai.yaml`: it stays Codex-only metadata. The shared `SKILL.md` frontmatter is
deliberately kept to `name` + `description` because DSH drops a skill entirely when an optional
invocation value is malformed, so host-specific keys belong in `agents/` and nowhere else.

### 4.6 Corrected after review

Two claims from §4.4/§4.5 did not survive an independent check, and both were cases of a test that
agreed with the implementation instead of with reality. They are recorded here rather than quietly
patched, because the failure mode — verification that cannot fail — is the one worth remembering.

**The Node panel writer corrupted the payload for certain comment text.**
`build_review_html.mjs` used `template.replace(placeholder, embedded)`, and
`String.prototype.replace` treats `$&`, `` $` ``, `$'`, `$1` and `$$` in a *string* replacement as
substitution patterns. A comment containing `$&` re-inserted the placeholder itself, and `` $` ``
spliced the whole document head into the middle of the embedded JSON, leaving a panel whose data no
longer parses. The Python port was unaffected (`str.replace` is literal), so "the two writers are
byte-identical" was true only for the corpora that had been tried — every fixture used benign
comments. Fixed with a function replacement, and `doctor --self-test` now runs a **hostile-comment**
variant (`$&`, `` $` ``, `$'`, `$1`, `__REVIEWS_JSON__`, an embedded `; let active=0;`, a
`<script>` tag, backslashes, tabs and a newline) and, separately, reads the payload back out of the
generated panel and requires it to match the input. The round-trip check is deliberately independent
of byte comparison, which passes whenever two writers are wrong the same way — and it is the check
that outlived the comparison, since the Node writer has since been deleted. Reintroducing the
original one-liner makes the self-test fail with two checks, which was verified by running the new
fixture before applying the fix.

**The package was not self-contained, and the verification could not see it.**
`resolve_env` kept the author's signature path as a last-resort default, so the constant travelled
inside the archive: an unpacked copy on any other machine resolved the template to a path that only
exists on the build machine. `package_skill --verify` ran its `doctor` with the *builder's* home, so
it reported success. Three things changed:

- the out-of-bundle signature fallback is gone. Resolution is flag → env → `config/local.json` →
  `assets/signature/` → unresolved. The signature is now a **per-machine asset**, like OCR helpers:
  `bootstrap.py` lists it (and the font, OCR helper and pdftoppm) with the layer that answered and
  the ways to supply it, marks it `BLOCK` because the approved write-back needs it, and exits
  non-zero while it is unresolved — so a machine that would grade a batch and then refuse to write
  it back says so up front;
- `--verify` now checks the extracted copy under an **empty `HOME` and no config**, asserts the
  signature is unresolved there, asserts no per-machine asset resolves out of a personal home
  directory, and asserts that everything `doctor` calls missing is a documented per-machine item.
  It also scans the shipped files for machine-specific home paths, which is what would have caught
  the baked-in constant;
- the signature is excluded from archives (`assets/signature/*.pdf`, alongside `config/local.json`)
  and `.gitignore`d, so preferring a local copy stays a single-machine convenience instead of a
  distribution decision made on the teacher's behalf.

What the corrected drill shows: a fresh unpack reports `['tesseract', 'pdftoppm', 'signature
template']` and a blocking bootstrap exit, and stops reporting that once the signature is
configured — which is the honest version of "the packaged skill works on a new machine".

## 6. Open questions

- **WorkBuddy vs CodeBuddy roots.** Verified from vendor docs (`~/.workbuddy/skills`) and a
  WorkBuddy skill-development article, not from a local install — neither `~/.workbuddy` nor
  `~/.codebuddy` exists on this machine. `install_skill.py` should probe both rather than assume.
- **Codex user root.** Official docs say `~/.agents/skills`; `~/.codex/skills` exists locally with
  installed skills, and the ecosystem still defaults there (see openai/skills#420,
  oh-my-codex#836). Register in both, or in `~/.agents/skills` plus a documented opt-in.
- **Openpyxl output in real Excel.** Equivalence was measured cell by cell with openpyxl reading
  both files (§4.1), which covers everything a teacher sees but cannot prove that Excel recalculates
  the formulas or renders the tables identically. A one-off check in Excel/LibreOffice after P1
  wires the fallback in is still worth doing.
- **Signature and font in a shared bundle.** Resolved for the signature (§4.6): it is per-machine
  configuration, reported as a blocking item, and excluded from archives. A *licensed* handwriting
  face bundled with a distributed archive is still an open question — the current default probes the
  operating system and falls back to whatever exists, and a bundled face under `assets/fonts/` would
  need a licence check first.
- **Grading semantics are out of scope.** Nothing in this plan changes rubrics, bands, budgets or
  scoring behavior. A migration must be verifiable as behavior-preserving, which is what the
  `--self-test` dry run is for.

## References

- DSH skill discovery: `@deepseek-ai/dsh-skill-filesystem` (`README.zh.md` roots table, rank order,
  one-level rule, frontmatter validation).
- Codex: <https://developers.openai.com/codex/skills>; `agents/openai.yaml` field reference in the
  `openai/codex` repository.
- WorkBuddy / CodeBuddy: <https://www.workbuddy.cn/docs/cli/skills>.
- Claude Code: <https://code.claude.com/docs/en/skills> (location table, symlink support,
  frontmatter reference).
