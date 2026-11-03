// Real-browser smoke test of a running NOVA deployment, driving an installed Chrome or Edge (no browser download).
// Prerequisites: the API has data from `python scripts/deployment_smoke.py --write ...` (a repository named smoke-*), and playwright-core:
//   cd frontend && npm install --no-save playwright-core@1.64.0
//   NOVA_API_TOKEN=... node e2e/browser_smoke.mjs http://127.0.0.1:8080 chrome     # or msedge; screenshots are written to the current directory
// Checks: 401 guidance without a token, token entry, real data pages, deep-link reload, backend-down error, phone-width overflow, a11y basics.
import { chromium } from "playwright-core";

const base = process.argv[2] ?? "http://localhost:4173";
const channel = process.argv[3] ?? "chrome";
const token = process.env.NOVA_API_TOKEN;
const results = [];
const check = (name, ok, detail = {}) => { results.push({ name, ok: !!ok, ...detail }); console.log(JSON.stringify({ check: name, ok: !!ok, ...detail })); };

const browser = await chromium.launch({ channel, headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push(String(e)));

// 1. No token: the dashboard must explain the 401 and link to Settings (production always requires a token).
await page.goto(base + "/", { waitUntil: "networkidle" });
const alert = page.getByRole("alert").first();
await alert.waitFor({ timeout: 15000 });
check("no token: 401 explained", (await alert.textContent()).includes("A valid bearer token is required"));
check("no token: Settings link shown", await page.getByRole("link", { name: "Enter it in Settings" }).first().isVisible());

// 2. Enter the token in Settings (kept in sessionStorage for this tab).
await page.getByRole("link", { name: "Enter it in Settings" }).first().click();
await page.waitForURL("**/settings");
await page.getByLabel("Bearer token").fill(token);
await page.getByRole("button", { name: "Save token" }).click();
check("token saved", await page.getByText("Saved for this tab.").isVisible());
await page.waitForTimeout(500);
check("workspace shows bearer auth mode", (await page.textContent("main")).includes("Bearer token required"));

// 3. Authenticated navigation with real data.
await page.goto(base + "/repositories", { waitUntil: "networkidle" });
const repoLink = page.getByRole("link", { name: /^smoke-/ }).first();
await repoLink.waitFor({ timeout: 15000 });
check("repositories list shows the registered repository", true, { name: await repoLink.textContent() });
await repoLink.click();
await page.waitForURL(/\/repositories\/[0-9a-f]+$/, { timeout: 15000 });
await page.waitForLoadState("networkidle");
check("repository detail renders", /\/repositories\/[0-9a-f]+$/.test(page.url()), { url: page.url().replace(base, "") });
await page.goto(page.url() + "/intelligence", { waitUntil: "networkidle" });
await page.getByRole("region", { name: "Index overview" }).waitFor({ timeout: 15000 });
check("intelligence overview renders from the API", true);
await page.goto(page.url().replace(/\/intelligence$/, "/health"), { waitUntil: "networkidle" });
await page.getByRole("region", { name: "Code health overview" }).waitFor({ timeout: 15000 });
check("code health overview renders from the API", true);
await page.screenshot({ path: "health-desktop.png", fullPage: true });

// 4. Deep link reload keeps working (SPA fallback) and the token survives a reload within the tab.
await page.reload({ waitUntil: "networkidle" });
check("deep link reload", await page.getByRole("region", { name: "Code health overview" }).isVisible());

// 5. Backend unreachable: requests fail closed with a clear message (simulated by aborting /api in the browser).
await page.route("**/api/**", (r) => r.abort());
await page.goto(base + "/repositories", { waitUntil: "load" });
const down = page.getByRole("alert").first();
await down.waitFor({ timeout: 20000 });
check("backend down: explicit error", (await down.textContent()).includes("Cannot reach the NOVA API"));
await page.unroute("**/api/**");

// 6. Phone width: no horizontal page scroll on key pages.
const mobile = await ctx.newPage();
await mobile.setViewportSize({ width: 390, height: 844 });
for (const path of ["/", "/repositories", "/settings"]) {
  await mobile.goto(base + path, { waitUntil: "networkidle" });
  const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(`mobile 390px: no horizontal scroll on ${path}`, overflow <= 0, { overflow_px: overflow });
}
await mobile.screenshot({ path: "settings-mobile.png", fullPage: true });

// 7. Basic accessibility facts: skip link, labelled navigation, a single main landmark, every input labelled.
const a11y = await page.evaluate(() => ({
  skip: !!document.querySelector('a[href="#main"]'),
  nav: !!document.querySelector('nav[aria-label]'),
  mains: document.querySelectorAll("main").length,
  unlabelled: [...document.querySelectorAll("input, select, textarea")].filter((el) => !(el.labels?.length || el.getAttribute("aria-label") || el.getAttribute("aria-labelledby"))).length,
}));
check("a11y basics on /repositories", a11y.skip && a11y.nav && a11y.mains === 1 && a11y.unlabelled === 0, a11y);

const unexpected = consoleErrors.filter((e) => !/Failed to load resource|net::ERR_FAILED|status of 401/.test(e));
check("no unexpected console errors", unexpected.length === 0, { unexpected: unexpected.slice(0, 5), expected_network_errors: consoleErrors.length - unexpected.length });
await browser.close();
const failed = results.filter((r) => !r.ok).map((r) => r.name);
console.log(JSON.stringify({ summary: { browser: channel, checks: results.length, passed: results.length - failed.length, failed } }));
process.exit(failed.length ? 1 : 0);
