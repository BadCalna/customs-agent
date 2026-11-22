const SYSTEM_API = "/api/v1/ops/system";

export function newKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(20)), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export class OpsError extends Error {
  constructor(status, code, requestId) {
    super("Ops request failed");
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export async function call(path, {method = "GET", body, key, file = false, signal} = {}) {
  const headers = {Accept: file ? "text/csv, application/json" : "application/json"};
  if (method !== "GET") {
    const token = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
    headers["X-CSRFToken"] = token || "";
    headers["Idempotency-Key"] = key || newKey();
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path === "@logout" ? "/api/v1/auth/logout" : `${SYSTEM_API}${path}`, {
    method, headers, credentials: "same-origin", cache: "no-store", mode: "same-origin", redirect: "error", signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const requestId = response.headers.get("X-Request-ID");
  if (file && response.ok && /^text\/csv\b/i.test(response.headers.get("Content-Type") || "")) {
    return {blob: await response.blob(), requestId};
  }
  if (response.status === 204) return {};
  const data = (response.headers.get("Content-Type") || "").includes("application/json")
    ? await response.json() : null;
  if (!response.ok || !data || (file && data?.status !== "accepted" && !data?.run_id)) {
    throw new OpsError(response.status, data?.error?.code, data?.request_id || requestId);
  }
  return data;
}

export function requestRef(value) {
  return typeof value === "string" && /^[A-Za-z0-9._:-]{1,128}$/.test(value) ? value : "";
}

export function errorText(error, fallback = "操作未完成，请稍后重试。") {
  const messages = {
    anonymous_required: "请先退出当前会话，再打开一次性设密链接。",
    csrf_failed: "会话校验已失效，请刷新页面后再试。",
    invalid_credentials: "无法登录，请检查用户名、密码和账号状态。",
    idempotency_conflict: "该请求编号已用于不同的内容，请重新发起操作。",
    last_active_admin: "必须保留至少一名拥有关键权限的活跃管理员。",
    account_not_deletable: "该账号不能物理删除，已停用。",
    account_disabled: "已禁用账号无法签发凭证，请先启用账号。",
    secret_required: "此账号必须先完成一次性设密。",
    reserved_account: "此保留账号须通过原生管理后台维护。",
    delegation_denied: "不能分配或变更超出自身权限范围的授权。",
    username_exists: "用户名已存在，请核对后再试。",
    confirmation_required: "此操作需要二次确认，请返回检查并确认。",
    invalid_filter: "筛选值不符合要求，请核对日期和标识格式。",
    export_too_large: "导出超过 500 条，请缩小筛选范围后再试。",
    export_expired: "导出已过期，请重新生成。",
  };
  const statuses = {
    401: "未登录或会话已失效，请重新登录。",
    403: "当前权限不允许执行此操作。",
    404: "对象不存在或不在当前可见范围。",
    409: "操作与当前状态冲突，请刷新数据后核对。",
    422: "提交内容不符合要求，请检查输入。",
    429: "请求过于频繁，请稍后重试。",
  };
  const ref = requestRef(error?.requestId);
  return (messages[error?.code] || statuses[error?.status] || (!error?.status ? "未能连接到服务，请检查网络后重试。" : fallback))
    + (ref ? `\n请求 ID：${ref}` : "");
}

export function feedback(node, text = "", tone = "", focus = false) {
  node.textContent = text;
  node.hidden = !text;
  node.dataset.tone = tone;
  if (text && focus) node.focus();
}

export function busy(form, isBusy) {
  form.setAttribute("aria-busy", String(isBusy));
  form.querySelector('button[type="submit"]').disabled = isBusy;
}
