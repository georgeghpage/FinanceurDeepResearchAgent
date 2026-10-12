"""Generate and render a report using validated finding/source references."""

import json
import re

from .llm import Backend, ResponseError
from .models import ReportDraft, ResearchState, Source


def write(backend: Backend, state: ResearchState) -> ReportDraft:
    supported = set(state.verification.supported_finding_ids) if state.verification else set()
    findings = {finding.id: finding for finding in state.findings}
    source_ids = {source.id for source in state.sources}
    if any(fid not in findings or findings[fid].source_id not in source_ids for fid in supported):
        raise ResponseError("A supported finding has no stored source reference.", category="evidence_reference")
    draft = backend.structured(
        ReportDraft,
        "Write a concise research report answering the question with only the supplied "
        "supported findings. Each factual paragraph must reference the finding_ids "
        "that support every claim in that paragraph. Do not insert URLs, Markdown links, "
        "or citation numbers; the renderer adds those. Distinguish inference from reported "
        "facts. Do not invent facts or use general knowledge to fill evidence gaps. "
        "If evidence is insufficient, write fewer sections; limitations are appended separately.",
        json.dumps({
            "question": state.question,
            "findings": [finding.model_dump() for finding in state.findings if finding.id in supported],
            "verification": state.verification.model_dump() if state.verification else None,
        }),
    )
    if not any(section.paragraphs for section in draft.sections):
        raise ResponseError("Report contained no evidence-backed paragraphs.", category="empty_report")
    for section in draft.sections:
        for paragraph in section.paragraphs:
            if not paragraph.text.strip() or not paragraph.finding_ids or not set(paragraph.finding_ids).issubset(supported):
                raise ResponseError("Report contains a paragraph with missing or unsupported finding references.", category="evidence_reference")
    return draft


def plain(text: str) -> str:
    """Escape model text so report links can only come from stored sources."""
    text = " ".join(text.split())
    text = re.sub(r"https?://\S+", "(URL omitted)", text)
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def source_reference(source: Source) -> str:
    """Show the source name beside the claim, using the retrieved URL directly."""
    url = source.url.replace("<", "%3C").replace(">", "%3E")
    return f"[{plain(source.id)}: {plain(source.title)}](<{url}>)"


def render(state: ResearchState, draft: ReportDraft | None = None) -> str:
    lines = [f"# {plain(draft.title if draft else state.question)}", "",
             f"Research started: {state.started_at}", "",
             f"Stop reason: {state.stop_reason}. Rounds: {state.rounds}. "
             f"LLM calls: {state.api_calls}. Search calls: {state.search_calls}. Tokens used: {state.tokens_used}.", ""]
    sources = {source.id: source for source in state.sources}
    findings = {finding.id: finding for finding in state.findings}

    def citations(ids: list[str]) -> str:
        if not ids or any(fid not in findings or findings[fid].source_id not in sources for fid in ids):
            raise ResponseError("A reported claim has no stored source reference.", category="evidence_reference")
        source_ids = dict.fromkeys(findings[fid].source_id for fid in ids)
        return " ".join(source_reference(sources[sid]) for sid in source_ids)

    if draft is not None:
        for section in draft.sections:
            lines.extend([f"## {plain(section.title)}", ""])
            for paragraph in section.paragraphs:
                lines.extend([f"{plain(paragraph.text)} {citations(paragraph.finding_ids)}", ""])
    else:
        lines.extend(["## Collected findings (no synthesized report)", ""])
        if not state.findings:
            lines.extend(["No source-backed findings were collected.", ""])
        for finding in state.findings:
            if finding.evidence_status not in {"SUPPORTED", "PARTIALLY_SUPPORTED"} or not finding.evidence:
                continue
            lines.extend([f"- {plain(finding.claim)} {citations([finding.id])}"])
        lines.append("")

    limitations = list(state.warnings)
    if hasattr(state.plan, "research_requirements"):
        evaluations = {r.requirement_id: r for r in state.judge.requirements} if state.judge else {}
        addressed = {findings[fid].requirement_id for section in draft.sections
                     for paragraph in section.paragraphs for fid in paragraph.finding_ids} if draft else {
                         f.requirement_id for f in state.findings if f.evidence and f.evidence_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"}}
        for requirement in state.plan.research_requirements:
            evaluation = evaluations.get(requirement.id)
            if evaluation is None or evaluation.status != "COVERED" or requirement.id not in addressed:
                limitations.append(f"Unresolved requirement {requirement.id}: {requirement.question}")
                if evaluation:
                    limitations.extend(evaluation.missing_evidence)
        limitations.extend(state.unresolved)
        limitations.extend(f"Conflict: {v}" for v in state.contradictions)
        limitations.extend(f"{f.id}: {v}" for f in state.findings for v in f.limitations)
        if state.judge:
            limitations.extend(state.judge.critical_gaps)
            limitations.extend(f"Conflict: {v}" for v in state.judge.contradictions)
    if state.verification:
        limitations.extend(state.verification.gaps)
        limitations.extend(f"Conflict: {conflict}" for conflict in state.verification.conflicts)
        if not state.verification.sufficient:
            limitations.append("The evidence audit did not establish sufficient coverage.")
    else:
        limitations.append("Collected findings have not completed an evidence audit.")
    if state.stop_reason != "coverage":
        limitations.append("Research stopped before sufficient coverage was established.")
    if limitations:
        lines.extend(["## Limitations", ""])
        lines.extend(f"- {plain(item)}" for item in dict.fromkeys(limitations))
        lines.append("")
    lines.extend(["## Sources", ""])
    for source in state.sources:
        lines.extend([f"- {source_reference(source)} — "
                      f"publication date: {plain(source.published_at or 'unknown')}; "
                      f"retrieved: {source.retrieved_at}"])
    return "\n".join(lines).rstrip() + "\n"


def write_evidence(backend, state, input_limit):
    """Write from a bounded evidence snapshot, including qualified partial answers."""
    from .budget import stage_prompt, token_bound
    from .judge import evidence_snapshot
    instructions = (
        "Answer the original question directly in a coherent concise report. Use logical sections "
        "and only supplied evidence. Each factual paragraph must cite the finding_ids that support "
        "every statement. Never insert URLs, Markdown links or citation numbers. Do not add factual "
        "claims from memory. Preserve numbers, dates, units and constraints. Clearly distinguish "
        "partial evidence and inference; uncertainty does not license guessing. Address each original "
        "requirement; unresolved portions will be explicitly appended by the renderer. No research tools."
    )
    base = {"question": state.question, "requirements": [r.model_dump() for r in state.plan.research_requirements],
            "latest_evaluation": state.judge.model_dump() if state.judge else None, "evidence": {}}
    available = input_limit - token_bound(stage_prompt(instructions, ReportDraft)) - token_bound(json.dumps(base)) - 256
    snapshot = evidence_snapshot(state, max(0, available))
    snapshot["findings"] = [f for f in snapshot["findings"] if f["evidence_status"] in {"SUPPORTED", "PARTIALLY_SUPPORTED"} and f["evidence"]]
    base["evidence"] = snapshot
    draft = backend.structured(ReportDraft, instructions, json.dumps(base, ensure_ascii=False))
    allowed = {f["id"] for f in snapshot["findings"]}
    validate_draft(state, draft, allowed)
    return draft


def validate_draft(state, draft, allowed=None):
    findings = {f.id: f for f in state.findings}
    sources = {s.id for s in state.sources}
    allowed = allowed if allowed is not None else {f.id for f in state.findings if f.evidence_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"} and f.evidence}
    if not any(section.paragraphs for section in draft.sections):
        raise ResponseError("Empty report.", category="empty_report")
    for section in draft.sections:
        for paragraph in section.paragraphs:
            if (not paragraph.text.strip() or not paragraph.finding_ids or not set(paragraph.finding_ids) <= allowed
                    or any(findings[fid].source_id not in sources for fid in paragraph.finding_ids)):
                raise ResponseError("Unsupported report references.", category="evidence_reference")
    if state.judge:
        addressed = {findings[fid].requirement_id for section in draft.sections
                     for paragraph in section.paragraphs for fid in paragraph.finding_ids}
        essential_covered = {r.requirement_id for r in state.judge.requirements if r.critical and r.status == "COVERED"}
        if not essential_covered <= addressed:
            raise ResponseError("Report omitted a covered essential requirement.", category="evidence_reference")


def audit_report(backend, state, draft):
    from .models import ReportAudit
    findings = {f.id: f for f in state.findings}
    paragraphs = [{"paragraph_id": f"P{sidx}-{pidx}", "text": p.text,
                   "evidence": [findings[fid].model_dump() for fid in p.finding_ids]}
                  for sidx, section in enumerate(draft.sections) for pidx, p in enumerate(section.paragraphs)]
    audit = backend.structured(ReportAudit,
        "Check each paragraph against its cited evidence. Mark supported only if every factual addition, "
        "number, date and causal assertion is entailed by the evidence or explicitly qualified as inference. "
        "Return each supplied paragraph_id exactly once. Do not rewrite or research.",
        json.dumps({"paragraphs": paragraphs}, ensure_ascii=False))
    expected = {p["paragraph_id"] for p in paragraphs}
    if len(audit.paragraphs) != len(expected) or {p.paragraph_id for p in audit.paragraphs} != expected or not all(p.supported for p in audit.paragraphs):
        raise ResponseError("Report semantic evidence validation failed.", category="evidence_reference")


def evidence_draft(state):
    """Guaranteed bounded writing fallback without adding or synthesizing factual claims."""
    from .models import ReportParagraph, ReportSection
    sections = []
    for requirement in state.plan.research_requirements if hasattr(state.plan, "research_requirements") else []:
        paragraphs = [ReportParagraph(text=("Partially supported: " if f.evidence_status == "PARTIALLY_SUPPORTED" else "") + f.claim, finding_ids=[f.id]) for f in state.findings
                      if f.requirement_id == requirement.id and f.evidence_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"} and f.evidence]
        if paragraphs:
            sections.append(ReportSection(title=requirement.question, paragraphs=paragraphs))
    return ReportDraft(title=state.question, sections=sections)
