// The project drop-down in a real browser, against `tests/sample_dashboard.py --projects 3`.
//
//     node tests/dashboard_filter_browser.js http://127.0.0.1:4747/
//
// It needs Node and the `playwright` module; tests/test_dashboard_browser.py runs it when both are there.
// Each check prints PASS or FAIL, and the exit code is 1 when one fails.
const { chromium } = require("playwright");

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
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(base + "#/board");
  await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
  check("a drop-down, not buttons", (await page.$$("#filters button")).length === 0);
  check("the select lists all, then each project",
    JSON.stringify(await texts(page, "#filters option")) === JSON.stringify(["all projects", "billing", "portal", "ops & tools"]));
  await chooseProject(page, "portal");
  const chosen = new URL(page.url());
  check("choosing a project writes ?project= and keeps the hash",
    chosen.searchParams.get("project") === "portal" && chosen.hash === "#/board", page.url());
  await chooseProject(page, "ops & tools");
  await page.reload();
  await page.waitForSelector("#filters select.filters-select", { timeout: 15000 });
  const kept = await page.$eval("#filters select.filters-select", (node) => node.value);
  check("reload keeps an id with a space and &", kept === "ops & tools", page.url());
  await chooseProject(page, "all");
  check("all removes ?project=", !new URL(page.url()).searchParams.has("project"), page.url());
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
