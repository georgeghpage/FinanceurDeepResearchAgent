"""Assess evidence coverage and contradictions before report generation."""

import json

from .llm import Backend
from .models import ResearchState, Verification


def verify(backend: Backend, state: ResearchState, max_tasks: int, available_sources: list[str]) -> Verification:
    result = backend.structured(
        Verification,
        "Audit the supplied findings against the question and cited research notes. "
        "supported_finding_ids must include only claims whose supplied evidence "
        "supports the full statement, period, and units. Identify conflicting claims "
        "and missing coverage; do not resolve contradictions by guessing. "
        "Set sufficient true only if all material parts of the question are covered "
        "and conflicts do not prevent an answer. Use only existing finding IDs. "
        f"Create at most {max_tasks} targeted follow_up_tasks for unresolved gaps. "
        "Write executable search queries following the system prompt's Tavily "
        "query guidance. Review research_notes for queries already tried and "
        "the evidence they returned. Change the evidence target, terminology, "
        "source focus, or restrictive terms to address the specific gap; "
        "do not repeat a previous query or merely rephrase the original question. "
        f"Each task source must be one of {available_sources}.",
        json.dumps({
            "question": state.question,
            "as_of_utc": state.started_at,
            "plan": state.plan.model_dump() if state.plan else None,
            "findings": [finding.model_dump() for finding in state.findings],
            "sources": [source.model_dump() for source in state.sources],
            "research_notes": [note.model_dump() for note in state.notes],
        }),
    )
    known = {finding.id for finding in state.findings}
    result.supported_finding_ids = list(dict.fromkeys(fid for fid in result.supported_finding_ids if fid in known))
    result.follow_up_tasks = [task for task in result.follow_up_tasks if task.question.strip() and task.query.strip() and task.source in available_sources][:max_tasks]
    if not result.supported_finding_ids or result.gaps or result.conflicts:
        result.sufficient = False
    return result
