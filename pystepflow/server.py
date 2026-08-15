"""
Web server for pystepflow, the Python code visualizer.

Serves the UI and exposes a single API endpoint that traces a snippet of code.

    python -m pystepflow.server                  # start on http://127.0.0.1:8000
    python -m pystepflow.server my_script.py     # start with that file preloaded
    python -m pystepflow.server -p 9000 -w .     # custom port, allow opening files under .

Tracing happens in a *subprocess* with a wall-clock timeout, so an infinite
loop or a segfault in the traced code cannot take the server down.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from . import tracer
except ImportError:  # running the file directly rather than as a package
    import tracer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULT_TIMEOUT = 15          # seconds of wall clock per trace
MAX_CODE_BYTES = 200_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

# Set from the command line; the UI loads this file on first paint.
PRELOAD_FILE = None
WORKSPACE = None
REDACT = True                 # mask values that look like API keys

# Project mode: trace every module under the workspace, not just one file.
PROJECT = False
INCLUDE = []
EXCLUDE = []
TRACE_IMPORTS = False


def run_trace(code, stdin="", timeout=None, project=False):
    """Trace in a child process and return the result dictionary.

    With ``project`` set, the entry script configured on the command line is
    run from disk and every module in the project is traced; otherwise the
    ``code`` string is traced on its own.
    """
    timeout = timeout or DEFAULT_TIMEOUT
    request = {"code": code, "stdin": stdin, "redact": REDACT}

    if project and PRELOAD_FILE:
        request.update({
            "mode": "project",
            "entry": PRELOAD_FILE,
            "project_root": WORKSPACE,
            "include": INCLUDE,
            "exclude": EXCLUDE,
            "trace_imports": TRACE_IMPORTS,
        })
    payload = json.dumps(request)
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(BASE_DIR, "tracer.py")],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=WORKSPACE or BASE_DIR,
        )
    except subprocess.TimeoutExpired:
        return {
            "steps": [], "stdout": "", "step_count": 0,
            "error": {
                "type": "Timeout",
                "message": "Execution exceeded %gs and was stopped. Check for an "
                           "infinite loop, or raise the limit with "
                           "--timeout if you are waiting on a slow call." % timeout,
            },
        }

    raw = proc.stdout or ""
    marker = tracer.RESULT_MARKER
    if marker in raw:
        raw = raw.rsplit(marker, 1)[1]
    try:
        return json.loads(raw)
    except Exception:
        detail = (proc.stderr or raw or "").strip()[-2000:]
        return {
            "steps": [], "stdout": "", "step_count": 0,
            "error": {
                "type": "TracerCrashed",
                "message": "The tracer process did not return a result.",
                "traceback": detail,
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "pystepflow/1.0"

    # ---- plumbing ----------------------------------------------------

    def log_message(self, fmt, *args):
        if os.environ.get("PYVIS_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # browser navigated away mid-response

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj, default=str))

    # ---- routes ------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path)
        path = route.path

        if path == "/":
            return self._serve_static("index.html")
        if path == "/api/preload":
            return self._serve_preload()
        if path == "/api/open":
            return self._serve_open(parse_qs(route.query))
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/trace":
            return self._send(404, "Not found", "text/plain; charset=utf-8")

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_CODE_BYTES:
            return self._send_json(413, {"error": {
                "type": "TooLarge", "message": "Source is larger than 200 KB."}})

        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._send_json(400, {"error": {
                "type": "BadRequest", "message": str(exc)}})

        result = run_trace(
            request.get("code", ""),
            stdin=request.get("stdin", ""),
            timeout=float(request.get("timeout") or DEFAULT_TIMEOUT),
            project=bool(request.get("project")),
        )
        self._send_json(200, result)

    # ---- helpers -----------------------------------------------------

    def _serve_static(self, relative):
        # Refuse anything that tries to escape the static directory.
        target = os.path.normpath(os.path.join(STATIC_DIR, relative))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            self._send(200, handle.read(),
                       CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _serve_preload(self):
        # `project_available` tells the UI whether to offer the project toggle:
        # it needs a real file on disk to use as an entry point.
        info = {
            "code": None,
            "project_available": bool(PRELOAD_FILE),
            "project_default": PROJECT,
            "workspace": WORKSPACE,
        }
        if not PRELOAD_FILE:
            return self._send_json(200, info)
        try:
            with open(PRELOAD_FILE, "r", encoding="utf-8-sig") as handle:
                info.update({
                    "code": handle.read(),
                    "path": os.path.abspath(PRELOAD_FILE),
                    "name": os.path.basename(PRELOAD_FILE),
                })
        except OSError as exc:
            info["message"] = str(exc)
        return self._send_json(200, info)

    def _serve_open(self, query):
        """Load any .py file from the allowed workspace directory."""
        requested = (query.get("path") or [""])[0]
        if not requested:
            return self._send_json(400, {"message": "No path given."})
        if not WORKSPACE:
            return self._send_json(403, {
                "message": "Opening files is disabled. Start the server with "
                           "-w <directory> to allow it."})

        target = os.path.normpath(os.path.join(WORKSPACE, requested))
        if not target.startswith(WORKSPACE):
            return self._send_json(403, {"message": "Path is outside the workspace."})
        if not target.endswith(".py") or not os.path.isfile(target):
            return self._send_json(404, {"message": "No such .py file: %s" % requested})
        try:
            with open(target, "r", encoding="utf-8-sig") as handle:
                return self._send_json(200, {
                    "code": handle.read(),
                    "path": target,
                    "name": os.path.basename(target),
                })
        except OSError as exc:
            return self._send_json(500, {"message": str(exc)})


def main(argv=None):
    global PRELOAD_FILE, WORKSPACE, DEFAULT_TIMEOUT, REDACT
    global PROJECT, INCLUDE, EXCLUDE, TRACE_IMPORTS

    parser = argparse.ArgumentParser(description="Python code visualizer server.")
    parser.add_argument("file", nargs="?", help="a .py file to preload in the editor")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("-w", "--workspace",
                        help="directory the UI may open .py files from "
                             "(defaults to the preloaded file's directory)")
    parser.add_argument("-n", "--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("-t", "--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds a trace may run before it is stopped "
                             "(default %(default)s; raise it for slow API calls)")
    parser.add_argument("--show-secrets", action="store_true",
                        help="do not mask values that look like API keys")
    parser.add_argument("-P", "--project", action="store_true",
                        help="trace every module in the project, not just the "
                             "one file (requires a file argument as the entry "
                             "point)")
    parser.add_argument("--include", action="append", metavar="GLOB",
                        help="only trace files matching this glob, relative to "
                             "the project root; repeatable. Recommended on a "
                             "large codebase")
    parser.add_argument("--exclude", action="append", metavar="GLOB",
                        help="never trace files matching this glob; repeatable. "
                             "Dependencies are excluded already")
    parser.add_argument("--trace-imports", action="store_true",
                        help="also step through module bodies as they are "
                             "imported (very verbose)")
    args = parser.parse_args(argv)

    DEFAULT_TIMEOUT = args.timeout
    REDACT = not args.show_secrets
    PROJECT = args.project
    INCLUDE = args.include or []
    EXCLUDE = args.exclude or []
    TRACE_IMPORTS = args.trace_imports

    if args.project and not args.file:
        parser.error("--project needs a file argument to use as the entry point")

    if args.file:
        if not os.path.isfile(args.file):
            parser.error("no such file: %s" % args.file)
        PRELOAD_FILE = os.path.abspath(args.file)

    if args.workspace:
        WORKSPACE = os.path.abspath(args.workspace)
    elif PRELOAD_FILE:
        WORKSPACE = os.path.dirname(PRELOAD_FILE)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)

    print("Python Code Visualizer running at %s" % url)
    if PRELOAD_FILE:
        print("  preloaded : %s" % PRELOAD_FILE)
    if WORKSPACE:
        print("  workspace : %s" % WORKSPACE)
    if PROJECT:
        print("  mode      : project (tracing every module under the workspace)")
        if INCLUDE:
            print("  include   : %s" % ", ".join(INCLUDE))
        if EXCLUDE:
            print("  exclude   : %s" % ", ".join(EXCLUDE))
    print("  timeout   : %gs per trace" % DEFAULT_TIMEOUT)
    if not REDACT:
        print("  secrets   : NOT masked (--show-secrets)")
    print("  Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
