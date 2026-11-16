"""P22：部署资产（Dockerfile / compose / env 模板 / nginx / dockerignore）的结构检查。

这些用例不启动容器——没有 Docker 的机器上也要能跑，所以检查的是“资产形状”而不是运行结果：
- 运行进程不能是开发服务器；
- 数据库不能发布端口；
- 模板里不能出现真实凭据；
- 破坏性操作不能出现在备份脚本里。
真正跑容器的证据写在 docs/开发文档/交接/P22.md，不靠这些用例冒充。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_lines(text: str) -> str:
    """去掉整行注释，只留下会真正执行的配置行。"""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.text = read(DEPLOY / "Dockerfile")

    def test_runs_gunicorn_not_dev_server(self):
        # 只看真正会被执行的行：注释里出现 “不是 runserver” 的说明不算违规。
        code = code_lines(self.text)
        self.assertIn("gunicorn", code)
        self.assertIn("config.wsgi:application", code)
        self.assertNotIn("runserver", code, "runserver 只用于本机调试，不应出现在镜像命令里")

    def test_compose_never_starts_dev_server(self):
        self.assertNotIn("runserver", code_lines(read(DEPLOY / "docker-compose.yml")))

    def test_base_image_pinned_and_non_root(self):
        from_lines = [line for line in self.text.splitlines() if line.startswith("FROM ")]
        self.assertTrue(from_lines, "Dockerfile 必须有 FROM")
        for line in from_lines:
            self.assertNotIn(":latest", line, f"基础镜像不要用 latest：{line}")
            self.assertNotRegex(line, r"@?sha256", "不需要固定 digest，但版本 tag 要写清楚")
        self.assertRegex(self.text, r"(?m)^USER app$", "必须以非 root 用户运行")
        self.assertIn("chown -R app:app", self.text)

    def test_installs_locked_dependencies(self):
        self.assertIn("uv sync --locked", self.text)
        # gunicorn 是部署期补充，不进 uv.lock（避免改动公共依赖锁文件）。
        self.assertRegex(self.text, r"gunicorn==\d+\.\d+\.\d+")


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.text = read(DEPLOY / "docker-compose.yml")

    def test_required_services_present(self):
        for service in ("db:", "app:", "migrate:", "web:"):
            self.assertIn(service, self.text)
        self.assertIn("image: postgres:17", self.text)

    def test_only_one_service_publishes_ports(self):
        # db 不能对外发布端口；只有 web 映射端口。
        self.assertEqual(len(re.findall(r"(?m)^    ports:$", self.text)), 1)

    def test_database_is_not_published(self):
        db_block = self.text.split("  db:", 1)[1].split("\n  migrate:", 1)[0]
        self.assertNotIn("ports:", db_block)
        self.assertIn("pgdata:/var/lib/postgresql/data", db_block)

    def test_persistent_volumes_declared(self):
        volumes_block = self.text.split("\nvolumes:\n", 1)[1]
        for name in ("pgdata:", "files:", "static:"):
            self.assertIn(name, volumes_block)
        self.assertIn("files:/srv/customs", self.text)

    def test_password_has_no_default(self):
        # 没有默认密码：缺变量时 compose 直接报错，而不是起一个空口令库。
        self.assertIn("CUSTOMS_PG_PASSWORD:?", self.text)

    def test_queue_services_are_opt_in_and_marked_unavailable(self):
        self.assertIn('profiles: ["queue"]', self.text)
        # 必须写明 P13 未实现，避免有人以为启用了队列。
        self.assertIn("P13", self.text)
        self.assertIn("尚未实现", self.text)

    def test_migrate_runs_before_app(self):
        self.assertIn("service_completed_successfully", self.text)
        self.assertIn("python manage.py migrate --noinput", self.text)
        self.assertIn("collectstatic", self.text)


class EnvTemplateTests(unittest.TestCase):
    REQUIRED_KEYS = (
        "DJANGO_SECRET_KEY",
        "CUSTOMS_PG_PASSWORD",
        "CUSTOMS_ALLOWED_HOSTS",
        "CUSTOMS_HTTP_PORT",
        "CUSTOMS_DEBUG",
        "CUSTOMS_CSRF_TRUSTED_ORIGINS",
        "CUSTOMS_HTTPS",
    )

    def setUp(self):
        self.text = read(DEPLOY / ".env.example")

    def test_required_keys_documented(self):
        for key in self.REQUIRED_KEYS:
            self.assertRegex(self.text, rf"(?m)^#?\s*{key}=", f"模板缺少 {key}")

    def test_secret_values_are_empty(self):
        """两项敏感值是空值：逼部署者自己生成，绝不带示例口令进仓库。"""
        for key in ("DJANGO_SECRET_KEY", "CUSTOMS_PG_PASSWORD"):
            match = re.search(rf"(?m)^{key}=(.*)$", self.text)
            self.assertIsNotNone(match, f"{key} 必须有独立的行")
            self.assertEqual(match.group(1).strip(), "", f"{key} 在模板里必须是空值")

    def test_no_real_looking_secret(self):
        for line in self.text.splitlines():
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip()
            # 32 位以上、混合大小写与数字的串当真实凭据处理，直接判失败。
            self.assertFalse(
                re.fullmatch(r"[A-Za-z0-9+/=_-]{32,}", value),
                f"模板里疑似真实凭据：{line.split('=', 1)[0]}",
            )

    def test_real_env_file_not_committed(self):
        self.assertFalse((DEPLOY / ".env").exists(), "deploy/.env 是本地文件，不应出现在工作区提交里")


class NginxAndIgnoreTests(unittest.TestCase):
    def test_nginx_serves_static_and_proxies(self):
        text = read(DEPLOY / "nginx" / "default.conf")
        self.assertIn("proxy_pass http://app:8000", text)
        self.assertIn("alias /usr/share/nginx/static/", text)
        self.assertIn("X-Forwarded-Proto", text)
        # 与 CUSTOMS_FILE_MAX_BYTES（100MB）留余量
        self.assertRegex(text, r"client_max_body_size\s+1[0-9]{2}m")

    def test_https_example_does_not_shadow_default(self):
        # 示例文件不能被 nginx 直接 include，否则会因缺证书起不来。
        https_example = DEPLOY / "nginx" / "https.conf.example"
        self.assertTrue(https_example.exists())
        self.assertFalse((DEPLOY / "nginx" / "https.conf").exists())

    def test_dockerignore_excludes_secrets_and_data(self):
        text = read(REPO_ROOT / ".dockerignore")
        self.assertIn("deploy/.env", text)
        self.assertIn("data/", text)
        self.assertIn(".git", text)


if __name__ == "__main__":
    unittest.main()
