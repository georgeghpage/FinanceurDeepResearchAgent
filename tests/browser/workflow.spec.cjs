const { test, expect, open, submit, complete } = require('./helpers.cjs');
const AxeBuilder = require('@axe-core/playwright').default;

test('normal research reaches actual backend, cites sources and downloads report', async ({ page }) => {
  await open(page);
  const requests = [];
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/research')) requests.push(request.postDataJSON()); });
  await submit(page, 'Establish facts in 中文 in 2026');
  await complete(page);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({ question: 'Establish facts in 中文 in 2026', provider: 'aliyun', model: 'deepseek-v4.1-flash' });
  await expect(page.locator('#answer')).toContainText('measured value of 42');
  await expect(page.locator('#answer h1')).toHaveText('Evidence report');
  const link = page.locator('#answer a').first();
  await expect(link).toHaveAttribute('href', /https:\/\/example.com\/source\//);
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#download').click()]);
  expect(download.suggestedFilename()).toBe('research-report.md');
  const stream = await download.createReadStream();
  let body = ''; for await (const chunk of stream) body += chunk;
  expect(body).toContain('[S1: Official publication]');
  await expect(page.locator('#events')).toContainText('Planning research');
});

test('refinement searches only targeted follow-up and completes', async ({ page }) => {
  await open(page);
  await submit(page, '[refine] Establish missing fact');
  await complete(page);
  expect(await page.locator('#events li').filter({ hasText: 'Searching the web' }).count()).toBe(2);
  await expect(page.locator('#events')).not.toContainText('targeted additional evidence');
  await expect(page.locator('#answer')).toContainText('Search calls: 16');
});

test('provider failure ends loading with usable explanation', async ({ page }) => {
  await open(page);
  await submit(page, '[failure] Provider rejected planning');
  await complete(page);
  await expect(page.locator('#answer')).toContainText('provider_unavailable');
  await expect(page.locator('#answer')).not.toContainText('CANARY-INTERNAL-SECRET');
  await expect(page.locator('#typing')).toBeHidden();
  await expect(page.locator('#stop')).toBeHidden();
});

test('cancellation reaches backend and cannot later become completion', async ({ page }) => {
  await open(page);
  await submit(page, '[pause:search] Cancel blocked retrieval');
  await expect(page.locator('#events')).toContainText('Searching the web');
  const stopped = page.waitForResponse(response => response.url().endsWith('/stop'));
  await page.locator('#stop').click();
  expect((await stopped).status()).toBe(200);
  await complete(page);
  await expect(page.locator('#answer')).toContainText('Stop reason: cancelled');
  await expect(page.locator('#activity-title')).toHaveText('Research stopped');
});

test('overlapping browser sessions receive bounded busy response then isolate reports', async ({ page, context, request }) => {
  await open(page);
  await submit(page, '[pause:verify] First isolated chat');
  await expect(page.locator('#status')).toContainText('Cross-checking findings');
  const second = await context.newPage();
  await open(second);
  await submit(second, 'Second isolated chat');
  await expect(second.locator('#error')).toContainText('already running');
  await request.post('/__qa__/release');
  await complete(page);
  await submit(second, 'Second isolated chat');
  await complete(second);
  await expect(second.locator('#sent-question')).toHaveText('Second isolated chat');
  await expect(second.locator('#answer')).not.toContainText('First isolated chat');
  await page.reload();
  await expect(page.locator('#chat-list')).toContainText('First isolated chat');
  await page.getByRole('button', { name: /First isolated chat/ }).click();
  await expect(page.locator('#sent-question')).toContainText('First isolated chat');
  await expect(page.locator('#answer')).toContainText('measured value of 42');
  await second.close();
});

test('interrupted response reconnects once to same run without duplicate result', async ({ page, browserDiagnostics }) => {
  await open(page);
  let posts = 0;
  await page.route('**/api/research', async route => {
    posts++;
    if (posts === 1) {
      const response = await route.fetch(); // backend finishes; browser loses the response
      await response.dispose();
      await route.abort('connectionreset');
    } else await route.continue();
  });
  // Expected connection-reset console entry is recorded separately.
  browserDiagnostics.disconnect = true;
  await submit(page, 'Recover original run');
  await complete(page);
  expect(posts).toBe(2);
  await expect(page.locator('#answer h1')).toHaveCount(1);
  await expect(page.locator('#events')).toContainText('Reconnecting');
});

test('input limits, keyboard submission and rapid duplicate submit', async ({ page }) => {
  await open(page);
  await page.locator('#submit').click();
  await expect(page.locator('#sent-message')).toBeHidden();
  await expect(page.locator('#question')).toHaveAttribute('maxlength', '8000');
  await page.locator('#question').fill('Keyboard Русский 한국어 question');
  await page.locator('#question').press('Control+Enter');
  await complete(page);
  await expect(page.locator('#sent-question')).toHaveText('Keyboard Русский 한국어 question');
});

test('axe WCAG AA, labels and keyboard focus on empty and complete UI', async ({ page }, info) => {
  await open(page);
  const audit = async () => {
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
    await info.attach('axe-results', { body: JSON.stringify(results), contentType: 'application/json' });
    expect(results.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) }))).toEqual([]);
  };
  await audit();
  await page.getByLabel('Your research question').focus();
  await expect(page.locator('#question')).toBeFocused();
  await submit(page, 'Accessible research');
  await complete(page);
  await audit();
  await expect(page.locator('#status')).toHaveAttribute('aria-live', 'polite');
});

for (const width of [360, 430, 768, 1280, 1920]) {
  test(`responsive layout has no main overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    await submit(page, `Responsive report ${width}`);
    await complete(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

test('untrusted report content renders as text with safe source links', async ({ page }) => {
  await open(page);
  await submit(page, '<img src=x onerror=alert(1)> [click](javascript:alert(1))');
  await complete(page);
  expect(await page.locator('#sent-question img').count()).toBe(0);
  expect(await page.locator('#answer script, #answer img, #answer a[href^="javascript:"]').count()).toBe(0);
});

test('long report renders completely and remains keyboard accessible', async ({ page }, info) => {
  await open(page);
  const started = Date.now();
  await submit(page, '[long] Compare established multilingual facts');
  await expect(page.locator('#events')).toContainText('Planning');
  const firstUpdateMs = Date.now() - started;
  await complete(page);
  const reportMs = Date.now() - started;
  await expect(page.locator('#answer h2').filter({ hasText: 'Evidence section' })).toHaveCount(3);
  await expect(page.locator('#answer a').filter({ hasText: 'Official publication' })).toHaveCount(24);
  const last = page.locator('#answer a').last();
  await last.scrollIntoViewIfNeeded();
  await last.focus();
  await expect(last).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await info.attach('ui-latency', { body: JSON.stringify({ firstUpdateMs, reportMs, provider: 'scripted' }), contentType: 'application/json' });
});

test('keyboard tab order, visible focus, zoom and reduced motion', async ({ page, browserName }) => {
  await open(page);
  await page.locator('#question').focus();
  await page.keyboard.press(browserName === 'webkit' ? 'Alt+Tab' : 'Tab');
  await expect(page.locator('#submit')).toBeFocused();
  expect(await page.locator('#submit').evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
  await page.setViewportSize({ width: 640, height: 450 }); // equivalent CSS viewport at 200% on 1280px
  await submit(page, 'Zoom and motion');
  await complete(page);
  expect(await page.locator('#answer-section').evaluate(el => getComputedStyle(el).animationName)).toBe('none');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('compact input expands for multiple lines, caps its height and submits with Enter', async ({ page }) => {
  await open(page);
  const input = page.locator('#question');
  const height = () => input.evaluate(el => el.getBoundingClientRect().height);
  expect(await height()).toBeLessThanOrEqual(32);
  await input.fill('First line');
  await input.press('Shift+Enter');
  await input.press('a');
  expect(await height()).toBeGreaterThan(32);
  await expect(page.locator('#sent-message')).toBeHidden();
  await input.fill(Array.from({ length: 20 }, (_, i) => `Line ${i}`).join('\n'));
  expect(await height()).toBeLessThanOrEqual(160);
  expect(await input.evaluate(el => getComputedStyle(el).overflowY)).toBe('auto');
  await input.fill('Single line again');
  expect(await height()).toBeLessThanOrEqual(32);
  await input.press('Enter');
  await complete(page);
  await expect(page.locator('#sent-question')).toHaveText('Single line again');
  expect(await height()).toBeLessThanOrEqual(32);
});

test('temporary search error recovers and final answer streams through the actual HTTP server', async ({ page }) => {
  await open(page);
  await submit(page, '[retry] Recover search');
  await complete(page);
  await expect(page.locator('#answer')).toContainText('Search calls: 16');
  await expect(page.locator('#events')).not.toContainText('CANARY-INTERNAL-SECRET');
  await page.locator('#activity-history summary').click();
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#download-trace').click()]);
  const stream = await download.createReadStream();
  let saved = ''; for await (const chunk of stream) saved += chunk;
  const emitted = JSON.parse(saved);
  expect(emitted.filter(e => e.type === 'answer_delta').length).toBeGreaterThan(1);
  expect(emitted.some(e => e.kind === 'search_recovered')).toBe(true);
  expect(emitted.filter(e => e.type === 'answer_delta').map(e => e.text).join('')).toBe(emitted.find(e => e.type === 'result').report);
});

for (const mode of ['partial-search', 'search-unavailable']) {
  test(`search failures preserve available evidence and show controlled limitations: ${mode}`, async ({ page }) => {
    await open(page);
    await submit(page, `[${mode}] Preserve research`);
    await complete(page);
    await expect(page.locator('#answer')).toContainText('Limitations');
    await expect(page.locator('#answer')).not.toContainText('CANARY-INTERNAL-SECRET');
    await expect(page.locator('#error')).toBeHidden();
    if (mode === 'partial-search') {
      await expect(page.locator('#answer')).toContainText('Source coverage incomplete');
      await expect(page.locator('#answer a')).not.toHaveCount(0);
    } else await expect(page.locator('#answer')).toContainText('No eligible source-backed evidence');
  });
}
