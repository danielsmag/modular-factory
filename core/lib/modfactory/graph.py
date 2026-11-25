"""The pipeline as a graph: step types, outcome edges, limits and the checks that keep it finite."""

import re

TYPES = ("agent", "checks", "review", "human", "merge", "verify")
END_STATES = {"done", "rejected", "failed"}
OUTCOME = re.compile(r"[A-Z][A-Z0-9-]*")

# Outcomes a step must have an edge for. Custom agent steps and human steps
# return whatever their `on` table lists.
ROLE_OUTCOMES = {
    "intake": {"ACCEPT", "NEEDS-INFO", "REJECT"},
    "spec": {"DONE"},
    "build": {"READY", "BLOCKED"},
    "debug": {"FOUND", "UNKNOWN"},
    "approve": {"APPROVE", "ESCALATE"},
}
TYPE_OUTCOMES = {
    "checks": {"PASS", "FAIL"},
    "review": {"PASS", "CHANGES"},
    "merge": {"MERGED", "FAILED"},
    "verify": {"PASS", "FAIL", "BLOCKED"},
}


def role(name, step):
    """What an agent step's agent does: a built-in role, or `custom`."""
    return step.get("role") or (name if name in ROLE_OUTCOMES else "custom")


def fixed_outcomes(name, step):
    if step["type"] == "agent":
        return ROLE_OUTCOMES.get(role(name, step))
    return TYPE_OUTCOMES.get(step["type"])


def enabled(step):
    return step.get("enabled", True)


def live_edges(step):
    """The edges a job can leave this step by: all of them, or only `skip` when it is off."""
    edges = step.get("on", {})
    if enabled(step):
        return edges
    return {step["skip"]: edges[step["skip"]]} if step.get("skip") in edges else {}


def resolve(steps, target):
    """Follow disabled steps from `target` to the first step that runs, or an end state."""
    skipped = []
    while target not in END_STATES and not enabled(steps[target]):
        if target in skipped:
            raise ValueError(f"disabled steps loop: {' -> '.join(skipped + [target])}")
        skipped.append(target)
        target = steps[target]["on"][steps[target]["skip"]]["to"]
    return target, skipped


def problems(data):
    """Every way the graph is broken, each naming the key to fix."""
    steps, run, out = data.get("steps", {}), data.get("run", {}), []
    valid = {n: s for n, s in steps.items() if isinstance(s, dict) and s.get("type") in TYPES}
    if not valid:
        return ["[steps] is empty: the pipeline needs at least one step"]

    for name, step in valid.items():
        label = f"[steps.{name}]"
        edges = step.get("on")
        if not isinstance(edges, dict) or not edges:
            out.append(f"{label}.on is missing: say where each outcome goes")
            continue
        for outcome, edge in edges.items():
            where = f"{label}.on.{outcome}"
            if not OUTCOME.fullmatch(outcome):
                out.append(f"{where}: an outcome is capital letters, digits and hyphens")
            if not isinstance(edge, dict) or not isinstance(edge.get("to"), str):
                out.append(f'{where} must be a step name, or {{ to = "step", max = N }}')
                continue
            for key, value in edge.items():
                if key not in ("to", "max"):
                    out.append(f"{where}.{key} is not a setting. Known: to, max")
                elif key == "max" and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    out.append(f"{where}.max must be a whole number of 0 or more")
            if edge["to"] not in valid and edge["to"] not in END_STATES:
                out.append(f'{where} points at unknown step "{edge["to"]}". Steps: '
                           f"{', '.join(valid)}; end states: {', '.join(sorted(END_STATES))}")
        wanted = fixed_outcomes(name, step)
        if wanted is not None:
            kind = f"{role(name, step)} agent" if step["type"] == "agent" else step["type"]
            if wanted - edges.keys():
                out.append(f"{label}.on has no edge for {', '.join(sorted(wanted - edges.keys()))}")
            for extra in sorted(edges.keys() - wanted):
                out.append(f"{label}.on.{extra} is not an outcome of a {kind} step. "
                           f"Outcomes: {', '.join(sorted(wanted))}")
        if not enabled(step) and step.get("skip") not in edges:
            out.append(f"{label} is off, so {label}.skip must name the outcome to continue with: "
                       f"{', '.join(edges)}")
    if out:
        return out

    start, escalate = run.get("start"), run.get("escalate_to")
    if start not in valid:
        out.append(f'[run].start = "{start}" is not a step')
    if escalate not in valid or valid[escalate]["type"] != "human" or not enabled(valid[escalate]):
        out.append(f'[run].escalate_to = "{escalate}" must be an enabled human step')
    if out:
        return out

    # Reachability, following only the edges a job can really take.
    # A hand merge follows a disabled merge step's MERGED edge without entering that step.
    seen, todo = set(), [start, escalate]
    for step in valid.values():
        if step["type"] == "merge" and not enabled(step) and "MERGED" in step.get("on", {}):
            todo.append(step["on"]["MERGED"]["to"])
    while todo:
        name = todo.pop()
        if name in seen or name in END_STATES:
            continue
        seen.add(name)
        todo += [edge["to"] for edge in live_edges(valid[name]).values()]
    for name, step in valid.items():
        if enabled(step) and name not in seen:
            out.append(f'[steps.{name}] can\'t be reached from [run].start = "{start}". '
                       "Point an edge at it, or set enabled = false")

    # A loop must contain an edge with max, or a human step where a person decides each time.
    def unbounded(name):
        step = valid[name]
        if enabled(step) and step["type"] == "human":
            return []
        return [e["to"] for e in live_edges(step).values()
                if (not enabled(step) or "max" not in e) and e["to"] in valid]

    state = {}

    def visit(name, path):
        state[name] = "open"
        for nxt in unbounded(name):
            if state.get(nxt) == "open":
                return path[path.index(nxt):] + [nxt]
            if nxt not in state and (found := visit(nxt, path + [nxt])):
                return found
        state[name] = "done"
        return None

    for name in valid:
        if name not in state and (loop := visit(name, [name])):
            out.append(f"loop {' -> '.join(loop)} has no edge with max and no human step, "
                       "so a job could go round it forever. Add max to one of its edges")
            break
    return out


def hand_merge_warning(steps: dict[str, dict]) -> str | None:
    """A disabled merge step that a live edge still names, or that nobody is asked to finish."""
    merges: list[str] = [
        name for name, step in steps.items()
        if isinstance(step, dict) and step.get("type") == "merge" and not enabled(step)]
    if not merges:
        return None
    name: str = merges[0]
    pointed: list[str] = []
    for step_name, step in steps.items():
        if not isinstance(step, dict) or step.get("type") not in TYPES or not enabled(step):
            continue
        for outcome, edge in live_edges(step).items():
            if edge.get("to") == name:
                pointed.append(f"{step_name}.{outcome}")
    if pointed:
        noun: str = "that edge" if len(pointed) == 1 else "those edges"
        return (f"[steps.{name}] is off, but {', '.join(pointed)} still points at it. "
                "The job skips the merge and the pull request stays open. "
                f"Point {noun} at a human step with pr_ready = true.")
    asked: bool = any(
        isinstance(step, dict) and step.get("type") == "human" and enabled(step) and step.get("pr_ready")
        for step in steps.values())
    if not asked:
        return (f"[steps.{name}] is off and no human step has pr_ready = true, "
                "so nothing asks a person to merge.")
    return None


def closed_issue_warning(steps: dict[str, dict], issue_link: str) -> str | None:
    """A step that runs after the merge while GitHub closes the issue on merge.

    A hand merge follows the merge step's MERGED edge whether that step is on or off.
    """
    if issue_link != "closes":
        return None
    merges: list[str] = [name for name, step in steps.items()
                         if isinstance(step, dict) and step.get("type") == "merge"]
    if not merges:
        return None
    edge: object = steps[merges[0]].get("on", {}).get("MERGED")
    if not isinstance(edge, dict) or not edge.get("to"):
        return None
    target, _ = resolve(steps, edge["to"])
    if target in END_STATES:
        return None
    return (f'[steps.{target}] runs after the merge, but [project].issue_link = "closes" lets GitHub close '
            f'the issue when the pull request merges, so {target} sees a closed issue. Use issue_link = "refs".')


def describe(cfg):
    """`factory graph`: one line per step."""
    rows = []
    for name, step in cfg.steps.items():
        kind = step["type"]
        edges = " | ".join(
            f"{outcome} -> {edge['to']}" + (f" (max {edge['max']})" if "max" in edge else "")
            for outcome, edge in step["on"].items())
        if not enabled(step):
            edges = f"off, continues as {step['skip']} -> {step['on'][step['skip']]['to']}"
        rows.append((name, kind, edges))
    widths = [max(len(r[i]) for r in rows) for i in (0, 1)]
    lines = [f"{n.ljust(widths[0])}  {k.ljust(widths[1])}  {e}" for n, k, e in rows]
    lines.append("")
    lines.append(f"start: {cfg['run']['start']}   on a reached max: {cfg['run']['escalate_to']}   "
                 f"end states: {', '.join(sorted(END_STATES))}")
    return "\n".join(lines) + "\n"


def as_json(cfg):
    return {
        "start": cfg["run"]["start"],
        "escalate_to": cfg["run"]["escalate_to"],
        "end_states": sorted(END_STATES),
        "steps": [{
            "name": name, "type": step["type"], "enabled": enabled(step), "skip": step.get("skip"),
            "who": step.get("who"),
            "edges": [{"outcome": o, "to": e["to"], "max": e.get("max")} for o, e in step["on"].items()],
        } for name, step in cfg.steps.items()],
    }
