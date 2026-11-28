"use strict";
// A pipeline drawn as a directed graph, left to right, the way a workflow scheduler draws a DAG.
// Steps sit in layers by the longest forward path from the start step. Edges that go back to an
// earlier layer (a review sending a job back to build) are drawn as arcs under the graph. With a
// job's step states, each node is coloured by what happened to it in that job.
// Uses el() and svg() from dashboard.js; nothing here builds markup from strings.

const DAG_W = 164;
const DAG_H = 54;
const DAG_GAP_X = 62;
const DAG_GAP_Y = 26;
const DAG_PAD = 28;
const DAG_STATES = {
  success: { label: "succeeded", glyph: "✓" },
  running: { label: "running", glyph: "" },
  waiting: { label: "waiting for a person", glyph: "…" },
  queued: { label: "queued", glyph: "·" },
  retry: { label: "sent back", glyph: "↺" },
  failed: { label: "failed", glyph: "✕" },
  skipped: { label: "skipped", glyph: "»" },
  disabled: { label: "off", glyph: "" },
  none: { label: "not reached", glyph: "" },
};
const DAG_LEGEND = ["success", "running", "waiting", "queued", "retry", "failed", "skipped", "none"];

// Nodes, edges and their place: { nodes: Map(name -> node), edges: [...], width, height }.
function dagLayout(graph, keep) {
  const fedBy = new Set();
  (graph.steps || []).forEach((s) => (s.edges || []).forEach((e) => { if (e && e.to !== s.name) fedBy.add(e.to); }));
  // A step that is off and that nothing leads to can never run: it is left out, unless `keep` says otherwise.
  // A merge step stays: a person's merge continues from its MERGED edge even while it is off.
  const hidden = (s) => s.enabled === false && s.type !== "merge" && s.name !== graph.start && !fedBy.has(s.name)
    && !(keep && keep(s.name));
  const steps = new Map((graph.steps || []).filter((s) => !hidden(s)).map((s) => [s.name, s]));
  const ends = (graph.end_states || []).slice();
  const names = [...steps.keys()];
  const all = names.concat(ends.filter((e) => !steps.has(e)));
  const edges = [];
  steps.forEach((step, from) => {
    (step.edges || []).forEach((edge, at) => {
      if (!edge || typeof edge.to !== "string") return;
      if (!steps.has(edge.to) && !ends.includes(edge.to)) return;
      edges.push({ from, to: edge.to, outcome: edge.outcome, max: edge.max, main: at === 0, back: false, id: from + "." + edge.outcome });
    });
  });
  const out = new Map(all.map((n) => [n, []]));
  edges.forEach((e) => out.get(e.from).push(e));

  // Depth-first from the start: an edge to a step still on the stack goes back.
  const seen = new Set();
  const open = new Set();
  const found = [];
  function visit(name) {
    seen.add(name);
    open.add(name);
    found.push(name);
    for (const e of out.get(name) || []) {
      if (e.from === e.to || open.has(e.to)) { e.back = true; continue; }
      if (!seen.has(e.to)) visit(e.to);
    }
    open.delete(name);
  }
  if (graph.start && out.has(graph.start)) visit(graph.start);
  const reach = new Set(seen);
  names.concat(ends).forEach((n) => { if (!seen.has(n)) visit(n); });
  // An edge from a step the start never reaches back into the main graph must not push that graph along.
  edges.forEach((e) => { if (!reach.has(e.from) && reach.has(e.to)) e.back = true; });

  // Longest forward path from the roots gives each step its layer. End states share the last one.
  const layer = new Map(all.map((n) => [n, 0]));
  const forward = edges.filter((e) => !e.back);
  // A step nothing leads to, other than the start (a merge a person does by hand), goes right
  // after the step declared before it, so the pipeline still reads in its written order.
  const fed = new Set(edges.filter((e) => e.from !== e.to).map((e) => e.to));
  const orphans = names.filter((n) => n !== graph.start && !fed.has(n));
  for (let round = 0; round < 4; round++) {
    let changed = false;
    for (let pass = 0; pass < all.length; pass++) {
      let moved = false;
      forward.forEach((e) => {
        if (ends.includes(e.to)) return;
        const want = layer.get(e.from) + 1;
        if (want > layer.get(e.to)) { layer.set(e.to, want); moved = true; changed = true; }
      });
      if (!moved) break;
    }
    orphans.forEach((n) => {
      const before = names[names.indexOf(n) - 1];
      const want = before ? layer.get(before) + 1 : 0;
      if (want > layer.get(n)) { layer.set(n, want); changed = true; }
    });
    if (!changed) break;
  }
  const deepest = Math.max(0, ...names.map((n) => layer.get(n)));
  ends.forEach((n) => layer.set(n, deepest + 1));
  // An end state nothing leads to is left out.
  const targeted = new Set(edges.map((e) => e.to));
  const shown = all.filter((n) => !ends.includes(n) || targeted.has(n));
  // Layers with no step in them close up.
  const used = [...new Set(shown.map((n) => layer.get(n)))].sort((a, b) => a - b);
  shown.forEach((n) => layer.set(n, used.indexOf(layer.get(n))));

  // The main chain: the first edge of each step, from the start. It runs along the middle row.
  const spine = new Set();
  for (let at = graph.start; at && steps.has(at) && !spine.has(at);) {
    spine.add(at);
    const first = (out.get(at) || []).find((e) => e.main && !e.back);
    at = first ? first.to : null;
  }

  const columns = [];
  shown.forEach((n) => {
    const l = layer.get(n);
    (columns[l] = columns[l] || []).push(n);
  });
  const rank = new Map(found.map((n, i) => [n, i]));
  columns.forEach((col) => col.sort((a, b) => (rank.get(a) || 0) - (rank.get(b) || 0)));
  // Barycentre sweeps: each step moves towards the steps that lead to it.
  const pos = new Map();
  const place = () => columns.forEach((col) => col && col.forEach((n, i) => pos.set(n, i - (col.length - 1) / 2)));
  place();
  for (let sweep = 0; sweep < 4; sweep++) {
    columns.forEach((col, l) => {
      if (!col || l === 0) return;
      const centre = new Map(col.map((n) => {
        const from = forward.filter((e) => e.to === n && pos.has(e.from)).map((e) => pos.get(e.from));
        return [n, from.length ? from.reduce((a, b) => a + b, 0) / from.length : pos.get(n)];
      }));
      col.sort((a, b) => centre.get(a) - centre.get(b));
      // The spine step takes the middle slot, so the main path reads as a straight line.
      const main = col.find((n) => spine.has(n));
      if (main && col.length > 1) {
        col.splice(col.indexOf(main), 1);
        col.splice(Math.floor(col.length / 2), 0, main);
      }
    });
    place();
  }
  const rows = Math.max(1, ...columns.map((col) => (col ? col.length : 0)));
  const height = DAG_PAD * 2 + rows * DAG_H + (rows - 1) * DAG_GAP_Y;
  const mid = height / 2;
  const nodes = new Map();
  columns.forEach((col, l) => {
    (col || []).forEach((n) => {
      const x = DAG_PAD + l * (DAG_W + DAG_GAP_X);
      const y = mid + pos.get(n) * (DAG_H + DAG_GAP_Y) - DAG_H / 2;
      nodes.set(n, { name: n, x, y, layer: l, step: steps.get(n) || null, end: ends.includes(n), spine: spine.has(n) });
    });
  });
  const width = DAG_PAD * 2 + columns.length * DAG_W + Math.max(0, columns.length - 1) * DAG_GAP_X;
  // Back edges run in lanes under the graph, the shortest nearest.
  const backs = edges.filter((e) => e.back && nodes.has(e.from) && nodes.has(e.to));
  backs.sort((a, b) => Math.abs(nodes.get(a.from).x - nodes.get(a.to).x) - Math.abs(nodes.get(b.from).x - nodes.get(b.to).x));
  const lanes = [];
  backs.forEach((e) => {
    const lo = Math.min(nodes.get(e.from).x, nodes.get(e.to).x);
    const hi = Math.max(nodes.get(e.from).x, nodes.get(e.to).x);
    let lane = lanes.findIndex((used) => used.every(([a, b]) => hi < a - 8 || lo > b + 8));
    if (lane < 0) { lane = lanes.length; lanes.push([]); }
    lanes[lane].push([lo, hi]);
    e.lane = lane;
  });
  const below = lanes.length ? 26 + lanes.length * 18 : 0;
  return { nodes, edges: edges.filter((e) => nodes.has(e.from) && nodes.has(e.to)), width, height: height + below, base: height };
}

function dagEdgePath(e, layout) {
  const a = layout.nodes.get(e.from);
  const b = layout.nodes.get(e.to);
  if (!e.back) {
    const sx = a.x + DAG_W, sy = a.y + DAG_H / 2, tx = b.x, ty = b.y + DAG_H / 2;
    const bend = Math.max(36, (tx - sx) / 2);
    return { d: `M${sx},${sy} C${sx + bend},${sy} ${tx - bend},${ty} ${tx},${ty}`, lx: (sx + tx) / 2, ly: (sy + ty) / 2 };
  }
  if (e.from === e.to) {
    const x = a.x + DAG_W - 28, y = a.y + DAG_H;
    return { d: `M${x},${y} C${x},${y + 34} ${x + 26},${y + 34} ${x + 26},${y}`, lx: x + 13, ly: y + 30 };
  }
  const floor = layout.base - DAG_PAD + 22 + e.lane * 18;
  const sx = a.x + DAG_W / 2 + 14, sy = a.y + DAG_H, tx = b.x + DAG_W / 2 - 14, ty = b.y + DAG_H;
  const r = 14;
  const dir = tx < sx ? -1 : 1;
  const d = `M${sx},${sy} L${sx},${floor - r} Q${sx},${floor} ${sx + dir * r},${floor} L${tx - dir * r},${floor} Q${tx},${floor} ${tx},${floor - r} L${tx},${ty}`;
  return { d, lx: (sx + tx) / 2, ly: floor };
}

function dagNodeState(name, opts) {
  if (opts.states && opts.states[name]) return opts.states[name].state || "none";
  return null;
}

// The graph as an element. opts: { states, taken, counts(name), selected, onSelect(name), label }.
function dagView(graph, opts) {
  opts = opts || {};
  let layout;
  try {
    const visited = (name) => !!(opts.states && opts.states[name] && (opts.states[name].visits || []).length);
    layout = dagLayout(graph, visited);
  } catch (error) {
    return el("div", { class: "notice", text: "This pipeline could not be drawn." });
  }
  const job = !!opts.states;
  const root = svg("svg", {
    class: "dag" + (job ? " dag-job" : ""), viewBox: `0 0 ${layout.width} ${layout.height}`,
    width: layout.width, height: layout.height, role: "img",
    "aria-label": opts.label || "Pipe graph",
  });
  const defs = svg("defs", {});
  ["base", "hot", "taken", "back"].forEach((kind) => {
    defs.append(svg("marker", { id: "dag-arrow-" + kind, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svg("path", { d: "M0,0 L10,5 L0,10 z", class: "dag-arrow " + kind })));
  });
  root.append(defs);
  const edgeLayer = svg("g", { class: "dag-edges" });
  const labelLayer = svg("g", { class: "dag-labels" });
  const nodeLayer = svg("g", { class: "dag-nodes" });
  const taken = opts.taken || {};
  layout.edges.forEach((e) => {
    const path = dagEdgePath(e, layout);
    const count = taken[e.id] || 0;
    const toEnd = layout.nodes.get(e.to).end;
    const quiet = !e.main && !count && (toEnd || e.to === graph.escalate_to);
    const cls = ["dag-edge", e.back ? "back" : "", count ? "taken" : "", quiet ? "quiet" : "", e.main ? "main" : ""].filter(Boolean).join(" ");
    const marker = count ? "taken" : e.back ? "back" : "base";
    edgeLayer.append(svg("path", { d: path.d, class: cls, "data-from": e.from, "data-to": e.to, "marker-end": `url(#dag-arrow-${marker})` },
      svg("title", { text: `${e.from} ${e.outcome} → ${e.to}` + (e.max !== null && e.max !== undefined ? ` (max ${e.max})` : "") + (count ? ` · taken ${count}×` : "") })));
    const text = e.outcome + (e.max !== null && e.max !== undefined ? " ≤" + e.max : "") + (count > 1 ? " ×" + count : "");
    const width = text.length * 6.3 + 10;
    labelLayer.append(svg("g", { class: "dag-label" + (quiet ? " quiet" : "") + (count ? " taken" : ""), "data-from": e.from, "data-to": e.to, transform: `translate(${path.lx - width / 2},${path.ly - 8})` },
      svg("rect", { width, height: 16, rx: 3 }),
      svg("text", { x: width / 2, y: 11.5, "text-anchor": "middle", text })));
  });
  layout.nodes.forEach((node) => {
    const step = node.step;
    const st = dagNodeState(node.name, opts);
    const off = step && step.enabled === false;
    const human = step && step.type === "human";
    const count = opts.counts ? opts.counts(node.name) : 0;
    const cls = ["dag-node", node.end ? "end" : "", off ? "off" : "", human ? "human" : "", st ? "st-" + st : "",
      !job && human && count ? "wait" : "", opts.selected === node.name ? "on" : ""].filter(Boolean).join(" ");
    const sub = node.end ? "end state" : step ? step.type + (human && step.who && typeof step.who === "string" ? " · " + step.who : "") + (off ? " · off" : "") : "";
    const g = svg("g", {
      class: cls, transform: `translate(${node.x},${node.y})`, tabindex: opts.onSelect ? 0 : null,
      role: opts.onSelect ? "button" : null, "data-node": node.name, "data-focus": opts.onSelect ? "dag-node:" + node.name : null,
      "aria-label": node.name + (st ? ", " + DAG_STATES[st].label : "") + (sub ? ", " + sub : ""),
    });
    g.append(svg("rect", { class: "dag-box", width: DAG_W, height: DAG_H, rx: 7 }));
    g.append(svg("rect", { class: "dag-strip", width: 4, height: DAG_H - 12, x: 0, y: 6, rx: 2 }));
    g.append(svg("text", { class: "dag-name", x: 16, y: 23, text: node.name.length > 19 ? node.name.slice(0, 18) + "…" : node.name }));
    let line = sub;
    if (job && st) {
      const info = opts.states[node.name] || {};
      const visits = (info.visits || []).length;
      const secs = info.seconds || 0;
      line = DAG_STATES[st].label + (secs ? " · " + span(secs) : "") + (visits > 1 ? " ×" + visits : "");
    }
    g.append(svg("text", { class: "dag-sub", x: 16, y: 41, text: line }));
    if (job && st && st !== "none" && st !== "disabled") {
      const badge = svg("g", { class: "dag-state", transform: `translate(${DAG_W - 20},${DAG_H / 2})` });
      badge.append(svg("circle", { r: 9 }));
      if (st === "running") badge.append(svg("circle", { class: "dag-spin", r: 9 }));
      badge.append(svg("text", { y: 4, "text-anchor": "middle", text: DAG_STATES[st].glyph }));
      g.append(badge);
    } else if (!job && count) {
      const badge = svg("g", { class: "dag-count", transform: `translate(${DAG_W - 20},${DAG_H / 2})` });
      badge.append(svg("circle", { r: 10 }), svg("text", { y: 4, "text-anchor": "middle", text: String(count) }));
      g.append(badge);
    }
    g.append(svg("title", { text: node.name + (sub ? " — " + sub : "") + (st ? " — " + DAG_STATES[st].label : "") }));
    if (opts.onSelect) {
      g.addEventListener("click", () => opts.onSelect(node.name));
      g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); opts.onSelect(node.name); } });
    }
    const light = (on) => {
      root.classList.toggle("focus", on);
      root.querySelectorAll(`[data-from="${CSS.escape(node.name)}"], [data-to="${CSS.escape(node.name)}"]`).forEach((n) => n.classList.toggle("hot", on));
      g.classList.toggle("hot", on);
    };
    g.addEventListener("mouseenter", () => light(true));
    g.addEventListener("mouseleave", () => light(false));
    g.addEventListener("focus", () => light(true));
    g.addEventListener("blur", () => light(false));
    nodeLayer.append(g);
  });
  root.append(edgeLayer, nodeLayer, labelLayer);
  return dagFrame(root, layout, opts);
}

// The graph in a frame that scrolls, pans with a drag, and zooms with buttons.
function dagFrame(root, layout, opts) {
  const key = opts.zoomKey || "dag";
  const stored = state.dagZoom && state.dagZoom[key];
  const zoom = stored || 1;
  const apply = (z) => {
    state.dagZoom = state.dagZoom || {};
    state.dagZoom[key] = z;
    root.setAttribute("width", String(Math.round(layout.width * z)));
    root.setAttribute("height", String(Math.round(layout.height * z)));
  };
  apply(zoom);
  const stage = el("div", { class: "dag-stage", "data-scroll-key": "dag:" + key }, root);
  let drag = null;
  stage.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.closest(".dag-node")) return;
    drag = { x: ev.clientX, y: ev.clientY, left: stage.scrollLeft, top: stage.scrollTop };
    stage.classList.add("grab");
  });
  stage.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    stage.scrollLeft = drag.left - (ev.clientX - drag.x);
    stage.scrollTop = drag.top - (ev.clientY - drag.y);
  });
  const stop = () => { drag = null; stage.classList.remove("grab"); };
  stage.addEventListener("pointerup", stop);
  stage.addEventListener("pointerleave", stop);
  const fit = () => {
    const room = stage.clientWidth - 8;
    apply(Math.max(0.4, Math.min(1.6, room / layout.width)));
  };
  // The first time a graph is shown, it fits the frame's width, unless that would make it too small to read.
  if (!stored && typeof requestAnimationFrame === "function") {
    requestAnimationFrame(() => {
      if (!stage.isConnected || (state.dagZoom && state.dagZoom[key] && state.dagZoom[key] !== zoom)) return;
      const room = stage.clientWidth - 8;
      if (room > 0) apply(Math.max(0.8, Math.min(1, room / layout.width)));
    });
  }
  // A step picked just now is scrolled into view, once: later redraws keep the scroll the reader left.
  if (opts.selected && state.dagShown !== key + ":" + opts.selected && typeof requestAnimationFrame === "function") {
    state.dagShown = key + ":" + opts.selected;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const node = layout.nodes.get(opts.selected);
      if (!node || !stage.isConnected) return;
      const z = (state.dagZoom && state.dagZoom[key]) || 1;
      const left = node.x * z, right = (node.x + DAG_W) * z;
      if (left < stage.scrollLeft || right > stage.scrollLeft + stage.clientWidth) {
        stage.scrollLeft = Math.max(0, left - (stage.clientWidth - DAG_W * z) / 2);
      }
    }));
  }
  const bar = el("div", { class: "dag-tools", role: "group", "aria-label": "Zoom" },
    el("button", { type: "button", title: "Zoom out", "aria-label": "Zoom out", text: "−", onclick: () => apply(Math.max(0.4, (state.dagZoom[key] || 1) / 1.2)) }),
    el("button", { type: "button", title: "Fit to width", text: "fit", onclick: fit }),
    el("button", { type: "button", title: "Actual size", text: "1:1", onclick: () => apply(1) }),
    el("button", { type: "button", title: "Zoom in", "aria-label": "Zoom in", text: "+", onclick: () => apply(Math.min(2.5, (state.dagZoom[key] || 1) * 1.2)) }));
  return el("div", { class: "dag-frame" }, bar, stage);
}

function dagLegend(job) {
  if (!job) {
    return el("div", { class: "legend dag-legend" },
      el("span", {}, el("i", { class: "ln" }), "main path"),
      el("span", {}, el("i", { class: "ln back" }), "back to an earlier step"),
      el("span", {}, el("i", { class: "ln quiet" }), "to an end state or escalation"),
      el("span", {}, el("i", { class: "box wait" }), "waiting for a person"),
      el("span", {}, el("i", { class: "box off" }), "step is off"));
  }
  return el("div", { class: "legend dag-legend" }, DAG_LEGEND.map((st) => el("span", {}, el("i", { class: "sw st-" + st }), DAG_STATES[st].label)));
}
