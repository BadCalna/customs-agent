"""Hold a real PostgreSQL write freeze and the host-local file writer lock.

Protocol: print one JSON ready line; hold until RELEASE or parent EOF. Failure to
acquire *any* lock aborts. All locks are released by rollback/process termination.
Requires the native deployment's Python/Django/psycopg environment and table owner.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


@contextmanager
def frozen_database(connection, root):
    from adapters.files.backup_guard import file_guard
    from psycopg import sql
    with file_guard(root, exclusive=True), connection.transaction(force_rollback=True):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='1s'")
            cursor.execute("SET LOCAL idle_in_transaction_session_timeout='10min'")
            cursor.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                           "WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname")
            tables = [row[0] for row in cursor.fetchall()]
            if not tables:
                raise ValueError("备份源没有 public 业务表，拒绝空库证据")
            # SHARE conflicts with INSERT/UPDATE/DELETE/TRUNCATE/ALTER; NOWAIT
            # fails rather than deadlocking with a request waiting on the file lock.
            cursor.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE NOWAIT").format(
                sql.SQL(',').join(sql.Identifier('public', table) for table in tables)))
            cursor.execute("SELECT sequencename, increment_by FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
            sequences = cursor.fetchall()
            for name, increment in sequences:
                # ALTER acquires the sequence lock needed to block nextval/setval;
                # setting the existing value changes no sequence definition/state.
                cursor.execute(sql.SQL("ALTER SEQUENCE {} INCREMENT BY {}").format(
                    sql.Identifier('public', name), sql.Literal(increment)))
            cursor.execute("SELECT pg_export_snapshot(), current_database()")
            snapshot, database = cursor.fetchone()
            yield {"method": "postgres-table-sequence-locks+local-file-lock", "database": database,
                   "file_root": str(Path(root).resolve()), "snapshot": snapshot,
                   "locked_tables": len(tables), "locked_sequences": len(sequences)}
        # force_rollback releases locks without persisting even the no-op ALTERs.


def main():
    import django
    django.setup()
    from django.conf import settings
    import psycopg
    database = settings.DATABASES['default']
    if database['ENGINE'] != 'django.db.backends.postgresql':
        raise ValueError("native 一致备份必须使用 PostgreSQL")
    with psycopg.connect(dbname=database['NAME'], user=database['USER'], password=database['PASSWORD'],
                         host=database['HOST'], port=database['PORT'], connect_timeout=5) as connection:
        with frozen_database(connection, settings.CUSTOMS_FILE_ROOT) as evidence:
            print(json.dumps(evidence), flush=True)
            if sys.stdin.readline().strip() != "RELEASE":
                raise ValueError("备份协调进程中断，回滚冻结事务")


if __name__ == '__main__':
    main()
