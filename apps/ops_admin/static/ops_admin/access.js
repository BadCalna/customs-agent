import {busy, call, errorText, feedback, newKey, requestRef} from "./client.js";

const login = document.querySelector("#ops-login");
if (login) {
  const result = document.querySelector("#login-feedback");
  let pending = false;
  login.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    busy(login, true);
    feedback(result);
    try {
      await call("/login", {method: "POST", body: {
        username: login.elements.username.value, password: login.elements.password.value,
      }});
      location.replace("/ops/admin/");
    } catch (error) {
      feedback(result, errorText(error, "登录暂时不可用，请稍后重试。"), "error");
    } finally {
      login.elements.password.value = "";
      pending = false;
      busy(login, false);
    }
  });
  window.addEventListener("pagehide", () => login.reset());
  login.hidden = false;
}

const secret = document.querySelector("#ops-secret");
if (secret) {
  const result = document.querySelector("#secret-feedback");
  const tokenField = document.querySelector("#secret-token-field");
  const detected = document.querySelector("#secret-link-detected");
  let fragmentTicket = new URLSearchParams(location.hash.slice(1)).get("ticket") || "";
  // A fragment never reaches the server; remove it before users copy or share this page.
  if (location.hash || location.search) history.replaceState(null, "", location.pathname);
  if (fragmentTicket.length > 128) fragmentTicket = "";

  function reflectTicket() {
    tokenField.hidden = Boolean(fragmentTicket);
    secret.elements.ticket.disabled = Boolean(fragmentTicket);
    detected.hidden = !fragmentTicket;
  }
  let pending = false;
  let replayKey = newKey();
  let finished = false;
  reflectTicket();
  secret.addEventListener("input", () => {
    secret.elements.confirm_password.setCustomValidity("");
    replayKey = newKey();
  });
  secret.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || finished) return;
    const password = secret.elements.new_password;
    const confirm = secret.elements.confirm_password;
    if (password.value !== confirm.value) {
      confirm.setCustomValidity("两次输入的密码不一致。");
      confirm.reportValidity();
      return;
    }
    pending = true;
    busy(secret, true);
    feedback(result);
    try {
      await call("/secret/consume", {method: "POST", key: replayKey, body: {
        ticket: fragmentTicket || secret.elements.ticket.value.trim(), new_password: password.value,
      }});
      finished = true;
      fragmentTicket = "";
      secret.reset();
      reflectTicket();
      secret.hidden = true;
      document.querySelector("#secret-next").hidden = false;
      feedback(result, "新密码已保存。此凭证已失效；请使用新密码重新登录。", "", true);
    } catch (error) {
      const ref = requestRef(error?.requestId);
      let message = errorText(error, "暂时无法设置密码，请稍后重试。");
      if ([400, 404, 410].includes(error?.status) || error?.code === "invalid_ticket") {
        message = "凭证无效、已失效或已使用。如有需要，请向管理员申请新凭证。";
        fragmentTicket = "";
        secret.reset();
        reflectTicket();
      } else if (error?.code === "invalid_password") {
        message = "密码不符合要求。请使用至少 8 字符且不类似账号信息、不是常用密码或全数字的密码，再试一次。";
      }
      password.value = "";
      confirm.value = "";
      if (ref && !message.includes(ref)) message += `\n请求 ID：${ref}`;
      feedback(result, message, "error", true);
    } finally {
      pending = false;
      busy(secret, false);
    }
  });
  window.addEventListener("pagehide", () => {
    fragmentTicket = "";
    secret.reset();
    reflectTicket();
  });
  secret.hidden = false;
}
