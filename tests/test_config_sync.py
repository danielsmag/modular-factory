"""factory.toml over the defaults, and the files `factory sync` generates from it."""

import tomllib

import pytest
from modfactory import config, sync
from modfactory.util import ConfigError


def load(world, text=""):
    return config.load(world.repo, text="schema = 1\n" + text)


def problems(world, text):
    with pytest.raises(ConfigError) as error:
        load(world, text)
    return error.value.problems


# ---- merge and validation --------------------------------------------------


def test_tables_merge_key_by_key_and_other_values_replace(world):
    cfg = load(world, '[run]\nconcurrency = 5\n[checks]\ncommands = ["make test"]\n')
    assert cfg["run"] == {"concurrency": 5, "start": "intake", "escalate_to": "escalated"}
    assert cfg["checks"] == {"commands": ["make test"], "timeout_minutes": 30}


def test_an_edge_can_change_its_limit_or_its_target_alone(world):
    cfg = load(world, '[steps.gate]\non = { FAIL = { max = 5 } }\n[steps.review]\non = { CHANGES = "spec" }\n')
    assert cfg.steps["gate"]["on"]["FAIL"] == {"to": "debug", "max": 5}
    assert cfg.steps["gate"]["on"]["PASS"] == {"to": "review"}
    assert cfg.steps["review"]["on"]["CHANGES"] == {"to": "spec", "max": 2}
    assert cfg.steps["review"]["on"]["PASS"] == {"to": "approve"}


def test_config_show_names_the_source_of_every_value(world):
    cfg = load(world, '[run]\nconcurrency = 5\n[steps.gate]\non = { FAIL = { max = 5 } }\n'
                      '[models.claude]\nbuild = { effort = "max" }\n')
    shown = config.show(cfg).splitlines()

    def line(start):
        return " ".join(next(row for row in shown if row.startswith(start)).split())

    assert line("concurrency") == "concurrency = 5 # factory.toml"
    assert line("start") == 'start = "intake" # default'
    assert line("on.FAIL.max") == "on.FAIL.max = 5 # factory.toml"
    assert line("on.FAIL.to") == 'on.FAIL.to = "debug" # default'
    assert line("build.model") == 'build.model = "opus" # adapter claude'
    assert line("build.effort") == 'build.effort = "max" # factory.toml'
    tomllib.loads(config.show(cfg))                          # the output is itself valid TOML


def test_every_problem_names_its_key(world):
    assert problems(world, """
[run]
concurrency = 0
max_rounds = 3

[tools]
enabled = ["claude", "copilot"]

[checks]
commands = "npm test"

[steps.build]
port = "yes"

[reviewers.perf]

[surfaces.api]
when = "it changes the API"
reviewers = ["contract"]

[models.claude]
build = { model = "opus", effort = "extreme" }
deploy = { model = "opus" }

[models.cursor]
reviewer-code = { low = { speed = "fast" } }
""") == [
        "[run].concurrency must be a whole number of at least 1 (got 0)",
        "[run].max_rounds is not a setting. Known: concurrency, escalate_to, start",
        '[checks].commands must be a list of non-empty strings (got "npm test")',
        '[tools].enabled has "copilot", which is not a tool. Known: claude, codex, cursor',
        '[steps.build].port must be true or false (got "yes")',
        "[reviewers.perf].focus is missing: say what this reviewer looks for",
        '[surfaces.api].reviewers has "contract", but there is no [reviewers.contract]',
        '[models.claude].build.effort = "extreme" is not an effort level claude documents. '
        "Known: low, medium, high, xhigh, max",
        "[models.claude].deploy names no agent. Agents: approve, build, debug, default, intake, reviewer-code, "
        "reviewer-perf, reviewer-security, reviewer-ui, reviewer-ux, spec, verify",
        "[models.cursor].reviewer-code.low.speed is not a setting. Known: model, effort",
    ]


def test_a_newer_schema_is_refused(world):
    with pytest.raises(ConfigError, match="schema = 2, but this factory reads schema 1"):
        config.load(world.repo, text="schema = 2\n")


def test_broken_toml_is_a_config_problem(world):
    with pytest.raises(ConfigError, match="factory.toml: "):
        config.load(world.repo, text="schema = \n")


def test_a_custom_step_needs_instructions(world):
    assert problems(world, '[steps.gate]\non = { PASS = "docs" }\n[steps.docs]\ntype = "agent"\n'
                           'on = { DONE = "review" }\n') == [
        "[steps.docs].instructions is missing: say what this step's agent must do"]


# ---- models, effort and risk variants --------------------------------------


def test_model_and_effort_come_per_tool_per_step_with_a_default(world):
    cfg = load(world, """
[tools]
enabled = ["claude", "cursor", "codex"]

[models.cursor]
default = { model = "gpt-5", effort = "medium" }
build = { model = "claude-opus", effort = "high" }

[models.codex]
default = { model = "gpt-5-codex" }
spec = { model = "gpt-5-mini" }
""")
    assert cfg.model_for("claude", "build") == {"model": "opus", "effort": "high"}
    assert cfg.model_for("cursor", "build") == {"model": "claude-opus", "effort": "high"}
    assert cfg.model_for("cursor", "spec") == {"model": "gpt-5", "effort": "medium"}
    # A step's entry sits over the tool's default key by key: the adapter's effort stays.
    assert cfg.model_for("codex", "build") == {"model": "gpt-5-codex", "effort": "high"}
    assert cfg.model_for("codex", "spec") == {"model": "gpt-5-mini", "effort": "medium"}
    assert cfg.model_for("codex", "reviewer-code", "low") == {"model": "gpt-5-codex", "effort": "low"}


def test_without_model_settings_cursor_and_codex_use_the_sessions_model(world):
    cfg = load(world, '[tools]\nenabled = ["cursor", "codex"]\n')
    assert cfg.model_for("cursor", "build") == {"model": "inherit"}
    assert cfg.model_for("codex", "build") == {"model": "inherit", "effort": "high"}


def test_a_risk_variant_overrides_only_what_it_names(world):
    cfg = load(world, '[models.claude]\nbuild = { low = { effort = "medium" }, high = { model = "fable" } }\n')
    assert cfg.variants("build") == ["low", "high"]
    assert cfg.model_for("claude", "build", "low") == {"model": "opus", "effort": "medium"}
    assert cfg.model_for("claude", "build", "high") == {"model": "fable", "effort": "high"}
    assert cfg.agent_name("build", "medium") == "factory-build"
    assert cfg.agent_name("build", "high") == "factory-build-high"


def test_the_job_s_risk_picks_the_agent_variant(world):
    world.configure('[models.claude]\nbuild = { high = { effort = "max" } }\n')
    job = world.create(world.issue())
    world.next(job)
    world.intake(job, risk="high")
    world.done(job)
    world.play(job, until="build")
    assert world.next(job)[0] == "start-agent factory-build-high"
    world.play(job, until="review")
    assert world.next(job)[0] == "start-agent factory-reviewer-code"    # the low variant is for low risk only


# ---- generated files -------------------------------------------------------


ALL_TOOLS = '[tools]\nenabled = ["claude", "cursor", "codex"]\n'


def plan(world, text=""):
    cfg = load(world, text)
    files, notes = sync.plan(cfg)
    return files, notes


def frontmatter(text):
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line and not line.startswith(" "))


def test_claude_agents_carry_model_effort_and_tools(world):
    files, _ = plan(world)
    assert sorted(files) == [
        ".claude/agents/factory-approve.md", ".claude/agents/factory-build.md", ".claude/agents/factory-intake.md",
        ".claude/agents/factory-reviewer-code-low.md", ".claude/agents/factory-reviewer-code.md",
        ".claude/agents/factory-reviewer-security.md", ".claude/agents/factory-reviewer-ui.md",
        ".claude/agents/factory-reviewer-ux.md", ".claude/agents/factory-spec.md", ".claude/skills/factory/SKILL.md"]
    build = frontmatter(files[".claude/agents/factory-build.md"])
    assert (build["name"], build["model"], build["effort"]) == ("factory-build", "opus", "high")
    assert build["tools"] == "Read, Grep, Glob, Bash, Edit, Write, Skill"
    intake = frontmatter(files[".claude/agents/factory-intake.md"])
    assert intake["tools"] == "Read, Grep, Glob, Write"       # no shell
    low = frontmatter(files[".claude/agents/factory-reviewer-code-low.md"])
    assert (low["model"], low["effort"]) == ("haiku", "low")
    assert "Generated by `factory sync` from factory.toml. Do not edit" in files[".claude/agents/factory-build.md"]
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/build.md`" in files[".claude/agents/factory-build.md"]


def test_cursor_agents_put_effort_in_the_model_and_make_intake_read_only(world):
    files, notes = plan(world, '[tools]\nenabled = ["cursor"]\n[models.cursor]\n'
                               'build = { model = "gpt-5", effort = "high" }\nspec = { effort = "low" }\n')
    assert frontmatter(files[".cursor/agents/factory-build.md"])["model"] == "gpt-5[effort=high]"
    assert frontmatter(files[".cursor/agents/factory-spec.md"])["model"] == "inherit"
    assert notes == ['[models.cursor].spec: Cursor sets effort only on a named model, so effort = "low" is ignored '
                     'while model = "inherit"']
    intake = files[".cursor/agents/factory-intake.md"]
    assert frontmatter(intake)["readonly"] == "true"
    assert "reply with\nits full contents and nothing else" in intake
    assert "readonly" not in files[".cursor/agents/factory-build.md"]
    assert ".agents/skills/factory/SKILL.md" in files


def test_codex_agents_are_toml_with_reasoning_effort_and_a_shell_less_intake(world):
    files, _ = plan(world, '[tools]\nenabled = ["codex"]\n[models.codex]\nbuild = { model = "gpt-5-codex" }\n')
    build = tomllib.loads(files[".codex/agents/factory-build.toml"])
    assert (build["name"], build["model"], build["model_reasoning_effort"]) == ("factory-build", "gpt-5-codex", "high")
    assert "`.factory/roles/build.md`" in build["developer_instructions"]
    spec = tomllib.loads(files[".codex/agents/factory-spec.toml"])
    assert "model" not in spec and spec["model_reasoning_effort"] == "medium"      # inherits the session's model
    intake = tomllib.loads(files[".codex/agents/factory-intake.toml"])
    assert intake["features"] == {"shell_tool": False}
    assert "features" not in build

    files, _ = plan(world, '[tools]\nenabled = ["codex"]\n[tools.codex]\nintake_shell = true\n')
    assert "features" not in tomllib.loads(files[".codex/agents/factory-intake.toml"])


def test_variants_exist_in_every_tool_so_agent_names_do_not_depend_on_the_tool(world):
    files, _ = plan(world, ALL_TOOLS + '[models.codex]\nbuild = { high = { effort = "xhigh" } }\n')
    for path in (".claude/agents/factory-build-high.md", ".cursor/agents/factory-build-high.md",
                 ".codex/agents/factory-build-high.toml"):
        assert path in files
    assert frontmatter(files[".claude/agents/factory-build-high.md"])["effort"] == "high"
    assert tomllib.loads(files[".codex/agents/factory-build-high.toml"])["model_reasoning_effort"] == "xhigh"


def test_one_copy_of_the_skill_per_folder_a_tool_reads(world):
    def skills(tools):
        files, notes = plan(world, f"[tools]\nenabled = {tools}\n")
        return sorted(f for f in files if f.endswith("SKILL.md")), notes

    assert skills('["claude"]')[0] == [".claude/skills/factory/SKILL.md"]
    assert skills('["cursor"]')[0] == [".agents/skills/factory/SKILL.md"]
    assert skills('["codex", "cursor"]')[0] == [".agents/skills/factory/SKILL.md"]
    found, notes = skills('["claude", "cursor"]')
    assert found == [".claude/skills/factory/SKILL.md"]    # Cursor loads Claude Code's folder too
    assert notes == ["Cursor uses the factory skill in .claude/skills/factory, which it loads alongside its own "
                     "folders."]
    found, notes = skills('["claude", "cursor", "codex"]')
    assert found == [".agents/skills/factory/SKILL.md", ".claude/skills/factory/SKILL.md"]
    assert "so it may list it twice" in notes[0]


def test_optional_steps_and_custom_steps_get_agents_only_when_on(world):
    files, _ = plan(world, """
[steps.debug]
enabled = true

[steps.verify]
enabled = true
agent = true

[steps.gate]
on = { PASS = "docs" }

[steps.docs]
type = "agent"
instructions = "Update the changelog for this change."
context = "The changelog is CHANGELOG.md."
on = { DONE = "review", SKIP = "review" }
""")
    assert ".claude/agents/factory-debug.md" in files and ".claude/agents/factory-verify.md" in files
    docs = files[".claude/agents/factory-docs.md"]
    assert "2. `.factory/roles/step.md`" in docs
    assert "## Your task\n\nUpdate the changelog for this change." in docs
    assert "Write `docs.md` in the job folder. Its first line must be `STATUS: <OUTCOME>`, with OUTCOME one of: " \
           "DONE, SKIP." in docs
    assert "## Project context\n\nThe changelog is CHANGELOG.md." in docs
    assert frontmatter(docs)["model"] == "sonnet"           # [models.claude].default


def test_a_custom_reviewer_is_only_configuration(world):
    text = """
[reviewers.perf]
focus = "Queries inside loops and work repeated on every request."

[surfaces.api]
when = "it changes an HTTP endpoint"
reviewers = ["perf"]
"""
    files, _ = plan(world, text)
    perf = files[".claude/agents/factory-reviewer-perf.md"]
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/reviewer.md`\n\n" in perf
    assert "## What to look for\n\nQueries inside loops" in perf
    assert "- `api`: it changes an HTTP endpoint" in files[".claude/agents/factory-intake.md"]

    cfg = load(world, text)
    assert cfg.reviewers_for([]) == ["code"]
    assert cfg.reviewers_for(["api", "ui"]) == ["code", "perf", "ux", "ui"]


def test_sync_writes_reports_and_removes_what_is_stale(world):
    world.configure('[steps.debug]\nenabled = true\n')
    cfg = config.load(world.repo)
    actions, _ = sync.sync(cfg)
    assert ("create", ".claude/agents/factory-debug.md") in actions and len(actions) == 11
    assert sync.sync(cfg) == ([], [])

    (world.repo / ".claude/agents/factory-build.md").write_text("hand edit\n")
    (world.repo / ".claude/agents/factory-mine.md").write_text("not generated\n")
    assert sync.sync(cfg, check=True)[0] == [("update", ".claude/agents/factory-build.md")]
    assert (world.repo / ".claude/agents/factory-build.md").read_text() == "hand edit\n"   # check changes nothing

    world.configure('[tools]\nenabled = ["cursor"]\n')
    actions, _ = sync.sync(config.load(world.repo))
    assert ("remove", ".claude/agents/factory-debug.md") in actions
    assert ("remove", ".claude/skills/factory/SKILL.md") in actions
    assert ("remove", ".claude/agents/factory-build.md") not in actions    # no longer marked as generated
    assert (world.repo / ".claude/agents/factory-mine.md").exists()
    assert not (world.repo / ".claude/skills/factory").exists()


# ---- project skills --------------------------------------------------------


def add_skill(world, folder, name, description="Does things."):
    path = world.repo / folder / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n")


SKILLS = """
[skills]
all = ["code-style"]

[steps.build]
skills = ["testing"]

[reviewers.ui]
skills = ["design-system"]
"""


def test_skills_resolve_per_step_and_reviewer(world):
    add_skill(world, ".agents/skills", "code-style")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".cursor/skills", "design-system")
    files, _ = plan(world, ALL_TOOLS + SKILLS)

    for tool_file in (".claude/agents/factory-build.md", ".cursor/agents/factory-build.md"):
        assert ("## Project skills\n\nRead each of these in the main checkout before you start, and apply it to "
                "this job:\n\n- `code-style`: `.agents/skills/code-style/SKILL.md`\n"
                "- `testing`: `.claude/skills/testing/SKILL.md`\n") in files[tool_file]
    codex = tomllib.loads(files[".codex/agents/factory-reviewer-ui.toml"])["developer_instructions"]
    assert "- `design-system`: `.cursor/skills/design-system/SKILL.md`" in codex
    spec = files[".claude/agents/factory-spec.md"]
    assert "`code-style`" in spec and "testing" not in spec


def test_claude_preloads_only_skills_in_its_own_folder(world):
    add_skill(world, ".agents/skills", "code-style")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".cursor/skills", "design-system")
    files, _ = plan(world, SKILLS)
    assert "skills:\n  - testing\n" in files[".claude/agents/factory-build.md"]
    assert "skills:" not in files[".claude/agents/factory-spec.md"]


def test_the_first_folder_wins_when_a_skill_is_in_several(world):
    add_skill(world, ".claude/skills", "code-style")
    add_skill(world, ".agents/skills", "code-style")
    assert str(sync.resolve_skill(world.repo, "code-style")) == ".agents/skills/code-style/SKILL.md"


def test_a_missing_skill_names_the_key_that_lists_it(world):
    add_skill(world, ".agents/skills", "code-style")
    with pytest.raises(ConfigError) as error:
        plan(world, SKILLS)
    assert error.value.problems == [
        '[steps.build].skills: skill "testing" not found in .agents/skills, .claude/skills, .cursor/skills, '
        ".codex/skills",
        '[reviewers.ui].skills: skill "design-system" not found in .agents/skills, .claude/skills, .cursor/skills, '
        ".codex/skills"]


def test_factory_skills_lists_what_is_on_disk_and_who_reads_it(world):
    add_skill(world, ".agents/skills", "code-style", "House style for this repo.")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".claude/skills", "unused")
    add_skill(world, ".claude/skills", "factory")             # the factory's own skill is not a project skill
    cfg = load(world, "[skills]\nall = [\"code-style\"]\n[steps.build]\nskills = [\"testing\"]\n")
    text = sync.describe_skills(cfg)
    assert "code-style  .agents/skills/code-style/SKILL.md\n    read by: every agent\n    House style" in text
    assert "testing  .claude/skills/testing/SKILL.md\n    read by: build\n" in text
    assert "unused  .claude/skills/unused/SKILL.md\n    read by: no agent\n" in text
    assert "factory  " not in text
