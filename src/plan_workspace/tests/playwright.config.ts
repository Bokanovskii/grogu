import { defineConfig, devices } from "@playwright/test";

const PORT = process.env.FIXTURE_PORT ?? "5180";
const BASE = `http://127.0.0.1:${PORT}`;

// The fixture server is started for the whole run and reused. It serves the
// committed dist/ and the contract fixture, all on loopback. No test ever
// reaches the network.
export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    baseURL: BASE,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `node fixture-server.mjs`,
    env: { PORT },
    url: `${BASE}/api/health`,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
  },
});
