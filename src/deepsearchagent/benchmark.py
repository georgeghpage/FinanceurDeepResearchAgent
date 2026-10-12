"""Human-reviewed classifier benchmarks; synthetic fixtures are never human labels."""

import argparse
import json
import statistics
import time
from pathlib import Path

from .budget import RunBudget, stage_prompt, token_bound
from .config import Config
from .diagnostics import failure_from_exception
from .evidence_store import atomic_write
from .judge import OpenAICompatibleJudge, ready_eligible, validate_decision
from .models import Finding, JudgeDecision, Requirement, Source

LABELS = {"READY", "RESEARCH_MORE", "INSUFFICIENT_EVIDENCE"}
CATEGORIES = ("complete", "partial", "missing_critical", "contradictory", "unsupported",
              "irrelevant_extensive", "outdated", "unavailable", "low_marginal_gain")


def read_jsonl(path):
    records = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    ids = [r["id"] for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError("Benchmark IDs must be unique")
    return records


def review_queue(count=225):
    if not 200 <= count <= 500:
        raise ValueError("Initial review queue must contain 200–500 snapshots")
    for index in range(count):
        category = CATEGORIES[index % len(CATEGORIES)]
        entity = f"Synthetic Organization {index+1}"
        requirements = [Requirement(id="R1", question=f"Establish {entity}'s 2026 metric and units", critical=True, weight=1),
                        Requirement(id="R2", question="Establish the measurement method", critical=True, weight=1)]
        sources = [Source(id="S1", url=f"https://example.com/synthetic/{index+1}", title="Synthetic official record",
                          published_at="2026-10-01", retrieved_at="2026-10-12T00:00:00+00:00")]
        findings = [Finding(id="F1", task_question="R1", requirement_id="R1", claim=f"{entity}'s metric was {index+10} units in 2026.",
                            evidence=f"2026 metric: {index+10} units.", source_id="S1", source_url=sources[0].url),
                    Finding(id="F2", task_question="R2", requirement_id="R2", claim="Metric uses an annual census.",
                            evidence="Method: annual census.", source_id="S1", source_url=sources[0].url)]
        contradictions, unresolved, history = [], [], []
        if category in {"partial", "missing_critical", "unavailable", "low_marginal_gain"}:
            findings = findings[:1]
            unresolved = ["Measurement method not established."]
        if category == "partial":
            findings[0].evidence_status = "PARTIALLY_SUPPORTED"
            findings[0].limitations = ["Only first-quarter observations available."]
        if category == "contradictory":
            sources.append(Source(id="S2", url=f"https://example.com/synthetic/contradiction/{index+1}",
                title="Synthetic independent record", published_at="2026-10-02", retrieved_at="2026-10-12T00:00:00+00:00"))
            findings.append(Finding(id="F3", task_question="R1", requirement_id="R1", claim="Metric was zero units in 2026.",
                evidence="2026 metric: zero units.", source_id="S2", evidence_status="CONTRADICTED"))
            contradictions = ["Records disagree on the same entity, period and units."]
        if category == "unsupported":
            findings[0].evidence = ""
            findings[0].evidence_status = "UNVERIFIED"
        if category == "irrelevant_extensive":
            findings = [Finding(id=f"F{i}", task_question="R1", requirement_id="R1", claim=f"Unrelated historical event {i} occurred.",
                evidence=f"Historical event {i}.", source_id="S1", limitations=["Unrelated to requested metrics."]) for i in range(1, 13)]
        if category == "outdated":
            sources[0].published_at = "2020-01-01"
            findings[0].claim = f"{entity}'s metric was {index+10} units in 2019."
            findings[0].evidence = f"2019 metric: {index+10} units."
        if category == "unavailable":
            unresolved.append("Synthetic public notice: methodology is confidential and not publicly released.")
        if category == "low_marginal_gain":
            history = [f"methodology targeted search attempt {i}" for i in range(3)]
            unresolved.append("Repeated targeted retrieval returned no method evidence.")
        yield {"id": f"snapshot-{index+1:03}", "category": category, "origin": "synthetic_review_fixture",
               "question": f"What was {entity}'s metric in 2026 and how was it measured?",
               "requirements": [r.model_dump() for r in requirements],
               "evidence": {"findings": [f.model_dump() for f in findings], "sources": [s.model_dump() for s in sources],
                            "contradictions": contradictions, "unresolved_questions": unresolved, "evidence_version": 1},
               "research_state": {"round": 1 + len(history), "previously_executed_queries": history, "remaining_budget": {}},
               "human_decision": None, "human_gap_requirement_ids": None, "reviewer": None,
               "review_notes": "", "follow_up_query_usefulness": None}


def score(dataset, predictions):
    gold = {record["id"]: record for record in dataset if record.get("human_decision") is not None}
    if not gold:
        raise ValueError("No human-labeled snapshots. Annotate human_decision, reviewer and gap IDs first.")
    if any(record["human_decision"] not in LABELS or not record.get("reviewer") or
           not isinstance(record.get("human_gap_requirement_ids"), list) for record in gold.values()):
        raise ValueError("Each human label requires a valid decision, reviewer and gap requirement IDs")
    predicted = {record["id"]: record for record in predictions}
    pairs = [(g, predicted[identifier]) for identifier, g in gold.items() if identifier in predicted]
    valid = [(g, p) for g, p in pairs if p.get("decision") in LABELS]
    if not valid:
        raise ValueError("No valid predictions match reviewed snapshots")
    negatives = [(g, p) for g, p in valid if g["human_decision"] != "READY"]
    ready = [(g, p) for g, p in valid if g["human_decision"] == "READY"]
    approvals = [(g, p) for g, p in valid if p["decision"] == "READY"]
    ratio = lambda numerator, denominator: numerator / denominator if denominator else None
    tp = fp = fn = 0
    for g, p in valid:
        actual, found = set(g["human_gap_requirement_ids"]), set(p.get("gap_requirement_ids", []))
        tp += len(actual & found)
        fp += len(found - actual)
        fn += len(actual - found)
    query_labels = [value for _, p in valid for value in (p.get("follow_up_query_usefulness") or [])]
    if any(type(v) is not bool for v in query_labels):
        raise ValueError("Follow-up query usefulness labels must be booleans")
    latencies = sorted(p["latency_seconds"] for _, p in pairs if p.get("latency_seconds") is not None)
    costs = [p["cost_usd"] for _, p in pairs if p.get("cost_usd") is not None]
    return {"human_labeled_count": len(gold), "matched_count": len(pairs), "valid_count": len(valid),
            "classification_failure_rate": ratio(len(pairs) - len(valid), len(pairs)),
            "false_ready_rate": ratio(sum(p["decision"] == "READY" for _, p in negatives), len(negatives)),
            "incorrect_approval_rate": ratio(sum(g["human_decision"] != "READY" for g, _ in approvals), len(approvals)),
            "false_research_more_rate": ratio(sum(p["decision"] == "RESEARCH_MORE" for _, p in ready), len(ready)),
            "decision_agreement": ratio(sum(g["human_decision"] == p["decision"] for g, p in valid), len(valid)),
            "gap_precision": ratio(tp, tp + fp), "gap_recall": ratio(tp, tp + fn),
            "gap_f1": ratio(2 * tp, 2 * tp + fp + fn),
            "follow_up_query_utility": ratio(sum(query_labels), len(query_labels)),
            "mean_latency_seconds": statistics.mean(latencies) if latencies else None,
            "p95_latency_seconds": latencies[min(len(latencies)-1, int(0.95 * len(latencies)))] if latencies else None,
            "total_tokens": sum(p.get("tokens", 0) for _, p in pairs),
            "mean_cost_usd": statistics.mean(costs) if costs else None,
            "priced_count": len(costs),
            "note": "Scores are estimates, not calibrated confidence probabilities. Compare models on the same reviewed IDs."}


def predict(records, config):
    judge = OpenAICompatibleJudge(config)
    try:
        for record in records:
            started = time.monotonic()
            budget = RunBudget(config)
            requirements = [Requirement.model_validate(r) for r in record["requirements"]]
            findings = [Finding.model_validate(f) for f in record["evidence"]["findings"]]
            sources = [Source.model_validate(s) for s in record["evidence"]["sources"]]
            prediction = {"id": record["id"], "provider": judge.provider, "model": judge.model}
            payload = judge.request_payload(record["question"], requirements, record["evidence"], record["research_state"])
            input_tokens = token_bound(stage_prompt(judge.prompt, JudgeDecision)) + token_bound(payload) + 64
            try:
                for attempt in range(config.judge_retries + 1):
                    reservation = budget.reserve_model(input_tokens, config.judge_output_tokens, judge.rates, writing=True)
                    try:
                        judge.timeout = min(config.judge_timeout, budget.remaining_time(True))
                        result = judge.evaluate(record["question"], requirements, record["evidence"], record["research_state"])
                        result = validate_decision(result, requirements, findings, sources)
                        eligible = ready_eligible(result, requirements, findings, sources, config.ready_coverage_threshold,
                                                  record["evidence"].get("contradictions", []))
                        prediction.update(decision=result.decision if result.decision != "READY" or eligible else "INSUFFICIENT_EVIDENCE",
                            advisory_decision=result.decision, ready_eligible=eligible,
                            gap_requirement_ids=[r.requirement_id for r in result.requirements if r.critical and r.status != "COVERED"],
                            evaluation=result.model_dump())
                        break
                    except Exception:
                        if attempt == config.judge_retries:
                            raise
                    finally:
                        budget.settle(reservation, judge.usage)
            except Exception as exc:
                prediction.update(decision=None, error=failure_from_exception(exc, provider=judge.provider, stage="benchmark", operation="classify").model_dump())
            prediction.update(latency_seconds=round(time.monotonic() - started, 4), tokens=budget.tokens,
                              cost_usd=budget.cost if budget.cost_complete else None)
            yield prediction
    finally:
        judge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    queue = sub.add_parser("prepare")
    queue.add_argument("output", type=Path)
    queue.add_argument("--count", type=int, default=225)
    run = sub.add_parser("predict")
    run.add_argument("dataset", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--provider", choices=["pai", "aliyun", "kimi"], default="pai")
    run.add_argument("--model", default="pai-judge")
    run.add_argument("--base-url")
    run.add_argument("--json-mode", choices=["json_object", "json_schema", "none"], default="json_object")
    metrics = sub.add_parser("score")
    metrics.add_argument("dataset", type=Path)
    metrics.add_argument("predictions", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        atomic_write(args.output, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in review_queue(args.count)))
        print(f"Prepared {args.count} unlabeled synthetic review fixtures in {args.output}. Human review is required.")
    elif args.command == "predict":
        config = Config.from_env(judge_provider=args.provider, judge_model=args.model,
                                 judge_base_url=args.base_url, judge_json_mode=args.json_mode)
        if args.provider != "pai" and not args.base_url:
            parser.error("Set --base-url to the verified compatible deployment for non-PAI models")
        records = read_jsonl(args.dataset)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as stream:
            for record in predict(records, config):
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
    else:
        print(json.dumps(score(read_jsonl(args.dataset), read_jsonl(args.predictions)), indent=2))


if __name__ == "__main__":
    main()
