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
const signatureValue = args["signature-file"] ?? args["signature-pdf"];
const signaturePdf = signatureValue ? path.resolve(signatureValue) : null;
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
const submissionPreflight = path.join(skillDir, "scripts", "submission_preflight.py");
const spreadsheetExporter = path.join(skillDir, "scripts", "export_scores.py");
const insertFeedback = path.join(skillDir, "scripts", "insert_feedback_page.py");
const materializer = path.join(skillDir, "scripts", "materialize_reviews.py");
const batchPipeline = path.join(skillDir, "scripts", "batch_pipeline.py");
const token = crypto.randomBytes(24).toString("hex");
let operation = Promise.resolve();
const writebackPreviews = new Map();

function previewFingerprint(reviewFile, review, options) {
  return crypto.createHash("sha256").update(JSON.stringify({ reviewFile, review, options })).digest("hex");
}

async function fileSha256(filePath) {
  return crypto.createHash("sha256").update(await fs.readFile(filePath)).digest("hex");
}

function writebackFlags(options, seed) {
  return [
    "--feedback-position", options.feedbackPosition === "last" ? "last" : "first",
    "--seed", String(seed),
    ...(options.checkmarks ? [] : ["--no-checkmarks"]),
    ...(options.signature ? [] : ["--no-signature"]),
    ...(options.score ? [] : ["--no-score"]),
    ...(options.handwrittenComments ? ["--handwritten-comments"] : []),
  ];
}

async function createWritebackPreview(reviewFile, incoming, options = {}) {
  const reviewPath = safeReviewPath(reviewFile);
  const stored = JSON.parse(await fs.readFile(reviewPath, "utf8"));
  if (String(incoming.source_sha256).toLowerCase() !== String(stored.source_sha256).toLowerCase()) throw new Error("Source hash mismatch");
  if (stored.submission_status) throw new Error("该报告正在等待补交或重新评分，不能批准旧评分");
  if (stored.adjudication_status === "required" && incoming.adjudication_acknowledged !== true) throw new Error("请先确认评分裁决");
  const normalizedOptions = {
    handwrittenComments: options.handwrittenComments === true,
    checkmarks: options.checkmarks !== false,
    signature: options.signature !== false,
    score: options.score !== false,
    feedbackPosition: options.feedbackPosition === "last" ? "last" : "first",
  };
  const source = validateReview({ ...stored, ...incoming, source_file: stored.source_file, source_sha256: stored.source_sha256, student_name: stored.student_name, student_id: stored.student_id });
  const backup = path.join(originalBackupDir, `原始备份_${path.basename(source)}`);
  let inputSource = source;
  try { await fs.access(backup); inputSource = backup; } catch {}
  const previewReview = { ...stored, ...incoming, schema_version: stored.schema_version,
    source_file: stored.source_file, source_sha256: stored.source_sha256, student_name: stored.student_name,
    student_id: stored.student_id, experiment: stored.experiment, rubric_id: stored.rubric_id,
    max_score: stored.max_score, score_offset: stored.score_offset,
    review_status: "approved", approved_at: new Date().toISOString(), adjudication_status: stored.adjudication_status,
    items: incoming.items.map((item, index) => ({ ...stored.items[index], ...item, criterion: stored.items[index].criterion,
      max_score: stored.items[index].max_score, evidence: stored.items[index].evidence })) };
  previewReview.total_score = previewReview.items.reduce((sum, item) => sum + Number(item.score), 0);
  const offset = Number(previewReview.score_offset);
  if (Number.isFinite(offset) && offset > 0) previewReview.adjusted_total = Math.min(Number(previewReview.max_score), previewReview.total_score + offset);
  const id = crypto.randomUUID();
  const seed = crypto.randomInt(1, 1 << 30);
  const reviewTemp = path.join(tempRoot, `${id}.review.json`);
  const pdfTemp = path.join(tempRoot, `${id}.preview.pdf`);
  await fs.mkdir(tempRoot, { recursive: true });
  try {
    await fs.writeFile(reviewTemp, JSON.stringify(previewReview), "utf8");
    const result = await runFile(python, [insertFeedback, "--source", inputSource, "--review", reviewTemp, "--output", pdfTemp,
      "--approved", ...writebackFlags(normalizedOptions, seed), ...(normalizedOptions.signature && signaturePdf ? ["--signature-pdf", signaturePdf] : [])],
    { cwd: workspace, windowsHide: true, maxBuffer: 8 * 1024 * 1024 });
    const summary = JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1));
    const fingerprint = previewFingerprint(reviewFile, incoming, normalizedOptions);
    const timer = setTimeout(async () => { writebackPreviews.delete(id); await fs.rm(pdfTemp, { force: true }); }, 15 * 60 * 1000);
    timer.unref?.();
    writebackPreviews.set(id, { reviewFile, fingerprint, options: normalizedOptions, seed, pdfTemp, timer });
    return { ok: true, preview_id: id, preview_url: `/api/writeback-preview/${id}`, summary };
  } catch (error) {
    await fs.rm(pdfTemp, { force: true });
    throw error;
  } finally { await fs.rm(reviewTemp, { force: true }); }
}

async function discardWritebackPreview(id) {
  const preview = writebackPreviews.get(id);
  if (!preview) return;
  clearTimeout(preview.timer);
  writebackPreviews.delete(id);
  await fs.rm(preview.pdfTemp, { force: true });
}

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

async function scanSubmissions(reportDir) {
  const { stdout } = await runFile(python, [submissionPreflight, reportDir], {
    encoding: "utf8",
    env: { ...process.env, PYTHONUTF8: "1" },
    maxBuffer: 10 * 1024 * 1024,
  });
  const result = JSON.parse(stdout);
  return Array.isArray(result.entries) ? result.entries : [];
}

async function selectBatch(reportDirInput, batchName, inspectionModeInput = "text_only", autoUpgradeVisualInput = false) {
  const reportDir = path.resolve(String(reportDirInput ?? ""));
  const stat = await fs.stat(reportDir);
  if (!stat.isDirectory()) throw new Error("报告地址不是文件夹");
  const pdfFiles = (await fs.readdir(reportDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".pdf"))
    .map((entry) => entry.name)
    .sort();
  const submissionPreflight = await scanSubmissions(reportDir);
  const batchId = safeBatchId(batchName || path.basename(reportDir));
  const previousPreflight = path.resolve(batch?.report_dir ?? "") === reportDir && batch?.batch_id === batchId
    ? (batch?.submission_preflight ?? [])
    : [];
  const priorReturns = previousPreflight
    .filter((entry) => String(entry.status ?? "").includes("需重新提交") && entry.student_id)
    .map((entry) => `${entry.student_id}|${String(entry.experiment ?? "").toLowerCase()}`);
  for (const entry of submissionPreflight) {
    if (entry.file_type !== "PDF" || !entry.included_in_grading) continue;
    const key = `${entry.student_id}|${String(entry.experiment ?? "").toLowerCase()}`;
    if (!priorReturns.includes(key)) continue;
    entry.status = "已重新提交";
    entry.resubmitted = true;
    entry.reasons = [...(entry.reasons ?? []), "已检测到此前退回报告的 PDF 补交，可开始重新评分"];
  }
  const inspectionMode = ["text_only", "smart", "full_visual"].includes(inspectionModeInput) ? inspectionModeInput : "text_only";
  const autoUpgradeVisual = autoUpgradeVisualInput !== false;
  const resultDir = path.join(reportDir, `${batchId}_批改结果`);
  let savedBatchInfo = {};
  try { savedBatchInfo = JSON.parse(await fs.readFile(path.join(resultDir, "批次信息.json"), "utf8")); }
  catch {}
  const next = {
    ...savedBatchInfo,
    batch_id: batchId,
    batch_name: String(batchName || path.basename(reportDir)),
    report_dir: reportDir,
    pdf_count: pdfFiles.length,
    eligible_pdf_count: submissionPreflight.filter((entry) => entry.file_type === "PDF" && entry.included_in_grading).length,
    pdf_files: pdfFiles,
    submission_preflight: submissionPreflight,
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
    pdf_count: Number(info.submitted_pdf_count ?? info.pdf_count ?? pdfFiles.length),
    eligible_pdf_count: Number(info.eligible_pdf_count ?? info.pdf_count ?? pdfFiles.length),
    pdf_files: pdfFiles,
    inspection_mode: info.inspection_mode || "smart",
    auto_upgrade_visual: info.auto_upgrade_visual !== false,
    score_increment: Number(info.score_increment ?? 1),
    // Settings-level notes written by finalize; the panel shows them once above the report list.
    batch_notices: Array.isArray(info.batch_notices) ? info.batch_notices : [],
    submission_preflight: Array.isArray(info.submission_preflight) ? info.submission_preflight : await scanSubmissions(reportDir),
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

function managedWorkDir() {
  const root = path.resolve(workspace, "tmp", "experiment-report-grader");
  const candidate = path.resolve(root, safeBatchId(batch.batch_id));
  if (path.dirname(candidate) !== root) throw new Error("拒绝访问批次临时目录之外的路径");
  if (batch.work_dir && path.resolve(batch.work_dir) !== candidate) {
    throw new Error("批次记录的暂存路径与 skill 管理的批次路径不一致，拒绝清理");
  }
  return { root, candidate };
}

async function cleanupSummary() {
  const { root, candidate } = managedWorkDir();
  const reasons = [];
  const entries = [];
  let fileCount = 0;
  let byteCount = 0;
  let exists = false;
  try {
    const rootReal = await fs.realpath(root);
    const info = await fs.lstat(candidate);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("评分暂存路径不是普通目录，拒绝清理");
    const candidateReal = await fs.realpath(candidate);
    if (path.dirname(candidateReal) !== rootReal) throw new Error("评分暂存目录解析到管理范围之外，拒绝清理");
    exists = true;
    for (const entry of await fs.readdir(candidate, { withFileTypes: true })) {
      entries.push({ name: entry.name, type: entry.isDirectory() ? "文件夹" : entry.isFile() ? "文件" : "其他" });
    }
    async function measure(directory) {
      for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
        const file = path.join(directory, entry.name);
        if (entry.isSymbolicLink()) { fileCount++; continue; }
        if (entry.isDirectory()) await measure(file);
        else if (entry.isFile()) { const stat = await fs.stat(file); fileCount++; byteCount += stat.size; }
      }
    }
    await measure(candidate);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const infoPath = path.join(outputDir, "批次信息.json");
  let batchInfo = {};
  try { batchInfo = JSON.parse(await fs.readFile(infoPath, "utf8")); }
  catch { reasons.push("缺少可读取的批次信息"); }
  if (!exists) reasons.push("没有找到本批次的评分暂存目录");
  if (!batchInfo.finalized_at || Number(batchInfo.pending_count ?? 0) > 0) reasons.push("评分或盲复核尚未全部完成");
  const reviews = await readReviews();
  if (!reviews.length) reasons.push("没有评分记录可供完成状态核验");
  const incomplete = reviews.filter((review) => review.review_status !== "approved" || review.submission_status || review.adjudication_status === "required");
  if (incomplete.length) reasons.push(`还有 ${incomplete.length} 份报告未完成教师审核、补交处理或评分裁决`);
  for (const file of [infoPath, workbookPath, batchHtmlPath]) {
    try { if ((await fs.stat(file)).size <= 0) reasons.push(`正式结果文件为空：${path.basename(file)}`); }
    catch { reasons.push(`缺少正式结果文件：${path.basename(file)}`); }
  }
  try {
    const manifest = JSON.parse(await fs.readFile(path.join(candidate, "grading-manifest.json"), "utf8"));
    if (manifest.batch_id !== batch.batch_id || !manifest.result_dir || path.resolve(manifest.result_dir) !== path.resolve(outputDir)) reasons.push("暂存目录中的批次清单与当前结果目录不匹配");
  } catch { reasons.push("缺少匹配当前批次的评分清单"); }
  return {
    ok: true,
    available: reasons.length === 0,
    reasons,
    directory: candidate,
    exists,
    entries,
    file_count: fileCount,
    bytes: byteCount,
    preserved: [outputDir, reviewDir, workbookPath, batchHtmlPath, originalBackupDir],
  };
}

async function cleanBatchWork() {
  const summary = await cleanupSummary();
  if (!summary.available) throw new Error(summary.reasons.join("；"));
  const { candidate } = managedWorkDir();
  await runFile(python, [batchPipeline, "clean", "--work-dir", candidate], {
    cwd: workspace,
    windowsHide: true,
    env: { ...process.env, EXPERIMENT_REPORT_GRADER_WORKSPACE: workspace, PYTHONUTF8: "1" },
    maxBuffer: 1024 * 1024,
  });
  batch.work_dir = null;
  return { ok: true, removed: candidate, preserved: summary.preserved };
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

async function saveReview(reviewFile, incoming, approve, writebackOptions = {}, previewSeed = null) {
  const reviewPath = safeReviewPath(reviewFile);
  const originalText = await fs.readFile(reviewPath, "utf8");
  const original = JSON.parse(originalText);
  if (original.submission_status) throw new Error("该报告正在等待补交或重新评分，不能保存旧评分或写回");
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
          ...writebackFlags(writebackOptions, previewSeed ?? crypto.randomInt(1, 1 << 30)),
          ...(writebackOptions.signature && signaturePdf ? ["--signature-pdf", signaturePdf] : []),
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
        signature_image_count: annotation.signature_image_count ?? null,
        signature_image_rect: annotation.signature_image_rect ?? null,
        checkmarked_pages: (annotation.checkmarked_pages ?? []).length,
        unmarked_pages: annotation.unmarked_pages ?? null,
        seed: annotation.seed ?? null,
        handwritten_comments: writebackOptions.handwrittenComments === true,
        checkmarks_enabled: writebackOptions.checkmarks !== false,
        signature_enabled: writebackOptions.signature !== false,
        score_enabled: writebackOptions.score !== false,
        feedback_position: writebackOptions.feedbackPosition === "last" ? "last" : "first",
      };
      await fs.writeFile(reviewPath, JSON.stringify(review, null, 2) + "\n", "utf8");
      if (pdftoppm) {
        // Render the feedback sheet at its selected end and the review page, which may
        // carry the selected checkmark, signature, score, or handwritten comments.
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

async function setResubmissionStatus(reviewFile, action) {
  const reviewPath = safeReviewPath(reviewFile);
  const originalText = await fs.readFile(reviewPath, "utf8");
  const review = JSON.parse(originalText);
  const source = validateReview(review);
  if (action === "request") {
    if (review.submission_status) throw new Error("该报告已处于补交流程中");
    const baselineHash = await fileSha256(source);
    await fs.mkdir(originalBackupDir, { recursive: true });
    const parsedSource = path.parse(source);
    const resubmissionBackup = path.join(originalBackupDir, `退回前_${parsedSource.name}_${baselineHash.slice(0, 10)}${parsedSource.ext}`);
    try { await fs.access(resubmissionBackup); } catch { await fs.copyFile(source, resubmissionBackup); }
    review.resubmission_baseline_sha256 = baselineHash;
    review.resubmission_backup = await fs.realpath(resubmissionBackup);
    review.resubmission_previous_review_status = review.review_status || "draft";
    review.resubmission_previous_approved_at = review.approved_at ?? null;
    review.submission_status = "resubmission_required";
    review.resubmission_requested_at = new Date().toISOString();
    review.review_status = "resubmission_required";
    review.approved_at = null;
  } else if (action === "confirm_received") {
    if (review.submission_status !== "resubmission_required") throw new Error("请先将报告标记为需要重新提交");
    const currentHash = await fileSha256(source);
    review.submission_status = "resubmitted";
    review.resubmitted_at = new Date().toISOString();
    review.resubmitted_sha256 = currentHash;
    review.review_status = "resubmitted";
    review.approved_at = null;
  } else if (action === "cancel") {
    if (!review.submission_status) throw new Error("该报告当前没有待取消的补交流程");
    const currentHash = await fileSha256(source);
    if (currentHash !== String(review.resubmission_baseline_sha256 || review.source_sha256).toLowerCase()) {
      throw new Error("PDF 已变化，不能直接恢复旧评分。请先核实当前文件，再决定是否重新评分。");
    }
    review.review_status = review.resubmission_previous_review_status || "draft";
    review.approved_at = review.resubmission_previous_approved_at ?? null;
    delete review.submission_status;
    delete review.resubmission_baseline_sha256;
    delete review.resubmission_backup;
    delete review.resubmission_requested_at;
    delete review.resubmitted_at;
    delete review.resubmitted_sha256;
    delete review.resubmission_previous_review_status;
    delete review.resubmission_previous_approved_at;
  } else {
    throw new Error("Invalid resubmission action");
  }
  const temp = `${reviewPath}.${crypto.randomUUID()}.tmp`;
  await fs.writeFile(temp, JSON.stringify(review, null, 2) + "\n", "utf8");
  await replaceFile(temp, reviewPath);
  const tempHtml = path.join(tempRoot, `${crypto.randomUUID()}.html`);
  const tempWorkbook = path.join(tempRoot, `${crypto.randomUUID()}.xlsx`);
  try {
    await runArtifacts(tempHtml, tempWorkbook);
    await fs.mkdir(path.dirname(batchHtmlPath), { recursive: true });
    await fs.copyFile(tempHtml, batchHtmlPath);
    await replaceFile(tempWorkbook, workbookPath);
  } catch (error) {
    await fs.writeFile(reviewPath, originalText, "utf8");
    throw error;
  } finally {
    await Promise.all([tempHtml, tempWorkbook].map((file) => fs.rm(file, { force: true })));
  }
  return { ok: true, review: { ...review, __review_file: reviewFile } };
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
    if (request.method === "GET" && url.pathname === "/api/cleanup") {
      return sendJson(response, 200, await cleanupSummary());
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
    if (request.method === "GET" && url.pathname.startsWith("/api/writeback-preview/")) {
      const id = url.pathname.split("/").at(-1);
      const preview = writebackPreviews.get(id);
      if (!preview) return sendError(response, 404, "Preview expired; generate a new preview");
      return serveFile(request, response, preview.pdfTemp, "application/pdf");
    }
    if (request.method === "POST" && url.pathname === "/api/review-preview") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      return sendJson(response, 200, await createWritebackPreview(body.review_file, body.review, body.options));
    }
    if (request.method === "POST" && url.pathname === "/api/review-preview/discard") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      await discardWritebackPreview(String(body.preview_id || ""));
      return sendJson(response, 200, { ok: true });
    }
    if (request.method === "POST" && url.pathname === "/api/resubmission") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      const task = () => setResubmissionStatus(body.review_file, body.action);
      operation = operation.then(task, task);
      return sendJson(response, 200, await operation);
    }
    if (request.method === "POST" && url.pathname === "/api/review") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      if (!['save_draft', 'approve_and_generate'].includes(body.action)) return sendError(response, 400, "Invalid action");
      let preview = null;
      const options = body.action === "approve_and_generate" ? {
        handwrittenComments: body.options?.handwrittenComments === true,
        checkmarks: body.options?.checkmarks !== false,
        signature: body.options?.signature !== false,
        score: body.options?.score !== false,
        feedbackPosition: body.options?.feedbackPosition === "last" ? "last" : "first",
      } : {};
      const task = async () => {
        const result = await saveReview(body.review_file, body.review, body.action === "approve_and_generate", options, null);
        return result;
      };
      operation = operation.then(task, task);
      return sendJson(response, 200, await operation);
    }
    if (request.method === "POST" && url.pathname === "/api/batch/select") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      const selected = await selectBatch(body.report_dir, body.batch_name, body.inspection_mode, body.auto_upgrade_visual);
      return sendJson(response, 200, { ok: true, batch: selected, reviews: await readReviews() });
    }
    if (request.method === "POST" && url.pathname === "/api/cleanup") {
      if (request.headers["x-review-token"] !== token) return sendError(response, 403, "Invalid review token");
      const body = await readBody(request);
      if (body.confirm !== true) return sendError(response, 400, "请先确认清理操作");
      const task = cleanBatchWork;
      operation = operation.then(task, task);
      return sendJson(response, 200, await operation);
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
