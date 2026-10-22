import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const template = fs.readFileSync(
  path.join(import.meta.dirname, "..", "assets", "review-panel-template.html"),
  "utf8",
);
const fixture = (id) => ({
  student_name: "测试学生",
  student_id: id,
  source_file: `report-${id}.pdf`,
  __review_file: `report-${id}.review.json`,
  adjudication_status: "required",
  review_status: "draft",
  max_score: 100,
  items: [{ score: 92, max_score: 100 }],
});
const script = template
  .split("<script>")[1]
  .split("</script>")[0]
  .replace("__REVIEWS_JSON__", JSON.stringify([fixture("001"), fixture("002")]))
  .replace("__BATCH_NOTICES_JSON__", "[]")
  .replace("__PREFLIGHT_JSON__", "[]")
  .replace(/init\(\);\s*$/, "");

function element() {
  const listeners = new Map();
  return {
    hidden: true,
    checked: false,
    disabled: false,
    textContent: "",
    value: "",
    focus() {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type) { listeners.delete(type); },
    click() { listeners.get("click")?.({ target: this }); },
  };
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
const memory = new Map();
const posts = [];
const context = vm.createContext({
  document: {
    activeElement: element(),
    getElementById: get,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    removeEventListener() {},
  },
  sessionStorage: {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
  },
  window: { confirm: () => { throw new Error("Disputed report must use its own confirmation"); } },
  fetch: async (_url, options) => {
    const body = JSON.parse(options.body);
    posts.push(body);
    return { ok: true, json: async () => ({ ok: true, review: { ...body.review, review_status: "approved" } }) };
  },
  setTimeout,
});
vm.runInContext(script, context);
vm.runInContext(
  'serverConfig={token:"test",batch:{batch_id:"batch-one"}};render=()=>{};refreshCleanupControl=async()=>{};',
  context,
);

// Closing the dialog must not post, acknowledge, or enable continuous adjudication.
const cancelled = vm.runInContext("directWriteback()", context);
assert.equal(get("approvalDialog").hidden, false);
get("skipDisputeConfirm").checked = true;
get("cancelApproval").click();
await cancelled;
assert.equal(posts.length, 0);
assert.equal(memory.size, 0);
assert.equal(vm.runInContext("reviews[0].adjudication_acknowledged", context), undefined);

// Explicit confirmation sends the acknowledgement only with this report's approval.
const approved = vm.runInContext("directWriteback()", context);
get("skipDisputeConfirm").checked = true;
get("confirmApproval").click();
await approved;
assert.equal(posts.length, 1);
assert.equal(posts[0].review.adjudication_acknowledged, true);
assert.equal(memory.get("skip-adjudication-confirm:batch-one"), "1");
assert.equal(vm.runInContext("active", context), 1);

// Continuous mode skips the popup, but the second report still needs its own button click.
await vm.runInContext("directWriteback()", context);
assert.equal(posts.length, 2);
assert.equal(posts[1].review.student_id, "002");
assert.equal(posts[1].review.adjudication_acknowledged, true);
assert.equal(get("approvalDialog").hidden, true);
console.log("review confirmation tests passed");
