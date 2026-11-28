"""Pipelines as YAML files: the schema, extends, several pipelines, choosing one, and a job that runs one."""

import json
import subprocess
import sys

import pytest
from conftest import CLI
from modfactory import config, graph, jsonschema_lite, pipelines, runstate, sync
from modfactory.queue import NeedsPipeline
from modfactory.util import ConfigError, Refused

BASE = 'schema = 1\n[project]\nbase_branch = "main"\n[checks]\ncommands = ["true"]\n'

HOTFIX = """\
pipes:
  hotfix:
    description: Small fixes, no spec and no approval
    extends: default
    steps:
      spec: { enabled: false, skip: DONE }
      approve: { enabled: false, skip: APPROVE }
"""


def project(tmp_path, toml="", **files):
    (tmp_path / "factory").mkdir(exist_ok=True)
    (tmp_path / "factory" / "factory.toml").write_text(BASE + toml)
    folder = tmp_path / "factory" / "pipes"
    for name, text in files.items():
        folder.mkdir(exist_ok=True)
        (folder / name.replace("_", ".")).write_text(text)
    return tmp_path


def problems(tmp_path):
    with pytest.raises(ConfigError) as caught:
        config.load(tmp_path)
    return caught.value.problems


# ---- YAML and the schema ---------------------------------------------------


def test_the_built_in_pipeline_matches_the_old_defaults(tmp_path):
    cfg = config.load(project(tmp_path))
    assert list(cfg.pipelines) == ["default"]
    assert cfg.pipelines["default"].file == "built-in"
    assert cfg["run"]["start"] == "intake" and cfg["run"]["escalate_to"] == "escalated"
    assert cfg.steps["gate"]["on"]["FAIL"] == {"to": "debug", "max": 2}
    assert cfg.sources[("steps", "gate", "type")] == "default"


def test_on_and_no_stay_words_not_booleans():
    doc = pipelines.parse("a:\n  on: { YES: no, off: x }\n", "t.yaml")
    assert doc == {"a": {"on": {"YES": "no", "off": "x"}}}
    assert pipelines.parse("a: true\nb: false\n", "t.yaml") == {"a": True, "b": False}


def test_a_key_given_twice_is_an_error(tmp_path):
    found = problems(project(tmp_path, x_yaml="pipes:\n  x:\n    start: a\n    start: b\n"))
    assert len(found) == 1 and found[0].startswith("factory/pipes/x.yaml (line 4, column 5): 'start' is given twice")


def test_schema_errors_name_the_file_and_the_path(tmp_path):
    found = problems(project(tmp_path, bad_yaml="""\
pipes:
  Bad:
    stepz: {}
  ok:
    extends: default
    steps:
      gate: { type: checks, port: true, on: { FAIL: { to: debug, max: -1 } } }
      build: { type: agent, enabled: "yes" }
"""))
    assert found == [
        "factory/pipes/bad.yaml: pipes.Bad is not a valid name: lowercase letters, digits and hyphens",
        "factory/pipes/bad.yaml: pipes.Bad.stepz is not a setting. Known: description, extends, start, "
        "escalate_to, steps",
        "factory/pipes/bad.yaml: pipes.ok.steps.gate.on.FAIL.max must be at least 0 (got -1)",
        "factory/pipes/bad.yaml: pipes.ok.steps.gate.port is not a setting here. Known: type, enabled, skip, on",
        'factory/pipes/bad.yaml: pipes.ok.steps.build.enabled must be true or false (got "yes")',
    ]


def test_the_schema_knows_every_step_setting_the_config_checks():
    schema = pipelines.schema()
    defs = schema["$defs"]
    for kind, keys in config.STEP_KEYS.items():
        part = {"agent": "agentStep", "human": "humanStep", "verify": "verifyStep"}.get(kind, "plainStep")
        allowed = set(defs[part]["propertyNames"]["enum"])
        assert allowed == set(config.STEP_COMMON) | {"on"} | set(keys), kind
    assert set(defs["step"]["properties"]["type"]["enum"]) == set(graph.TYPES)
    roles = set(defs["agentStep"]["properties"]["role"]["enum"])
    assert roles == set(graph.ROLE_OUTCOMES) | {"custom"}


def test_validator_keywords():
    schema = {"type": "object", "required": ["a"], "additionalProperties": False,
              "properties": {"a": {"anyOf": [{"type": "string", "minLength": 1},
                                             {"type": "object", "properties": {"to": {"type": "string"}}}]},
                             "n": {"type": "integer", "minimum": 1},
                             "e": {"enum": ["x", "y"]},
                             "l": {"type": "array", "items": {"$ref": "#/$defs/s"}}},
              "$defs": {"s": {"type": "string", "pattern": "^[a-z]+$", "description": "Lowercase."}}}
    assert jsonschema_lite.validate({"a": "go"}, schema) == []
    assert jsonschema_lite.validate({"a": {"to": "x"}}, schema) == []
    assert jsonschema_lite.validate({"a": {"to": 1}, "n": True, "e": "z", "l": ["ok", "No"], "q": 1}, schema) == [
        ("a.to", "must be a string (got 1)"),
        ("n", "must be a whole number (got true)"),
        ("e", 'must be one of "x", "y" (got "z")'),
        ("l[1]", 'lowercase (got "No")'),
        ("q", "is not a setting. Known: a, n, e, l"),
    ]
    assert jsonschema_lite.validate({}, schema) == [("a", "is missing")]
    assert jsonschema_lite.validate({"a": 3}, schema) == [("a", "must be a string or a mapping (got 3)")]


# ---- Several pipelines -----------------------------------------------------


def test_a_pipeline_extends_another_step_by_step(tmp_path):
    cfg = config.load(project(tmp_path, hotfix_yaml=HOTFIX))
    assert list(cfg.pipelines) == ["default", "hotfix"]
    hot = cfg.view("hotfix")
    assert hot.pipeline.description == "Small fixes, no spec and no approval"
    assert hot["run"]["start"] == "intake"
    assert hot.steps["spec"]["enabled"] is False and hot.steps["spec"]["on"] == {"DONE": {"to": "spec-approval"}}
    assert graph.resolve(hot.steps, "spec") == ("build", ["spec", "spec-approval"])
    assert cfg.steps["spec"].get("enabled", True) is True  # default is untouched
    assert cfg.view("hotfix") is hot and hot.view("default") is cfg


def test_the_project_default_replaces_the_built_in_one(tmp_path):
    text = """\
pipes:
  default:
    start: build
    escalate_to: stuck
    steps:
      build: { type: agent, on: { READY: gate, BLOCKED: stuck } }
      gate: { type: checks, on: { PASS: done, FAIL: { to: build, max: 1 } } }
      stuck: { type: human, on: { FEEDBACK: build, REJECT: rejected } }
"""
    cfg = config.load(project(tmp_path, default_yaml=text))
    assert list(cfg.steps) == ["build", "gate", "stuck"]
    assert cfg.pipelines["default"].file == "factory/pipes/default.yaml"
    assert cfg.sources[("steps", "gate", "type")] == "factory/pipes/default.yaml"
    assert cfg.agent_keys() == ["build", "reviewer-code", "reviewer-security"]


def test_factory_default_extends_the_built_in_pipeline(tmp_path):
    text = "pipes:\n  default:\n    extends: factory:default\n    steps:\n      debug: { enabled: true }\n"
    cfg = config.load(project(tmp_path, default_yaml=text))
    assert cfg.steps["debug"]["enabled"] is True and "intake" in cfg.steps


def test_graph_problems_name_the_pipeline_and_its_file(tmp_path):
    text = "pipes:\n  broken:\n    extends: default\n    steps:\n      build: { on: { READY: nowhere } }\n"
    found = problems(project(tmp_path, broken_yaml=text))
    assert found == ['factory/pipes/broken.yaml (pipe "broken"): steps.build.on.READY points at unknown step '
                     '"nowhere". Steps: intake, ask-author, spec, spec-approval, build, gate, debug, review, '
                     "approve, ready-to-merge, merge, ready-to-verify, verify, escalated; "
                     "end states: done, failed, rejected"]


def test_extends_loops_and_unknown_parents_are_refused(tmp_path):
    loop = "pipes:\n  a: { extends: b }\n  b: { extends: a }\n  c: { extends: nope }\n"
    found = problems(project(tmp_path, x_yaml=loop))
    assert "pipes extend each other in a loop: a -> b -> a" in found
    assert 'factory/pipes/x.yaml: pipe "c" extends "nope", which is not a pipe. Pipes: a, b, c, default' \
        in found


def test_a_pipeline_declared_twice_is_refused(tmp_path):
    found = problems(project(tmp_path, a_yaml=HOTFIX, b_yaml=HOTFIX))
    assert found == ['factory/pipes/b.yaml: pipe "hotfix" is also declared in factory/pipes/a.yaml']


def test_run_pipeline_must_name_a_pipeline(tmp_path):
    found = problems(project(tmp_path, '[run]\npipe = "nope"\n', hotfix_yaml=HOTFIX))
    assert found == ['[run].pipe = "nope" is not a pipe. Pipes: default, hotfix']


def test_choosing_a_pipeline(tmp_path):
    cfg = config.load(project(tmp_path))
    assert cfg.choose_pipeline(None) == "default"  # the only one
    cfg = config.load(project(tmp_path, hotfix_yaml=HOTFIX))
    assert cfg.choose_pipeline(None) is None       # ask
    assert cfg.choose_pipeline("hotfix") == "hotfix"
    with pytest.raises(Refused, match='there is no pipe "nope". Pipes: default, hotfix'):
        cfg.choose_pipeline("nope")
    cfg = config.load(project(tmp_path, '[run]\npipe = "hotfix"\n'))
    assert cfg.choose_pipeline(None) == "hotfix"


def test_a_step_that_differs_gets_its_own_agent(tmp_path):
    text = HOTFIX + "      build: { instructions: Keep it small., role: build }\n"
    cfg = config.load(project(tmp_path, '[models.claude]\nbuild = { model = "opus" }\n'
                                        'hotfix-build = { effort = "low" }\n', hotfix_yaml=text))
    assert cfg.agent_key("hotfix", "build") == "hotfix-build"
    assert cfg.agent_key("hotfix", "intake") == "intake"
    assert "hotfix-build" in cfg.agent_keys()
    hot = cfg.view("hotfix")
    assert hot.agent_name("build") == "mfactory-hotfix-build"
    assert hot.agent_name("intake") == "mfactory-intake"
    assert hot.model_for("claude", "build") == {"model": "opus", "effort": "low"}
    names = {a.name for a in sync.agents(cfg)}
    assert {"mfactory-build", "mfactory-hotfix-build"} <= names


def test_legacy_steps_in_factory_toml_still_override_default(tmp_path):
    cfg = config.load(project(tmp_path, "[steps.debug]\nenabled = true\n"))
    assert cfg.steps["debug"]["enabled"] is True
    assert cfg.sources[("steps", "debug", "enabled")] == "factory.toml"
    assert cfg.legacy_pipeline is True


def test_migrate_moves_steps_to_yaml(tmp_path):
    root = project(tmp_path, '[run]\nconcurrency = 2\nstart = "intake"\n\n[steps.debug]\nenabled = true\n')
    before = config.load(root).steps
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    res = subprocess.run([sys.executable, str(CLI), "migrate", "pipes"], cwd=root, capture_output=True,
                         text=True)
    assert res.returncode == 0, res.stderr
    toml = (root / "factory" / "factory.toml").read_text()
    assert "[steps" not in toml and "start" not in toml and "concurrency = 2" in toml
    cfg = config.load(root)
    assert cfg.legacy_pipeline is False and cfg.pipelines["default"].file == "factory/pipes/default.yaml"
    assert cfg.steps == before
    assert (root / "factory" / "pipes" / "default.yaml").read_text().count("\npipes:\n") == 1


def test_a_project_as_0_32_0_left_it_still_runs_until_migrate_layout(tmp_path):
    (tmp_path / "factory.toml").write_text(BASE + '[run]\npipeline = "hotfix"\n')
    early = tmp_path / "pipelines"
    early.mkdir()
    (early / "hotfix.yaml").write_text(HOTFIX.replace("pipes:", "pipelines:", 1))
    cfg = config.load(tmp_path)
    assert list(cfg.pipelines) == ["default", "hotfix"] and cfg.pipelines["hotfix"].file == "pipelines/hotfix.yaml"
    assert cfg.choose_pipeline(None) == "hotfix"  # [run].pipeline, 0.32.0's name for [run].pipe
    assert config.old_names(cfg) == [
        "factory.toml is at the root of the repository, not in factory/",
        "the pipes are in pipelines/, not factory/pipes/",
        "pipelines/hotfix.yaml declares `pipelines:`, not `pipes:`",
        "[run].pipeline is now [run].pipe",
    ]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    res = subprocess.run([sys.executable, str(CLI), "migrate", "layout"], cwd=tmp_path, capture_output=True,
                         text=True)
    assert res.returncode == 0, res.stderr
    assert "moved [run].pipeline -> [run].pipe" in res.stdout
    cfg = config.load(tmp_path)
    assert config.old_names(cfg) == [] and cfg.choose_pipeline(None) == "hotfix"
    assert cfg.pipelines["hotfix"].file == "factory/pipes/hotfix.yaml"
    assert 'pipe = "hotfix"' in (tmp_path / "factory" / "factory.toml").read_text()


def test_a_factory_toml_at_the_root_still_loads_and_migrate_layout_moves_it(tmp_path):
    (tmp_path / "factory.toml").write_text(BASE + '[run]\nconcurrency = 2\n')
    early = tmp_path / "pipelines"
    early.mkdir()
    (early / "hotfix.yaml").write_text("# yaml-language-server: $schema=../.factory/schema/pipeline.schema.json\n"
                                       + HOTFIX.replace("pipes:", "pipelines:", 1))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "factory.toml"], cwd=tmp_path, check=True)
    assert config.is_legacy_layout(tmp_path) and config.config_rel(tmp_path) == "factory.toml"
    assert config.load(tmp_path)["run"]["concurrency"] == 2

    res = subprocess.run([sys.executable, str(CLI), "migrate", "layout"], cwd=tmp_path, capture_output=True,
                         text=True)
    assert res.returncode == 0, res.stderr
    assert "moved factory.toml -> factory/factory.toml" in res.stdout
    assert "moved pipelines/hotfix.yaml -> factory/pipes/hotfix.yaml" in res.stdout
    assert not (tmp_path / "factory.toml").exists() and not early.exists()
    assert not config.is_legacy_layout(tmp_path)
    staged = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=tmp_path, capture_output=True,
                            text=True).stdout
    assert "factory/factory.toml" in staged  # git mv: the file's history follows it
    moved = (tmp_path / "factory" / "pipes" / "hotfix.yaml").read_text()
    assert moved.startswith("# yaml-language-server: $schema=../../.factory/schema/pipe.schema.json")
    cfg = config.load(tmp_path)
    assert list(cfg.pipelines) == ["default", "hotfix"] and cfg["run"]["concurrency"] == 2
    again = subprocess.run([sys.executable, str(CLI), "migrate", "layout"], cwd=tmp_path, capture_output=True,
                           text=True)
    assert "nothing to move" in again.stdout


# ---- Jobs on a pipeline ----------------------------------------------------


def test_a_job_runs_the_pipeline_it_was_created_with(world):
    world.pipeline_file("hotfix.yaml", HOTFIX)
    number = world.issue(title="Fix the typo")
    with pytest.raises(NeedsPipeline) as asked:
        world.create(number)
    assert [c["name"] for c in asked.value.choices] == ["default", "hotfix"]
    assert str(asked.value).endswith("; hotfix (Small fixes, no spec and no approval)")

    job = world.create(number, "hotfix")
    assert world.job(job)["pipeline"] == "hotfix"
    world.play(job)
    j = world.job(job)
    assert [h["step"] + " " + h["outcome"] for h in j["history"]] == [
        "intake ACCEPT", "build READY", "gate PASS", "review PASS"]
    assert j["history"][0]["skipped"] == ["spec", "spec-approval"]
    assert j["history"][-1]["skipped"] == ["approve"]
    assert j["step"] == "ready-to-merge"

    other = world.create(world.issue(title="Add dark mode"), "default")
    world.play(other, until="build")
    assert [h["step"] for h in world.job(other)["history"]] == ["intake", "spec"]


def test_run_pipeline_picks_the_pipeline_without_a_flag(world):
    world.configure('[run]\npipe = "hotfix"\n')
    world.pipeline_file("hotfix.yaml", HOTFIX)
    job = world.create(world.issue(title="Fix it"))
    assert world.job(job)["pipeline"] == "hotfix"


def test_jobs_from_before_pipelines_run_default(world):
    job = world.create(world.issue(title="Old job"))
    path = world.dir(job) / "job.json"
    data = json.loads(path.read_text())
    data.pop("pipeline")
    path.write_text(json.dumps(data))
    world.play(job, until="build")
    assert [h["step"] for h in world.job(job)["history"]] == ["intake", "spec"]


def test_the_cli_lists_validates_and_shows_pipelines(world):
    world.pipeline_file("hotfix.yaml", HOTFIX)
    listed = world.cli("pipelines", "list", "--json")  # the name before `pipes`, kept as an alias
    assert listed.returncode == 0, listed.stderr
    data = json.loads(listed.stdout)
    assert data["ask"] is True and data["default"] is None
    assert [p["name"] for p in data["pipes"]] == ["default", "hotfix"]
    assert "spec" not in data["pipes"][1]["steps"]
    valid = world.cli("pipes", "validate")
    assert "ok    factory/pipes/hotfix.yaml: hotfix" in valid.stdout
    shown = world.cli("pipes", "show", "hotfix")
    assert pipelines.parse(shown.stdout, "x")["pipes"]["hotfix"]["steps"]["spec"]["enabled"] is False
    drawn = world.cli("graph", "--pipe", "hotfix")
    assert "spec             agent   off, continues as DONE -> spec-approval" in drawn.stdout
    created = world.cli("create", str(world.issue(title="Fix")), "--pipeline", "hotfix")  # alias of --pipe
    assert created.returncode == 0, created.stderr
    assert created.stdout.strip().endswith("at intake (pipe hotfix)")
    asked = world.cli("create", str(world.issue(title="Other")))
    assert asked.returncode == 1 and "choose a pipe with --pipe NAME" in asked.stderr


# ---- Run state ------------------------------------------------------------


def _h(step, outcome, to, skipped=()):
    return {"step": step, "outcome": outcome, "to": to, "entered_at": "2026-01-01T10:00:00Z",
            "at": "2026-01-01T10:05:00Z", "reason": None, "skipped": list(skipped)}


def test_step_states_from_a_history(tmp_path):
    steps = config.load(project(tmp_path)).steps
    job = {"step": "build", "running_since": "2026-01-01T11:00:00Z", "entered_at": "2026-01-01T11:00:00Z",
           "history": [_h("intake", "ACCEPT", "spec"), _h("spec", "DONE", "build", ["spec-approval"]),
                       _h("build", "READY", "gate"), _h("gate", "FAIL", "build")]}
    states = runstate.step_states(job, steps)
    assert {n: s["state"] for n, s in states.items() if s["state"] != "none"} == {
        "intake": "success", "spec": "success", "spec-approval": "skipped", "build": "running", "gate": "retry",
        "debug": "disabled", "merge": "disabled", "ready-to-verify": "disabled", "verify": "disabled"}
    assert states["intake"]["seconds"] == 300 and states["build"]["since"] == "2026-01-01T11:00:00Z"
    assert runstate.edges_taken(job) == {"intake.ACCEPT": 1, "spec.DONE": 1, "build.READY": 1, "gate.FAIL": 1}
    assert runstate.job_status(job, steps) == "running"

    waiting = dict(job, step="escalated", running_since=None)
    assert runstate.step_states(waiting, steps)["escalated"]["state"] == "waiting"
    assert runstate.job_status(dict(job, running_since=None), steps) == "queued"

    rejected = dict(job, step="rejected", history=job["history"] + [_h("build", "BLOCKED", "escalated"),
                                                                    _h("escalated", "REJECT", "rejected")])
    final = runstate.step_states(rejected, steps)
    assert final["escalated"]["state"] == "failed" and final["rejected"]["state"] == "failed"
    assert final["gate"]["state"] == "failed" and final["intake"]["state"] == "success"
    assert runstate.job_status(rejected, steps) == "rejected"
