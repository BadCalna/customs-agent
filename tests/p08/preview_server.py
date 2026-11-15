"""Local-only P08 preview; no changes to the production URL configuration.

Run from the repository root: python -m tests.p08.preview_server --port 8788
"""
import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server

from django.core.wsgi import get_wsgi_application


def preview_application():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django = get_wsgi_application()
    from django.conf import settings

    files = {"/p08/": ("preview.html", "text/html"), "/p08/preview.js": ("preview.js", "text/javascript")}

    def application(environ, start_response):
        entry = files.get(environ.get("PATH_INFO"))
        if entry and settings.DEBUG and environ.get("REQUEST_METHOD") in ("GET", "HEAD"):
            name, content_type = entry
            data = (Path(__file__).parent / name).read_bytes()
            start_response("200 OK", [("Content-Type", content_type + "; charset=utf-8"),
                                     ("Cache-Control", "no-store"), ("Content-Length", str(len(data)))])
            return [] if environ["REQUEST_METHOD"] == "HEAD" else [data]
        return django(environ, start_response)
    return application


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P08 loopback development preview")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    with make_server("127.0.0.1", args.port, preview_application()) as server:
        print(f"P08 preview: http://127.0.0.1:{args.port}/p08/", flush=True)
        server.serve_forever()
