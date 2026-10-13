const { test, expect, open, submit, complete } = require('./helpers.cjs');
test.setTimeout(60000);

test('report supports direct editing, formatting, undo, replay protection and saved restoration', async ({ page, request }) => {
  await open(page);
  await submit(page, 'Editable evidence report');
  await complete(page);
  const editor = page.locator('#answer .tiptap');
  await expect(editor).toHaveAttribute('contenteditable', 'true');
  await expect(page.locator('#document-status')).toContainText('Saved');
  const originalCitation = await editor.locator('a').first().getAttribute('href');
  await expect(editor.locator('a').first()).toHaveAttribute('title', originalCitation);
  expect(await page.evaluate(() => ReportEditor.hasEdits())).toBe(false);
  const chats = await (await request.get('/api/chats')).json();
  const id = chats.chats[0].id;
  expect((await (await request.get(`/api/documents/${id}`)).json()).document).toBeNull();
  await editor.locator('p').first().click();
  await page.keyboard.press('End');
  await page.keyboard.type(' My editorial note.');
  await expect(editor).toContainText('My editorial note.');
  await page.locator('[data-editor-command="undo"]').click();
  await expect(editor).not.toContainText('My editorial note.');
  await page.locator('[data-editor-command="redo"]').click();
  await expect(editor).toContainText('My editorial note.');
  await expect(page.locator('#document-status')).toHaveText('Saved');
  await page.locator('#edit-toggle').click();
  await expect(editor).toHaveAttribute('contenteditable', 'false');
  await page.locator('#edit-toggle').click();
  await expect(editor).toContainText('My editorial note.');
  const original = await (await request.get(`/api/chats/${id}`)).json();
  const result = original.events.find(e => e.type === 'result');
  expect(result.report).not.toContain('My editorial note.');
  await page.evaluate(result => { receiveAnswer({type: 'answer_start'}); showResult(result); }, result);
  await expect(editor).toContainText('My editorial note.');
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer')).toContainText('My editorial note.');
  await expect(page.locator('#answer a').first()).toHaveAttribute('href', originalCitation);
  // Remove the local recovery cache to prove PostgreSQL-compatible API restoration.
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer')).toContainText('My editorial note.');
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#download').click()]);
  const stream = await download.createReadStream();
  let body = ''; for await (const chunk of stream) body += chunk;
  expect(body).toContain('My editorial note.');
  expect(body).toContain(originalCitation);
  await page.context().route('https://example.com/**', route => route.fulfill({ contentType: 'text/html', body: '<p>Offline citation target</p>' }));
  for (const reading of [false, true]) {
    if (reading) await page.locator('#edit-toggle').click();
    if (reading) await page.locator('#answer a').first().focus();
    else await page.locator('#answer a').first().click();
    const [popup] = await Promise.all([page.waitForEvent('popup'), page.keyboard.press(reading ? 'Enter' : 'ControlOrMeta+Enter')]);
    await popup.waitForLoadState();
    expect(popup.url()).toBe(originalCitation);
    await popup.close();
  }
});

test('tables and formatted Markdown round-trip without executing source HTML', async ({ page }) => {
  await open(page);
  await submit(page, 'Formatted evidence report');
  await complete(page);
  const editor = page.locator('#answer .tiptap');
  await editor.click();
  await page.keyboard.press('ControlOrMeta+End');
  await page.keyboard.press('Enter');
  await page.locator('[data-editor-command="table"]').click();
  await expect(editor.locator('table')).toHaveCount(1);
  await editor.locator('th').first().click();
  await page.keyboard.type('Comparison');
  await page.locator('[data-editor-command="row"]').click();
  await expect(editor.locator('tr')).toHaveCount(4);
  await expect(page.locator('#document-status')).toHaveText('Saved');
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer table')).toContainText('Comparison');
  await expect(page.locator('#answer tr')).toHaveCount(4);
});

test('formulas and charts render safely, remain editable and survive export and restoration', async ({ page }) => {
  await open(page);
  await submit(page, 'Media evidence report');
  await complete(page);
  const editor = page.locator('#answer .tiptap');
  for (const [action, source] of [
    ['math', 'E = mc^2'],
    ['chart', JSON.stringify({ title: 'Evidence comparison', values: [{ label: '<img onerror=alert(1)>', value: 42 }, { label: 'B', value: 21 }] })],
    ['chart', '{"title":"Invalid","values":[{"label":"Bad","value":-1}]}'],
  ]) {
    await editor.locator('p').filter({ hasText: 'Research date:' }).click();
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    page.once('dialog', dialog => dialog.accept(source));
    await page.locator(`[data-editor-command="${action}"]`).click();
  }
  await expect(editor.locator('math')).toHaveCount(1);
  await expect(editor.locator('svg[aria-label="Evidence comparison"] rect')).toHaveCount(2);
  await expect(editor).toContainText('Preview unavailable');
  await expect(editor.locator('img, script')).toHaveCount(0);
  await expect(page.locator('#document-status')).toHaveText('Saved');
  const markdown = await page.evaluate(() => ReportEditor.markdown());
  expect(markdown).toContain('```math');
  expect(markdown).toContain('```chart');
  expect(markdown).toContain('E = mc^2');
  await page.locator('#edit-toggle').click();
  await expect(editor.locator('.report-media pre').first()).toBeHidden();
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer math')).toHaveCount(1);
  await expect(page.locator('#answer svg rect')).toHaveCount(2);
});

test('streamed synthesis stays read-only and cancellation retains an explicitly incomplete report', async ({ page }) => {
  await open(page);
  await submit(page, '[pause:preview] Stream complete cited paragraphs');
  const editor = page.locator('#answer .tiptap');
  await expect(editor).toContainText('42');
  await expect(editor).toHaveAttribute('contenteditable', 'false');
  await expect(page.locator('#document-completeness')).toContainText('incomplete');
  await expect(editor.locator('a').first()).toHaveAttribute('href', /^https:/);
  await page.locator('#stop').click();
  await complete(page);
  await expect(page.locator('#document-completeness')).toContainText('Incomplete research');
  await expect(page.locator('#answer')).toContainText('42');
  await expect(page.locator('#answer .tiptap')).toHaveAttribute('contenteditable', 'true');
});

test('interrupted synthesis transitions to an editable incomplete report', async ({ page }) => {
  await open(page);
  await submit(page, '[interrupted] Preserve complete report content');
  await complete(page);
  await expect(page.locator('#document-completeness')).toContainText('Incomplete research');
  await expect(page.locator('#answer')).toContainText('42');
  await expect(page.locator('#answer .tiptap')).toHaveAttribute('contenteditable', 'true');
});

test('stale tab writes preserve both the server document and the local conflicting copy', async ({ page, request, browserDiagnostics }) => {
  await open(page);
  await submit(page, 'Conflicting evidence report');
  await complete(page);
  const id = (await (await request.get('/api/chats')).json()).chats[0].id;
  browserDiagnostics.documentErrors.add(`${new URL(page.url()).origin}/api/documents/${id}`);
  const other = await page.context().newPage();
  await open(other);
  await other.locator('#chat-list button').first().click();
  await expect(other.locator('#document-status')).toContainText('Saved');
  await page.locator('#answer p').first().click();
  await page.keyboard.press('End');
  await page.keyboard.type(' First tab revision.');
  await expect(page.locator('#document-status')).toHaveText('Saved');
  await other.locator('#answer p').first().click();
  await other.keyboard.press('End');
  await other.keyboard.type(' Conflicting second copy.');
  await expect(other.locator('#document-status')).toContainText('Newer edits');
  await other.keyboard.type(' Further local revision.');
  await expect(other.locator('#document-status')).toContainText('Newer edits');
  const remote = (await (await request.get(`/api/documents/${id}`)).json()).document;
  expect(remote.markdown).toContain('First tab revision');
  expect(remote.markdown).not.toContain('Conflicting second copy');
  expect(await other.evaluate(() => ReportEditor.markdown())).toContain('Conflicting second copy');
  await other.reload();
  await other.locator('#chat-list button').first().click();
  await expect(other.locator('#answer')).toContainText('Conflicting second copy');
  await expect(other.locator('#document-status')).toContainText('Newer edits');
});

test('edits during an in-flight save remain dirty even with the same clock timestamp', async ({ page, request }) => {
  await open(page);
  await submit(page, 'In-flight editorial save');
  await complete(page);
  const id = (await (await request.get('/api/chats')).json()).chats[0].id;
  const url = `${new URL(page.url()).origin}/api/documents/${id}`;
  let release, notify, first = true;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { notify = resolve; });
  await page.route(url, async route => {
    if (route.request().method() === 'POST' && first) {
      first = false; notify(); await gate;
    }
    await route.continue();
  });
  // Wall-clock equality must never be used to decide whether content changed.
  await page.evaluate(() => { const fixed = Date.now(); Date.now = () => fixed; });
  try {
    await page.locator('#answer p').first().click();
    await page.keyboard.press('End');
    await page.keyboard.type(' First saved edit.');
    await started;
    await page.keyboard.type(' Second pending edit.');
    release();
    await expect(page.locator('#document-status')).toHaveText('Saved');
    const remote = (await (await request.get(`/api/documents/${id}`)).json()).document;
    expect(remote.markdown).toContain('First saved edit. Second pending edit.');
    expect(remote.revision).toBe(2);
  } finally { release(); }
});

test('server save outage restores local edits and retries when connectivity returns', async ({ page, request, browserDiagnostics }) => {
  await open(page);
  await submit(page, 'Offline editorial recovery');
  await complete(page);
  const id = (await (await request.get('/api/chats')).json()).chats[0].id;
  const url = `${new URL(page.url()).origin}/api/documents/${id}`;
  browserDiagnostics.documentErrors.add(url);
  const unavailable = route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, contentType: 'application/json', body: '{}' }) : route.continue();
  await page.route(url, unavailable);
  await page.locator('#answer p').first().click();
  await page.keyboard.press('End');
  await page.keyboard.type(' Locally recovered note.');
  await expect(page.locator('#document-status')).toContainText('server save unavailable');
  await page.reload();
  await page.locator('#chat-list button').first().click();
  await expect(page.locator('#answer')).toContainText('Locally recovered note');
  await expect(page.locator('#document-status')).toContainText('server save unavailable');
  await page.unroute(url, unavailable);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect(page.locator('#document-status')).toHaveText('Saved');
  expect((await (await request.get(`/api/documents/${id}`)).json()).document.markdown).toContain('Locally recovered note');
});
