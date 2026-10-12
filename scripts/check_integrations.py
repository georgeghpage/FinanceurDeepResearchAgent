"""Opt-in live provider probes and a small, logged research reproduction.

Run from the project directory with PYTHONPATH=src. This script uses paid APIs;
the unittest suite never imports it or makes live provider requests.
"""

import argparse
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from deepsearchagent.config import Config, require_api_key, require_tavily_key
from deepsearchagent.diagnostics import DiagnosticJournal, failure_from_exception, run_metadata
from deepsearchagent.llm import AliyunBackend, KimiBackend
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from deepsearchagent.tools.tavily import TavilyMCPSearch


class APIProbe(BaseModel):
    ok: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", action="store_true", help="Also run the default 15-query multi-agent flow (uses paid APIs)")
    parser.add_argument("--direct-tavily", action="store_true", help="Ignore system/environment proxy settings for Tavily in this test only")
    parser.add_argument("--log-dir", type=Path, default=Path("reports/diagnostics"))
    args = parser.parse_args()
    config = Config.from_env()
    model_key, tavily_key = require_api_key(config.provider), require_tavily_key()
    backend_class = AliyunBackend if config.provider == "aliyun" else KimiBackend
    run_id = str(uuid4())
    base = args.log_dir / run_id
    journal = DiagnosticJournal(base.with_suffix(".jsonl"), run_id)
    passed = True

    def trace(event):
        failed_before = journal.failed
        if not journal.record(event) and not failed_before:
            print(json.dumps({"check": "logging", "ok": False, "message": "Could not save diagnostic log."}), flush=True)

    trace(run_metadata(config))
    backend = backend_class(replace(config, request_timeout=45, max_seconds=60, max_output_tokens=256), model_key, trace=trace)
    try:
        probe = backend.structured(APIProbe, "API connectivity check. Return JSON with ok set to true.", 'Return {"ok":true}.')
        passed = passed and probe.ok
        print(json.dumps({"check": config.provider, "ok": probe.ok, "tokens": backend.tokens_used}), flush=True)
    except Exception as exc:
        passed = False
        failure = backend.last_failure or failure_from_exception(exc, provider=config.provider, stage="probe", operation="chat_completion")
        print(json.dumps({"check": config.provider, "ok": False, "failure": failure.model_dump()}), flush=True)
    finally:
        backend.client.close()

    web = TavilyMCPSearch(config.tavily_mcp_url, tavily_key, trust_env=not args.direct_tavily)
    web.trace = trace
    try:
        result = web.search("site:docs.tavily.com Tavily MCP", timeout=45)
        passed = passed and bool(result.sources)
        print(json.dumps({"check": "tavily", "ok": bool(result.sources), "sources": len(result.sources), "trust_env": web.trust_env}), flush=True)
    except Exception as exc:
        passed = False
        failure = failure_from_exception(exc, provider="tavily", stage="probe", operation="search")
        print(json.dumps({"check": "tavily", "ok": False, "failure": failure.model_dump(), "trust_env": web.trust_env}), flush=True)

    if args.research:
        config = replace(config, max_rounds=0, max_searches=15, max_calls=10, max_seconds=300)
        trace(run_metadata(config))
        backend = backend_class(config, model_key, trace=trace)
        try:
            question = "Connectivity smoke test: What is Tavily MCP? Establish its official documented purpose and how it exposes search. Return a short report with source citations."
            state, report = ResearchAgent(backend, config, SearchRouter(web), trace=trace,
                progress=lambda message: print(json.dumps({"check": "research_progress", "message": message}), flush=True)).run(question)
            passed = passed and state.stop_reason == "coverage"
            args.log_dir.mkdir(parents=True, exist_ok=True)
            base.with_suffix(".state.json").write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
            base.with_suffix(".md").write_text(report, encoding="utf-8")
            print(json.dumps({"check": "research", "ok": state.stop_reason == "coverage", "stop_reason": state.stop_reason,
                              "failure": state.failure.model_dump() if state.failure else None,
                              "sources": len(state.sources), "search_calls": state.search_calls,
                              "api_calls": state.api_calls, "tokens": state.tokens_used,
                              "seconds": state.elapsed_seconds}), flush=True)
        finally:
            backend.client.close()
    print(json.dumps({"diagnostic_log": str(journal.path), "log_saved": not journal.failed}), flush=True)
    raise SystemExit(0 if passed and not journal.failed else 1)


if __name__ == "__main__":
    main()
