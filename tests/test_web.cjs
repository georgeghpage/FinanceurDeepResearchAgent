"use strict";

// Execute the actual browser script with a small DOM adapter and real web streams.
// No browser, network provider, or npm dependency is required.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const assets = resolve(__dirname, "../src/deepsearchagent/web");
const script = readFileSync(resolve(assets, "app.js"), "utf8");
const dashboardScript = readFileSync(resolve(assets, "dashboard.js"), "utf8");
const html = readFileSync(resolve(assets, "index.html"), "utf8");
const encoder = new TextEncoder();

class Element {
  constructor(tag, text = "") {
    this.tagName = tag;
    this.children = [];
    this._text = text;
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.style = {};
    this.dataset = {};
    this.scrollTop = 0;
    this.clientHeight = 560;
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set textContent(text) { this._text = String(text); this.children = []; }
  get scrollHeight() { return this.textContent.length; }
  append(...children) {
    for (const child of children) {
      if (child.tagName === "#fragment") this.children.push(...child.children);
      else this.children.push(typeof child === "string" ? new Element("#text", child) : child);
    }
  }
  replaceChildren(...children) { this._text = ""; this.children = []; this.append(...children); }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  click() { return this.listeners.get("click")?.({ preventDefault() {} }); }
}

function walk(element) { return [element, ...element.children.flatMap(walk)]; }

function harness(fetchImpl, historyImpl = async () => new Response(JSON.stringify({ chats: [], has_more: false })),
                 modelsImpl = async path => new Response(JSON.stringify(path.includes("provider=kimi")
                   ? { provider: "kimi", models: ["kimi-k3", "kimi-k2.6"] }
                   : { provider: "aliyun", models: ["qwen-plus"] })),
                 statusImpl = null) {
  const nodes = Object.fromEntries([...html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)]
    .map(match => [match[2], new Element(match[1])]));
  nodes.question.value = "Find revenue";
  const downloads = [];
  const blobs = [];
  const timers = new Set();
  class TestURL extends URL {
    static createObjectURL(blob) { blobs.push(blob); return `blob:test-${blobs.length}`; }
    static revokeObjectURL() {}
  }
  const document = {
    getElementById: id => nodes[id],
    createTextNode: text => new Element("#text", text),
    createDocumentFragment: () => new Element("#fragment"),
    createElement(tag) {
      const element = new Element(tag);
      if (tag === "a") element.click = () => downloads.push(element);
      return element;
    },
  };
  const context = vm.createContext({
    document, fetch: (url, options) => url === "/api/status"
      ? statusImpl ? statusImpl(url, options) : Promise.resolve(new Response(JSON.stringify({ configured: true, provider: "aliyun", model: "deepseek-v4.1-flash" })))
      : url.startsWith("/api/models")
      ? modelsImpl(url, options)
      : url.startsWith("/api/chats")
      ? historyImpl(url, options)
      : fetchImpl(url, options), TextDecoder, Blob, URL: TestURL, crypto: webcrypto,
    location: { origin: "http://127.0.0.1:8000" },
    setTimeout: callback => { queueMicrotask(callback); return 0; },
    setInterval: callback => { timers.add(callback); return callback; },
    clearInterval: callback => timers.delete(callback),
  });
  vm.runInContext(dashboardScript, context, { filename: "dashboard.js" });
  vm.runInContext(script, context, { filename: "app.js" });
  if (!statusImpl) vm.runInContext("configurationReady = true; updateProvider();", context);
  return {
    nodes, context, downloads, blobs, timers,
    submit: () => nodes["research-form"].listeners.get("submit")({ preventDefault() {} }),
  };
}

const events = [
  { type: "progress", message: "Planning research" },
  { type: "trace", kind: "llm_start", call_id: 1, schema: "ResearchPlan", model: "offline" },
  { type: "trace", kind: "thinking_delta", call_id: 1, text: "Checking 收入 🌱 <script>alert(1)</script>" },
  { type: "trace", kind: "output_delta", call_id: 1, text: '{"objective":"Revenue",' },
  { type: "trace", kind: "output_delta", call_id: 1, text: '"tasks":[]}' },
  { type: "trace", kind: "llm_complete", call_id: 1, duration_seconds: 0.5, tokens: 25 },
  { type: "trace", kind: "plan", objective: "Revenue", tasks: [{ question: "Find revenue", source: "web", query: "company revenue" }] },
  { type: "trace", kind: "search_start", question: "Find revenue", source: "web", round: 1, query: "company revenue" },
  { type: "trace", kind: "search_complete", sources: [
    { id: "S1", title: "Filing", url: "https://example.com/filing" },
    { id: "S2", title: "Unsafe", url: "javascript:alert(1)" },
  ] },
  { type: "trace", kind: "findings", findings: [{ id: "F1", source_id: "S1", claim: "Revenue grew." }] },
  { type: "trace", kind: "verification", sufficient: false, supported_finding_ids: ["F1"], gaps: ["Check costs"], conflicts: [], follow_up_tasks: [{ question: "Find costs" }] },
  { type: "result", stop_reason: "coverage", report: "# Revenue\n\nRevenue grew. [S1: Filing](<https://example.com/filing>)", sources: 1, search_calls: 1, api_calls: 1, tokens_used: 25 },
].map((event, index) => ({ ...event, sequence: index + 1, elapsed_seconds: index / 10 }));

function wire(data = events, ending = "\n") { return data.map(event => JSON.stringify(event)).join("\n") + ending; }
function response(text = wire()) { return new Response(text, { headers: { "Content-Type": "application/x-ndjson" } }); }
function interrupted(text) {
  let sent = false;
  return { ok: true, body: { getReader: () => ({
    async read() {
      if (sent) throw new TypeError("Failed to fetch");
      sent = true;
      return { value: encoder.encode(text), done: false };
    },
    async cancel() {}, releaseLock() {},
  }) } };
}
function assertReady(ui) {
  assert.equal(ui.nodes["answer-section"].hidden, false);
  assert.equal(ui.nodes["activity-title"].textContent, "Answer ready");
  assert.equal(ui.nodes.error.hidden, true);
  assert.match(ui.nodes.answer.textContent, /Revenue/);
  assert.equal(ui.nodes.submit.disabled, false);
  assert.equal(ui.nodes.question.readOnly, false);
  assert.equal(ui.timers.size, 0);
}

async function until(predicate) {
  for (let i = 0; i < 50; i++) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail("Expected streamed DOM update did not arrive");
}

test("internal thinking and JSON stay out of the UI while compact progress updates", async () => {
  let controller;
  const stream = new ReadableStream({ start(value) { controller = value; } });
  const ui = harness(async () => new Response(stream));
  const pending = ui.submit();
  assert.equal(ui.nodes["sent-message"].hidden, false);
  assert.equal(ui.nodes["sent-message"].className, "sent-message message-enter");
  assert.equal(ui.nodes["sent-question"].textContent, "Find revenue");
  assert.equal(ui.nodes.typing.hidden, true);
  controller.enqueue(encoder.encode(wire(events.slice(0, 4))));
  await until(() => ui.nodes.events.textContent.includes("Planning research"));
  assert.equal(ui.nodes["answer-section"].hidden, true);
  assert.doesNotMatch(ui.nodes.events.textContent, /Thinking:|Checking 收入|objective/);
  assert.equal(ui.nodes.events.children.length, 1);
  controller.enqueue(encoder.encode(wire(events.slice(4))));
  controller.close();
  await pending;
  assertReady(ui);
  assert.equal(ui.nodes.typing.hidden, true);
});

test("saved chat opens its question, trace, report and downloads without starting research", async () => {
  const chat = { id: "saved-chat", question: "Saved <script>question</script>", status: "completed",
    created_at: "2026-10-12T03:00:00Z", events };
  let modelRequests = 0;
  const ui = harness(async () => { modelRequests++; return response(); }, async url =>
    new Response(JSON.stringify(url.startsWith("/api/chats?") ? { chats: [chat], has_more: false } : chat)));
  await until(() => ui.nodes["chat-list"].children.length === 1);
  await ui.nodes["chat-list"].children[0].click();
  assert.equal(modelRequests, 0);
  assert.equal(ui.nodes["sent-question"].textContent, chat.question);
  assertReady(ui);
  assert.equal(ui.nodes["chat-list"].children[0]["aria-current"], "true");
  assert.equal(walk(ui.nodes["sent-message"]).filter(node => node.tagName === "script").length, 0);
  ui.nodes.download.click();
  assert.equal(await ui.blobs[0].text(), events.at(-1).report);
  ui.nodes["new-chat"].click();
  assert.equal(ui.nodes["sent-message"].hidden, true);
  assert.equal(ui.nodes["answer-section"].hidden, true);
  assert.equal(ui.nodes.intro.hidden, false);
});

test("older chats load on demand and history failures leave research usable", async () => {
  const offsets = [];
  const chat = { id: "old", question: "An older chat", status: "partial", created_at: "2026-10-11T03:00:00Z" };
  const ui = harness(async () => response(), async url => {
    offsets.push(url);
    return new Response(JSON.stringify({ chats: [chat], has_more: offsets.length === 1 }));
  });
  await until(() => ui.nodes["chat-list"].children.length === 1);
  assert.equal(ui.nodes["load-more"].hidden, false);
  await ui.nodes["load-more"].click();
  assert.equal(offsets[1], "/api/chats?offset=1");
  assert.equal(ui.nodes["chat-list"].children.length, 2);
  assert.equal(ui.nodes["load-more"].hidden, true);
  const unavailable = harness(async () => response(), async () => new Response(
    JSON.stringify({ error: "PostgreSQL unavailable" }), { status: 503 }));
  await until(() => unavailable.nodes["history-status"].textContent.includes("unavailable"));
  await unavailable.submit();
  assertReady(unavailable);
});

test("database persistence warnings remain visible after a successful report", async () => {
  const warning = "Some chat history could not be saved. Download this report.";
  const ui = harness(async () => response(wire([{ ...events.at(-1), history_error: warning }])));
  await ui.submit();
  assertReady(ui);
  assert.equal(ui.nodes["history-status"].textContent, warning);
});

test("byte-fragmented UTF-8, blank lines, CRLF, and a final line without newline", async () => {
  const bytes = encoder.encode("\r\n" + wire(events, "").replaceAll("\n", "\r\n"));
  let index = 0;
  const ui = harness(async () => new Response(new ReadableStream({
    pull(controller) {
      if (index === bytes.length) controller.close();
      else controller.enqueue(bytes.slice(index, ++index));
    },
  })));
  await ui.submit();
  assertReady(ui);
  assert.doesNotMatch(ui.nodes.events.textContent, /收入 🌱/);
  ui.nodes["download-trace"].click();
  assert.match(await ui.blobs[0].text(), /收入 🌱/);
  assert.doesNotMatch(ui.nodes.events.textContent, /�/);
});

test("trace details, safe links, report citations, and both downloads", async () => {
  const ui = harness(async () => response());
  await ui.submit();
  assertReady(ui);
  const text = ui.nodes.events.textContent;
  for (const expected of ["Planning research", "Searching the web", "Cross-checking findings"]) assert.ok(text.includes(expected), expected);
  assert.doesNotMatch(text, /company revenue|Revenue grew|Check costs|Thinking:|objective/);
  assert.equal(walk(ui.nodes.events).filter(node => node.tagName === "script").length, 0);
  assert.deepEqual(walk(ui.nodes.events).filter(node => node.tagName === "a"), []);
  assert.equal(walk(ui.nodes.answer).find(node => node.tagName === "a").href, "https://example.com/filing");
  ui.nodes["download-trace"].click();
  ui.nodes.download.click();
  assert.deepEqual(ui.downloads.map(link => link.download), ["research-trace.json", "research-report.md"]);
  assert.deepEqual(JSON.parse(await ui.blobs[0].text()), events);
  assert.equal(await ui.blobs[1].text(), events.at(-1).report);
});

test("Failed to fetch mid-stream resumes by cursor without duplicate trace text", async () => {
  const requests = [];
  const ui = harness(async (url, options) => {
    requests.push({ url, options });
    return requests.length === 1 ? interrupted(wire(events.slice(0, 4))) : response(wire(events.slice(3)));
  });
  await ui.submit();
  assertReady(ui);
  assert.equal(requests.length, 2);
  const runId = JSON.parse(requests[0].options.body).run_id;
  assert.equal(requests[1].url, `/api/research/${runId}?after=4`);
  assert.equal(ui.nodes.events.textContent.split("Planning research").length - 1, 1);
  assert.equal(ui.nodes.events.textContent.split("Model call 1").length - 1, 0);
  assert.match(ui.nodes.events.textContent, /Reconnecting \(1\/3\)/);
});

test("failure before response retries POST with the same run ID", async () => {
  const requests = [];
  const ui = harness(async (url, options) => {
    requests.push(JSON.parse(options.body));
    if (requests.length === 1) throw new TypeError("Failed to fetch");
    return response();
  });
  await ui.submit();
  assertReady(ui);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].run_id, requests[1].run_id);
});

test("a socket error after the result cannot replace a ready answer", async () => {
  const ui = harness(async () => interrupted(wire()));
  await ui.submit();
  assertReady(ui);
  assert.doesNotMatch(ui.nodes.events.textContent, /Reconnecting/);
});

test("clean EOF without a result also reconnects to finish the run", async () => {
  let calls = 0;
  const ui = harness(async () => ++calls === 1 ? response(wire(events.slice(0, 4))) : response(wire(events.slice(4))));
  await ui.submit();
  assertReady(ui);
  assert.equal(calls, 2);
});

test("exhausted retries show an actionable connection error and retain trace", async () => {
  let calls = 0;
  const ui = harness(async () => {
    calls++;
    if (calls === 1) return interrupted(wire(events.slice(0, 4)));
    throw new TypeError("Failed to fetch");
  });
  await ui.submit();
  assert.equal(calls, 4);
  assert.equal(ui.nodes.error.hidden, false);
  assert.match(ui.nodes.error.textContent, /http:\/\/127\.0\.0\.1:8000/);
  assert.match(ui.nodes.error.textContent, /Check that the server is running/);
  assert.doesNotMatch(ui.nodes.error.textContent, /Failed to fetch/);
  assert.match(ui.nodes.events.textContent, /Planning research/);
  assert.doesNotMatch(ui.nodes.events.textContent, /Checking 收入|Partial output retained/);
  assert.equal(ui.nodes.submit.disabled, false);
  assert.equal(ui.nodes.submit.textContent, "Reconnect research ↗");
  assert.equal(ui.timers.size, 0);
  ui.nodes["download-trace"].click();
  assert.equal(JSON.parse(await ui.blobs[0].text()).filter(event => event.type !== "connection").length, 4);
});

test("manual reconnect after exhausted retries resumes the retained run and trace", async () => {
  let calls = 0;
  let connected = false;
  const requests = [];
  const ui = harness(async (url, options) => {
    requests.push({ url, options });
    calls++;
    if (connected) return response(wire(events.slice(4)));
    if (calls === 1) return interrupted(wire(events.slice(0, 4)));
    throw new TypeError("Failed to fetch");
  });
  await ui.submit();
  assert.equal(ui.nodes.submit.textContent, "Reconnect research ↗");
  connected = true;
  await ui.submit();
  assertReady(ui);
  const runId = JSON.parse(requests[0].options.body).run_id;
  assert.equal(requests.at(-1).url, `/api/research/${runId}?after=4`);
  assert.equal(ui.nodes.events.textContent.split("Planning research").length - 1, 1);
  assert.equal(ui.nodes.submit.textContent, "Start research ↗");
});

test("provider errors and HTTP configuration failures are not retried", async () => {
  for (const result of [response(wire([{ type: "error", message: "Provider stopped" }])),
    new Response(JSON.stringify({ error: "Missing KIMI_API_KEY" }), { status: 400 })]) {
    let calls = 0;
    const ui = harness(async () => { calls++; return result; });
    await ui.submit();
    assert.equal(calls, 1);
    assert.equal(ui.nodes.error.hidden, false);
    assert.match(ui.nodes.error.textContent, /Provider stopped|Missing KIMI_API_KEY/);
    assert.equal(ui.nodes.submit.disabled, false);
  }
});

test("malformed events stop safely; heartbeats do not clutter the trace", async () => {
  const ui = harness(async () => response('{"type":"heartbeat"}\n' + wire(events.slice(0, 4)) + '{broken}\n'));
  await ui.submit();
  assert.match(ui.nodes.error.textContent, /invalid research event/);
  assert.match(ui.nodes.events.textContent, /Planning research/);
  ui.nodes["download-trace"].click();
  assert.equal(JSON.parse(await ui.blobs[0].text()).length, 4);
});

test("partial reports stay downloadable and a new run resets the old trace", async () => {
  let calls = 0;
  const partial = { ...events.at(-1), stop_reason: "error", report: "# Partial report\n\n## Limitations\n\nProvider stopped." };
  const ui = harness(async () => ++calls === 1 ? response(wire([...events.slice(0, 4), partial])) : response(wire([events.at(-1)])));
  await ui.submit();
  assert.equal(ui.nodes["activity-title"].textContent, "Partial answer ready");
  assert.match(ui.nodes.answer.textContent, /Provider stopped/);
  ui.nodes.download.click();
  assert.equal(await ui.blobs[0].text(), partial.report);
  ui.nodes.question.value = "Find revenue";
  await ui.submit();
  assertReady(ui);
  assert.equal(ui.nodes.events.children.length, 0);
});

test("provider and stage explain partial results and survive trace download", async () => {
  for (const failure of [
    { provider: "tavily", stage: "search", operation: "initialize", category: "authentication", http_status: 401, message: "Provider rejected authentication." },
    { provider: "kimi", stage: "verify", operation: "chat_completion", category: "output_limit", call_id: 3, finish_reason: "length", message: "Kimi reached the output token limit." },
  ]) {
    let calls = 0;
    const result = { ...events.at(-1), stop_reason: "error", failure };
    const stopped = { type: "trace", kind: "run_stopped", failure, sequence: result.sequence - 1 };
    const ui = harness(async () => { calls++; return response(wire([stopped, result])); });
    await ui.submit();
    assert.equal(calls, 1);
    assert.equal(ui.nodes["answer-section"].hidden, false);
    assert.equal(ui.nodes.error.hidden, true);
    assert.ok(ui.nodes["dashboard-issue-list"].textContent.includes(failure.message));
    assert.doesNotMatch(ui.nodes.events.textContent, /authentication|output_limit|HTTP/);
    ui.nodes["download-trace"].click();
    assert.deepEqual(JSON.parse(await ui.blobs[0].text()).at(-1).failure, failure);
    ui.nodes.download.click();
    assert.equal(await ui.blobs[1].text(), result.report);
  }
});

test("model failure shows output and thinking counts without claiming unknown token usage", async () => {
  const failure = { provider: "kimi", stage: "verify", category: "output_limit", call_id: 3, finish_reason: "length", message: "Output token limit reached." };
  const ui = harness(async () => response(wire([
    { type: "trace", kind: "llm_start", call_id: 3, schema: "Verification", model: "offline", timeout_seconds: 120, max_output_tokens: 4000 },
    { type: "trace", kind: "thinking_delta", call_id: 3, text: "Checking" },
    { type: "trace", kind: "llm_error", call_id: 3, diagnostic: failure, duration_seconds: 12, output_chars: 0, reasoning_chars: 8, usage_observed: false },
    { ...events.at(-1), failure, stop_reason: "error" },
  ])));
  await ui.submit();
  assert.match(ui.nodes.events.textContent, /Cross-checking findings/);
  assert.doesNotMatch(ui.nodes.events.textContent, /output_limit|Thinking:|thinking characters|Output limit/);
  ui.nodes["download-trace"].click();
  const saved = JSON.parse(await ui.blobs[0].text());
  assert.equal(saved.find(e => e.kind === "llm_error").reasoning_chars, 8);
});

test("real HTTP fetch reconnects to the same offline research run", { skip: !process.env.RESEARCH_TEST_URL }, async () => {
  const base = process.env.RESEARCH_TEST_URL;
  const requests = [];
  const ui = harness(async (path, options) => {
    requests.push({ path, options });
    const result = await fetch(new URL(path, base), options);
    if (requests.length !== 1) return result;
    const reader = result.body.getReader();
    let sent = false;
    return { ok: result.ok, status: result.status, json: () => result.json(), body: { getReader: () => ({
      async read() {
        if (sent) { await reader.cancel(); throw new TypeError("Failed to fetch"); }
        sent = true;
        return reader.read();
      },
      cancel: () => reader.cancel(), releaseLock: () => reader.releaseLock(),
    }) } };
  });
  await ui.submit();
  assertReady(ui);
  assert.equal(requests.length, 2);
  assert.match(requests[1].path, /\?after=\d+$/);
  assert.match(ui.nodes.status.textContent, /4 model calls · 400 tokens/);
  assert.equal(walk(ui.nodes.events).filter(node => node.className === "thinking").length, 0);
  ui.nodes["download-trace"].click();
  const trace = JSON.parse(await ui.blobs[0].text()).filter(event => event.sequence);
  assert.deepEqual(trace.map(event => event.sequence), Array.from({ length: trace.length }, (_, i) => i + 1));
  assert.equal(trace.at(-1).type, "result");
});

const { TraceDashboardState } = require(resolve(assets, "dashboard.js"));
const rateCard = { model: "kimi-k3", input_price_per_million: 3, output_price_per_million: 15,
  cached_price_per_million: .3, cache_write_price_per_million: 3, tavily_price_per_search: .016 };
function measuredRun() {
  return [
    { sequence: 1, kind: "run_start", pricing: rateCard, elapsed_seconds: 0 },
    { sequence: 2, kind: "llm_start", call_id: 1, stage: "plan", model: "kimi-k3", elapsed_seconds: 1 },
    { sequence: 3, kind: "llm_usage", call_id: 1, usage_observed: true, tokens: 1200, input_tokens: 1000,
      output_tokens: 200, cached_tokens: 600, cache_write_tokens: 100, reasoning_tokens: 150, elapsed_seconds: 3 },
    { sequence: 4, kind: "llm_complete", call_id: 1, usage_observed: true, tokens: 1200, input_tokens: 1000,
      output_tokens: 200, cached_tokens: 600, cache_write_tokens: 100, reasoning_tokens: 150, duration_seconds: 2, elapsed_seconds: 3 },
    { sequence: 5, kind: "search_start", search_id: 1, provider: "tavily", query: "revenue", elapsed_seconds: 4 },
    { sequence: 6, kind: "search_complete", search_id: 1, provider: "tavily", duration_seconds: 2,
      source_count: 2, elapsed_seconds: 6 },
    { sequence: 7, type: "result", stop_reason: "coverage", tokens_used: 1200, elapsed_seconds: 7 },
  ];
}

test("dashboard cache pricing, reasoning inclusion, replay and usage snapshots never double-count", () => {
  const state = new TraceDashboardState();
  measuredRun().forEach(event => state.receive(event));
  measuredRun().forEach(event => state.receive(event));
  const totals = state.totals();
  assert.equal(totals.tokens, 1200); assert.equal(totals.models, 1); assert.equal(totals.tools, 1);
  assert.equal(totals.input, 1000); assert.equal(totals.output, 200);
  assert.ok(Math.abs(totals.cost - .02038) < 1e-10);
  assert.equal(totals.unpriced, 0); assert.equal(state.status, "Complete");
  assert.equal(state.duration(state.spans.get("tool-1")), 2);
});

test("dashboard distinguishes missing usage, unknown model rates and failed tool billing", () => {
  const state = new TraceDashboardState();
  state.receive({ kind: "run_start", pricing: rateCard });
  state.receive({ kind: "llm_start", call_id: 1, model: "kimi-k3", elapsed_seconds: 0 });
  state.receive({ kind: "llm_error", call_id: 1, tokens: 0, usage_observed: false, duration_seconds: 4, elapsed_seconds: 4 });
  state.receive({ kind: "search_start", search_id: 1, provider: "tavily", elapsed_seconds: 4 });
  state.receive({ kind: "search_error", search_id: 1, duration_seconds: 3, elapsed_seconds: 7 });
  assert.equal(state.totals().cost, null); assert.equal(state.totals().errors, 2);
  assert.equal(state.spans.get("model-1").tokens, null);
  state.receive({ kind: "llm_start", call_id: 2, model: "custom-model", elapsed_seconds: 7 });
  state.receive({ kind: "llm_complete", call_id: 2, tokens: 10, usage_observed: true, input_tokens: 8, output_tokens: 2 });
  assert.equal(state.cost(state.spans.get("model-2")), null);
  state.reset(); assert.equal(state.totals().calls, 0); assert.equal(state.pricing, null);
});

test("dashboard renders live calls, filtering and inspector from the real stream", async () => {
  const data = measuredRun().map(e => ({ ...e, type: e.type || "trace", sources: [], report: "# Done", api_calls: 1, search_calls: 1 }));
  const ui = harness(async () => response(wire(data)));
  await ui.submit();
  assert.equal(ui.nodes["dashboard-cost"].textContent, "$0.0204");
  assert.equal(ui.nodes["dashboard-tokens"].textContent, "1,200");
  assert.equal(ui.nodes["dashboard-rows"].children.length, 2);
  const callButton = walk(ui.nodes["dashboard-rows"]).find(n => n.tagName === "button");
  await callButton.click();
  assert.equal(ui.nodes["dashboard-detail"].hidden, false);
  assert.match(ui.nodes["dashboard-detail-values"].textContent, /600 \/ 100/);
  ui.nodes["dashboard-filter"].listeners.get("change")({ target: { value: "tool" } });
  assert.equal(ui.nodes["dashboard-rows"].children.length, 1);
  await ui.nodes["new-chat"].click();
  assert.equal(ui.nodes["dashboard-tokens"].textContent, "0");
});

test("research stages, query names and failure causes are visible without opening a call", async () => {
  const failure = { provider: "tavily", stage: "search", operation: "initialize", category: "quota_exhausted",
    origin: "downstream", downstream_effects: ["search / search"],
    http_status: 432, message: "Provider credits or quota are exhausted." };
  const data = [
    { kind: "llm_start", stage: "plan", schema: "PlanningBrief", call_id: 1 },
    { kind: "llm_complete", call_id: 1 },
    { kind: "llm_start", stage: "plan", schema: "QueryBatch", call_id: 2 },
    { kind: "llm_complete", call_id: 2 },
    { kind: "search_start", search_id: 13, query_id: "Q7", provider: "tavily", query: "company revenue <script>" },
    { kind: "search_error", search_id: 13, query_id: "Q7", diagnostic: failure },
    { kind: "search_degraded", search_id: 13, query_id: "Q7", diagnostic: failure, fallback_required: true },
    { type: "result", status: "PARTIAL", stop_reason: "round_limit", report: "# Partial", sources: 0,
      stop_explanation: "The research round limit was reached before all evidence requirements were covered.",
      warnings: ["Revenue remains unsupported."], issues: [] },
  ].map((e, i) => ({ ...e, type: e.type || "trace", sequence: i + 1 }));
  const ui = harness(async () => response(wire(data)));
  await ui.submit();
  const rows = ui.nodes["dashboard-rows"].textContent;
  assert.match(rows, /Define research plan/);
  assert.match(rows, /Generate search queries/);
  assert.match(rows, /Search Q7: company revenue <script>/);
  assert.doesNotMatch(rows, /tavily_search/);
  assert.match(rows, /call 13.*Quota exhausted/);
  assert.equal(ui.nodes["dashboard-issues"].hidden, false);
  assert.match(ui.nodes["dashboard-outcome"].textContent, /round limit/);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /HTTP 432.*credits or quota/);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /continued with available evidence/);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /Revenue remains unsupported/);
  assert.equal(ui.nodes["dashboard-issue-list"].children.length, 2);
  assert.equal(walk(ui.nodes["dashboard-issues"]).some(n => n.tagName === "script"), false);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /Downstream effectssearch \/ search/);
  const toolButton = walk(ui.nodes["dashboard-rows"].children[2]).find(n => n.tagName === "button");
  await toolButton.click();
  assert.match(ui.nodes["dashboard-detail-values"].textContent, /Failed operationinitialize/);
  assert.match(ui.nodes["dashboard-detail-values"].textContent, /Downstream effectssearch \/ search/);
});

test("planning recovery and error types survive result-only replay and reset", async () => {
  const issue = { kind: "planning_recovered", schema: "QueryBatch", recovered: true,
    recovery: "Using available queries and generating missing searches from plan requirements.",
    diagnostic: { provider: "kimi", stage: "plan", operation: "queries", category: "output_limit",
      message: "Model reached the output token limit before completing JSON." } };
  const result = { ...events.at(-1), status: "PARTIAL", issues: [issue], warnings: [],
    stop_explanation: "Evidence coverage was reached with limitations." };
  const ui = harness(async () => response(wire([result])));
  await ui.submit();
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /Generate search queries · Output limit/);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /before completing JSON/);
  assert.match(ui.nodes["dashboard-issue-list"].textContent, /Using available queries/);
  assert.match(ui.nodes["dashboard-outcome"].textContent, /with limitations/);
  await ui.nodes["new-chat"].click();
  assert.equal(ui.nodes["dashboard-issues"].hidden, true);
  assert.equal(ui.nodes["dashboard-issue-list"].children.length, 0);
});

test("successful retries keep the original failure and describe recovery", () => {
  const state = new TraceDashboardState();
  state.receive({ kind: "search_start", search_id: 1, query_id: "Q1", invocation_id: "query-1", provider: "tavily" });
  const failure = { provider: "tavily", stage: "search", operation: "search", category: "timeout", message: "Provider request timed out." };
  state.receive({ kind: "search_error", search_id: 1, invocation_id: "query-1", diagnostic: failure });
  state.receive({ kind: "search_recovered", search_id: 2, invocation_id: "query-1" });
  state.receive({ type: "result", stop_reason: "coverage", issues: [] });
  const issue = [...state.issues.values()][0];
  assert.equal(issue.diagnostic.category, "timeout");
  assert.equal(issue.recovered, true);
  assert.match(issue.recovery, /completed successfully/);
});

test("server configuration is used on every POST retry without model overrides or discovery", async () => {
  const requests = [];
  const ui = harness(async (path, options) => {
    requests.push({ path, body: JSON.parse(options.body) });
    if (requests.length === 1) throw new TypeError("Offline once");
    return response();
  });
  await ui.submit();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body.provider, undefined);
  assert.equal(requests[0].body.model, undefined);
  assert.deepEqual(requests[1].body, requests[0].body);
  assert.equal(ui.nodes.provider, undefined);
  assert.equal(ui.nodes.model, undefined);
});

test("stop during reasoning sends a server cancellation and keeps the report and trace", async () => {
  let controller;
  const requests = [];
  const cancelled = { ...events.at(-1), sequence: 4, stop_reason: "cancelled", report: "# Evidence collected before stopping" };
  const ui = harness(async (path, options) => {
    requests.push({ path, options });
    if (path.endsWith("/stop")) {
      controller.enqueue(encoder.encode(wire([
        { type: "trace", kind: "cancellation_requested", sequence: 3, message: "Stopping research…" }, cancelled,
      ])));
      controller.close();
      return new Response(JSON.stringify({ status: "stopping" }));
    }
    return new Response(new ReadableStream({
      start(c) { controller = c; c.enqueue(encoder.encode(wire(events.slice(1, 3).map((e, i) => ({ ...e, sequence: i + 1 }))))); },
    }));
  });
  const research = ui.submit();
  await until(() => ui.nodes.events.textContent.includes("Planning research"));
  assert.equal(ui.nodes.stop.hidden, false);
  await ui.nodes.stop.click();
  await research;
  assert.equal(requests.length, 2);
  assert.match(requests[1].path, /^\/api\/research\/[\w-]+\/stop$/);
  assert.equal(requests[1].options.method, "POST");
  assert.equal(ui.nodes["activity-title"].textContent, "Research stopped");
  assert.equal(ui.nodes["dashboard-state"].textContent, "Stopped");
  assert.equal(ui.nodes.stop.hidden, true);
  assert.doesNotMatch(ui.nodes.events.textContent, /Checking 收入|Partial output retained/);
  ui.nodes.download.click();
  assert.equal(await ui.blobs[0].text(), cancelled.report);
  ui.nodes["download-trace"].click();
  assert.equal(JSON.parse(await ui.blobs[1].text()).at(-1).stop_reason, "cancelled");
});

test("a failed stop remains retryable while research keeps streaming", async () => {
  let controller;
  let attempts = 0;
  const ui = harness(async path => {
    if (path.endsWith("/stop")) {
      if (++attempts === 1) return new Response(JSON.stringify({ error: "Try stopping again" }), { status: 503 });
      controller.enqueue(encoder.encode(wire([{ ...events.at(-1), sequence: 3, stop_reason: "cancelled" }])));
      controller.close();
      return new Response(JSON.stringify({ status: "stopping" }));
    }
    return new Response(new ReadableStream({
      start(c) { controller = c; c.enqueue(encoder.encode(wire(events.slice(0, 2)))); },
    }));
  });
  const research = ui.submit();
  await until(() => ui.nodes.events.textContent.includes("Planning research"));
  await ui.nodes.stop.click();
  assert.equal(ui.nodes.stop.disabled, false);
  assert.equal(ui.nodes.stop.hidden, false);
  assert.match(ui.nodes.error.textContent, /Try stopping again/);
  await ui.nodes.stop.click();
  await research;
  assert.equal(attempts, 2);
  assert.equal(ui.nodes["activity-title"].textContent, "Research stopped");
});

test("opening a saved cancelled chat does not disable stopping the next run", async () => {
  const savedEvents = [
    { type: "trace", kind: "cancellation_requested", sequence: 1, message: "Stopping research…" },
    { ...events.at(-1), sequence: 2, stop_reason: "cancelled" },
  ];
  const chat = { id: "cancelled", question: "Revenue", status: "partial", created_at: "2026-10-12T03:00:00Z", events: savedEvents };
  let controller;
  const ui = harness(async path => {
    if (path.endsWith("/stop")) {
      controller.enqueue(encoder.encode(wire([{ ...events.at(-1), sequence: 2, stop_reason: "cancelled" }])));
      controller.close();
      return new Response(JSON.stringify({ status: "stopping" }));
    }
    return new Response(new ReadableStream({ start(c) { controller = c; c.enqueue(encoder.encode(wire(events.slice(0, 1)))); } }));
  }, async url => new Response(JSON.stringify(url.startsWith("/api/chats?") ? { chats: [chat], has_more: false } : chat)));
  await until(() => ui.nodes["chat-list"].children.length === 1);
  await ui.nodes["chat-list"].children[0].click();
  assert.equal(ui.nodes["activity-title"].textContent, "Research stopped");
  ui.nodes.question.value = "Another question";
  const research = ui.submit();
  await until(() => ui.nodes.events.textContent.includes("Planning research"));
  assert.equal(ui.nodes.stop.disabled, false);
  await ui.nodes.stop.click();
  await research;
  assert.equal(ui.nodes["activity-title"].textContent, "Research stopped");
});

test("server configuration enables research without fetching a catalog", async () => {
  let discoveries = 0;
  const ui = harness(async () => response(), undefined,
    async () => { discoveries++; throw new Error("must not discover"); },
    async () => new Response(JSON.stringify({ configured: true, provider: "aliyun", model: "deepseek-v4.1-flash" })));
  await until(() => !ui.nodes.submit.disabled);
  assert.equal(discoveries, 0);
  assert.match(ui.nodes["provider-status"].textContent, /deepseek-v4.1-flash/);
  assert.doesNotMatch(html, /id="(?:provider|model|refresh-models)"/);
});

test("missing server credentials keep loading off and explain configuration", async () => {
  const ui = harness(async () => response(), undefined, undefined,
    async () => new Response(JSON.stringify({ configured: false, provider: "aliyun", model: "deepseek-v4.1-flash", missing_keys: ["ALIYUN_API_KEY"] })));
  await until(() => ui.nodes["provider-status"].textContent.includes("ALIYUN_API_KEY"));
  assert.equal(ui.nodes.submit.disabled, true);
  assert.equal(ui.nodes.typing.hidden, false); // No research request was started.
  assert.equal(ui.timers.size, 0);
});

test("multi-agent plan and PAI decisions render with targeted queries", async () => {
  const architecture = [
    { type: "trace", kind: "plan", query_count: 15,
      search_queries: Array.from({length: 15}, (_, i) => ({id: `Q${i+1}`, query: `evidence ${i+1}`})),
      research_requirements: [{id: "R1", question: "Find revenue"}],
      research_groups: [{theme: "Financial evidence", query_ids: ["Q1", "Q2", "Q3", "Q4", "Q5"]}] },
    { type: "trace", kind: "verification", provider: "pai", model: "pai-judge", sufficient: false,
      decision: {decision: "RESEARCH_MORE", requirements: [{supporting_claim_ids: ["F1"]}],
        critical_gaps: ["Revenue period missing"], contradictions: [], suggested_queries: ["company 2026 revenue official"]} },
    { type: "trace", kind: "classifier_ready_rejected" },
    {...events.at(-1), sequence: undefined},
  ];
  const ui = harness(async () => response(wire(architecture)));
  await ui.submit();
  assertReady(ui);
  assert.match(ui.nodes.events.textContent, /Planning research/);
  assert.match(ui.nodes.events.textContent, /Cross-checking findings/);
  assert.doesNotMatch(ui.nodes.events.textContent, /R1:|RESEARCH_MORE|company 2026/);
  ui.nodes["download-trace"].click();
  assert.match(await ui.blobs[0].text(), /company 2026 revenue official/);
});

test("validated answer blocks stream before completion and survive replay without duplicate content", async () => {
  let controller;
  const ui = harness(async () => new Response(new ReadableStream({ start(c) { controller = c; } })));
  const pending = ui.submit();
  const prefix = [
    { type: "status", code: "writing", message: "Writing the answer…", source_count: 2, sequence: 1 },
    { type: "answer_start", sequence: 2 },
    { type: "answer_delta", chunk_index: 0, text: "# Revenue\n\n", sequence: 3 },
    { type: "answer_delta", chunk_index: 1, text: "Measured 收入 🌱", sequence: 4 },
  ];
  controller.enqueue(encoder.encode(wire(prefix)));
  await until(() => ui.nodes.answer.textContent.includes("Measured 收入 🌱"));
  assert.equal(ui.nodes["answer-section"].hidden, false);
  assert.equal(ui.nodes.submit.disabled, true);
  controller.enqueue(encoder.encode(wire([
    prefix.at(-1),
    { type: "answer_delta", chunk_index: 1, text: "DUPLICATE", sequence: 5 },
    { type: "trace", kind: "output_delta", text: "PRIVATE JSON", sequence: 6 },
    { ...events.at(-1), sequence: 7, report: "# Revenue\n\nMeasured 收入 🌱" },
  ])));
  controller.close();
  await pending;
  assertReady(ui);
  assert.doesNotMatch(ui.nodes.answer.textContent, /DUPLICATE|PRIVATE JSON/);
  assert.equal(ui.nodes.answer.textContent.split("Measured").length - 1, 1);
});

test("status duplicates and internal chunks do not flicker or create activity rows", async () => {
  let controller;
  const ui = harness(async () => new Response(new ReadableStream({ start(c) { controller = c; } })));
  const pending = ui.submit();
  controller.enqueue(encoder.encode(wire([
    { type: "status", code: "searching", message: "Searching the web…", source_count: 1 },
    ...Array.from({ length: 40 }, (_, i) => ({ type: "status", code: "searching", message: "Searching the web…", source_count: i % 2 + 1 })),
    { type: "trace", kind: "llm_start", schema: "ResearchPlan", stage: "plan" },
    { type: "trace", kind: "thinking_delta", text: "PRIVATE" },
    { type: "trace", kind: "search_error", diagnostic: { message: "PRIVATE" } },
  ])));
  await until(() => ui.nodes["activity-title"].textContent === "Searching the web…");
  assert.equal(ui.nodes.events.children.length, 1);
  assert.equal(ui.nodes.error.hidden, false);
  assert.doesNotMatch(ui.nodes.error.textContent, /PRIVATE/);
  assert.doesNotMatch(ui.nodes.events.textContent, /PRIVATE|Planning/);
  controller.enqueue(encoder.encode(wire([
    { type: "status", code: "retrying", message: "Search temporarily unavailable. Retrying…" },
    { type: "status", code: "reading", message: "Reading sources…", source_count: 3 },
    events.at(-1),
  ])));
  controller.close();
  await pending;
  assertReady(ui);
  assert.equal(ui.nodes.events.children.length, 3);
  assert.match(ui.nodes.events.textContent, /Retrying/);
});

test("events belonging to another run cannot update progress or answer", async () => {
  const ui = harness(async () => response(wire([
    { type: "status", run_id: "old-run", code: "writing", message: "STALE", sequence: 999 },
    { type: "answer_delta", run_id: "old-run", chunk_index: 0, text: "STALE", sequence: 1000 },
    { ...events.at(-1), sequence: 1 },
  ])));
  await ui.submit();
  assertReady(ui);
  assert.doesNotMatch(ui.nodes.events.textContent + ui.nodes.answer.textContent, /STALE/);
});

test("Enter submits, Shift+Enter and composing text preserve editing", () => {
  const ui = harness(async () => response());
  let submissions = 0;
  ui.nodes["research-form"].requestSubmit = () => submissions++;
  const key = ui.nodes.question.listeners.get("keydown");
  let prevented = false;
  key({ key: "Enter", shiftKey: false, preventDefault() { prevented = true; } });
  assert.equal(submissions, 1); assert.equal(prevented, true);
  key({ key: "Enter", shiftKey: true });
  key({ key: "Enter", shiftKey: false, isComposing: true });
  assert.equal(submissions, 1);
});

test("model call namespaces preserve independent writers and evaluator decisions", () => {
  const { TraceDashboardState } = require('../src/deepsearchagent/web/dashboard.js');
  const state = new TraceDashboardState();
  state.receive({ kind: 'stage_start', task_id: 'writer-task', stage: 'write' });
  state.receive({ kind: 'llm_start', call_id: 1, span_id: 'research:1', model: 'deepseek-v4.1-flash', invocation_id: 'research-op', parent_id: 'research-task', elapsed_seconds: 0 });
  state.receive({ kind: 'llm_start', call_id: 1, span_id: 'writer:1', model: 'qwen3.8-flash', invocation_id: 'writer-op', parent_id: 'writer-task', elapsed_seconds: 1 });
  state.receive({ kind: 'llm_complete', call_id: 1, span_id: 'writer:1', elapsed_seconds: 3 });
  assert.equal(state.spans.size, 2);
  assert.equal(state.spans.get('research:1').status, 'running');
  assert.equal(state.spans.get('writer:1').status, 'complete');
  assert.equal(state.tasks.get('writer-task').stage, 'write');
  state.receive({ kind: 'llm_start', call_id: 'judge', schema: 'QualityEvaluation', model: 'qwen3.8-max' });
  state.receive({ kind: 'quality_evaluation', call_id: 'judge', decision: 'REVISE', overall_score: 2.5, scores: { relevance: 2 }, rejection_reasons: ['Missing requested comparison'], confidence: .8 });
  assert.equal(state.spans.get('model-judge').evaluation.decision, 'REVISE');
  assert.deepEqual(state.spans.get('model-judge').evaluation.reasons, ['Missing requested comparison']);
});

test("diagnostic updates merge retry history and recovered errors stop counting as active", async () => {
  const issue = { type: 'diagnostic', error_id: 'stable', invocation_id: 'call-group', attempt: 1, resolution: 'active', severity: 'warning', timestamp: '2026-10-13T01:00:00Z',
    diagnostic: { provider: 'aliyun', stage: 'write', operation: 'chat_completion', category: 'rate_limit', message: 'Provider rate limit.', http_status: 429, root_cause: 'Provider returned HTTP 429.', cause_confidence: 'confirmed' } };
  const ui = harness(async () => response(wire([issue, { ...issue, attempt: 2, resolution: 'recovered', recovered: true, recovery: 'A subsequent attempt succeeded.' }, events.at(-1)])));
  await ui.submit();
  assert.equal(ui.nodes['error-count'].hidden, true);
  assert.match(ui.nodes['error-center-summary'].textContent, /0 unresolved · 1 recovered/);
  assert.equal(ui.nodes['dashboard-issue-list'].children.length, 1);
  await ui.nodes['dashboard-issue-list'].children[0].children[1].click();
  assert.match(ui.nodes['dashboard-issue-list'].textContent, /history retained/);
});
