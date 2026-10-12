import { test, expect, type Page } from "@playwright/test";

// Synthetic content deliberately includes unbroken paths and large outputs.
// No test uses production configuration, conversations, or credentials.
const longPath = `https://example.invalid/${"archive-without-spaces".repeat(70)}`;
const developerRequest = `修复长内容布局 ${"开发需求".repeat(160)} ${longPath}`;
const developerTask = {
  id: "layout-task-12345678901234", actor: "console:admin", status: "pending_approval", version: 7,
  request: developerRequest,
  summary: `已整理修改方案。\n\n${longPath}\n\n\`\`\`diff\n+${"long-output".repeat(160)}\n\`\`\``,
  created_at: 1780000000, updated_at: 1780000001, iterations: 2, elapsed_seconds: 12,
  diff: `--- a/tools/example.py\n+++ b/tools/example.py\n@@ -1 +1 @@\n-${"before".repeat(200)}\n+${"after".repeat(240)}\n${" context line\n".repeat(90)}`,
  checks: [{ check: "py_compile", ok: false, output: `${longPath}\n${"Compilation diagnostics\n".repeat(100)}` }],
  calls: [{ iteration: 1, seconds: 6, status: "success" }], error: "",
};

async function mockUpgradePages(page: Page) {
  await page.route("**/api/admin/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const reply = (json: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
    if (path.endsWith("/auth/me")) return reply({ username: "admin", csrf_token: "layout-test-csrf" });
    if (request.method() !== "GET") return reply({ ok: true });
    if (path.endsWith("/agent/routes")) return reply({ items: [
      { role: "group_chat", provider: "antigravity", protocol: "openai", model: "gemini-3.8-flash-high", timeout: 90, max_output_tokens: 4000, concurrency: 2, daily_limit: 20, enabled: true, source: "yaml", version: 3, configured: true },
      { role: "expert", provider: "openai_responses", protocol: "responses", model: "gpt-5.6-sol", timeout: 180, max_output_tokens: 6000, concurrency: 2, daily_limit: 20, enabled: true, source: "console", version: 2, configured: true },
    ] });
    if (path.endsWith("/agent/memory")) return reply({ items: [{
      id: 41, group_id: 77, user_id: 123, kind: "fact", content: "经多次观察，喜欢五子棋和群内交流。".repeat(50),
      source: longPath, version: 5, protected: true,
      evidence: Array.from({ length: 80 }, (_, index) => ({ message_id: index, quote: `演示证据 ${longPath}` })),
    }] });
    if (path.endsWith("/agent/jobs")) return reply({
      spool: { pending: 3, bytes: 2048, last_error: longPath, warning: false },
      archive_bytes: 1024 * 1024, archive_limit_bytes: 5 * 1024 ** 3,
      embedding_tokens_reserved: { bootstrap: 12821, today: 219 },
      jobs: [{ group_id: 77, checkpoint: 7, status: "retry", pending: 3, attempts: 3, next_run: "2026-10-10T17:15:06+08:00", last_error: longPath }],
    });
    if (path.endsWith("/developer/tasks")) return reply({ items: [developerTask], total: 1 });
    if (path.includes("/developer/tasks/")) return reply(developerTask);
    return reply({ items: [], total: 0 });
  });
}

async function expectContainedControls(page: Page, width: number) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
  const controls = page.locator(".main-content textarea, .main-content select, .main-content input:not([type=checkbox])");
  for (const control of await controls.all()) {
    const bounds = await control.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width + 1);
    const inCard = await control.evaluate(element => {
      const card = element.closest(".card, .panel");
      if (!card) return true;
      const outer = card.getBoundingClientRect();
      const inner = element.getBoundingClientRect();
      return inner.left >= outer.left - 1 && inner.right <= outer.right + 1;
    });
    expect(inCard).toBe(true);
  }
}

async function expectLocalOutputScrolling(page: Page, selector: string) {
  const output = page.locator(selector).first();
  await expect(output).toBeVisible();
  const dimensions = await output.evaluate(element => ({
    overflow: getComputedStyle(element).overflowY,
    height: element.clientHeight, scrollHeight: element.scrollHeight,
  }));
  expect(["auto", "scroll"]).toContain(dimensions.overflow);
  expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.height);
}

for (const width of [1280, 390]) {
  test(`upgrade pages contain long content at ${width}px`, async ({ page }) => {
    await mockUpgradePages(page);
    await page.setViewportSize({ width, height: 900 });

    await page.goto("./#routes");
    await expect(page.getByRole("heading", { name: "用途路由", exact: true })).toBeVisible();
    await page.getByLabel("主聊天模型", { exact: true }).fill("long-model-name-".repeat(35));
    await expectContainedControls(page, width);

    await page.goto("./#memory-v2");
    await expect(page.getByLabel("记忆41", { exact: true })).toBeVisible();
    await page.locator("summary").filter({ hasText: "消息证据" }).click();
    await expectLocalOutputScrolling(page, ".developer-output");
    await expectContainedControls(page, width);

    await page.goto("./#memory-jobs");
    await expect(page.getByRole("heading", { name: "整理任务", exact: true })).toBeVisible();
    await page.getByText("诊断数据", { exact: true }).click();
    await expectContainedControls(page, width);
    const table = page.locator(".agent-job-table");
    await expect(table).toBeVisible();
    expect(await table.evaluate(element => getComputedStyle(element).overflowX)).toBe("auto");

    await page.goto("./#developers");
    await expect(page.getByRole("heading", { name: "开发任务", exact: true })).toBeVisible();
    const request = page.getByLabel("开发需求", { exact: true });
    await request.fill(developerRequest);
    const box = await request.boundingBox();
    expect(box!.width).toBeGreaterThan(width === 1280 ? 500 : 250);
    await page.getByRole("region", { name: "开发任务列表", exact: true }).getByRole("button", { name: /修复长内容布局/ }).click();
    await expectLocalOutputScrolling(page, ".developer-diff");
    await expectLocalOutputScrolling(page, ".developer-output");
    await expectContainedControls(page, width);
    await expect(page.getByRole("button", { name: "批准应用", exact: true })).toBeDisabled();
    await page.screenshot({ path: `test-results/upgrade-developer-${width}.png`, fullPage: true });
  });
}
