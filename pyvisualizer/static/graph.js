/* Flow-graph view.
 *
 * The server sends a laid-out flowchart per function (nodes with absolute
 * coordinates plus edges). This file turns one of those into SVG and paints
 * the execution on top of it: which boxes have run, how many times, which
 * arrows were actually taken, and where the program is standing right now.
 */

window.FlowGraph = (function () {
"use strict";

function esc(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------------------------------------------------------- *
 * Which graph belongs to the frame we are standing in?
 * ---------------------------------------------------------------- */

function pickGraph(graphs, step) {
  if (!graphs || !graphs.length || !step) return null;
  const frame = step.stack[step.stack.length - 1];
  const name = frame ? frame.func : "<module>";
  const candidates = graphs.filter((g) => g.name === name);
  if (!candidates.length) return graphs[0];
  if (candidates.length === 1) return candidates[0];
  // Same name defined more than once (two methods called __init__, say):
  // pick the one whose source range contains the line we are on.
  const line = step.line;
  return candidates.find((g) => line >= g.first_line && line <= g.last_line) || candidates[0];
}

/* ---------------------------------------------------------------- *
 * Execution overlay
 * ---------------------------------------------------------------- */

/** Map a source line to the node that represents it. */
function lineIndex(graph) {
  const map = new Map();
  graph.nodes.forEach((node) => {
    if (node.kind === "merge") return;
    if (!map.has(node.line)) map.set(node.line, node.id);
  });
  return map;
}

/** Edges leaving each node, for walking through invisible merge points. */
function adjacency(graph) {
  const out = new Map();
  graph.edges.forEach((edge) => {
    if (!out.has(edge.from)) out.set(edge.from, []);
    out.get(edge.from).push(edge);
  });
  return out;
}

/**
 * Find the chain of edges that gets from node `a` to node `b`, allowed to pass
 * through merge nodes on the way (they carry no source line of their own).
 */
function edgePath(graph, out, a, b, byId) {
  const queue = [[a, []]];
  const seen = new Set([a]);
  while (queue.length) {
    const [node, trail] = queue.shift();
    if (trail.length > 6) continue;
    for (const edge of out.get(node) || []) {
      if (edge.to === b) return trail.concat([edge]);
      const target = byId.get(edge.to);
      if (target && target.kind === "merge" && !seen.has(edge.to)) {
        seen.add(edge.to);
        queue.push([edge.to, trail.concat([edge])]);
      }
    }
  }
  return null;
}

function edgeKey(edge) { return edge.from + ">" + edge.to; }

/**
 * Replay the current frame invocation up to `index` and work out what was
 * executed. Returns per-node visit counts and the set of edges taken.
 */
function computeExecution(graph, steps, index) {
  const result = { counts: new Map(), edges: new Set(), current: null };
  if (!graph) return result;

  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const lines = lineIndex(graph);
  const out = adjacency(graph);

  const step = steps[index];
  const frame = step.stack[step.stack.length - 1];
  const fid = frame ? frame.fid : 0;

  // Only the steps belonging to this exact invocation, in order.
  const visited = [];
  for (let i = 0; i <= index; i++) {
    const other = steps[i];
    const top = other.stack[other.stack.length - 1];
    if (!top || top.fid !== fid) continue;
    const nodeId = lines.get(other.line);
    if (nodeId !== undefined) visited.push(nodeId);
  }

  visited.forEach((nodeId, i) => {
    result.counts.set(nodeId, (result.counts.get(nodeId) || 0) + 1);
    if (i === 0) return;
    const previous = visited[i - 1];
    if (previous === nodeId) return;
    const chain = edgePath(graph, out, previous, nodeId, byId);
    if (chain) chain.forEach((edge) => result.edges.add(edgeKey(edge)));
  });

  result.current = lines.get(step.line);

  // Entering a function: light the arrow from the start box.
  if (visited.length) {
    const startNode = graph.nodes.find((n) => n.kind === "start");
    if (startNode) {
      const chain = edgePath(graph, out, startNode.id, visited[0], byId);
      if (chain) chain.forEach((edge) => result.edges.add(edgeKey(edge)));
      result.counts.set(startNode.id, 1);
    }
  }
  return result;
}

/* ---------------------------------------------------------------- *
 * Drawing
 * ---------------------------------------------------------------- */

function nodeShape(node, classes) {
  const { x, y, w, h } = node;
  const attrs = 'class="' + classes + '" ';

  if (node.kind === "merge") {
    return "<circle " + attrs + 'cx="' + (x + w / 2) + '" cy="' + (y + h / 2) +
           '" r="4.5"/>';
  }
  if (node.kind === "cond" || node.kind === "loop") {
    const points = [
      [x + 10, y], [x + w - 10, y], [x + w, y + h / 2],
      [x + w - 10, y + h], [x + 10, y + h], [x, y + h / 2]
    ].map((p) => p.join(",")).join(" ");
    return "<polygon " + attrs + 'points="' + points + '"/>';
  }
  const radius = (node.kind === "start" || node.kind === "end") ? h / 2 : 6;
  return "<rect " + attrs + 'x="' + x + '" y="' + y + '" width="' + w +
         '" height="' + h + '" rx="' + radius + '"/>';
}

function drawNode(node, exec) {
  const count = exec.counts.get(node.id) || 0;
  const isCurrent = exec.current === node.id;

  let classes = "gnode k-" + node.kind;
  if (count) classes += " visited";
  if (isCurrent) classes += " current";

  let svg = '<g class="gnode-group" data-line="' + node.line + '" data-id="' + node.id + '">';
  svg += nodeShape(node, classes);

  if (node.kind !== "merge") {
    svg += '<text class="glabel' + (isCurrent ? " current" : "") + '" x="' +
           (node.x + node.w / 2) + '" y="' + (node.y + node.h / 2 + 4) +
           '" text-anchor="middle">' + esc(node.label) + "</text>";
  }
  if (count > 1) {
    svg += '<text class="gcount" x="' + (node.x + node.w - 3) + '" y="' +
           (node.y - 3) + '" text-anchor="end">×' + count + "</text>";
  }
  return svg + "</g>";
}

function drawEdge(edge, byId, exec) {
  const a = byId.get(edge.from), b = byId.get(edge.to);
  if (!a || !b) return "";

  const taken = exec.edges.has(edgeKey(edge));
  const classes = "gedge e-" + edge.kind + (taken ? " taken" : "");

  let path, labelAt;

  if (edge.kind === "back") {
    // Route the loop's back-edge down its own lane, just left of the loop
    // head, so nested loops get visibly separate lanes instead of overlapping.
    const lane = Math.max(4, b.x - 13);
    const sx = a.x + a.w / 2, sy = a.y + a.h;
    const tx = b.x, ty = b.y + b.h / 2;
    path = "M " + sx + " " + sy + " V " + (sy + 12) +
           " H " + lane + " V " + ty + " H " + tx;
    labelAt = [lane + 4, (sy + ty) / 2];
  } else {
    const sx = a.x + a.w / 2, sy = a.y + a.h;
    const tx = b.x + b.w / 2, ty = b.y;
    if (Math.abs(sx - tx) < 1.5) {
      path = "M " + sx + " " + sy + " L " + tx + " " + ty;
    } else {
      const bend = Math.max(14, (ty - sy) / 2);
      path = "M " + sx + " " + sy + " C " + sx + " " + (sy + bend) + ", " +
             tx + " " + (ty - bend) + ", " + tx + " " + ty;
    }
    labelAt = [(sx + tx) / 2 + 6, sy + 13];
  }

  let svg = '<path class="' + classes + '" d="' + path + '" marker-end="url(#' +
            (taken ? "gtip-on" : "gtip") + ')"/>';
  if (edge.label) {
    svg += '<text class="gedge-label' + (taken ? " taken" : "") + '" x="' +
           labelAt[0] + '" y="' + labelAt[1] + '">' + esc(edge.label) + "</text>";
  }
  return svg;
}

const MARKERS =
  '<defs>' +
  '<marker id="gtip" markerWidth="7" markerHeight="7" refX="6.2" refY="2.6" orient="auto">' +
  '<path d="M0,0 L6,2.6 L0,5.2 z" fill="#46545f"/></marker>' +
  '<marker id="gtip-on" markerWidth="7" markerHeight="7" refX="6.2" refY="2.6" orient="auto">' +
  '<path d="M0,0 L6,2.6 L0,5.2 z" fill="#4d9fff"/></marker>' +
  "</defs>";

function render(graph, exec, zoom) {
  if (!graph) {
    return '<div class="graph-empty">No flow graph for this frame.</div>';
  }
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const edges = graph.edges.map((e) => drawEdge(e, byId, exec)).join("");
  const nodes = graph.nodes.map((n) => drawNode(n, exec)).join("");

  return '<svg class="gsvg" width="' + Math.round(graph.width * zoom) +
    '" height="' + Math.round(graph.height * zoom) +
    '" viewBox="0 0 ' + graph.width + " " + graph.height + '">' +
    MARKERS + edges + nodes + "</svg>";
}

function fitZoom(graph, containerWidth) {
  if (!graph || !graph.width) return 1;
  return Math.max(0.35, Math.min(1.25, (containerWidth - 24) / graph.width));
}

return { pickGraph, computeExecution, render, fitZoom };
})();
