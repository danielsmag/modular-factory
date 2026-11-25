"""`mfactory tracker suggest`: the board that works well for this pipeline, and the columns table for it."""

import json

from conftest import AUTO_MERGE
from modfactory import board, config

SPEC_APPROVAL = """
[steps.spec-approval]
enabled = true
"""
DEBUG = """
[steps.debug]
enabled = true
"""
VERIFY = """
[steps.verify]
enabled = true
commands = ["true"]
"""
CUSTOM_AGENT = """
[steps.approve]
on = { APPROVE = "lint-docs", ESCALATE = "escalated" }

[steps.lint-docs]
type = "agent"
instructions = "Check the docs."
on = { DONE = "ready-to-merge" }
"""
CUSTOM_HUMAN = """
[steps.approve]
on = { APPROVE = "release-approval", ESCALATE = "escalated" }

[steps.release-approval]
type = "human"
on = { SHIP = "ready-to-merge", REDO = { to = "build", max = 2 }, REJECT = "rejected" }
"""
# Both outcomes of a person's step lead to the same step, so no column tells them apart.
SAME_TARGET = """
[steps.approve]
on = { APPROVE = "sign-off", ESCALATE = "escalated" }

[steps.sign-off]
type = "human"
[steps.sign-off.on]
SHIP = "ready-to-merge"
SMALL = { to = "build", max = 2 }
LARGE = { to = "build", max = 2 }
REJECT = "rejected"
"""


def suggested(world, extra=""):
    world.configure(extra)
    cfg = config.load(world.repo)
    return board.suggest(cfg.steps, cfg["run"]["start"], cfg["run"]["escalate_to"])


def names(found):
    return [column["name"] for column in found["columns"]]


def column(found, name):
    return next(c for c in found["columns"] if c["name"] == name)


def printed(result):
    return [line for line in result.stdout.splitlines() if not line.startswith("note: ")]


# ---- The default pipeline ----------------------------------------------------------------------


def test_the_default_pipeline_gets_eleven_columns_left_to_right(world):
    assert names(suggested(world)) == ["Backlog", "Ready for factory", "Intake", "Needs info", "Spec", "Build",
                                       "Review", "Ready to merge", "Escalated", "Done", "Rejected"]


def test_the_factorys_steps_share_a_column_and_a_persons_step_has_its_own(world):
    found = suggested(world)
    assert column(found, "Build")["steps"] == ["build", "gate"]
    assert column(found, "Review")["steps"] == ["review", "approve"]
    assert column(found, "Needs info")["steps"] == ["ask-author"]
    assert [c["name"] for c in found["columns"] if c["who"] == "you"] == [
        "Backlog", "Needs info", "Ready to merge", "Escalated"]


def test_the_columns_table_maps_every_step_that_runs_and_no_step_that_is_off(world):
    found = suggested(world)
    assert found["mapping"] == {
        "queue": "Ready for factory", "intake": "Intake", "ask-author": "Needs info", "spec": "Spec",
        "build": "Build", "gate": "Build", "review": "Review", "approve": "Review",
        "ready-to-merge": "Ready to merge", "escalated": "Escalated", "done": "Done", "rejected": "Rejected"}
    assert found["toml"].splitlines()[:3] == [
        "[tracker.jira.columns]", 'queue = "Ready for factory"', 'intake = "Intake"']


def test_the_suggested_table_is_a_valid_config_where_every_drag_is_clear(world):
    found = suggested(world, SPEC_APPROVAL)
    world.use_jira(extra=SPEC_APPROVAL, columns=found["toml"])
    cfg = config.load(world.repo)
    table = cfg["tracker"]["jira"]["columns"]
    for name, step in cfg.steps.items():
        if step["type"] == "human" and step.get("enabled", True):
            outcomes = board.drags(cfg.steps, lambda s: board.status_for(table, s), name).values()
            assert all(len(o) == 1 for o in outcomes), name
    assert found["notes"] == []


def test_only_the_end_columns_go_in_jiras_done_category(world):
    assert suggested(world)["done_category"] == ["Done", "Rejected"]


# ---- Steps switched on, added or changed ------------------------------------------------------------


def test_spec_approval_gets_a_column_between_spec_and_build(world):
    found = suggested(world, SPEC_APPROVAL)
    assert names(found)[4:7] == ["Spec", "Spec approval", "Build"]
    assert column(found, "Spec approval")["who"] == "you"


def test_debug_joins_the_build_column(world):
    assert column(suggested(world, DEBUG), "Build")["steps"] == ["build", "gate", "debug"]


def test_verify_after_a_hand_merge_gets_a_column_and_a_failed_column(world):
    found = suggested(world, VERIFY)
    assert names(found).index("Verify") == names(found).index("Ready to merge") + 1
    assert names(found)[-3:] == ["Done", "Rejected", "Failed"]
    assert found["done_category"] == ["Done", "Rejected", "Failed"]


def test_the_factory_merging_gets_a_merging_column_and_no_ready_to_merge(world):
    found = suggested(world, AUTO_MERGE)
    assert "Merging" in names(found)
    assert "Ready to merge" not in names(found)
    assert names(found).index("Merging") == names(found).index("Review") + 1


def test_a_custom_agent_step_joins_the_column_of_the_step_before_it(world):
    assert column(suggested(world, CUSTOM_AGENT), "Review")["steps"] == ["review", "approve", "lint-docs"]


def test_a_custom_persons_step_gets_its_own_column_named_after_it(world):
    found = suggested(world, CUSTOM_HUMAN)
    assert names(found)[6:9] == ["Review", "Release approval", "Ready to merge"]
    assert column(found, "Release approval")["who"] == "you"
    assert found["notes"] == []


def test_the_escalation_column_comes_last_before_the_end_columns(world):
    found = suggested(world, SPEC_APPROVAL + DEBUG)
    assert names(found)[-3] == "Escalated"


def test_two_outcomes_that_lead_to_the_same_step_are_noted(world):
    found = suggested(world, SAME_TARGET)
    assert found["notes"] == ["[steps.sign-off]: SMALL and LARGE both lead to `Build`, so moving the card there "
                              "answers nothing: answer them by comment"]


# ---- The command ---------------------------------------------------------------------------------------


def test_suggest_prints_the_board_and_the_table_to_paste(world):
    lines = printed(world.cli("tracker", "suggest"))
    assert lines[0] == "A Jira board for this pipeline, left to right. Name each column's status the same."
    table = {line.split()[0]: line for line in lines if line.startswith("  ")}
    assert table["Build"].split()[1:] == ["factory", "build,", "gate"]
    assert table["Escalated"].split()[1:] == ["you", "escalated"]
    assert table["Backlog"].split()[1] == "you"
    assert "[tracker.jira.columns]" in lines
    assert 'gate = "Build"' in lines
    assert any("Done category" in line and "`Done`, `Rejected`" in line for line in lines)
    assert any("Run this again after turning a step on" in line for line in lines)


def test_suggest_as_json_is_the_suggestion(world):
    found = json.loads(printed(world.cli("tracker", "suggest", "--json"))[0])
    assert found["mapping"]["gate"] == "Build"
    assert found["columns"][0]["name"] == "Backlog"


def test_suggest_needs_no_jira_and_reads_nothing_from_it(world):
    world.use_jira()
    res = world.cli("tracker", "suggest")
    assert res.returncode == 0
    assert world.jira.calls == []
