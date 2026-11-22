import { createClient } from "/app/assets/api.js";

const api = createClient({ mode: "live" });
const form = document.querySelector("#login-form");
const username = form.elements.username;
const password = form.elements.password;
const submit = document.querySelector("#sign-in");
const toggle = document.querySelector("#toggle-password");
const feedback = document.querySelector("#login-feedback");
let pending = false;

function workspaceDestination() {
  const source = new URLSearchParams(location.search),
    keep = new URLSearchParams();
  for (const name of ["task", "version"]) {
    const value = source.get(name);
    if (value) keep.set(name, value);
  }
  const query = keep.toString();
  return query ? `/?${query}` : "/";
}

function message(error) {
  if (error?.status === 401 || error?.code === "invalid_credentials") return "账号或密码不正确，请检查后重试。";
  if (error?.code === "csrf_failed") return "登录校验已失效，请刷新页面后重新登录。";
  if (error?.status === 403) return "当前账号无法访问工作台，请联系业务管理员。";
  if (error?.status === 429) return "尝试次数较多，请稍后再登录。";
  if (!error?.status) return "未能连接到服务，请检查网络后重试。";
  return "登录服务暂时不可用，请稍后重试。";
}

toggle.addEventListener("click", () => {
  const visible = password.type === "password";
  password.type = visible ? "text" : "password";
  toggle.setAttribute("aria-pressed", String(visible));
  toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
});
form.addEventListener("input", () => {
  feedback.hidden = true;
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  pending = true;
  submit.disabled = true;
  form.setAttribute("aria-busy", "true");
  submit.firstElementChild.textContent = "正在登录…";
  feedback.hidden = true;
  try {
    await api.login(username.value, password.value);
    password.value = "";
    location.replace(workspaceDestination());
  } catch (error) {
    feedback.textContent = message(error);
    feedback.hidden = false;
    if (error?.status === 401) {
      password.focus();
      password.select();
    }
    pending = false;
    submit.disabled = false;
    submit.firstElementChild.textContent = "登录";
    form.setAttribute("aria-busy", "false");
  }
});
submit.disabled = false;
