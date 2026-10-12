"""Command-line interface for live research and saved evidence state."""

import argparse
import sys
from pathlib import Path
from uuid import uuid4

from .config import Config, require_api_key, require_tavily_key
from .diagnostics import DiagnosticJournal, run_metadata
from .llm import AliyunBackend, KimiBackend
from .orchestrator import ResearchAgent
from .tools.search import SearchRouter
from .tools.tavily import TavilyMCPSearch


def main() -> None:
    parser = argparse.ArgumentParser(description="Research a question and produce a source-backed Markdown report.")
    parser.add_argument("question", help="Research question (quote it if it contains spaces)")
    parser.add_argument("--provider", choices=["kimi", "aliyun"], help="Cloud provider (default: LLM_PROVIDER or aliyun)")
    parser.add_argument("--model", help="Model ID for the selected provider")
    parser.add_argument("--base-url", help="Override the selected provider's API base URL")
    parser.add_argument("--max-rounds", type=int, help="Maximum additional research rounds (initial round always runs)")
    parser.add_argument("--max-tasks", type=int, help="Maximum targeted queries per additional round (up to five)")
    parser.add_argument("--max-calls", type=int, help="Total model-attempt ceiling")
    parser.add_argument("--max-searches", type=int, help="Maximum search attempts, including retries")
    parser.add_argument("--max-tokens", type=int, help="Total reserved/observed model token ceiling")
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-cost-usd", type=float, help="USD ceiling; requires configured prices for every model")
    parser.add_argument("--session-dir", help="Parent directory for isolated research_facts.md artifacts")
    parser.add_argument("--judge-model", help="Classifier model, independent of the research model")
    parser.add_argument("--output", type=Path, help="Save Markdown here instead of printing it")
    parser.add_argument("--state", type=Path, help="Save the research plan, evidence, sources, and usage as JSON")
    parser.add_argument("--log", type=Path, help="Diagnostic JSONL file (default: reports/logs/<run-id>.jsonl)")
    parser.add_argument("--quiet", action="store_true", help="Hide progress messages")
    args = parser.parse_args()
    try:
        if not args.question.strip():
            raise ValueError("Research question must not be empty.")
        destinations = [path.resolve() for path in (args.output, args.state, args.log) if path]
        if len(set(destinations)) != len(destinations):
            raise ValueError("--output, --state and --log must use different paths.")
        provider = args.provider or Config.from_env().provider
        endpoint = {"aliyun_base_url" if provider == "aliyun" else "kimi_base_url": args.base_url}
        config = Config.from_env(
            provider=provider, model=args.model, **endpoint, max_rounds=args.max_rounds, max_tasks=args.max_tasks,
            max_calls=args.max_calls, max_tokens=args.max_tokens, max_seconds=args.max_seconds,
            max_searches=args.max_searches,
            max_cost_usd=args.max_cost_usd, session_dir=args.session_dir, judge_model=args.judge_model,
        )
        api_key, tavily_key = require_api_key(config.provider), require_tavily_key()
        run_id = str(uuid4())
        journal = DiagnosticJournal(args.log or Path("reports/logs") / f"{run_id}.jsonl", run_id)

        def log(event):
            failed_before = journal.failed
            if not journal.record(event) and not failed_before:
                print("Could not save the diagnostic log.", file=sys.stderr)

        log(run_metadata(config))
        search = SearchRouter(TavilyMCPSearch(config.tavily_mcp_url, tavily_key))
        backend_class = AliyunBackend if config.provider == "aliyun" else KimiBackend
        backend = backend_class(config, api_key, diagnostic_trace=log)
        progress = None if args.quiet else lambda message: print(message, file=sys.stderr)
        try:
            state, report = ResearchAgent(backend, config, search, progress, trace=log).run(args.question)
        finally:
            backend.client.close()
        if state.failure:
            failure = state.failure
            print(f"Stopped: {failure.provider} / {failure.stage} / {failure.category}: {failure.message}", file=sys.stderr)
        if not args.quiet:
            print(f"Diagnostic log: {journal.path}", file=sys.stderr)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report, encoding="utf-8")
            if not args.quiet:
                print(f"Report saved to {args.output}", file=sys.stderr)
        else:
            print(report, end="")
        if args.state:
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    if state.stop_reason == "error":
        raise SystemExit(1)
