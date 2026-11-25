(() => {
  const page = document.body.dataset.page,
    base = "/api/v1/ops/debug/";
  let generation = 0,
    parentRun = null,
    submitBusy = false,
    previewAttempt = null;
  const csrf = () => {
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  };
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const fmt = (x) =>
    x ? new Date(x).toLocaleString("zh-CN", { hour12: false }) : "—";
  const apiError = (e) => e?.message || "请求失败，请稍后重试";
  async function req(path, o = {}) {
    const h = {
      Accept: "application/json",
      ...(o.body ? { "Content-Type": "application/json" } : {}),
      ...(o.headers || {}),
    };
    if (o.method && o.method !== "GET") h["X-CSRFToken"] = csrf();
    const r = await fetch(base + path, {
      credentials: "same-origin",
      cache: "no-store",
      ...o,
      headers: h,
    });
    let d;
    try {
      d = await r.json();
    } catch {}
    if (!r.ok)
      throw Object.assign(
        Error(d?.error?.message || `请求失败 (${r.status})`),
        { data: d, status: r.status },
      );
    return d;
  }
  function invalidate(msg = "输入已改变") {
    generation++;
    previewAttempt = null;
    document.querySelector("#resultEmpty").hidden = false;
    ["previewView", "runView"].forEach((id) => {
      const e = document.querySelector("#" + id);
      if (e) {
        e.hidden = true;
        e.innerHTML = "";
      }
    });
    const s = document.querySelector("#resultStatus");
    if (s) {
      s.textContent = msg;
      s.className = "status neutral";
    }
    const t = document.querySelector("#timeline");
    if (t && page === "console")
      t.innerHTML =
        '<div class="timeline-placeholder">预览或运行后，这里会显示执行步骤。</div>';
  }
  function attrs() {
    const out = {};
    document.querySelectorAll(".attribute-row").forEach((r) => {
      const k = r.querySelector("[data-k]")?.value.trim(),
        v = r.querySelector("[data-v]")?.value.trim();
      if (k && v) out[k] = v;
    });
    return out;
  }
  function addAttr(k = "", v = "") {
    const box = document.querySelector("#attributeRows");
    if (!box) return;
    const r = document.createElement("div");
    r.className = "attribute-row";
    r.innerHTML = `<input data-k maxlength="200" placeholder="属性名" value="${esc(k)}"><input data-v maxlength="200" placeholder="属性值" value="${esc(v)}"><button type="button" class="remove-attribute">×</button>`;
    r.querySelector(".remove-attribute").onclick = () => {
      r.remove();
      invalidate();
    };
    box.append(r);
  }
  function safeEvidence(m) {
    const u =
      typeof m?.url === "string" && m.url.startsWith("/ops/data/evidence?")
        ? m.url
        : null;
    return u
      ? `<a class="text-link" href="${esc(u)}">查看证据 →</a>`
      : '<span class="muted">未提供可定位链接</span>';
  }
  function showError(m) {
    const s = document.querySelector("#resultStatus");
    if (s) {
      s.textContent = "请求未完成";
      s.className = "status bad";
    }
    const e = document.querySelector("#resultEmpty");
    if (e) {
      e.hidden = false;
      e.innerHTML = `<div class="empty-icon">!</div><h3>无法完成操作</h3><p>${esc(m)}</p>`;
    }
  }
  const phases = {
    prepare: "检索与准备",
    model: "模型请求",
    mock: "本地模拟",
    parse: "结构校验",
    postcheck: "来源与结果核对",
  };
  const order = { prepare: 0, model: 1, mock: 1, parse: 2, postcheck: 3 };
  const business = {
    candidate: "存在待核候选",
    needs_facts: "暂不能定码 · 缺资料",
    undetermined: "暂不能定码",
    not_evaluated: "未评估",
    needs_review: "专家辅助核查 · 待人工复核",
    conflict: "存在核查冲突 · 待人工复核",
    checked: "已完成分项核查 · 待人工复核",
  };
  const stages = { precheck: "独立预审", expert_check: "专家核查" };
  const aspects = { elements: "申报要素", images: "商品图片", tax: "退税", regulatory: "监管" };
  const verdicts = { matched: "相符", conflict: "存在冲突", unknown: "待核实" };
  const modeText = (d) => d.mode === "mock" ? "MOCK · 合成模拟" : "LIVE · 来源核查";
  function businessText(d) {
    if (d.stage === "expert_check" && d.content_visible && d.status === "succeeded")
      return "已核查 · 待人工复核";
    return business[d.business_status] || "—";
  }
  function conclusion(d) {
    if (!d.content_visible)
      return '<p class="muted">当前权限仅可查看技术状态。</p>';
    if (d.stage === "expert_check") {
      const findings = new Map((d.findings || []).map((f) => [f.aspect, f]));
      const materials = new Map((d.materials || []).map((m) => [m.evidence_id, m]));
      const cards = Object.entries(aspects).map(([aspect, title]) => {
        const finding = findings.get(aspect),
          verdict = finding && Object.hasOwn(verdicts, finding.verdict) ? finding.verdict : "unknown";
        const evidence = [...new Set(finding?.evidence_ids || [])].map((id) => {
          const material = materials.get(id);
          return `<li>${esc(material?.label || "证据材料")} · ${safeEvidence(material)}</li>`;
        }).join("");
        return `<section class="finding-card ${verdict}"><div class="finding-head"><h4>${title}</h4><span class="status ${verdict === "conflict" ? "bad" : verdict === "matched" ? "good" : "warn"}">${verdicts[verdict]}</span></div><p>${esc(finding?.reason || "尚无核查结果，保持待核实")}</p><span class="muted">${esc(finding?.evidence_ids?.length || 0)} 条关联证据</span>${evidence ? `<ul class="finding-evidence">${evidence}</ul>` : ""}</section>`;
      }).join("");
      const gaps = (d.gaps || []).map((g) => `<li><code>${esc(g.code || "UNKNOWN")}</code> ${esc(g.message || "保持待核实")}</li>`).join("");
      return `<h3>专家分项核查</h3><div class="callout"><strong>${modeText(d)}</strong>。${d.mode === "mock" ? "以下为合成样例结果，仅验证调试流程，不构成真实业务核查。" : "AI 辅助结果，待人工复核。"}相符不代表审核通过，待核实不代表无风险。</div><dl class="expert-codes"><dt>本次核查编码</dt><dd class="mono">${esc(d.target_hs || "—")}</dd><dt>建议编码</dt><dd class="mono">${esc(d.suggested_hs || "未建议改码")}</dd></dl><div class="findings-grid">${cards}</div><h3>待核问题</h3>${gaps ? `<ul class="result-gaps">${gaps}</ul>` : '<p class="result-gaps">未记录其他问题，仍需人工复核</p>'}`;
    }
    return `<h3>候选与缺口</h3><p>${esc((d.candidates || []).map((x) => x.hs).join("、") || "暂无候选")}</p><p>${esc((d.gaps || []).map((x) => x.message || x.code).join("；") || "未记录缺口")}</p>`;
  }
  function timeline(items, runId) {
    const e = document.querySelector("#timeline");
    if (!e) return;
    const sorted = [...(items || [])].sort(
      (a, b) =>
        (a.started_at || "").localeCompare(b.started_at || "") ||
        (order[a.kind] ?? 9) - (order[b.kind] ?? 9) ||
        (a.seq || 0) - (b.seq || 0),
    );
    e.innerHTML =
      sorted
        .map(
          (s) =>
            `<div class="timeline-item reveal"><h4>${esc(phases[s.kind] || "步骤")}</h4><p>${esc(s.status || "—")} · ${esc(fmt(s.started_at))} → ${esc(fmt(s.finished_at))}</p><p>${esc(s.error_message || s.error_code || "")}</p>${s.kind === "model" ? `<p>请求：第 ${esc(s.attempt_no || "—")} 次 · 响应：${s.received ? `HTTP ${esc(s.http_status || "—")}` : "未记录，是否送达未知"}${s.truncated ? " · 已截断" : ""}</p>` : ""}${s.payload_available ? `<button class="button ghost tiny payload" data-run="${esc(runId || "")}" data-seq="${esc(s.seq)}">展开脱敏报文</button>` : ""}</div>`,
        )
        .join("") || '<div class="timeline-placeholder">暂无步骤记录。</div>';
    e.querySelectorAll(".payload").forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            const d = await req(
              `runs/${encodeURIComponent(b.dataset.run)}/steps/${encodeURIComponent(b.dataset.seq)}/payload`,
            );
            if (d.payload_state !== "available") {
              b.textContent =
                d.payload_state === "expired" ? "报文已到期" : "未保留报文";
              b.disabled = true;
              return;
            }
            const pair = document.createElement("div");
            pair.className = "trace-pair";
            for (const [title, value] of [
              ["请求摘要", d.request_redacted],
              ["响应摘要", d.response_redacted],
            ]) {
              const section = document.createElement("section");
              const label = document.createElement("strong");
              label.textContent = title;
              const pre = document.createElement("pre");
              pre.textContent = value
                ? JSON.stringify(value, null, 2)
                : "未保留可展示内容";
              section.append(label, pre);
              pair.append(section);
            }
            b.after(pair);
            b.remove();
          } catch (x) {
            b.textContent = apiError(x);
          }
        }),
    );
  }
  async function initConsole() {
    let c;
    try {
      c = await req("capabilities");
    } catch (e) {
      showError(apiError(e));
      return;
    }
    const qParent = new URLSearchParams(location.search).get("parent_run_id");
    if (qParent) {
      try {
        parentRun = await req(`runs/${encodeURIComponent(qParent)}`);
      } catch (e) {
        showError(apiError(e));
        return;
      }
    }
    document.querySelector("#connectionName").textContent = c.connection
      ? `${c.connection.alias} / ${c.connection.model}`
      : "未启用调试连接";
    document.querySelector("#connectionMeta").textContent =
      c.connection?.target || "—";
    document.querySelector("#modePill").textContent = (c.modes || ["mock"])
      .join(" · ")
      .toUpperCase();
    document.querySelector("#snapshotState").textContent =
      (c.snapshot_ready ? "资料快照已就绪" : "资料未装载") +
      (c.snapshot ? ` · ${c.snapshot}` : "");
    const mode = document.querySelector("#modeSelect"),
      stage = document.querySelector("#stageSelect"),
      f = document.querySelector("#factsForm"),
      expertFields = document.querySelector("#expertFields");
    stage.innerHTML = (c.stages || ["precheck"])
      .filter((x) => Object.hasOwn(stages, x))
      .map((x) => `<option value="${esc(x)}">${stages[x]}</option>`).join("");
    if (parentRun) {
      stage.value = parentRun.stage || "precheck";
      stage.disabled = true;
      mode.disabled = true;
      document.querySelector("#inputHint").textContent = "复制原运行的输入和阶段；如需修改，请返回新建调试";
    }
    mode.innerHTML = (c.modes || ["mock"])
      .map((x) => `<option value="${esc(x)}">${esc(x.toUpperCase())}</option>`)
      .join("");
    if (parentRun) mode.value = parentRun.mode;
    const list = document.querySelector("#sampleList");
    list.innerHTML = "";
    const syncSample = (select = null) => {
      list.querySelectorAll(".sample").forEach((b) => {
        b.hidden = Boolean(parentRun) || b.dataset.stage !== stage.value ||
          (b.dataset.mode === "mock" && mode.value === "live");
        b.classList.toggle("selected", !b.hidden && b === select);
      });
      if (select && !select.hidden) f.dataset.sampleId = select.dataset.id;
      else delete f.dataset.sampleId;
      expertFields.hidden = stage.value !== "expert_check";
      expertFields.disabled = expertFields.hidden || Boolean(f.dataset.sampleId) || Boolean(parentRun);
      document.querySelector("#stageHint").textContent = stage.value === "expert_check"
        ? "核对提交编码的要素、图片、退税和监管；本版图片未接入。"
        : "独立生成 HS 候选，再核对来源与待补事实。";
    };
    (c.samples || []).forEach((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "sample";
      b.dataset.mode = s.mode || "";
      b.dataset.id = s.id;
      b.dataset.stage = s.stage || "precheck";
      b.innerHTML = `<strong>${esc(s.label)}</strong><span>${esc(s.id)}</span>`;
      b.onclick = () => {
        syncSample(b);
        invalidate("样例已改变");
      };
      list.append(b);
    });
    syncSample();
    stage.addEventListener("change", () => {
      syncSample();
      invalidate("调试阶段已改变");
    });
    if (!c.samples?.length)
      list.innerHTML = '<p class="muted">暂无获批样例，请填写本机样例。</p>';
    addAttr();
    document.querySelector("#addAttribute").onclick = () => {
      syncSample();
      addAttr();
      invalidate();
    };
    f.addEventListener("input", (e) => {
      syncSample(
        e.target === mode ? list.querySelector(".sample.selected") : null,
      );
      invalidate();
    });
    document.querySelector("#clearBtn").onclick = () => {
      f.reset();
      if (parentRun) {
        stage.value = parentRun.stage || "precheck";
        mode.value = parentRun.mode;
      }
      syncSample();
      document.querySelector("#attributeRows").innerHTML = "";
      addAttr();
      invalidate("已清空，请重新预览");
    };
    f.onsubmit = async (e) => {
      e.preventDefault();
      invalidate("生成预览中");
      const g = generation,
        fd = new FormData(f),
        body = { mode: parentRun?.mode || fd.get("mode"),
                 stage: parentRun?.stage || stage.value || "precheck" };
      if (parentRun) body.parent_run_id = qParent;
      else if (f.dataset.sampleId) body.sample_id = f.dataset.sampleId;
      else {
        body.facts = {
          name: fd.get("name"),
          attributes: attrs(),
          elements_text: fd.get("elements_text"),
        };
        const basis = {};
        for (const key of ["declaration_date", "tariff_version"])
          if (fd.get(key)?.trim()) basis[key] = fd.get(key).trim();
        if (Object.keys(basis).length) body.query_basis = basis;
        if (body.stage === "expert_check")
          body.expert = {
            submitted_hs: fd.get("submitted_hs")?.trim() || "",
            supplier_tax_rate_raw: fd.get("supplier_tax_rate_raw")?.trim() || "",
            direction: fd.get("direction") || "unknown",
            rebate_code: fd.get("rebate_code")?.trim() || "",
          };
      }
      const b = document.querySelector("#previewBtn");
      b.disabled = true;
      b.textContent = "生成中…";
      try {
        const p = await req("previews", {
          method: "POST",
          body: JSON.stringify(body),
        });
        if (g === generation) {
          p.parent_run_id = parentRun?.id || null;
          renderPreview(p);
        }
      } catch (x) {
        if (g === generation) showError(apiError(x));
      } finally {
        b.disabled = false;
        b.innerHTML = "生成发送预览 <span>→</span>";
      }
    };
  }
  function renderPreview(p) {
    previewAttempt = { key: crypto.randomUUID(), generation };
    document.querySelector("#resultEmpty").hidden = true;
    const e = document.querySelector("#previewView");
    e.hidden = false;
    e.className = "preview-card reveal";
    const versions = Object.entries(p.versions || {}).map(([k, v]) => `${k}=${v}`).join(" · ");
    e.innerHTML = `<div class="destination"><span class="pill">${esc(p.destination?.target || "mock")}</span><strong>${esc(p.destination?.host || "本地")} · ${esc(p.destination?.model || "—")}</strong></div>
      <div class="callout">${esc((p.warnings || []).join("；"))}。预览仅显示技术元数据，不显示事实、消息、提示词或资料正文。</div>
      ${p.stage === "expert_check" ? `<p class="stage-note">专家核查 · ${p.model_required === false ? "仅核对来源，本次不调用模型；来源或要素不足的分项保持待核实。" : "核对提交编码的四项辅助结果，待人工复核。"}</p>` : ""}
      <dl><dt>报文摘要（SHA-256）</dt><dd class="mono">${esc(p.packet_digest || "—")}</dd><dt>报文大小</dt><dd>${esc(p.request_bytes ?? "—")} 字节</dd><dt>版本</dt><dd>${esc(versions || "—")}</dd></dl>
      <div class="form-foot"><span class="hint">有效至 ${esc(fmt(p.expires_at))}</span><button class="button primary" id="runBtn">确认并运行 →</button></div>`;
    const s = document.querySelector("#resultStatus");
    s.textContent = "预览已生成";
    s.className = "status good";
    document.querySelector("#runBtn").onclick = () => submitRun(p);
  }
  async function submitRun(p) {
    if (
      submitBusy ||
      !previewAttempt ||
      previewAttempt.generation !== generation
    )
      return;
    const external = p.destination?.target === "external";
    if (external && !window.confirm("确认将已审批的报文发送至外部目标？")) return;
    const { key, generation: submittedGeneration } = previewAttempt;
    submitBusy = true;
    const button = document.querySelector("#runBtn");
    if (button) {
      button.disabled = true;
      button.textContent = "提交中…";
    }
    try {
      const body = { ticket: p.ticket, confirm_external: external };
      if (p.parent_run_id) body.parent_run_id = p.parent_run_id;
      const r = await req("runs", {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify(body),
      });
      if (generation === submittedGeneration) renderRun(r, submittedGeneration);
    } catch (e) {
      if (generation !== submittedGeneration) return;
      if (button) {
        button.disabled = false;
        button.textContent = "重试提交 →";
      }
      showError(apiError(e));
    } finally {
      submitBusy = false;
    }
  }
  function renderRun(r, runGeneration) {
    document.querySelector("#previewView").hidden = true;
    const e = document.querySelector("#runView");
    e.hidden = false;
    e.innerHTML = `<div class="run-summary"><div class="metric"><b id="runState">${esc(r.status || "queued")}</b><span>执行状态</span></div><div class="metric"><b id="runBusiness">—</b><span>业务结论</span></div><div class="metric"><b id="runCounts">0 / 0 / 0</b><span>尝试 / 响应 / 解析</span></div></div><span id="runMeta" class="muted"></span><p id="runNote" class="muted">等待执行器…</p>`;
    e.innerHTML += '<div id="runConclusion"></div>';
    const poll = async () => {
      if (generation !== runGeneration) return;
      try {
        const d = await req(`runs/${encodeURIComponent(r.id)}`);
        if (generation !== runGeneration) return;
        const state = d.status || d.state || "—";
        e.querySelector("#runState").textContent = state;
        e.querySelector("#runBusiness").textContent =
          businessText(d);
        const c = d.outbound_counts || {};
        e.querySelector("#runCounts").textContent =
          `${c.attempts ?? 0} / ${c.responses ?? 0} / ${c.parsed ?? 0}`;
        e.querySelector("#runMeta").textContent = [
          stages[d.stage || "precheck"],
          modeText(d),
          d.owner && `负责人 ${d.owner}`,
          d.model && `模型 ${d.model}`,
          d.request_id && `请求 ${d.request_id}`,
        ]
          .filter(Boolean)
          .join(" · ");
        e.querySelector("#runNote").textContent =
          state === "queued"
            ? "等待执行器…"
            : state === "running"
              ? "执行中…"
              : d.error_message || d.error_code || "已完成";
        if (["succeeded", "failed", "interrupted"].includes(state)) {
          const st = document.querySelector("#resultStatus");
          st.textContent =
            d.error_message ||
            d.error_code ||
            businessText(d) ||
            "已完成";
          st.className = `status ${state === "succeeded" ? "good" : "bad"}`;
          e.querySelector("#runConclusion").innerHTML =
            conclusion(d) +
            `<a class="text-link" href="/ops/debug/runs/${encodeURIComponent(r.id)}/">查看运行详情 →</a>`;
          const x = await req(`runs/${encodeURIComponent(r.id)}/steps`);
          if (generation === runGeneration) timeline(x.items, r.id);
          return;
        }
      } catch (x) {
        if (generation !== runGeneration) return;
        e.querySelector("#runNote").textContent = apiError(x);
        if ([401, 403, 404].includes(x.status)) return;
      }
      setTimeout(poll, document.hidden ? 10000 : 2000);
    };
    poll();
  }
  async function initConnections() {
    const t = document.querySelector("#connectionsTable");
    try {
      const d = await req("connections");
      document.querySelector("#connectionCount").textContent =
        `${d.items?.length || 0} 个连接`;
      t.innerHTML = `<table class="data-table"><thead><tr><th>别名</th><th>REVISION</th><th>端点 / 模型</th><th>状态</th><th>最近测试</th><th>操作</th></tr></thead><tbody>${(d.items || []).map((c) => `<tr><td><strong>${esc(c.alias)}</strong>${c.is_default ? " · 默认" : ""}</td><td>v${esc(c.revision)}</td><td>${esc(c.endpoint_id)}<br><span class="muted">${esc(c.model)}</span></td><td><span class="state">${esc(c.state)}</span><br><span class="muted">${c.has_key ? "Key 已设置" : "未设置 Key"}</span></td><td>${esc(c.last_probe?.status || "—")}<br><span class="muted">${esc(c.last_probe?.error_message || c.last_probe?.error_code || "")}${c.last_probe ? ` · ${esc(fmt(c.last_probe.finished_at || c.last_probe.started_at))} · ${esc(c.last_probe.actor || "—")}` : ""}</span></td><td><button class="button ghost tiny edit" data-id="${esc(c.id)}">新 revision</button> <button class="button ghost tiny test" data-id="${esc(c.id)}">测试</button> <button class="button ghost tiny activate" data-id="${esc(c.id)}">启用</button> <button class="button ghost tiny revoke" data-id="${esc(c.id)}">撤销</button></td></tr>`).join("")}</tbody></table>`;
      window.__debugEndpoints = d.endpoints || [];
      setEndpointOptions();
      bindConnectionActions(d.default_version);
    } catch (e) {
      t.innerHTML = `<p class="muted">${esc(apiError(e))}</p>`;
    }
  }
  function setEndpointOptions() {
    const s = document.querySelector("#endpointSelect");
    if (!s) return;
    s.innerHTML = (window.__debugEndpoints || [])
      .map(
        (x) =>
          `<option value="${esc(x.id)}" data-models="${esc(JSON.stringify(x.models || []))}" data-auth="${esc(JSON.stringify(x.auth_mode ? [x.auth_mode] : []))}">${esc(x.label || x.id)}</option>`,
      )
      .join("");
    s.onchange = updateEndpointFields;
    updateEndpointFields();
  }
  function updateEndpointFields() {
    const s = document.querySelector("#endpointSelect"),
      o = s?.selectedOptions[0];
    if (!o) return;
    let models = [],
      auth = [];
    try {
      models = JSON.parse(o.dataset.models || "[]");
      auth = JSON.parse(o.dataset.auth || "[]");
    } catch {}
    const old = document.querySelector("[name=model]");
    if (old) {
      const n = document.createElement(models.length ? "select" : "input");
      n.name = "model";
      n.id = old.id;
      n.required = true;
      n.maxLength = 120;
      if (models.length)
        n.innerHTML = models
          .map((x) => `<option value="${esc(x)}">${esc(x)}</option>`)
          .join("");
      old.replaceWith(n);
    }
    const a = document.querySelector("[name=auth_mode]");
    if (a && Array.isArray(auth) && auth.length)
      a.innerHTML = auth
        .map((x) => `<option value="${esc(x)}">${esc(x)}</option>`)
        .join("");
  }
  function cleanConnectionBody(form) {
    const b = Object.fromEntries(new FormData(form));
    if (
      form.querySelector("[name=copy_secret_from]") &&
      !form.querySelector("[name=copy_secret_from]").checked
    )
      delete b.copy_secret_from;
    if (!b.api_key) delete b.api_key;
    return b;
  }
  function bindConnectionActions(v) {
    document.querySelectorAll(".test").forEach(
      (b) =>
        (b.onclick = async () => {
          const old = b.textContent;
          b.disabled = true;
          try {
            const p = await req(`connections/${b.dataset.id}/test`, {
              method: "POST",
              headers: { "Idempotency-Key": crypto.randomUUID() },
              body: "{}",
            });
            let n = 0;
            const poll = async () => {
              try {
                const d = await req(`probes/${p.probe_id}`);
                b.textContent =
                  d.status === "failed"
                    ? d.error_code || d.error_message || "测试失败"
                    : d.status || "测试";
                if (["queued", "running"].includes(d.status) && ++n < 30)
                  setTimeout(poll, 2000);
                else b.disabled = false;
              } catch (e) {
                b.textContent = apiError(e);
                b.disabled = false;
              }
            };
            poll();
          } catch (e) {
            b.textContent = apiError(e);
            setTimeout(() => {
              b.textContent = old;
              b.disabled = false;
            }, 3000);
          }
        }),
    );
    document.querySelectorAll(".activate,.revoke").forEach(
      (b) =>
        (b.onclick = async () => {
          const action = b.classList.contains("activate")
            ? "activate"
            : "revoke";
          b.disabled = true;
          try {
            await req(`connections/${b.dataset.id}/${action}`, {
              method: "POST",
              body: JSON.stringify({ expected_default_version: v }),
            });
            location.reload();
          } catch (e) {
            b.textContent = apiError(e);
            b.disabled = false;
          }
        }),
    );
    document
      .querySelectorAll(".edit")
      .forEach((b) => (b.onclick = () => openDialog(b.dataset.id)));
  }
  async function openDialog(id) {
    try {
      const d = await req("connections"),
        c = d.items.find((x) => String(x.id) === String(id));
      if (!c) return;
      resetConnectionForm(c);
    } catch (e) {
      showError(apiError(e));
    }
  }
  async function initRuns() {
    const t = document.querySelector("#runsTable"),
      load = async () => {
        const q = new URLSearchParams();
        ["stage", "state", "mode", "error_code", "date_from", "date_to", "id"].forEach(
          (k) => {
            const e = document.querySelector("#" + k);
            if (e?.value) q.set(k, e.value);
          },
        );
        if (document.querySelector("#cursor")?.value)
          q.set("cursor", document.querySelector("#cursor").value);
        try {
          const d = await req(`runs?${q}`);
          t.innerHTML = `<table class="data-table"><thead><tr><th>时间</th><th>样例 / 运行人</th><th>阶段 / 模式 / 模型</th><th>状态 / 错误</th><th>业务结论</th><th>调用/响应/解析</th><th>耗时</th><th></th></tr></thead><tbody>${(d.items || []).map((r) => `<tr><td>${esc(fmt(r.created_at))}</td><td>${esc(r.sample_label || "—")}<br><span class="muted">${esc(r.owner || "")}</span></td><td>${esc(stages[r.stage || "precheck"] || "未知阶段")} · ${esc(modeText(r))}<br><span class="muted">${esc(r.model || "")}</span></td><td><span class="state">${esc(r.status || "—")}</span><br><span class="muted">${esc(r.error_code || r.error_message || "")}</span></td><td>${esc(businessText(r))}</td><td>${esc(r.outbound_counts?.attempts ?? 0)} / ${esc(r.outbound_counts?.responses ?? 0)} / ${esc(r.outbound_counts?.parsed ?? 0)}</td><td>${esc(r.elapsed_ms ? `${r.elapsed_ms} ms` : "—")}</td><td><a class="text-link" href="/ops/debug/runs/${encodeURIComponent(r.id)}/">查看 →</a></td></tr>`).join("")}</tbody></table><div class="pager">${d.next_cursor ? '<button class="button ghost" id="nextPage">下一页 →</button>' : ""}</div>`;
          if (d.next_cursor)
            document.querySelector("#nextPage").onclick = () => {
              document.querySelector("#cursor").value = d.next_cursor;
              load();
            };
        } catch (e) {
          t.innerHTML = `<p class="muted">${esc(apiError(e))}</p>`;
        }
      };
    ["stage", "state", "mode", "error_code", "date_from", "date_to", "id"].forEach((k) =>
      document.querySelector("#" + k)?.addEventListener("change", () => {
        document.querySelector("#cursor").value = "";
        load();
      }),
    );
    document.querySelector("#refreshRuns").onclick = () => {
      document.querySelector("#cursor").value = "";
      load();
    };
    const reset = document.querySelector("#resetRuns");
    if (reset)
      reset.onclick = () => {
        ["stage", "state", "mode", "error_code", "date_from", "date_to", "id"].forEach(
          (k) => {
            const e = document.querySelector("#" + k);
            if (e) e.value = "";
          },
        );
        document.querySelector("#cursor").value = "";
        load();
      };
    load();
  }
  async function initDetail() {
    const id = location.pathname.split("/").filter(Boolean).pop(),
      box = document.querySelector("#runDetail");
    try {
      const d = await req(`runs/${encodeURIComponent(id)}`);
      const mats =
        (d.materials || [])
          .map(
            (m) =>
              `<div class="material-row"><span class="pill">${esc(m.role || "材料")}</span> ${esc(m.status || "—")} · ${esc(m.label || "—")} ${safeEvidence(m)}</div>`,
          )
          .join("") || "暂无材料";
      box.innerHTML = `<div class="run-summary"><div class="metric"><b>${esc(d.status || "—")}</b><span>执行状态</span></div><div class="metric"><b>${esc(businessText(d))}</b><span>业务结论</span></div><div class="metric"><b>${esc(d.outbound_counts?.attempts ?? 0)} / ${esc(d.outbound_counts?.responses ?? 0)} / ${esc(d.outbound_counts?.parsed ?? 0)}</b><span>尝试 / 响应 / 解析</span></div></div><div class="callout">${esc(stages[d.stage || "precheck"] || "未知阶段")} · ${esc(modeText(d))} · 请求 ID：${esc(d.request_id || "—")} · 负责人：${esc(d.owner || "—")} · 模型：${esc(d.model || "—")} · ${esc(d.error_code || d.error_message || "无错误")}</div>${conclusion(d)}<h3>材料</h3>${mats}<div class="form-foot">${d.can_rerun !== false ? `<a class="button ghost" href="/ops/debug/?parent_run_id=${encodeURIComponent(id)}">复制样例重跑 →</a>` : ""}</div>`;
      const st = await req(`runs/${encodeURIComponent(id)}/steps`);
      timeline(st.items, id);
      if (["queued", "running"].includes(d.status))
        setTimeout(initDetail, document.hidden ? 10000 : 2000);
    } catch (e) {
      timeline([], id);
      box.innerHTML = `<p class="muted">${esc(apiError(e))}</p>`;
    }
  }
  function resetConnectionForm(source = null) {
    const f = document.querySelector("#connectionForm");
    f.reset();
    document.querySelector("#connectionFeedback").textContent = "";
    setEndpointOptions();
    if (source) {
      f.elements.alias.value = source.alias;
      f.elements.endpoint_id.value = source.endpoint_id;
      updateEndpointFields();
      f.elements.model.value = source.model;
    }
    const copy = f.elements.copy_secret_from;
    copy.disabled = !source?.has_key;
    copy.value = source?.has_key ? source.id : "";
    document.querySelector("#connectionDialog").hidden = false;
  }
  if (page === "console") initConsole();
  if (page === "connections") {
    initConnections();
    document.querySelector("#newConnection").onclick = () =>
      resetConnectionForm();
    document.querySelector("#closeDialog").onclick = () =>
      (document.querySelector("#connectionDialog").hidden = true);
    document.querySelector("#cancelDialog").onclick = () =>
      (document.querySelector("#connectionDialog").hidden = true);
    document.querySelector("#connectionForm").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await req("connections", {
          method: "POST",
          body: JSON.stringify(cleanConnectionBody(e.currentTarget)),
        });
        location.reload();
      } catch (x) {
        document.querySelector("#connectionFeedback").textContent = apiError(x);
      }
    };
  }
  if (page === "runs") initRuns();
  if (page === "run-detail") initDetail();
})();
