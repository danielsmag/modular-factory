"use strict";
// Everything shown here may come from a GitHub issue, so no markup is ever built
// from strings: nodes are created one by one and text goes in as text.
const SVG = "http://www.w3.org/2000/svg";
const REFRESH_MS = 5000;
const QUEUE_MS = 15000;
const RUNNING = new Set(["agent", "checks", "review", "merge", "verify"]);
const CHOICE_KEY = "mfactory.fleet.selected";
const FOOT_NOTE = "read-only · approvals and feedback go through /mfactory or an issue comment";
const FALLBACK = [
  ["intake", ["intake", "ask-author"]],
  ["spec", ["spec"]],
  ["build", ["build", "gate", "debug"]],
  ["review", ["review"]],
  ["approve", ["approve", "merge", "verify"]],
  ["needs-you", ["spec-approval", "escalated"]],
  ["done", ["done", "rejected", "failed"]],
];
function queryProject() {
  try {
    if (typeof location === "undefined") return "";
    return new URLSearchParams(location.search).get("project") || "";
  } catch (error) {
    return "";
  }
}
function readChoice() {
  const chosen = queryProject();
  if (chosen) return chosen;
  try {
    if (typeof localStorage === "undefined") return "all";
    return localStorage.getItem(CHOICE_KEY) || "all";
  } catch (error) {
    return "all";
  }
}
function writeChoice(id) {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(CHOICE_KEY, id);
  } catch (error) {}
}
const SPLIT_KEY = "factory-split";
const SPLIT_MIN = 140;
const SPLIT_DEFAULT = 220;
function readSplit() {
  try {
    const n = Number(localStorage.getItem(SPLIT_KEY));
    if (n >= SPLIT_MIN && n <= 720) return n;
  } catch (error) {}
  return SPLIT_DEFAULT;
}
function writeSplit(px) {
  state.split = px;
  try { localStorage.setItem(SPLIT_KEY, String(px)); } catch (error) {}
}
const state = {
  index: null, graph: null, config: null, error: null, query: "", file: null, source: "all", drawer: null,
  fleet: false, projects: [], selected: readChoice(), queueRows: [], queueErrors: [], queueStale: false,
  split: readSplit(),
};
if (queryProject()) writeChoice(state.selected);

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
  return node;
}
function svg(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const kid of kids.flat(Infinity)) if (kid) node.append(kid);
  return node;
}
function link(url, text) {
  // Only real web addresses become links; anything else is shown as plain text.
  return /^https:\/\//.test(url || "") ? el("a", { class: "ext", href: url, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { class: "mono", text });
}

// ---- data helpers ----
const time = (stamp) => (stamp ? new Date(stamp).getTime() : null);
function ago(stamp) {
  const t = time(stamp);
  if (!t) return "-";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function span(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return "-";
  if (seconds < 60) return Math.round(seconds) + "s";
  if (seconds < 3600) return Math.round(seconds / 60) + "m";
  if (seconds < 172800) return (seconds / 3600).toFixed(1).replace(/\.0$/, "") + "h";
  return Math.round(seconds / 86400) + "d";
}
const money = (n) => "$" + Number(n).toFixed(2);
function graphOf(job) {
  if (job && job.graph) return job.graph;
  return state.graph;
}
function stepOf(name, job) {
  const graph = graphOf(job);
  if (!graph || !Array.isArray(graph.steps)) return null;
  return graph.steps.find((s) => s.name === name) || null;
}
function isEnd(name, job) {
  const graph = graphOf(job);
  return !!(graph && Array.isArray(graph.end_states) && graph.end_states.includes(name));
}
function kindOf(job) {
  if (job.step === "done") return "done";
  if (job.step === "rejected" || job.step === "failed") return "bad";
  if (job.waiting) return "wait";
  return "run";
}
function chip(job) {
  return el("span", { class: "chip " + kindOf(job), text: job.step });
}
function matches(job) {
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return [job.project, job.projectName, job.id, job.title, "#" + job.issue, job.step, job.author, job.pr ? "#" + job.pr : ""].some((v) => String(v || "").toLowerCase().includes(q));
}
function visibleProjects() {
  if (!state.fleet) return [];
  return state.projects.filter((project) => state.selected === "all" || state.selected === project.id);
}
function projectLabel(project) {
  const name = project.name || project.id;
  const shared = state.projects.filter((item) => (item.name || item.id) === name).length > 1;
  return shared ? project.id : name;
}
function taggedJob(project, job) {
  const copy = Object.assign({}, job);
  copy.project = project.id;
  copy.projectName = projectLabel(project);
  copy.graph = project.graph || null;
  return copy;
}
function jobs() {
  if (state.fleet) {
    const list = [];
    visibleProjects().forEach((project) => {
      (project.jobs || []).forEach((job) => list.push(taggedJob(project, job)));
    });
    list.sort((a, b) => {
      const name = String(a.projectName || "").localeCompare(String(b.projectName || ""));
      if (name) return name;
      return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
    });
    return list;
  }
  return state.index ? state.index.jobs.slice().sort((a, b) => (a.id < b.id ? 1 : -1)) : [];
}
function queueMatches(row) {
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return [row.project, row.projectName, row.note, "#" + row.issue, row.action, row.job, row.step].some((value) => String(value || "").toLowerCase().includes(q));
}
function queueCards(rows, jobList) {
  const ids = new Set(jobList.map((job) => job.id));
  const issues = new Set(jobList.map((job) => String(job.issue)));
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    if (row.action !== "new" && row.action !== "hold") continue;
    if (row.job && row.job !== "-" && ids.has(row.job)) continue;
    if (issues.has(String(row.issue))) continue;
    const key = String(row.issue);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(row);
  }
  return out;
}
function commands(job) {
  const step = stepOf(job.step, job);
  if (!step || !job.waiting) return [];
  const hint = { REJECT: " <why>", ANSWER: " <your answer>", APPROVE: "" };
  return (step.edges || []).map((e) => "/mfactory " + e.outcome.toLowerCase() + " " + job.issue + (e.outcome in hint ? hint[e.outcome] : " <what to change>"));
}

// ---- board ----
function boardTotals(all) {
  const running = all.filter((j) => { const s = stepOf(j.step, j); return s && RUNNING.has(s.type) && !j.waiting; }).length;
  const cost = all.reduce((sum, j) => sum + (Number(j.cost_usd) || 0), 0);
  return { running, cost };
}
function columnsFor(graph, allJobs) {
  const steps = new Map((graph.steps || []).map((s) => [s.name, s]));
  const ends = new Set(graph.end_states || []);
  const present = new Set(allJobs.map((j) => j.step).filter((name) => name));
  const cols = [];
  const columnOf = (name) => cols.find((col) => col.steps.includes(name));
  const workColumn = () => {
    for (let i = cols.length - 1; i >= 0; i--) if (cols[i].id !== "needs-you") return cols[i];
    return null;
  };
  for (const name of order(graph)) {
    if (ends.has(name)) continue;
    const step = steps.get(name);
    if (!step) continue;
    if (step.enabled === false && !present.has(name)) continue;
    if (step.type === "human") {
      if (step.who === "author") {
        const target = (step.edges || []).map((edge) => edge.to).find((to) => columnOf(to));
        if (target) { columnOf(target).steps.push(name); continue; }
      }
      let needs = cols.find((col) => col.id === "needs-you");
      if (!needs) {
        needs = { id: "needs-you", label: "Needs you", cls: "human", steps: [] };
        cols.push(needs);
      }
      needs.steps.push(name);
      continue;
    }
    if ((step.type === "checks" || step.type === "merge" || step.type === "verify") && workColumn()) {
      workColumn().steps.push(name);
      continue;
    }
    cols.push({ id: name, label: name, cls: "", steps: [name] });
  }
  const needs = cols.find((col) => col.id === "needs-you");
  const ordered = cols.filter((col) => col.id !== "needs-you");
  if (needs) ordered.push(needs);
  const covered = new Set(ordered.flatMap((col) => col.steps).concat(graph.end_states || []));
  const stray = [...present].filter((name) => !covered.has(name));
  if (stray.length) ordered.push({ id: "other", label: "Other", cls: "", steps: stray });
  if ((graph.end_states || []).length) ordered.push({ id: "done", label: "Done", cls: "done", steps: graph.end_states.slice() });
  return ordered;
}
function fallbackColumns(jobList) {
  const present = new Set(jobList.map((job) => job.step).filter((name) => name));
  const known = new Set(FALLBACK.flatMap((row) => row[1]));
  const cols = [];
  FALLBACK.forEach((row) => {
    const mine = row[1].filter((name) => present.has(name));
    if (!mine.length) return;
    const id = row[0];
    cols.push({
      id,
      label: id === "needs-you" ? "Needs you" : id === "done" ? "Done" : id,
      cls: id === "needs-you" ? "human" : id === "done" ? "done" : "",
      steps: mine,
    });
  });
  const stray = [...present].filter((name) => !known.has(name));
  if (stray.length) {
    const other = { id: "other", label: "Other", cls: "", steps: stray };
    const doneAt = cols.findIndex((col) => col.id === "done");
    if (doneAt >= 0) cols.splice(doneAt, 0, other);
    else cols.push(other);
  }
  return cols;
}
function columnsOf(project) {
  const list = project.jobs || [];
  const graph = project.graph;
  if (graph && Array.isArray(graph.steps) && Array.isArray(graph.end_states)) {
    try { return columnsFor(graph, list); } catch (error) { /* a broken graph still shows the jobs */ }
  }
  return fallbackColumns(list);
}
function unionColumns(projects, allJobs) {
  const merged = [];
  const claimed = new Set();
  function add(col) {
    const steps = [];
    (col.steps || []).forEach((name) => {
      if (!name || claimed.has(name)) return;
      claimed.add(name);
      steps.push(name);
    });
    if (!steps.length) return;
    const existing = merged.find((item) => item.id === col.id);
    if (existing) {
      existing.steps.push(...steps);
      return;
    }
    const entry = { id: col.id, label: col.label, cls: col.cls || "", steps };
    const needsAt = merged.findIndex((item) => item.id === "needs-you");
    const doneAt = merged.findIndex((item) => item.id === "done");
    let at = merged.length;
    if (col.id !== "done" && col.id !== "needs-you") {
      if (needsAt >= 0) at = Math.min(at, needsAt);
      if (doneAt >= 0) at = Math.min(at, doneAt);
    } else if (col.id === "needs-you" && doneAt >= 0) {
      at = doneAt;
    }
    merged.splice(at, 0, entry);
  }
  projects.forEach((project) => columnsOf(project).forEach(add));
  const stray = [];
  allJobs.forEach((job) => {
    if (job.step && !claimed.has(job.step) && stray.indexOf(job.step) < 0) stray.push(job.step);
  });
  if (stray.length) add({ id: "other", label: "Other", cls: "", steps: stray });
  return merged;
}
function queueFor(projects) {
  const rows = [];
  projects.forEach((project) => {
    const mine = state.queueRows.filter((row) => row.project === project.id);
    queueCards(mine, project.jobs || []).forEach((row) => {
      const copy = Object.assign({}, row);
      copy.projectName = projectLabel(project);
      rows.push(copy);
    });
  });
  return rows;
}
function boardColumns(all) {
  if (!state.fleet) return columnsFor(state.graph, all);
  const projects = visibleProjects();
  const cols = projects.length === 1
    ? columnsOf(projects[0]).map((col) => Object.assign({}, col, { steps: col.steps.slice() }))
    : unionColumns(projects, all);
  const queued = queueFor(projects);
  if (queued.length) cols.unshift({ id: "queue", label: "Queue", cls: "", steps: [], queue: queued });
  return cols;
}
function columnWho(col) {
  if (col.id === "queue") return "not a job yet";
  if (col.id === "needs-you" || col.id === "other" || col.id === "done") return col.steps.join(" · ");
  return col.steps.slice(1).join(" · ");
}
function jobNum(id) {
  const text = String(id || "");
  const cut = text.indexOf("-");
  return cut > 0 ? text.slice(0, cut) + " · " + text.slice(cut + 1) : text;
}
function elapsed(job) {
  const start = time(job.created_at);
  if (!start) return "-";
  return span(((time(job.finished_at) || Date.now()) - start) / 1000);
}
function flagOf(job) {
  if (job.waiting && job.waiting.who === "author") return "asked author";
  if (job.waiting && job.waiting.who === "maintainer") return "your call";
  if (job.step === "rejected") return "rejected";
  return "";
}
function riskClass(risk) {
  const name = String(risk || "").toLowerCase();
  if (name === "high") return "hot";
  if (name === "medium") return "warn";
  return "";
}
function chipVerdict(line, prefix) {
  const match = String(line || "").match(new RegExp("^" + prefix + ":\\s*(PASS|FAIL|CHANGES)\\b"));
  if (match) return match[1];
  return line ? "bad" : "pending";
}
function shortReview(name) {
  if (name === "security") return "sec";
  return name.length > 6 ? name.slice(0, 4) : name;
}
function roundRows(job) {
  const files = job.files || [];
  let max = job.round || 0;
  files.forEach((file) => {
    const match = String(file.path).match(/^round-(\d+)\//);
    if (match) max = Math.max(max, Number(match[1]));
  });
  const rows = [];
  for (let n = 1; n <= max; n++) {
    const checks = files.find((file) => file.path === "round-" + n + "/checks.md");
    const gate = checks ? chipVerdict(checks.first_line, "GATE") : "pending";
    const reviews = files.filter((file) => new RegExp("^round-" + n + "/review-[^/]+\\.md$").test(file.path)).map((file) => ({
      name: file.path.split("/").pop().replace(/^review-|\.md$/g, ""),
      v: chipVerdict(file.first_line, "VERDICT"),
    })).sort((a, b) => (a.name < b.name ? -1 : 1));
    rows.push({ n, gate, reviews: gate === "PASS" ? reviews : [] });
  }
  return rows;
}
function kchip(label, verdict, waiting, gate) {
  const known = verdict === "PASS" || verdict === "FAIL" || verdict === "CHANGES";
  const cls = "kchip" + (gate ? " gate" : "") + (known ? " " + verdict : verdict === "bad" ? " bad" : waiting ? " wait" : "");
  return el("span", { class: cls, title: label + " " + verdict, text: label });
}
function roundLine(job, row) {
  const current = row.n === job.round;
  const chips = [kchip("gate", row.gate, current && job.step === "gate", true)];
  if (row.gate === "PASS") row.reviews.forEach((review) => chips.push(kchip(shortReview(review.name), review.v, current && job.step === "review", false)));
  return el("div", { class: "round" }, el("span", { class: "r", text: "R" + row.n }), chips);
}
function cardHint(job) {
  if (!job.waiting) return "";
  if (job.reason) return String(job.reason).split("\n")[0].slice(0, 180);
  const lines = commands(job);
  return lines.length ? lines[0] : "";
}
function sameDrawer(job) {
  if (!state.drawer || state.drawer.id !== job.id) return false;
  if (!state.fleet) return true;
  return state.drawer.project === (job.project || null);
}
function cardNum(job) {
  const num = jobNum(job.id);
  if (state.fleet && state.selected === "all" && job.projectName) return job.projectName + " · " + num;
  return num;
}
function cardClass(job) {
  const parts = ["kcard"];
  if (sameDrawer(job)) parts.push("on");
  if (job.step === "rejected") parts.push("rejected");
  const step = stepOf(job.step, job);
  if (isEnd(job.step, job)) parts.push("done");
  else if (job.waiting) parts.push("human");
  else if (step && RUNNING.has(step.type)) parts.push("live");
  return parts.join(" ");
}
function jobCard(job) {
  const flag = flagOf(job);
  const hint = cardHint(job);
  const rounds = roundRows(job);
  const label = (job.title || job.id) + (state.fleet && state.selected === "all" && job.projectName ? ", " + job.projectName : "");
  return el("article", {
    class: cardClass(job), tabindex: 0, role: "button", "aria-label": label,
    onclick: (event) => { if (event.target.closest("a")) return; openDrawer(job); },
    onkeydown: (event) => {
      if (event.target.closest("a")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDrawer(job); }
    },
  },
    el("div", { class: "card-top" },
      cardClass(job).includes("live") ? el("span", { class: "pulse", "aria-hidden": "true" }) : null,
      el("span", { class: "num", text: cardNum(job) }),
      flag ? el("span", { class: "flag", text: flag }) : null),
    el("p", { class: "feature", text: job.title || job.id }),
    el("div", { class: "links" },
      link(job.issue_url, "issue #" + (job.issue == null ? "" : job.issue)),
      job.pr ? link(job.pr_url, "PR #" + job.pr) : null),
    rounds.map((row) => roundLine(job, row)),
    el("div", { class: "meta" },
      job.intake && job.intake.risk ? el("span", { class: riskClass(job.intake.risk), text: job.intake.risk }) : null,
      el("span", { text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "—" }),
      el("span", { text: elapsed(job) })),
    hint ? el("p", { class: "hint", text: hint }) : null);
}
const FILE_ORDER = ["issue.md", "intake.md", "spec.md", "build.md", "decision.md", "verify.md", "rejection.md"];
const REVIEW_ORDER = ["code", "security", "ux", "ui"];
function fileRank(path) {
  const exact = FILE_ORDER.indexOf(path);
  if (exact >= 0) return [0, exact, 0, path];
  const feedback = String(path).match(/^feedback-(\d+)\.md$/);
  if (feedback) return [1, Number(feedback[1]), 0, path];
  const round = String(path).match(/^round-(\d+)\/(.+)\.(md|log)$/);
  if (round) {
    const stem = round[2];
    let kind = 50;
    if (stem === "checks") kind = round[3] === "log" ? 1 : 0;
    else if (stem.startsWith("review-")) {
      const at = REVIEW_ORDER.indexOf(stem.slice(7));
      kind = 10 + (at >= 0 ? at : 9);
    } else if (stem.startsWith("debug")) kind = 20;
    return [2, Number(round[1]), kind, path];
  }
  return [3, 0, 0, path];
}
function sortedFiles(files) {
  return files.slice().sort((a, b) => {
    const left = fileRank(a.path), right = fileRank(b.path);
    return left[0] - right[0] || left[1] - right[1] || left[2] - right[2] || (left[3] < right[3] ? -1 : left[3] > right[3] ? 1 : 0);
  });
}
function fileGroup(path) {
  if (String(path).startsWith("round-")) return path.split("/")[0].replace("round-", "Round ");
  if (String(path).startsWith("feedback-")) return "Feedback";
  return "Job";
}
function fileLabel(path) {
  const round = String(path).match(/^round-(\d+)\/(.+)$/);
  if (!round) return path;
  const stem = round[2].replace(/\.md$/, "").replace(/\.log$/, "");
  if (stem === "checks" && String(path).endsWith(".log")) return "R" + round[1] + " checks.log";
  if (stem === "checks") return "R" + round[1] + " checks";
  if (stem.startsWith("review-")) return "R" + round[1] + " " + stem.slice(7);
  return "R" + round[1] + " " + stem;
}
function tone(first) {
  const line = String(first || "");
  if (/^(VERDICT:\s*PASS|GATE:\s*PASS|APPROVE|STATUS:\s*READY|DECISION:\s*ACCEPT|STATUS:\s*ok)/.test(line)) return "good";
  if (/^(VERDICT:\s*CHANGES|DECISION:\s*NEEDS-INFO)/.test(line)) return "warn";
  if (/^(ESCALATE|REJECTED|STATUS:\s*BLOCKED|GATE:\s*FAIL|DECISION:\s*REJECT|VERDICT:\s*FAIL)/.test(line)) return "stop";
  return "";
}
function closeDrawer() {
  state.drawer = null;
  render();
}
function fileUrl(job, path) {
  const tail = String(path).split("/").map(encodeURIComponent).join("/");
  if (state.fleet && job.project) {
    return "/projects/" + encodeURIComponent(job.project) + "/jobs/" + encodeURIComponent(job.id) + "/" + tail;
  }
  return "jobs/" + encodeURIComponent(job.id) + "/" + tail;
}
function openDrawer(job) {
  if (sameDrawer(job)) return;
  const files = sortedFiles(job.files || []);
  const path = files.length ? files[0].path : null;
  state.drawer = { project: job.project || null, id: job.id, path, text: path ? "Loading..." : "No files yet.", focus: true };
  render();
  if (path) fetchDrawer(job, path);
}
function pickFile(job, path) {
  if (!sameDrawer(job)) return;
  state.drawer.path = path;
  state.drawer.text = "Loading...";
  state.drawer.focus = false;
  render();
  fetchDrawer(job, path);
}
async function fetchDrawer(job, path) {
  let text;
  try {
    const res = await fetch(fileUrl(job, path), { cache: "no-store" });
    text = res.ok ? await res.text() : "Could not load " + path + " (" + res.status + ").";
  } catch (error) {
    text = "Could not load " + path + ".";
  }
  if (!sameDrawer(job) || !state.drawer || state.drawer.path !== path) return;
  state.drawer.text = text;
  render();
}
function clampSplit(px, box) {
  const max = Math.max(SPLIT_MIN, box.clientWidth - 280);
  return Math.min(max, Math.max(SPLIT_MIN, Math.round(px)));
}
function applySplit(box, px) {
  const width = clampSplit(px, box);
  state.split = width;
  box.style.setProperty("--split", width + "px");
  const bar = box.querySelector(".grip");
  if (bar) bar.setAttribute("aria-valuenow", String(width));
  return width;
}
function startSplit(event) {
  if (event.button !== 0) return;
  const bar = event.currentTarget;
  const box = bar.parentElement;
  if (!box) return;
  event.preventDefault();
  const origin = event.clientX;
  const start = state.split;
  bar.classList.add("on");
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
  function move(ev) {
    applySplit(box, start + (ev.clientX - origin));
  }
  function end() {
    bar.classList.remove("on");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", end);
    document.removeEventListener("pointercancel", end);
    writeSplit(state.split);
  }
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);
}
function keySplit(event) {
  const box = event.currentTarget.parentElement;
  if (!box) return;
  let next = state.split;
  if (event.key === "ArrowLeft") next -= 20;
  else if (event.key === "ArrowRight") next += 20;
  else if (event.key === "Home") next = SPLIT_MIN;
  else if (event.key === "End") next = box.clientWidth - 280;
  else return;
  event.preventDefault();
  writeSplit(applySplit(box, next));
}
function resetSplit(event) {
  const box = event.currentTarget.parentElement;
  if (box) writeSplit(applySplit(box, SPLIT_DEFAULT));
}
function grip() {
  return el("div", {
    class: "grip", role: "separator", title: "Drag to resize", tabindex: "0",
    "aria-orientation": "vertical", "aria-label": "Resize the file list",
    "aria-valuemin": String(SPLIT_MIN), "aria-valuenow": String(state.split),
    onpointerdown: startSplit, onkeydown: keySplit, ondblclick: resetSplit,
  });
}
function splitStyle() {
  return "--split:" + state.split + "px";
}
function fileList(job, files) {
  const box = el("div", { class: "d-files" });
  let group = "";
  files.forEach((file) => {
    const name = fileGroup(file.path);
    if (name !== group) {
      group = name;
      box.append(el("div", { class: "d-group", text: name }));
    }
    const mark = tone(file.first_line);
    box.append(el("button", {
      type: "button", class: "d-file" + (state.drawer.path === file.path ? " on" : ""),
      "aria-pressed": String(state.drawer.path === file.path),
      onclick: () => pickFile(job, file.path),
    }, el("i", { class: "dot" + (mark ? " " + mark : "") }), el("span", { text: fileLabel(file.path) })));
  });
  if (!files.length) box.append(el("p", { class: "kempty", text: "No files yet." }));
  return box;
}
function drawer() {
  if (!state.drawer) return null;
  const job = jobs().find((item) => sameDrawer(item));
  const close = el("button", { type: "button", class: "close", "data-close": "true", text: "close", onclick: closeDrawer });
  if (!job) {
    return el("div", { class: "overlay" }, el("div", { class: "scrim", onclick: closeDrawer }),
      el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": "Job" },
        el("div", { class: "d-head" }, el("h3", { text: "That job is gone." }), close)));
  }
  const files = sortedFiles(job.files || []);
  return el("div", { class: "overlay" },
    el("div", { class: "scrim", onclick: closeDrawer }),
    el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "d-title" },
      el("div", { class: "d-head" },
        el("div", { class: "num", text: (state.fleet && state.selected === "all" && job.projectName ? job.projectName + " · " : "") + job.id + " · " + (job.step || "") + (job.round ? " · round " + job.round : "") }),
        el("h3", { id: "d-title", text: job.title || job.id }),
        el("div", { class: "links" },
          link(job.issue_url, "issue #" + (job.issue == null ? "" : job.issue)),
          job.pr ? link(job.pr_url, "PR #" + job.pr) : null),
        close),
      el("div", { class: "d-body", style: splitStyle() }, fileList(job, files), grip(), el("pre", { class: "file d-doc", tabindex: 0, text: state.drawer.text || "" }))));
}
function queueCard(row) {
  const who = state.fleet && state.selected === "all" && row.projectName ? row.projectName + " · " : "";
  return el("article", { class: "kcard" },
    el("div", { class: "card-top" },
      el("span", { class: "num", text: who + (row.action || "new") }),
      el("span", { class: "flag", text: "#" + (row.issue || "") })),
    el("p", { class: "feature", text: row.note || ("issue #" + (row.issue || "")) }));
}
function column(col, shown) {
  const queued = col.queue ? col.queue.filter(queueMatches) : null;
  const mine = queued || shown.filter((job) => col.steps.includes(job.step));
  const who = columnWho(col);
  return el("section", { class: "kcol" + (col.cls ? " " + col.cls : ""), "aria-label": col.label },
    el("div", { class: "kcol-head" },
      el("h3", { text: col.label }),
      who ? el("span", { class: "who", text: who }) : null,
      el("span", { class: "count", text: String(mine.length) })),
    mine.length ? mine.map((item) => col.queue ? queueCard(item) : jobCard(item)) : el("p", { class: "kempty", text: "—" }));
}
function boardSub() {
  if (!state.fleet) return (state.index.project && state.index.project.description) || "Jobs by step";
  const projects = visibleProjects();
  if (projects.length === 1) return projects[0].description || projects[0].path || "Jobs by step";
  return projects.length + " projects";
}
function problemLines() {
  if (!state.fleet) return [];
  const lines = [];
  visibleProjects().forEach((project) => {
    if (project.error) lines.push((state.selected === "all" ? (project.name || project.id) + ": " : "") + String(project.error));
  });
  state.queueErrors.forEach((item) => {
    const text = String(item);
    if (state.selected === "all" || text.indexOf(state.selected + ": ") === 0) lines.push(text);
  });
  return lines.map((text) => el("p", { class: "error", text }));
}
function board() {
  const all = jobs();
  const shown = all.filter(matches);
  const cols = boardColumns(all);
  const nodes = [
    el("div", { class: "head" },
      el("div", {}, el("h1", { text: "Board" }), el("p", { class: "sub", text: boardSub() })),
      el("span", { class: "src", text: shown.length + " of " + all.length })),
    ...problemLines(),
    cols.length ? el("div", { class: "kanban" }, cols.map((col) => column(col, shown))) : el("p", { class: "kempty", text: state.fleet && !state.projects.length ? "no projects yet" : "no jobs" }),
  ];
  const panel = drawer();
  if (panel) nodes.push(panel);
  return nodes;
}
// ---- pipeline ----
function order(graph) {
  // The first edge of each step is its normal way on; that chain is the spine.
  // A step off the spine sits right under the last spine step that leads to it,
  // except the escalation step: any step can lead there, so it goes last.
  const steps = new Map(graph.steps.map((s) => [s.name, s]));
  const spine = [];
  let name = graph.start;
  while (name && steps.has(name) && !spine.includes(name)) {
    spine.push(name);
    const step = steps.get(name);
    const next = step.enabled === false ? step.edges.find((e) => e.outcome === step.skip) : step.edges.find((e) => !spine.includes(e.to));
    name = next ? next.to : null;
  }
  const out = spine.slice();
  let moved = true;
  while (moved) {
    moved = false;
    for (const step of graph.steps) {
      if (out.includes(step.name) || step.name === graph.escalate_to) continue;
      const from = out.filter((n) => steps.get(n).edges.some((e) => e.to === step.name));
      if (!from.length) continue;
      let at = Math.max(...from.map((n) => out.indexOf(n))) + 1;
      while (at < out.length && !spine.includes(out[at])) at++;
      out.splice(at, 0, step.name);
      moved = true;
    }
  }
  for (const step of graph.steps) if (!out.includes(step.name)) out.push(step.name);
  const ends = graph.end_states.slice().sort((x, y) => (x === "done" ? -1 : y === "done" ? 1 : x < y ? -1 : 1));
  return out.concat(ends);
}
function lanes(groups) {
  // Shortest runs sit closest to the steps; runs that do not overlap share a lane.
  const taken = [];
  for (const group of groups.slice().sort((x, y) => (x.hi - x.lo) - (y.hi - y.lo))) {
    let lane = 0;
    while ((taken[lane] || []).some((g) => group.lo < g.hi + 8 && g.lo < group.hi + 8)) lane++;
    (taken[lane] = taken[lane] || []).push(group);
    group.lane = lane;
  }
  return taken.length;
}
function pipelineGraph(graph, count) {
  const NW = 172, LABEL = 106, LANE = 12, PORT = 16, GAP = 14, PAD = 12, R = 5;
  const names = order(graph), index = new Map(names.map((n, i) => [n, i]));
  const nodes = new Map();
  let y = PAD;
  for (const name of names) {
    const step = (graph.steps || []).find((item) => item.name === name) || null;
    const edges = step ? step.edges.filter((e) => index.has(e.to)) : [];
    const fwd = edges.filter((e) => index.get(e.to) > index.get(name)), back = edges.filter((e) => index.get(e.to) <= index.get(name));
    const h = Math.max(42, 26 + PORT * (Math.max(fwd.length, back.length, 1) - 1) + 12);
    nodes.set(name, { name, step, y, h, fwd, back });
    y += h + GAP;
  }
  const H = y - GAP + PAD;
  const runs = [];
  for (const node of nodes.values()) {
    for (const [side, list] of [["fwd", node.fwd], ["back", node.back]]) {
      list.forEach((edge, i) => {
        const target = nodes.get(edge.to);
        runs.push({ side, from: node.name, to: edge.to, edge, y1: node.y + 26 + PORT * i, y2: target.y + 9,
          off: node.step.enabled === false && edge.outcome !== node.step.skip });
      });
    }
  }
  const groups = { fwd: new Map(), back: new Map() };
  for (const run of runs) {
    const group = groups[run.side].get(run.to) || { lo: Infinity, hi: -Infinity };
    group.lo = Math.min(group.lo, run.y1, run.y2);
    group.hi = Math.max(group.hi, run.y1, run.y2);
    groups[run.side].set(run.to, group);
  }
  const leftLanes = lanes([...groups.back.values()]), rightLanes = lanes([...groups.fwd.values()]);
  const xl = PAD + Math.max(0, leftLanes - 1) * LANE + (leftLanes ? LABEL : 0), xr = xl + NW;
  const W = xr + (rightLanes ? LABEL : 0) + Math.max(0, rightLanes - 1) * LANE + PAD;

  const root = svg("svg", { class: "graph", viewBox: `0 0 ${W} ${H}`, style: `width:${W}px`, role: "img",
    "aria-label": "The pipeline's steps and the edges between them. The Steps table lists the same." });
  const marker = (id, colour) => svg("marker", { id, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: "auto" }, svg("path", { d: "M0,0 L8,4 L0,8 z", fill: colour }));
  root.append(svg("defs", {}, marker("arrow", "#8a8a8a"), marker("arrow-back", "#c9836f"), marker("arrow-off", "#3a3a3a")));
  const wires = svg("g", {}), labels = svg("g", {});
  for (const run of runs.sort((a, b) => Number(b.off) - Number(a.off))) {
    const right = run.side === "fwd", dir = right ? 1 : -1, down = run.y2 > run.y1 ? 1 : -1;
    const x0 = right ? xr : xl, lane = x0 + dir * (LABEL + groups[run.side].get(run.to).lane * LANE);
    const d = `M${x0},${run.y1} H${lane - dir * R} Q${lane},${run.y1} ${lane},${run.y1 + down * R} V${run.y2 - down * R} Q${lane},${run.y2} ${lane - dir * R},${run.y2} H${x0 + dir * 2}`;
    const kind = run.off ? " off" : right ? "" : " back";
    wires.append(svg("path", { class: "edge" + kind, d, "marker-end": `url(#arrow${run.off ? "-off" : right ? "" : "-back"})`, "data-from": run.from, "data-to": run.to }));
    const text = run.edge.outcome + (run.edge.max !== null && run.edge.max !== undefined ? ` · max ${run.edge.max}` : "");
    labels.append(svg("circle", { class: "port" + kind, cx: x0, cy: run.y1, r: 2.5, "data-from": run.from, "data-to": run.to }),
      svg("text", { class: "elabel" + kind, x: x0 + dir * 8, y: run.y1 - 4, "text-anchor": right ? "start" : "end", text, "data-from": run.from, "data-to": run.to }));
  }
  root.append(wires);
  for (const node of nodes.values()) {
    const { name, step, h } = node, n = count(name), end = !step;
    const waiting = step && step.type === "human" && n > 0;
    const kind = end ? "end" : step.type + (step.who ? " · " + step.who : "") + (step.enabled === false ? " · off" : "");
    const group = svg("g", { class: "node" + (end ? " end" : "") + (step && step.enabled === false ? " off" : "") + (waiting ? " wait" : "") });
    group.append(svg("rect", { x: xl, y: node.y, width: NW, height: h, rx: 3 }),
      svg("text", { class: "name", x: xl + 11, y: node.y + 18, text: name.length > 20 ? name.slice(0, 19) + "…" : name }),
      svg("text", { class: "kind", x: xl + 11, y: node.y + 32, text: kind }));
    if (n) group.append(svg("g", { class: "count" }, svg("rect", { x: xr - 34, y: node.y + 9, width: 24, height: 18, rx: 9 }), svg("text", { x: xr - 22, y: node.y + 22, "text-anchor": "middle", text: n })));
    group.append(svg("title", { text: `${name}: ${n} job${n === 1 ? "" : "s"}` }));
    const focus = (on) => {
      root.classList.toggle("focus", on);
      for (const part of root.querySelectorAll("[data-from]")) part.classList.toggle("hot", on && (part.dataset.from === name || part.dataset.to === name));
    };
    group.addEventListener("mouseenter", () => focus(true));
    group.addEventListener("mouseleave", () => focus(false));
    root.append(group);
  }
  root.append(labels);
  return root;
}
function pipelineDiagram(graph, jobList) {
  if (!graph || !Array.isArray(graph.steps) || !Array.isArray(graph.end_states)) {
    return [el("div", { class: "notice", text: "No pipeline for this project yet." })];
  }
  let drawn;
  try {
    const count = (name) => jobList.filter((j) => j.step === name).length;
    const off = (step) => step.enabled === false;
    const stepRows = graph.steps.map((step) => el("tr", {},
      el("td", { class: "mono", text: step.name }),
      el("td", {}, el("span", { class: "chip" + (off(step) ? " off" : ""), text: step.type + (off(step) ? " · off" : "") }),
        step.who ? el("div", { class: "dim", style: "font-size:11px;margin-top:4px", text: step.who }) : null),
      el("td", { class: "mono wrap" }, (step.edges || []).map((e) => el("div", { class: off(step) && e.outcome !== step.skip ? "dim" : "" },
        e.outcome, el("span", { class: "dim", text: " → " }), e.to, e.max !== null && e.max !== undefined ? el("span", { class: "dim", text: `  max ${e.max}` }) : null))),
      el("td", { class: "mono dim", text: count(step.name) || "-" })));
    drawn = [el("div", { class: "pipe" },
      el("div", { class: "panel" }, el("div", { class: "scroll" }, pipelineGraph(graph, count)),
        el("div", { class: "legend" }, el("span", {}, el("i"), "on to a later step"), el("span", {}, el("i", { class: "back" }), "back to an earlier step"),
          el("span", {}, el("i", { class: "box" }), "waiting for a person"), el("span", {}, el("i", { class: "box off" }), "step is off"))),
      el("div", { class: "panel" }, el("h2", { text: "Steps" }), el("p", { class: "sub", text: "As set in factory.toml over the defaults" }),
        el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["Step", "Type", "Outcomes", "Jobs"].map((t) => el("th", { scope: "col", text: t })))), el("tbody", {}, stepRows)))))];
  } catch (error) {
    drawn = [el("div", { class: "notice", text: "This pipeline could not be drawn." })];
  }
  return drawn;
}
function pipelineIntro(graph) {
  return el("p", { class: "sub", text: `Starts at ${graph.start}. When an edge's max is used up, the job goes to ${graph.escalate_to}.` });
}
function pipeline() {
  if (!state.fleet) {
    return [el("h1", { text: "Pipeline" }), pipelineIntro(state.graph), ...pipelineDiagram(state.graph, jobs())];
  }
  const projects = visibleProjects();
  if (projects.length <= 1) {
    const project = projects[0];
    const nodes = [el("h1", { text: "Pipeline" })];
    if (!project) return nodes.concat([el("p", { class: "kempty", text: "no projects yet" })]);
    if (project.error) nodes.push(el("p", { class: "error", text: String(project.error) }));
    if (project.graph) nodes.push(pipelineIntro(project.graph));
    nodes.push(...pipelineDiagram(project.graph, jobs()));
    return nodes;
  }
  const nodes = [
    el("h1", { text: "Pipeline" }),
    el("p", { class: "sub", text: "One pipeline per project. Each factory.toml can differ." }),
  ];
  projects.forEach((project) => {
    const mine = jobs().filter((job) => job.project === project.id);
    nodes.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.path) nodes.push(el("p", { class: "sub", text: project.path }));
    if (project.error) nodes.push(el("p", { class: "error", text: String(project.error) }));
    if (project.graph) nodes.push(pipelineIntro(project.graph));
    nodes.push(...pipelineDiagram(project.graph, mine));
  });
  return nodes;
}

// ---- jobs ----
function sameFile(job) {
  return !!(state.file && state.file.job === job.id && (state.file.project || null) === (job.project || null));
}
async function showFile(job, path) {
  state.file = { job: job.id, project: job.project || null, path, text: "Loading..." };
  render();
  let text;
  try {
    const res = await fetch(fileUrl(job, path), { cache: "no-store" });
    text = res.ok ? await res.text() : `Could not load ${path} (${res.status}).`;
  } catch (e) {
    text = `Could not load ${path}.`;
  }
  if (sameFile(job) && state.file.path === path) { state.file.text = text; render(); }
}
function jobDetail(job) {
  const step = stepOf(job.step, job);
  const parts = [];
  parts.push(el("div", { class: "panel" },
    el("div", { class: "head" }, el("div", { style: "min-width:0" }, el("h2", { style: "font-size:17px;overflow-wrap:anywhere", text: job.title }), el("div", { class: "mono dim", style: "margin-top:3px", text: (state.fleet && state.selected === "all" && job.projectName ? job.projectName + " · " : "") + job.id })), chip(job)),
    el("div", { class: "facts" },
      el("span", {}, "Issue ", link(job.issue_url, "#" + job.issue)),
      job.pr ? el("span", {}, "PR ", link(job.pr_url, "#" + job.pr)) : null,
      el("span", {}, "By ", el("b", { text: "@" + job.author })),
      job.intake ? el("span", {}, "Risk ", el("b", { text: job.intake.risk })) : null,
      job.intake ? el("span", {}, "Surfaces ", el("b", { text: (job.intake.surfaces || []).join(", ") || "none" })) : null,
      el("span", {}, "Round ", el("b", { text: `${job.round || 0}` + (job.attempt > 1 ? `, attempt ${job.attempt}` : "") })),
      job.cost_usd !== null && job.cost_usd !== undefined ? el("span", {}, "Cost ", el("b", { text: money(job.cost_usd) })) : null,
      el("span", {}, "Started ", el("b", { text: ago(job.created_at) })))));

  if (job.waiting) {
    parts.push(el("div", { class: "ask" },
      el("h2", { text: `Waiting for ${job.waiting.who === "author" ? "the author, @" + job.author : "a maintainer"} since ${ago(job.waiting.since)}` }),
      el("p", { text: job.reason || `Step ${job.step} needs a reply. Type one of these in your coding tool, or comment it on the ${job.waiting.asked_on === "pr" ? "pull request" : "issue"} without the number.` }),
      commands(job).map((cmd) => el("div", { class: "cmd" }, el("code", { text: cmd }),
        el("button", { type: "button", text: "Copy", onclick: async (e) => {
          const literal = cmd.replace(/ <[^>]+>$/, " ");
          try { await navigator.clipboard.writeText(literal); e.target.textContent = "Copied"; } catch (_) { e.target.textContent = "Select it"; }
          setTimeout(() => { e.target.textContent = "Copy"; }, 1500);
        } })))));
  } else if (job.reason && isEnd(job.step, job) && job.step !== "done") {
    parts.push(el("div", { class: "notice", style: "white-space:pre-wrap;overflow-wrap:anywhere", text: job.reason }));
  }

  const items = (job.history || []).map((h) => {
    const bad = /^(FAIL|FAILED|REJECT|CHANGES|BLOCKED|ESCALATE|UNKNOWN|NEEDS-INFO)$/.test(h.outcome);
    return el("li", { class: bad ? "ko" : "ok" }, el("span", { class: "mark" }), el("span", { class: "mono", text: h.step }),
      el("span", { class: "what" }, el("span", { class: "mono", text: h.outcome + " → " + h.to }),
        h.skipped && h.skipped.length ? el("span", { class: "dim", text: "  skipped " + h.skipped.join(", ") }) : null,
        h.reason ? el("div", { class: "dim", style: "font-size:12px", text: h.reason.split("\n")[0].slice(0, 200) }) : null),
      el("span", { class: "when", text: span((time(h.at) - time(h.entered_at)) / 1000) + " · " + ago(h.at) }));
  });
  if (!isEnd(job.step, job)) {
    items.push(el("li", { class: "now" }, el("span", { class: "mark" }), el("span", { class: "mono", text: job.step }),
      el("span", { class: "what dim", text: job.waiting ? "waiting for a person" : step ? `${step.type} step, in progress` : "this step is no longer in factory.toml" }),
      el("span", { class: "when", text: "since " + ago(job.entered_at) })));
  }
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Steps" }), el("p", { class: "sub", text: "In the order the job took them" }), el("ol", { class: "steps" }, items)));

  const files = job.files || [];
  const open = sameFile(job) ? state.file : null;
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Files" }), el("p", { class: "sub", text: "What each step wrote, with its first line" }),
    files.length ? el("div", { class: "files", style: splitStyle() },
      el("div", { class: "filelist" }, files.map((f) => el("button", { type: "button", "aria-pressed": String(!!open && open.path === f.path), onclick: () => showFile(job, f.path) },
        el("span", { class: "path", text: f.path }), el("span", { class: "verdict", text: f.first_line || "" })))),
      grip(),
      el("pre", { class: "file", tabindex: 0, text: open ? open.text : "Pick a file to read it." }))
      : el("div", { class: "empty", text: "No files yet." })));
  return el("div", { class: "detail" }, parts);
}
function jobHref(job) {
  if (state.fleet) return "#/jobs/" + encodeURIComponent(job.project) + "/" + encodeURIComponent(job.id);
  return "#/jobs/" + encodeURIComponent(job.id);
}
function sameJob(job, projectId, id) {
  if (!job || !id || job.id !== id) return false;
  if (!state.fleet || !projectId) return true;
  return job.project === projectId;
}
function queueLabel() {
  if (state.index && state.index.labels && state.index.labels.queue) return state.index.labels.queue;
  for (const project of visibleProjects()) {
    if (project.labels && project.labels.queue) return project.labels.queue;
  }
  return "factory";
}
function jobsView(id, projectId) {
  const all = jobs(), shown = all.filter(matches);
  const current = all.find((job) => sameJob(job, projectId, id)) || shown[0] || null;
  if (!all.length) return [el("h1", { text: "Jobs" }), el("p", { class: "sub", text: "One job per labeled issue" }), el("div", { class: "notice" }, "No jobs yet. Label an issue ", el("code", { text: queueLabel() }), " and run ", el("code", { text: "/mfactory run" }), " in your coding tool.")];
  return [
    el("h1", { text: "Jobs" }),
    el("p", { class: "sub", text: `${shown.length} of ${all.length}` + (state.query ? " match the search" : "") }),
    el("div", { class: "jobview" },
      el("nav", { class: "panel joblist", "aria-label": "Jobs" }, shown.length ? shown.map((job) => el("a", { href: jobHref(job), "aria-current": !!current && sameJob(job, current.project || null, current.id) ? "true" : null },
        el("div", { class: "id", text: state.fleet && state.selected === "all" ? (job.projectName || job.project) + " · " + job.id : job.id }),
        el("div", { class: "meta" }, el("span", { text: "#" + job.issue + " · " + ago(job.updated_at) }), chip(job)))) : el("div", { class: "empty", text: "No job matches." })),
      current ? jobDetail(current) : el("div", { class: "notice", text: "Pick a job." })),
  ];
}

// ---- config ----
function configRows(rows) {
  const q = state.query.trim().toLowerCase();
  return rows.filter((r) => (state.source === "all" || r.source === state.source) && (!q || String(r.key).toLowerCase().includes(q) || JSON.stringify(r.value).toLowerCase().includes(q)));
}
function configTable(rows) {
  if (!rows) return el("div", { class: "empty", text: "No config has been written for this project yet." });
  const shown = configRows(rows);
  if (!shown.length) return el("div", { class: "empty", text: "No setting matches." });
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Key", "Value", "Source"].map((t) => el("th", { scope: "col", text: t })))),
    el("tbody", {}, shown.map((r) => el("tr", {}, el("td", { class: "mono", text: r.key }), el("td", { class: "mono wrap", text: JSON.stringify(r.value) }),
      el("td", {}, el("span", { class: "src" + (r.source === "factory.toml" ? " mine" : ""), text: r.source })))))));
}
function sourceFilters(rows) {
  const sources = ["all", ...new Set((rows || []).map((r) => r.source).filter((source) => source))];
  return el("div", { class: "filters", role: "group", "aria-label": "Source" }, sources.map((s) => el("button", {
    type: "button", "aria-pressed": String(state.source === s), text: s, onclick: () => { state.source = s; render(); },
  })));
}
function configView() {
  const intro = [
    el("h1", { text: "Config" }),
    el("p", { class: "sub", text: "factory.toml merged over the defaults, and where each value comes from" }),
  ];
  if (!state.fleet) {
    if (!state.config) return [el("h1", { text: "Config" }), el("p", { class: "sub", text: "Loading..." })];
    return intro.concat([sourceFilters(state.config), el("div", { class: "panel" }, configTable(state.config))]);
  }
  const projects = visibleProjects();
  if (!projects.length) return intro.concat([el("p", { class: "kempty", text: "no projects yet" })]);
  const allRows = projects.flatMap((project) => project.config || []);
  intro.push(sourceFilters(allRows));
  projects.forEach((project) => {
    if (projects.length > 1) intro.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.error) intro.push(el("p", { class: "error", text: String(project.error) }));
    intro.push(el("div", { class: "panel" }, configTable(project.config)));
  });
  return intro;
}

// ---- shell ----
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter((part) => part.length);
  const view = ["board", "pipeline", "jobs", "config"].includes(parts[0]) ? parts[0] : "board";
  if (view === "jobs" && state.fleet && parts.length >= 3) {
    return { view, project: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2]) };
  }
  return { view, project: null, id: view === "jobs" && parts[1] ? decodeURIComponent(parts[1]) : null };
}
function normalizeSelection() {
  if (!state.fleet || !state.projects.length) return;
  if (state.selected !== "all" && !state.projects.some((project) => project.id === state.selected)) {
    state.selected = "all";
    writeChoice("all");
  }
}
function filterButton(id, text) {
  return el("button", {
    type: "button", text, "aria-pressed": state.selected === id ? "true" : "false",
    onclick: () => { state.selected = id; writeChoice(id); render(); },
  });
}
function renderFilters() {
  const box = document.getElementById("filters");
  if (!box) return;
  if (!state.fleet || state.projects.length < 2) {
    box.replaceChildren();
    return;
  }
  const buttons = [filterButton("all", "all")];
  state.projects.forEach((project) => buttons.push(filterButton(project.id, projectLabel(project))));
  box.replaceChildren(...buttons);
}
function connectionLabel() {
  if (state.error) return "not connected";
  if (state.fleet && state.queueStale) return "queue stale";
  if (state.fleet) return state.projects.length ? "live" : "connecting";
  if (!state.index) return "connecting";
  return "live";
}
function paintLive() {
  const live = document.getElementById("live");
  if (!live) return;
  const stale = !!(state.error || (state.fleet && state.queueStale));
  live.className = "live" + (stale ? " stale" : "");
  const label = connectionLabel();
  const status = document.getElementById("live-status");
  const clock = document.getElementById("clock");
  if (status) status.textContent = label;
  if (clock) clock.textContent = label === "live" ? new Date().toLocaleTimeString() : "";
}
function fleetTitle() {
  const projects = visibleProjects();
  if (state.selected !== "all" && projects[0]) return projects[0].name || projects[0].id;
  return "all";
}
function fleetFoot() {
  const projects = visibleProjects();
  const versions = [...new Set(projects.map((project) => project.version).filter((version) => version))];
  const tools = [...new Set(projects.flatMap((project) => project.tools || []))];
  const bits = ["factory" + (versions.length ? " " + versions.join(", ") : "")];
  if (tools.length) bits.push(tools.join(", "));
  return bits.join(" · ");
}
function render() {
  normalizeSelection();
  renderFilters();
  const { view, id, project } = route();
  document.querySelectorAll("[data-view]").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  paintLive();
  const target = document.getElementById("view");
  const keep = document.activeElement && target.contains(document.activeElement) ? null : document.activeElement;
  const scrollers = ".joblist, pre.file, .d-doc, .d-files, .filelist";
  const scroll = [...target.querySelectorAll(scrollers)].map((n) => n.scrollTop);
  let content;
  const ready = state.fleet ? !state.error || state.projects.length : !!(state.index && state.graph);
  if (!ready) {
    content = [el("h1", { text: "factory" }), el("div", { class: "notice" }, state.error
      ? el("span", {}, "The dashboard's data could not be loaded: ", el("code", { text: state.error }), ". Start it with ", el("code", { text: ".factory/bin/mfactory dashboard" }), ".")
      : "Loading...")];
    document.getElementById("foot-left").textContent = FOOT_NOTE;
    document.getElementById("foot-right").textContent = "";
  } else if (state.fleet) {
    const totals = boardTotals(jobs());
    document.getElementById("foot-left").textContent = view === "board"
      ? totals.running + " running · " + money(totals.cost) + " · " + FOOT_NOTE
      : FOOT_NOTE;
    document.getElementById("foot-right").textContent = fleetFoot();
    document.title = "factory · " + fleetTitle();
    content = view === "pipeline" ? pipeline() : view === "jobs" ? jobsView(id, project) : view === "config" ? configView() : board();
  } else {
    const totals = boardTotals(jobs());
    document.getElementById("foot-left").textContent = view === "board"
      ? totals.running + " running · " + money(totals.cost) + " · " + FOOT_NOTE
      : FOOT_NOTE;
    document.getElementById("foot-right").textContent = `factory ${state.index.version} · ${state.index.tools.join(", ")}`;
    document.title = `factory · ${state.index.repo}`;
    content = view === "pipeline" ? pipeline() : view === "jobs" ? jobsView(id, project) : view === "config" ? configView() : board();
  }
  target.replaceChildren(...content);
  [...target.querySelectorAll(scrollers)].forEach((n, i) => { if (scroll[i]) n.scrollTop = scroll[i]; });
  if (state.drawer && state.drawer.focus) {
    const close = target.querySelector("[data-close]");
    if (close) close.focus();
    state.drawer.focus = false;
  } else if (keep && keep !== document.body) keep.focus({ preventScroll: true });
}
async function load(path) {
  const res = await fetch(path, { cache: "no-store" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}
function openJob() {
  if (!state.drawer || !state.drawer.path || route().view !== "board") return null;
  return jobs().find((job) => sameDrawer(job)) || null;
}
function refetchDrawer() {
  const open = openJob();
  if (open) fetchDrawer(open, state.drawer.path);
}
let probed = false;
let queueTimer = null;
async function refreshQueue() {
  if (!state.fleet) return;
  try {
    const body = await load("/queue.json");
    const rows = body.rows || [];
    const errors = body.errors || [];
    const changed = JSON.stringify([rows, errors]) !== JSON.stringify([state.queueRows, state.queueErrors]);
    state.queueRows = rows;
    state.queueErrors = errors;
    state.queueStale = false;
    if (changed) render();
    else paintLive();
  } catch (error) {
    state.queueStale = true;
    render();
  }
}
async function refreshFleet() {
  const body = await load("/projects.json");
  const next = body.projects || [];
  const changed = JSON.stringify(next) !== JSON.stringify(state.projects);
  state.projects = next;
  const hadError = state.error;
  state.error = null;
  if (changed || hadError || route().view === "config") {
    render();
    refetchDrawer();
  } else {
    paintLive();
  }
}
async function refreshSingle() {
  const [index, graph] = await Promise.all([load("jobs/index.json"), load("jobs/graph.json")]);
  const changed = JSON.stringify([index.jobs, graph]) !== JSON.stringify([state.index && state.index.jobs, state.graph]);
  state.index = index; state.graph = graph;
  const hadError = state.error; state.error = null;
  if (route().view === "config" || !state.config) state.config = await load("jobs/config.json");
  // Re-drawing only on change keeps selections and scroll positions while you read.
  if (changed || hadError || route().view === "config") {
    render();
    refetchDrawer();
  } else paintLive();
}
async function tryProjects() {
  // false: this server has no fleet. null: try again next tick. object: the fleet.
  try {
    const res = await fetch("/projects.json", { cache: "no-store" });
    if (res.status === 404) return false;
    if (!res.ok) return null;
    const body = await res.json();
    if (!body || !Array.isArray(body.projects)) return false;
    return body;
  } catch (error) {
    return null;
  }
}
async function refresh() {
  try {
    if (!state.fleet && !probed) {
      const found = await tryProjects();
      if (found === null) return;
      if (found) {
        probed = true;
        state.fleet = true;
        state.projects = found.projects;
        state.error = null;
        render();
        refetchDrawer();
        refreshQueue();
        if (!queueTimer) queueTimer = setInterval(refreshQueue, QUEUE_MS);
        return;
      }
      probed = true;
    }
    if (state.fleet) {
      await refreshFleet();
      return;
    }
    await refreshSingle();
  } catch (e) {
    state.error = String(e.message || e);
    render();
  }
}
/* boot */
const search = document.getElementById("search");
if (/Mac|iPhone|iPad/.test(navigator.platform)) document.getElementById("hotkey").textContent = "⌘ K";
search.addEventListener("input", () => {
  state.query = search.value;
  if (route().view === "pipeline") location.hash = "#/jobs"; else render();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); search.focus(); search.select(); }
  else if (e.key === "Escape" && document.activeElement === search) { search.value = ""; state.query = ""; search.blur(); render(); }
  else if (e.key === "Escape" && state.drawer && route().view === "board") closeDrawer();
});
window.addEventListener("hashchange", () => { if (route().view === "config") refresh(); render(); });
render();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(paintLive, 1000);
