import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const template = fs.readFileSync(
  path.join(import.meta.dirname, "..", "assets", "review-panel-template.html"),
  "utf8",
);
const preflight = [
  ["graded.pdf", "PDF", "可进入评分", true],
  ["approved.pdf", "PDF", "可进入评分", true],
  ["resubmitted.pdf", "PDF", "已重新提交", true],
  ["regraded.pdf", "PDF", "已重新提交", true],
  ["new.pdf", "PDF", "可进入评分", true],
  ["new.pdf", "PDF", "可进入评分", true],
  ["word.docx", "DOCX", "需重新提交 PDF", false],
  ["no-id.pdf", "PDF", "需重新提交：文件名缺少姓名或学号", false],
  ["awaiting.pdf", "PDF", "可进入评分", true],
  ["confirmed.pdf", "PDF", "可进入评分", true],
].map(([file, file_type, status, included_in_grading]) =>
  ({ file, file_type, status, included_in_grading }));
const reviews = [
  { source_file: "graded.pdf", review_status: "draft" },
  { source_file: "approved.pdf", review_status: "approved" },
  { source_file: "old-resubmission.pdf", review_status: "draft" },
  { source_file: "regraded.pdf", review_status: "draft" },
  { source_file: "awaiting.pdf", submission_status: "resubmission_required" },
  { source_file: "confirmed.pdf", submission_status: "resubmitted" },
];
const script = template
  .split("<script>")[1]
  .split("</script>")[0]
  .replace("__REVIEWS_JSON__", JSON.stringify(reviews))
  .replace("__BATCH_NOTICES_JSON__", "[]")
  .replace("__PREFLIGHT_JSON__", JSON.stringify(preflight))
  .replace(/init\(\);\s*$/, "");
const context = vm.createContext({
  document: { getElementById: () => ({ onclick: null }) },
});
vm.runInContext(script, context);
vm.runInContext(
  'serverConfig={batch:{batch_id:"实验1",report_dir:"D:/reports"}};',
  context,
);
const pending = Array.from(vm.runInContext(
  "pendingGradingReports().map(p=>p.file)",
  context,
));
assert.deepEqual(pending, ["resubmitted.pdf", "new.pdf", "confirmed.pdf"]);
const prompt = vm.runInContext(
  "batchGradingPrompt(pendingGradingReports())",
  context,
);
assert.match(prompt, /待评分报告（3 份）/);
assert.match(prompt, /resubmitted\.pdf（已重新提交）/);
assert.match(prompt, /new\.pdf/);
assert.match(prompt, /confirmed\.pdf/);
assert.doesNotMatch(prompt, /graded\.pdf|approved\.pdf|awaiting\.pdf|word\.docx|no-id\.pdf/);
assert.match(prompt, /不要批准或写回源 PDF/);
console.log("batch grading copy tests passed");
