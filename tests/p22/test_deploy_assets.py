"""P22 离线资产契约：结构解析、真实 git 忽略行为；不冒充 Docker/nginx 实测。"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def mapping(text: str, indent: int = 0) -> dict[str, str]:
    """只提取固定缩进下的 YAML 映射块（不解析值、不引入未锁定的 YAML 包）。"""
    lines = text.splitlines()
    starts = [(i, match.group(1)) for i, line in enumerate(lines)
              if (match := re.match(rf"^{' ' * indent}([\w-]+):(?:\s|$)", line))]
    return {key: "\n".join(lines[start:(starts[n + 1][0] if n + 1 < len(starts) else len(lines))])
            for n, (start, key) in enumerate(starts)}


def nginx_tree(text: str) -> list:
    """解析本仓 nginx 分号/大括号结构，能区分 server return 与 location return。"""
    text = re.sub(r"(?m)#.*$", "", text)
    tokens = re.findall(r'"[^"\\]*(?:\\.[^"\\]*)*"|[{};]|[^\s{};]+', text)
    root, stack, words = [], [], []
    current = root
    for token in tokens:
        if token == "{":
            child = []
            current.append((tuple(words), child))
            stack.append(current)
            current, words = child, []
        elif token == "}":
            if words or not stack:
                raise AssertionError("invalid nginx braces")
            current = stack.pop()
        elif token == ";":
            current.append((tuple(words), None))
            words = []
        else:
            words.append(token)
    if stack or words:
        raise AssertionError("unfinished nginx directive")
    return root


def docker_ignored(path: str, text: str) -> bool:
    """本仓 ignore 模式的受限匹配器：仅覆盖 glob、目录前缀、!，非 Docker 运行证明。"""
    ignored = False
    for pattern in text.splitlines():
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        include = pattern.startswith("!")
        pattern = pattern.lstrip("!")
        variants = [pattern]
        if pattern.startswith("**/"):
            variants.append(pattern[3:])
        matched = any(fnmatch.fnmatchcase(path, variant) or
                      (variant.endswith("/") and any(
                          fnmatch.fnmatchcase("/".join(path.split("/")[:index]), variant.rstrip("/"))
                          for index in range(1, len(path.split("/")))))
                      for variant in variants)
        if matched:
            ignored = not include
    return ignored


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.code = code_lines(read(DEPLOY / "Dockerfile"))

    def test_runs_locked_gunicorn_nonroot(self):
        self.assertIn("gunicorn", self.code)
        self.assertIn("config.wsgi:application", self.code)
        self.assertNotIn("runserver", self.code)
        self.assertIn("uv sync --locked", self.code)
        self.assertRegex(self.code, r"gunicorn==\d+\.\d+\.\d+")
        self.assertRegex(self.code, r"(?m)^USER app$")
        self.assertNotIn(":latest", self.code)

    def test_code_and_venv_are_not_runtime_user_owned(self):
        chowns = re.findall(r"chown\s+-R\s+app:app\s+([^\n&]+)", self.code)
        self.assertTrue(chowns)
        targets = {path for command in chowns for path in command.split()}
        self.assertEqual(targets, {"/srv/customs/files", "/srv/customs/restores", "/app/staticfiles"})
        mkdir = re.search(r"mkdir\s+-p\s+([^\n&]+)", self.code)
        self.assertIsNotNone(mkdir)
        self.assertTrue(targets <= set(mkdir.group(1).split()))
        self.assertIn("chmod 0755 /srv/customs", self.code)
        self.assertIn("chmod -R go-w /app", self.code)
        self.assertNotIn("COPY --chown=app", self.code)
        self.assertNotIn("/srv/customs", targets, "卷根不可被运行用户替换")


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.text = code_lines(read(DEPLOY / "docker-compose.yml"))
        self.top = mapping(self.text)
        self.services = mapping(self.top["services"], 2)
        self.app = mapping(self.top["x-app"], 2)

    def test_required_services_and_only_web_ports(self):
        self.assertTrue({"db", "app", "migrate", "web", "redis", "worker", "dispatcher"} <= self.services.keys())
        published = [name for name, block in self.services.items() if "ports" in mapping(block, 4)]
        self.assertEqual(published, ["web"])
        self.assertIn("pgdata:/var/lib/postgresql/data", self.services["db"])
        self.assertIn("CUSTOMS_PG_PASSWORD:?", self.services["db"])
        self.assertNotIn("runserver", self.text)

    def test_persistent_volumes_and_migrate_order(self):
        self.assertTrue({"pgdata", "files", "static"} <= mapping(self.top["volumes"], 2).keys())
        for name in ("migrate", "app", "worker", "dispatcher"):
            self.assertIn("files:/srv/customs", self.services[name])
        self.assertIn("CUSTOMS_FILE_ROOT:-/srv/customs/files", self.app["environment"])
        self.assertIn("service_completed_successfully", self.services["app"])
        self.assertIn("migrate --noinput", self.services["migrate"])
        self.assertIn("collectstatic --noinput", self.services["migrate"])

    def test_healthcheck_uses_the_loopback_host_covered_by_behavior_test(self):
        block = mapping(self.services["app"], 4)["healthcheck"]
        command = json.loads(re.search(r"test:\s*(\[.*\])", block).group(1))
        self.assertEqual(command[:3], ["CMD", "python", "-c"])
        self.assertIn("http://127.0.0.1:8000/api/health", command[3])
        compile(command[3], "compose-healthcheck", "exec")

    def test_all_services_have_bounded_logs_and_shutdown_grace(self):
        log = self.top["x-logging"]
        self.assertIn("driver: json-file", log)
        self.assertIn('max-size: "10m"', log)
        self.assertIn('max-file: "3"', log)
        for name, block in self.services.items():
            own = mapping(block, 4)
            inherited = "<<: *app" in block
            logging = own.get("logging", self.app.get("logging", "") if inherited else "")
            grace = own.get("stop_grace_period", self.app.get("stop_grace_period", "") if inherited else "")
            with self.subTest(service=name):
                self.assertIn("*logging", logging)
                seconds = int(re.search(r": (\d+)s", grace).group(1))
                self.assertGreaterEqual(seconds, 135 if name in {"app", "migrate", "worker", "web"} else 60)
                self.assertLessEqual(seconds, 180)

    def test_queue_services_start_with_app(self):
        for name in ("worker", "redis", "dispatcher"):
            self.assertNotIn("profiles:", self.services[name])
        self.assertIn("CUSTOMS_EXECUTOR_MODE: rq", self.app["environment"])
        self.assertIn("CUSTOMS_REDIS_URL: redis://redis:6379/0", self.app["environment"])
        self.assertIn("rqworker", self.services["worker"])
        self.assertIn("CUSTOMS_QUEUE_NAME:-customs", self.services["worker"])
        self.assertIn("dispatch_runs", self.services["dispatcher"])
        for name in ("worker", "dispatcher"):
            self.assertIn("service_completed_successfully", self.services[name])


class EnvTemplateTests(unittest.TestCase):
    def test_required_values_are_blank(self):
        values = dict(line.split("=", 1) for line in read(DEPLOY / ".env.example").splitlines()
                      if line and not line.startswith("#") and "=" in line)
        for key in ("DJANGO_SECRET_KEY", "CUSTOMS_PG_PASSWORD", "CUSTOMS_ALLOWED_HOSTS"):
            self.assertEqual(values[key], "", f"{key} 必须让部署者显式填写")
        for key in ("CUSTOMS_AI_API_BASE", "CUSTOMS_AI_API_KEY", "CUSTOMS_AI_MODEL"):
            self.assertEqual(values[key], "", f"{key} 必须让部署者显式填写")

    def test_sensitive_local_files_are_not_git_tracked(self):
        result = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                                capture_output=True, check=True)
        # 只检查索引路径，允许服务器存在被忽略的 .env / https.conf；不读取或回显配置正文。
        tracked = result.stdout.decode().split("\0")
        bad = [path for path in tracked if path and (
            (Path(path).name == ".env" or Path(path).name.startswith(".env."))
            and Path(path).name != ".env.example"
            or path == "deploy/nginx/https.conf"
            or Path(path).suffix in {".key", ".pem", ".p12", ".pfx"})]
        self.assertFalse(bool(bad), "git 索引包含真实环境配置或私钥；不输出具体值")

    def test_git_actually_ignores_operator_outputs_but_keeps_example(self):
        paths = ["deploy/.env", "nested/.env.production", "deploy/backups/a.dump",
                 "deploy/.state/state.json", "deploy/certs/cert.pem", "deploy/nginx/https.conf"]
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=REPO_ROOT,
                                input="\n".join(paths) + "\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(result.stdout.splitlines()), set(paths))
        result = subprocess.run(["git", "check-ignore", "--no-index", "deploy/.env.example"],
                                cwd=REPO_ROOT, capture_output=True)
        self.assertEqual(result.returncode, 1)


class NginxAndIgnoreTests(unittest.TestCase):
    def test_nginx_structures_preserve_host_port_and_static(self):
        for filename in ("default.conf", "https.conf.example"):
            tree = nginx_tree(read(DEPLOY / "nginx" / filename))
            host_map = next(body for words, body in tree
                            if words == ("map", "$http_host", "$customs_forwarded_host"))
            self.assertIn((("default", "$http_host"), None), host_map)
            self.assertIn((('""', "$host"), None), host_map)
            servers = [(words, body) for words, body in tree if words == ("server",)]
            proxies = [body for _, server in servers for directive, body in server
                       if directive == ("location", "/") and body and
                       (("proxy_pass", "http://app:8000"), None) in body]
            self.assertEqual(len(proxies), 1)
            self.assertIn((("proxy_set_header", "Host", "$customs_forwarded_host"), None), proxies[0])
            forwarded_proto = "$customs_forwarded_proto" if filename == "default.conf" else "$scheme"
            self.assertIn((("proxy_set_header", "X-Forwarded-Proto", forwarded_proto), None), proxies[0])
            if filename == "default.conf":
                proto_map = next(body for words, body in tree
                                 if words == ("map", "$http_x_forwarded_proto", "$customs_forwarded_proto"))
                self.assertIn((("https", "https"), None), proto_map)
            self.assertTrue(any((("alias", "/usr/share/nginx/static/"), None) in (body or [])
                                for _, server in servers for _, body in server))

    def test_tls_http_health_not_intercepted_by_server_redirect(self):
        servers = nginx_tree(read(DEPLOY / "nginx" / "https.conf.example"))
        http = next(server for words, server in servers if words == ("server",)
                    and (("listen", "80", "default_server"), None) in server)
        self.assertFalse(any(words[0] == "return" for words, _ in http))
        health = next(body for words, body in http if words == ("location", "=", "/nginx-health"))
        self.assertTrue(any(words[:2] == ("return", "200") for words, _ in health))
        redirect = next(body for words, body in http if words == ("location", "/"))
        self.assertIn((("return", "301", "https://$host$request_uri"), None), redirect)
        # 无端口的 https Location 与模板 Origin 及发布端口都采用标准 443。
        text = read(DEPLOY / "nginx" / "https.conf.example")
        self.assertIn('"443:443"', text)
        self.assertNotIn("8443", text)
        self.assertIn("CUSTOMS_CSRF_TRUSTED_ORIGINS=https://<你的域名>", text)

    def test_https_active_config_not_automatically_mounted(self):
        # 运维机器可自行创建 https.conf；只检查仓库默认挂载不会误加载它。
        self.assertIn("./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro",
                      read(DEPLOY / "docker-compose.yml"))

    def test_readme_activates_tls_config_and_preserves_static_volume(self):
        guide = read(DEPLOY / "README.md")
        tls = guide.split("## 7. 域名与 HTTPS", 1)[1].split("## 8.", 1)[0]
        self.assertIn("./nginx/https.conf:/etc/nginx/conf.d/default.conf:ro", tls)
        self.assertIn("static:/usr/share/nginx/static:ro", tls)
        self.assertIn("必须替换", tls)
        self.assertIn("--force-recreate app web", tls)

    def test_readme_old_volume_permissions_and_disaster_db_bootstrap(self):
        guide = read(DEPLOY / "README.md")
        upgrade = guide.split("### 8.1 升级", 1)[1].split("### 8.2", 1)[0]
        for command in ("--user 0", "chown 0:0 /srv/customs", "chmod 0755 /srv/customs",
                        "chown 10001:10001 /srv/customs/files /srv/customs/restores"):
            self.assertIn(command, upgrade)
        recovery = guide.split("### 9.4 恢复到独立新环境", 1)[1].split("## 10.", 1)[0]
        self.assertLess(recovery.index("up -d --wait db"), recovery.index("scripts/operations/restore.sh"))
        self.assertIn("--no-deps app", recovery)
        self.assertIn("run --rm --no-deps app python manage.py collectstatic --noinput", recovery)
        self.assertLess(recovery.index("collectstatic --noinput"), recovery.index("up -d --no-deps web"))
        self.assertNotIn("python manage.py migrate", recovery)
        self.assertNotIn("up -d --build", recovery)

    def test_documented_compose_commands_use_explicit_env_file(self):
        guide = read(DEPLOY / "README.md")
        for line in guide.splitlines():
            if "docker compose -f deploy/docker-compose.yml" in line:
                self.assertIn("--env-file deploy/.env", line)

    def test_dockerignore_secret_paths_at_any_depth_and_examples(self):
        text = read(REPO_ROOT / ".dockerignore")
        for prefix in ("", "deploy/", "nested/deeper/"):
            for name in (".env", ".env.production", "tls.key", "tls.pem", "tls.p12", "id_ed25519"):
                self.assertTrue(docker_ignored(prefix + name, text), prefix + name)
            for name in (".env.example", "tls.key.example", "tls.pem.example"):
                self.assertFalse(docker_ignored(prefix + name, text), prefix + name)
        for path in ("deploy/backups/a.dump", "deploy/.state/state.json", "deploy/certs/a.pem", "data/private/a"):
            self.assertTrue(docker_ignored(path, text), path)


if __name__ == "__main__":
    unittest.main()
