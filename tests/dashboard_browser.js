// The Board, Jobs and Config in a real browser, against `tests/sample_dashboard.py`.
//
//     node tests/dashboard_browser.js http://127.0.0.1:4747/
//
// It needs Node and the `playwright` module; tests/test_dashboard_browser.py runs it when both are there.
// Each check prints PASS or FAIL, and the exit code is 1 when one fails.
const { chromium, devices } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:4747/";
const project = "clinic-invoices";
const picksKey = "mfactory.jira.boards." + project;
const results = [];
function check(name, ok, detail) {
  results.push((ok ? "PASS " : "FAIL ") + name + (ok || detail === undefined ? "" : " — " + detail));
}
const texts = (page, selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));

async function board(context) {
  const page = await context.newPage();
  await page.goto(base + "#/board");
  await page.waitForSelector(".jboard .kcard", { timeout: 15000 });
  return page;
}

async function main() {
  const browser = await chromium.launch();
  const errors = [];
  const watch = (page) => {
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error" && !/status of 502/.test(m.text())) errors.push("console: " + m.text()); });
  };
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  context.on("page", watch);
  const page = await board(context);
  await page.waitForSelector("#filters", { timeout: 15000 });
  check("one project shows its name", (await page.textContent("#filters .filters-one")) === "project: clinic-invoices");

  // ---- the three blocks, and the project's own board until one is chosen
  check("three blocks in order", JSON.stringify(await texts(page, "section.block > .block-head .fold-title")) === JSON.stringify(["Jira", "GitHub issues", "Factory"]));
  check("the project's own board is shown by default", (await page.$$(".jboard")).length === 1);
  const chip = await page.$$eval(".jboard .kcard .chip", (els) => els.map((e) => e.textContent));
  check("a Jira card that is a job carries a short chip", chip.includes("job 016 · review"), JSON.stringify(chip));
  const jiraLink = await page.$eval(".jboard-head", (head) => { const a = head.querySelector("a"); return a ? a.getAttribute("href") : null; });
  check("\"open in Jira\" is a link, also for a Jira on this machine", /^http:\/\/127\.0\.0\.1:\d+\/secure\/RapidBoard/.test(jiraLink || ""), String(jiraLink));

  // ---- the board picker survives a refresh of the page under it
  await page.click(".picker-open");
  await page.waitForSelector(".picker");
  check("the picker opens with focus in its search", await page.evaluate(() => document.activeElement.classList.contains("picker-search")));
  await page.keyboard.type("plat");
  check("typing filters the boards", JSON.stringify(await texts(page, ".picker-item .picker-name")) === JSON.stringify(["Platform"]));
  await page.evaluate(() => render());
  const kept = await page.evaluate(() => {
    const input = document.querySelector(".picker-search");
    return { open: !!document.querySelector(".picker"), focused: document.activeElement === input, value: input.value, caret: input.selectionStart };
  });
  check("a refresh keeps the dialog open, focused, with its caret", kept.open && kept.focused && kept.value === "plat" && kept.caret === 4, JSON.stringify(kept));
  await page.click(".picker-item input");
  await page.evaluate(() => render());
  check("ticking a board keeps the dialog open", !!(await page.$(".picker")));
  await page.click(".pick-done");
  check("Done closes the dialog and gives focus back", !(await page.$(".picker")) && await page.evaluate(() => document.activeElement.classList.contains("picker-open")));
  await page.waitForFunction(() => document.querySelectorAll(".jboard").length === 2 && document.querySelectorAll(".jboard")[1].querySelector(".kcol"), null, { timeout: 15000 });
  const sprint = await page.$$eval(".jboard", (els) => [...els[1].querySelectorAll(".kcard .num")].map((n) => n.textContent));
  check("a scrum board shows its open sprint only", sprint.length === 3 && !sprint.includes("PLAT-10"), JSON.stringify(sprint));
  await page.click(".picker-open");
  await page.keyboard.press("Escape");
  check("Escape closes the dialog", !(await page.$(".picker")));

  // ---- a click on the backdrop closes the dialog and reaches nothing under it
  await page.click(".picker-open");
  const card = await page.$eval(".jboard .kcard", (el) => { const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  await page.mouse.click(card.x, card.y);
  await page.waitForTimeout(300);
  check("the backdrop closes the dialog and opens no drawer", !(await page.$(".picker")) && !(await page.$(".drawer")));

  // ---- folding, remembered after a reload
  for (const name of ["Jira", "GitHub issues", "Factory"]) await page.click(`section.block[aria-label="${name}"] > .block-head .fold`);
  await page.reload();
  await page.waitForSelector("section.block");
  const folded = await page.$$eval("section.block > .block-head .fold", (els) => els.map((e) => e.getAttribute("aria-expanded")));
  check("each block folds, and stays folded after a reload", folded.every((v) => v === "false"), JSON.stringify(folded));
  for (const name of ["Jira", "GitHub issues", "Factory"]) await page.click(`section.block[aria-label="${name}"] > .block-head .fold`);
  await page.waitForSelector(".irow", { timeout: 15000 });

  // ---- GitHub filters and the search box
  const numbers = () => texts(page, ".irow .num");
  const all = await numbers();
  await page.click('.tabs button[data-focus="ghf:mine"]');
  const mine = await numbers();
  await page.click('.tabs button[data-focus="ghf:outside"]');
  const outside = await numbers();
  await page.click('.tabs button[data-focus="ghf:all"]');
  check("All lists every open issue", all.length === 7, JSON.stringify(all));
  check("Assigned to me", JSON.stringify(mine) === JSON.stringify(["#60"]), JSON.stringify(mine));
  check("Not in the factory", JSON.stringify(outside.sort()) === JSON.stringify(["#60", "#61", "#62"]), JSON.stringify(outside));
  await page.fill("#search", "postgres");
  check("the search box filters the Jira cards", JSON.stringify(await texts(page, ".jboard .kcard .num")) === JSON.stringify(["PLAT-7"]));
  await page.fill("#search", "");

  // ---- drawers
  await page.click('.jboard .kcard:has(.num:text-is("PROJ-1"))');
  await page.waitForFunction(() => document.querySelector(".drawer h3") && !/Loading/.test(document.querySelector(".d-issue").textContent), null, { timeout: 15000 });
  check("a Jira card opens the issue drawer", /Let patients book appointments online/.test(await page.textContent(".drawer h3")));
  await page.keyboard.press("Escape");
  await page.click('.irow:has(.num:text-is("#60"))');
  await page.waitForSelector(".drawer h3");
  check("a GitHub row opens the issue drawer", /Crash when a patient has no email address/.test(await page.textContent(".drawer h3")));
  await page.keyboard.press("Escape");

  // ---- each kanban keeps its own scroll when the page draws again
  await page.evaluate(() => {
    document.querySelector('[data-scroll-key="factory"]').scrollLeft = 300;
    document.querySelector('[data-scroll-key^="jira:"]').scrollLeft = 60;
  });
  const scroll = () => page.evaluate(() => [document.querySelector('[data-scroll-key="factory"]').scrollLeft, document.querySelector('[data-scroll-key^="jira:"]').scrollLeft]);
  const before = await scroll();
  await page.evaluate(() => render());
  const after = await scroll();
  check("the factory and Jira kanbans keep their scroll", before[0] > 0 && JSON.stringify(before) === JSON.stringify(after), JSON.stringify([before, after]));

  // ---- a remembered board the account cannot see: its own error, and a way to drop it
  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify([7, 99, 98])), picksKey);
  await page.reload();
  await page.waitForFunction(() => document.querySelectorAll(".jboard .error").length === 2, null, { timeout: 15000 });
  const lost = await page.$$eval(".jboard", (els) => els.map((e) => [!!e.querySelector(".kcol"), (e.querySelector(".error") || {}).textContent || ""]));
  check("a board you cannot see shows its own error, the others still draw", lost[0][0] && /Could not load board 99 from Jira/.test(lost[1][1]), JSON.stringify(lost));
  await page.click(".picker-open");
  const gone = await page.$$eval(".picker-group", (groups) => groups.map((g) => [g.querySelector("h4").textContent, [...g.querySelectorAll("input")].map((i) => i.checked)]));
  const unavailable = gone.find((group) => group[0] === "No longer available");
  check("the picker lists boards Jira no longer shows, ticked", !!unavailable && unavailable[1].length === 2 && unavailable[1].every(Boolean), JSON.stringify(gone));
  const lostRow = await page.$('.picker-group:has(h4:text-is("No longer available")) .picker-item:has-text("#99") input');
  if (lostRow) await lostRow.click();
  await page.click(".pick-done");
  check("unticking a lost board drops it", JSON.stringify(await page.evaluate((key) => JSON.parse(localStorage.getItem(key)), picksKey)) === JSON.stringify([7, 98]));
  const remove = await page.$('.jboard button.drop-board');
  if (remove) await remove.click();
  check("a lost board's own Remove button drops it", JSON.stringify(await page.evaluate((key) => JSON.parse(localStorage.getItem(key)), picksKey)) === JSON.stringify([7]));
  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify([7, 8])), picksKey);

  // ---- a bad stored value still gives a page
  await page.evaluate(() => { localStorage.setItem("mfactory.board.collapsed", "5"); localStorage.setItem("mfactory.github.filter", "\"nonsense\""); });
  await page.reload();
  const blocks = await page.waitForSelector("section.block", { timeout: 5000 }).then(() => true, () => false);
  check("an odd stored value still draws the page", blocks && (await page.$$("section.block")).length === 3);
  await page.evaluate(() => { localStorage.removeItem("mfactory.board.collapsed"); localStorage.removeItem("mfactory.github.filter"); });

  // ---- a slow Jira never holds up the GitHub block (on a quiet page: nothing else draws it again)
  const quiet = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  quiet.on("page", watch);
  await quiet.route("**/jira/**", (route) => setTimeout(() => route.continue().catch(() => {}), 8000));
  await quiet.route("**/queue.json", () => {});
  await quiet.route("**/activity", () => {});
  const slow = await quiet.newPage();
  const start = Date.now();
  await slow.goto(base + "#/board");
  const fast = await slow.waitForSelector(".irow", { timeout: 3000 }).then(() => true, () => false);
  check("with Jira slow, the GitHub rows still draw at once", fast, "no rows after " + (Date.now() - start) + " ms");
  await quiet.close();

  // ---- Jobs: the table, a job's pipeline drawn with each step's state, and its tabs.
  // A context of its own, so the sort and tab it remembers stay out of the other checks.
  const jobsContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  jobsContext.on("page", watch);
  const jobsPage = await jobsContext.newPage();
  await jobsPage.goto(base + "#/jobs");
  await jobsPage.waitForSelector("table.jobs tbody tr", { timeout: 15000 });
  const rowCount = (await jobsPage.$$("table.jobs tbody tr")).length;
  check("the jobs table lists every job", rowCount === 20, String(rowCount));
  await jobsPage.click('[data-focus="jobfilter:running"]');
  const running = await texts(jobsPage, "table.jobs tbody tr td:first-child");
  check("the status chips filter the table", running.length === 3 && running.every((t) => t === "Running"), JSON.stringify(running));
  await jobsPage.click('[data-focus="jobfilter:all"]');
  await jobsPage.selectOption('[data-focus="jobpipe"]', "hotfix");
  const hot = await texts(jobsPage, "table.jobs tbody tr td:nth-child(2) a");
  check("the pipeline filter keeps only that pipeline's jobs", JSON.stringify(hot.sort()) === JSON.stringify(["019-fix-rounding-of-vat", "020-show-a-history-of"]), JSON.stringify(hot));
  await jobsPage.selectOption('[data-focus="jobpipe"]', "all");
  await jobsPage.click('[data-focus="sort:job"]');
  const firstAsc = (await texts(jobsPage, "table.jobs tbody tr td:nth-child(2) a"))[0];
  check("a header sorts the table", firstAsc === "001-add-dark-mode-to", firstAsc);
  await jobsPage.click("table.jobs tbody tr:has-text('019-fix-rounding-of-vat')");
  await jobsPage.waitForSelector("svg.dag-job", { timeout: 5000 });
  check("a row opens the job's page", /#\/jobs\/clinic-invoices\/019-fix-rounding-of-vat$/.test(jobsPage.url()), jobsPage.url());
  const nodeStates = await jobsPage.$$eval("svg.dag-job .dag-node", (els) => Object.fromEntries(els.map((e) => [e.getAttribute("data-node"), [...e.classList].find((c) => c.startsWith("st-")) || ""])));
  check("the job graph colours each step by its state", nodeStates.intake === "st-success" && nodeStates.build === "st-success"
    && nodeStates.gate === "st-running" && nodeStates.spec === "st-skipped" && nodeStates.review === "st-none", JSON.stringify(nodeStates));
  check("a step that is off and unreachable is left out of the graph", !("debug" in nodeStates), JSON.stringify(Object.keys(nodeStates)));
  await jobsPage.click('[data-node="build"]');
  const panel = await jobsPage.textContent(".nodepanel");
  check("clicking a step opens its visits and files", /Visits/.test(panel) && /build\.md/.test(panel), panel.slice(0, 200));
  await jobsPage.click('[data-focus="jobtab:gantt"]');
  check("the timeline draws a bar per visit", (await jobsPage.$$("svg.gantt .g-bar")).length === 3);
  await jobsPage.click('[data-focus="jobtab:details"]');
  const details = await jobsPage.textContent(".details");
  check("details show the branch and the pipeline", /factory\/019-fix-rounding-of-vat/.test(details) && /hotfix/.test(details));
  await jobsPage.click('[data-focus="jobtab:files"]');
  await jobsPage.click("table.files-table tbody tr:has-text('build')");
  await jobsPage.waitForSelector(".file-open .fileview", { timeout: 5000 });
  check("a file opens under the files table", /READY|Implemented/.test(await jobsPage.textContent(".file-open")));

  // ---- Config: every pipeline, its graph and its YAML
  await jobsPage.goto(base + "#/config");
  await jobsPage.waitForSelector(".pcard", { timeout: 15000 });
  check("config lists each pipeline", JSON.stringify(await texts(jobsPage, ".pcard .name")) === JSON.stringify(["default", "hotfix"]));
  await jobsPage.click('[data-focus="cfgpipe:hotfix"]');
  await jobsPage.click('[data-focus="cfgtab:yaml"]');
  const yaml = await jobsPage.textContent(".yaml");
  check("a pipeline's YAML file is shown", /extends: default/.test(yaml) && /description: Small fixes/.test(yaml), yaml.slice(0, 120));
  check("settings leave the pipeline's steps to the pipelines", !(await texts(jobsPage, "table td.mono")).some((t) => t.startsWith("steps.")));
  await jobsContext.close();

  // ---- a phone
  const phone = await browser.newContext({ ...devices["iPhone 13"] });
  phone.on("page", watch);
  const small = await phone.newPage();
  await small.goto(base + "#/board");
  await small.waitForSelector(".irow", { timeout: 15000 });
  const wide = await small.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  check("no sideways page scroll on a phone", wide[0] <= wide[1], JSON.stringify(wide));
  for (const hash of ["#/jobs", "#/jobs/clinic-invoices/018-export-invoices-as-csv", "#/config"]) {
    await small.goto(base + hash);
    await small.waitForTimeout(800);
    const span = await small.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    check("no sideways page scroll on a phone at " + hash, span[0] <= span[1], JSON.stringify(span));
  }

  check("no script errors", errors.length === 0, JSON.stringify(errors));
  await browser.close();
}

main().then(() => {
  console.log(results.join("\n"));
  process.exit(results.some((line) => line.startsWith("FAIL")) ? 1 : 0);
}, (error) => {
  console.log(results.join("\n"));
  console.log("ERROR " + error.message);
  process.exit(1);
});
