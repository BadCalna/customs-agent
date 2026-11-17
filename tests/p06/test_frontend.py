"""Node 执行真实页面 JS 的请求竞态测试；不冒充浏览器通过。"""
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless
from django.test import SimpleTestCase


class FrontendIntentTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Node 不可用，页面请求时序专项待运行")
    def test_upload_confirmation_keeps_original_intent(self):
        script=Path(__file__).with_suffix('.cjs')
        result=subprocess.run([shutil.which("node"),str(script)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('PASS:',result.stdout)
