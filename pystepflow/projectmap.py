"""
A map of the whole project: which modules exist, and which imports connect them.

The tracer shows what a single run *did*. This shows what the project *is* --
including code that never ran. Drawn together, the difference between the two
is the interesting part: dead modules, unused functions, and the paths a run
never took.

Built from ``ast`` alone, so nothing is executed. Import edges are resolved
against the files actually on disk, which means an edge always points at a real
module of yours; imports of third-party packages are dropped rather than drawn
as dangling nodes.
"""

import ast
import os

# Layout geometry, in CSS pixels, matching cfg.py's conventions.
CHAR_W = 6.6
NODE_PAD = 22
NODE_H = 38
ROW_GAP = 62
COL_GAP = 26
MIN_NODE_W = 96
MAX_LABEL = 34
MAX_MODULES = 250        # beyond this the picture stops being useful


def _node_width(label):
    return max(MIN_NODE_W, min(len(label), MAX_LABEL) * CHAR_W + NODE_PAD)


def _module_name(path, root):
    """Dotted module name for a file, the way Python would import it."""
    relative = os.path.relpath(path, root)
    parts = relative.replace(os.sep, "/").split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]          # strip .py
    return ".".join(p for p in parts if p)


def _iter_files(root, is_wanted):
    for folder, dirs, names in os.walk(root):
        # Prune excluded directories rather than walking into them.
        dirs[:] = [d for d in dirs
                   if is_wanted(os.path.join(folder, d, "__probe__.py"))
                   or not d.startswith((".", "_"))]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            if is_wanted(path):
                yield path


def _definitions(tree):
    """Top-level functions, plus methods as ``Class.method``."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append("%s.%s" % (node.name, child.name))
    return found


def _imports(tree, own_name):
    """Dotted names this module imports, with relative imports resolved."""
    package = own_name.rsplit(".", 1)[0] if "." in own_name else ""
    targets = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from ..pkg import y`
                base = own_name.split(".")
                base = base[:-1] if own_name else []
                for _ in range(node.level - 1):
                    base = base[:-1]
                prefix = ".".join(base)
                head = ".".join(p for p in (prefix, node.module) if p)
            else:
                head = node.module or ""
            if not head:
                continue
            targets.append(head)
            # `from package import module` imports a submodule, not a name.
            for alias in node.names:
                targets.append(head + "." + alias.name)

    if package:
        targets.append(package)
    return targets


def _layer(modules, edges, entry_name):
    """Assign each module a row: distance from the entry point along imports."""
    outgoing = {}
    for src, dst in edges:
        outgoing.setdefault(src, set()).add(dst)

    depth = {}
    if entry_name in modules:
        frontier = [entry_name]
        depth[entry_name] = 0
        while frontier:
            nxt = []
            for name in frontier:
                for target in sorted(outgoing.get(name, ())):
                    # `not in depth` also breaks import cycles: the first time
                    # we reach a module wins, and the back edge is ignored.
                    if target not in depth:
                        depth[target] = depth[name] + 1
                        nxt.append(target)
            frontier = nxt

    # Anything the entry never reaches still belongs on the map -- that is
    # exactly the code worth noticing.
    orphan_row = (max(depth.values()) + 1) if depth else 0
    for name in modules:
        depth.setdefault(name, orphan_row)
    return depth


def build_map(root, is_wanted=None, entry=None):
    """Return the project's module graph, laid out and ready to draw."""
    root = os.path.abspath(root)
    if is_wanted is None:
        is_wanted = lambda path: True

    modules = {}
    raw_imports = {}

    for path in _iter_files(root, is_wanted):
        if len(modules) >= MAX_MODULES:
            break
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                source = handle.read()
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue

        name = _module_name(path, root)
        key = os.path.relpath(path, root).replace(os.sep, "/")
        modules[name] = {
            "name": name,
            "file": key,
            "functions": _definitions(tree),
            "lines": source.count("\n") + 1,
        }
        raw_imports[name] = _imports(tree, name)

    # Keep only edges between modules that actually exist in this project.
    edges = set()
    for name, targets in raw_imports.items():
        for target in targets:
            if target in modules and target != name:
                edges.add((name, target))

    entry_name = None
    if entry:
        try:
            entry_name = _module_name(os.path.abspath(entry), root)
        except ValueError:
            entry_name = None

    depth = _layer(modules, edges, entry_name)

    # Place each row, widest label first so the row reads tidily.
    rows = {}
    for name in modules:
        rows.setdefault(depth[name], []).append(name)

    laid_out = []
    width = 0.0
    for row in sorted(rows):
        names = sorted(rows[row], key=lambda n: (-len(modules[n]["functions"]), n))
        x = 0.0
        for name in names:
            entry_node = modules[name]
            w = _node_width(name)
            entry_node.update({
                "x": x, "y": row * (NODE_H + ROW_GAP),
                "w": w, "h": NODE_H,
                "row": row,
                "is_entry": name == entry_name,
            })
            laid_out.append(entry_node)
            x += w + COL_GAP
        width = max(width, x - COL_GAP)

    height = (max(rows) + 1) * (NODE_H + ROW_GAP) - ROW_GAP if rows else 0

    pad = 18
    for node in laid_out:
        node["x"] += pad
        node["y"] += pad

    return {
        "modules": laid_out,
        "edges": [{"from": a, "to": b} for a, b in sorted(edges)],
        "entry": entry_name,
        "width": width + pad * 2,
        "height": height + pad * 2,
        "truncated": len(modules) >= MAX_MODULES,
    }


def overlay(project_map, steps):
    """Mark what a run actually touched, so the drawing can show the gap.

    Adds to each module: whether it ran, which of its functions ran, and how
    many steps happened inside it. Adds call edges observed between modules,
    which are not the same as import edges -- importing a module says nothing
    about whether you ever call into it.
    """
    by_file = {m["file"]: m for m in project_map["modules"]}
    for module in project_map["modules"]:
        module["ran"] = False
        module["ran_functions"] = []
        module["steps"] = 0

    calls = {}
    for step in steps:
        module = by_file.get(step.get("file"))
        if module is None:
            continue
        module["ran"] = True
        module["steps"] += 1
        func = step.get("func")
        if func and func != "<module>" and func not in module["ran_functions"]:
            module["ran_functions"].append(func)

        # A call edge is the file of the caller frame -> file of the callee.
        if step.get("event") == "call":
            stack = step.get("stack") or []
            if len(stack) >= 2:
                caller = by_file.get(stack[-2].get("file"))
                if caller is not None and caller is not module:
                    key = (caller["name"], module["name"])
                    calls[key] = calls.get(key, 0) + 1

    project_map["calls"] = [{"from": a, "to": b, "count": n}
                            for (a, b), n in sorted(calls.items())]
    project_map["ran_count"] = sum(1 for m in project_map["modules"] if m["ran"])
    return project_map
