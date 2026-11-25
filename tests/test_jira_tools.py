"""`mfactory tracker show|sync` and doctor: seeing the mapping against the live board, and putting cards right."""

import json

from conftest import JIRA_COLUMNS
from modfactory import board, config
from modfactory.service import tracker as trackers

SPEC_APPROVAL = """
[steps.spec-approval]
enabled = true
"""


def printed(result):
    return [line for line in result.stdout.splitlines() if not line.startswith("note: ")]


def report(world, **use):
    world.use_jira(**use)
    cfg = config.load(world.repo)
    return trackers.make(cfg).board_report()


def row(found, step):
    return next(r for r in found["rows"] if r["step"] == step)


def doctor(world, **use):
    world.use_jira(**use)
    cfg = config.load(world.repo)
    lines = {"ok": [], "warn": [], "fail": []}
    trackers.make(cfg).doctor(lines["ok"].append, lines["warn"].append, lines["fail"].append)
    return lines


# ---- The transitions the pipeline needs, read from the graph ---------------------------------


def needed(world, extra="", columns=JIRA_COLUMNS):
    world.use_jira(extra=extra, columns=columns)
    cfg = config.load(world.repo)
    table = cfg["tracker"]["jira"]["columns"]
    return board.transitions(cfg.steps, lambda step: board.status_for(table, step), board.queue_statuses(table),
                             cfg["run"]["start"], cfg["run"]["escalate_to"])


def test_the_pipeline_needs_a_transition_for_every_edge_a_job_can_take(world):
    pairs = {(a, b) for a, b, _ in needed(world)}
    assert ("Ready for factory", "Intake") in pairs       # a new job
    assert ("Intake", "Spec") in pairs                    # intake ACCEPT
    assert ("Spec", "Build") in pairs                     # spec DONE, through spec-approval, which is off
    assert ("Checks", "Build") in pairs                   # gate FAIL, through debug, which is off
    assert ("Review", "Escalated") in pairs               # review CHANGES reaches its max
    assert ("Ready to merge", "Rejected") in pairs
    assert ("Spec", "Spec approval") not in pairs         # a step that is off gets no card


def test_each_needed_transition_says_which_edge_needs_it(world):
    why = {(a, b): reason for a, b, reason in needed(world)}
    assert why[("Intake", "Spec")] == "intake ACCEPT"
    assert why[("Ready for factory", "Intake")] == "a new job"


def test_an_unmapped_step_is_passed_over_to_the_next_mapped_one(world):
    pairs = {(a, b) for a, b, _ in needed(world, columns=JIRA_COLUMNS.replace('gate = "Checks"\n', ""))}
    assert ("Build", "Review") in pairs
    assert not any("Checks" in pair for pair in pairs)


def test_steps_that_share_a_status_need_no_transition_between_them(world):
    shared = JIRA_COLUMNS.replace('gate = "Checks"', 'gate = "Build"')
    assert ("Build", "Build") not in {(a, b) for a, b, _ in needed(world, columns=shared)}


# ---- tracker show --------------------------------------------------------------------------


def test_the_report_has_a_row_for_the_queue_every_step_and_every_end_state(world):
    found = report(world)
    assert [r["step"] for r in found["rows"]][:2] == ["queue", "intake"]
    assert row(found, "build") == {"step": "build", "type": "agent", "enabled": True, "status": "Build",
                                   "column": "Build", "problem": None}
    assert row(found, "done")["status"] == "Done"
    assert found["board"] == 7
    assert found["unused"] == ["Backlog"]
    assert found["problems"] == []


def test_a_status_jira_does_not_have_is_a_problem_on_its_row(world):
    found = report(world, columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Biuld"'))
    assert row(found, "build")["column"] is None
    assert row(found, "build")["problem"] == "Jira has no status `Biuld`"


def test_a_status_on_no_column_of_the_board_is_a_problem(world):
    found = report(world, columns=JIRA_COLUMNS.replace('failed = "Failed"', 'failed = "Archived"'))
    assert row(found, "failed")["problem"] == "`Archived` is on no column of board 7, so the card leaves the board"


def test_a_column_holding_several_statuses_is_named_for_each(world):
    world.use_jira()
    world.jira.columns = [(name, statuses) for name, statuses in world.jira.columns if name != "Checks"]
    world.jira.columns = [(name, [*statuses, "Checks"] if name == "Build" else statuses)
                          for name, statuses in world.jira.columns]
    found = report(world)
    assert (row(found, "gate")["status"], row(found, "gate")["column"]) == ("Checks", "Build")


def test_an_unmapped_step_is_shown_without_a_status(world):
    found = report(world, columns=JIRA_COLUMNS.replace('review = "Review"\n', ""))
    assert row(found, "review") == {"step": "review", "type": "review", "enabled": True, "status": None,
                                    "column": None, "problem": None}
    assert "Review" in found["unused"]


def test_a_transition_the_workflow_lacks_is_a_problem_that_names_the_edge(world):
    world.use_jira()
    world.jira.ticket(status="Intake")
    world.jira.workflow = {"Intake": ["Needs info", "Rejected"]}
    found = report(world)
    assert "the workflow has no transition from `Intake` to `Spec` (intake ACCEPT)" in found["problems"]
    assert not any("`Intake` to `Rejected`" in problem for problem in found["problems"])


def test_transitions_are_checked_only_where_a_card_can_show_them(world):
    world.use_jira()
    world.jira.ticket(status="Intake")
    found = report(world)
    assert found["problems"] == []
    assert "Intake" not in found["unchecked"]
    assert "Spec" in found["unchecked"]


def test_show_prints_the_mapping_as_a_table(world):
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Biuld"').replace(
        'review = "Review"\n', ""))
    lines = printed(world.cli("tracker", "show"))
    assert lines[0] == f"Jira board 7 at {world.jira.url}"
    assert lines[1].split() == ["step", "type", "status", "column"]
    table = {line.split()[0]: line for line in lines[2:]}
    assert table["queue"].split() == ["queue", "-", "Ready", "for", "factory", "Ready", "for", "factory"]
    assert table["build"].endswith("Jira has no status `Biuld`")
    assert table["review"].endswith("no status: the card stays where it is")
    assert table["spec-approval"].endswith("switched off")
    assert "statuses no step uses: Backlog, Build, Review" in lines


def test_show_as_json_is_the_report(world):
    world.use_jira()
    found = json.loads(printed(world.cli("tracker", "show", "--json"))[0])
    assert found["board"] == 7
    assert row(found, "intake")["status"] == "Intake"


def test_show_fails_when_the_board_has_a_problem(world):
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Biuld"'))
    assert world.cli("tracker", "show").returncode == 1
    world.use_jira()
    assert world.cli("tracker", "show").returncode == 0


def test_show_on_github_says_there_is_no_board(world):
    res = world.cli("tracker", "show")
    assert res.returncode == 0
    assert printed(res) == ["GitHub issues have no board: the queue is the `factory` label"]


# ---- tracker sync --------------------------------------------------------------------------


def test_sync_moves_every_unfinished_jobs_card_to_its_steps_status(world):
    world.use_jira()
    keys = [world.jira.ticket("One"), world.jira.ticket("Two")]
    jobs = [world.create(key) for key in keys]
    world.play(jobs[0], "build")
    world.play(jobs[1], "spec")
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Review"'))
    lines = printed(world.cli("tracker", "sync"))
    assert lines == [f"moved  {keys[0]}  {jobs[0]}  build  `Build` -> `Review`",
                     f"ok     {keys[1]}  {jobs[1]}  spec  `Spec`"]
    assert world.jira.status(keys[0]) == "Review"
    assert world.job(jobs[0])["step"] == "build"


def test_sync_leaves_finished_jobs_and_unmapped_steps_alone(world):
    world.use_jira()
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, "build")
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"\n', ""))
    assert printed(world.cli("tracker", "sync")) == [f"skip   {key}  {job}  build  no status for this step"]
    assert world.jira.status(key) == "Build"


def test_sync_reports_a_card_that_will_not_move(world):
    world.use_jira()
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, "build")
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Review"'))
    world.jira.workflow = {}
    world.factory()  # the test's own factory lets go of the job, as a finished command does
    res = world.cli("tracker", "sync")
    assert res.returncode == 1
    assert any(line.startswith(f"FAIL   {key}  {job}  build") and "`Build` to `Review`" in line
               for line in printed(res))


def test_sync_on_github_has_nothing_to_move(world):
    assert printed(world.cli("tracker", "sync")) == ["GitHub issues have no board: nothing to move"]


# ---- doctor --------------------------------------------------------------------------------


def test_doctor_passes_a_board_that_has_every_mapped_status(world):
    lines = doctor(world)
    assert lines["fail"] == []
    assert "Jira board 7: every mapped status is on the board" in lines["ok"]


def test_doctor_fails_on_a_status_the_board_lacks(world):
    lines = doctor(world, columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Biuld"'))
    assert "[tracker.jira.columns].build: Jira has no status `Biuld`" in lines["fail"]


def test_doctor_fails_on_a_missing_transition(world):
    world.use_jira()
    world.jira.ticket(status="Intake")
    world.jira.workflow = {"Intake": ["Needs info", "Rejected"]}
    lines = doctor(world)
    assert "Jira: the workflow has no transition from `Intake` to `Spec` (intake ACCEPT)" in lines["fail"]


def test_doctor_says_which_statuses_it_could_not_check(world):
    world.use_jira()
    world.jira.ticket(status="Intake")
    lines = doctor(world)
    assert any(line.startswith("Jira: transitions not checked from ") and "Spec" in line for line in lines["warn"])


def test_doctor_warns_about_a_column_two_outcomes_share(world):
    lines = doctor(world, extra=SPEC_APPROVAL, columns=JIRA_COLUMNS.replace('spec = "Spec"', 'spec = "Build"'))
    assert ("[steps.spec-approval]: moving the card to `Build` could mean APPROVE or CHANGES, so it is "
            "answered by comment only") in lines["warn"]


def test_doctor_warns_about_a_human_step_with_no_status(world):
    lines = doctor(world, columns=JIRA_COLUMNS.replace('ready-to-merge = "Ready to merge"\n', ""))
    assert ("[steps.ready-to-merge] has no status in [tracker.jira.columns], so it is answered by comment "
            "only") in lines["warn"]


def test_doctor_stops_at_credentials_jira_refuses(world, monkeypatch):
    world.use_jira()
    monkeypatch.setenv("JIRA_API_TOKEN", "a-wrong-token")
    cfg = config.load(world.repo)
    lines = {"ok": [], "warn": [], "fail": []}
    trackers.make(cfg).doctor(lines["ok"].append, lines["warn"].append, lines["fail"].append)
    assert len(lines["fail"]) == 1
    assert "JIRA_API_TOKEN" in lines["fail"][0]


# ---- Jira's Done category --------------------------------------------------------------------


def test_a_queue_status_in_jiras_done_category_is_a_problem(world):
    found = report(world, columns=JIRA_COLUMNS.replace('queue = "Ready for factory"', 'queue = "Failed"'))
    assert row(found, "queue")["problem"] == ("`Failed` is in Jira's Done category, so a card there counts as a "
                                              "closed issue and no job starts from it")


def test_a_steps_status_in_jiras_done_category_is_a_warning(world):
    found = report(world, columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Done"'))
    assert row(found, "build")["problem"] is None
    assert found["warnings"] == ["[tracker.jira.columns].build: `Done` is in Jira's Done category, so Jira counts "
                                 "the issue as finished while the job is at build"]


def test_end_states_belong_in_jiras_done_category(world):
    found = report(world)
    assert found["warnings"] == []
    assert row(found, "done")["problem"] is None


def test_show_and_doctor_pass_on_the_warnings(world):
    columns = JIRA_COLUMNS.replace('build = "Build"', 'build = "Done"')
    world.use_jira(columns=columns)
    res = world.cli("tracker", "show")
    assert res.returncode == 0
    assert any(line.startswith("warning: [tracker.jira.columns].build") for line in printed(res))
    lines = doctor(world, columns=columns)
    assert any(line.startswith("[tracker.jira.columns].build: `Done` is in Jira's Done category")
               for line in lines["warn"])
