"""factory.toml over the defaults, and the files `mfactory sync` generates from it."""

import json
import tomllib

import pytest
from modfactory import config, graph, sync
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
    assert cfg["run"] == {"concurrency": 5, "start": "intake", "escalate_to": "escalated",
                          "pick": "all", "report_problems": False, "authors": [], "assignees": [], "pipe": ""}
    assert cfg["checks"] == {"commands": ["make test"], "timeout_minutes": 30}
    assert cfg["workspace"] == {"concurrency": 3}
    assert cfg.sources[("workspace", "concurrency")] == "default"
    assert cfg.base == "master"
    assert cfg.sources[("project", "base_branch")] == "default"


def test_a_project_cannot_set_the_workspace_cap(world):
    assert problems(world, "[workspace]\nconcurrency = 4\n") == [
        "[workspace].concurrency is the cap across projects. Set it in workspace.toml, "
        "or pass --concurrency to `mfactory workspace queue`.",
    ]


def test_an_old_fleet_table_names_its_new_home(world):
    assert problems(world, "[fleet]\nconcurrency = 4\n") == [
        "[fleet] is now [workspace]. The cap across projects is set in workspace.toml, "
        "or with --concurrency on `mfactory workspace queue`.",
    ]


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


def test_the_default_pipeline_does_not_warn_about_merging(world):
    cfg = load(world)
    assert cfg.steps["approve"]["on"]["APPROVE"]["to"] == "ready-to-merge"
    assert cfg.steps["merge"]["enabled"] is False
    assert cfg.steps["ready-to-merge"]["pr_ready"] is True
    assert graph.hand_merge_warning(cfg.steps) is None


def test_an_edge_into_a_disabled_merge_is_a_warning(world):
    cfg = load(world, '[steps.approve]\non = { APPROVE = "merge" }\n'
                      '[steps.ready-to-merge]\nenabled = false\nskip = "REJECT"\n')
    assert graph.hand_merge_warning(cfg.steps) == (
        "[steps.merge] is off, but approve.APPROVE still points at it. "
        "The job skips the merge and the pull request stays open. "
        "Point that edge at a human step with pr_ready = true.")


def test_a_disabled_merge_with_nobody_asked_is_a_warning(world):
    cfg = load(world, "[steps.ready-to-merge]\npr_ready = false\n[steps.escalated]\npr_ready = false\n")
    assert graph.hand_merge_warning(cfg.steps) == (
        "[steps.merge] is off and no human step has pr_ready = true, so nothing asks a person to merge.")


def test_the_approve_agent_says_who_merges(world):
    files, _ = plan(world)
    assert "APPROVE asks a maintainer to merge. The factory does not merge." in files[
        ".claude/agents/mfactory-approve.md"]
    files, _ = plan(world, '[steps.approve]\non = { APPROVE = "merge" }\n'
                           '[steps.ready-to-merge]\nenabled = false\nskip = "REJECT"\n'
                           '[steps.merge]\nenabled = true\n')
    assert "APPROVE means the factory merges after syncing the base branch and running the checks again." in files[
        ".claude/agents/mfactory-approve.md"]


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
        "[run].max_rounds is not a setting. "
        "Known: assignees, authors, concurrency, escalate_to, pick, pipe, pipeline, report_problems, start",
        '[checks].commands must be a list of non-empty strings (got "npm test")',
        '[tools].enabled has "copilot", which is not a tool. Known: claude, codex, cursor',
        '[steps.build].port must be true or false (got "yes")',
        "[reviewers.perf].focus is missing: say what this reviewer looks for",
        '[surfaces.api].reviewers has "contract", but there is no [reviewers.contract]',
        '[models.claude].build.effort = "extreme" is not an effort level claude documents. '
        "Known: low, medium, high, xhigh, max",
        "[models.claude].deploy names no agent. Agents: approve, build, debug, default, intake, planner-scan, "
        "reviewer-code, reviewer-perf, reviewer-security, reviewer-ui, reviewer-ux, spec, verify",
        "[models.cursor].reviewer-code.low.speed is not a setting. Known: model, effort",
    ]


def test_a_newer_schema_is_refused(world):
    with pytest.raises(ConfigError, match="schema = 2, but this factory reads schema 1"):
        config.load(world.repo, text="schema = 2\n")


def test_broken_toml_is_a_config_problem(world):
    with pytest.raises(ConfigError, match="factory.toml: "):
        config.load(world.repo, text="schema = \n")


def test_a_pull_request_title_pattern_must_compile(world):
    assert "must be a valid regular expression" in problems(
        world, "[project]\npr_title_pattern = '('\n")[0]
    assert problems(world, "[project]\npr_title = 1\n") == [
        "[project].pr_title must be a string (got 1)"]
    load(world, "[project]\npr_title = ''\npr_title_pattern = ''\n")


def test_pull_request_title_is_told_only_when_set(world):
    files, _ = plan(world)
    assert "Pull request title" not in files[".claude/agents/mfactory-build.md"]
    assert "Pull request title" not in files[".claude/agents/mfactory-intake.md"]

    text = """
[project]
pr_title = "feat: add CSV export [ABC-123]"
pr_title_pattern = '^(feat|fix): .+ \\[[A-Z]+-\\d+\\]$'
"""
    files, _ = plan(world, text)
    build = files[".claude/agents/mfactory-build.md"]
    intake = files[".claude/agents/mfactory-intake.md"]
    assert "## Pull request title" in build
    assert "not the spec title" in build
    assert "feat: add CSV export [ABC-123]" in build
    assert "The whole title must match `^(feat|fix): .+ \\[[A-Z]+-\\d+\\]$`." in build
    assert "gh pr edit" in build
    assert "Squash merge uses this title as the commit on the base branch." in build
    assert "NEEDS-INFO" in intake and "Do not invent one." in intake
    assert "gh pr edit" not in intake

    files, _ = plan(world, "[project]\npr_title_pattern = '^feat: .+$'\n")
    assert "The whole title must match `^feat: .+$`." in files[".claude/agents/mfactory-build.md"]
    assert "Form:" not in files[".claude/agents/mfactory-build.md"]

    files, _ = plan(world, '[project]\nmerge_method = "merge"\npr_title = "feat: add a thing"\n')
    built = files[".claude/agents/mfactory-build.md"]
    assert "feat: add a thing" in built and "Squash merge" not in built


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
    assert cfg.agent_name("build", "medium") == "mfactory-build"
    assert cfg.agent_name("build", "high") == "mfactory-build-high"


def test_the_job_s_risk_picks_the_agent_variant(world):
    world.configure('[models.claude]\nbuild = { high = { effort = "max" } }\n')
    job = world.create(world.issue())
    world.next(job)
    world.intake(job, risk="high")
    world.done(job)
    world.play(job, until="build")
    assert world.next(job)[0] == "start-agent mfactory-build-high"
    world.play(job, until="review")
    assert world.next(job)[0] == "start-agent mfactory-reviewer-code"    # the low variant is for low risk only


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
        ".claude/agents/mfactory-approve.md", ".claude/agents/mfactory-build.md", ".claude/agents/mfactory-intake.md",
        ".claude/agents/mfactory-planner-scan.md",
        ".claude/agents/mfactory-reviewer-code-low.md", ".claude/agents/mfactory-reviewer-code.md",
        ".claude/agents/mfactory-reviewer-security.md", ".claude/agents/mfactory-spec.md",
        ".claude/skills/mfactory/SKILL.md"]
    build = frontmatter(files[".claude/agents/mfactory-build.md"])
    assert (build["name"], build["model"], build["effort"]) == ("mfactory-build", "opus", "high")
    assert "tools" not in build                                # every tool by default, MCP included
    low = frontmatter(files[".claude/agents/mfactory-reviewer-code-low.md"])
    assert (low["model"], low["effort"]) == ("haiku", "low")
    assert "Generated by `mfactory sync` from factory.toml. Do not edit" in files[".claude/agents/mfactory-build.md"]
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/build.md`" in files[".claude/agents/mfactory-build.md"]


def test_tools_set_the_claude_allowlist(world, monkeypatch):
    home = use_home(monkeypatch, world.tmp / "home")
    add_plugin_skill(home, "claude", "superpowers", "test-driven-development")
    (home / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {"superpowers@market": False}}))
    files, notes = plan(world, '[tools.claude]\nagent_tools = ["default"]\n'
                               '[steps.build]\ntools = ["default", "WebFetch", "mcp__github__*", "Read"]\n'
                               '[steps.intake]\nskills = ["superpowers:test-driven-development"]\n'
                               '[reviewers.security]\ntools = ["all"]\n')
    build = frontmatter(files[".claude/agents/mfactory-build.md"])
    assert build["tools"] == "Read, Grep, Glob, Bash, Edit, Write, Skill, WebFetch, mcp__github__*"
    assert frontmatter(files[".claude/agents/mfactory-intake.md"])["tools"] == "Read, Grep, Glob, Write, Skill"
    assert "tools" not in frontmatter(files[".claude/agents/mfactory-reviewer-security.md"])
    assert 'skill "superpowers:test-driven-development" is not installed or enabled for Claude Code' in notes
    assert any("[steps.build].tools" in line for line in problems(world, '[steps.build]\ntools = ["all", "Read"]\n'))


def test_cursor_agents_put_effort_in_the_model_and_make_intake_read_only(world):
    files, notes = plan(world, '[tools]\nenabled = ["cursor"]\n[models.cursor]\n'
                               'build = { model = "gpt-5", effort = "high" }\nspec = { effort = "low" }\n')
    assert frontmatter(files[".cursor/agents/mfactory-build.md"])["model"] == "gpt-5[effort=high]"
    assert frontmatter(files[".cursor/agents/mfactory-spec.md"])["model"] == "inherit"
    assert notes == ['[models.cursor].spec: Cursor sets effort only on a named model, so effort = "low" is ignored '
                     'while model = "inherit"',
                     'rule "user:CLAUDE.md" is not installed on this machine']
    intake = files[".cursor/agents/mfactory-intake.md"]
    assert frontmatter(intake)["readonly"] == "true"
    assert "reply with\nits full contents and nothing else" in intake
    assert "readonly" not in files[".cursor/agents/mfactory-build.md"]
    assert ".agents/skills/mfactory/SKILL.md" in files


def test_codex_agents_are_toml_with_reasoning_effort_and_a_shell_less_intake(world):
    files, _ = plan(world, '[tools]\nenabled = ["codex"]\n[models.codex]\nbuild = { model = "gpt-5-codex" }\n')
    build = tomllib.loads(files[".codex/agents/mfactory-build.toml"])
    assert (build["name"], build["model"], build["model_reasoning_effort"]) == ("mfactory-build", "gpt-5-codex", "high")
    assert "`.factory/roles/build.md`" in build["developer_instructions"]
    spec = tomllib.loads(files[".codex/agents/mfactory-spec.toml"])
    assert "model" not in spec and spec["model_reasoning_effort"] == "medium"      # inherits the session's model
    intake = tomllib.loads(files[".codex/agents/mfactory-intake.toml"])
    assert intake["features"] == {"shell_tool": False}
    assert "features" not in build

    files, _ = plan(world, '[tools]\nenabled = ["codex"]\n[tools.codex]\nintake_shell = true\n')
    assert "features" not in tomllib.loads(files[".codex/agents/mfactory-intake.toml"])


def test_variants_exist_in_every_tool_so_agent_names_do_not_depend_on_the_tool(world):
    files, _ = plan(world, ALL_TOOLS + '[models.codex]\nbuild = { high = { effort = "xhigh" } }\n')
    for path in (".claude/agents/mfactory-build-high.md", ".cursor/agents/mfactory-build-high.md",
                 ".codex/agents/mfactory-build-high.toml"):
        assert path in files
    assert frontmatter(files[".claude/agents/mfactory-build-high.md"])["effort"] == "high"
    assert tomllib.loads(files[".codex/agents/mfactory-build-high.toml"])["model_reasoning_effort"] == "xhigh"


def test_one_copy_of_the_skill_per_folder_a_tool_reads(world):
    def skills(tools):
        files, notes = plan(world, f"[tools]\nenabled = {tools}\n")
        return sorted(f for f in files if f.endswith("SKILL.md")), notes

    assert skills('["claude"]')[0] == [".claude/skills/mfactory/SKILL.md"]
    assert skills('["cursor"]')[0] == [".agents/skills/mfactory/SKILL.md"]
    assert skills('["codex", "cursor"]')[0] == [".agents/skills/mfactory/SKILL.md"]
    found, notes = skills('["claude", "cursor"]')
    assert found == [".claude/skills/mfactory/SKILL.md"]    # Cursor loads Claude Code's folder too
    assert notes == ["Cursor uses the factory skill in .claude/skills/mfactory, which it loads alongside its own "
                     "folders.",
                     'rule "user:CLAUDE.md" is not installed on this machine']
    found, notes = skills('["claude", "cursor", "codex"]')
    assert found == [".agents/skills/mfactory/SKILL.md", ".claude/skills/mfactory/SKILL.md"]
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
    assert ".claude/agents/mfactory-debug.md" in files and ".claude/agents/mfactory-verify.md" in files
    docs = files[".claude/agents/mfactory-docs.md"]
    assert "2. `.factory/roles/step.md`" in docs
    assert "## Your task\n\nUpdate the changelog for this change." in docs
    assert "Write `docs.md` in the job folder. Its first line must be `STATUS: <OUTCOME>`, with OUTCOME one of: " \
           "DONE, SKIP." in docs
    assert "## Project context\n\nThe changelog is CHANGELOG.md." in docs
    assert frontmatter(docs)["model"] == "sonnet"           # [models.claude].default


def test_a_prompt_file_is_read_after_the_role(world) -> None:
    (world.repo / "docs").mkdir()
    (world.repo / "docs" / "review-data.md").write_text("# Data\n")
    (world.repo / "docs" / "docs-step.md").write_text("# Docs\n")
    files: dict[str, str]
    files, _ = plan(world, """
[reviewers.data]
prompt = "docs/review-data.md"

[surfaces.pipelines]
when = "it changes a pipeline"
reviewers = ["data"]

[steps.gate]
on = { PASS = "docs" }

[steps.docs]
type = "agent"
instructions_file = "docs/docs-step.md"
on = { DONE = "review", SKIP = "review" }
""")
    data: str = files[".claude/agents/mfactory-reviewer-data.md"]
    docs: str = files[".claude/agents/mfactory-docs.md"]
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/reviewer.md`\n3. `docs/review-data.md`" in data
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/step.md`\n3. `docs/docs-step.md`" in docs


def test_a_custom_reviewer_is_only_configuration(world):
    text = """
[reviewers.perf]
focus = "Queries inside loops and work repeated on every request."

[surfaces.api]
when = "it changes an HTTP endpoint"
reviewers = ["perf"]
"""
    files, _ = plan(world, text)
    perf = files[".claude/agents/mfactory-reviewer-perf.md"]
    assert "1. `.factory/roles/common.md`\n2. `.factory/roles/reviewer.md`\n\n" in perf
    assert "## What to look for\n\nQueries inside loops" in perf
    assert "- `api`: it changes an HTTP endpoint" in files[".claude/agents/mfactory-intake.md"]

    cfg = load(world, text)
    assert cfg.reviewers_for([]) == ["code"]
    assert cfg.reviewers_for(["api", "ui"]) == ["code", "perf"]


UI_ON = """
[reviewers.ux]
enabled = true

[reviewers.ui]
enabled = true

[surfaces.ui]
enabled = true
"""


def test_ux_and_ui_are_off_until_a_project_switches_them_on(world):
    files, _ = plan(world)
    assert ".claude/agents/mfactory-reviewer-ux.md" not in files
    assert "- `ui`:" not in files[".claude/agents/mfactory-intake.md"]
    cfg = load(world)
    assert cfg.reviewers_for(["ui", "security"]) == ["code", "security"]
    assert "reviewer-ui" not in sync.describe_models(cfg)

    assert problems(world, "[surfaces.ui]\nenabled = true\n[reviewers.ui]\nenabled = true\n") == [
        '[surfaces.ui].reviewers has "ux", but [reviewers.ux] is off. '
        'Set [surfaces.ui] enabled = false, or drop "ux" from its reviewers',
    ]

    files, _ = plan(world, UI_ON)
    assert ".claude/agents/mfactory-reviewer-ux.md" in files
    assert "- `ui`:" in files[".claude/agents/mfactory-intake.md"]
    assert load(world, UI_ON).reviewers_for(["ui"]) == ["code", "ux", "ui"]


def test_sync_writes_reports_and_removes_what_is_stale(world):
    world.configure('[steps.debug]\nenabled = true\n')
    cfg = config.load(world.repo)
    actions, _ = sync.sync(cfg)
    assert ("create", ".claude/agents/mfactory-debug.md") in actions and len(actions) == 11
    assert ("status line", ".claude/settings.local.json") in actions
    assert sync.sync(cfg) == ([], ['rule "user:CLAUDE.md" is not installed on this machine'])

    (world.repo / ".claude/agents/mfactory-build.md").write_text("hand edit\n")
    (world.repo / ".claude/agents/mfactory-mine.md").write_text("not generated\n")
    assert sync.sync(cfg, check=True)[0] == [("update", ".claude/agents/mfactory-build.md")]
    assert (world.repo / ".claude/agents/mfactory-build.md").read_text() == "hand edit\n"   # check changes nothing

    world.configure('[tools]\nenabled = ["cursor"]\n')
    actions, _ = sync.sync(config.load(world.repo))
    assert ("remove", ".claude/agents/mfactory-debug.md") in actions
    assert ("remove", ".claude/skills/mfactory/SKILL.md") in actions
    assert ("remove", ".claude/agents/mfactory-build.md") not in actions    # no longer marked as generated
    assert (world.repo / ".claude/agents/mfactory-mine.md").exists()
    assert not (world.repo / ".claude/skills/mfactory").exists()


def test_sync_removes_generated_files_that_still_use_the_old_name(world):
    world.configure("")
    cfg = config.load(world.repo)
    sync.sync(cfg)
    old = world.repo / ".claude/agents/factory-build.md"
    old.write_text("<!-- Generated by `factory sync` from factory.toml. Do not edit -->\nold\n")
    actions, _ = sync.sync(config.load(world.repo))
    assert ("remove", ".claude/agents/factory-build.md") in actions
    assert not old.exists()


# ---- project skills --------------------------------------------------------


def add_skill(world, folder, name, description="Does things."):
    path = world.repo / folder / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n")


def use_home(monkeypatch, home):
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sync, "skill_home", lambda: home)
    return home


def add_user_skill(home, folder, name, description="Does things."):
    path = home / folder / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n")
    return path


def add_plugin_skill(home, tool, plugin, skill):
    root = {"claude": ".claude/plugins", "cursor": ".cursor/plugins", "codex": ".codex/plugins"}[tool]
    path = home / root / "cache" / "market" / plugin / "1.0.0" / "skills" / skill / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {skill}\ndescription: Plugin skill.\n---\n# {skill}\n")
    return path


SKILLS = """
[skills]
all = ["code-style"]

[steps.build]
skills = ["testing"]

[reviewers.ui]
enabled = true
skills = ["design-system"]

[reviewers.ux]
enabled = true

[surfaces.ui]
enabled = true
"""


def test_skills_resolve_per_step_and_reviewer(world):
    add_skill(world, ".agents/skills", "code-style")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".cursor/skills", "design-system")
    files, _ = plan(world, ALL_TOOLS + SKILLS)

    for tool_file in (".claude/agents/mfactory-build.md", ".cursor/agents/mfactory-build.md"):
        assert ("## Project skills\n\nRead each of these in the main checkout before you start, and apply it to "
                "this job:\n\n- `code-style`: `.agents/skills/code-style/SKILL.md`\n"
                "- `testing`: `.claude/skills/testing/SKILL.md`\n") in files[tool_file]
    codex = tomllib.loads(files[".codex/agents/mfactory-reviewer-ui.toml"])["developer_instructions"]
    assert "- `design-system`: `.cursor/skills/design-system/SKILL.md`" in codex
    spec = files[".claude/agents/mfactory-spec.md"]
    assert "`code-style`" in spec and "testing" not in spec


def test_claude_preloads_only_skills_in_its_own_folder(world):
    add_skill(world, ".agents/skills", "code-style")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".cursor/skills", "design-system")
    files, _ = plan(world, SKILLS)
    assert "skills:\n  - testing\n" in files[".claude/agents/mfactory-build.md"]
    assert "skills:" not in files[".claude/agents/mfactory-spec.md"]


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


def test_factory_skills_lists_what_is_on_disk_and_who_reads_it(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    add_skill(world, ".agents/skills", "code-style", "House style for this repo.")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".claude/skills", "unused")
    add_skill(world, ".claude/skills", "factory")             # the factory's own skill is not a project skill
    add_skill(world, ".claude/skills", "mfactory")
    cfg = load(world, "[skills]\nall = [\"code-style\"]\n[steps.build]\nskills = [\"testing\"]\n")
    text = sync.describe_skills(cfg)
    assert ('code-style  .agents/skills/code-style/SKILL.md\n    skills = ["code-style"]\n'
            "    read by: every agent\n    House style") in text
    assert 'testing  .claude/skills/testing/SKILL.md\n    skills = ["testing"]\n    read by: build\n' in text
    assert 'unused  .claude/skills/unused/SKILL.md\n    skills = ["unused"]\n    read by: no agent\n' in text
    assert "factory  " not in text
    assert "mfactory  " not in text
    assert "\nuser\n  (none)\n" in text
    assert "\nplugin\n  (none)\n" in text


def test_user_skill_uses_the_first_home_folder_in_every_tool(world, monkeypatch):
    home = use_home(monkeypatch, world.tmp / "home")
    add_user_skill(home, ".agents/skills", "python-best-practices", "House Python.")
    add_user_skill(home, ".cursor/skills", "python-best-practices", "Other copy.")
    files, notes = plan(world, ALL_TOOLS + '[steps.build]\nskills = ["user:python-best-practices"]\n')
    line = "- `python-best-practices`: `~/.agents/skills/python-best-practices/SKILL.md`"
    assert line in files[".claude/agents/mfactory-build.md"]
    assert line in files[".cursor/agents/mfactory-build.md"]
    codex = tomllib.loads(files[".codex/agents/mfactory-build.toml"])["developer_instructions"]
    assert line in codex
    assert "## User skills" in codex
    assert not any(note.startswith("skill ") for note in notes)
    assert "skills:" not in files[".claude/agents/mfactory-build.md"].split("---\n", 2)[1]


def test_a_missing_user_or_plugin_skill_syncs_and_is_listed_as_missing(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    text = ALL_TOOLS + """
[steps.build]
skills = ["user:missing", "superpowers:test-driven-development"]
"""
    files, notes = plan(world, text)
    assert 'skill "user:missing" is not installed on this machine' in notes
    for label in ("Claude Code", "Cursor", "Codex"):
        assert f'skill "superpowers:test-driven-development" is not installed or enabled for {label}' in notes
    claude = files[".claude/agents/mfactory-build.md"]
    assert "## User skills" in claude and "- `missing`" in claude
    assert "Invoke each of these with the Skill tool before you start" in claude
    assert "- `superpowers:test-driven-development`" in claude
    assert "Use each of these skills by name before you start" in files[".cursor/agents/mfactory-build.md"]
    codex = tomllib.loads(files[".codex/agents/mfactory-build.toml"])["developer_instructions"]
    assert "- `$superpowers:test-driven-development`" in codex
    listed = sync.describe_skills(load(world, text))
    assert 'user:missing  not installed\n    skills = ["user:missing"]\n    read by: build' in listed
    assert "superpowers:test-driven-development" in listed
    assert "Claude Code: not installed" in listed
    assert "Cursor: not installed" in listed
    assert "Codex: not installed" in listed


def test_a_bad_user_or_plugin_name_names_its_key(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    with pytest.raises(ConfigError) as error:
        plan(world, '[skills]\nall = ["user:"]\n[steps.build]\nskills = ["user:a/b", ":skill", "plugin:"]\n')
    assert error.value.problems == [
        '[skills].all: skill "user:" needs a user skill name after "user:", such as user:python-best-practices',
        '[steps.build].skills: skill "user:a/b" needs a user skill name after "user:", '
        "such as user:python-best-practices",
        '[steps.build].skills: skill ":skill" needs a plugin skill id, such as superpowers:test-driven-development',
        '[steps.build].skills: skill "plugin:" needs a plugin skill id, such as superpowers:test-driven-development',
    ]


def test_all_takes_every_skill_the_tool_can_see(world, monkeypatch):
    home = use_home(monkeypatch, world.tmp / "home")
    add_skill(world, ".claude/skills", "testing")
    add_skill(world, ".claude/skills", "mfactory")
    add_user_skill(home, ".cursor/skills", "python-best-practices")
    add_plugin_skill(home, "claude", "superpowers", "test-driven-development")
    add_plugin_skill(home, "cursor", "databricks-skills", "databricks-core")
    add_plugin_skill(home, "codex", "pdf", "pdf")
    add_plugin_skill(home, "codex", "spreadsheets", "spreadsheets")
    (home / ".codex" / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        '[plugins."pdf@openai-primary-runtime"]\nenabled = true\n\n'
        '[plugins."spreadsheets@openai-primary-runtime"]\nenabled = false\n')
    files, notes = plan(world, ALL_TOOLS + '[skills]\nall = ["all"]\n')
    assert not any(note.startswith("skill ") for note in notes)
    claude = files[".claude/agents/mfactory-build.md"]
    assert "- `testing`: `.claude/skills/testing/SKILL.md`" in claude
    assert "mfactory" not in claude.split("## Project skills", 1)[-1].split("##", 1)[0]
    assert "- `python-best-practices`: `~/.cursor/skills/python-best-practices/SKILL.md`" in claude
    assert ("- `superpowers:test-driven-development`: "
            "`~/.claude/plugins/cache/market/superpowers/1.0.0/skills/test-driven-development/SKILL.md`") in claude
    assert "databricks-core" not in claude
    assert "pdf:pdf" not in claude
    cursor = files[".cursor/agents/mfactory-build.md"]
    assert "databricks-skills:databricks-core" in cursor
    assert "superpowers:test-driven-development" not in cursor
    codex = tomllib.loads(files[".codex/agents/mfactory-build.toml"])["developer_instructions"]
    assert "- `$pdf:pdf`: `~/.codex/plugins/cache/market/pdf/1.0.0/skills/pdf/SKILL.md`" in codex
    assert "spreadsheets" not in codex
    assert "tools" not in frontmatter(files[".claude/agents/mfactory-intake.md"])
    head = claude.split("---\n", 2)[1]
    assert "skills:\n  - testing\n" in head
    assert "python-best-practices" not in head
    listed = sync.describe_skills(load(world, ALL_TOOLS + '[skills]\nall = ["all"]\n'))
    assert 'testing  .claude/skills/testing/SKILL.md\n    skills = ["testing"]\n    read by: every agent' in listed
    assert ('user:python-best-practices  ~/.cursor/skills/python-best-practices/SKILL.md\n'
            '    skills = ["user:python-best-practices"]\n    read by: every agent') in listed
    assert "Claude Code: ~/.claude/plugins/cache/market/superpowers/" in listed
    assert "spreadsheets" not in listed
    assert "`all` in that list reads every project skill" in listed


def test_claude_prefers_the_recorded_plugin_install(world, monkeypatch):
    home = use_home(monkeypatch, world.tmp / "home")
    add_plugin_skill(home, "claude", "superpowers", "test-driven-development")
    recorded = home / ".claude/plugins/cache/official/superpowers/9.0.0/skills/test-driven-development/SKILL.md"
    recorded.parent.mkdir(parents=True)
    recorded.write_text("---\nname: test-driven-development\ndescription: Recorded.\n---\n")
    record = {
        "plugins": {
            "superpowers@claude-plugins-official": [
                {"scope": "user", "installPath": str(recorded.parents[2]), "version": "9.0.0"}],
        },
    }
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps(record))
    files, _ = plan(world, '[steps.build]\nskills = ["superpowers:test-driven-development"]\n')
    assert "`~/.claude/plugins/cache/official/superpowers/9.0.0/skills/test-driven-development/SKILL.md`" in files[
        ".claude/agents/mfactory-build.md"]


def test_clearing_rules_drops_the_instruction_section(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    (world.repo / "AGENTS.md").write_text("# Agents\n")
    files, _ = plan(world, '[rules]\nall = []\n[steps.build]\nrules = []\n')
    assert "## Instruction files" not in files[".claude/agents/mfactory-build.md"]


def test_all_rules_lists_docs_and_rule_files_and_skips_skills(world, monkeypatch):
    home = use_home(monkeypatch, world.tmp / "home")
    (world.repo / "AGENTS.md").write_text("# Agents\n")
    (world.repo / "CLAUDE.md").write_text("# Claude\n")
    add_skill(world, ".claude/skills", "testing")
    rule = world.repo / ".claude/rules/testing.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("# Test rule\n")
    (world.repo / ".claude/rules/SKILL.md").write_text("# Not a rule\n")
    cursor_rule = world.repo / ".cursor/rules/ui/style.mdc"
    cursor_rule.parent.mkdir(parents=True)
    cursor_rule.write_text("# Style\n")
    (home / ".claude/CLAUDE.md").parent.mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("# User\n")
    files, notes = plan(world, '[skills]\nall = ["all"]\n[rules]\nall = ["all"]\n')
    body = files[".claude/agents/mfactory-build.md"]
    section = body.split("## Instruction files", 1)[1].split("## Project skills", 1)[0]
    assert "- `AGENTS.md`" in section
    assert "- `CLAUDE.md`" in section
    assert "- `.claude/rules/testing.md`" in section
    assert "- `.cursor/rules/ui/style.mdc`" in section
    assert "- `~/.claude/CLAUDE.md`" in section
    assert "SKILL.md" not in section
    assert not any(note.startswith("rule ") for note in notes)
    listed = sync.describe_skills(load(world, '[rules]\nall = ["all"]\n'))
    assert 'AGENTS.md  AGENTS.md\n    rules = ["AGENTS.md"]\n    read by: every agent' in listed
    assert 'user:CLAUDE.md  ~/.claude/CLAUDE.md\n    rules = ["user:CLAUDE.md"]\n    read by: every agent' in listed


def test_a_missing_project_rule_fails_and_a_missing_user_rule_syncs(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    (world.repo / "AGENTS.md").unlink()
    with pytest.raises(ConfigError) as error:
        plan(world, '[rules]\nall = []\n[steps.build]\nrules = ["AGENTS.md"]\n')
    assert error.value.problems == ['[steps.build].rules: rule "AGENTS.md" not found in the repository']
    files, notes = plan(world, '[steps.build]\nrules = ["user:CLAUDE.md"]\n')
    assert 'rule "user:CLAUDE.md" is not installed on this machine' in notes
    assert "- `user:CLAUDE.md`" in files[".claude/agents/mfactory-build.md"]
    listed = sync.describe_skills(load(world, '[steps.build]\nrules = ["user:CLAUDE.md"]\n'))
    assert 'user:CLAUDE.md  not installed\n    rules = ["user:CLAUDE.md"]\n    read by: build' in listed


def test_a_rule_path_that_escapes_its_root_is_rejected(world, monkeypatch):
    use_home(monkeypatch, world.tmp / "home")
    with pytest.raises(ConfigError) as error:
        plan(world, '[rules]\nall = ["user:../../.ssh/config", ".claude/rules/../../AGENTS.md"]\n')
    assert error.value.problems == [
        '[rules].all: rule "user:../../.ssh/config" must be user:CLAUDE.md, user:.codex/AGENTS.md, '
        "user:.agents/AGENTS.md, or a file under user:.claude/rules or user:.cursor/rules",
        '[rules].all: rule ".claude/rules/../../AGENTS.md" must be AGENTS.md, CLAUDE.md, CLAUDE.local.md, '
        ".claude/CLAUDE.md, or a file under .claude/rules, .cursor/rules, or .agents/rules",
    ]
