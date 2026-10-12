"""Orchestrator-owned evidence consolidation and atomic human-readable snapshots."""

import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .models import Finding, ResearchNote, ResearchTask


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".snapshot-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EvidenceStore:
    def __init__(self, state, root):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", state.session_id):
            raise ValueError("Session IDs must be safe single-directory identifiers.")
        self.state = state
        self.directory = Path(root) / state.session_id
        self.directory.mkdir(parents=True, exist_ok=False)
        state.facts_path = str((self.directory / "research_facts.md").resolve())
        self.persist()

    def add_result(self, query, result):
        state = self.state
        by_url = {canonical_url(s.url): s for s in state.sources}
        for source in result.sources:
            key = canonical_url(source.url)
            if key not in by_url:
                source.id = f"S{len(state.sources) + 1}"
                state.sources.append(source)
                by_url[key] = source
            stored = by_url[key]
            source.id = stored.id
            if query.id not in stored.query_ids:
                stored.query_ids.append(query.id)
        for document in result.documents:
            source = by_url.get(canonical_url(document.get("url", "")))
            if source:
                document["source_id"] = source.id
                document["query_id"] = query.id
        state.notes.append(ResearchNote(task=ResearchTask(question=", ".join(query.requirement_ids), query=query.query), result=result))

    def merge(self, extraction, requirement_ids, results, query_ids):
        state = self.state
        allowed = {s.id: s for result in results for s in result.sources}
        documents = {sid: [] for sid in allowed}
        for result in results:
            for document in result.documents:
                sid = document.get("source_id")
                if sid in documents:
                    documents[sid].append(document.get("content", ""))
        existing = {(f.requirement_id, " ".join(f.claim.casefold().split()), f.source_id): f for f in state.findings}
        added = 0
        for item in extraction.findings:
            source = allowed.get(item.source_id)
            if item.requirement_id not in requirement_ids or source is None or canonical_url(item.source_url) != canonical_url(source.url):
                state.warnings.append("Dropped a finding with an unknown requirement or source reference.")
                continue
            excerpt = item.supporting_excerpt.strip()
            # Evidence must be an actual excerpt from the particular cited source.
            matched = bool(excerpt) and any(" ".join(excerpt.split()) in " ".join(content.split()) for content in documents[source.id])
            status = item.evidence_status
            if status in {"SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED"} and not matched:
                status = "UNVERIFIED"
                state.warnings.append("An unmatched source excerpt was marked UNVERIFIED.")
            key = (item.requirement_id, " ".join(item.claim.casefold().split()), source.id)
            if key in existing:
                stored = existing[key]
                strength = {"UNVERIFIED": 0, "CONTRADICTED": 0, "PARTIALLY_SUPPORTED": 1, "SUPPORTED": 2}
                if strength[status] > strength[stored.evidence_status]:
                    stored.evidence, stored.evidence_status = excerpt, status
                    stored.limitations = item.limitations
                    stored.query_ids = list(dict.fromkeys(stored.query_ids + [qid for qid in query_ids if qid in source.query_ids]))
                    added += 1
                continue
            # Stable globally assigned IDs; worker-proposed claim IDs never become routing authority.
            state.findings.append(Finding(id=f"F{len(state.findings) + 1}", task_question=item.requirement_id,
                requirement_id=item.requirement_id, claim=item.claim, evidence=excerpt if matched else "",
                source_id=source.id, source_url=source.url, publication_date=source.published_at,
                evidence_status=status, limitations=item.limitations,
                query_ids=[qid for qid in query_ids if qid in source.query_ids]))
            existing[key] = state.findings[-1]
            added += 1
        state.contradictions = list(dict.fromkeys(state.contradictions + extraction.contradictions))
        state.unresolved = list(dict.fromkeys(state.unresolved + extraction.uncertainties))
        state.evidence_version += 1
        self.persist()
        return added

    def persist(self):
        state = self.state
        requirements = getattr(state.plan, "research_requirements", [])
        lines = ["# Research facts", "", f"Session: {state.session_id}", f"Evidence version: {state.evidence_version}",
                 "", "## Original question", state.question, "", "## Current research status", state.status,
                 f"Stop reason: {state.stop_reason}", "", "## Research requirements"]
        lines += [f"- {r.id} (critical={r.critical}, weight={r.weight}): {r.question}" for r in requirements]
        lines += ["", "## Key findings and evidence excerpts"]
        for finding in state.findings:
            lines += [f"- {finding.id} / {finding.requirement_id} / {finding.evidence_status}: {finding.claim}",
                      f"  Source: {finding.source_id} ({finding.source_url})", f"  Excerpt: {finding.evidence}",
                      f"  Limitations: {'; '.join(finding.limitations)}"]
        lines += ["", "## Supporting sources"]
        lines += [f"- {s.id}: {s.title} — {s.url}; published={s.published_at or 'unknown'}; retrieved={s.retrieved_at}; queries={','.join(s.query_ids)}" for s in state.sources]
        lines += ["", "## Contradictions", *[f"- {v}" for v in state.contradictions], "", "## Missing information",
                  *[f"- {v}" for v in state.unresolved], "", "## Research-round history", "```json",
                  json.dumps(state.round_history, ensure_ascii=False, indent=2), "```", "", "## Latest classifier evaluation", "```json",
                  state.judge.model_dump_json(indent=2) if state.judge else "null", "```", "", "## Execution metadata", "```json",
                  json.dumps({"api_calls": state.api_calls, "search_calls": state.search_calls, "tokens_used": state.tokens_used,
                              "total_cost_usd": state.total_cost_usd, "cost_complete": state.cost_complete,
                              "token_usage_complete": state.token_usage_complete,
                              "elapsed_seconds": state.elapsed_seconds, "warnings": state.warnings}, indent=2), "```", ""]
        atomic_write(state.facts_path, "\n".join(lines))
        atomic_write(self.directory / "state.json", state.model_dump_json(indent=2) + "\n")

    def save_evaluation(self, snapshot, decision, provider, model, advisory=None):
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        record = {"evidence_version": self.state.evidence_version, "snapshot_sha256": digest,
                  "provider": provider, "model": model, "decision": decision.model_dump(),
                  "advisory_decision": advisory or decision.model_dump()}
        self.state.judge_history.append(record)
        atomic_write(self.directory / f"evaluation-{len(self.state.judge_history)}.json",
                     json.dumps({**record, "snapshot": snapshot}, ensure_ascii=False, indent=2))
        return record
