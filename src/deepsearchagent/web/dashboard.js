"use strict";

// A call's provider usage is a snapshot, not an incremental token count.
// Updating the same span makes replay, reconnects and repeated usage safe.
class TraceDashboardState {
  constructor() { this.reset(); }
  reset() {
    this.spans = new Map(); this.pricing = null; this.elapsed = 0;
    this.status = "Idle"; this.runId = null; this.result = null;
    this.sequences = new Set(); this.searchSerial = 0; this.activeSearch = null;
  }
  finish(status = "interrupted") {
    this.status = status === "completed" ? "Complete" : status === "running" ? "Awaiting reconnect" : "Interrupted";
    if (status !== "running") for (const span of this.spans.values()) {
      if (span.status === "running") { span.status = "interrupted"; span.end = this.elapsed; }
    }
  }
  receive(event) {
    if (event.sequence != null) {
      if (this.sequences.has(event.sequence)) return;
      this.sequences.add(event.sequence);
    }
    this.elapsed = Math.max(this.elapsed, finiteNumber(event.elapsed_seconds) ?? 0);
    this.runId = event.run_id || this.runId;
    const kind = event.kind;
    if (this.status === "Reconnecting" && event.type !== "error") this.status = "Live";
    if (kind === "run_start") { this.pricing = event.pricing || null; this.status = "Live"; }
    if (kind === "llm_start" && event.call_id != null) {
      const key = `model-${event.call_id}`;
      if (!this.spans.has(key)) this.spans.set(key, {
        key, type: "model", id: event.call_id, name: stageName(event.stage, event.schema),
        provider: event.provider || "kimi", model: event.model, pricing: event.pricing, start: finiteNumber(event.elapsed_seconds),
        end: null, status: "running", usage: false, input: null, output: null, tokens: null,
        cached: null, cacheWrite: null, reasoning: null, ttft: null, outputChars: 0, reasoningChars: 0,
        timeout: event.timeout_seconds, limit: event.max_output_tokens,
      });
      this.status = "Live";
    }
    const call = this.spans.get(`model-${event.call_id}`);
    if (call && ["thinking_delta", "output_delta"].includes(kind)) {
      if (call.ttft == null && call.start != null && finiteNumber(event.elapsed_seconds) != null) {
        call.ttft = Math.max(0, event.elapsed_seconds - call.start);
      }
      if (kind === "thinking_delta") call.reasoningChars += (event.text || "").length;
      else call.outputChars += (event.text || "").length;
    }
    if (call && ["llm_usage", "llm_complete", "llm_error"].includes(kind)) {
      if (event.usage_observed === true || (event.usage_observed !== false && finiteNumber(event.tokens) != null)) {
        call.usage = true; call.tokens = finiteNumber(event.tokens);
        call.input = finiteNumber(event.input_tokens); call.output = finiteNumber(event.output_tokens);
        call.cached = finiteNumber(event.cached_tokens); call.cacheWrite = finiteNumber(event.cache_write_tokens);
        call.reasoning = finiteNumber(event.reasoning_tokens);
      }
      call.ttft = finiteNumber(event.time_to_first_delta_seconds) ?? call.ttft;
      call.outputChars = finiteNumber(event.output_chars) ?? call.outputChars;
      call.reasoningChars = finiteNumber(event.reasoning_chars) ?? call.reasoningChars;
      if (kind !== "llm_usage") {
        call.status = kind === "llm_error" ? "error" : "complete";
        call.duration = finiteNumber(event.duration_seconds);
        call.end = finiteNumber(event.elapsed_seconds);
        call.failure = event.diagnostic; call.finishReason = event.finish_reason;
      }
    }
    if (kind === "search_start") {
      const id = event.search_id ?? ++this.searchSerial;
      const key = `tool-${id}`; this.activeSearch = key;
      if (!this.spans.has(key)) this.spans.set(key, {
        key, type: "tool", id, name: event.provider === "tavily" ? "tavily_search" : `${event.source || "web"} search`,
        provider: event.provider || event.source, query: event.query, start: finiteNumber(event.elapsed_seconds),
        end: null, status: "running", timeout: event.timeout_seconds, steps: [],
      });
    }
    const tool = this.spans.get(event.search_id != null ? `tool-${event.search_id}` : this.activeSearch);
    if (tool && kind === "tavily_step") tool.steps.push({ operation: event.operation, at: event.elapsed_seconds });
    if (tool && ["search_complete", "search_error"].includes(kind)) {
      tool.status = kind === "search_error" ? "error" : "complete";
      tool.duration = finiteNumber(event.duration_seconds); tool.end = finiteNumber(event.elapsed_seconds);
      tool.sources = event.source_count ?? event.sources?.length; tool.failure = event.diagnostic;
    }
    if (["run_stopped", "server_error"].includes(kind) || event.type === "error") this.finish();
    if (kind === "run_complete") { this.finish("completed"); }
    if (event.type === "result") {
      this.result = event;
      this.elapsed = finiteNumber(event.elapsed_seconds) ?? this.elapsed;
      this.finish(event.failure ? "interrupted" : "completed");
      this.status = event.failure || event.stop_reason === "cancelled" ? "Stopped" : (event.status ? event.status === "COMPLETE" : event.stop_reason === "coverage") ? "Complete" : "Partial";
    }
  }
  duration(span) {
    if (span.duration != null) return span.duration;
    if (span.start == null) return null;
    return Math.max(0, (span.end ?? this.elapsed) - span.start);
  }
  cost(span) {
    const p = span.pricing || this.pricing;
    if (!p) return null;
    if (span.type === "tool") {
      return span.provider === "tavily" && span.status === "complete" ? finiteNumber(p.tavily_price_per_search) : null;
    }
    if (!span.usage || span.input == null || span.output == null) return null;
    // Only use a rate card for the model that produced this call.
    if (p.model !== span.model) return null;
    const inputRate = finiteNumber(p.input_price_per_million), outputRate = finiteNumber(p.output_price_per_million);
    if (inputRate == null || outputRate == null) return null;
    const cached = span.cached ?? 0, written = span.cacheWrite ?? 0;
    if (cached + written > span.input) return null;
    const cacheRate = finiteNumber(p.cached_price_per_million), writeRate = finiteNumber(p.cache_write_price_per_million);
    if ((cached > 0 && cacheRate == null) || (written > 0 && writeRate == null)) return null;
    return ((span.input - cached - written) * inputRate + cached * (cacheRate ?? 0)
      + written * (writeRate ?? 0) + span.output * outputRate) / 1e6;
  }
  totals() {
    const spans = [...this.spans.values()], models = spans.filter(s => s.type === "model");
    const known = spans.map(s => this.cost(s)).filter(c => c != null);
    const observed = models.filter(s => s.usage);
    const sum = field => observed.reduce((value, s) => value + (s[field] ?? 0), 0);
    return { calls: spans.length, models: models.length, tools: spans.length - models.length,
      errors: spans.filter(s => ["error", "interrupted"].includes(s.status)).length,
      pending: spans.filter(s => s.status === "running").length,
      tokens: Math.max(sum("tokens"), finiteNumber(this.result?.tokens_used) ?? 0),
      input: observed.some(s => s.input != null) ? sum("input") : null,
      output: observed.some(s => s.output != null) ? sum("output") : null,
      cached: observed.some(s => s.cached != null) ? sum("cached") : null,
      observed: observed.length, missingUsage: models.length - observed.length,
      cost: finiteNumber(this.result?.total_cost_usd) ?? (known.length ? known.reduce((a, b) => a + b, 0) : null),
      unpriced: spans.length - known.length,
      costComplete: this.result?.cost_complete,
    };
  }
}
function finiteNumber(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; }
function stageName(stage, schema) {
  return ({ plan: "Plan", extract: "Extract", verify: "Judge", write: "Write", validate: "Validate",
    ResearchPlan: "Plan", EvidencePlan: "Plan", Extraction: "Extract", EvidenceExtraction: "Extract",
    Verification: "Verify", JudgeDecision: "Judge", ReportDraft: "Write", ReportAudit: "Validate" })[stage || schema] || schema || stage || "Model";
}
function traceTime(value) {
  if (value == null) return "—";
  return value < 60 ? `${value.toFixed(1)}s` : `${Math.floor(value / 60)}m ${(value % 60).toFixed(0)}s`;
}
function traceMoney(value) { return value == null ? "—" : `$${value.toFixed(4)}`; }
function traceCount(value) { return value == null ? "—" : value.toLocaleString(); }

class TraceDashboard {
  constructor(document) {
    this.document = document; this.state = new TraceDashboardState(); this.selected = null;
    this.filter = "all";
    this.node("dashboard-filter").addEventListener("change", event => { this.filter = event.target.value; this.render(); });
    this.render();
  }
  node(id) { return this.document.getElementById(id); }
  text(id, value) { this.node(id).textContent = value; }
  element(tag, text, className) {
    const node = this.document.createElement(tag); if (text != null) node.textContent = text;
    if (className) node.className = className; return node;
  }
  reset() { this.state.reset(); this.selected = null; this.render(); }
  receive(event) {
    this.state.receive(event);
    // Streaming text updates the inspector on the shared one-second timer.
    if (!["thinking_delta", "output_delta"].includes(event.kind)) this.render();
  }
  tick(seconds) { this.state.elapsed = Math.max(this.state.elapsed, seconds); this.render(); }
  connection(lost) { this.state.status = lost ? "Disconnected" : "Reconnecting"; this.render(); }
  finish(status) { this.state.finish(status); this.render(); }
  render() {
    const state = this.state, total = state.totals();
    this.text("dashboard-state", state.status);
    this.node("dashboard-state").className = `run-badge${state.status === "Live" ? " live" : ["Stopped", "Interrupted", "Disconnected"].includes(state.status) ? " error" : ""}`;
    this.text("dashboard-run", state.runId ? `Run ${state.runId}` : "Start a research chat to see live measurements.");
    this.text("dashboard-tokens", traceCount(total.tokens));
    this.text("dashboard-token-note", `${total.observed}/${total.models} model calls reported usage${total.missingUsage ? " · incomplete" : ""}`);
    this.text("dashboard-cost", traceMoney(total.cost));
    this.text("dashboard-cost-note", total.costComplete === false ? "Incomplete estimate · some rates or usage unavailable"
      : total.unpriced ? `${total.unpriced} call${total.unpriced === 1 ? "" : "s"} unpriced · known subtotal` : total.calls ? "All calls estimated" : "Waiting for calls");
    this.text("dashboard-duration", traceTime(state.elapsed));
    this.text("dashboard-calls", `${total.calls} / ${total.errors}`);
    this.text("dashboard-call-note", `${total.models} model · ${total.tools} tool${total.pending ? ` · ${total.pending} active` : ""}`);
    this.text("dashboard-input", traceCount(total.input)); this.text("dashboard-output", traceCount(total.output));
    this.text("dashboard-cache", traceCount(total.cached)); this.text("dashboard-axis-end", traceTime(state.elapsed));
    const p = state.pricing;
    this.text("dashboard-pricing", p ? `${p.model} · ${p.basis} rates · verified ${p.verified_at}. Per 1M tokens: input ${traceMoney(p.input_price_per_million)}, output ${traceMoney(p.output_price_per_million)}, cache hit ${traceMoney(p.cached_price_per_million)}, cache write (5m) ${traceMoney(p.cache_write_price_per_million)}. Tavily advanced search: ${traceMoney(p.tavily_price_per_search)}/call.` : "No pricing snapshot for this run. Cost remains unknown. Rates are saved with new runs.");
    const spans = [...state.spans.values()].filter(s => this.filter === "all" || s.type === this.filter || (this.filter === "error" && ["error", "interrupted"].includes(s.status)));
    this.node("dashboard-empty").hidden = spans.length > 0;
    this.text("dashboard-empty", state.spans.size ? "No calls match this filter." : "Every model request and search tool call will appear here.");
    const rows = spans.map(span => {
      const row = this.element("tr", null, `${span.type} ${span.status === "running" ? "active" : span.status}${span.key === this.selected ? " selected" : ""}`);
      const cell = this.element("td"), button = this.element("button", `${span.name} #${span.id}`, "call-select");
      button.type = "button"; button.setAttribute("aria-pressed", String(span.key === this.selected));
      button.callKey = span.key;
      button.append(this.element("small", `${span.type} · ${span.status}`));
      button.addEventListener("click", () => { this.selected = span.key; this.render(); }); cell.append(button);
      const timelineCell = this.element("td"), timeline = this.element("div", null, "timeline");
      const bar = this.element("div", null, "timeline-bar"); const duration = state.duration(span);
      const scale = Math.max(.1, state.elapsed), left = Math.min(100, (span.start ?? 0) / scale * 100);
      bar.style.left = `${left}%`; bar.style.width = `${Math.min(100 - left, (duration ?? 0) / scale * 100)}%`;
      timeline.setAttribute("role", "img");
      timeline.setAttribute("aria-label", `Start ${traceTime(span.start)}, duration ${traceTime(duration)}`);
      timeline.append(bar); timelineCell.append(timeline);
      row.append(cell, timelineCell, this.element("td", traceTime(duration)),
        this.element("td", span.type === "tool" ? "n/a" : traceCount(span.tokens)), this.element("td", traceMoney(state.cost(span))));
      return row;
    });
    const focused = this.document.activeElement?.callKey;
    this.node("dashboard-rows").replaceChildren(...rows);
    if (focused) {
      for (const row of rows) {
        const button = row.children[0].children[0];
        if (button.callKey === focused) button.focus();
      }
    }
    const selected = state.spans.get(this.selected); this.node("dashboard-detail").hidden = !selected;
    if (selected) {
      this.text("dashboard-detail-title", `${selected.name} #${selected.id}`);
      this.text("dashboard-detail-description", selected.query || selected.model || selected.provider);
      const values = [["Status", selected.status], ["Started", traceTime(selected.start)], ["Duration", traceTime(state.duration(selected))], ["Estimated cost", traceMoney(state.cost(selected))]];
      if (selected.type === "model") values.push(["First streamed text", traceTime(selected.ttft)],
        ["Input / output tokens", `${traceCount(selected.input)} / ${traceCount(selected.output)}`],
        ["Cached / cache write", `${traceCount(selected.cached)} / ${traceCount(selected.cacheWrite)}`],
        ["Reasoning tokens (in output)", traceCount(selected.reasoning)], ["Finish reason", selected.finishReason || "—"],
        ["Thinking / output characters", `${selected.reasoningChars} / ${selected.outputChars}`]);
      else {
        values.push(["Retrieved sources", traceCount(selected.sources)]);
        for (const step of selected.steps) values.push([step.operation, traceTime(step.at)]);
      }
      if (selected.timeout != null) values.push(["Request timeout", traceTime(selected.timeout)]);
      if (selected.failure) values.push(["Failure", `${selected.failure.category}: ${selected.failure.message}`]);
      this.node("dashboard-detail-values").replaceChildren(...values.flatMap(([key, value]) => [this.element("dt", key), this.element("dd", value)]));
    }
  }
}
if (typeof module !== "undefined") module.exports = { TraceDashboardState, TraceDashboard };
