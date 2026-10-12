import { test, expect, type Page } from "@playwright/test";

async function mockSession(page: Page) {
  await page.route("**/api/admin/**", route => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith("/auth/me")
      ? { username: "admin", csrf_token: "appearance-test-csrf" }
      : { items: [], total: 0 };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("appearance choices persist across reload and presets remain editable", async ({ page }) => {
  await mockSession(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("./#appearance");
  await expect(page.getByRole("heading", { name: "外观设置", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "深色模式", exact: true }).click();
  await page.getByRole("button", { name: "纯色风格", exact: true }).click();
  await page.getByRole("button", { name: "星云紫", exact: true }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "纯色风格", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "星云紫", exact: true })).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("button", { name: "背景", exact: true }).click();
  await page.getByRole("button", { name: "紫雾星云", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "背景", exact: true }).click();
  await expect(page.getByRole("button", { name: "紫雾星云", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "主题预设", exact: true }).click();
  await page.getByRole("button", { name: /^简洁日光/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: "外观", exact: true }).click();
  await page.getByRole("button", { name: "毛玻璃风格", exact: true }).click();
  await expect(page.getByRole("button", { name: "毛玻璃风格", exact: true })).toHaveAttribute("aria-pressed", "true");
  const opacity = page.getByRole("slider", { name: /面板不透明度/ });
  await opacity.press("Home");
  await expect(opacity).toHaveValue("60");
  await page.getByRole("button", { name: "跟随系统", exact: true }).click();
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "背景", exact: true }).click();
  await page.getByRole("button", { name: "深空星河", exact: true }).click();
  await page.getByRole("button", { name: "外观", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1280);
  await page.screenshot({ path: "test-results/appearance-settings-desktop.png", fullPage: true });
});

test("local wallpaper survives refresh and can be removed", async ({ page }) => {
  await mockSession(page);
  // Enforce the production console's image directive, which rejects blob URLs.
  // Leave dev-script directives alone so Vite's React refresh prelude can run.
  await page.route("**/admin/", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: {
      ...response.headers(), "Content-Security-Policy": "img-src 'self' data: http: https:",
    } });
  });
  await page.goto("./#appearance");
  await page.getByRole("button", { name: "背景", exact: true }).click();
  const png = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 48; canvas.height = 32;
    const context = canvas.getContext("2d")!;
    context.fillStyle = "#173251"; context.fillRect(0, 0, 48, 32);
    context.fillStyle = "#9ac6fa"; context.fillRect(20, 12, 8, 8);
    return canvas.toDataURL("image/png").split(",")[1];
  });
  await page.getByLabel("选择自定义壁纸", { exact: true }).setInputFiles({
    name: "demo-wallpaper.png", mimeType: "image/png", buffer: Buffer.from(png, "base64"),
  });
  const image = page.getByAltText("已保存的自定义壁纸", { exact: true });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(48);
  await expect(page.locator("html")).toHaveAttribute("data-wallpaper", "custom");
  expect(await page.locator(".console-wallpaper").evaluate(element => getComputedStyle(element).backgroundImage)).toContain("data:image/");
  await page.reload();
  await page.getByRole("button", { name: "背景", exact: true }).click();
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(48);
  expect(await page.locator(".console-wallpaper").evaluate(element => getComputedStyle(element).backgroundImage)).toContain("data:image/");
  await page.getByRole("button", { name: "移除自定义壁纸", exact: true }).click();
  await expect(image).toHaveCount(0);
  await expect(page.locator("html")).toHaveAttribute("data-wallpaper", "starfield");
  await page.reload();
  await page.getByRole("button", { name: "背景", exact: true }).click();
  await expect(image).toHaveCount(0);
});

test("invalid stored appearance falls back to usable defaults on mobile", async ({ page }) => {
  await mockSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("qq-admin-appearance", "{not-valid-json"));
  await page.goto("./#appearance");
  await expect(page.getByRole("heading", { name: "外观设置", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "毛玻璃风格", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "背景", exact: true }).click();
  await expect(page.getByRole("button", { name: "深空星河", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "test-results/appearance-settings-mobile.png", fullPage: true });
});
