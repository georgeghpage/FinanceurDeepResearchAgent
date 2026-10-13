"""User-facing progress derived only from real execution boundaries."""

from typing import Literal, TypedDict


class StatusEvent(TypedDict):
    type: Literal["status"]
    code: str
    message: str
    source_count: int


STAGES = {
    "plan": ("planning", "Planning research…"),
    "search": ("searching", "Searching the web…"),
    "extract": ("reading", "Reading sources…"),
    "verify": ("checking", "Cross-checking findings…"),
    "write": ("writing", "Synthesizing the answer…"),
    "validate": ("validating", "Validating the answer…"),
    "finalize": ("finalizing", "Finalizing research…"),
}
SCHEMAS = {"PlanningBrief": "plan", "QueryBatch": "plan", "ResearchPlan": "plan", "EvidencePlan": "plan", "Extraction": "extract",
           "EvidenceExtraction": "extract", "Verification": "verify", "JudgeDecision": "verify",
           "ReportDraft": "write", "ReportAudit": "validate"}


class ResearchProgress:
    def __init__(self):
        self.stage = None
        self.last = None
        self.sources = set()
        self.active = set()
        self.stopping = False

    def receive(self, event) -> StatusEvent | None:
        kind = event.get("kind")
        if self.stopping:
            return None
        status = None
        if kind == "cancellation_requested":
            self.stopping = True
            status = ("stopping", "Stopping research…")
        elif kind == "stage_start" or (kind == "llm_start" and self.stage is None):
            self.stage = event.get("stage") or SCHEMAS.get(event.get("schema"))
            status = STAGES.get(self.stage)
        elif kind in {"llm_retry", "llm_continuation"}:
            status = STAGES.get(self.stage)
        elif kind == "llm_recovered":
            status = STAGES.get(self.stage)
        elif kind == "search_start":
            self.active.add(event.get("search_id"))
            if self.stage in {None, "search"}:
                self.stage = "search"
                status = STAGES["search"]
        elif kind == "search_complete":
            self.active.discard(event.get("search_id"))
            self.sources.update(s["url"] for s in event.get("sources", []) if s.get("url"))
            if self.stage == "search":
                status = STAGES["search"]
        elif kind == "search_error":
            self.active.discard(event.get("search_id"))
        elif kind == "search_retry" and self.stage == "search" and not self.active:
            status = STAGES.get(self.stage)
        elif kind == "search_degraded" and self.stage == "search" and not self.active:
            status = ("continuing", "Continuing with available sources…")
        if not status:
            return None
        value = (*status, len(self.sources))
        if value == self.last:
            return None
        self.last = value
        return {"type": "status", "code": status[0], "message": status[1], "source_count": len(self.sources)}
