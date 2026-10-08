import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';

const settings = { version: 2, agent_enabled: true, model: 'gemini-3.5-flash', development_mode: 'test', test_groups: [123456], decision_timeout_seconds: 90, decision_concurrency: 2, decision_context_chars: 5000, decision_direct_context_chars: 5000, decision_recent_messages: 40, decision_direct_recent_messages: 60, decision_images: 3, decision_avatars: 1, summary_timeout_seconds: 180, decision_backend_search: true, hourly_reply_soft_limit: 30, daily_reply_limit: 200, quota_enforcement_enabled: true, quota_enforcement_groups: [123456] };
const groups = [
  { group_id: 123456, name: '日常交流群', mode: 'auto', enabled: true, version: 1, hourly_reply_soft_limit: 30, daily_reply_limit: 200, quota: { count: 146, limit: 200, hard_limit: 300, stage: 'normal', hour_count: 12, enforced: true } },
  { group_id: 234567, name: '开发测试群', mode: 'at', enabled: true, version: 3, hourly_reply_soft_limit: 20, daily_reply_limit: 100, quota: { count: 105, limit: 100, hard_limit: 150, stage: 'at_only', hour_count: 5, enforced: true } },
  { group_id: 345678, name: '游戏讨论群', mode: 'off', enabled: false, version: 1, hourly_reply_soft_limit: 30, daily_reply_limit: 200, quota: { count: 0, limit: 200, hard_limit: 300, stage: 'normal', hour_count: 0, enforced: false } },
];
const personas = { active_id: 'rin', items: [{ persona_id: 'rin', name: '远坂凛', documents: [{ id: 'doc1', name: 'prompt.md', path: 'legacy/prompt.md', persona_id: 'rin' }, { id: 'doc2', name: 'commands.md', path: 'shared/commands.md', persona_id: 'shared', shared: true }] }, { persona_id: 'eres', name: '艾蕾', documents: [] }, { persona_id: 'ishtar', name: '伊什塔尔', documents: [] }] };
const overview = { totals: { incoming: 2847, interaction: 421, reply_round: 251, model_request: 430 }, active_users: 86, active_groups: 2, daily: [{ day: '2026-09-17', incoming: 280, interaction: 34, reply_round: 22 }, { day: '2026-09-18', incoming: 360, interaction: 57, reply_round: 41 }, { day: '2026-09-19', incoming: 240, interaction: 29, reply_round: 17 }, { day: '2026-09-20', incoming: 510, interaction: 83, reply_round: 47 }, { day: '2026-09-21', incoming: 475, interaction: 63, reply_round: 39 }, { day: '2026-09-22', incoming: 600, interaction: 108, reply_round: 57 }, { day: '2026-09-23', incoming: 382, interaction: 47, reply_round: 28 }], status: { agent_enabled: true, active_persona: 'rin', model: 'gemini-3.5-flash', active_groups: 2 }, quotas: groups };

async function mock(page: Page, loggedIn = true) {
  await page.route('**/test-assets/rin.jpg', route => route.fulfill({path: path.resolve('../data/dutyroster/rin.jpg'), contentType: 'image/jpeg'}));
  await page.route(/https:\/\/.*qlogo\.cn\//, route => route.abort());
  const writes: { path: string; method: string; body: any; csrf: string | undefined }[] = [];
  let authenticated = loggedIn;
  await page.route('**/api/admin/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname.replace('/api/admin', ''); const method = request.method();
    const reply = (json: any, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
    if (path === '/auth/me') return reply(authenticated ? { username: 'admin', csrf_token: 'test-csrf' } : { detail: 'Unauthorized' }, authenticated ? 200 : 401);
    if (method !== 'GET') {
      const body = request.postDataJSON(); writes.push({ path, method, body, csrf: request.headers()['x-csrf-token'] });
      if (path === '/auth/login') authenticated = true;
      if (path === '/auth/logout') authenticated = false;
      return reply({ ok: true, succeeded_ids: body?.ids || [], items: [], failed: [] });
    }
    if (path === '/overview') return reply(overview);
    if (path === '/settings') return reply(settings);
    if (path === '/groups') return reply({ items: groups, total: groups.length });
    if (path === '/stats/users') return reply({ items: [{ user_id: 10001, group_id: 123456, incoming: 35, interaction: 8, direct_at: 3, reply_round: 6 }], total: 1 });
    if (path === '/personas') return reply(personas);
    if (path === '/personas/documents/doc1') return reply({ id: 'doc1', name: 'prompt.md', path: 'legacy/prompt.md', persona_id: 'rin', content: '# 远坂凛\n\n测试人格文档。', version: 'version-one' });
    if (path.endsWith('/revisions')) return reply({ items: [{ id: 'revision-old', version: 'version-old', actor: 'admin', reason: '初始版本', created_at: '2026-09-22T10:00:00' }], total: 1 });
    if (path === '/memory/affinities') return reply({ items: [{ id: 1, user_id: 10001, persona_id: 'rin', affinity_score: 25, updated_at: '2026-09-23T10:00:00' }], total: 1 });
    if (path === '/memory/facts') return reply({ items: [{ id: 2, group_id: 123456, user_id: 10001, fact: '喜欢格斗游戏', category: 'preference', status: 'active', importance: 3, confidence: 1, protected: true, updated_at: '2026-09-23T10:00:00' }], total: 1 });
    if (path === '/memes') return reply({ items: [{ id: 1, object_name: 'happy-face.png', emotion: 'happy', status: 'active', send_count: 23, note: '', url: '' }, { id: 2, object_name: 'calm-face.png', emotion: 'calm', status: 'active', send_count: 12, note: '', url: '' }], total: 2 });
    if (path === '/requests') return reply({ items: [{ id: 'r1', model: 'gemini-3.5-flash', provider: 'antigravity', source: 'agent', group_id: 123456, status: 'ok', latency_ms: 2380, total_tokens: null, input_tokens: null, output_tokens: null, created_at: '2026-09-23T10:00:00' }], total: 1 });
    return reply({ items: [], total: 0 });
  });
  return writes;
}

test('login uses cookie session and never persists credentials', async ({ page }) => {
  const writes = await mock(page, false);
  await page.goto('./');
  await expect(page).toHaveTitle('管理控制台');
  await expect(page.locator('.login-brand')).toHaveText('管理控制台');
  await expect(page.getByLabel('用户名', { exact: true })).toHaveAttribute('placeholder', 'admin');
  await page.getByLabel('用户名').fill('admin'); await page.getByLabel('密码', { exact: true }).fill('example-test-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行概览' })).toBeVisible();
  await expect(page.locator('.brand-copy strong')).toHaveText('管理控制台');
  await expect(page.locator('body')).not.toContainText('QQ Agent');
  await expect(page.locator('body')).not.toContainText('QQ Bot');
  expect(writes[0].body).toEqual({ username: 'admin', password: 'example-test-password' });
  const storage = await page.evaluate(() => JSON.stringify(localStorage));
  expect(storage).not.toContain('password'); expect(storage).not.toContain('test-csrf');
});

test('dashboard displays API metrics and charts, desktop and dark screenshots', async ({ page }) => {
  await mock(page); await page.setViewportSize({ width: 1440, height: 1050 }); await page.goto('./');
  await expect(page.getByText('2,847', { exact: true })).toBeVisible();
  await expect(page.locator('.recharts-line-curve')).toHaveCount(3);
  await expect(page.getByText('日常交流群', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/desktop-dashboard.png', fullPage: true });
  await page.getByRole('button', { name: '切换深色模式' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: 'test-results/dark-dashboard.png', fullPage: true });
});

test('group bulk editing includes versions and CSRF', async ({ page }) => {
  const writes = await mock(page); await page.goto('./#groups');
  await page.getByLabel('选择群 123456', { exact: true }).check(); await page.getByLabel('选择群 234567', { exact: true }).check();
  await page.getByRole('button', { name: '批量设置', exact: true }).click();
  await page.getByRole('dialog').getByLabel('回复模式').selectOption('at');
  await page.getByRole('dialog').getByLabel('每日回复上限').fill('150');
  await page.getByRole('dialog').getByRole('button', { name: '保存设置' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(writes[0]).toMatchObject({ path: '/groups/bulk', method: 'PATCH', csrf: 'test-csrf', body: { group_ids: [123456, 234567], patch: { mode: 'at', daily_reply_limit: 150 }, versions: { '123456': 1, '234567': 3 } } });
});

test('settings preserve group ID input and save only changes', async ({ page }) => {
  const writes = await mock(page); await page.goto('./#settings');
  await page.getByLabel('测试群组', { exact: true }).fill('123456, 234567');
  await page.getByLabel('决策超时（秒）').fill('100');
  await page.getByRole('button', { name: '保存设置' }).click();
  await expect(page.getByText('设置已保存', { exact: true })).toBeVisible();
  expect(writes[0].body).toEqual({ version: 2, patch: { test_groups: [123456, 234567], decision_timeout_seconds: 100 } });
});

test('meme hard deletion requires explicit confirmation', async ({ page }) => {
  const writes = await mock(page); await page.goto('./#memes');
  await page.getByRole('button', { name: '删除表情 1', exact: true }).click();
  await page.getByLabel('同时永久删除存储文件').check();
  await expect(page.getByRole('button', { name: '永久删除', exact: true })).toBeDisabled();
  await page.getByLabel('我确认永久删除这 1 个文件').check();
  await page.getByRole('button', { name: '永久删除', exact: true }).click();
  expect(writes[0]).toMatchObject({ path: '/memes/batch', body: { ids: [1], action: 'delete', hard: true }, csrf: 'test-csrf' });
});

test('persona Markdown saves current version and restores backups', async ({ page }) => {
  const writes = await mock(page); await page.goto('./#personas');
  await page.getByLabel('Markdown 文档内容').fill('# 新标题\n\n更新内容');
  await page.getByRole('button', { name: '预览', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新标题' })).toBeVisible();
  await page.getByRole('button', { name: '保存文档' }).click();
  await expect(page.getByText('文档已保存并备份', { exact: true })).toBeVisible();
  expect(writes[0]).toMatchObject({ path: '/personas/documents/doc1', method: 'PUT', body: { content: '# 新标题\n\n更新内容', version: 'version-one' } });
  await page.getByRole('button', { name: '查看版本历史' }).click();
  await page.getByRole('button', { name: '恢复版本 version-ol' }).click();
  await page.getByRole('button', { name: '确认', exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].body).toEqual({ revision_id: 'revision-old', version: 'version-one' });
});

test('affinity adjustment requires a reason', async ({ page }) => {
  const writes = await mock(page); await page.goto('./#memory');
  await page.getByRole('button', { name: '好感度', exact: true }).click();
  await page.getByRole('button', { name: '编辑记录 1', exact: true }).click();
  const save = page.getByRole('dialog').getByRole('button', { name: '保存', exact: true });
  await expect(save).toBeDisabled();
  await page.getByLabel('好感度分值').fill('30'); await page.getByLabel('修改原因', { exact: true }).fill('修正误记的分数');
  await save.click(); await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(writes[0]).toMatchObject({ path: '/affinities/rin/10001', method: 'PUT', csrf: 'test-csrf', body: { score: 30, reason: '修正误记的分数', group_id: 0 } });
});

test('mobile layout, navigation and tables do not overflow the viewport', async ({ page }) => {
  await mock(page); await page.setViewportSize({ width: 390, height: 844 }); await page.goto('./');
  await expect(page.getByText('2,847', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/mobile-dashboard.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('link', { name: '群聊管理', exact: true }).click();
  await expect(page.getByRole('heading', { name: '群聊管理' })).toBeVisible();
  await expect(page.locator('.drawer-backdrop')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/mobile-groups.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('failure states do not fabricate metrics and can retry', async ({ page }) => {
  await mock(page); await page.route('**/api/admin/overview**', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Database unavailable' }) }));
  await page.goto('./'); await expect(page.getByRole('alert')).toContainText('Database unavailable');
  await expect(page.locator('.metric')).toHaveCount(0); await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible();
});

test('unknown token usage remains unavailable and filters reach API', async ({ page }) => {
  await mock(page); await page.goto('./#requests');
  await expect(page.locator('tbody')).toContainText('未知 输入 / 未知 输出');
  const request = page.waitForRequest(request => request.url().includes('/requests') && request.url().includes('status=timeout'));
  await page.getByLabel('请求状态筛选').selectOption('timeout'); await request;
});

test('meme image renders real bitmap pixels with lazy loading', async ({page}) => {
  await mock(page);
  await page.route('**/api/admin/memes?*', route => route.fulfill({contentType:'application/json', body:JSON.stringify({items:[{id:1, object_name:'rin.jpg', status:'active', emotion:'happy', url:'/test-assets/rin.jpg'}], total:1})}));
  await page.goto('./#memes');
  const image = page.getByAltText('rin.jpg');
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  await page.screenshot({path:'test-results/memes-bitmap.png', fullPage:true});
});

test('relationship and affinity sorting is sent to API and resets pagination', async ({ page }) => {
  await mock(page);
  await page.route('**/api/admin/memory/relationships?*', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [{ id: 7, user_id: 10002, group_id: 123456, persona_id: 'rin', message_count: 72, explicit_interaction_count: 8 }], total: 30 }) }));
  await page.goto('./#memory');
  await page.getByRole('button', { name: '关系状态', exact: true }).click();
  await expect(page.getByLabel('用户列表排序')).toBeVisible();
  await expect(page.locator('tbody')).toContainText('72 条消息');
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('.pagination')).toContainText('26–30 / 30');
  const countRequest = page.waitForRequest(request => request.url().includes('/memory/relationships?') && request.url().includes('sort_by=message_count'));
  await page.getByLabel('用户列表排序').selectOption('message_count');
  const countQuery = new URL((await countRequest).url()).searchParams;
  expect(countQuery.get('sort_order')).toBe('desc');
  expect(countQuery.get('offset')).toBe('0');
  await page.getByRole('button', { name: '好感度', exact: true }).click();
  await expect(page.getByLabel('用户列表排序')).toHaveValue('updated_at');
  const scoreRequest = page.waitForRequest(request => request.url().includes('/memory/affinities?') && request.url().includes('sort_by=affinity_score'));
  await page.getByLabel('用户列表排序').selectOption('affinity_score');
  expect(new URL((await scoreRequest).url()).searchParams.get('sort_order')).toBe('desc');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: 'test-results/mobile-memory-sort.png', fullPage: true });
});

test('same-origin meme image carries session cookie and retries transient errors', async ({ page, context }) => {
  await mock(page);
  await context.addCookies([{ name: 'test_admin_session', value: 'image-cookie', domain: '127.0.0.1', path: '/', httpOnly: true, sameSite: 'Strict' }]);
  await page.route('**/api/admin/memes?*', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [{ id: 1, object_name: 'authenticated-rin.jpg', status: 'active', emotion: 'happy', url: '/api/admin/memes/1/image' }], total: 1 }) }));
  let attempts = 0;
  let cookie = '';
  await page.route('**/api/admin/memes/1/image', async route => {
    attempts += 1;
    cookie = (await route.request().allHeaders()).cookie || '';
    if (attempts === 1) return route.fulfill({ status: 502, body: 'Storage unavailable' });
    return route.fulfill({ path: path.resolve('../data/dutyroster/rin.jpg'), contentType: 'image/jpeg', headers: { 'Cache-Control': 'private, no-store' } });
  });
  await page.goto('./#memes');
  await expect(page.getByText('图片不可用', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重新加载图片 authenticated-rin.jpg' }).click();
  const image = page.getByAltText('authenticated-rin.jpg');
  await expect(image).toHaveAttribute('src', '/api/admin/memes/1/image');
  await expect(image).toHaveAttribute('loading', 'lazy');
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  expect(cookie).toContain('test_admin_session=image-cookie');
  expect(attempts).toBe(2);
  await expect(page.getByText('图片不可用', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/memes-same-origin.png', fullPage: true });
});
