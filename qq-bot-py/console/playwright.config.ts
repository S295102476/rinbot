import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 45000,
  workers: 2,
  fullyParallel: true,
  use: { baseURL: 'http://127.0.0.1:5173/admin/', headless: true, trace: 'retain-on-failure' },
  webServer: { command: 'npm run dev -- --port 5173 --strictPort', url: 'http://127.0.0.1:5173/admin/', reuseExistingServer: true, timeout: 30000 },
});
