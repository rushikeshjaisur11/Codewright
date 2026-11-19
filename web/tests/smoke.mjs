// UI smoke test: starts `codewright serve` on a copy of a fixture repo and drives the app in Chromium.
// Run: node web/tests/smoke.mjs   (needs `codewright` on PATH and Playwright with Chromium installed)
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { cpSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright"); // a local install or a global one (NODE_PATH)
const here = dirname(fileURLToPath(import.meta.url));
const fixture = join(here, "..", "..", "tests", "fixtures", "complex_agents");
const repo = join(mkdtempSync(join(tmpdir(), "cw-ui-")), "repo");
cpSync(fixture, repo, { recursive: true });
// one plain finding so the Review page has something to show
writeFileSync(join(repo, "requirements.txt"), "requests==2.32.3\nfastapi==0.115.0\n");
writeFileSync(join(repo, "svc.py"), "import requests\n\n\ndef fetch():\n    return requests.get('https://example.com')\n");
for (const args of [["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.email=ui@example.com", "-c", "user.name=ui", "commit", "-qm", "init"]]) {
  spawnSync("git", args, { cwd: repo });
}

const port = 8800 + Math.floor(Math.random() * 500);
const home = mkdtempSync(join(tmpdir(), "cw-home-"));
const server = spawn("codewright", ["serve", repo, "--port", String(port)], {
  env: { ...process.env, CODEWRIGHT_HOME: home, CODEWRIGHT_CACHE_DIR: join(home, "cache") },
  stdio: ["ignore", "pipe", "pipe"],
});
let log = "";
server.stdout.on("data", (d) => (log += d));
server.stderr.on("data", (d) => (log += d));
const base = `http://localhost:${port}`;

const failures = [];
async function check(name, fn) {
  try { await fn(); console.log(`ok   ${name}`); } catch (e) { failures.push(name); console.log(`FAIL ${name}: ${process.env.CW_UI_VERBOSE ? e.message : e.message.split("\n")[0]}`); }
}

async function waitForServer() {
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(`${base}/api/status`)).ok) return; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server did not start:\n${log}`);
}

let browser, page;
try {
  await waitForServer();
  browser = await chromium.launch();
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const text = (t) => page.getByText(t, { exact: false }).first();

  await page.goto(base);
  await page.waitForFunction(async (b) => (await (await fetch(`${b}/api/workspaces`)).json())[0]?.has_result, base, { timeout: 60000 });

  await check("AI page: architecture review, cost and diagram", async () => {
    await page.goto(`${base}/#/ai`);
    await text("Architecture review").waitFor();
    await text("decides by searching the model's free-text reply").waitFor();
    await text("Model calls per request").waitFor();
    await page.getByRole("cell", { name: "POST /chat" }).waitFor();
    await page.locator(".react-flow__node").nth(7).waitFor(); // laid out after the page: wait for the boxes
  });

  await check("AI page: add a missing link", async () => {
    await page.getByLabel("From", { exact: true }).fill("desk-service");
    await page.getByLabel("To", { exact: true }).fill("writer");
    await page.getByRole("button", { name: "Add missing link" }).click();
    await text("Saved to .codewright/agents.yaml").waitFor();
    await page.locator("b", { hasText: "desk-service" }).first().waitFor();
  });

  await check("Review: a finding explains its proof and offers actions", async () => {
    await page.goto(`${base}/#/review`);
    await page.getByRole("button", { name: /Ask AI to check|Ask AI for a second opinion/ }).first().waitFor();
    await text("There is no automatic patch for this").waitFor(); // why tests can't prove it, and what to do
  });

  await check("Review: how to verify, and the branch bar", async () => {
    await page.getByRole("button", { name: "How to verify" }).first().click();
    await text("Or ask the AI").waitFor();
    await page.getByRole("button", { name: "Let AI write a test and run it" }).first().waitFor();
    await page.getByRole("button", { name: "New branch", exact: true }).waitFor();
    await page.getByRole("button", { name: "New branch from…" }).click();
    await page.getByLabel("Start from").waitFor();
    await page.getByRole("button", { name: "Cancel" }).click();
  });

  await check("Knowledge: teach a card that becomes a check", async () => {
    await page.goto(`${base}/#/knowledge`);
    await page.getByRole("button", { name: "Teach Codewright" }).click();
    await page.locator("label", { hasText: "Title" }).locator("input").fill("Pass a timeout to requests");
    await page.locator("label", { hasText: "What to do" }).locator("textarea").fill("Always pass timeout=.");
    await page.getByText("Turn it into a check").click();
    await page.getByLabel("Calls to flag").fill("requests.get");
    await page.getByLabel("Missing keyword").fill("timeout");
    await page.getByRole("button", { name: "Save card" }).click();
    await text("the next scan flags requests.get").waitFor();
  });

  await check("Knowledge: keeping it current", async () => {
    await page.goto(`${base}/#/knowledge`);
    await text("Keeping it current").waitFor({ timeout: 60000 });
  });

  await check("Knowledge: add a library, deepen one", async () => {
    await page.getByRole("button", { name: "Add library" }).click();
    await page.getByLabel("Library", { exact: true }).fill("langgraph");
    await page.getByRole("button", { name: "Draft cards with AI" }).waitFor();
    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: /^pydantic/ }).first().click();
    await page.getByRole("button", { name: "Deepen pydantic" }).click();
    await text("Go deeper on").first().waitFor().catch(() => page.getByLabel("Focus").waitFor());
    await page.getByRole("button", { name: "Cancel" }).click();
  });

  await check("Dependencies: upgrade with code changes", async () => {
    await page.goto(`${base}/#/releases`);
    const btn = page.getByRole("button", { name: "Upgrade…" }).first();
    try { await btn.waitFor({ timeout: 45000 }); } catch { console.log("     (no newer release reachable: skipped)"); return; }
    await btn.click();
    await text("1. The requirement").waitFor();
    await text("3. Code changes the new version calls for").waitFor();
    await page.getByRole("button", { name: "Cancel" }).click();
  });

  await check("Settings: scan scope and AI usage", async () => {
    await page.goto(`${base}/#/settings`);
    await text("What gets reviewed").waitFor();
    await page.getByRole("cell", { name: "assistant", exact: true }).waitFor();
    await text("AI usage today").waitFor();
  });

  await check("Activity feed shows what the server did", async () => {
    await page.getByRole("button", { name: "Activity" }).click();
    await text("Review done").waitFor();
  });

  await check("no errors in the browser console", async () => {
    const real = errors.filter((e) => !/favicon/.test(e));
    if (real.length) throw new Error(real.join(" | "));
  });
} catch (e) {
  failures.push("setup");
  console.log(`FAIL setup: ${e.message}`);
} finally {
  await browser?.close();
  server.kill();
}
if (failures.length) {
  console.log(`\n${failures.length} UI check(s) failed\n--- server log ---\n${log.slice(-3000)}`);
  process.exit(1);
}
console.log("\nall UI checks passed");
