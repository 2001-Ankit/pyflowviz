"""
Execution tracer for the Python Code Visualizer.

Runs a snippet of Python under ``sys.settrace`` and records a full snapshot of
the program state at every step: the line about to execute, the call stack,
every local variable, and the heap objects those variables point at.

The output is a JSON document consumed by the web UI.

Can be used as a library::

    from tracer import trace_code
    result = trace_code(source, stdin="")

or as a subprocess (this is what ``server.py`` does, so runaway code cannot
take the server down)::

    echo '{"code": "...", "stdin": ""}' | python tracer.py
"""

import io
import json
import sys
import traceback

FILENAME = "<user_code>"
RESULT_MARKER = "\n@@TRACE_RESULT@@\n"

# Safety limits. Tuned so a teaching-sized program traces instantly while a
# runaway loop stops rather than eating all memory.
MAX_STEPS = 5000        # snapshots recorded before we stop
MAX_OUTPUT = 200_000    # characters of stdout kept
MAX_STRING = 300        # characters kept per repr'd string
MAX_ITEMS = 200         # elements shown per container
MAX_DEPTH = 8           # nesting depth before we fall back to repr

_PRIMITIVES = (type(None), bool, int, float, complex, str, bytes)

# ----------------------------------------------------------------------
# Secret redaction
#
# The snapshot contains every variable, and the whole trace travels over HTTP
# to the browser, where it may well end up in a screenshot or a shared link.
# Code that talks to an API therefore leaks its key by default, so mask the
# obvious cases. Turn it off with --show-secrets when you need the real value.
# ----------------------------------------------------------------------

# Names that mean "credential" whatever the value looks like.
_SECRET_NAMES = (
    "api_key", "apikey", "api_secret", "secret", "password", "passwd", "pwd",
    "credential", "private_key", "access_key", "secret_key", "auth_token",
    "access_token", "refresh_token", "bearer_token", "session_key",
)

# Names that are only sometimes secrets. In this domain `token` and `key` are
# usually innocent (text tokens, dict keys), so those need a secret-looking
# value before anything is hidden.
_WEAK_SECRET_NAMES = ("token", "key", "auth", "authorization")

# Value shapes that are a credential no matter what the variable is called.
_SECRET_PREFIXES = (
    "sk-", "sk_live_", "sk_test_", "pk_live_", "rk_live_", "ghp_", "gho_",
    "github_pat_", "AKIA", "ASIA", "xoxb-", "xoxp-", "AIza", "hf_", "Bearer ",
)

REDACTED = "<redacted>"


def _looks_like_secret_value(text):
    if not isinstance(text, str):
        return False
    if text.startswith(_SECRET_PREFIXES):
        return True
    # A long unbroken run of key-ish characters is very likely a credential.
    return len(text) >= 24 and " " not in text and any(c.isdigit() for c in text)


def is_secret(name, value):
    """Should this name/value pair be hidden from the snapshot?"""
    if isinstance(value, str) and value.startswith(_SECRET_PREFIXES):
        return True
    if not isinstance(name, str) or not isinstance(value, (str, bytes)):
        return False
    lowered = name.lower()
    if any(part in lowered for part in _SECRET_NAMES):
        return True
    if any(part == lowered or lowered.endswith("_" + part) for part in _WEAK_SECRET_NAMES):
        return _looks_like_secret_value(value)
    return False


class _StepLimit(Exception):
    """Raised inside the traced program once MAX_STEPS snapshots are taken."""


class Tracer:
    def __init__(self, code, stdin="", max_steps=MAX_STEPS, redact=True):
        # Editors on Windows often save a UTF-8 BOM, which compile() rejects.
        self.code = code.lstrip("﻿")
        self.stdin = stdin
        self.max_steps = max_steps
        self.redact = redact

        self.steps = []
        self.error = None

        self._out = io.StringIO()
        self._truncated_output = False

        # id(obj) -> small stable integer used by the UI to identify a heap
        # object across steps. Objects are kept alive so CPython cannot recycle
        # an id and make two different objects look like the same one.
        self._obj_ids = {}
        self._keepalive = []
        self._next_obj_id = 1

        # id(frame) -> small stable integer, so the UI can follow one
        # invocation of a function (recursion creates several).
        self._frame_ids = {}
        self._next_frame_id = 1

        # Per-step scratch space, reset for every snapshot.
        self._heap = {}
        self._seen = set()

    # ------------------------------------------------------------------
    # Value encoding
    # ------------------------------------------------------------------
    # Values are encoded Python-Tutor style: primitives are inlined, everything
    # else lives in a per-step heap and is referenced by id. That is what makes
    # aliasing ("both names point at the SAME list") visible in the UI.

    def _prim(self, value, display=None):
        return {"t": "prim", "v": value, "d": display if display is not None else repr(value)}

    def _obj_id(self, obj):
        key = id(obj)
        if key not in self._obj_ids:
            self._obj_ids[key] = self._next_obj_id
            self._next_obj_id += 1
            self._keepalive.append(obj)
        return self._obj_ids[key]

    def _safe_repr(self, obj, limit=MAX_STRING):
        try:
            text = repr(obj)
        except Exception as exc:  # a broken __repr__ must not kill the trace
            text = "<unreprable %s: %s>" % (type(obj).__name__, exc)
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    def encode_named(self, name, obj, depth=0):
        """Encode a value that has a name attached, hiding it if it is a secret."""
        if self.redact and is_secret(name, obj):
            return self._prim(REDACTED, REDACTED)
        return self.encode(obj, depth)

    def encode(self, obj, depth=0):
        """Encode one value as either an inline primitive or a heap reference."""
        if self.redact and isinstance(obj, str) and obj.startswith(_SECRET_PREFIXES):
            return self._prim(REDACTED, REDACTED)
        if isinstance(obj, _PRIMITIVES):
            if isinstance(obj, str):
                shown = obj if len(obj) <= MAX_STRING else obj[:MAX_STRING] + "…"
                return self._prim(shown, repr(shown))
            if isinstance(obj, float):
                # NaN / Infinity are not valid JSON, so send them as text.
                if obj != obj or obj in (float("inf"), float("-inf")):
                    return self._prim(repr(obj), repr(obj))
                return self._prim(obj)
            if isinstance(obj, (complex, bytes)):
                return self._prim(self._safe_repr(obj), self._safe_repr(obj))
            return self._prim(obj)

        if depth > MAX_DEPTH:
            return self._prim(self._safe_repr(obj), self._safe_repr(obj))

        oid = self._obj_id(obj)
        if oid not in self._seen:
            self._seen.add(oid)
            # Insert a placeholder first so a self-referential structure
            # (x = []; x.append(x)) terminates instead of recursing forever.
            self._heap[oid] = {"kind": "pending"}
            self._heap[oid] = self._encode_object(obj, depth)
        return {"t": "ref", "id": oid}

    def _encode_object(self, obj, depth):
        import types as _types

        entry = {"id": self._obj_id(obj), "type": type(obj).__name__}

        if isinstance(obj, (list, tuple, set, frozenset)):
            kind = ("list" if isinstance(obj, list) else
                    "tuple" if isinstance(obj, tuple) else "set")
            try:
                members = list(obj)
            except Exception:
                return dict(entry, kind="opaque", repr=self._safe_repr(obj))
            entry["truncated"] = len(members) > MAX_ITEMS
            entry["size"] = len(members)
            entry["kind"] = kind
            entry["items"] = [self.encode(m, depth + 1) for m in members[:MAX_ITEMS]]
            return entry

        if isinstance(obj, dict):
            entry["kind"] = "dict"
            entry["size"] = len(obj)
            pairs = list(obj.items())[:MAX_ITEMS]
            entry["truncated"] = len(obj) > MAX_ITEMS
            entry["items"] = [[self.encode(k, depth + 1),
                               self.encode_named(k, v, depth + 1)]
                              for k, v in pairs]
            return entry

        if isinstance(obj, _types.FunctionType):
            return dict(entry, kind="function", name=getattr(obj, "__name__", "<lambda>"))

        if isinstance(obj, _types.ModuleType):
            return dict(entry, kind="module", name=getattr(obj, "__name__", "?"))

        if isinstance(obj, type):
            return dict(entry, kind="class", name=obj.__name__)

        if isinstance(obj, BaseException):
            return dict(entry, kind="opaque", repr="%s: %s" % (type(obj).__name__, obj))

        # A plain instance: show its attributes.
        attrs = None
        if hasattr(obj, "__dict__"):
            try:
                attrs = list(vars(obj).items())
            except Exception:
                attrs = None
        elif hasattr(type(obj), "__slots__"):
            attrs = []
            for slot in type(obj).__slots__:
                if hasattr(obj, slot):
                    attrs.append((slot, getattr(obj, slot)))

        if attrs is not None:
            entry["kind"] = "instance"
            entry["name"] = type(obj).__name__
            entry["items"] = [[k, self.encode_named(k, v, depth + 1)]
                              for k, v in attrs[:MAX_ITEMS]]
            return entry

        return dict(entry, kind="opaque", repr=self._safe_repr(obj))

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    def _frame_id(self, frame):
        key = id(frame)
        if key not in self._frame_ids:
            self._frame_ids[key] = self._next_frame_id
            self._next_frame_id += 1
        return self._frame_ids[key]

    def _is_user_frame(self, frame):
        return frame is not None and frame.f_code.co_filename == FILENAME

    def _encode_frame(self, frame, is_global):
        names = []
        if is_global:
            # Module scope: hide the machinery we injected, keep user names.
            for name in frame.f_locals:
                if name.startswith("__") and name.endswith("__"):
                    continue
                names.append(name)
        else:
            code = frame.f_code
            # Arguments first, in declaration order, then the rest as they appear.
            argcount = code.co_argcount + code.co_kwonlyargcount
            ordered = list(code.co_varnames[:argcount])
            for name in code.co_varnames[argcount:]:
                if name not in ordered:
                    ordered.append(name)
            for name in frame.f_locals:
                if name not in ordered:
                    ordered.append(name)
            names = ordered

        variables = []
        for name in names:
            if name not in frame.f_locals:
                continue  # declared but not yet assigned at this point
            value = frame.f_locals[name]
            variables.append({"name": name, "value": self.encode_named(name, value)})

        return {
            "fid": 0 if is_global else self._frame_id(frame),
            "func": "<module>" if is_global else frame.f_code.co_name,
            "line": frame.f_lineno,
            "is_global": is_global,
            "vars": variables,
        }

    def _stack(self, frame):
        """Frames from the module level down to the currently executing one."""
        chain = []
        while self._is_user_frame(frame):
            chain.append(frame)
            frame = frame.f_back
        chain.reverse()
        return [self._encode_frame(f, i == 0) for i, f in enumerate(chain)]

    # ------------------------------------------------------------------
    # Tracing
    # ------------------------------------------------------------------

    def _snapshot(self, frame, event, arg):
        self._heap = {}
        self._seen = set()

        stack = self._stack(frame)
        step = {
            "i": len(self.steps),
            "event": event,
            "line": frame.f_lineno,
            "func": stack[-1]["func"] if stack else "<module>",
            "depth": len(stack),
            "stack": stack,
            "heap": self._heap,
            "out": self._output_length(),
        }

        if event == "return":
            step["retval"] = self.encode(arg)
        elif event == "exception":
            exc_type, exc_value, _ = arg
            step["exc"] = "%s: %s" % (exc_type.__name__, exc_value)

        self.steps.append(step)

    def _output_length(self):
        return min(len(self._out.getvalue()), MAX_OUTPUT)

    def _trace(self, frame, event, arg):
        if frame.f_code.co_filename != FILENAME:
            return None  # never step into library code
        if event not in ("call", "line", "return", "exception"):
            return self._trace
        # The module body itself is reported as a 'call' at line 0. That is an
        # artefact of exec(), not something the user wrote, so skip it.
        if event == "call" and not self._is_user_frame(frame.f_back):
            return self._trace
        if len(self.steps) >= self.max_steps:
            raise _StepLimit()
        self._snapshot(frame, event, arg)
        return self._trace

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        try:
            compiled = compile(self.code, FILENAME, "exec")
        except SyntaxError as exc:
            self.error = {
                "type": "SyntaxError",
                "message": str(exc.msg),
                "line": exc.lineno,
            }
            return self.result()

        env = {"__name__": "__main__", "__builtins__": __builtins__, "__file__": FILENAME}

        real_stdout, real_stderr, real_stdin = sys.stdout, sys.stderr, sys.stdin
        sys.stdout = sys.stderr = self._out
        sys.stdin = io.StringIO(self.stdin)

        limit_hit = False
        try:
            sys.settrace(self._trace)
            try:
                exec(compiled, env)
            finally:
                sys.settrace(None)
        except _StepLimit:
            limit_hit = True
        except SystemExit:
            pass
        except BaseException as exc:
            tb = exc.__traceback__
            line = None
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == FILENAME:
                    line = tb.tb_lineno
                tb = tb.tb_next
            self.error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "line": line,
                "traceback": "".join(
                    traceback.format_exception_only(type(exc), exc)
                ).strip(),
            }
        finally:
            sys.stdout, sys.stderr, sys.stdin = real_stdout, real_stderr, real_stdin

        if limit_hit:
            self.error = {
                "type": "StepLimitExceeded",
                "message": "Stopped after %d steps. The program may loop forever, "
                           "or is simply too long to visualise." % self.max_steps,
                "line": self.steps[-1]["line"] if self.steps else None,
            }

        return self.result()

    def result(self):
        output = self._out.getvalue()
        truncated = len(output) > MAX_OUTPUT
        return {
            "steps": self.steps,
            "stdout": output[:MAX_OUTPUT],
            "stdout_truncated": truncated,
            "error": self.error,
            "code": self.code,
            "step_count": len(self.steps),
            "graphs": self._graphs(),
        }

    def _graphs(self):
        """Flowcharts for the flow-graph view. Never fatal to a trace."""
        try:
            from . import cfg
        except ImportError:
            try:
                import cfg
            except ImportError:
                return []
        try:
            return cfg.build_graphs(self.code)
        except Exception:
            return []


def trace_code(code, stdin="", max_steps=MAX_STEPS, redact=True):
    """Trace ``code`` and return the JSON-serialisable result dictionary.

    ``redact`` masks values that look like API keys or passwords; pass False
    when you actually need to inspect them.
    """
    return Tracer(code, stdin=stdin, max_steps=max_steps, redact=redact).run()


def _main():
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        json.dump({"error": {"type": "BadRequest", "message": str(exc)},
                   "steps": [], "stdout": ""}, sys.stdout)
        return

    result = trace_code(
        request.get("code", ""),
        stdin=request.get("stdin", ""),
        max_steps=int(request.get("max_steps", MAX_STEPS)),
        redact=bool(request.get("redact", True)),
    )
    # Written to the real stdout, which the parent process reads. The marker
    # lets the parent discard anything the traced program managed to write
    # directly to the underlying file descriptor.
    sys.__stdout__.write(RESULT_MARKER)
    json.dump(result, sys.__stdout__, default=str)
    sys.__stdout__.flush()


if __name__ == "__main__":
    _main()
