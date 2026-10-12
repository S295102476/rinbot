import { test, expect } from "@playwright/test";

test("purpose routes save only the selected role and memory corrections retain revision", async ({ page }) => {
  const writes: { path: string; body: any; csrf?: string }[] = [];
  const privacy = { learning: true, style_learning: true, evaluation: true, version: 9 };
  const routes = [
    { role: "group_chat", provider: "antigravity", protocol: "openai", model: "gemini-3.8-flash-high", timeout: 90, max_output_tokens: 4000, concurrency: 2, daily_limit: 20, enabled: true, source: "yaml", version: 3, configured: true },
    { role: "emotion", provider: "antigravity", protocol: "openai", model: "gemini-3.8-flash-low", timeout: 15, max_output_tokens: 128, concurrency: 2, daily_limit: 20, enabled: true, source: "default", version: 0, configured: true },
  ];
  await page.route("**/api/admin/**", async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const reply = (json: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
    if (path.endsWith("/auth/me")) return reply({ username: "admin", csrf_token: "upgrade-test-csrf" });
    if (request.method() !== "GET") {
      writes.push({ path, body: request.postDataJSON(), csrf: request.headers()["x-csrf-token"] });
      if (path.endsWith("/agent/privacy/123")) {
        privacy.style_learning = request.postDataJSON().style_learning;
        privacy.version++;
        return reply(privacy);
      }
      return reply({ ok: true });
    }
    if (path.endsWith("/agent/routes")) return reply({ items: routes });
    if (path.endsWith("/agent/memory")) return reply({ items: [{ id: 41, group_id: 77, user_id: 123, kind: "fact", content: "喜欢五子棋", source: "observation", version: 5, protected: false, evidence: [{ message_id: 100, quote: "来一局五子棋" }] }] });
    if (path.endsWith("/agent/privacy/123")) return reply(privacy);
    if (path.endsWith("/agent/jobs")) return reply({ pending: 3, archive_bytes: 1024, daily_embedding_tokens: 64 });
    return reply({ items: [], total: 0 });
  });
  await page.goto("./#routes");
  await expect(page.getByRole("heading", { name: "用途路由", exact: true })).toBeVisible();
  await page.getByLabel("主聊天模型", { exact: true }).fill("gemini-3.8-flash-low");
  await page.getByRole("button", { name: "保存主聊天" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ path: "/api/admin/agent/routes/group_chat", csrf: "upgrade-test-csrf", body: { version: 3, patch: { model: "gemini-3.8-flash-low" } } });
  await expect(page.getByLabel("情绪判断模型", { exact: true })).toHaveValue("gemini-3.8-flash-low");
  await page.goto("./#memory-v2");
  await expect(page.getByRole("heading", { name: "长期记忆", exact: true })).toBeVisible();
  await page.getByLabel("记忆41", { exact: true }).fill("喜欢五子棋，偏好人机对局");
  await page.getByRole("button", { name: "保存修订" }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({ path: "/api/admin/agent/memory/41", body: { version: 5, content: "喜欢五子棋，偏好人机对局" } });
  await page.getByLabel("记忆群号", { exact: true }).fill("77");
  await page.getByLabel("记忆用户", { exact: true }).fill("123");
  await page.getByRole("checkbox", { name: "风格学习", exact: true }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2]).toMatchObject({ path: "/api/admin/agent/privacy/123", body: { group_id: 77, version: 9, style_learning: false } });
  await expect(page.getByRole("checkbox", { name: "风格学习", exact: true })).not.toBeChecked();
  await page.goto("./#memory-jobs");
  await expect(page.getByRole("heading", { name: "整理任务", exact: true })).toBeVisible();
  await page.getByText("诊断数据", { exact: true }).click();
  await expect(page.locator(".developer-output")).toContainText('"pending": 3');
  await page.getByRole("button", { name: "重试待整理任务" }).click();
  await expect.poll(() => writes.length).toBe(4);
  expect(writes[3].path).toBe("/api/admin/agent/jobs/retry");
});
