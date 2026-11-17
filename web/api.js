/* =============================================================================
 * web/api.js —— fixture / live 统一 client（D3 第 4、6 节）
 *
 * 两条口径：
 * 1. fixture 与 live 暴露完全相同的方法与返回形状，业务代码不写模式分支；
 * 2. CSRF 由这里统一处理：先 GET /me 种 cookie，登录后必须重读 csrftoken，
 *    因为 Django 登录成功会 rotate_token，旧令牌立刻失效（D3 6.2 实测坑）。
 *
 * 本文件不渲染任何东西，也不保存页面状态；状态在 state.js。
 * ========================================================================== */

export class ApiError extends Error {
  constructor(status, code, message, details, requestId) {
    super(message || code || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details || {};
    this.requestId = requestId || "";
  }

  /** 页面只需要区分这几种处置方式，不需要认识所有错误码。 */
  get kind() {
    if (this.status === 401) return "unauthenticated";
    if (this.status === 403) return this.code === "csrf_failed" ? "csrf" : "forbidden";
    if (this.status === 404) return "missing";
    if (this.status === 501) return "not_implemented";
    if (this.status === 409) return "conflict";
    if (this.status === 400 || this.status === 422) return "invalid";
    return "failure";
  }
}

export function attachmentFilename(disposition) {
  const extended = /filename\*\s*=\s*UTF-8'[^']*'([^;]+)/i.exec(disposition || "");
  let name = "";
  if (extended) { try { name = decodeURIComponent(extended[1].trim()); } catch {} }
  if (!name) {
    const ordinary = /filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))/i.exec(disposition || "");
    name = ordinary ? (ordinary[1] ?? ordinary[2] ?? "").replace(/\\(.)/g, "$1").trim() : "";
  }
  name = name.split(/[\\/]/).pop().replace(/[\x00-\x1f\x7f]/g, "");
  if (!name || !/\.xlsx$/i.test(name)) throw new ApiError(502, "invalid_attachment", "响应缺少有效的 XLSX 附件文件名");
  return name;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

function readCookie(name) {
  const hit = document.cookie.split("; ").find((row) => row.startsWith(`${name}=`));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function readPayload(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    // 网关或调试页可能返回 HTML；保留片段便于排障，不假装是契约错误。
    return { error: { code: "non_json_response", message: text.slice(0, 200), details: {} } };
  }
}

/* ------------------------------- live ------------------------------------ */

function createLiveAdapter(baseUrl) {
  let csrf = "";

  async function raw(method, path, { body, headers = {} } = {}) {
    const multipart = typeof FormData !== "undefined" && body instanceof FormData;
    const response = await fetch(baseUrl + path, {
      method,
      credentials: "same-origin",
      headers: { ...(body !== undefined && !multipart ? JSON_HEADERS : {}), ...headers },
      body: body !== undefined ? (multipart ? body : JSON.stringify(body)) : undefined,
    });
    const payload = await readPayload(response);
    if (!response.ok) {
      const error = (payload && payload.error) || {};
      throw new ApiError(response.status, error.code, error.message, error.details, payload && payload.request_id);
    }
    return payload;
  }

  async function ensureCsrf() {
    if (csrf) return csrf;
    try {
      await raw("GET", "/me");
    } catch {
      // 401 也会种下 csrftoken；这里只关心 cookie，不关心身份结果。
    }
    csrf = readCookie("csrftoken");
    return csrf;
  }

  async function write(method, path, body, key) {
    csrf = readCookie("csrftoken") || await ensureCsrf();
    return raw(method, path, {body, headers: {"X-CSRFToken": csrf, ...(key ? {"Idempotency-Key": key} : {})}});
  }

  return {
    mode: "live",
    getHistory(taskId) { return raw("GET", `/tasks/${encodeURIComponent(taskId)}/history`); },
    confirmItem(versionId, itemId, body) { return write("PATCH", `/versions/${encodeURIComponent(versionId)}/items/${encodeURIComponent(itemId)}/confirmation`, body); },
    submitTask(taskId, body, key) { return write("POST", `/tasks/${encodeURIComponent(taskId)}/submissions`, body, key); },
    decideSubmission(submissionId, body, key) { return write("POST", `/submissions/${encodeURIComponent(submissionId)}/decisions`, body, key); },
    async exportFile(versionId, body) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      let response;
      try {
        response = await fetch(baseUrl + `/versions/${encodeURIComponent(versionId)}/exports`, {
          method: "POST", credentials: "same-origin", headers: {...JSON_HEADERS, "X-CSRFToken": csrf}, body: JSON.stringify(body),
        });
      } catch (e) { throw new ApiError(0, "network_error", e.message || "网络连接失败"); }
      const mime = response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
      if (!response.ok || mime !== "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") {
        const payload = await readPayload(response), error = payload?.error || {};
        throw new ApiError(response.ok ? 502 : response.status, error.code || "invalid_attachment", error.message || "未返回有效的审核附件", error.details, payload?.request_id);
      }
      const filename = attachmentFilename(response.headers.get("Content-Disposition"));
      const blob = await response.blob();
      const magic = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      if (magic.length !== 4 || magic[0] !== 80 || magic[1] !== 75 || magic[2] !== 3 || magic[3] !== 4) throw new ApiError(502, "invalid_attachment", "返回内容不是 XLSX 工作簿");
      return {blob, filename, runId: response.headers.get("X-Export-Run"), resultMode: response.headers.get("X-Result-Mode")};
    },

    async me() {
      try {
        return await raw("GET", "/me");
      } catch (error) {
        if (error.status === 401) return null;
        throw error;
      }
    },

    async login(username, password) {
      await ensureCsrf();
      const user = await raw("POST", "/auth/login", {
        body: { username, password },
        headers: { "X-CSRFToken": csrf },
      });
      csrf = readCookie("csrftoken"); // 登录轮换令牌，必须重读
      return user;
    },

    async logout() {
      csrf = readCookie("csrftoken");
      await raw("POST", "/auth/logout", { headers: { "X-CSRFToken": csrf } });
      csrf = readCookie("csrftoken");
    },

    async uploadFile(file, { taskId, currentVersionId, confirmDuplicate = false, key } = {}) {
      const body = new FormData();
      body.append("file", file);
      if (taskId) {
        body.append("expected_current_version_id", currentVersionId);
        body.append("confirm_duplicate", String(confirmDuplicate));
      }
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", taskId ? `/tasks/${encodeURIComponent(taskId)}/versions` : "/tasks", {
        body, headers: { "X-CSRFToken": csrf, "Idempotency-Key": key },
      });
    },
    listClaimable(params = {}) { return raw("GET", `/tasks/claimable${query(params)}`); },
    async claimTask(taskId, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/tasks/${encodeURIComponent(taskId)}/claim`, {headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    getImport(versionId) { return raw("GET", `/versions/${encodeURIComponent(versionId)}/import`); },
    listTasks(params = {}) {
      return raw("GET", `/tasks${query({ stage: params.stage, limit: params.limit, cursor: params.cursor })}`);
    },
    getTask(taskId) {
      return raw("GET", `/tasks/${encodeURIComponent(taskId)}`);
    },
    listItems(versionId, params = {}) {
      return raw("GET", `/versions/${encodeURIComponent(versionId)}/items${query({ limit: params.limit, cursor: params.cursor })}`);
    },
    getItem(versionId, itemId) {
      return raw("GET", `/versions/${encodeURIComponent(versionId)}/items/${encodeURIComponent(itemId)}`);
    },
    getRun(runId) {
      return raw("GET", `/runs/${encodeURIComponent(runId)}`);
    },
  };
}

/* ------------------------------ fixture ---------------------------------- */

function createFixtureAdapter(url) {
  let dataset = null;

  async function load() {
    if (dataset) return dataset;
    const response = await fetch(url);
    if (!response.ok) {
      throw new ApiError(response.status, "fixture_unavailable", `无法读取 ${url}；先执行 make contracts`, {}, "fixture");
    }
    dataset = await readPayload(response);
    return dataset;
  }

  function pageOf(rows, { limit = 20, cursor } = {}) {
    const start = Number(cursor || 0);
    const size = Math.max(1, Math.min(Number(limit) || 20, 100));
    const slice = rows.slice(start, start + size);
    return { items: slice, next_cursor: start + size < rows.length ? String(start + size) : null };
  }

  return {
    mode: "fixture",
    async getHistory() { throw new ApiError(501, "live_required", "任务历史接线仅支持 live API"); },
    async confirmItem() { throw new ApiError(501, "live_required", "真实确认仅支持 live API"); },
    async submitTask() { throw new ApiError(501, "live_required", "真实提交仅支持 live API"); },
    async decideSubmission() { throw new ApiError(501, "live_required", "真实决定仅支持 live API"); },
    async exportFile() { throw new ApiError(501, "live_required", "真实导出仅支持 live API"); },
    async uploadFile() { throw new ApiError(501, "live_required", "真实上传仅支持 live API"); },
    async listClaimable() { throw new ApiError(501, "live_required", "认领仅支持 live API"); },
    async claimTask() { throw new ApiError(501, "live_required", "认领仅支持 live API"); },
    async getImport() { throw new ApiError(501, "live_required", "解析报告仅支持 live API"); },

    // fixture 模式没有真实身份，明确返回演示身份；页面必须显示"不校验权限"标记。
    async me() {
      await load();
      return { id: "fixture", username: "fixture", display_name: "演示账号", role: "operator", role_label: "业务员 · FIXTURE 模式" };
    },
    async login() {
      return this.me();
    },
    async logout() {},

    async listTasks(params = {}) {
      const data = await load();
      const rows = data.tasks.filter((task) => !params.stage || task.stage === params.stage);
      return pageOf(rows, params);
    },
    async getTask(taskId) {
      const data = await load();
      const task = data.tasks.find((entry) => entry.task_id === taskId);
      if (!task) throw new ApiError(404, "not_found", "演示数据中没有该任务", {}, "fixture");
      return task;
    },
    async listItems(versionId, params = {}) {
      const data = await load();
      return pageOf(data.items[versionId] || [], params);
    },
    async getItem(versionId, itemId) {
      const data = await load();
      const card = data.cards[itemId];
      if (!card) throw new ApiError(404, "not_found", "演示数据中没有该商品", {}, "fixture");
      if (card.version_id !== versionId) throw new ApiError(404, "not_found", "商品不属于该版本", {}, "fixture");
      return card;
    },
    async getRun(runId) {
      const data = await load();
      const run = data.runs[runId];
      if (!run) throw new ApiError(404, "not_found", "演示数据中没有该执行", {}, "fixture");
      return run;
    },
  };
}

/* ------------------------------- 入口 ------------------------------------ */

/**
 * @param {{mode?: "live"|"fixture", baseUrl?: string, fixtureUrl?: string}} options
 * @returns 统一 client；fixture 写动作明确拒绝，live 使用真实会话和服务端守卫。
 */
export function createClient({ mode = "live", baseUrl = "/api/v1", fixtureUrl = "/demo/fixtures/DEMO-SET.json" } = {}) {
  return mode === "fixture" ? createFixtureAdapter(fixtureUrl) : createLiveAdapter(baseUrl);
}
