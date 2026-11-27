# GitHub and Jira Together, and a Three-Block Board: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one project take factory jobs from GitHub issues and a Jira board at once, and turn the dashboard's Board into three blocks: Jira boards (one or several), open GitHub issues, and the factory's own jobs and queue.

**Architecture:** `[tracker] kind` takes a list. A `Trackers` router sends each issue key to its tracker and takes the queues in turn. A single tracker stays the plain object it is today, and gets the router's set API from a mixin. The dashboard keeps its stdlib server: a route table replaces the `do_GET` if-chain, and three new read-only routes are served from server-agnostic readers in `workspace/feeds.py`, with a keyed TTL cache. The page draws the blocks with the existing `el()` helpers. The board picker is a dialog mounted outside `#view`.

**Tech Stack:** Python 3.11+ standard library only, vanilla JS, pytest with the repo's stubs (`tests/stub_gh.py`, `tests/stub_jira.py`), and Playwright (preinstalled Chromium) for the visual check.

**Spec:** `docs/superpowers/specs/2026-11-27-github-and-jira-together-design.md` (approved). Section numbers below (§1.2, §2.3 …) refer to it.

## Global Constraints

- Standard library only. No new dependency, no pip step in `install.sh`.
- Type hints everywhere, including locals. Ruff with line length 120 and rules E, F, W, I, B, UP, SIM. Target py311.
- **A one-tracker project behaves exactly as today:** `make()` returns the plain tracker, and the generated agent files are word for word the same. All 439 existing tests pass with no edits to their assertions.
- **Web page:**
  - Nodes are built only with `el()` / `svg()`. No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval(`.
  - Every storage access goes through `prefs`, inside try/catch.
- **Dashboard server:**
  - GET only, localhost `Host` check, the same CSP.
  - Tokens never reach the page, an error message or a file.
  - Every string sent goes through `activity.redact`. URLs must be `https://`, or `http://` on `127.0.0.1` / `localhost`.
- The readers in `workspace/feeds.py` and the new tracker functions import nothing from `http.server`, and take their Jira `Client` / `GitHub` as arguments.
- **Commits:**
  - Subject: an imperative sentence in sentence case, ending with a period.
  - Body: plain prose.
  - Each ends with the session's `Co-Authored-By` / `Claude-Session` lines.
  - Never touch `core/VERSION`.
- Every check runs as `ruff check . && bash -n install.sh && python3 -m pytest -o addopts=-q`. The container has no pytest-xdist.

## Review Focus

1. **Switching `kind = "jira"` to `["github", "jira"]` with Jira jobs in flight.** Old jobs have no `"tracker"` field; they must keep running, routed by key shape. Test in Task 3.
2. **Jira credentials missing from the dashboard's environment**, though set in the shell that runs jobs. The Jira block says which variables to set, and the GitHub and factory blocks still load. Test in Task 7.
3. **A picked board id the account can no longer view** (deleted board, lost access), still remembered by the browser. Only that board's area shows the error; the others render. Test in Task 7, page behaviour checked in Task 8.
4. **Keys typed in other forms:** `proj-12` (lowercase) and `#12` route to the right tracker. Test in Task 2.
5. **Two workspace projects whose `[tracker.jira]` name different `email_env` / `token_env`.** Each project's routes use its own variables. Test in Task 7.

---

### Task 1: `[tracker] kind` takes a list, and people are named per tracker

**Files:**
- Modify: `core/lib/modfactory/config.py` (`TRACKER_KINDS` area at line 388; `check()` lines 536-567)
- Modify: `core/defaults.toml:46-60`, `core/templates/factory.toml:226-252` (comments only)
- Test: `tests/test_both_trackers.py` (create)

**Interfaces:**
- Produces:
  - `config.tracker_kinds(raw: object) -> list[str]`: the kinds named, in order. `[]` for junk, so `check()` reports it.
  - `config.people_for(value: object, kind: str) -> list[str]`: a list as-is, or the table's entry for `kind` (`[]` when absent).
  - `config.who_for(step: dict, kind: str) -> str | list[str]`: `step.get("who", "maintainer")`, resolved for `kind` when it is a table.

- [ ] **Step 1: Write the failing tests.** Each `world.configure(...)` passes or fails as stated, with `ConfigError.problems` text asserted like `test_jira_board.py` does.
  - `test_kind_takes_a_list_of_both_trackers`: `kind = ["github", "jira"]` plus Jira site, board and columns gives no problems.
  - `test_kind_refuses_empty_duplicate_unknown_and_junk`: `[]`, `["jira","jira"]`, `["gitlab"]` and `5` each give a problem containing `[tracker].kind must be "github", "jira", or a list of them`.
  - `test_two_trackers_need_people_tables`: `[run] authors = ["alice"]` with both kinds gives a problem containing `authors = { github = [...], jira = [...] }`. `authors = { github = ["alice"], jira = ["712020:abc"] }` passes.
  - `test_people_tables_check_each_entry_by_its_tracker`: `authors = { github = ["bad login!"] }` fails with "GitHub logins".
  - `test_who_table_must_name_every_tracker`: `[steps.spec-approval] who = { github = ["alice"] }` with both kinds fails, naming `jira`.
  - `test_closes_is_allowed_with_both_trackers`: `issue_link = "closes"` passes with both, and still fails with `kind = "jira"`.
  - `test_people_for_and_who_for_resolve_one_tracker`: direct calls. `people_for({"github": ["a"]}, "jira") == []`, `people_for(["a"], "github") == ["a"]`, `who_for({"who": {"github": ["a"], "jira": ["b"]}}, "jira") == ["b"]`, `who_for({}, "github") == "maintainer"`.
- [ ] **Step 2: Run them; they fail.** `python3 -m pytest -o addopts=-q tests/test_both_trackers.py`
- [ ] **Step 3: Implement.**
  - The kind validator replaces `one_of(*TRACKER_KINDS)` for `kind`. `uses_jira` / `uses_github` derive from `tracker_kinds`.
  - With two kinds, `authors` / `assignees` use a table validator: per entry, `a_login_list` for github and `a_jira_account_list` for jira. A human step's `who` takes `a_who` / `a_jira_who` per entry, and a table must have every kind.
  - The branch pattern is checked for `""` and `_KEY_SAMPLE` when Jira is on with GitHub.
  - The `closes` refusal applies only when `tracker_kinds == ["jira"]`.
  - Defaults and template comments show `kind = ["github", "jira"]` and a people table.
- [ ] **Step 4: Run Task 1's tests and the full suite.** They pass, and the 439 old tests still pass.
- [ ] **Step 5: Commit** `Let [tracker] kind name both trackers, with people named per tracker.`

### Task 2: The router, `Trackers`

**Files:**
- Create: `core/lib/modfactory/service/tracker/routing.py`
- Modify: `service/tracker/base.py` (add `OneTracker`), `github.py` / `jira.py` (inherit it), `service/tracker/__init__.py` (`make`, `for_repo`)
- Test: `tests/test_both_trackers.py`
- Modify: `tests/conftest.py`: `World.use_both(extra="", columns=JIRA_COLUMNS, settings="")` writes `kind = ["github", "jira"]` plus the Jira table, and sets `self.github_issues = True` so `issue()` makes GitHub issues while `self.jira.ticket()` makes Jira cards.

**Interfaces:**
- Consumes: `config.tracker_kinds`.
- Produces, on every tracker object (plain or router):
  - `of(key: Key) -> Tracker`
  - `members: list[Tracker]`, `primary: Tracker`
  - `member(kind: str) -> Tracker` (refuses an unknown kind)
  - `board_member() -> Tracker | None`
  - `problems: list[str]`, `unread: set[str]` (empty on a plain tracker)
- Produces, on `Trackers(members: list[Tracker])`:
  - key routing for: `key`, `ref`, `issue`, `issue_details`, `withdrawn`, `queue_gate`, `dequeue`, `close_issue`, `comments`, `comment`, `can_write`, `waiting_on`, `waiting_off`, `move`, `last_move`, `branch_tag`, `pr_title_problem`, `pr_link_problem`
  - set calls: `queue`, `statuses`, `queue_note`, `empty_note`, `enqueue_hint`, `create_issue`, `install`, `doctor`, `uninstall`, `board_report`, `pr_link_rule`
  - none of `kind`, `label`, `via`, `pr_threads`, `comment_limit`, `links_by_title`, `drag_answers`, `operator`, `mention`, `status_for`, `requeue_hint`

- [ ] **Step 1: Write the failing tests:**
  - `test_one_kind_still_makes_the_plain_tracker`: `type(make(cfg)) is GitHubTracker` and `make(cfg).of(12) is make(cfg)`. The same holds for Jira.
  - `test_router_sends_each_key_to_its_tracker`:
    - `t.key("12") == 12` and `t.key("#12") == 12`, with `t.of(12).kind == "github"`;
    - `t.key("proj-3") == "PROJ-3"`, with `t.of("PROJ-3").kind == "jira"`.
  - `test_router_refuses_a_key_neither_tracker_takes`: `pytest.raises(Refused, match=r"12.*PROJ-123")` for `t.key("x y")`.
  - `test_router_names_a_tracker_that_left_kind`: in a GitHub-only router, `of("PROJ-3")` refuses with "jira".
  - `test_queue_takes_turns_primary_first`: GitHub issues 1 and 2 and Jira cards A and B give `[1, "PROJ-…A", 2, "PROJ-…B"]` in that order.
  - `test_statuses_splits_keys_by_tracker`: returns the Jira key's status and leaves the int out.
  - `test_pr_link_rule_has_one_rule_per_tracker`: contains "number such as 12" and "PROJ-123". A one-tracker rule is unchanged (compare it with `GitHubTracker.pr_link_rule()`).
  - `test_new_issues_go_to_the_first_tracker`: `t.create_issue(...)` returns a GitHub URL when `kind = ["github","jira"]`.
  - `test_router_has_no_per_tracker_facts`: `not hasattr(t, "pr_threads")`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement `OneTracker`, `Trackers` and `make()`.** `make()` returns `Trackers` only when `tracker_kinds` has two. `for_repo` keeps its GitHub fallback. `queue` takes turns without sorting. `pr_link_rule` prefixes each member's rule with "If the issue is a number such as 12:" or "If the issue is a key such as PROJ-123:".
- [ ] **Step 4: Run the tests and the full suite.** All pass.
- [ ] **Step 5: Commit** (with Task 3, as one commit) `Route each job to its own tracker and take the queues in turn.`

### Task 3: Per-job facts through the job's own tracker

**Files:**
- Modify: `core/lib/modfactory/engine.py`:
  - `_operator` (52-54) becomes `_operator(job)`, with its callers at 737, 753 and 901;
  - the closes early return at 716 is for GitHub jobs only;
  - 57, 142-143, 189, 231-232, 240, 292-293, 341, 463-464, 645, 661-663, 699, 732-736, 780, 800, 805, 947-963.
- Modify: `service/human.py`:
  - 61-62, 79 (`who_for`), 81-85, 112, 135-142, 153, 183, 229;
  - `waiting["who"]` stores the resolved value.
- Modify: `service/cards.py`:
  - 33, 62, 64, 76, 94, 98, 114-117, 138, 161, 166;
  - `_drag_choices(job, name, step)`, with its callers at `human.py:103` and `cards.py:69`.
- Modify: `scripted.py:172,192`.
- Modify: `queue.py`: `_QueueHost.tracker` type, `_picks` per issue's tracker using `people_for`, `"tracker": kind` in the new job (line 69), and 152 (`who_for`).
- Modify: `store.py:120-135`: add `"trackers": [...]`; `"tracker"` is now the primary.
- Modify: `workspace/jobs.py:build_snapshot`: pass `trackers`, string values only.
- Modify: `service/tracker/jira_acli.py:293`: `who_for`.
- Test: `tests/test_both_trackers.py`

**Interfaces:**
- Consumes: Task 2's set API, Task 1's `who_for` / `people_for`.
- Produces: job field `tracker: "github" | "jira"`, and `index.json`'s `trackers: list[{kind,label,enqueue}]`.

- [ ] **Step 1: Write the failing tests.** All use `world.use_both()`.
  - `test_github_and_jira_jobs_run_side_by_side`:
    - create a job from a GitHub issue and one from a Jira card;
    - play both to `ready-to-merge`;
    - the GitHub job's ask is a PR comment or issue comment with the needs-human label;
    - the Jira job's card is in the `ready-to-merge` column, with a Jira comment.
  - `test_each_job_reads_replies_from_its_own_tracker`:
    - a `/mfactory approve` on the GitHub PR moves only the GitHub job;
    - `world.jira.drag(key, "Done"…)` answers only the Jira job.
  - `test_pr_rules_follow_the_job`: the Jira job's PR title must contain the key, and the GitHub job's must not. With `issue_link = "closes"`, the GitHub body may say `Closes #N` and the Jira body must start with `Refs KEY`.
  - `test_agent_issue_line_names_the_tracker_only_with_two`: the prompt contains `Issue: PROJ-1 (Jira)`. The one-tracker text is unchanged (`test_pipeline.py:64` keeps passing).
  - `test_jobs_and_index_record_their_tracker`: `job["tracker"] == "jira"`, and `index.json["trackers"]` lists both kinds.
  - `test_people_tables_filter_each_tracker`: `[run] authors = { github = ["alice"] }` keeps every Jira card in `queue` and drops a GitHub issue by bob.
  - **Review Focus 1:** `test_jira_jobs_in_flight_survive_adding_github`:
    - start a Jira job under `kind = "jira"`, then delete its `"tracker"` field;
    - reconfigure with `use_both()`;
    - `next` continues it, and `queue` shows it as `resume` / `wait`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement the call-site changes listed under Files.** Calls that take a key stay as they are and route by themselves.
- [ ] **Step 4: Run the tests and the full suite.** All pass.
- [ ] **Step 5: Commit** Tasks 2 and 3 together.

### Task 4: The queue keeps going when one tracker is down

**Files:**
- Modify: `service/tracker/routing.py` (`queue`, `statuses` tolerance), `queue.py:123-172`, `cli/__init__.py:239-243`, `workspace/queue.py:collect_queue`
- Test: `tests/test_both_trackers.py`, `tests/test_queue_checks.py` (new test only)

**Interfaces:**
- Produces:
  - `Trackers.problems` gets `"<Label>: <reason>"` lines, and `Trackers.unread` gets the kinds that failed.
  - `queue` stderr carries `problem: <Label>: <reason>` lines.
  - `collect_queue` adds `"<project>: <Label>: <reason>"` to `errors` even when the exit code is 0.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_jira_outage_does_not_stop_github_jobs`:
    - set `world.jira.fail = ("/rest/agile", 503)`;
    - `world.cli("queue")` exits 0 and still prints the GitHub `new` row;
    - stderr has `problem: Jira:`.
  - `test_a_job_whose_tracker_is_down_waits_with_the_reason`: a Jira job parked at a human step gets the row `wait  PROJ-1  <job>  <step>  could not read Jira: …`, and it is not `skip` (not withdrawn).
  - `test_every_tracker_down_still_fails`: both fail, and the exit code is non-zero as today.
  - `test_a_job_from_a_removed_tracker_is_skipped`: a Jira job, then `kind = "github"`, shows `skip … its tracker, Jira, is not in [tracker].kind`.
  - `test_workspace_queue_reports_problem_lines` (in `test_queue_checks.py`): a fake `.factory/bin/mfactory` that prints JSON rows and a `problem: Jira: down` line to stderr shows up in `errors` as `"<label>: Jira: down"`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement the guards (spec §1.4).** In `queue_and_issues`:
  - wrap `_find_reply`, `_audience` and `withdrawn` per job;
  - skip `withdrawn` for jobs whose tracker is in `unread`;
  - handle an unknown key before the loop.
- [ ] **Step 4: Run the tests and the full suite.** All pass.
- [ ] **Step 5: Commit** `Keep the queue going when one tracker is down.`

### Task 5: `file --tracker`, board commands, doctor for both, and the docs

**Files:**
- Modify: `cli/parser.py:80` (`file --tracker {github,jira}`), `cli/__init__.py:74-104, 251-259, 282-286`, `install.py:195,454`, `cli/maintain.py:275`
- Modify docs: `core/skill/SKILL.md`, `docs/configuration.md` (line 882; new `### GitHub issues and a Jira board together`; the `issue_link` bullet at 1046), `site/docs/configuration.html`, `site/docs/cli.html`, `site/docs/docs.js` (search heads), `README.md` (the Jira paragraph), `docs/design.md` (module list)
- Test: `tests/test_both_trackers.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_file_goes_to_the_first_tracker_or_the_one_named`: `world.cli("file", "T", input=…)` prints a GitHub URL; `world.cli("file", "--tracker", "jira", "T", …)` creates `PROJ-…`.
  - `test_doctor_checks_both_trackers`: the output has both GitHub label lines and `Jira board 7`.
  - `test_tracker_show_uses_the_jira_board`: with both kinds, it prints `Jira board 7`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement, and write the docs.** The docs cover:
  - the queue taking turns;
  - where people reply;
  - `file --tracker`;
  - `issue_link`;
  - people tables;
  - what happens when one tracker is down.

  Mirror the HTML pages by hand.
- [ ] **Step 4: Run the tests and the full suite.** All pass.
- [ ] **Step 5: Commit** `Add file --tracker, run doctor and install for both, and document both trackers.`

### Task 6: A route table for the dashboard server (no behaviour change)

**Files:**
- Modify: `core/lib/modfactory/workspace/service.py:157-230`
- Test: `tests/test_cli_install.py` (existing server tests, unchanged), plus `tests/test_dashboard_feeds.py` (create, routing tests only)

**Interfaces:**
- Produces:
  - `Route = tuple[re.Pattern[str], Callable[[Workspace, re.Match[str], dict[str, list[str]]], Reply]]`
  - `Reply = tuple[HTTPStatus, bytes | str, str]`
  - `ROUTES: list[Route]`, built in `_handler`
  - `do_GET` = Host check, then the first matching route, then `send`. A `FactoryError` gives 500 JSON, as today.

- [ ] **Step 1: Write the failing test** `test_unknown_paths_and_methods_are_refused`: `/projects/x/nope` gives 404, a foreign Host gives 403, and `/queue.json` gives 200 JSON. The existing server tests also run.
- [ ] **Step 2: Run it.** It may pass before the refactor; it is the safety net.
- [ ] **Step 3: Move each existing branch into a table entry.** Same paths, statuses and headers.
- [ ] **Step 4: Run the full suite.** All pass.
- [ ] **Step 5: Commit** `Route the dashboard server's requests through a table.`

### Task 7: Read Jira boards and open GitHub issues for the dashboard

**Files:**
- Modify: `service/tracker/jira_api.py:106` (`pages(..., with_total=False)` returns `(items, total)` when true), `service/tracker/jira.py` (module functions), `service/gh.py` (a `timeout` on `call` / `json`, plus `open_issues` and `repo_url`)
- Create: `core/lib/modfactory/workspace/feeds.py`
- Modify: `workspace/service.py`:
  - three routes;
  - `_web_url` allows loopback http;
  - `issue_response` routes with `of(key)`, with a plain-GitHub fallback for numeric keys.
- Modify: `docs/design.md` (new "Dashboard API" section with the three shapes; dashboard rules wording), `site/docs/guards.html#the-dashboard`
- Modify stubs:
  - `tests/stub_jira.py`:
    - `boards` dict: board 7 stays the default, plus board 8 (PLAT, scrum);
    - `/rest/agile/1.0/board` list route;
    - `priority` / `issuetype` fields;
    - JQL `OR`, `statusCategory != Done`, `updated >= -14d`, `sprint in openSprints()`.
  - `tests/stub_gh.py`: `issue list` without `--label`, honoring `--limit` / `--search`; `repo view --json url`.
- Test: `tests/test_dashboard_feeds.py`

**Interfaces:**
- Produces:
  - `jira.boards(client: Client, limit: int = 500) -> list[dict[str, Any]]`, each `{id: int, name, type, project}`
  - `jira.board_view(client: Client, board_id: int, days: int = 14, limit: int = 300) -> dict[str, Any]` = `{board:{id,name,type}, columns:[{name, cards:[{key,title,status,type,priority,assignee,labels,updated_at}]}], shown, total, hidden, truncated}`
  - `GitHub.open_issues(limit: int = 200) -> list[dict[str, Any]]`, `GitHub.repo_url() -> str | None`
  - `feeds.jira_boards(project: Project) -> tuple[HTTPStatus, dict]`
  - `feeds.jira_board(project: Project, board_id: int) -> tuple[HTTPStatus, dict]`
  - `feeds.github_issues(project: Project) -> tuple[HTTPStatus, dict]`
  - `FeedCache(ttl, error_ttl, size=64).get(key, load, fresh=False)`
  - Routes and response shapes exactly as in spec §2.2.

- [ ] **Step 1: Write the failing tests:**
  - `test_boards_lists_every_board_the_account_sees`: paged across `page_size = 1`, it returns boards 7 and 8 with their types.
  - `test_board_view_puts_cards_in_columns_by_status_id`: columns come in board order, and a card in an unmapped status counts in `hidden`.
  - `test_board_view_leaves_out_old_done_cards`: a Done card updated 30 days ago is absent, and one updated today is present.
  - `test_scrum_board_shows_the_open_sprint`: on board 8, only `sprint`-flagged cards are present.
  - `test_board_view_caps_cards_and_reports_the_total`: with `limit = 2` and 3 cards, `truncated is True` and `total == 3`.
  - `test_readers_take_their_clients_and_import_no_server`: call `jira.boards(Client(...))` directly. The source text of `feeds.py` has no `http.server`.
  - `test_github_issues_lists_open_issues_newest_first_with_me`: `me == "operator"`, sorted by `updatedAt` descending, no closed issue.
  - `test_jira_off_and_acli_only_say_how_to_turn_it_on`: a GitHub-only project gives `{"off": …}`. With acli only, `note` names `JIRA_EMAIL`.
  - **Review Focus 2:** `test_missing_jira_credentials_name_the_variables`: with no env vars, the board route gives 502 with an error naming `JIRA_EMAIL`. The GitHub route still gives 200.
  - **Review Focus 3:** `test_a_board_the_account_cannot_see_is_a_502`: board 99 gives 502 "Could not load board 99 from Jira".
  - **Review Focus 5:** `test_each_project_uses_its_own_jira_variables`: two projects with different `email_env` each succeed only with their own variable set.
  - `test_feed_cache_reuses_answers_and_fresh_skips_them`: two calls make one Jira request (`world.jira.called`); `fresh=1` after 5 s makes a second; a failure is cached for `error_ttl`.
  - `test_feed_routes_over_http`: a 404 for a bad board id (`abc`) or an unknown project, a 200 JSON for each route, redacted strings, loopback http URLs kept, and `javascript:` dropped.
  - `test_drawer_opens_github_issues_in_a_jira_only_project`: `issue_response(project, "48")` returns 200 via gh.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement the readers, cache, routes and docs.** `board_view` uses the board's `type` to choose the JQL (spec §2.1).
- [ ] **Step 4: Run the tests and the full suite.** All pass.
- [ ] **Step 5: Commit** `Read Jira boards and open GitHub issues for the dashboard, and write down the dashboard's API.`

### Task 8: Three blocks on the Board

**Files:**
- Modify: `web/dashboard.html` (`<div id="layer"></div>` after `<main>`; search placeholder and sr text), `web/dashboard.js`, `web/dashboard.css`
- Test: `tests/test_cli_install.py` (the existing `node --check` / no-innerHTML scans), plus a Playwright check (Task 9)

**Interfaces:**
- Consumes: Task 7's routes and shapes. The job field `tracker` and `trackers` come from Task 3.
- Produces (JS):
  - `prefs.get(key, fallback)` / `prefs.set(key, value)`
  - `state.feeds = {jira: {[project]: {boards, board: {[id]: entry}}}, github: {[project]: entry}}`, where entry = `{at, data, error, json}`
  - `state.jiraPicker = {project, query} | null`, `state.ghFilter`, `state.collapsed: Set<string>`
  - `blockSection(id, title, count, controls, body)`, `jiraBlock()`, `githubBlock()`, `factoryBlock()`
  - `openPicker(project)`, `closePicker()`, `renderPicker()` (into `#layer`), `refreshFeeds(force)`
  - `whoText(who)`, `personFor(login, name, key, tracker)`

- [ ] **Step 1: Implement:**
  - **Blocks:** fold buttons in an `<h2>` with sibling controls; bodies use `hidden` when folded.
  - **Jira:** areas per project, board headings that fold, and kanbans with `data-scroll-key`. Columns are capped at `70vh`. Cards carry job chips and drawer clicks.
  - **GitHub:** rows with the All, Mine and Not in the factory filter.
  - **Factory:** the block wraps today's content.
  - **Render loop:**
    - scroll is restored by `data-scroll-key` in `render()`;
    - block nodes are memoized by a key of `json + query + filter + collapsed`;
    - the 60 s polling runs only on the Board with the block open, with no overlapping rounds.
  - **Search:** it also covers cards and rows.
  - **Keys:** Escape closes the picker first. A `pointerdown` on the picker's scrim closes it.
  - **Single-file mode:** the new blocks are hidden.
  - **`whoText`:** used at `dashboard.js:1750,1779`.
  - **`personFor`:** replaces the per-project `person` decision at `:412` and in the drawers.
- [ ] **Step 2: Run the full suite.** It passes, including `node --check` on the inlined page.
- [ ] **Step 3: Commit** `Show Jira, GitHub and the factory as three blocks on the Board.`

### Task 9: Sample dashboard, docs and screenshot

**Files:**
- Modify: `tests/sample_dashboard.py`. Add:
  - a `FakeJira` with boards "Clinic app" (PROJ, kanban, id 7) and "Platform" (PLAT, scrum, id 8), with made-up cards in several columns;
  - open GitHub issues in the stub state;
  - `kind = ["github","jira"]` in the sample config;
  - `JIRA_EMAIL` and `JIRA_API_TOKEN` set for the server.
- Modify: `README.md` (dashboard section, `?v=3`), `site/docs/workspace.html` (five views, One board), `docs/dashboard-board.png`

- [ ] **Step 1: Run it.** `python3 tests/sample_dashboard.py --port 4747` runs in the background.
- [ ] **Step 2: Check it with Playwright (Chromium).**
  - Board at 1440×1000 and 390×844 (at deviceScaleFactor 2 for the README image).
  - Open the picker, type "plat", tick Platform while a refresh lands. The dialog stays open and the caret stays at the end of "plat".
  - Fold and unfold each block. Switch the GitHub filter to Mine.
  - Open a Jira card and a GitHub row drawer.
  - Scroll the factory kanban sideways, wait 6 s, and check `scrollLeft` is unchanged.
  - No console errors.
- [ ] **Step 3: Update the docs. Save the 1440 px Board capture as `docs/dashboard-board.png`.**
- [ ] **Step 4: Run the full suite.** All pass.
- [ ] **Step 5: Commit** `Add both trackers to the sample dashboard and refresh the Board screenshot.` Push to `claude/busy-brahmagupta-j2k95l` with retries.
