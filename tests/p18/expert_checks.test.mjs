import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { renderExpertChecks, EXPERT_ASPECTS } from "../../web/components/expert-checks.js";

function fakeEl() {
  const listeners = new Map();
  return {
    innerHTML: "",
    listeners,
    addEventListener(type, fn) {
      listeners.set(type, fn);
    },
    removeEventListener(type, fn) {
      if (listeners.get(type) === fn) listeners.delete(type);
    },
    contains(target) {
      return Boolean(target && target._owned);
    },
  };
}

function fakeEvent(attrs, { aspect = "elements", isRun = false } = {}) {
  const rowNode = { dataset: { aspect } };
  const btn = {
    _owned: true,
    dataset: attrs,
    closest(selector) {
      return selector === "[data-aspect]" ? rowNode : null;
    },
  };
  return {
    target: {
      closest(selector) {
        if (selector === "[data-run-id]" && isRun) return btn;
        if (selector === "[data-evidence-id]" && !isRun) return btn;
        if (selector === "[data-aspect]") return rowNode;
        return null;
      },
    },
  };
}

test("renderExpertChecks renders target_hs, all 4 aspects, counts, reasons, and expandable locators", () => {
  const el = fakeEl();
  renderExpertChecks(el, {
    submittedHs: "8482102000",
    resultMode: "mock",
    expertCheck: {
      run_id: "run-1234567890ab",
      submission_id: "sub-1234567890ab",
      target_hs: "8482102000",
      findings: [
        { aspect: "elements", verdict: "conflict", reason: "缺少型号要素", evidence_ids: ["ev-el-1"] },
        { aspect: "images", verdict: "unknown", reason: "附图读取失败，不推断材质", evidence_ids: [] },
        { aspect: "tax", verdict: "matched", reason: "适用 13% 出口退税", evidence_ids: ["ev-tx-1"] },
        { aspect: "regulatory", verdict: "unknown", reason: "涉及两用物项线索", evidence_ids: ["ev-rg-1", "ev-rg-2"] },
      ],
    },
    evidence: [
      {
        evidence_id: "ev-el-1",
        status: "obtained",
        source_type: "official_declaration",
        title: "2026申报要素目录",
        document: "海关总署公告2026年第1号",
        locator: "条目 8482102000",
        retrieved_at: "2026-05-20T00:00:00Z",
      },
      {
        evidence_id: "ev-tx-1",
        status: "obtained",
        source_type: "tax_rate",
        title: "出口退税率文库",
        document: "国税退税2026库",
        locator: "第 88 条",
      },
    ],
  });

  assert.equal(EXPERT_ASPECTS.length, 4);
  assert.match(el.innerHTML, /data-target-hs="8482102000"/);
  assert.match(el.innerHTML, /MOCK 核查/);
  assert.match(el.innerHTML, /data-aspect="elements" data-verdict="conflict"/);
  assert.match(el.innerHTML, /data-aspect="images" data-verdict="unknown"/);
  assert.match(el.innerHTML, /data-aspect="tax" data-verdict="matched"/);
  assert.match(el.innerHTML, /data-aspect="regulatory" data-verdict="unknown"/);
  assert.match(el.innerHTML, /证据 1 条/);
  assert.match(el.innerHTML, /证据 0 条/);
  assert.match(el.innerHTML, /证据 2 条/);
  assert.match(el.innerHTML, /缺少型号要素/);
  assert.match(el.innerHTML, /海关总署公告2026年第1号 · 条目 8482102000/);
  assert.match(el.innerHTML, /展开定位（2 条依据）/);
  assert.match(el.innerHTML, /展开定位（0 条依据）/);
});

test("renderExpertChecks synthesizes missing aspects as unknown and notes expert recode boundary", () => {
  const el = fakeEl();
  renderExpertChecks(el, {
    submittedHs: "8482102000",
    expertFinalHs: "8536490090",
    expertCheck: {
      run_id: "run-rec",
      submission_id: "sub-rec",
      target_hs: "8482102000",
      findings: [{ aspect: "elements", verdict: "matched", reason: "要素齐全", evidence_ids: [] }],
    },
  });

  assert.match(el.innerHTML, /不覆盖改码后的新终值/);
  assert.match(el.innerHTML, /data-aspect="images" data-verdict="unknown"/);
  assert.match(el.innerHTML, /data-aspect="tax" data-verdict="unknown"/);
  assert.match(el.innerHTML, /data-aspect="regulatory" data-verdict="unknown"/);
  assert.doesNotMatch(el.innerHTML, /已核实|核查通过|批准/);
});

test("renderExpertChecks renders empty state on null check, escapes HTML, and manages listeners cleanly", () => {
  const emptyEl = fakeEl();
  renderExpertChecks(emptyEl, { expertCheck: null });
  assert.match(emptyEl.innerHTML, /暂无专家分项核查记录/);

  const openedRuns = [];
  const selectedEv = [];
  const el = fakeEl();
  renderExpertChecks(el, {
    expertCheck: {
      run_id: "run-abc",
      submission_id: "sub-abc",
      target_hs: "8482102000",
      findings: [
        { aspect: "elements", verdict: "conflict", reason: "<script>alert(1)</script>", evidence_ids: ["ev-1"] },
      ],
    },
    evidence: [{ evidence_id: "ev-1", status: "obtained", document: "<b>doc</b>", locator: "L1" }],
    onOpenRun: (runId) => openedRuns.push(runId),
    onSelectEvidence: (payload) => selectedEv.push(payload),
  });
  assert.doesNotMatch(el.innerHTML, /<script>/);
  assert.match(el.innerHTML, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);

  const handler = el.listeners.get("click");
  handler(fakeEvent({ runId: "run-abc" }, { isRun: true }));
  handler(fakeEvent({ evidenceId: "ev-1" }, { aspect: "elements", isRun: false }));
  assert.deepEqual(openedRuns, ["run-abc"]);
  assert.equal(selectedEv.length, 1);
  assert.equal(selectedEv[0].evidenceId, "ev-1");
  assert.equal(selectedEv[0].aspect, "elements");
  assert.equal(selectedEv[0].entry.locator, "L1");

  // Re-render detaches prior click listener cleanly
  renderExpertChecks(el, { expertCheck: null });
  assert.equal(el.listeners.size, 0);
});

test("source code satisfies pure-render boundary without network or approval words", () => {
  const src = readFileSync(new URL("../../web/components/expert-checks.js", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "XMLHttpRequest", "localStorage", "sessionStorage", "已核实", "批准", "放行", "通过"]) {
    assert.equal(src.includes(forbidden), false, `禁止在 expert-checks.js 出现 ${forbidden}`);
  }
});
