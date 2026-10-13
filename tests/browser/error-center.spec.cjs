const { test, expect, open, submit, complete } = require('./helpers.cjs');
const AxeBuilder = require('@axe-core/playwright').default;

test('retries recover one logical issue and remain inspectable with the keyboard', async ({ page }) => {
  await open(page);
  await submit(page, '[retry] Recover a provider rate limit');
  await complete(page);
  await expect(page.locator('#critical-error')).not.toBeVisible();
  await expect(page.locator('#error-count')).toBeHidden();
  await page.locator('#error-center-toggle').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#error-center')).toBeVisible();
  await expect(page.locator('#error-center-summary')).toContainText('0 unresolved · 1 recovered');
  await expect(page.locator('#dashboard-issue-list details')).toHaveCount(1);
  const issue = page.locator('#dashboard-issue-list details');
  await issue.locator('summary').click();
  await expect(issue).toContainText('attempt 2');
  await expect(issue).toContainText('A subsequent attempt succeeded');
  await expect(issue).toContainText('Correlation ID');
  await expect(issue).not.toContainText('CANARY');
  await page.screenshot({ path: 'reports/qa/error-center-preview.png' });
  const accessibility = await new AxeBuilder({ page }).include('#error-center').analyze();
  expect(accessibility.violations).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(page.locator('#error-center')).not.toBeVisible();
  await expect(page.locator('#error-center-toggle')).toBeFocused();
  await expect(page.locator('#answer')).not.toContainText('rate_limit');
});

test('dismissing a nonblocking notification preserves the unresolved diagnostic', async ({ page }) => {
  await open(page);
  await submit(page, '[partial-search] Retain available evidence');
  await complete(page);
  await expect(page.locator('#critical-error')).not.toBeVisible();
  await expect(page.locator('#error-count')).toContainText('1');
  await page.locator('#error-center-toggle').click();
  const issue = page.locator('#dashboard-issue-list details').first();
  await issue.locator('summary').click();
  await expect(issue).toContainText('attempt 4');
  await page.locator('#dashboard-issue-list button').first().click();
  await expect(page.locator('#dashboard-issue-list details')).toHaveCount(1);
  await expect(page.locator('#error-count')).toContainText('1');
  await expect(page.locator('#dashboard-issue-list')).toContainText('history retained');
  await expect(page.locator('#answer')).not.toContainText('Source coverage incomplete');
  await expect(page.locator('#answer a')).not.toHaveCount(0);
});

test('only terminal failure opens a prominent dialog and history replays without popups', async ({ page }) => {
  await open(page);
  await submit(page, '[failure] Provider stopped before collecting evidence');
  await complete(page);
  await expect(page.locator('#critical-error')).toBeVisible();
  await expect(page.locator('#critical-error-message')).toContainText('Provider returned a server error');
  await page.locator('#critical-inspect').click();
  await expect(page.locator('#error-center')).toBeVisible();
  await expect(page.locator('#dashboard-issue-list')).toContainText('fatal');
  await expect(page.locator('#answer')).not.toContainText('provider_unavailable');
  await page.locator('#error-center-close').click();
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer-section')).toBeVisible();
  await expect(page.locator('#critical-error')).not.toBeVisible();
  await expect(page.locator('#error-count')).not.toBeHidden();
});
