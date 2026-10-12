const { test: base, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const nativeSelectLines = new Set(fs.readFileSync(path.join(__dirname, '../../src/deepsearchagent/web/index.html'), 'utf8')
  .split('\n').flatMap((line, i) => line.includes('<select') ? [i + 1] : []));
const nativeCspMessage = "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline' does not appear in the style-src directive of the Content Security Policy.";

const test = base.extend({
  browserDiagnostics: [async ({ context, request, browserName }, use, info) => {
    await expect.poll(async () => (await request.post('/__qa__/reset')).status()).toBe(200);
    const errors = [], consoleErrors = [], failed = [], cancelled = [], csp = [], expectedBusy = new Set();
    await context.exposeBinding('__qaCsp', (source, record) => csp.push({ ...record, url: source.page.url() }));
    await context.exposeBinding('__qaStreamCancelled', (source, record) => cancelled.push(record));
    await context.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', e => window.__qaCsp({
        source: e.sourceFile, line: e.lineNumber, blocked: e.blockedURI, directive: e.effectiveDirective,
      }));
      const original = window.fetch;
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (!new URL(response.url).pathname.startsWith('/api/research') || !response.body) return response;
        const url = response.url;
        const runId = args[1]?.body ? JSON.parse(args[1].body).run_id : new URL(url).pathname.split('/')[3];
        const getReader = response.body.getReader.bind(response.body);
        response.body.getReader = (...options) => {
          const reader = getReader(...options), read = reader.read.bind(reader), cancel = reader.cancel.bind(reader);
          const decoder = new TextDecoder();
          let buffer = '', terminal = false;
          reader.read = async (...values) => {
            const chunk = await read(...values);
            buffer += decoder.decode(chunk.value, { stream: !chunk.done });
            const lines = buffer.split('\n'); buffer = lines.pop();
            if (chunk.done && buffer.trim()) lines.push(buffer);
            for (const line of lines) {
              try { const data = JSON.parse(line); terminal ||= data.type === 'result' || data.type === 'error'; } catch (_) {}
            }
            return chunk;
          };
          reader.cancel = async (...values) => {
            // Record only the application's deliberate cancellation after a
            // terminal NDJSON record was actually read on this exact request.
            if (terminal) await window.__qaStreamCancelled({ url, runId });
            return cancel(...values);
          };
          return reader;
        };
        return response;
      };
    });
    context.on('page', page => {
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => {
        if (message.type() === 'error') consoleErrors.push({ text: message.text(), url: message.location().url });
      });
      page.on('response', response => {
        if (response.status() === 409 && response.url().endsWith('/api/research')) expectedBusy.add(response.url());
      });
      page.on('requestfailed', req => {
        let runId;
        try { runId = req.method() === 'POST' ? req.postDataJSON()?.run_id : new URL(req.url()).pathname.split('/')[3]; } catch (_) {}
        failed.push({ url: req.url(), runId, error: req.failure()?.errorText });
      });
    });
    const expected = { disconnect: false };
    await use(expected);
    await request.post('/__qa__/release');
    await info.attach('browser-diagnostics', { body: JSON.stringify({ errors, consoleErrors, failed, cancelled, csp }), contentType: 'application/json' });
    expect(errors).toEqual([]);
    const isResearch = url => new URL(url).pathname.startsWith('/api/research');
    expect(failed.filter(r => {
      if (expected.disconnect && isResearch(r.url) && /ERR_CONNECTION_RESET|NS_ERROR_NET_RESET|Blocked by Web Inspector/i.test(r.error)) return false;
      return !(/ERR_ABORTED|NS_BINDING_ABORTED|cancelled/i.test(r.error) && cancelled.some(c => c.url === r.url && c.runId === r.runId));
    })).toEqual([]);
    // Reproduced independently on a script-free native select. Match only
    // WebKit's exact message AND a violation at a select's actual source line.
    const nativeWarnings = csp.filter(r => browserName === 'webkit' && r.source === '' && r.blocked === 'inline'
      && r.directive.startsWith('style-src') && nativeSelectLines.has(r.line) && new URL(r.url).pathname === '/');
    expect(consoleErrors.filter(r => {
      const index = nativeWarnings.findIndex(v => v.url === r.url);
      if (r.text === nativeCspMessage && index >= 0) { nativeWarnings.splice(index, 1); return false; }
      return !(
      expectedBusy.has(r.url) && /409/.test(r.text)
      ) && !(expected.disconnect && isResearch(r.url) && /ERR_CONNECTION_RESET|network.*(lost|reset)|Web Inspector/i.test(r.text));
    })).toEqual([]);
  }, { auto: true }],
});

async function open(page) {
  await page.goto('/');
  await expect(page.locator('#submit')).toBeEnabled();
  await expect(page.locator('#submit')).toBeEnabled();
}
async function submit(page, question) {
  await page.locator('#question').fill(question);
  await page.locator('#submit').click();
}
async function complete(page) {
  await expect(page.locator('#answer-section')).toBeVisible();
  await expect(page.locator('#submit')).toBeEnabled();
}
module.exports = { test, expect, open, submit, complete };
