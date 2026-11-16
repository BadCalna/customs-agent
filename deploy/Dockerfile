# P22 云端多人测试（单台服务器）用的应用镜像。
#
# 要点：
# - 构建上下文是仓库根目录（compose 里 build.context: ..），因此能读到 pyproject.toml
#   与 uv.lock，装出来的依赖和本机 .venv-customs 是同一份锁定集合。
# - 运行进程是 gunicorn，不是 `manage.py runserver`。开发服务器只用于本机调试，
#   不进入任何对外长期运行的配置。
# - 镜像内 Python 固定 3.13：uv.lock 是通用锁，3.12～3.14 都能装；3.13 的
#   psycopg[binary] / Pillow 轮子最齐，构建时不需要编译工具链。本机开发环境是 3.14，
#   兼容性由 lock 的 requires-python 范围保证（见 pyproject.toml）。
FROM python:3.13-slim

# uv 只用于按锁文件装依赖；版本固定，避免“今天能构建、明天不能”。
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=3.13 \
    UV_LINK_MODE=copy

# 非 root 运行：容器里只有应用进程，不需要 root 权限。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# 先只拷依赖清单，让依赖层可缓存；代码变动不必重装依赖。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python 3.13 \
    && uv pip install --python /app/.venv/bin/python "gunicorn==23.0.0"

COPY . .

# 两个可写目录：受控文件存储根（原件/图片/导出）与 collectstatic 输出。
# 权限在这里一次给足，compose 的命名卷首次创建会继承这些目录的属主。
RUN mkdir -p /srv/customs/files /app/staticfiles \
    && chown -R app:app /srv/customs /app/staticfiles /app

USER app

ENV PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=deploy.settings_cloud

EXPOSE 8000

# --access-logfile - 让 nginx/容器日志里能看到请求；超时覆盖大文件上传的解析时间。
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
