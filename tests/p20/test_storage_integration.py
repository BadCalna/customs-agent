"""Public routes + actual session/CSRF/upload/P05 disk/results/POST attachment. No storage stubs."""
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.parse import unquote
from django.test import Client, TransactionTestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from adapters.files.ooxml import Workbook
from apps.accounts.models import Role
from apps.reviews.models import Task, FileVersion, Submission, Decision, AuditEvent
from apps.reviews.services.files import read_version_original
from tests.p01.factories import make_user, PASSWORD
from tests.p20.integration_sample import upload_sample


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RealStorageExportTests(TransactionTestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)/'storage'
        config=override_settings(CUSTOMS_FILE_ROOT=self.root, CUSTOMS_DEFAULT_EXPERT_USERNAME='p20-real-expert')
        config.enable();self.addCleanup(config.disable)
        self.supplier=make_user('p20-real-supplier',Role.SUPPLIER)
        self.operator=make_user('p20-real-operator',Role.OPERATOR)
        self.expert=make_user('p20-real-expert',Role.EXPERT)
        self.source=upload_sample()
        self.s=self.login(self.supplier);self.o=self.login(self.operator);self.e=self.login(self.expert)
        self.upload=self.write(self.s,'/api/v1/tasks',file=self.source,key='integration-upload').json()
        self.task_id=self.upload['task_id'];self.version_id=self.upload['version_id'];self.run_id=self.upload['precheck_run_id']
        claimed=self.write(self.o,f'/api/v1/tasks/{self.task_id}/claim',key='integration-claim')
        self.assertEqual(claimed.status_code,200,claimed.content)

    def login(self,user):
        c=Client(enforce_csrf_checks=True);c.get('/api/v1/me')
        response=c.post('/api/v1/auth/login',data=json.dumps({'username':user.username,'password':PASSWORD}),content_type='application/json',HTTP_X_CSRFTOKEN=c.cookies['csrftoken'].value)
        self.assertEqual(response.status_code,200,response.content);return c

    def write(self,c,path,payload=None,*,file=None,key=None):
        options={'HTTP_X_CSRFTOKEN':c.cookies['csrftoken'].value}
        if key:options['HTTP_IDEMPOTENCY_KEY']=key
        if file is not None:return c.post(path,{'file':SimpleUploadedFile('合成验收.xlsx',file)},**options)
        return c.post(path,data=json.dumps(payload or {}),content_type='application/json',**options)

    def export(self,c,kind='precheck',**extra):
        return self.write(c,f'/api/v1/versions/{self.version_id}/exports',{'kind':kind,'run_id':self.run_id,**extra})

    def snapshot(self):
        t=Task.objects.get(pk=self.task_id)
        return t.stage,t.revision,Submission.objects.count(),Decision.objects.count(),AuditEvent.objects.count()

    def test_disk_upload_persisted_progress_and_both_downloads_preserve_source(self):
        version=FileVersion.objects.get(pk=self.version_id)
        original=read_version_original(self.operator,self.version_id)
        self.assertTrue((self.root/version.storage_path).is_file())
        self.assertEqual(original.content,self.source)
        tasks=self.o.get('/api/v1/tasks').json()['items'];self.assertIn(self.task_id,[t['task_id'] for t in tasks])
        run=self.o.get(f'/api/v1/runs/{self.run_id}').json()
        self.assertEqual(run['progress'],{'total':1,'queued':0,'running':0,'succeeded':1,'failed':0})
        before=self.snapshot()
        for kind in ('precheck','issues'):
            response=self.export(self.o,kind)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response['X-Export-Run'],self.run_id)
            self.assertEqual(response['X-Result-Mode'],'mock')
            self.assertIn('测试结果',unquote(response['Content-Disposition']))
            content=b''.join(response.streaming_content);book=Workbook(content)
            if kind=='precheck':
                sheet=book.find_sheet('申报要素')
                self.assertEqual(sheet.cell('G2').value,'原意见保留')
                self.assertEqual(sheet.cell('J2').formula,'1+2')
                self.assertIn('测试结果',sheet.cell('I2').value)
                self.assertEqual(book.media_bytes('xl/media/integration.png'),Workbook(self.source).media_bytes('xl/media/integration.png'))
            self.assertEqual(self.snapshot(),before)
        self.assertEqual(hashlib.sha256(read_version_original(self.operator,self.version_id).content).hexdigest(),hashlib.sha256(self.source).hexdigest())

    def test_public_router_enforces_session_csrf_and_scope_without_fake_attachments(self):
        response=self.export(self.s)
        self.assertEqual(response.status_code,403);self.assertIn('error',response.json())
        anonymous=Client();response=anonymous.post(f'/api/v1/versions/{self.version_id}/exports',data=json.dumps({'kind':'issues','run_id':self.run_id}),content_type='application/json')
        self.assertEqual(response.status_code,401)
        response=self.o.post(f'/api/v1/versions/{self.version_id}/exports',data='{}',content_type='application/json')
        self.assertEqual(response.status_code,403);self.assertEqual(response.json()['error']['code'],'csrf_failed')
        other=self.login(make_user('p20-invisible',Role.OPERATOR));response=self.export(other)
        self.assertEqual(response.status_code,404);self.assertNotIn('Content-Disposition',response)

    def test_versions_cannot_mix_runs_and_history_export_does_not_borrow_latest(self):
        # Version creation requires multipart + optimistic current-version reference.
        second=self.s.post(f'/api/v1/tasks/{self.task_id}/versions',{'file':SimpleUploadedFile('V2.xlsx',upload_sample('V2')),'expected_current_version_id':self.version_id},HTTP_X_CSRFTOKEN=self.s.cookies['csrftoken'].value,HTTP_IDEMPOTENCY_KEY='v2-real')
        self.assertEqual(second.status_code,200,second.content)
        v2=second.json()
        response=self.write(self.o,f"/api/v1/versions/{v2['version_id']}/exports",{'kind':'issues','run_id':self.run_id})
        self.assertEqual(response.status_code,422);self.assertNotIn('Content-Disposition',response)
        response=self.export(self.o);self.assertEqual(response.status_code,200)
        book=Workbook(b''.join(response.streaming_content));self.assertEqual(book.find_sheet('申报要素').cell('C2').value,'合成商品-V1')

    def test_expert_issues_binds_submission_and_workbook_keeps_layout_guard(self):
        card=self.o.get(f'/api/v1/versions/{self.version_id}/items').json()['items'][0]
        response=self.o.patch(f"/api/v1/versions/{self.version_id}/items/{card['item_id']}/confirmation",data=json.dumps({'confirmed_hs':'0000000001','expected_revision':card['revision']}),content_type='application/json',HTTP_X_CSRFTOKEN=self.o.cookies['csrftoken'].value)
        self.assertEqual(response.status_code,200,response.content);confirmation=response.json()
        task=self.o.get(f'/api/v1/tasks/{self.task_id}').json()
        response=self.write(self.o,f'/api/v1/tasks/{self.task_id}/submissions',{'version_id':self.version_id,'revision':task['revision'],'items':[{'item_id':card['item_id'],'confirmation_id':confirmation['confirmation_id']}]},key='submit')
        self.assertEqual(response.status_code,200,response.content);submission=response.json()
        self.run_id=submission['expert_run_id']
        response=self.export(self.e,'issues',submission_id=submission['submission_id'])
        self.assertEqual(response.status_code,200);b''.join(response.streaming_content)
        response=self.export(self.e,'precheck',submission_id=submission['submission_id'])
        self.assertEqual(response.status_code,422);self.assertEqual(response.json()['error']['code'],'expert_layout_pending')
        response=self.export(self.e,'issues',submission_id='wrong')
        self.assertEqual(response.status_code,422)
