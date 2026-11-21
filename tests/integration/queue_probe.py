"""P21 real HTTP/PostgreSQL/Redis/separate-worker acceptance, synthetic data only.

Run only against the dedicated P21 app on 127.0.0.1:8061, PostgreSQL database
customs_p21 on 55421 and Redis 63821/0. Stop the dedicated external P21 workers
and dispatcher first: this probe starts and kills only its own children.

Example (with the same explicit P21 environment as the app)::

    python tests/integration/queue_probe.py

The app and every child must share the explicit short acceptance settings:
CUSTOMS_QUEUE_TIMEOUT=15, CUSTOMS_QUEUE_LEASE_SECONDS=20,
CUSTOMS_QUEUE_REDELIVERY_SECONDS=3, CUSTOMS_RETRY_BACKOFF_SECONDS=1 and
CUSTOMS_ROW_MAX_ATTEMPTS=3 (the project default). The probe refuses production
lease/timeout values rather than changing the running app's configuration.

Optional real broker-outage coverage requires both --redis-server and
--redis-data-dir. The live broker's configured directory must exactly match
the explicitly supplied P21-only directory before any shutdown is attempted.
Absent these options, broker-outage coverage is reported as not executed.

This is Windows SimpleWorker/local-runtime evidence, not Linux/container,
real-model, legal-classification or business-signoff evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".local" / "p21" / "queue"
BASE = "http://127.0.0.1:8061"
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def setup_django():
    """Never fall back to checkout .env or a non-P21 database/broker."""
    if not __debug__:
        raise SystemExit("P21 queue acceptance requires assertions; disable -O/PYTHONOPTIMIZE")
    required = {
        "CUSTOMS_DB_BACKEND": "postgres", "CUSTOMS_PG_NAME": "customs_p21",
        "CUSTOMS_PG_USER": "customs_p21", "CUSTOMS_PG_HOST": "127.0.0.1",
        "CUSTOMS_PG_PORT": "55421", "CUSTOMS_EXECUTOR_MODE": "rq",
        "CUSTOMS_ENGINE_MODE": "mock", "CUSTOMS_NOTIFICATION_MODE": "mock",
        "CUSTOMS_REDIS_URL": "redis://127.0.0.1:63821/0",
        "CUSTOMS_QUEUE_NAME": "customs-p21",
        "CUSTOMS_QUEUE_TIMEOUT": "15", "CUSTOMS_QUEUE_LEASE_SECONDS": "20",
        "CUSTOMS_QUEUE_REDELIVERY_SECONDS": "3", "CUSTOMS_RETRY_BACKOFF_SECONDS": "1",
    }
    for name, value in required.items():
        if os.environ.get(name) != value:
            raise SystemExit(f"P21 isolation required: explicitly set {name}={value}")
    if not os.environ.get("DJANGO_SECRET_KEY"):
        raise SystemExit("Set a dedicated P21 test DJANGO_SECRET_KEY")
    sys.path.insert(0, str(ROOT))
    # config.settings normally loads .env. This probe accepts only explicit env.
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    import django
    django.setup()
    from django.conf import settings
    if (settings.DB_BACKEND != "postgres" or settings.ENGINE_MODE != "mock"
            or settings.EXECUTOR_MODE != "rq" or settings.CUSTOMS_QUEUE_NAME != "customs-p21"):
        raise SystemExit("P21 application settings mismatch")
    if settings.CUSTOMS_ROW_MAX_ATTEMPTS != 3:
        raise SystemExit("P21 queue acceptance requires CUSTOMS_ROW_MAX_ATTEMPTS=3")


class Session:
    def __init__(self, username):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.request("GET", "/api/v1/me", expected=401)
        self.request("POST", "/api/v1/auth/login",
                     {"username": username, "password": "customs-demo-only"})

    def request(self, method, path, body=None, *, expected=200, raw=False,
                content_type=None, key=None):
        headers = {}
        if method != "GET":
            headers["X-CSRFToken"] = next(c.value for c in self.cookies if c.name == "csrftoken")
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
        if body is not None:
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"
        request = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=35)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            assert response.code == expected, (method, path, response.code, content[:1000])
            return content if raw else json.loads(content)

    def upload(self):
        from tests.p06.samples import sample
        data, boundary = sample(), uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="P21-queue-synthetic.xlsx"\r\n'
                'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        before = time.monotonic()
        result = self.request("POST", "/api/v1/tasks", body,
                              content_type=f"multipart/form-data; boundary={boundary}")
        return result, round(time.monotonic() - before, 3)


def wait_for(function, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for actual P21 worker progress")


def finished(session, run_id):
    result = session.request("GET", f"/api/v1/runs/{run_id}")
    return result if result["status"] in ("succeeded", "failed", "superseded") else None


def worker(mode, fault_run, marker):
    """Explicit fault injection targets one synthetic Run, never other tasks."""
    import django_rq
    from apps.runs import services
    original = services.evaluate_row
    blocked = False

    def evaluate(run, item):
        nonlocal blocked
        target = str(run.pk) == fault_run
        if mode == "block" and target and not blocked:
            blocked = True
            marker.write_text(json.dumps({"run_id": str(run.pk), "item_id": str(item.pk),
                                          "pid": os.getpid()}), encoding="utf-8")
            time.sleep(60)
        if mode == "fail-first" and target and run.items.order_by("item__source_row").first().item_id == item.pk:
            raise TimeoutError("Explicit P21 single-run test injection")
        return original(run, item)

    with patch.object(services, "evaluate_row", side_effect=evaluate):
        django_rq.get_worker("customs-p21").work()


def crash_after_commit(result_path):
    from apps.accounts.models import User
    from apps.reviews.services.imports import upload
    from apps.runs.models import Run, RunWork
    from tests.p06.samples import sample

    def exit_before_dispatch(run_id):
        run = Run.objects.get(pk=run_id)
        assert RunWork.objects.filter(run=run, state="pending", dispatched_at__isnull=True).exists()
        result_path.write_text(json.dumps({"run_id": str(run.pk), "task_id": str(run.task_id)}), encoding="utf-8")
        os._exit(91)

    with patch("apps.runs.dispatch.safe_dispatch", side_effect=exit_before_dispatch):
        upload(User.objects.get(username="supplier_a"), filename="P21-after-commit-crash.xlsx",
               data=sample(), key=uuid.uuid4().hex)
    raise AssertionError("Expected after-commit process exit did not occur")


def broker_configuration(options):
    if bool(options.redis_server) != bool(options.redis_data_dir):
        raise SystemExit("Supply both --redis-server and --redis-data-dir, or neither")
    if not options.redis_server:
        return None
    binary = options.redis_server.resolve(strict=True)
    directory = options.redis_data_dir.resolve(strict=True)
    if binary.name.lower() not in ("redis-server", "redis-server.exe"):
        raise SystemExit("Explicit Redis server executable required")
    temp = Path(os.environ.get("TEMP", "/tmp")).resolve()
    private_temp = directory.is_relative_to(temp) and any(
        part.startswith("customs-p21-runtime-") for part in directory.parts)
    private_project = directory.is_relative_to((ROOT / ".local" / "p21").resolve())
    if not directory.is_dir() or not (private_temp or private_project):
        raise SystemExit("Broker outage requires an explicitly owned P21 data directory")
    from redis import Redis
    client = Redis(host="127.0.0.1", port=63821)
    config = client.config_get("dir")
    raw_directory = config.get("dir", config.get(b"dir"))
    if isinstance(raw_directory, bytes):
        raw_directory = raw_directory.decode()
    # redis-windows reports Cygwin's canonical directory, while the explicit
    # ownership path uses native Windows syntax. Normalize the drive spelling
    # before comparing; still require exact resolved-directory equality.
    cygwin = re.fullmatch(r"/cygdrive/([a-zA-Z])(/.*)", raw_directory) if os.name == "nt" else None
    if cygwin:
        raw_directory = f"{cygwin[1]}:{cygwin[2]}"
    actual = Path(raw_directory).resolve()
    if actual != directory:
        raise SystemExit("Live Redis directory differs from supplied P21 directory; refusing shutdown")
    info = client.info("server")
    if info.get("tcp_port") != 63821:
        raise SystemExit("Redis server identity mismatch")
    return {"binary": binary, "directory": directory, "initial_pid": info["process_id"]}


def probe(options):
    from apps.runs.models import Run, RunWork
    from tests.p06.samples import sample
    from redis import Redis
    OUT.mkdir(parents=True, exist_ok=True)
    # Check broker ownership before any synthetic business writes.
    broker = broker_configuration(options)
    report = {"mode": "mock", "backend": "postgres", "executor": "rq",
              "runtime": f"{os.name}; PostgreSQL + Redis + separate worker",
              "professional_quality": "not_evaluated", "checks": {}, "result": "running"}
    processes, logs = [], []

    def persist():
        (OUT / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(arguments, *, name="process", cwd=ROOT):
        log = (OUT / f"{name}-{len(logs)}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen([str(argument) for argument in arguments], cwd=cwd,
                                   stdout=log, stderr=log, creationflags=FLAGS)
        processes.append(process)
        return process

    def start_worker(mode="normal", run_id="", marker=None):
        args = [sys.executable, Path(__file__), "--worker-mode", mode]
        if run_id:
            args += ["--fault-run", run_id]
        if marker:
            args += ["--marker", marker]
        return start(args, name="worker")

    def start_dispatcher():
        return start([sys.executable, ROOT / "manage.py", "dispatch_runs", "--watch", "--interval", "0.5"],
                     name="dispatcher")

    def stop(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False, creationflags=FLAGS)
        else:
            process.kill()
        process.wait(timeout=10)

    def stop_all():
        for process in reversed(processes):
            stop(process)

    def checked(name):
        report["checks"][name] = "passed"
        persist()
        print(f"PASS {name}", flush=True)

    def restart_broker():
        log = (OUT / "redis-restarted.log").open("ab")
        # The restored broker remains available for the dedicated P21 app.
        process = subprocess.Popen([str(broker["binary"]), "--bind", "127.0.0.1", "--port", "63821",
            "--protected-mode", "yes", "--dir", str(broker["directory"]), "--appendonly", "yes",
            "--logfile", str(broker["directory"] / "redis.log")], cwd=broker["directory"],
            stdout=log, stderr=log, creationflags=FLAGS)
        log.close()
        report["redis_restarted_pid"] = process.pid
        from redis.exceptions import ConnectionError as RedisConnectionError

        def ready():
            try:
                return Redis(host="127.0.0.1", port=63821, socket_connect_timeout=.3).ping()
            except RedisConnectionError:
                return False

        wait_for(ready, 10)

    broker_stopped = False
    persist()
    try:
        supplier, operator, expert = Session("supplier_a"), Session("operator_a"), Session("expert")
        uploaded, report["upload_response_seconds"] = supplier.upload()
        report["task_id"] = uploaded["task_id"]
        assert uploaded["parse_status"] == "staged" and uploaded["executor_mode"] == "rq"
        assert not Run.objects.get(pk=uploaded["precheck_run_id"]).items.exists()
        checked("T01_http_returns_before_actual_worker")
        marker = OUT / f"crash-{uuid.uuid4().hex}.json"
        process = start_worker("block", uploaded["precheck_run_id"], marker)
        wait_for(marker.exists, 30)
        assert json.loads(marker.read_text())["run_id"] == uploaded["precheck_run_id"]
        stop(process)
        start_dispatcher()
        process = start_worker()
        run = wait_for(lambda: finished(supplier, uploaded["precheck_run_id"]))
        assert run["progress"]["succeeded"] == 3
        assert any(attempt["status"] == "interrupted" for row in run["items"] for attempt in row["attempt_history"])
        checked("T03_killed_worker_recovered_with_interrupted_attempt")
        operator.request("POST", f'/api/v1/tasks/{uploaded["task_id"]}/claim')
        lines = []
        for row in run["items"]:
            card = operator.request("GET", f'/api/v1/versions/{uploaded["version_id"]}/items/{row["item_id"]}')
            confirmation = operator.request("PATCH", f'/api/v1/versions/{uploaded["version_id"]}/items/{row["item_id"]}/confirmation',
                {"confirmed_hs": row["suggested_hs"], "expected_revision": card["revision"],
                 "reason": "P21 synthetic technical acceptance, no professional classification approval"})
            lines.append({"item_id": row["item_id"], "confirmation_id": confirmation["confirmation_id"]})
        task = operator.request("GET", f'/api/v1/tasks/{uploaded["task_id"]}')
        before = time.monotonic()
        submission = operator.request("POST", f'/api/v1/tasks/{uploaded["task_id"]}/submissions',
            {"version_id": uploaded["version_id"], "revision": task["revision"], "items": lines})
        report["submission_response_seconds"] = round(time.monotonic() - before, 3)
        expert.request("GET", f'/api/v1/tasks/{uploaded["task_id"]}')
        assert wait_for(lambda: finished(expert, submission["expert_run_id"]))["progress"]["succeeded"] == 3
        checked("T02_submission_automatically_reaches_expert_worker")
        stop(process)
        failed_upload, _ = supplier.upload()
        process = start_worker("fail-first", failed_upload["precheck_run_id"])
        failed = wait_for(lambda: finished(supplier, failed_upload["precheck_run_id"]))
        assert failed["progress"]["failed"] == 1 and failed["progress"]["succeeded"] == 2
        assert next(row for row in failed["items"] if row["status"] == "failed")["attempts"] == 3
        operator.request("POST", f'/api/v1/tasks/{failed_upload["task_id"]}/claim')
        allowed = operator.request("GET", f'/api/v1/runs/{failed["run_id"]}')
        assert allowed["retry_allowed"]
        old_rows = list(Run.objects.get(pk=failed["run_id"]).items.values("id", "status", "result"))
        stop(process)
        child = operator.request("POST", f'/api/v1/runs/{failed["run_id"]}/retries', {"item_ids": allowed["retry_item_ids"]})
        process = start_worker()
        retried = wait_for(lambda: finished(operator, child["run_id"]))
        assert retried["progress"]["succeeded"] == 3
        assert sum(bool(row["attempt_history"]) for row in retried["items"]) == 1
        assert old_rows == list(Run.objects.get(pk=failed["run_id"]).items.values("id", "status", "result"))
        checked("A09_T04_finite_failure_and_manual_retry_preserve_history")
        task = operator.request("GET", f'/api/v1/tasks/{failed_upload["task_id"]}')
        key, body = str(uuid.uuid4()), {"revision": task["revision"]}
        path = f'/api/v1/versions/{failed_upload["version_id"]}/prechecks'
        rerun, replay = operator.request("POST", path, body, key=key), operator.request("POST", path, body, key=key)
        assert rerun["run_id"] == replay["run_id"]
        assert wait_for(lambda: finished(operator, rerun["run_id"]))["progress"]["succeeded"] == 3
        checked("A15_same_version_rerun_idempotency")
        original = supplier.request("GET", uploaded["original_download_url"], raw=True)
        assert hashlib.sha256(original).hexdigest() == uploaded["sha256"]
        checked("original_hash_unchanged")
        stop_all()
        crash_file = OUT / f"commit-crash-{uuid.uuid4().hex}.json"
        with (OUT / "commit-crash.log").open("wb") as log:
            crash = subprocess.run([sys.executable, Path(__file__), "--commit-crash-result", crash_file],
                                   cwd=ROOT, stdout=log, stderr=log, timeout=20, creationflags=FLAGS)
        assert crash.returncode == 91
        persisted = json.loads(crash_file.read_text())
        assert RunWork.objects.filter(run_id=persisted["run_id"], state="pending", dispatched_at__isnull=True).exists()
        start_dispatcher()
        process = start_worker()
        assert wait_for(lambda: finished(supplier, persisted["run_id"]))["progress"]["succeeded"] == 3
        checked("T03_request_process_exit_after_commit_recovered")
        stop_all()
        if broker:
            # Directory match was checked before uploads; recheck immediately before shutdown.
            broker_configuration(options)
            Redis(host="127.0.0.1", port=63821).shutdown(save=True)
            broker_stopped = True
            lost, _ = supplier.upload()
            assert RunWork.objects.filter(run_id=lost["precheck_run_id"], state="pending", dispatched_at__isnull=True).exists()
            restart_broker()
            broker_stopped = False
            start_dispatcher()
            process = start_worker()
            assert wait_for(lambda: finished(supplier, lost["precheck_run_id"]))["progress"]["succeeded"] == 3
            checked("T03_actual_broker_outage_recovered")
        else:
            report["checks"]["T03_actual_broker_outage_recovered"] = "not_executed_no_owned_broker_restart_configuration"
        report["result"] = "passed"
        (OUT / "P21-queue-synthetic.xlsx").write_bytes(sample())
        persist()
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    except BaseException as error:
        report.update(result="failed", error_type=type(error).__name__, error=str(error)[:1000])
        persist()
        raise
    finally:
        stop_all()
        if broker_stopped:
            restart_broker()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-server", type=Path)
    parser.add_argument("--redis-data-dir", type=Path)
    parser.add_argument("--worker-mode", choices=("normal", "block", "fail-first"))
    parser.add_argument("--fault-run", default="")
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--commit-crash-result", type=Path)
    options = parser.parse_args()
    setup_django()
    if options.worker_mode:
        if options.worker_mode != "normal" and (not options.fault_run or (options.worker_mode == "block" and not options.marker)):
            parser.error("Fault workers require an explicit run ID and block workers require a marker")
        worker(options.worker_mode, options.fault_run, options.marker)
    elif options.commit_crash_result:
        crash_after_commit(options.commit_crash_result)
    else:
        probe(options)
