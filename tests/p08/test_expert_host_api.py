"""Real three-item submission -> actual host/P11 events -> real approval route and persisted recode."""
import json
import shutil
import subprocess
from django.conf import settings
from django.test import Client, TestCase, override_settings
from apps.accounts.models import Role
from apps.reviews.models import DecisionItem, Submission
from tests.p01.factories import PASSWORD, assign, make_item, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ExpertHostDecisionTests(TestCase):
    def login(self, user):
        client=Client(enforce_csrf_checks=True);client.get('/api/v1/me')
        response=self.write(client,'/api/v1/auth/login',{'username':user.username,'password':PASSWORD})
        self.assertEqual(response.status_code,200,response.content)
        return client

    def write(self, client, path, body, method='post', key=None):
        headers={'HTTP_X_CSRFTOKEN':client.cookies['csrftoken'].value}
        if key:headers['HTTP_IDEMPOTENCY_KEY']=key
        return getattr(client,method)(path,data=json.dumps(body),content_type='application/json',**headers)

    def test_card_browsing_keeps_all_labels_and_persists_typed_recode(self):
        supplier=make_user('p31-supplier',Role.SUPPLIER)
        operator=make_user('p31-operator',Role.OPERATOR)
        expert=make_user('p31-expert',Role.EXPERT)
        task=make_task(supplier,stage='awaiting_confirmation')
        assign(task,operator,Role.OPERATOR);assign(task,expert,Role.EXPERT)
        version=make_version(task,supplier)
        items=[make_item(version,row=row,name=f'合成整批商品{i}',supplier_hs='8544421900') for i,row in enumerate([8,15,22],1)]
        op=self.login(operator);ex=self.login(expert)
        confirmed=[]
        for item in items:
            response=self.write(op,f'/api/v1/versions/{version.pk}/items/{item.pk}/confirmation',
                                {'confirmed_hs':'8544421900','expected_revision':item.revision},method='patch')
            self.assertEqual(response.status_code,200,response.content)
            confirmed.append({'item_id':str(item.pk),'confirmation_id':response.json()['confirmation_id']})
        task.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            response=self.write(op,f'/api/v1/tasks/{task.pk}/submissions',
                                {'version_id':str(version.pk),'revision':task.revision,'items':confirmed},key='p31-batch')
        self.assertEqual(response.status_code,200,response.content)
        submitted=response.json();run=ex.get(f"/api/v1/runs/{submitted['expert_run_id']}").json()
        state={'taskId':str(task.pk),'versionId':str(version.pk),'runId':run['run_id'],'run':run,
               'task':ex.get(f'/api/v1/tasks/{task.pk}').json(),
               'history':ex.get(f'/api/v1/tasks/{task.pk}/history').json(),
               'items':ex.get(f'/api/v1/versions/{version.pk}/items').json()['items'],
               'itemId':None,'card':None,'loading':False}
        snapshot=state['history']['versions'][0]['submissions'][0]
        self.assertEqual(len(snapshot['items']),3)
        self.assertNotIn('name',snapshot['items'][0],'exercise the published snapshot shape without prefilled labels')
        cards=[ex.get(f'/api/v1/versions/{version.pk}/items/{item.pk}').json() for item in items]
        node=shutil.which('node');self.assertIsNotNone(node,'P08 live-host regression requires Node')
        result=subprocess.run([node,'tests/p08/drive_expert_review.mjs'],cwd=settings.BASE_DIR,text=True,
                              input=json.dumps({'state':state,'cards':cards,'targetItemId':str(items[1].pk),
                                                'finalHs':'8536901900','reason':'合成核查改码原因'}),
                              capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        request=json.loads(result.stdout)
        self.assertEqual(request['submission_id'],submitted['submission_id'])
        response=self.write(ex,f"/api/v1/submissions/{request['submission_id']}/decisions",request['body'],key=request['key'])
        self.assertEqual(response.status_code,200,response.content)
        saved=DecisionItem.objects.get(decision__submission_id=submitted['submission_id'],item=items[1])
        self.assertEqual(saved.previous_hs,'8544421900')
        self.assertEqual(saved.final_hs,'8536901900')
        self.assertEqual(saved.change_reason,'合成核查改码原因')
        self.assertEqual(DecisionItem.objects.filter(decision=saved.decision).count(),3)
        self.assertEqual(Submission.objects.get(pk=submitted['submission_id']).status,'approved')
