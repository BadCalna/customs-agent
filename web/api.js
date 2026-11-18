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

  return {
    mode: "live",

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
    async startPrecheck(versionId, payload, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/versions/${encodeURIComponent(versionId)}/prechecks`, {body: payload,
        headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    async retryRun(runId, itemIds, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/runs/${encodeURIComponent(runId)}/retries`, {body: {item_ids: itemIds},
        headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    async confirmItem(versionId, itemId, payload) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("PATCH", `/versions/${encodeURIComponent(versionId)}/items/${encodeURIComponent(itemId)}/confirmation`,
        {body: payload, headers: {"X-CSRFToken": csrf}});
    },
    async submitTask(taskId, payload, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/tasks/${encodeURIComponent(taskId)}/submissions`,
        {body: payload, headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    async decideSubmission(submissionId, payload, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/submissions/${encodeURIComponent(submissionId)}/decisions`,
        {body: payload, headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    async returnTask(taskId, payload, key) {
      csrf = readCookie("csrftoken") || await ensureCsrf();
      return raw("POST", `/tasks/${encodeURIComponent(taskId)}/returns`,
        {body: payload, headers: {"X-CSRFToken": csrf, "Idempotency-Key": key}});
    },
    taskHistory(taskId) { return raw("GET", `/tasks/${encodeURIComponent(taskId)}/history`); },
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
    async uploadFile() { throw new ApiError(501, "live_required", "真实上传仅支持 live API"); },
    async listClaimable() { throw new ApiError(501, "live_required", "认领仅支持 live API"); },
    async claimTask() { throw new ApiError(501, "live_required", "认领仅支持 live API"); },
    async getImport() { throw new ApiError(501, "live_required", "解析报告仅支持 live API"); },
    async startPrecheck() { throw new ApiError(501, "live_required", "后台重新预审仅支持 live API"); },
    async retryRun() { throw new ApiError(501, "live_required", "真实失败行重试仅支持 live API"); },

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
 * @returns 统一的只读 client；两种模式的返回值形状完全一致。
 */
export function createClient({ mode = "live", baseUrl = "/api/v1", fixtureUrl = "/demo/fixtures/DEMO-SET.json" } = {}) {
  return mode === "fixture" ? createFixtureAdapter(fixtureUrl) : createLiveAdapter(baseUrl);
}
