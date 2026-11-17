from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch
from django.db import OperationalError
from django.test import SimpleTestCase
from apps.accounts.models import Role, User
from apps.accounts.errors import ApiError
from apps.reviews.services.claims import claim_task


class SQLiteClaimRaceTests(SimpleTestCase):
    def test_real_http_competition_and_replays_on_independent_file_database(self):
        script=Path(__file__).with_name('check_claim_race.py')
        result=subprocess.run([sys.executable,str(script)],cwd=script.parents[2],capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(result.stdout.count('PASS:'),3,result.stdout)

    def test_lock_exhaustion_is_explicit_retryable_conflict(self):
        cause=sqlite3.OperationalError('database is locked')
        cause.sqlite_errorcode=sqlite3.SQLITE_BUSY
        error=OperationalError('database is locked');error.__cause__=cause
        user=User(username='race',role=Role.OPERATOR)
        with patch('apps.reviews.services.claims._claim_once',side_effect=error) as once, patch('apps.reviews.services.claims.connection') as conn, patch('apps.reviews.services.claims.time.sleep'):
            conn.vendor='sqlite'
            with self.assertRaises(ApiError) as caught:claim_task(user,'task-id','key')
        self.assertEqual(once.call_count,4)
        self.assertEqual((caught.exception.status,caught.exception.code),(409,'claim_busy'))
        self.assertTrue(caught.exception.details['retryable'])

    def test_unrelated_database_failure_is_not_masked_as_conflict(self):
        user=User(username='race',role=Role.OPERATOR)
        with patch('apps.reviews.services.claims._claim_once',side_effect=OperationalError('no such table')):
            with self.assertRaises(OperationalError):claim_task(user,'task-id','key')
