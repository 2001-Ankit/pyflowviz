# pystepflow

Step through Python code in your browser and watch it run: the current line,
the call stack, every variable, the objects on the heap with arrows showing
what points at what, the output as it appears — and a live flow graph of the
code itself.

Standard library only. No pip dependencies, no internet access, nothing leaves
your machine.

---

## Install

```bash
pip install pystepflow
```

Or from a checkout of this repository:

```bash
pip install -e .
```

Either gives you a `pystepflow` command. If it isn't found afterwards, pip put it
in a Scripts directory that isn't on your PATH — this form always works:

```bash
python -m pystepflow.server
```

Full documentation lives in [docs/documentation.html](docs/documentation.html)
(open it in a browser, or print it to PDF — see [docs/README.md](docs/README.md)).

## Use it on your own code

```bash
pystepflow                       # open the editor, paste or type code
pystepflow my_script.py          # open with your script loaded
pystepflow my_script.py -p 9000  # different port
pystepflow -w ~/projects         # allow the UI to open .py files under that folder
pystepflow --no-browser          # don't launch a browser
pystepflow agent.py -t 120       # allow 120s per trace (slow API calls)
pystepflow --show-secrets        # don't mask values that look like API keys
```

Your browser opens at <http://127.0.0.1:8000>. Press **Visualize** (or
`Ctrl+Enter`) and step through.

You can also load code straight from the UI with **Open file**, or pick one of
the built-in examples from the **Examples…** menu.

## Tracing a whole project

By default only the file you are looking at is traced, so a call into another
module of your own shows up as a single step. Pass `--project` to step across
every module instead:

```bash
pystepflow main.py --project
```

`main.py` is then run from disk exactly as `python main.py` would run it, and
every `.py` file under the project root is traced. The code panel follows
execution from file to file, the call stack labels each frame with its module,
and the flow graph resolves the right function even when two modules define the
same name.

Dependencies are excluded automatically — `site-packages`, `.venv`, `venv`,
`__pycache__`, `node_modules`, `build`, `dist` and friends — even when they sit
inside the project root. Module bodies are skipped as they are imported, since
that is class and function definitions rather than the flow you asked to see;
pass `--trace-imports` if you want them.

```bash
pystepflow main.py --project --root .            # project root, if not the entry's folder
pystepflow main.py --project --include "services/*"  # narrow to one subsystem
pystepflow main.py --project --exclude "*/legacy/*"  # skip a corner of the tree
pystepflow main.py --project --trace-imports         # include import-time code
```

### The project map

Project mode adds a third tab on the right: every module in the project, laid
out by how far it sits from the entry point, with this run painted on top.

- Modules that ran are filled in and show `2/3 fn · 14 steps`.
- Modules that never ran are dashed and greyed, with their function count —
  `2 fn never ran`.
- Grey dashed arrows are **imports** (static, from the source).
- Blue arrows are **calls that actually happened**, with counts. Importing a
  module says nothing about whether you ever call into it, so these are drawn
  separately.

That difference is the point: dead modules, unused functions and unexercised
paths are visible next to the code that did run. Clicking a module opens its
source in the code panel — including modules that never executed, which are
fetched from disk on demand.

Two things worth knowing:

- **`--include` matters on a large codebase.** Tracing is capped at 5000 steps;
  a big program will hit that before it reaches what you care about. Narrow to
  the subsystem you are studying.
- **Edits in the browser are ignored in project mode.** The files on disk are
  what run. Edit them in your editor and press Visualize again.
- **Dependencies must be importable by the interpreter running the server.** If
  your project uses a virtualenv, start pystepflow from inside it.

## Getting around

| Control | Does |
| --- | --- |
| `→` / `←` | next / previous step |
| `↓` | step **over** — run a call without descending into it |
| `↑` | step **out** — run until the current function returns |
| `Space` | play / pause |
| `Home` / `End` | jump to the first / last step |
| `Ctrl+Enter` | re-run the trace |
| `Esc` | back to editing |

The timeline slider scrubs through the whole run. Clicking a box in the flow
graph, or a call in the call tree, jumps to the moment it ran.

## What the panels show

**Code** — the line about to execute is highlighted in blue. Lines of calling
functions further up the stack are violet, a `return` is green, an exception is
red.

**Program state** — the call stack (innermost frame on top) with every local
variable, and the heap next to it. Variables holding a list, dict, object or
function show a chip with an arrow drawn to the actual object, so aliasing is
visible: if two names point at the same list, you see two arrows into one box.
Values that changed since the previous step are tinted amber. Hover a chip to
light up its target.

**Flow graph** — a flowchart built from your code's own structure: branches fan
out and merge, loops route a dashed arrow back to their test. As you step, the
boxes that have run fill in, the arrows actually taken turn blue, `×N` badges
count how many times each box ran, and the current box is outlined. It follows
you into whichever function is executing; untick **follow** to pin one function,
or pick another from the dropdown.

**Call tree** — every call made, nested, with arguments and return values.

**Input / Output** — anything you put in the Input box is fed to `input()`.
Program output appears incrementally, so you see exactly how much had been
printed at each step.

## What it can trace

Ordinary Python that runs to completion: functions, recursion, classes and
inheritance, closures and decorators, comprehensions, generators, `try`/`except`,
`with`, f-strings, `input()`, and imports of standard-library modules.

Some things it deliberately does **not** step into:

- **Library internals.** Stepping stops at your file's boundary. Calling
  `json.dumps(...)` shows the call and its result, not a walk through `json`.
- **C-level code.** `sorted()`, `len()`, NumPy operations and the like are one
  step, because there are no Python lines inside to show.
- **Threads and async.** Only the main thread is traced; `asyncio` code runs but
  the event loop's own frames are skipped.
- **Anything needing a real terminal, GUI, or network prompt.** Use the Input
  box for `input()`; `curses`, `tkinter` and friends won't render.

## GenAI / LLM code

It works well, and it is arguably the best thing to point it at — an agent loop
is exactly the kind of control flow that is hard to hold in your head. Verified
against agent loops with tool dispatch, retries, streaming and pydantic
response objects.

What you see is **your orchestration logic**, stepped one line at a time: the
message list growing turn by turn, which tool got selected and why, the retry
branch being taken, chunks accumulating in the streaming loop. The flow graph
shows the agent loop as a loop, with `×N` counting the turns.

What you don't see is the inside of the SDK. `client.messages.create(...)` is a
single step showing the call and the response object — the HTTP machinery
inside `anthropic`, `openai` or `langchain` is not stepped through, the same as
any other library.

Two things to set up first:

```bash
pystepflow agent.py --timeout 120      # API calls are slower than the 15s default
```

Without that, a real model call trips the timeout. Give it enough room for the
whole run, not just one call — an agent doing five turns needs five turns'
worth.

**API keys are masked by default.** Every variable ends up in the browser, so
values named like credentials (`api_key`, `password`, `access_token`, …) and
values shaped like them (`sk-…`, `ghp_…`, `AKIA…`) are replaced with
`<redacted>` in the variables panel, in object attributes, and inside dicts —
including a key read from `os.environ`, which never reaches the browser at all.
Ordinary vocabulary is left alone: `tokens`, `token_count`, `max_tokens`, `key`
and `keys` are not touched. Pass `--show-secrets` if you need the real value.

A key written as a literal in your source is still visible in the code panel,
because that is your source being displayed. Read it from the environment if
that matters.

Two practical notes: **the calls really happen**, so an agent loop you step
through costs the same as one you run — and each step re-snapshots your data,
so a 1536-float embedding or a long conversation makes traces large. Lists are
truncated at 200 elements for display, but keep the traced portion small.

## Limits

Limits that keep the browser responsive, all in `pystepflow/tracer.py`:

| Limit | Default | Meaning |
| --- | --- | --- |
| `MAX_STEPS` | 5000 | snapshots recorded, then it stops and tells you |
| `MAX_ITEMS` | 200 | elements shown per list/dict |
| `MAX_STRING` | 300 | characters per string |
| `MAX_OUTPUT` | 200000 | characters of output kept |
| timeout | 15 s | wall clock per trace, in `server.py` |

Raise them if your program is bigger; a long trace just uses more memory.

Snapshots are stored as deltas against the previous step, with a full keyframe
every 40 steps, so scrubbing the timeline stays fast while the payload stays
small — measured across four ordinary programs, 14 MB of full snapshots becomes
1.87 MB. Stepping is unaffected: the whole run is still recorded up front, and
moving backwards never re-executes anything.

## Careful

**The code really runs.** This is a tracer, not a sandbox — file writes,
network calls and `os.system` all do what they normally do. Only visualize code
you would be willing to run directly, and keep the server on `127.0.0.1`
(the default) rather than exposing it to a network.

## Using it as a library

```python
from pystepflow import trace_code

result = trace_code("""
a = [1, 2]
b = a
b.append(3)
""")

print(result["step_count"])          # 4
print(result["steps"][-1]["stack"])  # frames, variables, heap refs
print(result["graphs"][0]["nodes"])  # flowchart of the module
```

Or start the server from Python:

```python
from pystepflow import serve
serve(port=8000, file="my_script.py")
```

## How it works

`tracer.py` installs `sys.settrace` and takes a snapshot on every `line`,
`call`, `return` and `exception` event inside your file. Each snapshot walks the
frame chain and encodes every variable: primitives inline, everything else into
a per-step heap keyed by a stable id — which is what makes the reference arrows
possible.

`cfg.py` parses the same source with `ast` and lays out a flowchart per
function, recursively: a statement block is a column, an `if` fans into two
columns that merge, a loop puts its body under the test with a back-edge. The
browser only draws the finished coordinates.

`server.py` runs the tracer in a **subprocess** with a timeout, so an infinite
loop or a crash in your code cannot take the server down.

The frontend is three plain files — no framework, no build step, no CDN.

```
pystepflow/
├── tracer.py        sys.settrace snapshots → JSON
├── cfg.py           ast → laid-out flowcharts
├── server.py        http.server + /api/trace
└── static/
    ├── index.html
    ├── style.css
    ├── app.js       stepping, stack/heap rendering, arrows
    ├── graph.js     flow-graph SVG + execution overlay
    └── examples.js  the built-in programs
```

## License

MIT.
