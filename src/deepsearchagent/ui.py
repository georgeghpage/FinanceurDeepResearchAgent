"""Small loopback-only web UI with streamed research progress."""

import argparse
import json
import threading
from datetime import datetime, timezone
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from .cancellation import Cancellation
from .config import Config, require_api_key, require_tavily_key
from .diagnostics import DiagnosticError, ErrorHistory, DiagnosticJournal, failure_from_exception, research_outcome, run_metadata
from .history import ChatStore, database_url
from .llm import AliyunBackend, KimiBackend
from .model_catalog import list_models
from .orchestrator import ResearchAgent
from .progress import ResearchProgress
from .tools.search import SearchRouter
from .tools.tavily import TavilyMCPSearch
from .writer import render

ASSETS = Path(__file__).with_name("web")
HEARTBEAT_SECONDS = 10
RETAINED_RUNS = 5


class ResearchRun:
    """In-memory event history that survives an individual browser connection."""

    def __init__(self, run_id: str, question: str, journal: DiagnosticJournal | None = None,
                 store: ChatStore | None = None) -> None:
        self.id = run_id
        self.question = question
        self.started = time.monotonic()
        self.events: list[dict] = []
        self.issues: list[dict] = []
        self.error_history = ErrorHistory(run_id)
        self.progress = ResearchProgress()
        self.done = False
        self.terminal = False
        self.cancellation = Cancellation()
        self.config: Config | None = None
        self.condition = threading.Condition()
        self.journal = journal
        self.logging_failed = False
        self.store = store
        self.history_failed = False
        self.history_warning_emitted = False

    def emit(self, event: dict) -> None:
        with self.condition:
            if self.terminal or (self.cancellation.event.is_set() and event.get("type") in {"progress", "trace", "status", "answer_start", "answer_delta"}
                                 and event.get("kind") not in {"cancellation_requested", "run_stopped"}
                                 and event.get("code") != "stopping"):
                return
            saved = {**event, "run_id": self.id,
                                "sequence": len(self.events) + 1,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "elapsed_seconds": event.get(
                                    "elapsed_seconds", round(time.monotonic() - self.started, 3))}
            terminal = event.get("type") in {"result", "error"}
            if terminal:
                update = self.error_history.observe(saved)
                if update:
                    self.emit(update)
                    saved["sequence"] = len(self.events) + 1
                if event.get("type") == "result":
                    saved["issues"] = list(self.error_history.issues.values())
            if self.journal and not self.journal.record(saved) and not self.logging_failed:
                self.logging_failed = True
                # Keep research running even when the log directory is unwritable.
                self.emit({"type": "trace", "kind": "logging_error", "invocation_id": "diagnostic-storage",
                           "diagnostic": failure_from_exception(DiagnosticError("Storage failed", category="storage_error"),
                               provider="application", stage="storage", operation="save_diagnostics").model_dump(),
                           "message": "Could not save the diagnostic log. Save trace remains available."})
                saved["sequence"] = len(self.events) + 1
            if self.store:
                try:
                    self.store.save_event(self.id, saved)
                except Exception:
                    self.history_failed = True
                if self.history_failed:
                    saved["history_error"] = "Some chat history could not be saved. Download this report and trace before leaving."
                    if not self.history_warning_emitted:
                        self.history_warning_emitted = True
                        self.emit({"type": "trace", "kind": "history_error", "invocation_id": "history-storage",
                                   "diagnostic": failure_from_exception(DiagnosticError("Storage failed", category="storage_error"),
                                       provider="application", stage="storage", operation="save_history").model_dump(),
                                   "message": saved["history_error"]})
                        saved["sequence"] = len(self.events) + 1
            if terminal and event.get("type") == "result":
                saved["issues"] = list(self.error_history.issues.values())
            self.events.append(saved)
            if not terminal:
                update = self.error_history.observe(saved)
                if update:
                    self.emit(update)
            self.issues = list(self.error_history.issues.values())
            if event.get("type") in {"result", "error"}:
                self.terminal = True
            self.condition.notify_all()
            if event.get("type") == "trace":
                status = self.progress.receive(event)
                if status:
                    self.emit(status)

    def cancel(self) -> bool:
        with self.condition:
            if self.done or self.terminal:
                return False
            if not self.cancellation.event.is_set():
                self.cancellation.cancel()
                self.emit({"type": "trace", "kind": "cancellation_requested", "message": "Stopping research…"})
            return True


class ResearchServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], log_dir: Path | None = None,
                 store: ChatStore | None = None) -> None:
        super().__init__(address, ResearchHandler)
        self.run_lock = threading.Lock()
        self.runs_lock = threading.Lock()
        self.runs: dict[str, ResearchRun] = {}
        self.log_dir = log_dir
        self.store = store

    def find_run(self, run_id: str) -> ResearchRun | None:
        run = self.runs.get(run_id)
        if run is None and self.store:
            chat = self.store.get(run_id)
            if chat:
                run = ResearchRun(chat["id"], chat["question"])
                run.events = chat["events"]
                run.done = True
        return run

    def research(self, run: ResearchRun, backend: KimiBackend, config: Config,
                 search: SearchRouter) -> None:
        def trace(event: dict) -> None:
            run.emit({**event, "type": "trace", "research_id": run.id})

        backend.trace = trace
        backend.cancellation = run.cancellation
        agent = None
        try:
            trace(run_metadata(config))
            run.emit({"type": "progress", "message": "Starting research"})
            agent = ResearchAgent(backend, config, search,
                                  progress=lambda message: run.emit({"type": "progress", "message": message}),
                                  trace=trace, cancellation=run.cancellation)
            state, report = agent.run(run.question)
            with run.condition:
                if run.cancellation.event.is_set() and state.stop_reason != "cancelled":
                    state.stop_reason = "cancelled"
                    state.status = "CANCELLED"
                    state.failure = None
                    state.warnings.append("Research stopped by the user.")
                    report = render(state)
                # Only rendered, citation-validated report content enters this channel.
                # No artificial typing delay; clients render blocks as they arrive.
                if state.stop_reason != "cancelled":
                    run.emit({"type": "answer_start"})
                    for index, block in enumerate(report.splitlines(keepends=True)):
                        run.emit({"type": "answer_delta", "chunk_index": index, "text": block})
                run.emit({"type": "result", "report": report, "stop_reason": state.stop_reason,
                          "elapsed_seconds": state.elapsed_seconds, "sources": len(state.sources),
                          "api_calls": state.api_calls, "search_calls": state.search_calls,
                          "tokens_used": state.tokens_used,
                          "session_id": state.session_id, "facts_path": state.facts_path,
                          "total_cost_usd": state.total_cost_usd, "cost_complete": state.cost_complete,
                          "status": state.status, "quality_status": state.quality_status, "quality_evaluation": state.quality_evaluation.model_dump() if state.quality_evaluation else None, "report_status": state.report_status, "citation_validation_status": state.citation_validation_status,
                          "stop_explanation": research_outcome(state), "warnings": state.warnings,
                          "unresolved": list(dict.fromkeys([*state.unresolved,
                              *(state.judge.critical_gaps if state.judge else []),
                              *(state.verification.gaps if state.verification else [])])),
                          "issues": list(run.error_history.issues.values()),
                          "failure": state.failure.model_dump() if state.failure else None})
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider="agent", stage=agent.stage if agent else "initialize",
                                                operation="run")
            trace({"kind": "server_error", "diagnostic": diagnostic.model_dump()})
            run.emit({"type": "error", "message": diagnostic.message, "failure": diagnostic.model_dump()})
        finally:
            try:
                try:
                    if run.cancellation.event.is_set():
                        close_stream = getattr(backend, "close_stream", None)
                        if callable(close_stream):
                            close_stream()
                finally:
                    backend.client.close()
            except Exception as exc:
                # Cleanup must not erase a ready report or expose exception bodies.
                if run.journal:
                    run.journal.record({"kind": "cleanup_error", "diagnostic": failure_from_exception(
                        exc, provider=config.provider, stage="cleanup", operation="client_close").model_dump()})
            finally:
                # Finish cleanup before readers receive end-of-stream.
                self.run_lock.release()
                with run.condition:
                    run.done = True
                    run.condition.notify_all()


class ResearchHandler(BaseHTTPRequestHandler):
    server: ResearchServer

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, message: str) -> None:
        self._json_body(status, {"error": message})

    def _json_body(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self._headers(status, "application/json", len(body))
        self.wfile.write(body)

    def _local_request(self) -> bool:
        # Reject foreign origins and DNS rebinding against this local, credentialed API.
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (origin is None or origin == f"http://{host}")

    def do_GET(self) -> None:
        if not self._local_request():
            self._json(403, "Open the UI using its localhost address.")
            return
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/documents/"):
            try:
                chat_id = str(UUID(parsed.path.removeprefix("/api/documents/")))
                if self.server.store is None:
                    self._json(503, "Server document storage unavailable; edits remain on this device.")
                elif self.server.store.get(chat_id) is None:
                    self._json(404, "Chat not found.")
                else:
                    self._json_body(200, {"document": self.server.store.get_document(chat_id)})
            except ValueError:
                self._json(400, "Invalid document ID.")
            except Exception:
                self._json(503, "Document storage unavailable; local edits are retained.")
            return
        if parsed.path == "/api/models":
            provider = parse_qs(parsed.query, keep_blank_values=True).get("provider", ["aliyun"])[0]
            try:
                config = Config.from_env(provider=provider)
                api_key = require_api_key(config.provider)
            except ValueError as exc:
                self._json(400, str(exc))
                return
            try:
                models = list_models(config, api_key)
                self._json_body(200, {"provider": provider, "models": models})
            except Exception as exc:
                failure = failure_from_exception(exc, provider=provider, stage="models", operation="list_models")
                self._json_body(502, {"error": f"Could not fetch {provider.title()} models. {failure.message}",
                                      "failure": failure.model_dump()})
            return
        if parsed.path == "/api/chats" or parsed.path.startswith("/api/chats/"):
            if self.server.store is None:
                self._json(503, "Chat history requires PostgreSQL. Run scripts/start_postgres.py and restart the UI.")
                return
            try:
                if parsed.path == "/api/chats":
                    offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                    if offset < 0:
                        raise ValueError
                    self._json_body(200, self.server.store.list(offset))
                else:
                    chat_id = str(UUID(parsed.path.removeprefix("/api/chats/")))
                    with self.server.runs_lock:
                        chat = self.server.store.get(chat_id)
                    if chat is None:
                        self._json(404, "Chat not found.")
                    else:
                        self._json_body(200, chat)
            except ValueError:
                self._json(400, "Invalid chat ID or history cursor.")
            except Exception:
                self._json(503, "Chat history is unavailable. Check that PostgreSQL is running.")
            return
        if parsed.path.startswith("/api/research/"):
            try:
                run_id = str(UUID(parsed.path.removeprefix("/api/research/")))
            except ValueError:
                self._json(404, "Research run not found.")
                return
            try:
                with self.server.runs_lock:
                    run = self.server.find_run(run_id)
            except Exception:
                self._json(503, "Chat history is unavailable. Check PostgreSQL.")
                return
            if run is None:
                self._json(404, "This research run is no longer available. Start a new run.")
                return
            try:
                values = parse_qs(parsed.query, keep_blank_values=True)
                after = int(values.get("after", ["0"])[0])
                if after < 0 or after > (run.events[-1]["sequence"] if run.events else 0):
                    raise ValueError
            except ValueError:
                self._json(400, "Invalid research trace cursor.")
                return
            self._stream(run, after)
            return
        if self.path == "/api/status":
            missing = []
            try:
                config = Config.from_env()
                configuration_valid = True
            except ValueError:
                config = Config()
                configuration_valid = False
            for name, reader in ((("ALIYUN_API_KEY" if config.provider == "aliyun" else "KIMI_API_KEY"),
                                  lambda: require_api_key(config.provider)), ("TAVILY_API_KEY", require_tavily_key)):
                try:
                    reader()
                except ValueError:
                    missing.append(name)
            providers = []
            for provider, label, fallback_model in (("kimi", "Kimi / Moonshot", "kimi-k3"),
                                                     ("aliyun", "AliYun / Model Studio", Config.model)):
                try:
                    require_api_key(provider)
                    available = True
                except ValueError:
                    available = False
                try:
                    default_model = Config.from_env(provider=provider).model
                except ValueError:
                    default_model = fallback_model
                providers.append({"id": provider, "label": label, "configured": available,
                                  "default_model": default_model,
                                  "models": []})
            # Presence only: never return credentials or contact a paid API here.
            self._json_body(200, {"configured": not missing and configuration_valid,
                                  "missing_keys": missing,
                                  "configuration_valid": configuration_valid,
                                  "diagnostics_enabled": self.server.log_dir is not None,
                                  "history_enabled": self.server.store is not None,
                                  "provider": config.provider, "model": config.model, "providers": providers,
                                  "busy": self.server.run_lock.locked()})
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/editor.js": ("editor.js", "text/javascript; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        asset = files.get(self.path)
        if asset is None:
            self._json(404, "Not found.")
            return
        body = (ASSETS / asset[0]).read_bytes()
        self._headers(200, asset[1], len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._local_request():
            self._json(403, "Research requests must come from the local UI.")
            return
        if self.path.startswith("/api/documents/"):
            try:
                chat_id = str(UUID(self.path.removeprefix("/api/documents/")))
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    self._json(413, "Document exceeds the 2 MB save limit.")
                    return
                data = json.loads(self.rfile.read(length))
                if (not isinstance(data, dict) or not isinstance(data.get("content"), dict)
                        or data["content"].get("type") != "doc" or not isinstance(data.get("markdown"), str)
                        or type(data.get("revision")) is not int or data["revision"] < 0):
                    raise ValueError
                if self.server.store is None:
                    self._json(503, "Server document storage unavailable; edits remain on this device.")
                elif self.server.store.get(chat_id) is None:
                    self._json(404, "Chat not found.")
                else:
                    saved = self.server.store.save_document(chat_id, data["content"], data["markdown"], data["revision"])
                    if saved is None:
                        self._json(409, "A newer document is saved. Copy or export your edits before reloading.")
                    else:
                        self._json_body(200, saved)
            except (ValueError, TypeError):
                self._json(400, "Invalid document.")
            except Exception:
                self._json(503, "Document save unavailable; local edits are retained.")
            return
        if self.path.startswith("/api/research/") and self.path.endswith("/stop"):
            try:
                run_id = str(UUID(self.path.removeprefix("/api/research/").removesuffix("/stop")))
                with self.server.runs_lock:
                    run = self.server.find_run(run_id)
                if run is None:
                    self._json(404, "Research run not found.")
                    return
                stopping = run.cancel()
                self._json_body(200, {"run_id": run.id, "status": "stopping" if stopping else "finished"})
            except ValueError:
                self._json(400, "Invalid run ID.")
            except Exception:
                self._json(503, "Could not stop this research run. Check the server.")
            return
        if self.path != "/api/research":
            self._json(404, "Not found.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32_768:
                raise ValueError("Request must contain a question of at most 8,000 characters.")
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length))
            question = payload.get("question") if isinstance(payload, dict) else None
            if not isinstance(question, str) or not question.strip() or len(question) > 8_000:
                raise ValueError("Enter a research question of 1–8,000 characters.")
            if "run_id" in payload and not isinstance(payload["run_id"], str):
                raise ValueError("Invalid run ID.")
            run_id = str(UUID(payload["run_id"])) if "run_id" in payload else str(uuid4())
            for name in ("provider", "model"):
                if name in payload and (not isinstance(payload[name], str) or not payload[name].strip()):
                    raise ValueError(f"Invalid {name}.")
        except (ValueError, OSError):
            self._json(400, "Enter a research question of 1–8,000 characters.")
            return
        # Repeating a POST after a network error must not start a second paid run.
        with self.server.runs_lock:
            try:
                run = self.server.find_run(run_id)
            except Exception:
                self._json(503, "Could not access chat history. Check PostgreSQL before sending again.")
                return
            if run is not None:
                if run.question != question.strip():
                    self._json(409, "This run ID already belongs to a different research question.")
                    return
                if run.config and any(name in payload and payload[name] != getattr(run.config, name)
                                      for name in ("provider", "model")):
                    self._json(409, "This run ID already uses a different provider or model.")
                    return
            else:
                if not self.server.run_lock.acquire(blocking=False):
                    self._json(409, "Research is already running. Wait for it to finish.")
                    return
                backend = None
                try:
                    config = Config.from_env(provider=payload.get("provider"), model=payload.get("model"))
                    api_key, tavily_key = require_api_key(config.provider), require_tavily_key()
                except ValueError as exc:
                    self.server.run_lock.release()
                    self._json(400, str(exc))
                    return
                try:
                    search = SearchRouter(TavilyMCPSearch(config.tavily_mcp_url, tavily_key))
                    backend_class = AliyunBackend if config.provider == "aliyun" else KimiBackend
                    backend = backend_class(config, api_key)
                    journal = DiagnosticJournal(self.server.log_dir / f"{run_id}.jsonl", run_id) if self.server.log_dir else None
                    if self.server.store:
                        self.server.store.create(run_id, question.strip())
                    run = ResearchRun(run_id, question.strip(), journal, self.server.store)
                    run.config = config
                    # Bound memory usage while retaining recent reports for reconnects.
                    completed = [key for key, value in self.server.runs.items() if value.done]
                    for key in completed[:max(0, len(self.server.runs) - RETAINED_RUNS + 1)]:
                        del self.server.runs[key]
                    self.server.runs[run_id] = run
                    threading.Thread(target=self.server.research, args=(run, backend, config, search),
                                     daemon=True).start()
                except Exception:
                    self.server.run_lock.release()
                    self.server.runs.pop(run_id, None)
                    if backend is not None:
                        backend.client.close()
                    self._json(500, "Could not initialize the research agent. Check the server configuration.")
                    return
        self._stream(run)

    def _stream(self, run: ResearchRun, after: int = 0) -> None:
        self._headers(200, "application/x-ndjson; charset=utf-8")
        self.connection.settimeout(None)
        try:
            while True:
                with run.condition:
                    if after == (run.events[-1]["sequence"] if run.events else 0) and not run.done:
                        run.condition.wait(HEARTBEAT_SECONDS)
                    events = [event for event in run.events if event["sequence"] > after]
                    done = run.done
                if events:
                    for event in events:
                        self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                        if event.get("type") == "answer_delta":
                            self.wfile.flush()
                    after = events[-1]["sequence"]
                elif not done:
                    # Keep long searches/model waits active through local proxies.
                    self.wfile.write(b'{"type":"heartbeat"}\n')
                self.wfile.flush()
                if done:
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Research and its final report remain available for reconnection.
            if run.journal:
                run.journal.record({"kind": "stream_disconnected", "operation": "ui_stream", "stage": "connection"})
            return

    def log_message(self, format: str, *args: object) -> None:
        # Avoid recording research questions or provider responses in request logs.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a simple local research UI.")
    parser.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    parser.add_argument("--log-dir", type=Path, default=Path("reports/logs"), help="Diagnostic JSONL log directory")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        store = ChatStore(database_url())
        store.initialize()
        server = ResearchServer(("127.0.0.1", args.port), log_dir=args.log_dir, store=store)
        store.interrupt_unfinished()
    except OSError as exc:
        parser.exit(2, f"Could not start the UI: {exc}\n")
    except Exception:
        parser.exit(2, "Could not connect to PostgreSQL. Run `python scripts/start_postgres.py` "
                    "or configure DATABASE_URL in .env, then restart the UI.\n")
    print(f"Deep Research → http://127.0.0.1:{args.port}", flush=True)
    print(f"Diagnostic logs → {args.log_dir}", flush=True)
    print("Chat history → PostgreSQL", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
