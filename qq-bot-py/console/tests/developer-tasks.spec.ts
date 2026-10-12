import { test, expect } from "@playwright/test";

test("development approval requires reviewing full diff and sends version with CSRF", async ({ page }) => {
  const task = {
    id: "a".repeat(24), actor: "console:admin", status: "pending_approval", version: 7,
    request: "修正签到图片尺寸", summary: "调整图片宽度", created_at: 1780000000,
    updated_at: 1780000001, iterations: 2, elapsed_seconds: 12,
    diff: "--- a/plugins/example.py\n+++ b/plugins/example.py\n@@ -1 +1 @@\n-width = 100\n+width = 200\n",
    checks: [{ check: "py_compile", ok: true, output: "Compilation passed" }],
    calls: [{ iteration: 1, seconds: 6, status: "success" }], error: "",
  };
  const writes: { body: unknown; csrf?: string }[] = [];
  await page.route("**/api/admin/**", async route => {
    const request = route.request(); const url = new URL(request.url());
    const reply = (json: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
    if (url.pathname.endsWith("/auth/me")) return reply({ username: "admin", csrf_token: "developer-test-csrf" });
    if (url.pathname.endsWith("/approve")) {
      writes.push({ body: request.postDataJSON(), csrf: request.headers()["x-csrf-token"] });
      task.status = "applied"; task.version++; return reply(task);
    }
    if (url.pathname.endsWith("/developer/tasks")) return reply({ items: [task], total: 1 });
    if (url.pathname.includes("/developer/tasks/")) return reply(task);
    return reply({ items: [], total: 0 });
  });
  await page.goto("./#developers");
  await expect(page.getByRole("heading", { name: "开发任务", exact: true })).toBeVisible();
  await page.getByRole("button", { name: /修正签到图片尺寸/ }).click();
  await expect(page.locator(".developer-diff")).toContainText("+width = 200");
  const approve = page.getByRole("button", { name: "批准应用" });
  await expect(approve).toBeDisabled();
  await page.getByRole("checkbox", { name: "我已检查完整差异与检查结果" }).check();
  page.once("dialog", dialog => dialog.accept());
  await approve.click();
  await expect(page.getByRole("button", { name: "回滚源文件" })).toBeVisible();
  expect(writes).toEqual([{ body: { version: 7 }, csrf: "developer-test-csrf" }]);
});
