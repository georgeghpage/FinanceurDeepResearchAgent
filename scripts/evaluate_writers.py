"""Compare 2–3 writers on one frozen evidence input; default is offline preparation."""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deepsearchagent.budget import RunBudget, StageBackend
from deepsearchagent.capabilities import model_limits
from deepsearchagent.config import Config, require_api_key
from deepsearchagent.diagnostics import failure_from_exception
from deepsearchagent.llm import AliyunBackend, KimiBackend
from deepsearchagent.models import ReportDraft, ResearchState
from deepsearchagent.writer import (
    SYNTHESIS_PROMPT,
    render,
    synthesis_payload,
    validate_draft,
)


def prepare(state, config, models, rates=None):
    rates = rates or {}
    if not 2 <= len(models) <= 3 or len(set(models)) != len(models):
        raise ValueError("Choose two or three distinct models.")
    configs = []
    for model in models:
        prices = rates.get(model, {})
        same = model == config.model
        configs.append(
            replace(
                config,
                model=model,
                research_model=None,
                final_writer_model=None,
                input_price_per_million=prices.get(
                    "input", config.input_price_per_million if same else None
                ),
                output_price_per_million=prices.get(
                    "output", config.output_price_per_million if same else None
                ),
                cached_price_per_million=None,
                cache_write_price_per_million=None,
                llm_retries=0,
                max_continuations=0,
                max_calls=1,
                writer_reserve_tokens=0,
            )
        )
    # The same input and output allocations are used by every candidate.
    output_limit = min(
        config.writer_output_tokens, *(model_limits(c)[1] for c in configs)
    )
    input_limit = min(
        config.writer_input_tokens,
        *(model_limits(c)[0] - output_limit - 128 for c in configs),
    )
    payload, allowed = synthesis_payload(state, input_limit)
    manifest = {
        "evidence_sha256": hashlib.sha256(state.model_dump_json().encode()).hexdigest(),
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "prompt_sha256": hashlib.sha256(SYNTHESIS_PROMPT.encode()).hexdigest(),
        "models": models,
        "input_limit": input_limit,
        "output_limit": output_limit,
        "allowed_finding_ids": sorted(allowed),
        "payload": json.loads(payload),
        "instructions": SYNTHESIS_PROMPT,
        "schema": ReportDraft.model_json_schema(),
    }
    return manifest, configs, payload, allowed


def compare(state, config, models, directory, *, factory=None, rates=None):
    manifest, configs, payload, allowed = prepare(state, config, models, rates)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "input.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    results = []
    if factory is not None:
        for index, candidate in enumerate(configs):
            budget, backend = RunBudget(candidate), None
            started = time.monotonic()
            result = {
                "sample": f"writer-{index + 1}",
                "model": candidate.model,
                "payload_sha256": manifest["payload_sha256"],
                "passed_references": False,
            }
            try:
                backend = factory(candidate)
                draft = StageBackend(
                    backend,
                    budget,
                    manifest["output_limit"],
                    manifest["input_limit"],
                    writing=True,
                    timeout=min(60, candidate.request_timeout),
                    max_continuations=0,
                ).structured(ReportDraft, SYNTHESIS_PROMPT, payload)
                validate_draft(state, draft, allowed)
                cited = {
                    fid
                    for section in draft.sections
                    for paragraph in section.paragraphs
                    for fid in paragraph.finding_ids
                }
                result.update(
                    passed_references=True,
                    finding_reference_recall=len(cited) / len(allowed)
                    if allowed
                    else None,
                )
                (directory / f"writer-{index + 1}.json").write_text(
                    draft.model_dump_json(indent=2) + "\n"
                )
                # Keep latency/model metadata out of the blinded review document.
                review_state = state.model_copy(
                    update={
                        "citation_validation_status": "references_only",
                        "warnings": [
                            *state.warnings,
                            "Writer comparison sample: semantic support and analytical quality require independent human review.",
                        ],
                    }
                )
                (directory / f"writer-{index + 1}.md").write_text(
                    render(review_state, draft), encoding="utf-8"
                )
            except Exception as exc:
                result["failure"] = failure_from_exception(
                    exc, provider=candidate.provider, stage="write", operation="compare"
                ).model_dump()
            finally:
                if backend is not None:
                    backend.client.close()
            result.update(
                seconds=round(time.monotonic() - started, 3),
                calls=budget.calls,
                tokens=budget.tokens + budget.pending_tokens,
                cost_usd=budget.cost if budget.cost_complete else None,
                usage_complete=budget.token_usage_complete,
            )
            results.append(result)
    scorecard = {
        "executed": factory is not None,
        "results": results,
        "factual_preservation": None,
        "instruction_following": None,
        "document_structure": None,
        "analytical_quality": None,
        "reviewer": None,
        "note": "Reference recall is a structural metric, not semantic accuracy. Human review is required.",
    }
    (directory / "scorecard.json").write_text(json.dumps(scorecard, indent=2) + "\n")
    return scorecard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--models", required=True, nargs="+")
    parser.add_argument(
        "--output", type=Path, default=Path("reports/writer-comparison") / uuid4().hex
    )
    parser.add_argument(
        "--rates",
        type=Path,
        help='Optional USD rates JSON: {"model":{"input":2,"output":6}}',
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly enable one paid synthesis request per model; no searches/retries/audits",
    )
    args = parser.parse_args()
    state = ResearchState.model_validate_json(args.state.read_text(encoding="utf-8"))
    rates = json.loads(args.rates.read_text()) if args.rates else None
    # Offline preparation does not read credentials or the workspace .env.
    config = Config.from_env() if args.live else Config()
    factory = None
    if args.live:
        backend_class = AliyunBackend if config.provider == "aliyun" else KimiBackend
        api_key = require_api_key(config.provider)
        factory = lambda candidate: backend_class(candidate, api_key)
    scorecard = compare(
        state, config, args.models, args.output, factory=factory, rates=rates
    )
    print(json.dumps({"output": str(args.output), "executed": scorecard["executed"]}))
    return int(any(not result["passed_references"] for result in scorecard["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
