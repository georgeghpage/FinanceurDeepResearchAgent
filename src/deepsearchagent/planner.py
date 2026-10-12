"""Turn a question into a small, bounded research plan."""

import json
from datetime import datetime, timezone

from .llm import Backend
from .models import ResearchPlan
from .tools.financial import FINANCIAL_GUIDANCE


def plan(backend: Backend, question: str, max_tasks: int, available_sources: list[str]) -> ResearchPlan:
    result = backend.structured(
        ResearchPlan,
        f"Create at most {max_tasks} focused, non-overlapping research tasks with "
        "executable, high-quality queries following the system prompt's Tavily "
        "query guidance. Each task.query is sent directly to the search tool: "
        "include the entity, evidence target, and relevant period; do not put "
        "analysis instructions there. Prioritize material evidence needs and "
        "use fewer tasks for simple questions. "
        f"Set each task source to one of {available_sources}; "
        "use documentation for reference documentation and web for public research. "
        "Resolve relative dates against as_of_utc and cover "
        "the evidence needed to answer the question.\n" + FINANCIAL_GUIDANCE,
        json.dumps({"question": question, "as_of_utc": datetime.now(timezone.utc).isoformat()}),
    )
    result.tasks = [task for task in result.tasks if task.question.strip() and task.query.strip() and task.source in available_sources][:max_tasks]
    return result


def plan_evidence(backend: Backend, question: str):
    from .models import EvidencePlan
    return backend.structured(
        EvidencePlan,
        "Identify all essential independently verifiable requirements of the original question. "
        "Preserve geographic, legal, temporal and other constraints. Assign unique requirement IDs, "
        "critical flags and positive coverage weights. Generate exactly 15 specific, authoritative, "
        "current Tavily queries in exactly three thematic groups of exactly five queries. "
        "Avoid semantic duplication. Each query has a unique ID and requirement_ids. "
        "requirement_query_mapping must agree exactly with those IDs and cover every requirement. "
        "Do not search or answer the question.\n" + FINANCIAL_GUIDANCE,
        json.dumps({"question": question, "as_of_utc": datetime.now(timezone.utc).isoformat()}),
    )
