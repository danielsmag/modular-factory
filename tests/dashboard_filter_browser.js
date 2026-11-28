// Project filter row in a real browser, against `tests/sample_dashboard.py --projects N`.
//
//     node tests/dashboard_filter_browser.js http://127.0.0.1:4747/
//
// It needs Node and the `playwright` module; tests/test_dashboard_browser.py runs it when both are there.
// Each check prints PASS or FAIL, and the exit code is 1 when one fails.
const { chromium, devices } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:4747/";
const results = [];
function check(name, ok, detail) {
  results.push((ok ? "PASS " : "FAIL ") + name + (ok || detail === undefined ? "" : " — " + detail));
}
const texts = (page, selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));

async function chooseProject(page, id) {
  const select = page.locator("#filters select.filters-select");
  await select.waitFor({ timeout: 15000 });
  await select.selectOption(id);
  await page.waitForFunction((value) => {
    const node = document.querySelector("#filters select.filters-select");
    return node && node.value === value;
  }, id);
}

async function selected(page) {
  return page.$eval("#filters select.filters-select", (node) => node.value);
}

async function phoneNoSidewaysScroll(browser, watch, errors) {
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
  await phone.close();
}

async function main() {
  const browser = await chromium.launch();
  const errors = [];
  const watch = (page) => {
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !/status of 502/.test(m.text()) && !/Failed to load resource: net::ERR_/.test(m.text())) {
        errors.push("console: " + m.text());
      }
    });
  };
  const page = await browser.newPage();
  watch(page);
  await page.goto(base);
  await page.waitForSelector("#filters", { timeout: 15000 });
  const projectCount = await page.evaluate(async () => {
    const res = await fetch("/projects.json", { cache: "no-store" });
    const body = await res.json();
    return body.projects.length;
  });
  const childCount = await page.evaluate(() => document.getElementById("filters").childElementCount);
  check("the filter row is shown", childCount >= 1, String(childCount));

  check("projects show a select, not buttons", (await page.$$("#filters button")).length === 0 && !!(await page.$("#filters select.filters-select")));
  const expected = projectCount > 6
    ? ["all projects", "billing", "portal", "ops & tools", "inventory", "payroll", "crm", "support", "analytics"]
    : ["all projects", "billing", "portal", "ops & tools"];
  check("the select lists all, then each project", JSON.stringify(await texts(page, "#filters option")) === JSON.stringify(expected));

  if (projectCount > 6) {
    await chooseProject(page, "crm");
    check("choosing in the select writes ?project=", new URL(page.url()).searchParams.get("project") === "crm", page.url());
    await page.reload();
    await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
    check("the select shows the choice after a reload", await selected(page) === "crm");
  } else {
    // R1: choosing writes the URL, keeps the hash
    await page.goto(base + "#/board");
    await chooseProject(page, "portal");
    check("choosing a project writes ?project=", new URL(page.url()).searchParams.get("project") === "portal", page.url());
    check("the hash survives", new URL(page.url()).hash === "#/board", page.url());
    // R1: all removes it
    await chooseProject(page, "all");
    check("all removes ?project=", !new URL(page.url()).searchParams.has("project"), page.url());
    // the bug: URL said billing, user picked portal, reload must stay on portal
    await page.goto(base + "?project=billing#/board");
    await chooseProject(page, "portal");
    await page.reload();
    await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
    check("reload keeps the chosen project", await selected(page) === "portal", await selected(page));
    // R2: no history entries
    const before = await page.evaluate(() => history.length);
    await chooseProject(page, "billing");
    await chooseProject(page, "ops & tools");
    check("switching adds no Back entries", (await page.evaluate(() => history.length)) === before);
    // Review focus 1: escaping round-trips
    await page.reload();
    await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
    check("an id with a space and & round-trips", await selected(page) === "ops & tools", page.url());
    // Review focus 2: stale project
    await page.goto(base + "?project=gone#/board");
    await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
    check("an unknown project falls back to all", await selected(page) === "all", await selected(page));
    check("and its parameter is removed", !new URL(page.url()).searchParams.has("project"), page.url());
    // Review focus 3: deep link
    await page.goto(base + "#/jobs");
    await chooseProject(page, "billing");
    check("the jobs route survives a switch", new URL(page.url()).hash === "#/jobs", page.url());
    // R3: bare URL falls back to localStorage
    await page.goto(base + "#/board");
    await chooseProject(page, "portal");
    await page.goto(base + "#/board");
    await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
    check("a bare URL opens the last choice", await selected(page) === "portal", await selected(page));
  }

  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
  check("no script errors", errors.length === 0, JSON.stringify(errors));
  await phoneNoSidewaysScroll(browser, watch, errors);
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