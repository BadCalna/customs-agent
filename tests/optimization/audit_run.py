"""Run the real Django service from an isolated baseline/candidate snapshot, never the working DB."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REV = 'f66c8fa851fedece88d0e154528b8084c13f089b'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', choices=['baseline', 'candidate'])
    p.add_argument('output', type=Path, help='NEW directory for images and measurements')
    p.add_argument('--django-python', type=Path, required=True)
    p.add_argument('--helper', type=Path, required=True, help='webapp-testing scripts/with_server.py')
    p.add_argument('--login-only', action='store_true', help='Inspect the independent login route only (integration preflight)')
    p.add_argument('--channel', default='chrome', help='Installed Chrome channel; chromium uses bundled Playwright browser')
    a = p.parse_args()
    output = a.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    python = str(a.django_python.absolute())  # Keep venv symlink rather than following to system Python.
    with tempfile.TemporaryDirectory(prefix='customs-audit-') as temp:
        tmp = Path(temp); src = tmp / 'snapshot'; src.mkdir()
        tar = tmp / 'source.tar'
        subprocess.run(['git', 'archive', '-o', str(tar), REV], cwd=ROOT, check=True)
        with tarfile.open(tar) as archive:
            archive.extractall(src, filter='data')
        if a.source == 'candidate':
            # Overlay exactly current runtime code, not a workstation config, uploads, or private data.
            for directory in ('web', 'apps', 'config', 'adapters'):
                shutil.copytree(ROOT / directory, src / directory, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__', '.DS_Store', '*.pyc'))
        h = hashlib.sha256()
        for folder in ['web', 'apps', 'config', 'adapters']:
            for f in sorted(x for x in (src / folder).rglob('*') if x.is_file()):
                h.update(str(f.relative_to(src)).encode()+b'\x00'+hashlib.sha256(f.read_bytes()).digest())
        (output / 'source.json').write_text(json.dumps({'source': a.source, 'archive': REV, 'runtime_tree_sha256': h.hexdigest(), 'mode': 'Django live + seed_contracts / mock inline / SQLite'}, indent=2)+'\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CUSTOMS_', 'DJANGO_', 'PYTHONPATH'))}
        env.update(PYTHONDONTWRITEBYTECODE='1', CUSTOMS_SQLITE_PATH=str(tmp/'db.sqlite'),
                   CUSTOMS_FILE_ROOT=str(tmp/'files'), CUSTOMS_PAGE_MODE='live', CUSTOMS_ENGINE_MODE='mock',
                   CUSTOMS_EXECUTOR_MODE='inline', CUSTOMS_DB_BACKEND='sqlite', DJANGO_SECRET_KEY=secrets.token_urlsafe(40))
        password = secrets.token_urlsafe(22)
        env['AUDIT_DEMO_PASSWORD'] = password
        env['AUDIT_LOGIN_ONLY'] = '1' if a.login_only else '0'
        for args in [['migrate','--verbosity','0'],['seed_demo'],['seed_contracts'],['shell','-c',
                     'from apps.accounts.models import User; import os; u=User.objects.get(username="operator_a"); u.set_password(os.environ["AUDIT_DEMO_PASSWORD"]); u.save()']]:
            subprocess.run([python, 'manage.py', *args], cwd=src, env=env, check=True, stdout=subprocess.DEVNULL)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        import shlex
        server = shlex.join([python, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'])
        subprocess.run([sys.executable, str(a.helper.resolve()), '--server', server, '--port', str(port), '--', sys.executable,
                        str(Path(__file__).with_name('audit_browser.py')), f'http://127.0.0.1:{port}', str(output), a.channel],
                       cwd=src, env=env, check=True)


if __name__ == '__main__':
    main()
