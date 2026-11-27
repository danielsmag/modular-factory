# Plan: GitHub and Jira together, and a three-block Board

## Context

Today a project takes jobs from **one** tracker: `[tracker] kind = "github"` or `"jira"`
(`core/defaults.toml:46`, `service/tracker/__init__.py:make`). The docs say so outright:
"A project uses one tracker" (`docs/configuration.md:882`). The dashboard's Board view
(`web/dashboard.js:board()`, line 1399) shows only the factory: a Now panel, then a kanban of
jobs by step with a Queue column of issues that are not jobs yet.

The user wants to work with GitHub and Jira at the same time, and wants the Board to show three
blocks, in this order:

1. **Jira**: a Jira board drawn like the existing board (columns and cards), with a picker to
   choose which board to see, or several boards at once.
2. **GitHub issues**.
3. **Factory**: what the Board shows today, the factory's own jobs and queue.

Decided with the user:
- **One project takes jobs from both trackers at once.** A GitHub issue labeled `factory` and a
  Jira card in the queue column both become jobs. Each job asks and listens on its own tracker.
- **The GitHub block lists all open issues** of each project's repo, with quick filters.
- **The dashboard stays on the standard-library server.** No FastAPI now. It gets a small route
  table instead.
- **Keep the door open for a team version later.** Four things are done now; see "Ready for a
  team version later" below.

My calls (each can be overruled):
- `kind` also takes a list: `kind = ["github", "jira"]`. The first one is where `mfactory file`
  opens new issues, unless `file --tracker` says otherwise.
- **People are named separately for each tracker.** A GitHub login and a Jira account id can look
  the same, so a single shared list can't be split between trackers. With two trackers, these
  take a table per tracker: `[run].authors`, `[run].assignees`, and a human step's `who` list.
  Example: `authors = { github = ["alice"], jira = ["712020:…"] }`. With one tracker the plain
  list works as today.
- The Jira block uses the project's Jira site and credentials, so it is on when `"jira"` is in
  `kind`. The picker lists every board the account can see. The project's own board is picked by
  default. The GitHub block shows for every project, because `gh` is always set up.
- **Several boards are stacked**, each with its own columns. Merging columns from different
  workflows would hide where a card lives.
- **Choices are remembered in the browser:** the picked boards, the GitHub filter, and which
  blocks are folded. That keeps the server read-only. A personal view setting does not belong
  in the committed `factory.toml`.
- **Cards shown, like Jira's own boards:**
  - kanban: Done cards older than 14 days are left out;
  - scrum: the open sprint only;
  - at most 300 cards per board, with a link to open it in Jira.
- The search box filters all three blocks.

## Approaches considered

- **Two trackers.** Chosen: a router, `Trackers`, used only when `kind` names two. It sends each
  issue to the tracker its key belongs to and takes the queues in turn. A single tracker stays
  exactly as it is today. Rejected: two factories in one repo, which means two configs,
  concurrency caps that don't add up, and job ids that collide. Also rejected: mirroring one
  tracker into the other, because syncing is fragile and people would reply in the wrong place.
- **Board data.** Chosen: new GET routes on the dashboard server, with a short cache. The page's
  policy (`connect-src 'self'`, `workspace/service.py:174`) lets it talk only to its own server,
  and the tokens stay there. Rejected: JSON files written by `mfactory queue`, which would be
  stale and could not cover browsing any board. Calling Jira from the browser is blocked by
  that policy and would expose tokens.
- **Server framework** (the user asked about FastAPI). Chosen: keep the standard library's
  `ThreadingHTTPServer`, with a small route table in place of the if/else chain. FastAPI is
  rejected:
  - It needs uvicorn, starlette and pydantic, whose core is compiled. `install.sh` would need a
    pip or virtualenv step, which breaks the stdlib-only promise (README, Development) and plain
    `curl | bash` installs.
  - It would not be faster. The time goes into waiting on Jira and GitHub, which the cache
    covers, and the server already answers requests in parallel.
  - It adds third-party code next to the user's Jira token and `gh` login.
  - The localhost Host check and the page policy would have to be rebuilt as middleware.

## Ready for a team version later (asked by the user)

Nothing team-level is built now. A team version would be a separate hosted service:
- logins through GitHub, Atlassian or company single sign-on;
- each viewer's own GitHub and Jira access;
- job snapshots pushed by each factory into a database;
- webhooks instead of polling.

FastAPI fits there. What is done now to keep that door open:

1. **Reusable data code.** The code that reads Jira boards and GitHub issues
   (`workspace/feeds.py` and the new functions in `service/tracker/jira.py` and `service/gh.py`)
   takes its Jira and GitHub connections as inputs and returns plain data. It has no
   web-server code inside and imports nothing from `http.server`. The route table (§2.2) only
   adapts HTTP to it. A team server could import it as is and pass in each viewer's own access.
2. **A written API.** The dashboard's JSON routes and their response shapes go into the design
   notes (`docs/design.md`, a new "Dashboard API" section). A hosted server can then serve the
   same routes, and the same page works unchanged. The page already talks only to its own
   server (`connect-src 'self'`).
3. **One place for view settings.** The picked boards, filters and folded blocks are saved
   through one small helper in the page (`prefs.get` / `prefs.set` in `dashboard.js`, wrapped
   in try/catch). They can later move from the browser into a per-user profile.
4. **No token now.** It wouldn't help a team, which needs real logins, not one shared password.
   For one person, the localhost-only rule and the Host-header check already keep other websites
   out.

---

## Part 1: one project, two trackers

### 1.1 Config (`core/lib/modfactory/config.py`, `core/defaults.toml`, `core/templates/factory.toml`)
- **`[tracker] kind`:** `"github"`, `"jira"`, or a non-empty list of distinct kinds.
  - Add `tracker_kinds(raw) -> list[str]`. It must not crash on junk such as `kind = 5`, because
    `check()` reads the kind before validating it (line 537).
  - Error: `[tracker].kind must be "github", "jira", or a list of them, such as ["github", "jira"]`.
- **In `check()`** (lines 537-567), `uses_jira` becomes `"jira" in kinds`, and `uses_github` is
  added.
  - **People with two trackers:** `[run].authors`, `[run].assignees` and a human step's `who`
    list must be a table keyed by tracker. Each entry is checked with its tracker's own rule:
    GitHub logins, or Jira account ids (`a_jira_account_list`). A plain list is refused, and the
    message shows the table form.
    - In `authors` / `assignees`, a tracker that is left out is not filtered.
    - A `who` table must name every tracker. The words `author`, `maintainer` and `operator`
      stay plain strings.
  - **Branch pattern:** checked with and without the `PROJ-123-` tag sample (`_branch_pattern_gaps`).
  - **`issue_link = "closes"`:** refused only when Jira is the only tracker. With both, it applies
    to GitHub issues. A Jira job's pull request still starts with `Refs KEY`.
  - Site and board are still required whenever `"jira"` is in the list.
- **Helpers** `people_for(value, kind)` and `who_for(step, kind)` resolve a list or a table for
  one tracker. When a job is asked, the resolved list is stored in `job["waiting"]["who"]`, so
  the code that reads it later is unchanged.
- **Defaults and template:** add a commented `kind = ["github", "jira"]` example. The template's
  link to the `#tracker-a-jira-board` anchor keeps working.

### 1.2 Router (`service/tracker/routing.py` new, `service/tracker/base.py`, `service/tracker/__init__.py`)
- **`make(cfg)` still returns the plain tracker when `kind` names one.** Tests read `.kind`,
  `.via`, `.operator()` from it (`test_tracker.py:29`, `test_jira_acli.py:42-73`,
  `test_jira_tracker.py:178`). The generated agent files must stay word for word the same.
- **A mixin in `base.py`, `OneTracker`,** gives `GitHubTracker` and `JiraTracker` the set's API:
  `of(key) -> self`, `members -> [self]`, `primary -> self`, `member(kind)`, `board_member()`,
  and empty `problems` / `unread`. Call sites are written once and work for one tracker or two.
- **`class Trackers`** (two members) has the same set API, plus routing:
  - **`of(key)`** is the member whose `key()` accepts it: GitHub keys are ints, Jira keys are
    `PROJ-12` strings. A key no member accepts, such as a job from a tracker since removed from
    `kind`, raises a refusal that names the missing tracker.
  - **Calls that take a key** go to `of(key)`: `ref`, `issue`, `issue_details`, `withdrawn`,
    `queue_gate` (by `issue["number"]`), `dequeue`, `close_issue`, `comments`, `comment`,
    `can_write`, `waiting_on`, `waiting_off`, `move`, `last_move`, `branch_tag`,
    `pr_title_problem`, `pr_link_problem`.
  - **Calls over the whole set:**
    - `key(ref)` tries each member. The error names both forms: `12` or `PROJ-123`.
    - `queue()` merges the members' queues (§1.4).
    - `statuses(keys)` splits the keys by member and merges the answers. A member that fails
      leaves its keys out, which the contract allows.
    - `queue_note`, `empty_note`, `enqueue_hint` join the members' phrases.
    - `create_issue` goes to the primary.
    - `install` / `doctor` / `uninstall` run each member.
    - `board_report` asks the board member.
    - `pr_link_rule` gives one rule per tracker, each saying how to recognize its key: "If the
      issue is a number such as 12, …; if it is a key such as PROJ-123, …".
  - **Not defined on `Trackers`:** the per-tracker facts (`kind`, `label`, `via`, `pr_threads`,
    `comment_limit`, `links_by_title`, `drag_answers`) and the calls with no key whose answer
    depends on the tracker (`operator`, `mention`, `status_for`, `requeue_hint`). Call sites use
    `self.tracker.of(key).x`, so a missed one fails loudly in a two-tracker test.
- **The agent's `Issue:` line** (`engine.py:341`) adds the tracker name only when there are two,
  for example `Issue: PROJ-12 (Jira)`. `test_pipeline.py:64` checks the one-tracker text.

### 1.3 Call sites: per-job facts through `of(job["issue"])`
- **`engine.py`:**
  - `_operator()` (52-54) becomes `_operator(job)`; update its callers at 737, 753 and 901.
  - The early return when `issue_link = "closes"` (716) applies only when the job's tracker is
    GitHub, so Jira PR bodies are still checked.
  - `links_by_title` (699), plus every other attribute read or no-key call: 57, 142-143, 189,
    231-232, 240, 292-293, 463-464, 645, 661-663, 732-736, 780, 800, 805 and 947-963.
  - Calls that take a key route by themselves and stay as they are.
- **`service/human.py`:**
  - `_audience` (61-62), `_ask` (81-85 and 112: `pr_threads`, `operator`, `mention`,
    `comment_limit`), `_may_reply` (137-142), `_find_reply` (153), 183, and `answer` (229).
  - `who` is resolved per tracker with `who_for`.
- **`service/cards.py`:**
  - `_drag_choices(name, step)` (92) gets a `job` parameter; update its callers at
    `human.py:103` and `cards.py:69`.
  - `status_for`, `label` and `drag_answers` at 33, 64, 76, 94, 98, 114-117 and 166 go through
    the job's tracker.
  - `board.drag_outcomes` / `board.drags` take the job's own `status_for`.
  - In `_settle_cards` (136-138) and `sync_cards` (161), keep only jobs whose tracker has a
    status for the step.
- **Problem issues:** `engine.py:293` (`_report_problem`) and `scripted.py:172`
  (`on_failure = "issue"`) file on the job's own tracker.
- **`queue.py`:**
  - The `_QueueHost` / `Queue` type hints accept either a tracker or `Trackers`.
  - `_picks` resolves `@me` and the people lists against each issue's own tracker, memoized per
    member.
- **`cli/__init__.py`:**
  - `tracker show` / `sync` use `board_member()`, and keep today's "no board" message when
    there is none.
  - `file` gets `--tracker github|jira`, parsed in `cli/parser.py:80`.

### 1.4 Queue: taking turns, and one tracker being down
- **Taking turns:** `Trackers.queue()` starts with the primary's first issue, then the other
  tracker's first, and so on. With `["github", "jira"]` that is g1, j1, g2, j2. Each tracker's
  own order survives (GitHub oldest first, Jira by board rank), and neither waits for the other.
  - The merged list is never sorted by key: ints and strings don't compare.
  - The concurrency cap applies to the merged list, as today.
- **When a member's queue fails while another works:**
  - The failure goes into `trackers.problems` and the member into `trackers.unread`.
  - New jobs still start from the trackers that answered.
  - If every member fails, the first error is raised, as today.
- **In `queue_and_issues`, each job's own tracker calls are guarded:**
  - the calls are `_find_reply` → `comments`, `_audience` → `operator`, and `withdrawn`;
  - when the job's tracker is unread or a call refuses, the job gets a row
    `wait <key> <job> <step> could not read Jira: <reason>` and a `problem:` line;
  - the `withdrawn` check is skipped, so an outage never marks a running job as withdrawn;
  - other jobs carry on.
- **A job whose key no member accepts** gets a `skip` row: "its tracker, Jira, is not in
  [tracker].kind". `_settle_cards` skips it too.
- **How problems are shown:**
  - The CLI prints each one to stderr as `problem: <tracker>: <reason>` (`cli/__init__.py:239`).
  - `workspace/queue.py:collect_queue` turns those lines into the project's `errors` even when
    the exit code is 0, so the Board shows them.
  - Older cores never print `problem:`.

### 1.5 Job and index fields (`queue.py:69`, `store.py:120-135`)
- A new job stores `"tracker": "<kind>"`. Routing uses the key. The field is there for the
  page. Old jobs without it fall back to the key's shape.
- `index.json` keeps `"tracker"`, now describing the primary, for older dashboards. It adds
  `"trackers": [{kind, label, enqueue}, ...]`. `workspace/jobs.py:build_snapshot` passes the
  list through, using only its string values.

### 1.6 Skill and docs
- **`core/skill/SKILL.md`:** with both trackers, `<n>` may be `12` or `PROJ-123`, and `file` takes
  `--tracker`.
- **`docs/configuration.md`:**
  - Replace "A project uses one tracker" (882).
  - Add `### GitHub issues and a Jira board together`. It covers the queue taking turns, where
    people reply, `file --tracker`, `issue_link`, people tables, and one tracker being down.
  - Update the "What else differs" bullet on `issue_link`.
- **`site/docs/configuration.html` and `cli.html`:** the same changes, since they are hand-kept
  mirrors. Add the section to the search index in `site/docs/docs.js`.
- **`README.md`:** "Jobs can come from a Jira kanban board instead of, or as well as, GitHub issues."
- **`docs/design.md`:** add the new modules to the module list.

---

## Part 2: three blocks on the Board

### 2.1 Reading Jira and GitHub (tracker layer)
- **`service/tracker/jira.py`** gets module-level functions that take a `Client`, so they work
  for any board:
  - **`boards(client, limit=500)`** lists boards with `GET /rest/agile/1.0/board`, paged on
    `values`. Each board comes back as `{id, name, type, project}`.
  - **`board_view(client, board_id, days=14, limit=300)`** returns one board's columns and cards:
    - its name and type come from `GET /rest/agile/1.0/board/{id}`;
    - columns come from `/configuration`, matched by status **id**;
    - cards come from `/issue` with `fields=summary,status,assignee,priority,issuetype,labels,updated`;
    - kanban and simple boards use `jql=statusCategory != Done OR updated >= -14d`;
    - scrum boards use `jql=sprint in openSprints()`;
    - cards whose status is on no column are dropped, as Jira does, and counted in `hidden`;
    - a site without priorities sends `priority: null`, and the card leaves it out.
  - **`Client.pages`** (`jira_api.py:106`) can also return the `total` it throws away today. "Showing
    300 of 812" needs it. Paging, the retry on 429, the 30 s timeout and the refusal to follow
    redirects are reused as they are.
- **`service/gh.py`:**
  - `GitHub.call` / `json` get a `timeout` argument (none today, line 22).
  - Add `open_issues(limit=200)`, which runs `gh issue list --state open --limit N --search "sort:updated-desc"
    --json number,title,url,author,assignees,labels,createdAt,updatedAt`.
  - Add `repo_url()` (`gh repo view --json url`).
  - "Me" is `GitHubTracker.operator()` (`gh api user`).
- **With acli only (no REST credentials)**, board browsing needs REST. The block shows: "Set
  JIRA_EMAIL and JIRA_API_TOKEN where the dashboard runs to see Jira boards."

### 2.2 Server (`workspace/feeds.py` new, `workspace/service.py` routes)
New GET routes. They keep the localhost Host check and the same page policy, and add no write route.

- **Route table first.** `do_GET` (`service.py:180-228`) becomes a short list of (compiled
  pattern, handler) pairs.
  - Each handler returns `(status, body, content type)`. One place sends the headers and turns
    errors into JSON.
  - The existing routes move into the table unchanged, and the existing server tests prove it.
  - The three new routes are then three entries.

| Route | 200 body |
|---|---|
| `/projects/<p>/jira/boards` | `{site, configured, boards:[{id,name,type,project,url}], note}`, or `{off: "<how to turn Jira on>"}` |
| `/projects/<p>/jira/boards/<id>` | `{board:{id,name,type,url}, columns:[{name, cards:[{key,title,url,status,type,priority,assignee,labels,updated_at}]}], shown, total, hidden, truncated}` |
| `/projects/<p>/github/issues` | `{repo_url, me, issues:[{number,title,url,author,assignees,labels,created_at,updated_at}], truncated}` |

- **Errors:**
  - A board id that is not 1-10 digits, or an unknown project, is 404.
  - A tracker failure is 502 `{"error": "Could not load board 12 from Jira: ..."}`, modelled on
    `issue_response` (`service.py:109`).
  - The feed code also catches timeouts, bad JSON and OS errors. `do_GET` (line 225) catches
    only `FactoryError`.
- **What leaves the server:**
  - Every string goes through `_text` / `activity.redact`.
  - `_web_url` and the page's `link()` keep `https://`, and also allow `http://` on
    `127.0.0.1` / `localhost`, as the config's site rule already does (`config.py:391`).
    Without that, the fake Jira in tests and the sample would show no links.
  - The board URL is `{site}/secure/RapidBoard.jspa?rapidView={id}`.
- **The issue drawer** (`issue_response`) routes through `of(key)` and uses that member's
  `label` in messages. If a project has no GitHub tracker, a numeric key is read through a plain
  GitHub reader, because the GitHub block lists those issues too.
- **`FeedCache`** is a keyed TTL cache with a lock per key, so a slow Jira board never blocks
  the GitHub list.
  - Cards and issues: 60 s. Board list, "me" and repo URL: 10 min. Failures: 15 s, so a service
    that is down is not hammered.
  - `?fresh=1` skips an entry older than 5 s. `do_GET` ignores the query today, so it starts
    reading it.
  - At most 64 entries; when full, the oldest goes first.

### 2.3 Front end (`web/dashboard.js`, `web/dashboard.css`, `web/dashboard.html`)
**Layout.** `board()` keeps the page heading, then draws three `<section class="block">`s.
- **Header:** an `<h2>` holding a fold `<button aria-expanded aria-controls>` with the title and
  count. The controls sit beside it, not inside the button: the picker button, the filter,
  "updated …" from the existing `ago`, and a refresh button.
- **Body:** stays in the page with `hidden` when folded.

**1. Jira.** One area per visible project with Jira on, headed by the project name in "all" mode.
- **Picker:** the button reads "Boards: Factory, Platform". It opens a small modal dialog
  ("Choose Jira boards") with a search field and real checkboxes grouped by project key.
  - The dialog is mounted **outside `#view`**, in a new `<div id="layer">` in `dashboard.html`.
    `render()` replaces all of `#view` on every refresh and every search keystroke
    (`dashboard.js:2113`, 2307). Outside it, the dialog keeps its caret, list scroll and open
    state. Only its list updates in place.
  - It closes on Esc, Done, or a `pointerdown` on its scrim. Focus goes back to the picker
    button through its `data-focus` token.
  - The picked boards are remembered under `mfactory.jira.boards.<project>`.
- **Each picked board:** a heading (name · key · "open in Jira" · "showing 300 of 812") that
  folds. Under it, a `.kanban` of `.kcol` columns with `.kcard` cards. A card shows the key,
  title, type · priority · assignee · updated, and label chips.
- **Clicking a card:**
  - A card that is a job in that project gets a "job 012 · build" chip, which opens the job
    drawer.
  - Any other card opens the issue drawer, through the existing `openIssue`.

**2. GitHub issues.**
- A segmented filter reusing `.tabs`: All · Assigned to me · Not in the factory. It is
  remembered under `mfactory.github.filter`.
- Rows show `#48`, the title, labels, a state chip ("job 018 · Needs you" or "queued"),
  assignees, author, and updated time. They are grouped by project in "all" mode, newest-updated
  first.
- A row opens the issue drawer.

**3. Factory.** Today's `problemLines`, `nowPanel` and kanban, unchanged, with "18 of 18" in its
header.

**Keeping `render()` cheap and stable** (it rebuilds `#view` on every change):
- **Scroll:** every scroll container carries a `data-scroll-key` (such as
  `jira:<project>:<board>`, `jira-col:<…>:<column>`, `factory`). Sideways and vertical scroll are
  restored by key. Today only the first `.kanban` keeps its sideways scroll (2086, 2115), which
  would reset the factory board under the Jira boards.
- **Column height:** Jira columns are capped at `70vh` and scroll on their own (`.kcol` has no
  cap today, `dashboard.css:130`).
- **Reuse:** a block's built nodes are reused while its data, the search, its filter and its
  folded state are unchanged. The 5 s jobs refresh then doesn't rebuild hundreds of cards.

**Other behaviour:**
- **Polling:** Jira and GitHub refresh every 60 s, and only while the Board is shown and the block
  is open. They fetch at once when you enter the Board with stale data. Fetches run in parallel,
  and a new round never starts while one is still running. A feed re-renders only when its JSON
  changed. Jobs (5 s) and the queue (15 s) are unchanged.
- **Linking to jobs:**
  - A card or row is matched to a job in the same project by `String(job.issue)`.
  - "Queued" means the key is in `state.queueRows` with `new` / `hold`.
- **`person()`** decides `@login` or a name for each issue, from the job's `tracker` or the key's
  shape, instead of for each project. `enqueueNodes()` joins the hints from `trackers`.
- **Search** also matches Jira cards (key, title, status, assignee, labels) and GitHub issues
  (number, title, labels, author, assignees). The placeholder becomes "Search jobs and issues...".
- **Escape** closes the picker before a drawer (2311-2315).
- **Single-file mode** (`refreshSingle`, 2247) has no server routes, so the new blocks are hidden
  there.
- **Storage:** every read and write goes through the one `prefs` helper, in try/catch as
  `readChoice` does. Folded blocks are remembered under `mfactory.board.collapsed`.
- **Rules kept:** nodes are built only with `el()`, never `innerHTML`.
- **CSS:**
  - Reuse the tokens and `.kanban`, `.kcol`, `.kcard`, `.chip`, `.tabs`, `.error`, `.stale`,
    `.kempty`, `.overlay`, `.scrim`.
  - Add `.block`, `.block-head`, `.picker` and `.irow`.
  - At 480 px and below, rows wrap.

### 2.4 Sample dashboard and docs
- **`tests/sample_dashboard.py`** gets:
  - a `FakeJira` with two made-up boards, "Clinic app" (PROJ, kanban) and "Platform" (PLAT,
    scrum), with columns and cards;
  - open issues in the stub `gh` state;
  - `kind = ["github", "jira"]` and the credential variables set.
  
  `make sample-dashboard` then shows all three blocks.
- **The blocks are described in:**
  - the `README.md` dashboard section;
  - `site/docs/workspace.html` ("five views" and "One board");
  - `site/docs/guards.html#the-dashboard`: the new routes only read, answer only on localhost,
    and keep tokens on the server;
  - `docs/design.md`'s dashboard rules: the page still loads nothing from elsewhere and changes
    nothing.
- **Screenshot:** refresh `docs/dashboard-board.png` from the sample dashboard and bump `?v=3` in
  the README. The old `site/index.html` demo stays as it is, out of scope.

---

## Tests (pytest, stubs only, offline)
- **Stubs and fixtures:**
  - **`tests/stub_jira.py`:**
    - extra boards, each with its own columns, project and type (one scrum), while board 7 stays
      the default that 26 existing test lines rely on;
    - the board list route (`/rest/agile/1.0/board`, paged `values`) and `priority` /
      `issuetype` fields;
    - JQL `OR` (today it splits only on ` AND `, line 197), `statusCategory != Done`,
      `updated >= -Nd` and `sprint in openSprints()`.
  - **`tests/stub_gh.py`:** `issue list` with no `--label` (today a KeyError, line 125), honoring
    `--limit` and `--search sort:updated-desc`. A `repo view --json url` command (there is no
    `repo` group today, line 193). `api user` already works.
  - **`tests/conftest.py`:** `world.use_both()`, with a flag so `issue()` / `start()` make GitHub
    issues and `ticket()` makes Jira cards. Today `issue()` makes a Jira ticket whenever Jira is
    on (line 176).
- **`tests/test_both_trackers.py`:**
  - Config: the list is accepted; empty, duplicate, unknown and junk kinds are refused. With two
    trackers, people tables are required and each entry is checked by its own tracker.
  - Keys go to the right member. A bad key's error names both forms. A removed tracker's job
    gets a `skip` row.
  - The queue takes turns, primary first, and respects concurrency.
  - A GitHub job and a Jira job run side by side:
    - each asks on its own tracker (GitHub label and PR or issue comment, Jira column and comment);
    - each reads replies from the right place;
    - a Jira drag answers only the Jira job;
    - each has its own PR title and body rules, including `issue_link = "closes"` for GitHub only.
  - One tracker down:
    - a Jira job parked at a human step while the Jira stub fails gets a `wait … could not read
      Jira` row and a `problem:` line;
    - the GitHub job still resumes;
    - no job is marked withdrawn.
  - `file --tracker jira`, and the default to the first kind. `doctor` reports both.
    `index.json` has `trackers` and each job's `tracker`. The agent's `Issue:` line names the
    tracker only with two.
- **`tests/test_dashboard_feeds.py`:**
  - The readers are called directly, with a stub-backed `Client` and `GitHub` passed in. A
    source scan checks that `feeds.py` and the tracker readers import nothing from `http.server`.
    This keeps the team-version seam honest.
  - Board list, paged. Board view: columns in order, cards placed by status id, old Done cards
    dropped, the scrum open sprint, and the cap with `total` and `truncated`.
  - The acli note and the `off` state. The GitHub list with `me` and sorting. The 502 text,
    including a timeout.
  - Redaction, the URL rule, and the plain-GitHub drawer fallback for a project with Jira only.
  - Over HTTP: the routes, 404 for a bad id or project, the cache (a second call within the TTL
    makes no new call), and `fresh=1`.
- **Existing suite:** all 439 tests must still pass with no edits to their assertions. That shows
  one-tracker projects and their generated files are unchanged. The `node --check` and
  no-`innerHTML` tests in `test_cli_install.py` cover the new script.

## Delivery
- Work on `claude/busy-brahmagupta-j2k95l`. Each commit carries its code, tests and docs, with an
  imperative subject ending in a period:
  1. Let `[tracker] kind` name both trackers, with people named per tracker.
  2. Route each job to its own tracker and take the queues in turn.
  3. Keep the queue going when one tracker is down.
  4. Add `file --tracker`, run doctor and install for both, and document both trackers.
  5. Route the dashboard server's requests through a table. This changes no behaviour.
  6. Read Jira boards and open GitHub issues for the dashboard, and write down the dashboard's API.
  7. Show Jira, GitHub and the factory as three blocks on the Board.
  8. Add both trackers to the sample dashboard and refresh the Board screenshot.
- Push to the branch. Open a pull request only when asked. Suggested title: `feat: take jobs
  from GitHub and Jira together, and show both on the Board.`
- CI's branch-name check (`.github/workflows/ci.yml:16-27`) rejects pull requests from
  `claude/...`. Opening one needs the user's OK to push the same commits as `feat/github-and-jira`.

## Verification
1. `ruff check .`, `bash -n install.sh`, `python3 -m pytest -o addopts=-q`. The container has a
   standalone `ruff` and no `pytest-xdist`, so `make check` is run as its three parts.
2. Run `make sample-dashboard` in the background and drive it with Playwright (Chromium is
   installed):
   - Board at 1440 px and 390 px.
   - Open the picker, search, and add the second board while a refresh happens. The dialog stays
     open and keeps its caret.
   - Fold and unfold each block. Switch the GitHub filter.
   - Open a Jira card drawer and a GitHub issue drawer.
   - Scroll the factory kanban sideways, wait for a refresh, and check it kept its place.
   - The console shows no errors.
   
   Send the screenshots to the user.
3. The command-line flow against the stubs is covered by the tests above: `queue` shows a GitHub
   `new` row and a Jira `new` row, both jobs run to a human step, and `tracker show` and
   `doctor` run.
