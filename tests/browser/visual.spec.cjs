const { test, expect, open, submit, complete } = require('./helpers.cjs');
test.setTimeout(60000);

async function snapshot(page, name) {
  await page.evaluate(() => document.fonts.ready);
  const mask = [
    '#elapsed', '#dashboard-duration', '#dashboard-run', '#dashboard-axis-end',
    '.timeline', '.calls-table td:nth-child(3)', '.trace-heading span',
    '#status',
  ].map(s => page.locator(`${s}:visible`));
  await expect(page).toHaveScreenshot(`${name}.png`, {
    fullPage: false, mask, maskColor: '#dce4de',
    style: '* { font-family: Arial, sans-serif !important; caret-color: transparent !important; animation: none !important; transition: none !important; }',
  });
}

test('empty and entered question', async ({ page }) => {
  await open(page);
  await snapshot(page, 'empty');
  await page.locator('#question').fill('Compare measurements in 2026');
  await snapshot(page, 'entered');
});

for (const [stage, text] of [['plan', 'Planning research'], ['search', 'Searching the web'], ['extract', 'Reading sources'], ['verify', 'Cross-checking findings'], ['write', 'Synthesizing the answer']]) {
  test(`in progress at ${stage}`, async ({ page }) => {
    await open(page);
    await submit(page, `[pause:${stage}] Visual workflow`);
    await expect(page.locator('#events')).toContainText(text);
    await expect(page.locator('#submit')).toBeDisabled();
    // Search and extractor fan-out must finish scheduling before the capture.
    if (stage === 'search') await expect(page.locator('#dashboard-rows tr')).toHaveCount(5);
    if (stage === 'extract') await expect(page.locator('#source-count')).toContainText('15 sources discovered');
    await snapshot(page, stage);
  });
}

for (const [name, question] of [['completed', 'Established facts'], ['refined', '[refine] Establish missing facts'], ['failure', '[failure] Provider unavailable'], ['long-report', '[long] Compare supported facts']]) {
  test(name, async ({ page }) => {
    await open(page); await submit(page, question); await complete(page);
    await page.locator('#answer-title').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await snapshot(page, name);
  });
}

test('cancelled', async ({ page }) => {
  await open(page); await submit(page, '[pause:search] Cancel visual workflow');
  await expect(page.locator('#events')).toContainText('Searching the web');
  await expect(page.locator('#dashboard-rows tr')).toHaveCount(5);
  await page.locator('#stop').click(); await complete(page);
  await snapshot(page, 'cancelled');
});

for (const width of [360, 768, 1440]) {
  test(`completed at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page); await submit(page, 'Responsive evidence report'); await complete(page);
    await page.locator('#answer-title').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await snapshot(page, `completed-${width}`);
  });
}

test('edited document', async ({ page }) => {
  await open(page); await submit(page, 'Editable evidence report'); await complete(page);
  await page.locator('#answer p').first().click();
  await page.keyboard.press('End');
  await page.keyboard.type(' Editorial review complete.');
  await expect(page.locator('#document-status')).toHaveText('Saved');
  await page.locator('#answer-title').evaluate(el => el.scrollIntoView({ block: 'start' }));
  await snapshot(page, 'edited-document');
  await page.screenshot({ path: 'reports/qa/edited-report.png', fullPage: true });
});

test('media document', async ({ page }) => {
  // Rendering uses a fixed generated document; editing/round-trip behavior has
  // dedicated functional tests independent of cursor timing in screenshots.
  await open(page); await submit(page, '[media] Formatted evidence report'); await complete(page);
  await expect(page.locator('#answer math')).toHaveCount(1);
  await expect(page.locator('#answer svg rect')).toHaveCount(2);
  await expect(page.locator('#document-status')).toContainText('Saved');
  await page.locator('#answer-title').evaluate(el => el.scrollIntoView({ block: 'start' }));
  await snapshot(page, 'media-document');
  await page.screenshot({ path: 'reports/qa/media-report.png', fullPage: true });
});
