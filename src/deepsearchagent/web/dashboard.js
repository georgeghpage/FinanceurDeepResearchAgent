"use strict";

// A call's provider usage is a snapshot, not an incremental token count.
// Updating the same span makes replay, reconnects and repeated usage safe.
class TraceDashboardState {
  constructor() { this.reset(); }
  reset() {
    this.spans = new Map(); this.pricing = null; this.elapsed = 0;
    this.status = "Idle"; this.runId = null; this.result = null;
    this.sequences = new Set(); this.searchSerial = 0; this.activeSearch = null;
    this.tasks = new Map(); this.issues = new Map(); this.outcome = null; this.warnings = [];
  }
  finish(status = "interrupted") {
    this.status = status === "completed" ? "Complete" : status === "running" ? "Awaiting reconnect" : "Interrupted";
    if (status !== "running") for (const span of this.spans.values()) {
      if (["running", "retrying"].includes(span.status)) { span.status = status === "cancelled" ? "cancelled" : "interrupted"; span.end = this.elapsed; }
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
    if (kind === "stage_start" && event.task_id) this.tasks.set(event.task_id, event);
    if (event.type === "diagnostic") this.recordIssue(event);
    if (this.status === "Reconnecting" && event.type !== "error") this.status = "Live";
    if (kind === "run_start") { this.pricing = event.pricing || null; this.status = "Live"; }
    if (kind === "llm_start" && event.call_id != null) {
      const key = event.span_id || `model-${event.call_id}`;
      if (!this.spans.has(key)) this.spans.set(key, {
        key, type: "model", id: event.call_id, name: stageName(event.stage, event.schema), schema: event.schema,
        provider: event.provider || "kimi", model: event.model, operation: event.operation, stage: event.stage, parentId: event.parent_id, invocationId: event.invocation_id, attempt: event.attempt || 1, timestamp: event.timestamp, requestId: event.request_id, pricing: event.pricing, start: finiteNumber(event.elapsed_seconds),
        end: null, status: "running", usage: false, input: null, output: null, tokens: null,
        cached: null, cacheWrite: null, reasoning: null, ttft: null, outputChars: 0, reasoningChars: 0,
        timeout: event.timeout_seconds, limit: event.max_output_tokens,
      });
      this.status = "Live";
    }
    const call = this.spans.get(event.span_id || `model-${event.call_id}`);
    if (kind === "quality_evaluation") {
      const evaluatedCall = call || [...this.spans.values()].reverse().find(s => s.schema === "QualityEvaluation");
      if (evaluatedCall) evaluatedCall.evaluation = { decision: event.decision, scores: event.scores,
        overall: event.overall_score, confidence: event.confidence, reasons: event.rejection_reasons || [] };
    }
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
        call.requestId = event.request_id || event.diagnostic?.request_id || call.requestId;
        call.failure = event.diagnostic; call.finishReason = event.finish_reason;
      }
    }
    if (kind === "search_start") {
      const id = event.search_id ?? ++this.searchSerial;
      const key = `tool-${id}`; this.activeSearch = key;
      if (!this.spans.has(key)) this.spans.set(key, {
        key, type: "tool", id, name: searchName(event), queryId: event.query_id,
        provider: event.provider || event.source, operation: event.operation || "search", stage: event.stage, parentId: event.parent_id, invocationId: event.invocation_id, attempt: event.attempt || 1, timestamp: event.timestamp, requestId: event.request_id, query: event.query, start: finiteNumber(event.elapsed_seconds),
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
    if (["llm_retry", "llm_continuation", "search_retry", "llm_recovered", "search_recovered"].includes(kind)) {
      for (const span of this.spans.values()) if (event.invocation_id && span.invocationId === event.invocation_id) {
        span.recovery = event.recovery || (["llm_recovered", "search_recovered"].includes(kind) ? "A subsequent attempt succeeded." : `Retry scheduled after ${event.retry_delay_seconds || 0}s.`);
        span.outcome = kind.endsWith("recovered") ? "Recovered by subsequent attempt" : "Retrying";
        if (kind.endsWith("retry") || kind === "llm_continuation") span.status = "retrying";
        if (kind.endsWith("recovered") && span.failure) span.status = "recovered";
      }
    }
    this.recordIssue(event);
    if (event.type === "error") this.outcome = event.failure
      ? `${stageName(event.failure.stage)} stopped: ${event.failure.message}` : event.message || "Research stopped because of an error.";
    if (kind === "search_recovered" && event.invocation_id) {
      for (const issue of this.issues.values()) {
        if (issue.invocation_id === event.invocation_id) {
          issue.recovered = true; issue.recovery = "A subsequent attempt completed successfully.";
        }
      }
    }
    if (["run_stopped", "server_error"].includes(kind) || event.type === "error") this.finish(event.stop_reason === "cancelled" ? "cancelled" : "interrupted");
    if (kind === "run_complete") { this.finish("completed"); }
    if (event.type === "result") {
      this.result = event;
      for (const issue of event.issues || []) this.recordIssue(issue);
      this.outcome = event.stop_explanation || outcomeDescription(event);
      this.warnings = [...new Set([...(event.warnings || []), ...(event.unresolved || [])])];
      this.elapsed = finiteNumber(event.elapsed_seconds) ?? this.elapsed;
      this.finish(event.stop_reason === "cancelled" ? "cancelled" : event.failure ? "interrupted" : "completed");
      this.status = event.failure || event.stop_reason === "cancelled" ? "Stopped" : (event.status ? event.status === "COMPLETE" : event.stop_reason === "coverage") ? "Complete" : "Partial";
    }
  }
  recordIssue(event) {
    const diagnostic = event.diagnostic || event.failure;
    if (!diagnostic) return;
    const callId = event.call_id ?? diagnostic.call_id;
    const span = this.spans.get(event.span_id || (event.search_id != null ? `tool-${event.search_id}` : `model-${callId}`));
    const key = event.invocation_id || diagnostic.correlation_id || event.error_id || [diagnostic.provider, diagnostic.stage, diagnostic.operation, diagnostic.category,
      callId, event.search_id, event.query_id].join("|");
    const previous = this.issues.get(key);
    this.issues.set(key, { ...previous, ...event, diagnostic, call_id: callId,
      downstream_effects: [...new Set([...(previous?.downstream_effects || []), ...(event.downstream_effects || []), ...(diagnostic.downstream_effects || [])])],
      name: span?.name || (diagnostic.stage === "search" ? searchName(event) : stageName(diagnostic.stage, event.schema)),
      schema: event.schema || span?.schema,
      recovered: event.resolution ? event.resolution === "recovered" : previous?.recovered || event.recovered,
      timestamp: previous?.timestamp || event.timestamp, severity: event.severity || previous?.severity || "warning",
      resolution: event.resolution || previous?.resolution || (event.recovered ? "recovered" : "active"), recovery: event.recovery || previous?.recovery || (event.fallback_required ? "Research continued with available evidence." : undefined) });
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
      errors: spans.filter(s => ["error", "failed", "interrupted", "retrying", "recovered"].includes(s.status)).length,
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
  const names = { initialize: "Initialize research", cleanup: "Close provider connections",
    plan: "Define research plan", search: "Search for evidence", extract: "Read sources and extract evidence",
    verify: "Evaluate evidence coverage", write: "Write cited report", validate: "Validate report citations", finalize: "Finalize research",
    PlanningBrief: "Define research plan", QueryBatch: "Generate search queries", ResearchPlan: "Define research plan",
    EvidencePlan: "Define research plan", Extraction: "Read sources and extract evidence", EvidenceExtraction: "Read sources and extract evidence",
    Verification: "Cross-check findings", JudgeDecision: "Evaluate evidence coverage", ReportDraft: "Write cited report", ReportAudit: "Validate report citations", QualityEvaluation: "Evaluate answer relevance and quality" };
  return names[schema] || names[stage] || schema || stage || "Model request";
}
function searchName(event) {
  const query = (event.query || "").trim();
  const label = `Search${event.query_id ? ` ${event.query_id}` : " for evidence"}`;
  return query ? `${label}: ${query.length > 85 ? query.slice(0, 82) + "…" : query}` : label;
}
function errorType(category) {
  const label = (category || "unknown_error").replace(/_/g, " ");
  return label[0].toUpperCase() + label.slice(1);
}
function outcomeDescription(event) {
  if (event.stop_reason === "cancelled") return "Research stopped at the user's request before the full plan finished.";
  if (event.failure) return `${stageName(event.failure.stage)} stopped: ${event.failure.message}`;
  if (event.stop_reason === "coverage") return event.status && event.status !== "COMPLETE"
    ? "Evidence coverage was reached with limitations. Some steps did not finish successfully."
    : "Research completed with sufficient evidence coverage.";
  return ({ round_limit: "The research round limit was reached before all evidence requirements were covered.",
    budget: "A research budget limit was reached before the full plan finished.",
    no_tasks: "Research ended without sufficient coverage; no further research tasks were available.",
    error: "An error stopped research before the full plan finished." })[event.stop_reason] || "Research did not finish.";
}
function traceTime(value) {
  if (value == null) return "—";
  return value < 60 ? `${value.toFixed(1)}s` : `${Math.floor(value / 60)}m ${(value % 60).toFixed(0)}s`;
}
function traceMoney(value) { return value == null ? "—" : `$${value.toFixed(4)}`; }
function traceCount(value) { return value == null ? "—" : value.toLocaleString(); }

class ErrorCenter {
  constructor(document) {
    this.document = document; this.seen = new Set(); this.dismissed = new Set(); this.fatalShown = new Set();
    this.node("error-center-toggle").addEventListener("click", () => this.open());
    this.node("toast-view").addEventListener("click", () => this.open());
    this.node("toast-dismiss").addEventListener("click", () => this.dismissToast());
    this.node("error-center-close").addEventListener("click", () => this.close("error-center"));
    this.node("critical-close").addEventListener("click", () => this.close("critical-error"));
    this.node("critical-inspect").addEventListener("click", () => { this.close("critical-error"); this.open(); });
  }
  node(id) { return this.document.getElementById(id); }
  reset() { this.seen.clear(); this.dismissed.clear(); this.fatalShown.clear(); this.dismissToast(); this.close("error-center"); this.close("critical-error"); }
  open() { const dialog = this.node("error-center"); if (!dialog.open) dialog.showModal?.(); }
  close(id) { this.node(id).close?.(); }
  dismissToast() { this.node("notification-toast").hidden = true; this.node("error").hidden = true; }
  notify(message, fatal = false, record = true) {
    if (record && this.state) {
      const key = `client:${message}`;
      this.state.recordIssue({ invocation_id: key, timestamp: new Date().toISOString(), severity: fatal ? "fatal" : "warning",
        diagnostic: { provider: "Application", stage: "connection", operation: "client", category: "client_error", message } });
      this.seen.add(key); this.render(this.state);
    }
    this.node("error").textContent = message; this.node("error").hidden = false;
    this.node("notification-toast").hidden = false;
    if (fatal && !this.node("critical-error").open) {
      this.node("critical-error-message").textContent = message;
      this.node("critical-error").showModal?.();
    }
  }
  receive(state, event) {
    let fresh = false;
    for (const [key, issue] of state.issues) {
      if (!this.seen.has(key)) { this.seen.add(key); if (!issue.recovered && !event.replay) fresh = true; }
      if (issue.severity === "fatal" && !this.fatalShown.has(key) && !event.replay) {
        this.fatalShown.add(key); this.notify(issue.diagnostic.message, true, false);
      }
    }
    if ([...state.issues.values()].every(issue => issue.recovered)) this.dismissToast();
    if (fresh && !this.node("critical-error").open) {
      const count = [...state.issues.values()].filter(i => !i.recovered).length;
      this.notify(`Research encountered ${count} issue${count === 1 ? "" : "s"}. Open the Error Center for details.`, false, false);
    }
  }
  render(state) {
    const issues = [...state.issues.entries()];
    const active = issues.filter(([, issue]) => !issue.recovered).length;
    this.node("error-count").textContent = String(active); this.node("error-count").hidden = !active;
    this.node("error-center-toggle").setAttribute("aria-label", `Open Error Center${active ? `, ${active} unresolved issues` : ""}`);
    this.node("error-center-summary").textContent = `${active} unresolved · ${issues.length - active} recovered · ${issues.length} in session history`;
    const list = this.node("dashboard-issue-list"); list.className = "issue-history";
    const expanded = new Set(Array.from(list.children || []).filter(n => n.children[0]?.open).map(n => n.issueKey));
    const focused = this.document.activeElement?.issueKey;
    const make = (tag, text) => { const n = this.document.createElement(tag); if (text != null) n.textContent = text; return n; };
    this.state = state;
    list.replaceChildren(...issues.map(([key, issue]) => {
      const diagnostic = issue.diagnostic, li = make("li"), details = make("details");
      li.issueKey = key; li.className = issue.recovered ? "recovered" : issue.severity;
      details.open = expanded.has(key);
      const summary = make("summary", `${diagnostic.provider || "Application"} · ${issue.name || diagnostic.stage || "Unknown component"} · ${errorType(diagnostic.category)} · ${issue.recovered ? "Recovered" : issue.resolution || "Active"}`);
      summary.issueKey = `summary-${key}`;
      details.append(summary);
      const metadata = make("p", `${issue.timestamp ? new Date(issue.timestamp).toLocaleString() : "Timestamp unavailable"} · ${diagnostic.stage || issue.stage || "Unknown component"} · ${issue.severity} · attempt ${issue.attempt || 1}`);
      metadata.className = "issue-meta"; details.append(metadata, make("p", `${diagnostic.http_status ? `HTTP ${diagnostic.http_status} — ` : ""}${diagnostic.message}`));
      const dl = make("dl");
      for (const [label, value] of [["HTTP / code", `${diagnostic.http_status || "—"} / ${diagnostic.provider_code || diagnostic.category}`],
        ["Root cause", `${diagnostic.root_cause || "Unknown"} (${diagnostic.cause_confidence || "unknown"})`],
        ["Recovery", issue.recovery || "No recovery recorded"], ["Outcome", issue.outcome || issue.resolution || "Active"],
        ["Request ID", diagnostic.request_id || "Not supplied by provider"], ["Correlation ID", issue.invocation_id || diagnostic.correlation_id || "—"],
        ["Failure origin", diagnostic.origin === "downstream" ? "Downstream consequence" : "Original failing operation"],
        ["Upstream call", diagnostic.upstream_span_id || "—"], ["Downstream effects", (issue.downstream_effects || []).join(" · ") || "None recorded"]]) dl.append(make("dt", label), make("dd", value));
      details.append(dl); li.append(details);
      const button = make("button", this.dismissed.has(key) ? "Notification dismissed · history retained" : "Dismiss notification");
      button.type = "button"; button.className = "secondary"; button.issueKey = `dismiss-${key}`;
      button.disabled = this.dismissed.has(key);
      button.addEventListener("click", () => { this.dismissed.add(key); this.dismissToast(); this.render(state); }); li.append(button);
      return li;
    }), ...state.warnings.map(warning => make("li", warning)));
    if (focused) for (const li of list.children) for (const node of [li.children[0]?.children[0], li.children[1]]) if (node?.issueKey === focused) node.focus();
  }
}

class TraceDashboard {
  constructor(document) {
    this.document = document; this.state = new TraceDashboardState(); this.selected = null;
    this.filter = "all";
    this.errorCenter = this.node("error-center") ? new ErrorCenter(document) : null;
    this.node("dashboard-filter").addEventListener("change", event => { this.filter = event.target.value; this.render(); });
    this.render();
  }
  node(id) { return this.document.getElementById(id); }
  text(id, value) { this.node(id).textContent = value; }
  element(tag, text, className) {
    const node = this.document.createElement(tag); if (text != null) node.textContent = text;
    if (className) node.className = className; return node;
  }
  reset() { this.state.reset(); this.selected = null; this.errorCenter?.reset(); this.render(); }
  receive(event) {
    this.state.receive(event);
    this.errorCenter?.receive(this.state, event);
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
    const issues = [...state.issues.values()];
    this.node("dashboard-issues").hidden = !issues.length && !state.outcome && !state.warnings.length;
    this.text("dashboard-outcome", state.outcome || "Research is running. Issues and recovery details appear below.");
    if (this.errorCenter) this.errorCenter.render(state);
    const p = state.pricing;
    this.text("dashboard-pricing", p ? `${p.model} · ${p.basis} rates · verified ${p.verified_at}. Per 1M tokens: input ${traceMoney(p.input_price_per_million)}, output ${traceMoney(p.output_price_per_million)}, cache hit ${traceMoney(p.cached_price_per_million)}, cache write (5m) ${traceMoney(p.cache_write_price_per_million)}. Tavily advanced search: ${traceMoney(p.tavily_price_per_search)}/call.` : "No pricing snapshot for this run. Cost remains unknown. Rates are saved with new runs.");
    const spans = [...state.spans.values()].filter(s => this.filter === "all" || s.type === this.filter || (this.filter === "error" && ["error", "failed", "interrupted", "retrying", "recovered"].includes(s.status)));
    this.node("dashboard-empty").hidden = spans.length > 0;
    this.text("dashboard-empty", state.spans.size ? "No calls match this filter." : "Every model request and search tool call will appear here.");
    const rows = spans.map(span => {
      const row = this.element("tr", null, `${span.type} ${span.status === "running" ? "active" : span.status}${span.key === this.selected ? " selected" : ""}`);
      const cell = this.element("td"), button = this.element("button", span.name, "call-select");
      button.type = "button"; button.setAttribute("aria-pressed", String(span.key === this.selected));
      button.callKey = span.key;
      button.setAttribute("title", span.query || span.name);
      button.append(this.element("small", `${span.provider || span.type} · call ${span.id} · ${({complete:"success",error:"failed",interrupted:"failed",recovered:"failed"})[span.status] || span.status} · attempt ${span.attempt || 1}${span.failure ? ` · ${errorType(span.failure.category)}` : ""}`));
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
      this.text("dashboard-detail-title", selected.name);
      this.text("dashboard-detail-description", selected.query || selected.model || selected.provider);
      const values = [["Provider / model", `${selected.provider || selected.type} / ${selected.model || selected.operation || "tool"}`],
        ["Stage / operation", `${selected.stage || "—"} / ${selected.operation || selected.schema || "—"}`],
        ["Timestamp", selected.timestamp ? new Date(selected.timestamp).toLocaleString() : "Unknown"],
        ["Request ID", selected.requestId || selected.failure?.request_id || "Not supplied by provider"],
        ["Correlation ID", selected.invocationId || "—"], ["Parent task", state.tasks.get(selected.parentId)?.stage || selected.parentId || "—"],
        ["Attempt", String(selected.attempt || 1)], ["Recovery", selected.recovery || "No recovery recorded"],
        ["Final outcome", selected.outcome || selected.status], ["Call", `${selected.provider || selected.type} #${selected.id}`], ["Status", ({complete:"success",error:"failed",interrupted:"failed",recovered:"failed"})[selected.status] || selected.status], ["Started", traceTime(selected.start)], ["Duration", traceTime(state.duration(selected))], ["Estimated cost", traceMoney(state.cost(selected))]];
      if (selected.type === "model") values.push(["First streamed text", traceTime(selected.ttft)],
        ["Input / output tokens", `${traceCount(selected.input)} / ${traceCount(selected.output)}`],
        ["Cached / cache write", `${traceCount(selected.cached)} / ${traceCount(selected.cacheWrite)}`],
        ["Reasoning tokens (in output)", traceCount(selected.reasoning)], ["Finish reason", selected.finishReason || "—"],
        ["Thinking / output characters", `${selected.reasoningChars} / ${selected.outputChars}`]);
      else {
        values.push(["Retrieved sources", traceCount(selected.sources)]);
        for (const step of selected.steps) values.push([step.operation, traceTime(step.at)]);
      }
      if (selected.evaluation) {
        const evaluation = selected.evaluation;
        values.push(["Quality decision", evaluation.decision], ["Overall score", `${evaluation.overall} / 5`],
          ["Judge confidence", String(evaluation.confidence)]);
        for (const [dimension, score] of Object.entries(evaluation.scores || {})) values.push([errorType(dimension), `${score} / 5`]);
        if (evaluation.reasons.length) values.push(["Rejection reasons", evaluation.reasons.join(" · ")]);
      }
      if (selected.timeout != null) values.push(["Request timeout", traceTime(selected.timeout)]);
      if (selected.failure) values.push(["Error type", `${errorType(selected.failure.category)} (${selected.failure.category})`],
        ["Why this step failed", selected.failure.message], ["Failed operation", selected.failure.operation || "—"], ["HTTP / provider code", `${selected.failure.http_status || "—"} / ${selected.failure.provider_code || selected.failure.category}`],
        ["Root cause", `${selected.failure.root_cause || "Unknown"} (${selected.failure.cause_confidence || "unknown"})`],
        ["Failure origin", selected.failure.origin === "downstream" ? "Downstream consequence" : "Original failing operation"],
        ["Upstream call", selected.failure.upstream_span_id || "—"],
        ["Downstream effects", (selected.failure.downstream_effects || []).join(" · ") || "None recorded"]);
      this.node("dashboard-detail-values").replaceChildren(...values.flatMap(([key, value]) => [this.element("dt", key), this.element("dd", value)]));
    }
  }
}
if (typeof module !== "undefined") module.exports = { TraceDashboardState, TraceDashboard, ErrorCenter };
