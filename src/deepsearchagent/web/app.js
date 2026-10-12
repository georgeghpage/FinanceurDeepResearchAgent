"use strict";

const $ = (id) => document.getElementById(id);
const dashboard = new TraceDashboard(document);
let report = "";
let running = false;
let traceEvents = [];
let currentStatus = "";
let hasStructuredStatus = false;
let answerChunk = -1;
const discoveredSources = new Set();
let pendingRun = null;
let selectedChat = null;
let historyOffset = 0;
let historyLoading = false;
let openingChat = false;
let historyWarning = "";
let stopping = false;
let configuredProvider = "aliyun";
let configuredModel = "deepseek-v4.1-flash";
let configurationReady = false;

function updateProvider() {
  $("provider-status").textContent = `${configuredModel} · ${configuredProvider === "aliyun" ? "Aliyun Model Studio" : configuredProvider}`;
  updateRunControls();
}

async function loadProviderSettings() {
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    if (!response.ok) throw new Error("Could not load server configuration.");
    configuredProvider = data.provider;
    configuredModel = data.model;
    configurationReady = data.configured;
    updateProvider();
    if (!configurationReady) $("provider-status").textContent = data.configuration_valid === false
      ? "Server configuration is invalid. Check the environment settings."
      : `Set ${(data.missing_keys || []).join(" and ")} on the server before starting research.`;
  } catch (_) {
    $("provider-status").textContent = "Could not load server configuration. Check the server before starting.";
  }
}

function updateRunControls() {
  const active = running || Boolean(pendingRun);
  $("submit").disabled = running || openingChat || (!pendingRun && !configurationReady);
  $("stop").hidden = !active;
  $("stop").disabled = stopping;
  $("stop").textContent = stopping ? "Stopping…" : "Stop research ■";
}

function showQuestion(question, animate = false) {
  $("intro").hidden = true;
  $("sent-question").textContent = question;
  $("sent-message").hidden = false;
  $("sent-message").className = animate ? "sent-message message-enter" : "sent-message";
}

function updateHistorySelection() {
  for (const button of $("chat-list").children) {
    button.className = button.chatId === selectedChat ? "chat-link selected" : "chat-link";
    button.disabled = running || openingChat;
    button.setAttribute("aria-current", button.chatId === selectedChat ? "true" : "false");
  }
  $("new-chat").disabled = running || openingChat;
}

async function loadHistory(append = false) {
  if (historyLoading) return;
  historyLoading = true;
  $("load-more").disabled = true;
  try {
    const offset = append ? historyOffset : 0;
    const response = await fetch(`/api/chats?offset=${offset}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load chat history.");
    if (!append) $("chat-list").replaceChildren();
    for (const chat of data.chats) {
      const button = document.createElement("button");
      button.type = "button";
      button.chatId = chat.id;
      button.title = chat.question;
      const title = document.createElement("strong");
      title.textContent = chat.question;
      const detail = document.createElement("small");
      detail.textContent = `${new Date(chat.created_at).toLocaleDateString()} · ${chat.status}`;
      button.append(title, detail);
      button.addEventListener("click", () => openChat(chat.id));
      $("chat-list").append(button);
    }
    historyOffset = offset + data.chats.length;
    $("load-more").hidden = !data.has_more;
    $("history-status").textContent = historyWarning || (historyOffset ? "Saved in PostgreSQL" : "Your chats will appear here.");
    updateHistorySelection();
  } catch (error) {
    $("history-status").textContent = error.message || "Chat history is unavailable.";
  } finally {
    historyLoading = false;
    $("load-more").disabled = false;
  }
}

function resetChat() {
  pendingRun = null;
  stopping = false;
  updateProvider();
  selectedChat = null;
  report = "";
  historyWarning = "";
  traceEvents = [];
  resetProgress();
  dashboard.reset();
  $("events").replaceChildren();
  $("answer").replaceChildren();
  $("question").value = "";
  resizeQuestion();
  $("intro").hidden = false;
  for (const id of ["sent-message", "typing", "activity", "answer-section", "error"]) $(id).hidden = true;
  $("activity-count").textContent = "";
  $("source-count").textContent = "";
  $("submit").textContent = "Start research ↗";
  updateHistorySelection();
}

async function openChat(id) {
  if (running || openingChat) return;
  openingChat = true;
  $("submit").disabled = true;
  updateHistorySelection();
  try {
    const response = await fetch(`/api/chats/${id}`);
    const chat = await response.json();
    if (!response.ok) throw new Error(chat.error || "Could not open this chat.");
    resetChat();
    selectedChat = id;
    showQuestion(chat.question);
    traceEvents = chat.events;
    $("activity").hidden = false;
    for (const data of chat.events) {
      dashboard.receive(data);
      if (data.type === "trace") receiveTrace(data);
      if (data.type === "progress") receiveLegacyProgress(data);
      if (data.type === "status") receiveStatus(data);
      if (data.type === "answer_start" || data.type === "answer_delta") receiveAnswer(data);
      if (data.type === "result") showResult(data);
      if (data.type === "error") {
        $("error").textContent = data.failure ? failureDescription(data.failure) : data.message;
        $("error").hidden = false;
      }
    }
    if (chat.status === "running") {
      dashboard.finish("running");
      const settings = chat.events.find(event => event.kind === "run_start");
      pendingRun = { id, question: chat.question, provider: settings?.provider, model: settings?.model, started: Date.parse(chat.created_at),
        sequence: chat.events.at(-1)?.sequence || 0 };
      updateProvider(false, settings?.model);
      $("question").value = chat.question;
      $("submit").textContent = "Reconnect research ↗";
      $("activity-title").textContent = "Research in progress";
      $("status").textContent = "Reconnect to follow this chat's research.";
      $("activity").className = "activity running";
    } else if (!report) {
      dashboard.finish(chat.status);
      $("activity-title").textContent = chat.status === "interrupted" ? "Research interrupted" : "Research stopped";
      $("activity").className = "activity failed";
      $("status").textContent = "Saved trace retained. Start a new chat to try again.";
    }
  } catch (error) {
    $("error").textContent = error.message;
    $("error").hidden = false;
  } finally {
    openingChat = false;
    updateRunControls();
    updateHistorySelection();
  }
}

function showResult(data) {
  stopping = false;
  report = data.report;
  renderReport(report);
  $("answer-section").hidden = false;
  $("typing").hidden = true;
  $("activity").className = data.stop_reason === "cancelled" ? "activity stopped" : data.failure ? "activity failed" : "activity done";
  const partial = data.status ? data.status !== "COMPLETE" : data.stop_reason !== "coverage";
  $("activity-title").textContent = data.stop_reason === "cancelled" ? "Research stopped" : partial ? "Partial answer ready" : "Answer ready";
  $("status").className = "";
  $("source-count").textContent = "";
  $("status").textContent = `${data.sources} source${data.sources === 1 ? "" : "s"} · ${data.search_calls} searches · ${data.api_calls} model calls · ${data.tokens_used} tokens · ${Math.round(data.elapsed_seconds)}s${partial ? " · See the report's limitations." : ""}`;
  $("elapsed").textContent = `${Math.round(data.elapsed_seconds || 0)}s`;
  if (data.failure) {
    $("error").textContent = failureDescription(data.failure);
    $("error").hidden = false;
  } else {
    $("error").hidden = true;
  }
}

class ConnectionError extends Error {}

async function readResearchStream(response, receive) {
  if (!response.body) throw new ConnectionError("The research stream is unavailable.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  function line(text) {
    if (!text.trim()) return false;
    let data;
    try { data = JSON.parse(text); }
    catch (_) { throw new Error("The server sent an invalid research event. The trace received so far is still available."); }
    return receive(data);
  }
  try {
    while (true) {
      let chunk;
      try { chunk = await reader.read(); }
      catch (_) { throw new ConnectionError("The research connection was interrupted."); }
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const text of lines) if (line(text)) return;
      if (chunk.done) {
        if (buffer.trim() && line(buffer)) return;
        throw new ConnectionError("The connection ended before an answer arrived.");
      }
    }
  } finally {
    // A result is final: do not let a later socket failure replace a ready answer.
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function traceItem(title, data) {
  const item = document.createElement("li");
  item.className = "trace-item";
  const heading = document.createElement("div");
  heading.className = "trace-heading";
  const label = document.createElement("strong");
  label.textContent = title;
  const time = document.createElement("span");
  time.textContent = `${Number(data.elapsed_seconds || 0).toFixed(1)}s`;
  heading.append(label, time);
  item.append(heading);
  $("events").append(item);
  $("activity-count").textContent = `(${$("events").children.length})`;
  return item;
}

function failureDescription(failure) {
  return failure.message || "Research could not complete. Try again.";
}

function resetProgress() {
  currentStatus = "";
  hasStructuredStatus = false;
  answerChunk = -1;
  discoveredSources.clear();
}

function receiveStatus(data) {
  if (data.type === "status") hasStructuredStatus = true;
  if (stopping && data.code !== "stopping") return;
  const title = data.message || "Thinking…";
  if (data.code !== currentStatus) {
    currentStatus = data.code;
    const text = document.createElement("span");
    text.className = "status-copy";
    text.textContent = title;
    $("activity-title").replaceChildren(text);
    traceItem(title, data);
  }
  $("status").className = "sr-only";
  const count = data.source_count;
  $("status").textContent = Number.isInteger(count) && count > 0
    ? `${count} source${count === 1 ? "" : "s"} discovered` : "";
  $("source-count").textContent = $("status").textContent;
  // A single polite live region announces the activity and real source count.
  $("status").setAttribute("aria-label", title);
  $("status").textContent = title + ($("status").textContent ? ` · ${$("status").textContent}` : "");
}

function receiveAnswer(data) {
  if (data.type === "answer_start") {
    report = "";
    answerChunk = -1;
    $("answer").replaceChildren();
    return;
  }
  if (data.chunk_index <= answerChunk || typeof data.text !== "string") return;
  answerChunk = data.chunk_index;
  report += data.text;
  renderReport(report);
  $("answer-section").hidden = false;
  $("typing").hidden = true;
}

function receiveLegacyProgress(data) {
  if (hasStructuredStatus) return;
  const message = data.message || "";
  const match = [
    [/^Planning (research|15 research queries)$/, "planning", "Planning research…"],
    [/^(Retrieving evidence concurrently|Starting research)$/, "thinking", "Thinking…"],
    [/^Checking evidence and coverage$/, "checking", "Cross-checking findings…"],
    [/^Writing cited report$/, "writing", "Writing the answer…"],
  ].find(([pattern]) => pattern.test(message));
  if (match) receiveStatus({ ...data, code: match[1], message: match[2] });
}

function receiveTrace(data) {
  // Legacy saved traces get the same compact presentation. Internal deltas,
  // tool arguments and extracted findings never enter the answer renderer.
  if (data.kind === "cancellation_requested") {
    stopping = true;
    updateRunControls();
    receiveStatus({ ...data, code: "stopping", message: "Stopping research…" });
    return;
  }
  if (data.kind === "history_error") $("history-status").textContent = data.message;
  if (hasStructuredStatus) return;
  const stages = { plan: ["planning", "Planning research…"], search: ["searching", "Searching the web…"],
    extract: ["reading", "Reading sources…"], verify: ["checking", "Cross-checking findings…"],
    write: ["writing", "Writing the answer…"], validate: ["finalizing", "Finalizing research…"] };
  const schemas = { ResearchPlan: "plan", EvidencePlan: "plan", Extraction: "extract", EvidenceExtraction: "extract",
    Verification: "verify", JudgeDecision: "verify", ReportDraft: "write", ReportAudit: "validate" };
  let status;
  if (data.kind === "plan") status = stages.plan;
  else if (data.kind === "verification") status = stages.verify;
  else if (data.kind === "stage_start") status = stages[data.stage];
  else if (data.kind === "llm_start") status = stages[data.stage || schemas[data.schema]];
  else if (data.kind === "search_start" && currentStatus !== "searching") status = stages.search;
  else if (data.kind === "search_complete") {
    for (const source of data.sources || []) if (source.url) discoveredSources.add(source.url);
    // Source counters must not reset a retry or later-stage status.
    if (currentStatus === "searching") status = stages.search;
  }
  if (status) receiveStatus({ ...data, code: status[0], message: status[1], source_count: discoveredSources.size });
}

// The agent's Markdown uses headings, paragraphs, bullets and source links.
// Build DOM nodes so retrieved content cannot inject HTML or executable URLs.
function appendInline(parent, text) {
  const pattern = /\\([\\`*_{}\[\]<>#!|])|\[((?:\\.|[^\]\\])*)\]\(<(https?:\/\/[^>\s]+)>\)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    if (match[1]) {
      parent.append(document.createTextNode(match[1]));
    } else {
      const link = document.createElement("a");
      link.textContent = match[2].replace(/\\([\\`*_{}\[\]<>#!|])/g, "$1");
      link.href = match[3];
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.append(link);
    }
    offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function renderReport(markdown) {
  const fragment = document.createDocumentFragment();
  let list = null;
  for (const block of markdown.trim().split(/\n\s*\n/)) {
    for (const line of block.split("\n")) {
      const heading = line.match(/^(#{1,3}) (.*)$/);
      let element;
      if (line.startsWith("- ")) {
        if (!list) { list = document.createElement("ul"); fragment.append(list); }
        element = document.createElement("li");
        appendInline(element, line.slice(2));
        list.append(element);
      } else {
        list = null;
        element = document.createElement(heading ? `h${heading[1].length}` : "p");
        appendInline(element, heading ? heading[2] : line);
        fragment.append(element);
      }
    }
    list = null;
  }
  $("answer").replaceChildren(fragment);
}

function resizeQuestion() {
  const input = $("question");
  input.style.height = "auto";
  const height = Math.min(160, Math.max(28, input.scrollHeight));
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight > 160 ? "auto" : "hidden";
}
$("question").addEventListener("input", () => { resizeQuestion(); updateRunControls(); });

$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !running && !openingChat && !$("submit").disabled) {
    event.preventDefault();
    $("research-form").requestSubmit();
  }
});

$("research-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || running || openingChat) return;
  const resuming = pendingRun?.question === question;
  if (!resuming && !configurationReady) return;
  running = true;
  updateRunControls();
  showQuestion(question, !resuming);
  if (!resuming) { $("question").value = ""; historyWarning = ""; }
  $("typing").hidden = true;
  resizeQuestion();
  updateHistorySelection();
  if (!resuming) report = "";
  $("submit").disabled = true;
  $("submit").textContent = "Researching…";
  $("question").readOnly = true;
  $("answer-section").hidden = !resuming || !report;
  $("error").hidden = true;
  $("activity").hidden = false;
  $("activity").className = "activity running";
  $("activity-title").textContent = "Thinking…";
  $("status").textContent = "Starting research";
  if (!resuming) {
    $("events").replaceChildren();
    traceEvents = [];
    resetProgress();
    dashboard.reset();
    $("activity-count").textContent = "";
    $("elapsed").textContent = "0s";
  }
  const started = resuming ? pendingRun.started : Date.now();
  const timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    dashboard.tick(seconds);
    $("elapsed").textContent = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }, 1000);
  let lastSequence = resuming ? pendingRun.sequence : 0;

  function receive(data) {
    if (data.run_id && data.run_id !== pendingRun?.id) return false;
    if (data.type === "heartbeat") return false;
    if (data.sequence && data.sequence <= lastSequence) return false;
    dashboard.receive(data);
    traceEvents.push(data);
    if (data.history_error) {
      historyWarning = data.history_error;
      $("history-status").textContent = historyWarning;
    }
    const follow = $("trace").scrollHeight - $("trace").scrollTop - $("trace").clientHeight < 60;
    if (data.type === "progress") {
      receiveLegacyProgress(data);
    } else if (data.type === "status") {
      receiveStatus(data);
    } else if (data.type === "answer_start" || data.type === "answer_delta") {
      receiveAnswer(data);
    } else if (data.type === "trace") {
      receiveTrace(data);
    } else if (data.type === "result") {
      showResult(data);
    } else if (data.type === "error") {
      throw new Error(data.failure ? failureDescription(data.failure) : data.message);
    }
    if (data.sequence) lastSequence = data.sequence;
    if (pendingRun) pendingRun.sequence = lastSequence;
    if (follow) $("trace").scrollTop = $("trace").scrollHeight;
    return data.type === "result";
  }

  try {
    const runId = resuming ? pendingRun.id : crypto.randomUUID();
    selectedChat = runId;
    updateHistorySelection();
    const provider = resuming ? pendingRun.provider : configuredProvider;
    const model = resuming ? pendingRun.model : configuredModel;
    pendingRun = { id: runId, question, provider, model, started, sequence: lastSequence };
    for (let attempt = 0; ; attempt++) {
      try {
        let response;
        try {
          response = lastSequence
            ? await fetch(`/api/research/${runId}?after=${lastSequence}`)
            : await fetch("/api/research", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ question, run_id: runId, provider, model }),
            });
        } catch (_) { throw new ConnectionError("Could not reach the research server."); }
        if (!response.ok) {
          let data;
          try { data = await response.json(); }
          catch (_) { throw new Error(`The research server returned HTTP ${response.status}. Try again.`); }
          throw new Error(data.error || "Research could not start. Try again.");
        }
        await readResearchStream(response, receive);
        pendingRun = null;
        break;
      } catch (error) {
        if (!(error instanceof ConnectionError)) throw error;
        if (attempt >= 3) {
          throw new ConnectionError(`Could not reconnect to the research server at ${location.origin}. Check that the server is running. Any trace already received is still available to save.`);
        }
        const message = `Connection interrupted. Reconnecting (${attempt + 1}/3)…`;
        dashboard.connection(false);
        $("status").textContent = message;
        const connection = { type: "connection", message, elapsed_seconds: (Date.now() - started) / 1000 };
        traceEvents.push(connection);
        traceItem(message, connection);
        await new Promise(resolve => setTimeout(resolve, 500 * (2 ** attempt)));
      }
    }
  } catch (error) {
    const disconnected = error instanceof ConnectionError;
    if (disconnected) dashboard.connection(true);
    else if (!["Complete", "Partial", "Stopped"].includes(dashboard.state.status)) dashboard.finish();
    if (!disconnected) pendingRun = null;
    $("activity").className = "activity failed";
    $("activity-title").textContent = disconnected ? "Connection lost" : "Research failed";
    $("status").className = "";
    $("status").textContent = disconnected ? "Click Reconnect research to resume this run." : "You can try again below.";
    $("error").textContent = error.message || "Could not connect to the research agent.";
    $("error").hidden = false;

  } finally {
    clearInterval(timer);
    running = false;
    if (!pendingRun) stopping = false;
    updateProvider();
    $("typing").hidden = true;
    $("submit").replaceChildren(document.createTextNode(pendingRun ? "Reconnect research ↗" : "Start research ↗"));
    $("question").readOnly = false;
    $("question").value = pendingRun ? pendingRun.question : "";
    resizeQuestion();
    $("sent-message").className = "sent-message";
    updateHistorySelection();
    await loadHistory();
  }
});

$("stop").addEventListener("click", async () => {
  if (!pendingRun || stopping) return;
  stopping = true;
  updateRunControls();
  const runId = pendingRun.id;
  try {
    const response = await fetch(`/api/research/${runId}/stop`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not stop research.");
    if (pendingRun?.id !== runId) return;
    $("status").textContent = "Stopping research…";
    if (!running) $("research-form").requestSubmit();
  } catch (error) {
    if (pendingRun?.id !== runId) return;
    stopping = false;
    updateRunControls();
    $("error").textContent = error.message || "Could not stop research. Try again.";
    $("error").hidden = false;
  }
});

$("download-trace").addEventListener("click", () => {
  if (!traceEvents.length) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(traceEvents, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "research-trace.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$("download").addEventListener("click", () => {
  if (!report) return;
  const url = URL.createObjectURL(new Blob([report], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "research-report.md";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$("new-chat").addEventListener("click", () => {
  if (!running && !openingChat) { resetChat(); $("question").focus(); }
});
$("load-more").addEventListener("click", () => loadHistory(true));
loadHistory();

updateProvider();
updateRunControls();
loadProviderSettings();
