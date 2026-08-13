"""
pyflowviz — step through your Python code in the browser.

Run any script and watch it execute line by line: the call stack, every
variable, the objects on the heap and the references between them, a flow
graph of the code itself, and the output as it appears.

Command line::

    pyflowviz                    # open the editor
    pyflowviz my_script.py       # open with that script loaded
    pyflowviz agent.py -t 120    # allow slow calls (API requests, etc.)

From Python::

    from pyflowviz import trace_code, serve

    result = trace_code("x = [1, 2]\\nx.append(3)\\n")
    print(result["step_count"])

    serve(port=8000)             # start the web UI
"""

from .tracer import trace_code, Tracer

__version__ = "1.0.0"
__all__ = ["trace_code", "Tracer", "serve", "main"]


def serve(port=8000, host="127.0.0.1", file=None, workspace=None,
          open_browser=True, timeout=None, show_secrets=False):
    """Start the visualizer web server (blocks until interrupted)."""
    from . import server

    argv = []
    if file:
        argv.append(str(file))
    argv += ["--port", str(port), "--host", host]
    if workspace:
        argv += ["--workspace", str(workspace)]
    if not open_browser:
        argv.append("--no-browser")
    if timeout:
        argv += ["--timeout", str(timeout)]
    if show_secrets:
        argv.append("--show-secrets")
    return server.main(argv)


def main(argv=None):
    """Console-script entry point for ``pyflowviz``."""
    from . import server

    return server.main(argv)
