"""Real HTTP + PostgreSQL + Redis + separate worker process acceptance.

Run against the isolated P13 localhost server, with no other P13 workers or
dispatcher active. This creates synthetic tasks, kills only its own worker and
restarts the dedicated broker. No source business Excel is modified.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
from apps.runs.models import Run, RunWork
from tests.p06.samples import sample


class Session:
    def __init__(self, base, username):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.request("GET", "/api/v1/me", expected=401)
        self.request("POST", "/api/v1/auth/login", {"username":username,"password":"customs-demo-only"})

    def request(self, method, path, body=None, *, expected=200, raw=False, content_type=None, key=None):
        headers = {}
        if method != "GET":
            headers["X-CSRFToken"] = next(c.value for c in self.cookies if c.name == "csrftoken")
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
        if body is not None:
            if not isinstance(body,bytes):
                body = json.dumps(body).encode()
            headers["Content-Type"] = content_type or "application/json"
        try:
            response = self.opener.open(urllib.request.Request(self.base+path,data=body,headers=headers,method=method),timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            assert response.code == expected, (method,path,response.code,data[:1000])
            return data if raw else json.loads(data)

    def upload(self):
        data, boundary = sample(), uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="P13-synthetic.xlsx"\r\n'
                'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        start=time.monotonic()
        result=self.request("POST","/api/v1/tasks",body,content_type=f"multipart/form-data; boundary={boundary}")
        return result, time.monotonic()-start


def wait_for(fn, timeout=40):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(0.2)
    raise AssertionError("timed out waiting for real worker progress")


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--base",default="http://127.0.0.1:8033")
    parser.add_argument("--leave-failed",action="store_true")
    args=parser.parse_args()
    if args.base not in ("http://127.0.0.1:8033",):
        raise SystemExit("This acceptance script is restricted to its isolated localhost port")
    if settings.DB_BACKEND != "postgres" or settings.CUSTOMS_QUEUE_NAME != "customs-p13" or settings.DATABASES["default"]["NAME"]!="customs_p13":
        raise SystemExit("requires isolated customs_p13 PostgreSQL database and queue")
    out=ROOT/".local/p13-live";out.mkdir(exist_ok=True)
    processes=[]
    logs=[]
    def start(script,*options):
        log=(out/f"process-{len(processes)}.log").open("wb"); logs.append(log)
        proc=subprocess.Popen([sys.executable,str(ROOT/script),*options],cwd=ROOT,stdout=log,stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        processes.append(proc)
        return proc
    def stop(proc):
        if proc.poll() is None:
            if os.name=="nt":
                subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],capture_output=True,check=False)
            else:proc.kill()
            proc.wait(timeout=10)
    def done(session,run_id):
        value=session.request("GET",f"/api/v1/runs/{run_id}")
        return value if value["status"] in ("succeeded","failed","superseded") else None
    report={"mode":"mock","runtime":"PostgreSQL + Redis + separate SimpleWorker; Windows local acceptance"}
    try:
        supplier=Session(args.base,"supplier_a")
        operator=Session(args.base,"operator_a")
        expert=Session(args.base,"expert")
        uploaded, elapsed=supplier.upload()
        assert uploaded["parse_status"]=="staged" and uploaded["executor_mode"]=="rq"
        assert not Run.objects.get(pk=uploaded["precheck_run_id"]).items.exists()
        report["upload_response_seconds"]=round(elapsed,3)
        report["task_id"]=uploaded["task_id"]
        print("HTTP upload returned before worker; starting actual interruption test",flush=True)
        marker=out/f"crash-{uuid.uuid4().hex}.json"
        worker=start("tests/p13/worker_probe.py","--marker",str(marker))
        wait_for(lambda: marker.exists(),30)
        blocked=json.loads(marker.read_text())
        assert blocked["run_id"]==uploaded["precheck_run_id"]
        stop(worker)
        start("manage.py","dispatch_runs","--watch","--interval","0.5")
        worker=start("tests/p13/worker_probe.py")
        run=wait_for(lambda:done(supplier,uploaded["precheck_run_id"]),40)
        assert run["progress"]["succeeded"]==3,run
        assert any(a["status"]=="interrupted" for row in run["items"] for a in row["attempt_history"])
        report["killed_worker_recovered"]=True
        print("Killed worker recovered from persistent row attempts",flush=True)
        operator.request("POST",f'/api/v1/tasks/{uploaded["task_id"]}/claim')
        lines=[]
        for row in run["items"]:
            card=operator.request("GET",f'/api/v1/versions/{uploaded["version_id"]}/items/{row["item_id"]}')
            confirmation=operator.request("PATCH",f'/api/v1/versions/{uploaded["version_id"]}/items/{row["item_id"]}/confirmation',
                {"confirmed_hs":row["suggested_hs"],"expected_revision":card["revision"],"reason":"P13 合成技术演练，未经专业归类确认"})
            lines.append({"item_id":row["item_id"],"confirmation_id":confirmation["confirmation_id"]})
        task=operator.request("GET",f'/api/v1/tasks/{uploaded["task_id"]}')
        before=time.monotonic()
        submission=operator.request("POST",f'/api/v1/tasks/{uploaded["task_id"]}/submissions',
            {"version_id":uploaded["version_id"],"revision":task["revision"],"items":lines})
        report["submission_response_seconds"]=round(time.monotonic()-before,3)
        expert.request("GET",f'/api/v1/tasks/{uploaded["task_id"]}')
        erun=wait_for(lambda:done(expert,submission["expert_run_id"]),30)
        assert erun["progress"]["succeeded"]==3
        report["expert_submission_id"]=submission["submission_id"]
        report["automatic_expert_handoff"]=True
        print("HTTP submission and background expert check passed",flush=True)
        stop(worker)
        failing=start("tests/p13/worker_probe.py","--fail-first-item")
        failed_upload,_=supplier.upload()
        failed=wait_for(lambda:done(supplier,failed_upload["precheck_run_id"]),30)
        assert failed["progress"]["failed"]==1 and failed["progress"]["succeeded"]==2,failed
        assert next(r for r in failed["items"] if r["status"]=="failed")["attempts"]==3
        operator.request("POST",f'/api/v1/tasks/{failed_upload["task_id"]}/claim')
        allowed=operator.request("GET",f'/api/v1/runs/{failed["run_id"]}')
        assert allowed["retry_allowed"]
        original_rows=list(Run.objects.get(pk=failed["run_id"]).items.values("id","status","result"))
        stop(failing)
        child=operator.request("POST",f'/api/v1/runs/{failed["run_id"]}/retries',{"item_ids":allowed["retry_item_ids"]})
        worker=start("tests/p13/worker_probe.py")
        retried=wait_for(lambda:done(operator,child["run_id"]),30)
        assert retried["progress"]["succeeded"]==3
        assert sum(bool(r["attempt_history"]) for r in retried["items"])==1
        assert original_rows==list(Run.objects.get(pk=failed["run_id"]).items.values("id","status","result"))
        report["failed_row_retry_preserved_successes"]=True
        report["retry_task_id"]=failed_upload["task_id"]
        print("Actual failed row auto-limit and manual retry passed",flush=True)
        task=operator.request("GET",f'/api/v1/tasks/{failed_upload["task_id"]}')
        key=str(uuid.uuid4());body={"revision":task["revision"]}
        rerun=operator.request("POST",f'/api/v1/versions/{failed_upload["version_id"]}/prechecks',body,key=key)
        replay=operator.request("POST",f'/api/v1/versions/{failed_upload["version_id"]}/prechecks',body,key=key)
        assert rerun["run_id"]==replay["run_id"]
        assert wait_for(lambda:done(operator,rerun["run_id"]))["progress"]["succeeded"]==3
        report["same_version_rerun_idempotent"]=True
        original=supplier.request("GET",uploaded["original_download_url"],raw=True)
        assert hashlib.sha256(original).hexdigest()==uploaded["sha256"]
        report["original_hash_unchanged"]=True
        for proc in processes:stop(proc)
        crash_file=out/f"commit-crash-{uuid.uuid4().hex}.json"
        crashed=subprocess.run([sys.executable,str(ROOT/"tests/p13/commit_crash.py"),str(crash_file)],cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,timeout=20)
        assert crashed.returncode==91,crashed.returncode
        persisted=json.loads(crash_file.read_text())
        assert RunWork.objects.filter(run_id=persisted["run_id"],state="pending",dispatched_at__isnull=True).exists()
        start("manage.py","dispatch_runs","--watch","--interval","0.5")
        worker=start("tests/p13/worker_probe.py")
        assert wait_for(lambda:done(supplier,persisted["run_id"]))["progress"]["succeeded"]==3
        report["request_process_crashed_after_commit_recovered"]=True
        print("Actual exit after DB commit and before broker dispatch recovered",flush=True)
        for proc in processes:stop(proc)
        from redis import Redis
        Redis(host="127.0.0.1",port=63813).shutdown(save=True)
        lost,_=supplier.upload()
        assert RunWork.objects.filter(run_id=lost["precheck_run_id"],state="pending",dispatched_at__isnull=True).exists()
        subprocess.run([sys.executable,str(ROOT/"scripts/queue/local_windows.py"),"start"],cwd=ROOT,check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,timeout=30)
        start("manage.py","dispatch_runs","--watch","--interval","0.5")
        worker=start("tests/p13/worker_probe.py")
        assert wait_for(lambda:done(supplier,lost["precheck_run_id"]))["progress"]["succeeded"]==3
        report["broker_outage_recovered"]=True
        print("Actual Redis outage retained request and resumed work",flush=True)
        if args.leave_failed:
            stop(worker)
            failing=start("tests/p13/worker_probe.py","--fail-first-item")
            demo,_=supplier.upload()
            failed_demo=wait_for(lambda:done(supplier,demo["precheck_run_id"]))
            assert failed_demo["progress"]["failed"]==1
            stop(failing)
            operator.request("POST",f'/api/v1/tasks/{demo["task_id"]}/claim')
            report["manual_retry_task_id"]=demo["task_id"]
        report["result"]="passed"
        (out/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        (out/"P13-合成测试表.xlsx").write_bytes(sample())
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    finally:
        for proc in reversed(processes):stop(proc)
        for log in logs:log.close()
