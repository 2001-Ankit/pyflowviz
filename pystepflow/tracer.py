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

    echo '{"code": "...", "stdin": ""}' | python -m pystepflow.tracer
"""

import fnmatch
import io
import json
import os
import runpy
import sys
import traceback

FILENAME = "<user_code>"
RESULT_MARKER = "\n@@TRACE_RESULT@@\n"

# Directories never counted as "your code", even when they sit inside the
# project root. Without these, tracing a project descends into its own
# virtualenv and drowns in library internals.
DEFAULT_EXCLUDES = (
    "*/site-packages/*", "*/dist-packages/*", "*/.venv/*", "*/venv/*",
    "*/env/*", "*/__pycache__/*", "*/node_modules/*", "*/.git/*",
    "*/.tox/*", "*/build/*", "*/dist/*",
)

# Safety limits. Tuned so a teaching-sized program traces instantly while a
# runaway loop stops rather than eating all memory.
MAX_STEPS = 5000        # snapshots recorded before we stop
MAX_OUTPUT = 200_000    # characters of stdout kept
MAX_STRING = 300        # characters kept per repr'd string
MAX_ITEMS = 200         # elements shown per container
MAX_DEPTH = 8           # nesting depth before we fall back to repr

# Snapshots are stored as deltas against the previous step, because almost
# nothing changes between two lines of a program: re-sending every frame and
# every heap object each time is what made traces enormous. Every Nth step is
# a full "keyframe", so the browser can jump anywhere on the timeline and
# rebuild the state by replaying at most this many deltas.
KEYFRAME_EVERY = 40

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
    def __init__(self, code=None, stdin="", max_steps=MAX_STEPS, redact=True,
                 entry=None, project_root=None, include=None, exclude=None,
                 trace_imports=False):
        # Editors on Windows often save a UTF-8 BOM, which compile() rejects.
        self.code = (code or "").lstrip("﻿")
        self.stdin = stdin
        self.max_steps = max_steps
        self.redact = redact

        # ---- project mode -------------------------------------------------
        # With an entry script we trace every file under the project root
        # instead of a single source string, so calls between your own modules
        # are stepped into rather than skipped over.
        self.entry = os.path.abspath(entry) if entry else None
        if project_root:
            self.project_root = os.path.abspath(project_root)
        elif self.entry:
            self.project_root = os.path.dirname(self.entry)
        else:
            self.project_root = None
        self.include = list(include or [])
        self.exclude = list(exclude or []) + list(DEFAULT_EXCLUDES)
        self.trace_imports = trace_imports

        self._file_cache = {}      # abs path -> is it ours?
        self._file_keys = {}       # abs path -> short key shown in the UI
        self._sources = {}         # short key -> source text
        self._entry_key = None

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

        # What the browser already knows, mirrored here so we can send only the
        # difference. Both are replaced wholesale on a keyframe.
        self._prev_heap = {}
        self._prev_frames = {}     # fid -> the vars list last sent for it

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

    # ------------------------------------------------------------------
    # Which files count as "your code"
    # ------------------------------------------------------------------

    @property
    def project_mode(self):
        return self.entry is not None

    def _is_user_file(self, path):
        """True when a file is part of the program being visualised.

        This is the whole boundary of the tracer. In single-snippet mode it is
        one virtual filename; in project mode it is everything under the
        project root that is not a dependency.
        """
        if not self.project_mode:
            return path == FILENAME

        cached = self._file_cache.get(path)
        if cached is not None:
            return cached

        verdict = self._classify(path)
        self._file_cache[path] = verdict
        return verdict

    def _classify(self, path):
        if not path or path.startswith("<"):
            return False              # <frozen importlib>, <string>, and friends
        try:
            full = os.path.abspath(path)
        except (OSError, ValueError):
            return False
        if not full.lower().endswith(".py"):
            return False

        root = os.path.normcase(self.project_root)
        if not os.path.normcase(full).startswith(root + os.sep) and \
                os.path.normcase(full) != os.path.normcase(self.entry):
            return False

        posix = full.replace(os.sep, "/")
        for pattern in self.exclude:
            if fnmatch.fnmatch(posix, pattern):
                return False
        if self.include:
            relative = os.path.relpath(full, self.project_root).replace(os.sep, "/")
            if not any(fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(posix, p)
                       for p in self.include):
                return False
        return True

    def _file_key(self, path):
        """Short, stable, display-friendly name for a traced file."""
        key = self._file_keys.get(path)
        if key is not None:
            return key

        if not self.project_mode:
            key = "main.py"
        else:
            try:
                key = os.path.relpath(path, self.project_root).replace(os.sep, "/")
            except ValueError:
                key = os.path.basename(path)

        self._file_keys[path] = key
        if key not in self._sources:
            self._sources[key] = self._read_source(path)
        return key

    def _read_source(self, path):
        if not self.project_mode:
            return self.code
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                return handle.read()
        except OSError:
            return ""

    def _is_user_frame(self, frame):
        return frame is not None and self._is_user_file(frame.f_code.co_filename)

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

        # Which values moved since this frame was last shown. Computed here
        # rather than in the browser, which no longer holds two steps at once.
        # A frame's first appearance reports nothing, so entering a function
        # does not light up every argument.
        fid = self._frame_id(frame)
        previous = self._prev_frames.get(fid)
        changed = []
        if previous is not None:
            was = {v["name"]: v["value"] for v in previous}
            changed = [v["name"] for v in variables
                       if v["name"] in was and was[v["name"]] != v["value"]]

        return {
            "fid": fid,
            "func": "<module>" if is_global else frame.f_code.co_name,
            "line": frame.f_lineno,
            "file": self._file_key(frame.f_code.co_filename),
            "is_global": is_global,
            "vars": variables,
            "chg": changed,
        }

    def _stack(self, frame):
        """Frames from the outermost traced frame down to the current one.

        The chain stops at the first frame that is not ours, so a callback
        invoked from library code (a ``sorted`` key function, say) shows only
        the part of the stack that belongs to the program.
        """
        chain = []
        while self._is_user_frame(frame):
            chain.append(frame)
            frame = frame.f_back
        chain.reverse()
        # Module scope is decided per frame, not by position: in project mode
        # the outermost traced frame is not necessarily a module body.
        return [self._encode_frame(f, f.f_code.co_name == "<module>") for f in chain]

    # ------------------------------------------------------------------
    # Tracing
    # ------------------------------------------------------------------

    def _snapshot(self, frame, event, arg):
        self._heap = {}
        self._seen = set()

        stack = self._stack(frame)
        heap = self._heap
        step = {
            "i": len(self.steps),
            "event": event,
            "line": frame.f_lineno,
            "func": stack[-1]["func"] if stack else "<module>",
            "file": self._file_key(frame.f_code.co_filename),
            "depth": len(stack),
            "out": self._output_length(),
        }
        self._encode_step(step, stack, heap)

        if event == "return":
            step["retval"] = self.encode(arg)
        elif event == "exception":
            exc_type, exc_value, _ = arg
            step["exc"] = "%s: %s" % (exc_type.__name__, exc_value)

        self.steps.append(step)

    def _encode_step(self, step, stack, heap):
        """Store the step as a keyframe or as a delta against the last one.

        A keyframe carries the whole stack and heap. A delta carries only the
        frames whose variables moved, the heap objects that were added or
        changed, and the ids that went out of scope. Frame identity, line
        numbers and file names are always sent, so the browser only has to
        reconstruct variables and the heap.
        """
        if len(self.steps) % KEYFRAME_EVERY == 0:
            step["full"] = 1
            step["stack"] = stack
            step["heap"] = heap
            self._prev_frames = {f["fid"]: f["vars"] for f in stack}
            self._prev_heap = heap
            return

        compact = []
        for entry in stack:
            previous = self._prev_frames.get(entry["fid"])
            if previous is not None and previous == entry["vars"]:
                # Unchanged: send identity and position, drop the variables.
                compact.append({
                    "fid": entry["fid"], "func": entry["func"],
                    "line": entry["line"], "file": entry["file"],
                    "is_global": entry["is_global"],
                })
            else:
                compact.append(entry)
                self._prev_frames[entry["fid"]] = entry["vars"]
        step["stack"] = compact

        changed = {oid: obj for oid, obj in heap.items()
                   if self._prev_heap.get(oid) != obj}
        gone = [oid for oid in self._prev_heap if oid not in heap]
        if changed:
            step["hset"] = changed
        if gone:
            step["hdel"] = gone
        self._prev_heap = heap

    def _output_length(self):
        return min(len(self._out.getvalue()), MAX_OUTPUT)

    def _trace(self, frame, event, arg):
        code = frame.f_code
        if not self._is_user_file(code.co_filename):
            return None  # never step into library or dependency code

        # Importing a module runs its whole body: class and function
        # definitions, constants, decorators. That is setup, not the flow you
        # asked to see, and it is enormous -- importing a handful of modules
        # costs tens of thousands of events. Skip those bodies unless asked.
        if (self.project_mode and not self.trace_imports
                and code.co_name == "<module>"
                and os.path.abspath(code.co_filename) != self.entry):
            return None

        if event not in ("call", "line", "return", "exception"):
            return self._trace
        # A module body is reported as a 'call' at line 0, an artefact of how
        # it is executed rather than something the user wrote.
        if event == "call" and code.co_name == "<module>":
            return self._trace
        if len(self.steps) >= self.max_steps:
            raise _StepLimit()
        self._snapshot(frame, event, arg)

        # Once a frame returns, forget its id. CPython reuses the memory of a
        # dead frame, so without this a later call could be handed the same
        # frame id -- and with delta encoding it would inherit its variables.
        if event == "return":
            retired = self._frame_ids.pop(id(frame), None)
            if retired is not None:
                self._prev_frames.pop(retired, None)
        return self._trace

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        if self.project_mode:
            return self._run(self._exec_project)

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
        return self._run(lambda: exec(compiled, env))

    def _exec_project(self):
        """Run the entry script the way ``python entry.py`` would."""
        entry_dir = os.path.dirname(self.entry)
        for path in (self.project_root, entry_dir):
            if path and path not in sys.path:
                sys.path.insert(0, path)
        sys.argv = [self.entry]
        self._entry_key = self._file_key(self.entry)
        runpy.run_path(self.entry, run_name="__main__")

    def _run(self, body):
        real_stdout, real_stderr, real_stdin = sys.stdout, sys.stderr, sys.stdin
        sys.stdout = sys.stderr = self._out
        sys.stdin = io.StringIO(self.stdin)

        limit_hit = False
        try:
            sys.settrace(self._trace)
            try:
                body()
            finally:
                sys.settrace(None)
        except _StepLimit:
            limit_hit = True
        except SystemExit:
            pass
        except BaseException as exc:
            # Report the deepest frame that belongs to the program, so an
            # exception raised inside a library is still blamed on the line of
            # your code that led there.
            tb = exc.__traceback__
            line = file_key = None
            while tb is not None:
                path = tb.tb_frame.f_code.co_filename
                if self._is_user_file(path):
                    line = tb.tb_lineno
                    file_key = self._file_key(path)
                tb = tb.tb_next
            self.error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "line": line,
                "file": file_key,
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

        if not self.project_mode and not self._sources:
            self._sources["main.py"] = self.code

        return {
            "steps": self.steps,
            "stdout": output[:MAX_OUTPUT],
            "stdout_truncated": truncated,
            "error": self.error,
            "code": self.code,
            "step_count": len(self.steps),
            "graphs": self._graphs(),
            # The browser needs to know how to read `steps`.
            "format": "delta",
            "keyframe_every": KEYFRAME_EVERY,
            # Project mode extras. The UI shows one file at a time and follows
            # execution across them, so it needs every source that ran.
            "mode": "project" if self.project_mode else "snippet",
            "sources": self._sources,
            "entry": self._entry_key,
            "files": sorted(self._sources),
            "map": self._project_map(),
        }

    def _project_map(self):
        """The static module graph, with this run painted onto it."""
        if not self.project_mode:
            return None
        try:
            try:
                from . import projectmap
            except ImportError:
                import projectmap
            built = projectmap.build_map(
                self.project_root, is_wanted=self._is_user_file, entry=self.entry)
            return projectmap.overlay(built, self.steps)
        except Exception:
            return None      # a missing map must never cost you the trace

    def _graphs(self):
        """Flowcharts for the flow-graph view. Never fatal to a trace."""
        try:
            from . import cfg
        except ImportError:
            try:
                import cfg
            except ImportError:
                return []

        graphs = []
        for key, source in self._sources.items():
            if not source:
                continue
            try:
                built = cfg.build_graphs(source)
            except Exception:
                continue
            for graph in built:
                graph["file"] = key
            graphs.extend(built)
        return graphs


def trace_code(code, stdin="", max_steps=MAX_STEPS, redact=True):
    """Trace a single source string and return the result dictionary.

    ``redact`` masks values that look like API keys or passwords; pass False
    when you actually need to inspect them.
    """
    return Tracer(code, stdin=stdin, max_steps=max_steps, redact=redact).run()


def trace_project(entry, project_root=None, stdin="", max_steps=MAX_STEPS,
                  redact=True, include=None, exclude=None, trace_imports=False):
    """Trace a real script, stepping across every module in the project.

    ``entry`` is run as ``__main__``. Every ``.py`` file under
    ``project_root`` (defaulting to the entry's directory) is traced, except
    dependencies and anything matched by ``exclude``. Pass ``include`` globs to
    narrow tracing to one subsystem, which is usually necessary on a large
    codebase.
    """
    return Tracer(
        entry=entry, project_root=project_root, stdin=stdin,
        max_steps=max_steps, redact=redact, include=include, exclude=exclude,
        trace_imports=trace_imports,
    ).run()


def _main():
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        json.dump({"error": {"type": "BadRequest", "message": str(exc)},
                   "steps": [], "stdout": ""}, sys.stdout)
        return

    common = dict(
        stdin=request.get("stdin", ""),
        max_steps=int(request.get("max_steps", MAX_STEPS)),
        redact=bool(request.get("redact", True)),
    )
    if request.get("mode") == "project" and request.get("entry"):
        result = trace_project(
            request["entry"],
            project_root=request.get("project_root"),
            include=request.get("include"),
            exclude=request.get("exclude"),
            trace_imports=bool(request.get("trace_imports")),
            **common
        )
    else:
        result = trace_code(request.get("code", ""), **common)
    # Written to the real stdout, which the parent process reads. The marker
    # lets the parent discard anything the traced program managed to write
    # directly to the underlying file descriptor.
    sys.__stdout__.write(RESULT_MARKER)
    json.dump(result, sys.__stdout__, default=str)
    sys.__stdout__.flush()


if __name__ == "__main__":
    _main()
