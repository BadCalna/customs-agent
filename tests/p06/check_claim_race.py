"""独立文件 SQLite + 两连接 + 真实 HTTP 视图，复现认领竞争窗口。"""
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='p06-claim-http-') as temp:
        os.environ.update(DJANGO_SETTINGS_MODULE='config.settings',CUSTOMS_DB_BACKEND='sqlite',
                          CUSTOMS_SQLITE_PATH=str(Path(temp)/'db.sqlite3'),CUSTOMS_FILE_ROOT=str(Path(temp)/'files'))
        import django
        django.setup()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Lock
        from unittest.mock import patch
        from django.core.management import call_command
        from django.db import close_old_connections, connections
        from django.test import Client
        from apps.accounts.models import Role, TaskAssignment
        from apps.reviews.models import TaskClaim, AuditEvent
        from apps.reviews.services.imports import upload
        from tests.p01.factories import make_user
        from tests.p06.samples import sample
        call_command('migrate',verbosity=0)
        supplier=make_user('race-s',Role.SUPPLIER)
        a=make_user('race-a',Role.OPERATOR)
        b=make_user('race-b',Role.OPERATOR)

        def new_task(key):
            return upload(supplier,filename='sample.xlsx',data=sample(),key=key)['task_id']

        def client(user):
            c=Client(enforce_csrf_checks=True, SERVER_NAME="localhost")
            c.force_login(user)
            assert c.get('/api/v1/me').status_code==200
            return c

        def race(entries):
            barrier=Barrier(2);lock=Lock();calls=0
            create=TaskClaim.objects.create
            def synchronized(*args,**kwargs):
                nonlocal calls
                with lock:
                    calls+=1
                    first=calls<=2
                if first:barrier.wait(timeout=10)
                return create(*args,**kwargs)
            def request(entry):
                c,task_id,key=entry
                close_old_connections()
                try:
                    r=c.post(f'/api/v1/tasks/{task_id}/claim',{},HTTP_IDEMPOTENCY_KEY=key,
                             HTTP_X_CSRFTOKEN=c.cookies['csrftoken'].value)
                    return r.status_code,r.json()
                finally:connections.close_all()
            with patch.object(TaskClaim.objects,'create',synchronized):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    return list(pool.map(request,entries))

        task_id=new_task('different-operators')
        results=race([(client(a),task_id,'a'),(client(b),task_id,'b')])
        assert sorted(status for status,_ in results)==[200,409],results
        assert next(body['error']['code'] for status,body in results if status==409)=='task_already_claimed',results
        assert TaskClaim.objects.filter(task_id=task_id).count()==1
        assert TaskAssignment.objects.filter(task_id=task_id,role=Role.OPERATOR).count()==1
        print('PASS: two operators HTTP 200/409; exactly one claim and assignment',flush=True)

        task_id=new_task('same-operator')
        results=race([(client(a),task_id,'same-key'),(client(a),task_id,'same-key')])
        assert all(status==200 for status,_ in results),results
        assert sum(body['idempotent_replay'] for _,body in results)==1,results
        assert AuditEvent.objects.filter(task_id=task_id,action='task.claim').count()==1
        print('PASS: same operator/key HTTP 200/200; one audit and idempotent replay',flush=True)

        first,second=new_task('cross-key-1'),new_task('cross-key-2')
        results=race([(client(a),first,'cross-task-key'),(client(a),second,'cross-task-key')])
        assert sorted(status for status,_ in results)==[200,409],results
        assert next(body['error']['code'] for status,body in results if status==409)=='idempotency_conflict',results
        assert TaskClaim.objects.filter(task_id__in=[first,second]).count()==1
        print('PASS: same key/different tasks HTTP 200/409 idempotency_conflict; one claim',flush=True)
        connections.close_all()
