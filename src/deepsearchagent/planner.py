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
    from .models import PlanningBrief
    return backend.structured(
        PlanningBrief,
        "Produce a compact research plan, not an answer or search queries. Use one objective, "
        "2–5 concrete steps and only the essential verifiable requirements (usually 2–5, at most 8). "
        "Aim for 150 words total. Each step names the evidence to collect or comparison to make. "
        "Stay within the user's scope: a narrow factual question usually needs only 1–2 requirements. "
        "Do not add setup guides, compatibility surveys, version audits or independent reviews unless "
        "the question asks for them or they are necessary to establish its core claims. "
        "Source URLs, access dates and citation formatting are metadata, never separate research requirements. "
        "Preserve entities, dates, geography and other constraints. Assign unique requirement IDs, "
        "critical flags and positive coverage weights. Resolve relative dates against as_of_utc. "
        "Do not deliberate at length or speculate about results.\n" + FINANCIAL_GUIDANCE,
        json.dumps({"question": question, "as_of_utc": datetime.now(timezone.utc).isoformat()}),
    )


def generate_queries(backend, question, brief, limit=15):
    from .models import QueryBatch
    return backend.structured(
        QueryBatch,
        f"Generate at most {limit} executable search queries from this plan. Usually one or two "
        "queries per requirement suffice; use fewer for simple questions. Never exceed two queries "
        "per requirement or add new research objectives. Output queries and "
        "requirement_ids only. Do not analyze, answer, explain, or revise the plan. Include entity, "
        "evidence target and relevant period. Prefer primary sources; never invent domains or facts. "
        "Cover each requirement, avoid duplicates and keep each query under 400 characters.",
        json.dumps({"question": question, "plan": brief.model_dump(),
                    "as_of_utc": datetime.now(timezone.utc).isoformat()}),
    )


def recover_brief(question, content=""):
    """Keep complete plan fields, or start retrieval from the user's actual question."""
    from pydantic import ValidationError
    from pydantic_core import from_json
    from .models import PlanningBrief, Requirement
    data = None
    try:
        data = from_json(content, allow_partial=True)
        return PlanningBrief.model_validate(data, strict=True)
    except (ValueError, ValidationError):
        pass
    requirements = []
    if isinstance(data, dict):
        for item in data.get("research_requirements", []) if isinstance(data.get("research_requirements"), list) else []:
            try:
                requirement = Requirement.model_validate(item, strict=True)
                if requirement.id not in {r.id for r in requirements}:
                    requirements.append(requirement)
            except ValidationError:
                continue
    return PlanningBrief(objective=question[:500],
        steps=["Retrieve primary sources addressing the question and its stated constraints.",
               "Compare the available evidence and report supported conclusions and remaining gaps."],
        research_requirements=requirements[:8] or [Requirement(id="R1", question=question[:1000], critical=True, weight=1)])


def recover_queries(content):
    from pydantic import ValidationError
    from pydantic_core import from_json
    from .models import QuerySpec
    try:
        data = from_json(content, allow_partial=True)
    except ValueError:
        return []
    queries = []
    for item in data.get("queries", []) if isinstance(data, dict) and isinstance(data.get("queries"), list) else []:
        try:
            queries.append(QuerySpec.model_validate(item, strict=True))
        except ValidationError:
            continue
    return queries


def assemble_plan(brief, queries, limit=15):
    """IDs, deduplication, grouping and coverage are controller work, not model work."""
    from .models import EvidencePlan, PlannedQuery, QuerySpec, ResearchGroup
    requirements = {r.id: r for r in brief.research_requirements}
    selected = {}
    # Missing/invalid query output costs no extra model round trip.
    candidates = list(queries)
    covered = {rid for q in candidates if q.query.strip() for rid in q.requirement_ids}
    candidates += [QuerySpec(query=r.question[:500], requirement_ids=[r.id])
                   for r in requirements.values() if r.id not in covered]
    for item in candidates:
        ids = [rid for rid in dict.fromkeys(item.requirement_ids) if rid in requirements]
        query = " ".join(item.query.split())
        if not query or not ids:
            continue
        key = query.casefold()
        if key in selected:
            selected[key].requirement_ids = list(dict.fromkeys(selected[key].requirement_ids + ids))
        elif len(selected) < limit:
            selected[key] = PlannedQuery(id=f"Q{len(selected) + 1}", query=query, requirement_ids=ids)
    planned = list(selected.values())
    covered = {rid for q in planned for rid in q.requirement_ids}
    # Very small search budgets combine remaining evidence needs in the last query.
    missing = [r for rid, r in requirements.items() if rid not in covered]
    if missing:
        last = planned[-1]
        last.query = " ".join([brief.objective] + [r.question for r in missing])[:500]
        last.requirement_ids = list(dict.fromkeys(last.requirement_ids + [r.id for r in missing]))
        duplicate = next((q for q in planned[:-1] if q.query.casefold() == last.query.casefold()), None)
        if duplicate:
            duplicate.requirement_ids = list(dict.fromkeys(duplicate.requirement_ids + last.requirement_ids))
            planned.pop()
    groups = [ResearchGroup(id=f"G{i // 5 + 1}", theme="Evidence collection",
                            query_ids=[q.id for q in planned[i:i + 5]]) for i in range(0, len(planned), 5)]
    return EvidencePlan(objective=brief.objective, steps=brief.steps,
        research_requirements=brief.research_requirements, search_queries=planned, research_groups=groups,
        requirement_query_mapping={rid: [q.id for q in planned if rid in q.requirement_ids] for rid in requirements})
