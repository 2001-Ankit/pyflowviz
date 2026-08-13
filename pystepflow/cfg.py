"""
Control-flow graphs for the visualizer's flowchart view.

Parses the source with ``ast`` and produces, for the module and for every
function in it, a laid-out flowchart: nodes with absolute x/y/width/height and
edges between them. The browser only has to draw what it is handed.

Layout is recursive and mirrors Python's own nesting: a block of statements is
a vertical column, an ``if`` fans out into two columns that merge again, a loop
puts its body under the test and routes a back-edge around the left.
"""

import ast

# Geometry, in CSS pixels. The UI scales the finished drawing to fit.
CHAR_W = 6.6
NODE_PAD = 20
NODE_H = 30
COND_H = 34
DOT = 11
V_GAP = 26
H_GAP = 34
BACK_MARGIN = 30
MIN_NODE_W = 74
MAX_LABEL = 42
MAX_STATEMENTS = 400          # graphs bigger than this are not worth drawing


def _node_width(label):
    return max(MIN_NODE_W, min(len(label), MAX_LABEL) * CHAR_W + NODE_PAD)


class Block:
    """A laid-out fragment: nodes positioned relative to the fragment's origin.

    ``spine`` is the x of the vertical line flow enters and leaves on, ``entry``
    the node flow arrives at, and ``exits`` the nodes flow leaves from.
    """

    def __init__(self, nodes=None, edges=None, width=0.0, height=0.0,
                 spine=0.0, entry=None, exits=None):
        self.nodes = nodes or []
        self.edges = edges or []
        self.width = width
        self.height = height
        self.spine = spine
        self.entry = entry
        self.exits = exits if exits is not None else []

    def translate(self, dx, dy):
        for node in self.nodes:
            node["x"] += dx
            node["y"] += dy
        self.spine += dx
        return self


class GraphBuilder:
    def __init__(self, source_lines):
        self.lines = source_lines
        self.nodes = {}
        self._next_id = 1
        self.statements = 0

    # ---- helpers -----------------------------------------------------

    def _text(self, node):
        """The user's own source for this statement, trimmed to one line."""
        index = getattr(node, "lineno", 0) - 1
        if 0 <= index < len(self.lines):
            text = self.lines[index].strip()
        else:
            try:
                text = ast.unparse(node)
            except Exception:
                text = type(node).__name__
        text = text.rstrip(":")
        if len(text) > MAX_LABEL:
            text = text[:MAX_LABEL - 1] + "…"
        return text

    def _calls(self, node):
        """Names called by this statement, so the UI can offer a jump."""
        found = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = child.func
                name = None
                if isinstance(target, ast.Name):
                    name = target.id
                elif isinstance(target, ast.Attribute):
                    name = target.attr
                if name and name not in found:
                    found.append(name)
        return found[:4]

    def _make(self, kind, label, line, calls=None, height=None):
        node_id = self._next_id
        self._next_id += 1
        width = DOT if kind == "merge" else _node_width(label)
        self.nodes[node_id] = {
            "id": node_id, "kind": kind, "label": label, "line": line,
            "x": 0.0, "y": 0.0,
            "w": width,
            "h": height or (DOT if kind == "merge" else NODE_H),
            "calls": calls or [],
        }
        return node_id

    def _single(self, kind, label, line, calls=None, terminal=False):
        """A one-node block centred on its own spine."""
        node_id = self._make(kind, label, line, calls)
        node = self.nodes[node_id]
        node["x"] = 0.0
        node["y"] = 0.0
        return Block(nodes=[node], edges=[], width=node["w"], height=node["h"],
                     spine=node["w"] / 2, entry=node_id,
                     exits=[] if terminal else [node_id])

    @staticmethod
    def _edge(src, dst, kind="seq", label=""):
        return {"from": src, "to": dst, "kind": kind, "label": label}

    # ---- layout ------------------------------------------------------

    def layout_body(self, statements):
        blocks = [self.layout_statement(s) for s in statements]
        blocks = [b for b in blocks if b.nodes]
        if not blocks:
            return Block()

        spine = max(b.spine for b in blocks)
        nodes, edges = [], []
        y = 0.0
        width = 0.0
        entry = None
        pending_exits = None

        for block in blocks:
            block.translate(spine - block.spine, y)
            nodes.extend(block.nodes)
            edges.extend(block.edges)

            if entry is None:
                entry = block.entry
            if pending_exits and block.entry is not None:
                for exit_id in pending_exits:
                    edges.append(self._edge(exit_id, block.entry))

            if block.entry is not None:
                pending_exits = block.exits
            width = max(width, spine - block.spine + block.width)
            y += block.height + V_GAP

        return Block(nodes, edges, width, y - V_GAP, spine, entry,
                     pending_exits or [])

    def layout_statement(self, statement):
        self.statements += 1

        if isinstance(statement, ast.If):
            return self._layout_if(statement)
        if isinstance(statement, (ast.While, ast.For, ast.AsyncFor)):
            return self._layout_loop(statement)
        if isinstance(statement, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            return self._layout_try(statement)
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            return self._layout_with(statement)
        if isinstance(statement, ast.Return):
            return self._single("return", self._text(statement), statement.lineno,
                                self._calls(statement), terminal=True)
        if isinstance(statement, ast.Raise):
            return self._single("raise", self._text(statement), statement.lineno,
                                terminal=True)
        if isinstance(statement, (ast.Break, ast.Continue)):
            kind = "break" if isinstance(statement, ast.Break) else "continue"
            return self._single(kind, kind, statement.lineno, terminal=True)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Definitions get their own graph; here they are just one box.
            word = "class" if isinstance(statement, ast.ClassDef) else "def"
            return self._single("def", word + " " + statement.name, statement.lineno)

        return self._single("stmt", self._text(statement), statement.lineno,
                            self._calls(statement))

    def _layout_if(self, statement):
        label = self._text(statement)
        cond = self._make("cond", label, statement.lineno, self._calls(statement),
                          height=COND_H)
        cond_node = self.nodes[cond]

        yes = self.layout_body(statement.body)
        no = self.layout_body(statement.orelse)

        yes_w = yes.width or MIN_NODE_W * 0.6
        no_w = no.width or MIN_NODE_W * 0.6

        yes.translate(0, cond_node["h"] + V_GAP)
        no.translate(yes_w + H_GAP, cond_node["h"] + V_GAP)

        total_w = yes_w + H_GAP + no_w
        spine = yes_w + H_GAP / 2
        cond_node["x"] = spine - cond_node["w"] / 2
        cond_node["y"] = 0.0

        branch_h = max(yes.height, no.height)
        merge_y = cond_node["h"] + V_GAP + (branch_h + V_GAP if branch_h else 0)
        merge = self._make("merge", "", statement.lineno)
        merge_node = self.nodes[merge]
        merge_node["x"] = spine - merge_node["w"] / 2
        merge_node["y"] = merge_y

        nodes = [cond_node] + yes.nodes + no.nodes + [merge_node]
        edges = yes.edges + no.edges

        edges.append(self._edge(cond, yes.entry if yes.entry else merge,
                                "true", "True"))
        edges.append(self._edge(cond, no.entry if no.entry else merge,
                                "false", "False"))
        for exit_id in yes.exits:
            edges.append(self._edge(exit_id, merge))
        for exit_id in no.exits:
            edges.append(self._edge(exit_id, merge))

        # If both branches ended in return/raise, nothing reaches the merge.
        if not any(e["to"] == merge for e in edges):
            nodes.remove(merge_node)
            del self.nodes[merge]
            height = merge_y - V_GAP
            return Block(nodes, edges, total_w, height, spine, cond, [])

        left = min(n["x"] for n in nodes)
        if left < 0:
            for node in nodes:
                node["x"] -= left
            spine -= left
            total_w -= left

        height = merge_y + merge_node["h"]
        total_w = max(total_w, max(n["x"] + n["w"] for n in nodes))
        return Block(nodes, edges, total_w, height, spine, cond, [merge])

    def _layout_loop(self, statement):
        label = self._text(statement)
        head = self._make("loop", label, statement.lineno, self._calls(statement),
                          height=COND_H)
        head_node = self.nodes[head]

        body = self.layout_body(statement.body)
        after = self.layout_body(statement.orelse)

        spine = BACK_MARGIN + max(head_node["w"] / 2, body.spine)
        head_node["x"] = spine - head_node["w"] / 2
        head_node["y"] = 0.0

        body.translate(spine - body.spine, head_node["h"] + V_GAP)

        exit_y = head_node["h"] + V_GAP + (body.height + V_GAP if body.height else 0)
        exit_dot = self._make("merge", "", statement.lineno)
        exit_node = self.nodes[exit_dot]
        exit_node["x"] = spine - exit_node["w"] / 2
        exit_node["y"] = exit_y

        nodes = [head_node] + body.nodes + [exit_node]
        edges = list(body.edges)

        if body.entry is not None:
            edges.append(self._edge(head, body.entry, "true", "each"))
            for exit_id in body.exits:
                edges.append(self._edge(exit_id, head, "back"))
        edges.append(self._edge(head, exit_dot, "false", "done"))

        block = Block(nodes, edges,
                      max(spine + head_node["w"] / 2,
                          max(n["x"] + n["w"] for n in nodes)),
                      exit_y + exit_node["h"], spine, head, [exit_dot])

        if after.nodes:                       # for/while ... else
            after.translate(spine - after.spine, block.height + V_GAP)
            block.nodes.extend(after.nodes)
            block.edges.extend(after.edges)
            if after.entry is not None:
                block.edges.append(self._edge(exit_dot, after.entry))
                block.exits = after.exits
            block.height += V_GAP + after.height
            block.width = max(block.width, max(n["x"] + n["w"] for n in block.nodes))

        return block

    def _layout_with(self, statement):
        head = self._single("stmt", self._text(statement), statement.lineno)
        body = self.layout_body(statement.body)
        return self._stack(head, body)

    def _layout_try(self, statement):
        head = self._single("stmt", "try", statement.lineno)
        body = self.layout_body(statement.body)
        block = self._stack(head, body)

        handlers = getattr(statement, "handlers", [])
        for handler in handlers:
            label = self._text(handler) if getattr(handler, "lineno", None) else "except"
            branch = self._single("cond", label, getattr(handler, "lineno", statement.lineno))
            inner = self.layout_body(handler.body)
            branch = self._stack(branch, inner)
            branch.translate(block.width + H_GAP, 0)
            block.edges.append(self._edge(head.entry, branch.entry, "false", "raises"))
            block.nodes.extend(branch.nodes)
            block.edges.extend(branch.edges)
            block.exits = block.exits + branch.exits
            block.width = max(n["x"] + n["w"] for n in block.nodes)
            block.height = max(block.height, branch.height)

        for extra in (getattr(statement, "orelse", []), getattr(statement, "finalbody", [])):
            if extra:
                tail = self.layout_body(extra)
                block = self._stack(block, tail)
        return block

    def _stack(self, top, bottom):
        """Put ``bottom`` directly under ``top``, aligned on the shared spine."""
        if not bottom.nodes:
            return top
        if not top.nodes:
            return bottom
        spine = max(top.spine, bottom.spine)
        top.translate(spine - top.spine, 0)
        bottom.translate(spine - bottom.spine, top.height + V_GAP)
        edges = top.edges + bottom.edges
        for exit_id in top.exits:
            if bottom.entry is not None:
                edges.append(self._edge(exit_id, bottom.entry))
        nodes = top.nodes + bottom.nodes
        return Block(nodes, edges, max(n["x"] + n["w"] for n in nodes),
                     top.height + V_GAP + bottom.height, spine,
                     top.entry, bottom.exits)


def _build_one(name, statements, source_lines, signature, first_line, last_line):
    builder = GraphBuilder(source_lines)
    body = builder.layout_body(statements)
    if builder.statements > MAX_STATEMENTS or not body.nodes:
        if not body.nodes:
            return None
        return None

    start = builder._single("start", signature, first_line)
    end = builder._single("end", "end", last_line)

    block = builder._stack(builder._stack(start, body), end)

    pad = 16
    for node in block.nodes:
        node["x"] += pad
        node["y"] += pad

    return {
        "name": name,
        "signature": signature,
        "first_line": first_line,
        "last_line": last_line,
        "width": block.width + pad * 2,
        "height": block.height + pad * 2,
        "nodes": block.nodes,
        "edges": block.edges,
    }


def _end_line(node, default):
    return getattr(node, "end_lineno", None) or default


def build_graphs(source):
    """Return a list of flowcharts: one for the module, one per function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.split("\n")
    graphs = []

    module_body = [s for s in tree.body]
    module = _build_one("<module>", module_body, lines, "start", 1, len(lines))
    if module:
        graphs.append(module)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = ""
        signature = "%s(%s)" % (node.name, args)
        if len(signature) > MAX_LABEL:
            signature = signature[:MAX_LABEL - 1] + "…"
        graph = _build_one(node.name, node.body, lines, signature,
                           node.lineno, _end_line(node, node.lineno))
        if graph:
            graphs.append(graph)

    return graphs
