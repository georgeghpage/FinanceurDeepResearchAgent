const { test, expect } = require('@playwright/test');

test('strict CSP native-select browser diagnostic is reproducible without app scripts', async ({ page, browserName }, info) => {
  const errors = [];
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  await page.route('**/native-select', route => route.fulfill({
    headers: { 'Content-Security-Policy': "default-src 'self'; style-src 'self'" },
    contentType: 'text/html', body: '<!doctype html><html><body><select aria-label="Native"><option>Native</option></select></body></html>',
  }));
  await page.goto('/native-select');
  await expect(page.getByLabel('Native')).toHaveValue('Native');
  await info.attach('native-select-console', { body: JSON.stringify(errors), contentType: 'application/json' });
  if (browserName === 'webkit') expect(errors).toEqual([
    "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline' does not appear in the style-src directive of the Content Security Policy.",
  ]);
  else expect(errors).toEqual([]);
});
