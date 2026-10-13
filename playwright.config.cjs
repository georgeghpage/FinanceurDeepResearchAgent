const { defineConfig } = require('@playwright/test');
const port = Number(process.env.QA_BROWSER_PORT || 8765);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid QA_BROWSER_PORT');
const baseURL = `http://127.0.0.1:${port}`;
const reportRoot = process.env.QA_BROWSER_REPORT_DIR || 'reports/qa/browser';
module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30000,
  expect: { timeout: 10000, toHaveScreenshot: { animations: 'disabled', maxDiffPixelRatio: 0.005 } },
  snapshotPathTemplate: '{testDir}/snapshots/{platform}/{projectName}/{testFilePath}/{arg}{ext}',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  outputDir: `${reportRoot}/artifacts`,
  reporter: [['list'], ['html', { outputFolder: `${reportRoot}/html`, open: 'never' }], ['junit', { outputFile: `${reportRoot}/junit.xml` }], ['json', { outputFile: `${reportRoot}/results.json` }]],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', reducedMotion: 'reduce', locale: 'en-US', timezoneId: 'UTC' },
  webServer: { command: `${process.env.QA_PYTHON || '.venv/bin/python'} tests/browser/server.py ${port}`, url: `${baseURL}/api/status`, reuseExistingServer: false, timeout: 20000 },
  projects: [
    { name: 'chromium', testIgnore: /visual/, use: { browserName: 'chromium' } },
    { name: 'firefox', testIgnore: /visual/, use: { browserName: 'firefox' } },
    { name: 'webkit', testIgnore: /visual/, use: { browserName: 'webkit' } },
    { name: 'visual', testMatch: /visual/, use: { browserName: 'chromium', bypassCSP: true, viewport: { width: 1440, height: 1000 } } },
  ],
});
