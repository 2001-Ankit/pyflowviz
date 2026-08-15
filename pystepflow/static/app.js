/* Python Code Visualizer — UI logic.
 *
 * The server hands back a list of *steps*. Each step is a complete snapshot of
 * the program: which line is about to run, the call stack, every variable, and
 * the heap objects those variables reference. Everything below is just
 * rendering one step and letting you move between them.
 */

(function () {
"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  editor: $("editor"), codeView: $("codeView"), gutter: $("gutter"),
  codeWrap: $("codeWrap"), codeStatus: $("codeStatus"), filename: $("filename"),
  stdinBox: $("stdinBox"), stdout: $("stdout"), outMeta: $("outMeta"),
  frames: $("frames"), heap: $("heap"), arrows: $("arrows"),
  stateMeta: $("stateMeta"), flow: $("flow"), flowMeta: $("flowMeta"),
  runBtn: $("runBtn"), runLabel: $("runLabel"), editBtn: $("editBtn"),
  fileSelect: $("fileSelect"), projectToggle: $("projectToggle"), projectChk: $("projectChk"),
  fileInput: $("fileInput"), exampleSelect: $("exampleSelect"),
  timeline: $("timeline"), counter: $("counter"), speedSelect: $("speedSelect"),
  playBtn: $("playBtn"), toast: $("toast"),
  map: $("map"), mapTab: $("mapTab"), mapLegend: $("mapLegend"),
  graph: $("graph"), graphSelect: $("graphSelect"), graphToolbar: $("graphToolbar"),
  followChk: $("followChk"), zoomIn: $("zoomIn"), zoomOut: $("zoomOut"), zoomFit: $("zoomFit"),
  nav: {
    first: $("firstBtn"), prev: $("prevBtn"), next: $("nextBtn"),
    last: $("lastBtn"), over: $("overBtn"), out: $("outBtn")
  }
};

const state = {
  trace: null,      // the whole server response
  steps: [],
  index: 0,
  playing: false,
  timer: null,
  mode: "edit",     // "edit" | "view"
  flowNodes: [],
  seenObjects: new Set(),  // heap ids already rendered, to animate new ones
  graphs: [],
  tab: "graph",     // "graph" | "tree"
  zoom: null,       // null means "fit to the panel"
  pinnedGraph: null,// index into state.graphs when "follow" is off

  // Project mode: several files take part in one run, so the code panel shows
  // whichever file is executing unless the user pins one.
  sources: {},      // file key -> source text
  files: [],
  shownFile: null,  // the file currently rendered
  pinnedFile: null, // set when the user picks from the dropdown
  builtFiles: {}    // file key -> rendered HTML, so we tokenize each file once
};

/* ================================================================== *
 * Syntax highlighting — a deliberately small Python tokenizer.
 * ================================================================== */

const KEYWORDS = new Set(("False None True and as assert async await break class " +
  "continue def del elif else except finally for from global if import in is " +
  "lambda nonlocal not or pass raise return try while with yield match case")
  .split(" "));

const BUILTINS = new Set(("abs all any bool bytes callable chr dict dir divmod " +
  "enumerate filter float format frozenset getattr hasattr hash hex id input int " +
  "isinstance issubclass iter len list map max min next object oct open ord pow " +
  "print range repr reversed round set setattr slice sorted str sum tuple type zip " +
  "self cls super Exception ValueError TypeError KeyError IndexError")
  .split(" "));

function esc(text) {
  return text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function span(cls, text) { return '<span class="' + cls + '">' + esc(text) + "</span>"; }

function tokenizeLine(line, ctx) {
  let out = "", i = 0, prevWord = "";

  if (ctx.triple) {                       // continuing a multi-line string
    const end = line.indexOf(ctx.triple);
    if (end === -1) return span("tok-str", line);
    out += span("tok-str", line.slice(0, end + 3));
    i = end + 3;
    ctx.triple = null;
  }

  while (i < line.length) {
    const ch = line[i];

    if (ch === "#") { out += span("tok-com", line.slice(i)); break; }

    if (ch === '"' || ch === "'") {
      const triple = ch + ch + ch;
      if (line.substr(i, 3) === triple) {
        const end = line.indexOf(triple, i + 3);
        if (end === -1) { out += span("tok-str", line.slice(i)); ctx.triple = triple; break; }
        out += span("tok-str", line.slice(i, end + 3));
        i = end + 3;
        continue;
      }
      let j = i + 1;
      while (j < line.length) {
        if (line[j] === "\\") { j += 2; continue; }
        if (line[j] === ch) { j++; break; }
        j++;
      }
      out += span("tok-str", line.slice(i, j));
      i = j;
      continue;
    }

    if (/[0-9]/.test(ch) && !/[A-Za-z0-9_]/.test(line[i - 1] || "")) {
      let j = i;
      while (j < line.length && /[0-9a-fA-FxXoObB_.jJ]/.test(line[j])) j++;
      out += span("tok-num", line.slice(i, j));
      i = j;
      continue;
    }

    if (/[A-Za-z_]/.test(ch)) {
      let j = i;
      while (j < line.length && /[A-Za-z0-9_]/.test(line[j])) j++;
      const word = line.slice(i, j);
      if (prevWord === "def" || prevWord === "class") out += span("tok-def", word);
      else if (KEYWORDS.has(word)) out += span("tok-kw", word);
      else if (BUILTINS.has(word)) out += span("tok-bi", word);
      else out += esc(word);
      prevWord = word;
      i = j;
      continue;
    }

    out += esc(ch);
    i++;
  }
  return out;
}

/* ================================================================== *
 * Editor
 * ================================================================== */

function syncGutter() {
  const count = el.editor.value.split("\n").length;
  const numbers = [];
  for (let n = 1; n <= count; n++) numbers.push(n);
  el.gutter.textContent = numbers.join("\n");
  el.gutter.style.transform = "translateY(" + -el.editor.scrollTop + "px)";
}

function setMode(mode) {
  state.mode = mode;
  const editing = mode === "edit";
  el.editor.hidden = !editing;
  el.codeView.hidden = editing;
  el.gutter.hidden = !editing;
  el.codeStatus.textContent = editing ? "editing" : "stepping — press Edit to change the code";
  if (editing) { stopPlaying(); el.editor.focus(); }
}

function renderSource(source) {
  const ctx = { triple: null };
  return source.split("\n").map((line, i) =>
    '<div class="code-line" data-line="' + (i + 1) + '">' +
      '<span class="ln">' + (i + 1) + "</span>" +
      '<span class="src">' + (tokenizeLine(line, ctx) || "&nbsp;") + "</span>" +
    "</div>"
  ).join("");
}

/** Show a file in the code panel, tokenizing it only the first time. */
function showFile(file) {
  if (!file || state.shownFile === file) return;
  if (state.builtFiles[file] === undefined) {
    state.builtFiles[file] = renderSource(state.sources[file] || "");
  }
  el.codeView.innerHTML = state.builtFiles[file];
  state.shownFile = file;
  if (el.fileSelect.value !== file) el.fileSelect.value = file;
}

function fillFileSelect() {
  const many = state.files.length > 1;
  el.fileSelect.hidden = !many;
  if (!many) return;
  el.fileSelect.innerHTML = state.files.map((f) =>
    '<option value="' + esc(f) + '">' + esc(f) + "</option>").join("");
}

/** Which file the code panel should display for this step. */
function fileForStep(step) {
  if (state.pinnedFile) return state.pinnedFile;
  return (step && step.file) || state.files[0] || null;
}

/* ================================================================== *
 * Value rendering
 * ================================================================== */

function summarize(obj) {
  if (!obj) return "?";
  switch (obj.kind) {
    case "list":     return "list[" + obj.size + "]";
    case "tuple":    return "tuple[" + obj.size + "]";
    case "set":      return "set{" + obj.size + "}";
    case "dict":     return "dict{" + obj.size + "}";
    case "instance": return obj.name;
    case "function": return "ƒ " + obj.name;
    case "class":    return "class " + obj.name;
    case "module":   return "module " + obj.name;
    default:         return obj.type || "object";
  }
}

function renderValue(value, heap) {
  if (!value) return '<span class="val val-other">?</span>';

  if (value.t === "prim") {
    const v = value.v;
    let cls = "val-other";
    if (v === null) cls = "val-none";
    else if (typeof v === "boolean") cls = "val-bool";
    else if (typeof v === "number") cls = "val-num";
    else if (typeof v === "string") cls = value.d && value.d[0] !== "<" ? "val-str" : "val-other";
    let text = value.d;
    if (v === null) text = "None";
    else if (typeof v === "boolean") text = v ? "True" : "False";
    return '<span class="val ' + cls + '">' + esc(String(text)) + "</span>";
  }

  const target = heap[value.id];
  return '<span class="ref-chip" data-target="' + value.id + '">' +
         esc(summarize(target)) + "</span>";
}

function renderHeapObject(obj, heap) {
  const isNew = !state.seenObjects.has(obj.id);
  state.seenObjects.add(obj.id);

  let body = "";
  if (obj.kind === "list" || obj.kind === "tuple" || obj.kind === "set") {
    if (!obj.items.length) {
      body = '<div class="heap-note">empty</div>';
    } else {
      body = '<div class="seq">' + obj.items.map((item, i) =>
        '<div class="cell">' +
          (obj.kind === "set" ? "" : '<span class="idx">' + i + "</span>") +
          renderValue(item, heap) +
        "</div>"
      ).join("") + "</div>";
    }
  } else if (obj.kind === "dict") {
    body = obj.items.length
      ? obj.items.map(([k, v]) =>
          '<div class="map-row"><span class="map-key">' + renderValue(k, heap) +
          "</span><span>" + renderValue(v, heap) + "</span></div>").join("")
      : '<div class="heap-note">empty</div>';
  } else if (obj.kind === "instance") {
    body = obj.items.length
      ? obj.items.map(([k, v]) =>
          '<div class="map-row"><span class="map-key">' + esc(k) +
          "</span><span>" + renderValue(v, heap) + "</span></div>").join("")
      : '<div class="heap-note">no attributes</div>';
  } else if (obj.kind === "function" || obj.kind === "class" || obj.kind === "module") {
    body = '<div class="heap-repr">' + esc(obj.name) + "</div>";
  } else {
    body = '<div class="heap-repr">' + esc(obj.repr || obj.type) + "</div>";
  }

  if (obj.truncated) body += '<div class="heap-note">… showing first 200 of ' + obj.size + "</div>";

  return '<div class="heap-obj' + (isNew ? " new" : "") + '" data-id="' + obj.id + '">' +
    '<div class="heap-head"><span class="kind">' + esc(summarize(obj)) +
    '</span><span>#' + obj.id + "</span></div>" + body + "</div>";
}

/* ================================================================== *
 * Rendering one step
 * ================================================================== */

function valueKey(value) {
  return value.t === "prim" ? "p:" + value.d : "r:" + value.id;
}

function previousVarsFor(fid) {
  if (state.index === 0) return null;
  const prev = state.steps[state.index - 1];
  const frame = prev.stack.find((f) => f.fid === fid);
  if (!frame) return null;
  const map = {};
  frame.vars.forEach((v) => { map[v.name] = valueKey(v.value); });
  return map;
}

function renderFrames(step) {
  const html = step.stack.map((frame, i) => {
    const active = i === step.stack.length - 1;
    const before = previousVarsFor(frame.fid);
    const rows = frame.vars.length
      ? frame.vars.map((v) => {
          const key = valueKey(v.value);
          const changed = before && before[v.name] !== undefined && before[v.name] !== key;
          const added = before && before[v.name] === undefined;
          return '<div class="var-row' + (changed || added ? " changed" : "") + '">' +
            '<span class="var-name">' + esc(v.name) + "</span>" +
            '<span class="var-value">' + renderValue(v.value, step.heap) + "</span></div>";
        }).join("")
      : '<div class="empty-note">no variables yet</div>';

    // In a multi-file run, say which module each frame lives in.
    const shown = state.shownFile;
    const foreign = state.files.length > 1 && frame.file && frame.file !== shown;
    const where = state.files.length > 1 && frame.file
      ? '<span class="frame-file" title="' + esc(frame.file) + '">' + esc(frame.file) + "</span>"
      : "";

    return '<div class="frame' + (active ? " active" : "") +
      (foreign ? " foreign" : "") + '">' +
      '<div class="frame-head"><span>' + esc(frame.func) +
      (frame.is_global ? "" : "()") + "</span>" + where +
      '<span class="at">line ' + frame.line + "</span></div>" + rows + "</div>";
  });

  // Innermost frame on top reads better next to the stack metaphor.
  el.frames.innerHTML = html.reverse().join("");
}

function renderHeap(step) {
  const ids = Object.keys(step.heap).map(Number).sort((a, b) => a - b);
  el.heap.innerHTML = ids.length
    ? ids.map((id) => renderHeapObject(step.heap[id], step.heap)).join("")
    : '<div class="empty-note">no objects yet</div>';
}

function renderOutput(step) {
  const all = state.trace.stdout || "";
  const shown = step ? all.slice(0, step.out) : all;
  let html = esc(shown);

  const atEnd = !step || state.index === state.steps.length - 1;
  const error = state.trace.error;
  if (atEnd && error) {
    html += '\n<span class="err">' + esc(
      (error.traceback || (error.type + ": " + error.message)) +
      (error.line ? "  (line " + error.line + ")" : "")
    ) + "</span>";
  }
  el.stdout.innerHTML = html;
  el.stdout.scrollTop = el.stdout.scrollHeight;
  el.outMeta.textContent = shown.length ? "(" + shown.length + " chars so far)" : "";
}

function highlightCode(step) {
  const file = fileForStep(step);
  showFile(file);

  const lines = el.codeView.querySelectorAll(".code-line");
  lines.forEach((node) => { node.className = "code-line"; });
  if (!step) return;

  // Lines of the frames that are waiting further up the stack — but only those
  // belonging to the file on screen, since a stack can now span modules.
  step.stack.slice(0, -1).forEach((frame) => {
    if ((frame.file || file) !== file) return;
    const node = el.codeView.querySelector('.code-line[data-line="' + frame.line + '"]');
    if (node) node.classList.add("parent");
  });

  // The executing line only belongs on screen when we are showing its file.
  if ((step.file || file) !== file) return;

  const current = el.codeView.querySelector('.code-line[data-line="' + step.line + '"]');
  if (!current) return;
  current.classList.add("current");
  if (step.event === "return") current.classList.add("returning");
  if (step.event === "exception") current.classList.add("error");

  const box = current.getBoundingClientRect();
  const frame = el.codeWrap.getBoundingClientRect();
  if (box.top < frame.top + 30 || box.bottom > frame.bottom - 30) {
    current.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

/* --- arrows from a reference to the object it points at --------------- */

function drawArrows() {
  const container = el.arrows.parentElement;
  const base = container.getBoundingClientRect();
  const dx = container.scrollLeft - base.left;
  const dy = container.scrollTop - base.top;

  const paths = [];
  container.querySelectorAll(".ref-chip").forEach((chip) => {
    const id = chip.getAttribute("data-target");
    const target = container.querySelector('.heap-obj[data-id="' + id + '"]');
    if (!target) return;

    const a = chip.getBoundingClientRect();
    const b = target.getBoundingClientRect();
    const x1 = a.right + dx, y1 = a.top + a.height / 2 + dy;
    const x2 = b.left + dx - 4, y2 = b.top + 11 + dy;
    const bend = Math.max(18, Math.min(70, Math.abs(x2 - x1) / 2));

    paths.push(
      '<path d="M ' + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " +
      (x2 - bend) + " " + y2 + ", " + x2 + " " + y2 +
      '" data-ref="' + id + '" fill="none" stroke="rgba(77,159,255,0.42)" ' +
      'stroke-width="1.2" marker-end="url(#arrowhead)"/>'
    );
  });

  el.arrows.innerHTML =
    '<defs><marker id="arrowhead" markerWidth="7" markerHeight="7" refX="6" refY="2.6" ' +
    'orient="auto"><path d="M0,0 L6,2.6 L0,5.2 z" fill="rgba(77,159,255,0.75)"/></marker></defs>' +
    paths.join("");
}

/* ================================================================== *
 * Flow graph — the flowchart of the function we are standing in.
 * ================================================================== */

function currentGraph(step) {
  if (!state.graphs.length) return null;
  if (!el.followChk.checked && state.pinnedGraph !== null) {
    return state.graphs[state.pinnedGraph] || null;
  }
  return window.FlowGraph.pickGraph(state.graphs, step);
}

function fillGraphSelect() {
  const many = state.files.length > 1;
  el.graphSelect.innerHTML = state.graphs.map((g, i) =>
    '<option value="' + i + '">' +
    (many && g.file ? esc(g.file) + " · " : "") +
    esc(g.signature) + "</option>"
  ).join("");
}

function renderGraph(step) {
  if (state.tab !== "graph") return;
  if (!state.graphs.length) {
    el.graph.innerHTML = '<div class="graph-empty">No flow graph available ' +
      "(the program may be too large to chart).</div>";
    return;
  }

  const graph = currentGraph(step);
  if (!graph) { el.graph.innerHTML = '<div class="graph-empty">No flow graph.</div>'; return; }

  const index = state.graphs.indexOf(graph);
  if (el.graphSelect.value !== String(index)) el.graphSelect.value = String(index);

  // Only overlay execution when the drawn graph is the one actually running.
  const live = graph === window.FlowGraph.pickGraph(state.graphs, step);
  const exec = live
    ? window.FlowGraph.computeExecution(graph, state.steps, state.index)
    : { counts: new Map(), edges: new Set(), current: null };

  const zoom = state.zoom || window.FlowGraph.fitZoom(graph, el.graph.clientWidth);
  el.graph.innerHTML = window.FlowGraph.render(graph, exec, zoom);

  const current = el.graph.querySelector(".gnode.current");
  if (current) {
    const box = current.getBoundingClientRect();
    const frame = el.graph.getBoundingClientRect();
    if (box.top < frame.top || box.bottom > frame.bottom) {
      current.closest(".gnode-group").scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

/* ================================================================== *
 * Project map — every module, and what this run touched.
 * ================================================================== */

function renderMap() {
  if (state.tab !== "map") return;
  const step = state.steps[state.index];
  const map = state.trace && state.trace.map;
  const zoom = state.zoom ||
    (map ? window.FlowGraph.fitZoom(map, el.map.clientWidth) : 1);
  el.map.innerHTML = window.FlowGraph.renderMap(map, fileForStep(step), zoom);
}

/**
 * Open a module from the map. Files that never ran have no source in the
 * trace, so fetch them from the workspace on demand — being able to read code
 * that did not execute is most of the point of the map.
 */
async function openModuleFile(file) {
  if (!file) return;
  if (state.sources[file] === undefined) {
    try {
      const response = await fetch("/api/open?path=" + encodeURIComponent(file));
      const data = await response.json();
      if (!data || typeof data.code !== "string") {
        return toast((data && data.message) || "Could not open " + file, true);
      }
      state.sources[file] = data.code;
      if (!state.files.includes(file)) {
        state.files = state.files.concat([file]).sort();
        fillFileSelect();
      }
    } catch (err) {
      return toast("Could not open " + file + ": " + err.message, true);
    }
  }
  state.pinnedFile = file;
  showFile(file);
  el.fileSelect.value = file;
  toast(file + " — pinned. Pick the executing file again to resume following.");
}

/** The panel header describes whichever view is showing. */
function updateMeta() {
  const map = state.trace && state.trace.map;
  if (state.tab === "map" && map) {
    const dead = map.modules.length - map.ran_count;
    el.flowMeta.textContent = map.modules.length + " modules · " +
      map.ran_count + " ran" + (dead ? " · " + dead + " did not" : "");
  } else if (state.flowCalls !== undefined) {
    el.flowMeta.textContent = state.flowCalls +
      " call" + (state.flowCalls === 1 ? "" : "s");
  }
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((node) =>
    node.classList.toggle("active", node.dataset.tab === tab));
  el.graph.hidden = tab !== "graph";
  el.graphToolbar.hidden = tab !== "graph";
  el.flow.hidden = tab !== "tree";
  el.map.hidden = tab !== "map";
  el.mapLegend.hidden = tab !== "map";
  state.zoom = null;                       // each view fits itself
  updateMeta();
  if (state.steps.length) renderStep();
  else if (tab === "map") renderMap();
}

function setZoom(delta) {
  const step = state.steps[state.index];
  const graph = currentGraph(step);
  const base = state.zoom || window.FlowGraph.fitZoom(graph, el.graph.clientWidth);
  state.zoom = Math.max(0.3, Math.min(2.5, base + delta));
  renderGraph(step);
}

function renderStep() {
  const step = state.steps[state.index];
  if (!step) { renderOutput(null); return; }

  highlightCode(step);
  renderFrames(step);
  renderHeap(step);
  renderOutput(step);
  renderFlow();
  renderGraph(step);
  renderMap();
  drawArrows();

  el.counter.textContent = (state.index + 1) + " / " + state.steps.length;
  el.timeline.value = state.index;

  const verb = { call: "entering", return: "returning from", exception: "exception in", line: "in" }[step.event];
  el.stateMeta.textContent = verb + " " + step.func + "() · line " + step.line;

  el.nav.first.disabled = el.nav.prev.disabled = state.index === 0;
  const atEnd = state.index >= state.steps.length - 1;
  el.nav.last.disabled = el.nav.next.disabled = el.nav.over.disabled = el.nav.out.disabled = atEnd;
}

/* ================================================================== *
 * Call-flow tree
 * ================================================================== */

const MAX_FLOW_NODES = 800;

function buildFlow() {
  const root = { func: "<module>", args: "", start: 0, end: state.steps.length - 1,
                 depth: 0, children: [], retval: null };
  const nodes = [root];
  const stack = [root];
  let truncated = false;

  state.steps.forEach((step, i) => {
    if (step.event === "call") {
      if (nodes.length >= MAX_FLOW_NODES) { truncated = true; return; }
      const frame = step.stack[step.stack.length - 1];
      const args = frame.vars.slice(0, 3)
        .map((v) => v.name + "=" + (v.value.t === "prim" ? v.value.d : "…")).join(", ");
      const node = { func: step.func, args, start: i, end: state.steps.length - 1,
                     depth: stack.length, children: [], retval: null };
      stack[stack.length - 1].children.push(node);
      stack.push(node);
      nodes.push(node);
    } else if (step.event === "return" && stack.length > 1) {
      const node = stack.pop();
      node.end = i;
      node.retval = step.retval;
    }
  });

  state.flowNodes = nodes;
  state.flowCalls = nodes.length - 1;
  el.flowMeta.textContent = state.flowCalls + " call" +
    (state.flowCalls === 1 ? "" : "s") + (truncated ? " (truncated)" : "");

  const html = [];
  (function walk(node) {
    const ret = node.retval && node.retval.t === "prim" ? " → " + node.retval.d : "";
    html.push(
      '<div class="flow-node" data-start="' + node.start + '" data-end="' + node.end +
      '" style="margin-left:' + node.depth * 11 + 'px">' +
      "<span>" + esc(node.func) + (node.depth ? "(" + esc(node.args) + ")" : "") + "</span>" +
      (ret ? '<span class="ret">' + esc(ret) + "</span>" : "") +
      "</div>"
    );
    node.children.forEach(walk);
  })(root);
  el.flow.innerHTML = html.join("");
}

function renderFlow() {
  if (state.tab !== "tree") return;
  const i = state.index;
  let active = null;
  el.flow.querySelectorAll(".flow-node").forEach((node) => {
    const start = +node.dataset.start, end = +node.dataset.end;
    node.classList.remove("active", "done");
    if (i >= start && i <= end) { node.classList.add("active"); active = node; }
    else if (i > end) node.classList.add("done");
  });
  if (active) {
    const box = active.getBoundingClientRect(), frame = el.flow.getBoundingClientRect();
    if (box.top < frame.top || box.bottom > frame.bottom) {
      active.scrollIntoView({ block: "nearest" });
    }
  }
}

/* ================================================================== *
 * Navigation
 * ================================================================== */

function goTo(index) {
  if (!state.steps.length) return;
  state.index = Math.max(0, Math.min(state.steps.length - 1, index));
  renderStep();
}

function stepBy(delta) { stopPlaying(); goTo(state.index + delta); }

/**
 * Jump to a step that ran the given source line. Prefers the next occurrence
 * inside the invocation we are already looking at, so clicking a box inside a
 * loop walks forward through its iterations.
 */
function jumpToLine(line) {
  const step = state.steps[state.index];
  const frame = step.stack[step.stack.length - 1];
  const fid = frame ? frame.fid : 0;

  const file = state.shownFile;
  const matches = (other, sameFrame) => {
    if (other.line !== line) return false;
    // A line number alone is ambiguous once several files are in play.
    if (file && other.file && other.file !== file) return false;
    if (!sameFrame) return true;
    const top = other.stack[other.stack.length - 1];
    return top && top.fid === fid;
  };

  for (const sameFrame of [true, false]) {
    for (let i = state.index + 1; i < state.steps.length; i++) {
      if (matches(state.steps[i], sameFrame)) return goTo(i);
    }
    for (let i = 0; i <= state.index; i++) {
      if (matches(state.steps[i], sameFrame)) return goTo(i);
    }
  }
  toast("Line " + line + " never ran.");
}

/** Run to the next step at the same depth or shallower — i.e. skip the call. */
function stepOver() {
  stopPlaying();
  const depth = state.steps[state.index].depth;
  for (let i = state.index + 1; i < state.steps.length; i++) {
    if (state.steps[i].depth <= depth) return goTo(i);
  }
  goTo(state.steps.length - 1);
}

/** Run until the current function has returned. */
function stepOut() {
  stopPlaying();
  const depth = state.steps[state.index].depth;
  for (let i = state.index + 1; i < state.steps.length; i++) {
    if (state.steps[i].depth < depth) return goTo(i);
  }
  goTo(state.steps.length - 1);
}

function stopPlaying() {
  state.playing = false;
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  el.playBtn.textContent = "▶";
}

function togglePlay() {
  if (state.playing) return stopPlaying();
  if (!state.steps.length) return;
  if (state.index >= state.steps.length - 1) state.index = 0;
  state.playing = true;
  el.playBtn.textContent = "❚❚";
  const tick = () => {
    if (state.index >= state.steps.length - 1) return stopPlaying();
    goTo(state.index + 1);
  };
  state.timer = setInterval(tick, +el.speedSelect.value);
}

/* ================================================================== *
 * Running a trace
 * ================================================================== */

function toast(message, bad) {
  el.toast.textContent = message;
  el.toast.classList.toggle("bad", !!bad);
  el.toast.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.toast.hidden = true; }, 6000);
}

async function run() {
  const code = el.editor.value;
  if (!code.trim()) return toast("Nothing to run — write some Python first.");

  stopPlaying();
  el.runBtn.disabled = true;
  el.runLabel.textContent = "Tracing…";

  try {
    const project = el.projectChk.checked;
    const response = await fetch("/api/trace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, stdin: el.stdinBox.value, project })
    });
    const result = await response.json();

    state.trace = result;
    state.steps = result.steps || [];
    state.index = 0;
    state.seenObjects = new Set();
    state.graphs = result.graphs || [];
    state.zoom = null;
    state.pinnedGraph = null;

    // In project mode the sources come back from disk, one per module that ran.
    state.sources = result.sources && Object.keys(result.sources).length
      ? result.sources : { "main.py": code };
    state.files = result.files && result.files.length
      ? result.files : Object.keys(state.sources);
    state.builtFiles = {};
    state.shownFile = null;
    state.pinnedFile = null;

    fillGraphSelect();
    fillFileSelect();
    showFile(result.entry || state.files[0]);

    // The project map only exists for a project-mode run.
    el.mapTab.hidden = !result.map;
    if (!result.map && state.tab === "map") setTab("graph");
    updateMeta();

    if (!state.steps.length) {
      setMode("edit");
      el.frames.innerHTML = el.heap.innerHTML = "";
      el.arrows.innerHTML = "";
      el.flow.innerHTML = "";
      el.counter.textContent = "0 / 0";
      el.timeline.disabled = true;
      renderOutput(null);
      const error = result.error;
      toast(error ? error.type + ": " + error.message : "The program produced no steps.", true);
      if (error && error.line) toast(error.type + " on line " + error.line + ": " + error.message, true);
      return;
    }

    el.timeline.disabled = false;
    el.timeline.max = state.steps.length - 1;
    setMode("view");
    buildFlow();
    renderStep();

    if (result.error) {
      toast(result.error.type + ": " + result.error.message, true);
    } else if (result.mode === "project") {
      toast(state.steps.length + " steps across " + state.files.length +
            " files — the code panel follows execution between them.");
    } else {
      toast(state.steps.length + " steps traced — use ← → or press play.");
    }
  } catch (err) {
    toast("Could not reach the server: " + err.message, true);
  } finally {
    el.runBtn.disabled = false;
    el.runLabel.textContent = "Visualize";
  }
}

/* ================================================================== *
 * Wiring
 * ================================================================== */

function loadCode(code, name, stdin) {
  el.editor.value = code;
  if (name) el.filename.textContent = name;
  if (typeof stdin === "string") el.stdinBox.value = stdin;
  state.trace = null;
  state.steps = [];
  state.graphs = [];
  state.zoom = null;
  state.pinnedGraph = null;
  state.sources = {};
  state.files = [];
  state.builtFiles = {};
  state.shownFile = null;
  state.pinnedFile = null;
  setMode("edit");
  syncGutter();
  el.fileSelect.hidden = true;
  el.graphSelect.innerHTML = "";
  el.graph.innerHTML = '<div class="graph-empty">Press Visualize to chart the flow.</div>';
  el.frames.innerHTML = el.heap.innerHTML = el.flow.innerHTML = "";
  el.arrows.innerHTML = "";
  el.stdout.textContent = "";
  el.counter.textContent = "0 / 0";
  el.stateMeta.textContent = "not running";
  el.flowMeta.textContent = "";
  el.timeline.disabled = true;
  el.timeline.value = 0;
}

function wire() {
  el.runBtn.addEventListener("click", run);
  el.editBtn.addEventListener("click", () => setMode("edit"));

  // Picking a file pins it; picking the executing file again resumes following.
  el.fileSelect.addEventListener("change", () => {
    const step = state.steps[state.index];
    const executing = step && step.file;
    state.pinnedFile = el.fileSelect.value === executing ? null : el.fileSelect.value;
    if (state.steps.length) renderStep();
    else showFile(el.fileSelect.value);
  });

  el.projectChk.addEventListener("change", () => {
    el.codeStatus.textContent = el.projectChk.checked
      ? "project mode — runs the file from disk" : "editing";
    if (el.projectChk.checked) {
      toast("Project mode runs the file on disk, so edits in this panel are ignored.");
    }
  });

  el.editor.addEventListener("input", syncGutter);
  el.editor.addEventListener("scroll", syncGutter);

  // Tab inserts four spaces instead of leaving the textarea.
  el.editor.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const start = el.editor.selectionStart, end = el.editor.selectionEnd;
    el.editor.setRangeText("    ", start, end, "end");
    syncGutter();
  });

  el.nav.first.addEventListener("click", () => { stopPlaying(); goTo(0); });
  el.nav.last.addEventListener("click", () => { stopPlaying(); goTo(state.steps.length - 1); });
  el.nav.prev.addEventListener("click", () => stepBy(-1));
  el.nav.next.addEventListener("click", () => stepBy(1));
  el.nav.over.addEventListener("click", stepOver);
  el.nav.out.addEventListener("click", stepOut);
  el.playBtn.addEventListener("click", togglePlay);

  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => setTab(tab.dataset.tab)));

  el.graphSelect.addEventListener("change", () => {
    state.pinnedGraph = +el.graphSelect.value;
    el.followChk.checked = false;
    state.zoom = null;
    renderGraph(state.steps[state.index]);
  });
  el.followChk.addEventListener("change", () => {
    if (el.followChk.checked) state.pinnedGraph = null;
    state.zoom = null;
    renderGraph(state.steps[state.index]);
  });
  el.zoomIn.addEventListener("click", () => setZoom(0.2));
  el.zoomOut.addEventListener("click", () => setZoom(-0.2));
  el.zoomFit.addEventListener("click", () => {
    state.zoom = null;
    renderGraph(state.steps[state.index]);
  });

  el.timeline.addEventListener("input", () => { stopPlaying(); goTo(+el.timeline.value); });
  el.speedSelect.addEventListener("change", () => {
    if (state.playing) { stopPlaying(); togglePlay(); }
  });

  // Clicking a reference lights up the object it points at.
  document.addEventListener("mouseover", (event) => {
    const chip = event.target.closest(".ref-chip");
    if (!chip) return;
    const id = chip.getAttribute("data-target");
    document.querySelectorAll('.heap-obj[data-id="' + id + '"]').forEach((n) => n.classList.add("lit"));
    el.arrows.querySelectorAll('path[data-ref="' + id + '"]').forEach((p) => {
      p.setAttribute("stroke", "rgba(77,159,255,0.95)");
      p.setAttribute("stroke-width", "2");
    });
  });
  document.addEventListener("mouseout", (event) => {
    if (!event.target.closest(".ref-chip")) return;
    document.querySelectorAll(".heap-obj.lit").forEach((n) => n.classList.remove("lit"));
    el.arrows.querySelectorAll("path").forEach((p) => {
      p.setAttribute("stroke", "rgba(77,159,255,0.42)");
      p.setAttribute("stroke-width", "1.2");
    });
  });
  document.addEventListener("click", (event) => {
    const chip = event.target.closest(".ref-chip");
    if (chip) {
      const target = document.querySelector('.heap-obj[data-id="' + chip.dataset.target + '"]');
      if (target) target.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }
    const node = event.target.closest(".flow-node");
    if (node) { stopPlaying(); goTo(+node.dataset.start); return; }

    // Clicking a box in the flow graph jumps to when that line ran.
    const box = event.target.closest(".gnode-group");
    if (box) { stopPlaying(); jumpToLine(+box.dataset.line); return; }

    // Clicking a module in the project map opens its source.
    const module = event.target.closest(".mnode-group");
    if (module) { stopPlaying(); openModuleFile(module.dataset.file); }
  });

  el.fileInput.addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => loadCode(String(reader.result), file.name);
    reader.readAsText(file);
    event.target.value = "";
  });

  (window.EXAMPLES || []).forEach((example, i) => {
    const option = document.createElement("option");
    option.value = String(i);
    option.textContent = example.name;
    el.exampleSelect.appendChild(option);
  });
  el.exampleSelect.addEventListener("change", (event) => {
    const example = window.EXAMPLES[+event.target.value];
    if (!example) return;
    loadCode(example.code, example.name.split(" — ")[0] + ".py", example.stdin || "");
    if (example.note) toast(example.note);
    event.target.value = "";
  });

  document.addEventListener("keydown", (event) => {
    const typing = document.activeElement === el.editor || document.activeElement === el.stdinBox;

    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      return run();
    }
    if (event.key === "Escape") { setMode("edit"); return; }
    if (typing || state.mode !== "view") return;

    const actions = {
      ArrowRight: () => stepBy(1),
      ArrowLeft: () => stepBy(-1),
      ArrowDown: stepOver,
      ArrowUp: stepOut,
      Home: () => { stopPlaying(); goTo(0); },
      End: () => { stopPlaying(); goTo(state.steps.length - 1); },
      " ": togglePlay
    };
    if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.steps.length) return;
      drawArrows();
      if (!state.zoom) renderGraph(state.steps[state.index]);   // refit
    }, 120);
  });
}

async function boot() {
  wire();
  let loaded = false;
  try {
    const response = await fetch("/api/preload");
    const data = await response.json();
    if (data && data.code) {
      loadCode(data.code, data.name || "preloaded.py");
      loaded = true;
    }
    // The project toggle only makes sense with a real file on disk to run.
    if (data && data.project_available) {
      el.projectToggle.hidden = false;
      el.projectChk.checked = !!data.project_default;
      if (data.project_default) {
        el.codeStatus.textContent = "project mode — runs the file from disk";
      }
    }
  } catch (err) { /* no preload; fall through to the example */ }

  if (!loaded) loadCode(window.EXAMPLES[0].code, "fibonacci.py");

  // #run traces immediately; #run@40 also jumps to that step, and a trailing
  // :graph / :tree / :map picks the right-hand view. Handy for bookmarking a
  // preloaded file, or pointing someone at one exact moment.
  const deepLink = /^#run(?:@(\d+))?(?::(graph|tree|map))?$/.exec(location.hash);
  if (deepLink) {
    await run();
    if (deepLink[1]) goTo(+deepLink[1] - 1);
    if (deepLink[2]) setTab(deepLink[2]);
  }
}

boot();
})();
