"""One project that takes jobs from GitHub issues and a Jira board at once."""

import json

import pytest
from conftest import JIRA_COLUMNS
from modfactory import config
from modfactory.service import tracker as trackers
from modfactory.service.tracker import GitHubTracker, JiraTracker, Trackers
from modfactory.util import MARKER, ConfigError, Refused
from modfactory.workspace import jobs as workspace_jobs
from modfactory.workspace.project import Project

BOTH = """
[tracker]
kind = ["github", "jira"]

[tracker.jira]
site = "https://acme.atlassian.net"
board = 7
"""
ONLY_JIRA = BOTH.replace('kind = ["github", "jira"]', 'kind = "jira"')


def problems(world, text, project=""):
    configure(world, text, project)
    with pytest.raises(ConfigError) as caught:
        config.load(world.repo)
    return "\n".join(caught.value.problems)


def configure(world, text, project=""):
    """`text` after the base config, and `project` lines inside its existing [project] table."""
    world.configure(text)
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace("[project]\n", "[project]\n" + project, 1))


# ---- [tracker].kind ---------------------------------------------------------------


def test_kind_takes_a_list_of_both_trackers(world):
    world.configure(BOTH + JIRA_COLUMNS)
    assert config.tracker_kinds(config.load(world.repo)["tracker"]["kind"]) == ["github", "jira"]


@pytest.mark.parametrize("kind", ["[]", '["jira", "jira"]', '["gitlab"]', "5"])
def test_kind_refuses_empty_duplicate_unknown_and_junk(world, kind):
    found = problems(world, BOTH.replace('["github", "jira"]', kind) + JIRA_COLUMNS)
    assert '[tracker].kind must be "github", "jira", or a list of them' in found


def test_tracker_kinds_reads_a_string_a_list_or_junk():
    assert config.tracker_kinds("jira") == ["jira"]
    assert config.tracker_kinds(["jira", "github"]) == ["jira", "github"]
    assert config.tracker_kinds(5) == []
    assert config.tracker_kinds(["github", 3]) == []


# ---- People, named per tracker ----------------------------------------------------


def test_two_trackers_need_people_tables(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nauthors = ["alice"]\n')
    assert "[run].authors" in found
    assert "authors = { github = [...], jira = [...] }" in found
    world.configure(BOTH + JIRA_COLUMNS + '\n[run]\nauthors = { github = ["alice"], jira = ["712020:abc"] }\n')
    assert config.load(world.repo)["run"]["authors"] == {"github": ["alice"], "jira": ["712020:abc"]}


def test_people_tables_check_each_entry_by_its_tracker(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nassignees = { github = ["bad login!"] }\n')
    assert "[run].assignees.github" in found
    assert "GitHub logins" in found


def test_a_people_table_names_only_known_trackers(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nauthors = { gitlab = ["alice"] }\n')
    assert "[run].authors.gitlab" in found


def test_who_table_must_name_every_tracker(world):
    step = '\n[steps.spec-approval]\nenabled = true\nwho = { github = ["alice"] }\n'
    found = problems(world, BOTH + JIRA_COLUMNS + step)
    assert "[steps.spec-approval].who" in found
    assert "jira" in found
    world.configure(BOTH + JIRA_COLUMNS
                    + '\n[steps.spec-approval]\nenabled = true\nwho = { github = ["alice"], jira = ["712020:abc"] }\n')
    assert config.load(world.repo).steps["spec-approval"]["who"] == {"github": ["alice"], "jira": ["712020:abc"]}


def test_a_who_word_stays_a_plain_string_with_two_trackers(world):
    world.configure(BOTH + JIRA_COLUMNS + '\n[steps.spec-approval]\nenabled = true\nwho = "operator"\n')
    assert config.load(world.repo).steps["spec-approval"]["who"] == "operator"


def test_one_tracker_keeps_plain_people_lists(world):
    world.configure('[run]\nauthors = ["alice"]\n')
    assert config.load(world.repo)["run"]["authors"] == ["alice"]
    assert "[run].authors" in problems(world, '[run]\nauthors = { github = ["alice"] }\n')


def test_people_for_and_who_for_resolve_one_tracker():
    assert config.people_for({"github": ["a"]}, "jira") == []
    assert config.people_for({"github": ["a"], "jira": ["b"]}, "jira") == ["b"]
    assert config.people_for(["a"], "github") == ["a"]
    assert config.people_for(None, "github") == []
    assert config.who_for({"who": {"github": ["a"], "jira": ["b"]}}, "jira") == ["b"]
    assert config.who_for({"who": ["a"]}, "jira") == ["a"]
    assert config.who_for({"who": "author"}, "jira") == "author"
    assert config.who_for({}, "github") == "maintainer"


# ---- Rules that differed per tracker ----------------------------------------------


def test_closes_is_allowed_with_both_trackers(world):
    closes = 'issue_link = "closes"\n'
    configure(world, BOTH + JIRA_COLUMNS, closes)
    assert config.load(world.repo)["project"]["issue_link"] == "closes"
    assert 'issue_link = "closes" needs GitHub issues' in problems(world, ONLY_JIRA + JIRA_COLUMNS, closes)


def test_a_branch_pattern_must_accept_branches_with_and_without_a_jira_key(world):
    # Accepts `factory/012-...` but not `factory/PROJ-123-012-...`.
    found = problems(world, BOTH + JIRA_COLUMNS, 'branch_pattern = "factory/[0-9][a-z0-9-]+"\n')
    assert "PROJ-123-" in found
    configure(world, BOTH + JIRA_COLUMNS, 'branch_pattern = "factory/[A-Za-z0-9-]+"\n')
    assert config.load(world.repo)["project"]["branch_pattern"] == "factory/[A-Za-z0-9-]+"


def test_jira_still_needs_a_site_and_a_board_with_both(world):
    found = problems(world, '[tracker]\nkind = ["github", "jira"]\n' + JIRA_COLUMNS)
    assert "[tracker.jira].site is missing" in found
    assert "[tracker.jira].board is missing" in found


# ---- Routing each issue to its tracker -------------------------------------------


def both(world):
    world.use_both()
    return trackers.make(config.load(world.repo))


def test_one_kind_still_makes_the_plain_tracker(world):
    plain = trackers.make(config.load(world.repo))
    assert type(plain) is GitHubTracker
    assert plain.of(12) is plain
    assert plain.members == [plain] and plain.primary is plain
    assert plain.board_member() is None
    assert plain.problems == [] and plain.unread == set()
    world.use_jira()
    jira = trackers.make(config.load(world.repo))
    assert isinstance(jira, JiraTracker)
    assert jira.of("PROJ-1") is jira and jira.board_member() is jira


def test_router_sends_each_key_to_its_tracker(world):
    found = both(world)
    assert isinstance(found, Trackers)
    assert found.key("12") == 12 and found.key("#12") == 12
    assert found.of(12).kind == "github"
    assert found.key("proj-3") == "PROJ-3"
    assert found.of("PROJ-3").kind == "jira"
    assert found.ref(12) == "#12" and found.ref("PROJ-3") == "PROJ-3"


def test_router_refuses_a_key_neither_tracker_takes(world):
    found = both(world)
    with pytest.raises(Refused, match=r"12.*PROJ-123"):
        found.key("x y")


def test_a_tracker_names_the_tracker_a_key_belongs_to(world):
    plain = trackers.make(config.load(world.repo))
    with pytest.raises(Refused, match=r"PROJ-3.*Jira.*\[tracker\]\.kind"):
        plain.of("PROJ-3")


def test_queue_takes_turns_primary_first(world):
    found = both(world)
    first, second = world.issue("First on GitHub"), world.issue("Second on GitHub")
    one, two = world.jira.ticket("First in Jira"), world.jira.ticket("Second in Jira")
    assert [issue["number"] for issue in found.queue()] == [first, one, second, two]


def test_statuses_splits_keys_by_tracker(world):
    found = both(world)
    key = world.jira.ticket("On the board", status="Build")
    number = world.issue("On GitHub")
    assert found.statuses([number, key]) == {key: "Build"}


def test_pr_link_rule_has_one_rule_per_tracker(world):
    rule = both(world).pr_link_rule()
    assert "number such as 12" in rule and "`Refs #<issue>`" in rule
    assert "key such as PROJ-123" in rule and "`Refs PROJ-123`" in rule


def test_new_issues_go_to_the_first_tracker(world):
    found = both(world)
    assert found.create_issue("Filed by the factory", "Body").startswith("https://github.test/")
    world.use_both(kind='["jira", "github"]')
    first_jira = trackers.make(config.load(world.repo))
    assert "/browse/PROJ-" in first_jira.create_issue("Filed by the factory", "Body")


def test_the_router_has_no_per_tracker_facts(world):
    found = both(world)
    for name in ("kind", "label", "pr_threads", "links_by_title", "drag_answers", "comment_limit",
                 "operator", "mention", "status_for", "requeue_hint"):
        assert not hasattr(found, name), name


def test_board_member_and_members(world):
    found = both(world)
    assert [member.kind for member in found.members] == ["github", "jira"]
    assert found.primary is found.members[0]
    assert found.board_member() is found.member("jira")
    with pytest.raises(Refused, match="gitlab"):
        found.member("gitlab")


# ---- GitHub and Jira jobs side by side --------------------------------------------

SPEC_APPROVAL = "[steps.spec-approval]\nenabled = true\n"


def side_by_side(world, until="spec-approval", extra=SPEC_APPROVAL):
    """A job from a GitHub issue and one from a Jira card, both played to `until`."""
    world.use_both(extra=extra)
    number, key = world.issue("On GitHub"), world.jira.ticket("In Jira")
    github_job, jira_job = world.create(number), world.create(key)
    world.play(github_job, until)
    world.play(jira_job, until)
    return github_job, number, jira_job, key


def test_github_and_jira_jobs_run_side_by_side(world):
    github_job, number, jira_job, key = side_by_side(world)
    assert world.job(github_job)["step"] == world.job(jira_job)["step"] == "spec-approval"
    assert world.comments(number)[-1].startswith(f"{MARKER}\n### Factory: approve the spec before it is built")
    assert "factory:needs-human" in world.labels(number)
    assert world.jira.status(key) == "Spec approval"
    assert any("approve the spec before it is built" in text for text in world.jira.comments(key))


def test_each_job_reads_replies_from_its_own_tracker(world):
    github_job, number, jira_job, key = side_by_side(world)
    world.comment(number, "/mfactory approve", "maintainer")
    assert world.next(github_job)[0] == f"{github_job} #{number}: spec-approval APPROVE -> build"
    assert world.next(jira_job)[0].startswith("wait ")
    world.jira.drag(key, "Build", "maint")
    assert world.next(jira_job)[0] == f"{jira_job} {key}: spec-approval APPROVE -> build"


def test_pull_request_rules_follow_the_job(world):
    github_job, number, jira_job, key = side_by_side(world, until="ready-to-merge", extra="")
    assert world.job(github_job)["step"] == world.job(jira_job)["step"] == "ready-to-merge"
    titles = {pr["headRefName"]: pr["title"] for pr in world.gh["prs"].values()}
    assert f"[{key}]" in titles[world.job(jira_job)["branch"]]
    assert key in world.job(jira_job)["branch"]
    assert "[" not in titles[world.job(github_job)["branch"]]


def test_a_jira_pull_request_still_starts_with_refs_when_github_closes(world):
    world.use_both()
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace("[project]\n", '[project]\nissue_link = "closes"\n', 1))
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until="build")
    world.build(job)

    def wrong_body(state):
        for pr in state["prs"].values():
            pr["body"] = "Closes it"
    world.set_gh(wrong_body)
    with pytest.raises(Refused, match=f"body must start with `Refs {key}`"):
        world.done(job)


def test_agent_issue_line_names_the_tracker_only_with_two(world):
    world.use_both()
    key, number = world.jira.ticket(), world.issue()
    assert f"Issue: {key} (Jira)." in "\n".join(world.next(world.create(key)))
    assert f"Issue: #{number} (GitHub)." in "\n".join(world.next(world.create(number)))


def test_jobs_and_index_record_their_tracker(world):
    github_job, _, jira_job, _ = side_by_side(world, until="intake", extra="")
    assert world.job(github_job)["tracker"] == "github"
    assert world.job(jira_job)["tracker"] == "jira"
    index = json.loads((world.repo / ".factory" / "jobs" / "index.json").read_text())
    assert [entry["kind"] for entry in index["trackers"]] == ["github", "jira"]
    assert index["trackers"][1]["enqueue"].startswith("Move a card to `Ready for factory`")
    assert index["tracker"]["kind"] == "github"


def test_people_tables_filter_each_tracker(world):
    world.use_both(extra='[run]\nauthors = { github = ["alice"] }\n')
    mine, other = world.issue("By alice", author="alice"), world.issue("By bob", author="bob")
    key = world.jira.ticket("By bob, in Jira", reporter="bob")
    fresh = [row[1] for row in world.queue() if row[0] == "new"]
    assert mine in fresh and key in fresh
    assert other not in fresh


def test_jira_jobs_in_flight_survive_adding_github(world):
    world.use_jira(extra=SPEC_APPROVAL)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until="spec-approval")
    path = world.dir(job) / "job.json"
    old = json.loads(path.read_text())
    old.pop("tracker", None)  # a job from before both trackers
    path.write_text(json.dumps(old))
    world.use_both(extra=SPEC_APPROVAL)
    assert ("wait", key, job, "spec-approval") == world.queue()[0][:4]
    world.jira.drag(key, "Build", "maint")
    assert world.next(job)[0] == f"{job} {key}: spec-approval APPROVE -> build"


def test_the_workspace_passes_both_trackers_to_the_page(world):
    world.use_both()
    world.create(world.jira.ticket())
    [project] = workspace_jobs.build_snapshot([Project(world.repo)], lambda p: "app")["projects"]
    assert [entry["kind"] for entry in project["trackers"]] == ["github", "jira"]
    assert project["jobs"][0]["tracker"] == "jira"


def test_an_older_index_still_gives_the_page_its_one_tracker(world):
    world.start(until="spec")
    path = world.repo / ".factory" / "jobs" / "index.json"
    old = json.loads(path.read_text())
    del old["trackers"]
    path.write_text(json.dumps(old))
    [project] = workspace_jobs.build_snapshot([Project(world.repo)], lambda p: "app")["projects"]
    assert [entry["kind"] for entry in project["trackers"]] == ["github"]


# ---- One tracker down -------------------------------------------------------------


def test_a_jira_outage_does_not_stop_github_jobs(world):
    world.use_both()
    number = world.issue("On GitHub")
    world.jira.ticket("In Jira")
    world.jira.fail = ("/rest", 503)
    res = world.cli("queue")
    assert res.returncode == 0, res.stderr
    assert f"new  {number}  -  -  On GitHub" in res.stdout.splitlines()
    assert "problem: Jira: Jira answered 503" in res.stderr


def test_a_job_whose_tracker_is_down_waits_with_the_reason(world):
    world.use_both(extra=SPEC_APPROVAL)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until="spec-approval")
    number = world.issue("On GitHub")
    world.jira.fail = ("/rest", 503)
    rows = world.queue()
    [row] = [row for row in rows if row[2] == job]
    assert row[:4] == ("wait", key, job, "spec-approval")
    assert row[4].startswith("could not read Jira: Jira answered 503")
    assert ("new", number, "-", "-", "On GitHub") in rows


def test_every_tracker_down_still_fails(world):
    world.use_both()
    world.issue()
    world.jira.fail = ("/rest", 503)
    world.set_gh(lambda state: state.update(fail="issue list"))
    assert world.cli("queue").returncode != 0


def test_a_job_from_a_removed_tracker_is_skipped(world):
    world.use_both()
    key = world.jira.ticket()
    job = world.create(key)
    world.configure("")  # back to GitHub issues only
    rows = world.queue()
    assert ("skip", key, job, "intake", "its tracker, Jira, is not in [tracker].kind") in rows


# ---- Commands with both trackers --------------------------------------------------


def test_file_goes_to_the_first_tracker_or_the_one_named(world):
    world.use_both()
    first = world.cli("file", "Add a thing", input="Please add it.")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip().startswith("https://github.test/acme/app/issues/")
    named = world.cli("file", "--tracker", "jira", "Add a thing", input="Please add it.")
    assert named.returncode == 0, named.stderr
    assert named.stdout.strip() == f"{world.jira.url}/browse/PROJ-1"
    assert world.jira.status("PROJ-1") == "Ready for factory"


def test_file_refuses_a_tracker_the_project_does_not_use(world):
    res = world.cli("file", "--tracker", "jira", "Add a thing", input="Please add it.")
    assert res.returncode != 0
    assert "takes no jobs from Jira" in res.stderr


def test_doctor_checks_both_trackers(world):
    world.use_both()
    found = world.cli("doctor").stdout
    assert "GitHub labels" in found
    assert "Jira: signed in to" in found and "Jira board 7" in found


def test_tracker_show_uses_the_jira_board(world):
    world.use_both()
    res = world.cli("tracker", "show")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Jira board 7 at" in res.stdout


def test_tracker_sync_moves_only_jira_cards(world):
    world.use_both()
    key = world.jira.ticket()
    job = world.create(key)
    world.jira.drag(key, "Backlog", "op")
    world.factory().store.release()  # the command line takes the job's lock itself
    res = world.cli("tracker", "sync")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"moved  {key}  {job}" in res.stdout.replace("   ", "  ")


# ---- One job's call failing, while its tracker answers ----------------------------


def test_a_jira_error_while_settling_cards_leaves_the_github_jobs(world):
    world.use_both(extra=SPEC_APPROVAL)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until="spec-approval")
    number = world.issue("On GitHub")
    world.jira.drag(key, "Build", "maint")                     # settling reads the card's history
    world.jira.fail = (f"/rest/api/3/issue/{key}/changelog", 503)
    world.factory().store.release()  # the command line takes the job's lock itself
    res = world.cli("queue")
    assert res.returncode == 0, res.stderr
    assert f"new  {number}  -  -  On GitHub" in res.stdout.splitlines()
    assert f"problem: Jira: {key}: Jira answered 503" in res.stderr


def test_an_unresolvable_me_only_reports_a_problem(world):
    world.use_both(extra='[run]\npick = "mine"\n')
    number = world.issue("Mine", author="operator")
    world.jira.ticket("Also mine, in Jira", reporter="op")
    world.jira.fail = ("/rest/api/3/myself", 503)
    res = world.cli("queue")
    assert res.returncode == 0, res.stderr
    assert f"new  {number}  -  -  Mine" in res.stdout.splitlines()
    assert "problem: Jira: @me: Jira answered 503" in res.stderr


def test_one_jobs_error_leaves_the_other_jobs_rows_alone(world):
    world.use_jira(extra=SPEC_APPROVAL)
    world.use_both(extra=SPEC_APPROVAL)
    first, second = world.jira.ticket("First"), world.jira.ticket("Second")
    jobs = [world.create(first), world.create(second)]
    for job in jobs:
        world.play(job, until="spec-approval")
    world.jira.fail = (f"/rest/api/3/issue/{first}/comment", 503)
    rows = {row[1]: row for row in world.queue()}
    assert rows[first][4].startswith("could not read Jira: Jira answered 503")
    assert rows[second][:4] == ("wait", second, jobs[1], "spec-approval")
    assert rows[second][4] == "waiting for a maintainer"


def test_tracker_sync_skips_a_job_whose_tracker_left(world):
    world.use_both()
    number = world.issue("On GitHub")
    job = world.create(number)
    world.use_jira()                                           # GitHub is no longer in [tracker].kind
    world.factory().store.release()
    res = world.cli("tracker", "sync")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"skip   {number}  {job}  intake  its tracker, GitHub, is not in [tracker].kind" in res.stdout


def test_a_github_outage_does_not_stop_jira_jobs(world):
    world.use_both()
    world.issue("On GitHub")
    key = world.jira.ticket("In Jira")
    world.set_gh(lambda state: state.update(fail="issue list"))
    res = world.cli("queue")
    assert res.returncode == 0, res.stderr
    assert f"new  {key}  -  -  In Jira" in res.stdout.splitlines()
    assert "problem: GitHub:" in res.stderr
