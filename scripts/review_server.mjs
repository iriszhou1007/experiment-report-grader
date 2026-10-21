#!/usr/bin/env node
// Local-only review service: persist drafts and generate approved grading artifacts.

import crypto from "node:crypto";
import fs from "node:fs/promises";
import fsSync from "node:fs";
import http from "node:http";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const runFile = promisify(execFile);

function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    if (!key?.startsWith("--") || argv[index + 1] === undefined) throw new Error(`Invalid argument near ${key}`);
    out[key.slice(2)] = argv[index + 1];
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
// Everything the panel writes -- the review JSON, the HTML, the workbook and the PDF write-back --
// is produced by this skill's own Python scripts. Node is the panel's own runtime, not an artifact
// dependency, so there is no writer to select here.
for (const required of ["workspace", "python"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}

const workspace = path.resolve(args.workspace);
const initialBatchId = args["run-id"] ?? "default-batch";
const port = Number(args.port ?? 8765);
const host = "127.0.0.1";
const python = path.resolve(args.python);
const pdftoppm = args.pdftoppm ? path.resolve(args.pdftoppm) : null;
const signaturePdf = args["signature-pdf"] ? path.resolve(args["signature-pdf"]) : null;
const readyFile = path.resolve(args["ready-file"] ?? path.join(workspace, "tmp", "review-server.ready.json"));
// Batch whose result folder (batch_id + "_批改结果") already exists: open it directly instead of
// starting on the bundled sample batch, so the panel shows the reviews that were just generated.
const openBatch = args["batch"] ?? args["batch-dir"] ?? null;

// The skill normally sits beside the workspace as <workspace>/experiment-report-grader, but it
// can live anywhere (e.g. a git checkout), so --skill-dir overrides the convention.
const skillDir = args["skill-dir"]
  ? path.resolve(args["skill-dir"])
  : path.join(workspace, "experiment-report-grader");
const serviceRoot = path.join(workspace, "tmp", "review-server-ui");
const registryPath = path.join(serviceRoot, "active-batch.json");
const htmlPath = path.join(serviceRoot, "实验报告审核面板.html");
let batch = null;
let reviewDir = null;
let outputDir = null;
let workbookPath = null;
let originalBackupDir = null;
let batchHtmlPath = null;
const tempRoot = path.join(workspace, "tmp", "review-server");
const htmlBuilder = path.join(skillDir, "scripts", "build_review_html.py");
const spreadsheetExporter = path.join(skillDir, "scripts", "export_scores.py");
const insertFeedback = path.join(skillDir, "scripts", "insert_feedback_page.py");
const materializer = path.join(skillDir, "scripts", "materialize_reviews.py");
const token = crypto.randomBytes(24).toString("hex");
let operation = Promise.resolve();

function safeBatchId(value) {
  const cleaned = String(value ?? "").trim().replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").replace(/[. ]+$/g, "");
  if (!cleaned || cleaned === "_审核服务" || cleaned === "." || cleaned === "..") throw new Error("请输入有效的实验批次名称");
  return cleaned.slice(0, 80);
}

function applyBatch(next) {
  batch = next;
  outputDir = path.join(batch.report_dir, `${batch.batch_id}_批改结果`);
  reviewDir = path.join(outputDir, "reviewjson");
  originalBackupDir = path.join(outputDir, "原始数据");
  batchHtmlPath = path.join(outputDir, "实验报告审核面板.html");
  workbookPath = path.join(outputDir, "实验报告成绩汇总.xlsx");
}

async function readReviews() {
  await fs.mkdir(reviewDir, { recursive: true });
  const names = (await fs.readdir(reviewDir)).filter((name) => name.toLowerCase().endsWith(".review.json")).sort();
  const reviews = [];
  for (const name of names) reviews.push({ ...JSON.parse(await fs.readFile(path.join(reviewDir, name), "utf8")), __review_file: name });
  return reviews;
}

async function selectBatch(reportDirInput, batchName, inspectionModeInput = "text_only", autoUpgradeVisualInput = false) {
  const reportDir = path.resolve(String(reportDirInput ?? ""));
  const stat = await fs.stat(reportDir);
  if (!stat.isDirectory()) throw new Error("报告地址不是文件夹");
  const pdfFiles = (await fs.readdir(reportDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".pdf"))
    .map((entry) => entry.name)
    .sort();
  const batchId = safeBatchId(batchName || path.basename(reportDir));
  const inspectionMode = ["text_only", "smart", "full_visual"].includes(inspectionModeInput) ? inspectionModeInput : "text_only";
  const autoUpgradeVisual = autoUpgradeVisualInput !== false;
  const next = {
    batch_id: batchId,
    batch_name: String(batchName || path.basename(reportDir)),
    report_dir: reportDir,
    pdf_count: pdfFiles.length,
    pdf_files: pdfFiles,
    inspection_mode: inspectionMode,
    auto_upgrade_visual: autoUpgradeVisual,
    score_increment: 1,
    updated_at: new Date().toISOString(),
  };
  applyBatch(next);
  await fs.mkdir(reviewDir, { recursive: true });
  await fs.mkdir(serviceRoot, { recursive: true });
  await fs.writeFile(path.join(outputDir, "批次信息.json"), JSON.stringify(next, null, 2), "utf8");
  await fs.writeFile(registryPath, JSON.stringify(next, null, 2), "utf8");
  return { ...next, result_dir: outputDir, review_count: (await readReviews()).length };
}

// Open a batch whose reviews were produced by batch_pipeline.py, without re-scanning the PDFs.
// Returns true when the given path refers to a usable result folder.
async function openExistingBatch(batchDirInput) {
  const batchDir = path.resolve(String(batchDirInput ?? ""));
  let stat;
  try {
    stat = await fs.stat(batchDir);
  } catch {
    return false;
  }
  if (!stat.isDirectory()) return false;
  const reviewDirCandidate = path.join(batchDir, "reviewjson");
  let reviewNames;
  try {
    reviewNames = (await fs.readdir(reviewDirCandidate)).filter((name) => name.toLowerCase().endsWith(".review.json"));
  } catch {
    return false;
  }
  const infoPath = path.join(batchDir, "批次信息.json");
  let info = {};
  try {
    info = JSON.parse(await fs.readFile(infoPath, "utf8"));
  } catch {
    info = {};
  }
  const parent = path.dirname(batchDir);
  const suffix = "_批改结果";
  const folderName = path.basename(batchDir);
  const batchId = safeBatchId(info.batch_id || (folderName.endsWith(suffix) ? folderName.slice(0, -suffix.length) : folderName));
  const reportDir = path.resolve(info.report_dir || path.join(parent, batchId));
  const pdfFiles = (info.pdf_files?.length ? info.pdf_files : await fs.readdir(reportDir).catch(() => []))
    .filter((name) => String(name).toLowerCase().endsWith(".pdf"))
    .sort();
  const next = {
    batch_id: batchId,
    batch_name: String(info.batch_name || batchId),
    report_dir: reportDir,
    pdf_count: pdfFiles.length,
    pdf_files: pdfFiles,
    inspection_mode: info.inspection_mode || "smart",
    auto_upgrade_visual: info.auto_upgrade_visual !== false,
    score_increment: Number(info.score_increment ?? 1),
    // Settings-level notes written by finalize; the panel shows them once above the report list.
    batch_notices: Array.isArray(info.batch_notices) ? info.batch_notices : [],
    updated_at: new Date().toISOString(),
  };
  applyBatch(next);
  await fs.mkdir(serviceRoot, { recursive: true });
  await fs.writeFile(registryPath, JSON.stringify(next, null, 2), "utf8");
  return reviewNames.length > 0;
}

async function reportFolders() {
  const current = path.resolve(batch.report_dir);
  const courseRoot = path.dirname(current);
  const folders = [];
  for (const entry of await fs.readdir(courseRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const candidate = path.join(courseRoot, entry.name);
    let pdfCount = 0;
    try {
      pdfCount = (await fs.readdir(candidate, { withFileTypes: true }))
        .filter((item) => item.isFile() && item.name.toLowerCase().endsWith(".pdf")).length;
    } catch {
      continue;
    }
    if (pdfCount) folders.push({ name: entry.name, path: candidate, pdf_count: pdfCount, current: candidate === current });
  }
  if (!folders.some((folder) => folder.path === current)) {
    const pdfCount = (await fs.readdir(current, { withFileTypes: true }))
      .filter((item) => item.isFile() && item.name.toLowerCase().endsWith(".pdf")).length;
    folders.push({ name: path.basename(current), path: current, pdf_count: pdfCount, current: true });
  }
  return folders.sort((left, right) => left.name.localeCompare(right.name, "zh-CN", { numeric: true }));
}

let restored = false;
if (openBatch) restored = await openExistingBatch(openBatch).catch(() => false);
if (!restored) {
  try {
    applyBatch(JSON.parse(await fs.readFile(registryPath, "utf8")));
  } catch {
    const fallbackReportDir = path.join(workspace, "实验报告样本");
    await selectBatch(fallbackReportDir, initialBatchId);
  }
}

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function sendJson(response, status, data) {
  const body = Buffer.from(JSON.stringify(data), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

function sendError(response, status, message) {
  sendJson(response, status, { ok: false, error: String(message) });
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 2_000_000) throw new Error("Request body is too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function safeReviewPath(reviewFile) {
  if (typeof reviewFile !== "string" || path.basename(reviewFile) !== reviewFile || !reviewFile.endsWith(".json")) {
    throw new Error("Invalid review file name");
  }
  return path.join(reviewDir, reviewFile);
}

function validateReview(review) {
  if (!review || typeof review !== "object" || !Array.isArray(review.items) || review.items.length === 0) {
    throw new Error("Review items are required");
  }
  const scores = review.items.map((item) => Number(item.score));
  const maxima = review.items.map((item) => Number(item.max_score));
  if (scores.some((score, index) => !Number.isFinite(score) || score < 0 || score > maxima[index])) {
    throw new Error("Each score must be between zero and its maximum");
  }
  const total = scores.reduce((sum, value) => sum + value, 0);
  const maximum = maxima.reduce((sum, value) => sum + value, 0);
  if (Math.abs(total - Number(review.total_score)) > 0.01 || Math.abs(maximum - Number(review.max_score)) > 0.01) {
    throw new Error("Item scores or maxima do not match the totals");
  }
  if (!review.source_file || !review.source_sha256 || !review.student_name || !review.student_id) {
    throw new Error("Source identity and student identity are required");
  }
  const source = path.resolve(workspace, String(review.source_file));
  if (!inside(path.resolve(batch.report_dir), source) || path.extname(source).toLowerCase() !== ".pdf") {
    throw new Error("Source PDF is outside the selected report folder");
  }
  return source;
}

// Both artifacts come from this skill's own Python writers, so the panel needs no Node package
// and no node_modules link. The Node variant that used to be chosen here required
// @oai/artifact-tool from the Codex runtime, and its runtime setup was where the Windows junction
// check went wrong.
async function runArtifacts(tempHtml, tempWorkbook) {
  await runFile(python, [htmlBuilder, reviewDir, tempHtml], {
    cwd: workspace,
    windowsHide: true,
    maxBuffer: 8 * 1024 * 1024,
  });
  await runFile(python, [spreadsheetExporter, reviewDir, tempWorkbook], {
    cwd: workspace,
    windowsHide: true,
    maxBuffer: 8 * 1024 * 1024,
  });
  await refreshIndex();
}

// index.md is otherwise only rewritten by `finalize`, so after an in-panel approval it would
// keep reporting 待审核 while the workbook already says 已审核.
async function refreshIndex() {
  if (!outputDir) return;
  const tempIndex = path.join(tempRoot, `index-${crypto.randomUUID()}.md`);
  await runFile(
    python,
    [materializer, "--index-only", "--output-dir", reviewDir, "--index-out", tempIndex],
    { cwd: workspace, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
  );
  await replaceFile(tempIndex, path.join(outputDir, "index.md"));
}

async function fileSize(filePath) {
  const stats = await fs.stat(filePath);
  return stats.size;
}

/**
 * Replace a file without ever leaving a half-written target behind.
 *
 * A direct copyFile over the target is not atomic: an interrupt — Ctrl+C, a closed
 * window, a killed server — mid-copy leaves a truncated file, which for a student PDF
 * means a corrupted submission. Copy into the target's own directory first, verify the
 * bytes landed, then rename, because rename within one filesystem is atomic.
 */
async function replaceFile(tempPath, finalPath) {
  await fs.mkdir(path.dirname(finalPath), { recursive: true });
  const staging = `${finalPath}.staging-${crypto.randomUUID()}`;
  try {
    await fs.copyFile(tempPath, staging);
    const [sourceSize, stagingSize] = await Promise.all([fileSize(tempPath), fileSize(staging)]);
    if (sourceSize !== stagingSize) {
      throw new Error(`staged copy is ${stagingSize} bytes but ${sourceSize} were expected`);
    }
    if (stagingSize === 0) throw new Error("staged copy is empty");
    await fs.rename(staging, finalPath);
  } catch (error) {
    await fs.rm(staging, { force: true });
    throw error;
  }
  await fs.rm(tempPath, { force: true });
}

async function saveReview(reviewFile, incoming, approve) {
  const reviewPath = safeReviewPath(reviewFile);
  const originalText = await fs.readFile(reviewPath, "utf8");
  const original = JSON.parse(originalText);
  if (String(incoming.source_sha256).toLowerCase() !== String(original.source_sha256).toLowerCase()) {
    throw new Error("Source hash in the submitted review does not match the current draft");
  }
  const review = structuredClone(incoming);
  delete review.__review_file;
  delete review.__graded_pdf_url;
  for (const key of ["schema_version", "source_file", "source_sha256", "student_id", "student_name", "experiment", "rubric_id", "max_score", "score_offset"]) {
    review[key] = original[key];
  }
  review.adjudication_status = original.adjudication_status;
  review.regrade_dispute = original.regrade_dispute;
  if (approve && original.adjudication_status === "required" && incoming.adjudication_acknowledged !== true) {
    throw new Error("This report has an unresolved blind-regrade dispute; confirm the adjudication before approval");
  }
  if (!Array.isArray(original.items) || review.items.length !== original.items.length) {
    throw new Error("Rubric item count cannot be changed in the review page");
  }
  review.items = review.items.map((item, index) => {
    const originalItem = original.items[index];
    const scoreUnchanged = Number(item.score) === Number(originalItem.score);
    const stableMetadata = scoreUnchanged && originalItem.score_rule
      ? {
          score_rule: originalItem.score_rule,
          score_rule_description: originalItem.score_rule_description,
        }
      : {
          score_rule: undefined,
          score_rule_description: undefined,
          teacher_override: true,
        };
    return {
      ...item,
      criterion: originalItem.criterion,
      max_score: originalItem.max_score,
      evidence: originalItem.evidence,
      visual_evidence: originalItem.visual_evidence,
      deductions: originalItem.deductions,
      ...stableMetadata,
    };
  });
  review.total_score = review.items.reduce((sum, item) => sum + Number(item.score), 0);
  // A course-level offset must follow an in-panel edit, otherwise the reported
  // adjusted figure would describe the pre-edit score.
  const scoreOffset = Number(review.score_offset);
  if (Number.isFinite(scoreOffset) && scoreOffset > 0) {
    const cap = Number(review.max_score);
    review.adjusted_total = Number.isFinite(cap)
      ? Math.min(cap, review.total_score + scoreOffset)
      : review.total_score + scoreOffset;
  } else {
    delete review.adjusted_total;
  }
  review.review_status = approve ? "approved" : "draft";
  review.approved_at = approve ? new Date().toISOString() : null;
  if (approve && original.adjudication_status === "required") {
    review.adjudication_status = "resolved";
    review.adjudicated_at = review.approved_at;
    review.adjudication_acknowledged = true;
  }
  const source = validateReview(review);
  await fs.mkdir(tempRoot, { recursive: true });
  const id = crypto.randomUUID();
  const tempHtml = path.join(tempRoot, `${id}.html`);
  const tempWorkbook = path.join(tempRoot, `${id}.xlsx`);
  const tempReview = `${reviewPath}.${id}.tmp`;
  const originalBackup = path.join(originalBackupDir, `原始备份_${path.basename(source)}`);
  const tempPdf = path.join(tempRoot, `${id}.pdf`);
  const tempCurrentSource = path.join(tempRoot, `${id}.current.pdf`);
  let sourceReplaced = false;
  let committed = false;
  try {
    await fs.writeFile(tempReview, JSON.stringify(review, null, 2) + "\n", "utf8");
    await replaceFile(tempReview, reviewPath);
    if (approve) {
      await fs.mkdir(originalBackupDir, { recursive: true });
      try {
        await fs.access(originalBackup);
      } catch {
        await fs.copyFile(source, originalBackup);
      }
      review.source_modified_at = new Date().toISOString();
      // Absolute and realpath-resolved on purpose. `path.relative(workspace, backup)` produced
      // nonsense such as ../../private/tmp/... because macOS symlinks /tmp to /private/tmp while
      // `workspace` stays unresolved; on top of that, resolve_backup() reads a *relative* value
      // against the Python side's WORKSPACE_DIR, which need not be this server's --workspace.
      // An absolute path has neither ambiguity, and resolve_backup's fallback candidate still
      // covers a result folder that later gets moved.
      review.original_backup = await fs.realpath(originalBackup);
      await fs.writeFile(reviewPath, JSON.stringify(review, null, 2) + "\n", "utf8");
      const pdfResult = await runFile(
        python,
        [
          insertFeedback,
          "--source",
          originalBackup,
          "--review",
          reviewPath,
          "--output",
          tempPdf,
          "--approved",
          ...(signaturePdf ? ["--signature-pdf", signaturePdf] : []),
        ],
        {
          cwd: workspace,
          windowsHide: true,
          maxBuffer: 8 * 1024 * 1024,
        },
      );
      const pdfSummary = JSON.parse(pdfResult.stdout.trim().split(/\r?\n/).at(-1));
      // Record what the write-back actually did, with the same field names approve_batch.py uses.
      // Without it a panel-approved report carries no writeback provenance, and
      // audit_written_batch.py — which reads feedback_page / review_page_in_output / total_pages
      // from here — reported a batch that was in fact correct as broken (wrong page count,
      // checkmarks "unexpected", "signature page has no ink").
      const annotation = pdfSummary.annotation ?? {};
      review.writeback = {
        feedback_page: pdfSummary.feedback_page ?? null,
        review_page_in_output: pdfSummary.review_page_in_output ?? null,
        source_pages: pdfSummary.source_pages ?? null,
        total_pages: pdfSummary.total_pages ?? null,
        score_written: annotation.score?.score_text ?? null,
        signature_ink_annotations: annotation.signature_ink_annotations ?? null,
        checkmarked_pages: (annotation.checkmarked_pages ?? []).length,
        unmarked_pages: annotation.unmarked_pages ?? null,
        seed: annotation.seed ?? null,
      };
      await fs.writeFile(reviewPath, JSON.stringify(review, null, 2) + "\n", "utf8");
      if (pdftoppm) {
        // Render the sheet where the generator says it landed — it is prepended, not
        // appended — and the review page too, since that page now carries the checkmark,
        // the signature and the score.
        const feedbackPage = Number(pdfSummary.feedback_page ?? pdfSummary.total_pages ?? 1);
        const reviewPage = Number(pdfSummary.review_page_in_output ?? 0);
        for (const [label, pageNumber] of [
          ["feedback", feedbackPage],
          ["review", reviewPage],
        ]) {
          if (!Number.isFinite(pageNumber) || pageNumber < 1) continue;
          const renderPrefix = path.join(tempRoot, `${id}-${label}`);
          await runFile(
            pdftoppm,
            [
              "-f",
              String(pageNumber),
              "-l",
              String(pageNumber),
              "-singlefile",
              "-png",
              "-r",
              "96",
              tempPdf,
              renderPrefix,
            ],
            { cwd: workspace, windowsHide: true },
          );
          const rendered = `${renderPrefix}.png`;
          const renderedStat = await fs.stat(rendered);
          if (renderedStat.size < 1024) {
            throw new Error(`Generated PDF smoke render for the ${label} page is too small`);
          }
          await fs.rm(rendered, { force: true });
        }
      }
    }
    await runArtifacts(tempHtml, tempWorkbook);
    if (approve) {
      await fs.copyFile(source, tempCurrentSource);
      await replaceFile(tempPdf, source);
      sourceReplaced = true;
    }
    await fs.mkdir(path.dirname(batchHtmlPath), { recursive: true });
    await fs.copyFile(tempHtml, batchHtmlPath);
    await replaceFile(tempHtml, htmlPath);
    await replaceFile(tempWorkbook, workbookPath);
    committed = true;
    return {
      ok: true,
      action: approve ? "approved" : "saved",
      review: {
        ...review,
        __review_file: reviewFile,
      },
      workbook: path.basename(workbookPath),
      source_pdf_url: approve ? `/api/pdf?review_file=${encodeURIComponent(reviewFile)}&v=${Date.now()}` : null,
      source_pdf_updated: approve,
    };
  } finally {
    if (!committed) {
      await fs.writeFile(reviewPath, originalText, "utf8");
      if (sourceReplaced) await fs.copyFile(tempCurrentSource, source);
    }
    await Promise.all([tempReview, tempHtml, tempWorkbook, tempPdf, tempCurrentSource].map((file) => fs.rm(file, { force: true })));
  }
}

async function sourceForReview(reviewFile) {
  const review = JSON.parse(await fs.readFile(safeReviewPath(reviewFile), "utf8"));
  return validateReview(review);
}

// The panel HTML is regenerated on every save, so it normally lives under serviceRoot. When the
// service is started against an already-finalized batch, the builder has not run yet: serve the
// batch's own panel so the instructor sees the existing reviews immediately.
async function panelHtmlPath() {
  for (const candidate of [htmlPath, batchHtmlPath]) {
    if (!candidate) continue;
    try {
      if ((await fs.stat(candidate)).isFile()) return candidate;
    } catch {
      continue;
    }
  }
  return htmlPath;
}

async function serveFile(request, response, filePath, contentType) {
  const stat = await fs.stat(filePath);
  const range = request.headers.range;
  response.setHeader("Accept-Ranges", "bytes");
  response.setHeader("Content-Type", contentType);
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  if (range) {
    const match = /^bytes=(\d+)-(\d*)$/.exec(range);
    if (!match) return sendError(response, 416, "Invalid range");
    const start = Number(match[1]);
    const end = match[2] ? Math.min(Number(match[2]), stat.size - 1) : stat.size - 1;
    if (start > end || start >= stat.size) return sendError(response, 416, "Range outside file");
    response.writeHead(206, { "Content-Range": `bytes ${start}-${end}/${stat.size}`, "Content-Length": end - start + 1 });
    fsSync.createReadStream(filePath, { start, end }).pipe(response);
  } else {
    response.writeHead(200, { "Content-Length": stat.size });
    fsSync.createReadStream(filePath).pipe(response);
  }
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://${host}:${port}`);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return serveFile(request, response, await panelHtmlPath(), "text/html; charset=utf-8");
    }
    if (request.method === "GET" && url.pathname === "/api/config") {
      return sendJson(response, 200, { ok: true, mode: "local-server", token, batch: { ...batch, result_dir: outputDir, review_count: (await readReviews()).length } });
    }
    if (request.method === "GET" && url.pathname === "/api/health") {
      return sendJson(response, 200, { ok: true, workspace, batch_id: batch.batch_id, artifact_backend: "python" });
    }
    if (request.method === "GET" && url.pathname === "/api/reviews") {
      return sendJson(response, 200, { ok: true, reviews: await readReviews() });
    }
    if (request.method === "GET" && url.pathname === "/api/report-folders") {
      return sendJson(response, 200, { ok: true, folders: await reportFolders() });
    }
    if (request.method === "GET" && url.pathname === "/api/pdf") {
      return serveFile(request, response, await sourceForReview(url.searchParams.get("review_file")), "application/pdf");
    }
    if (request.method === "GET" && url.pathname === "/api/graded-pdf") {
      return serveFile(request, response, await sourceForReview(url.searchParams.get("review_file")), "application/pdf");
    }
    if (request.method === "POST" && url.pathname === "/api/review") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      if (!['save_draft', 'approve_and_generate'].includes(body.action)) return sendError(response, 400, "Invalid action");
      const task = () => saveReview(body.review_file, body.review, body.action === "approve_and_generate");
      operation = operation.then(task, task);
      return sendJson(response, 200, await operation);
    }
    if (request.method === "POST" && url.pathname === "/api/batch/select") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      const selected = await selectBatch(body.report_dir, body.batch_name, body.inspection_mode, body.auto_upgrade_visual);
      return sendJson(response, 200, { ok: true, batch: selected, reviews: await readReviews() });
    }
    sendError(response, 404, "Not found");
  } catch (error) {
    sendError(response, 500, error?.message ?? error);
  }
});

server.listen(port, host, async () => {
  await fs.mkdir(path.dirname(readyFile), { recursive: true });
  await fs.writeFile(readyFile, JSON.stringify({ pid: process.pid, url: `http://${host}:${port}/`, workspace, batch_id: batch.batch_id }, null, 2), "utf8");
  console.log(`Review server ready at http://${host}:${port}/`);
});
