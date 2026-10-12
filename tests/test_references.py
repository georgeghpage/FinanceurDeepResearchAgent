"""References connect reported claims to the actual retrieved source pages."""

import unittest
from unittest.mock import Mock

from deepsearchagent.llm import ResponseError
from deepsearchagent.models import (
    Finding, ReportDraft, ReportParagraph, ReportSection, ResearchState, Source, Verification,
)
from deepsearchagent.writer import render, write


class ReferenceTests(unittest.TestCase):
    def state(self):
        return ResearchState(
            question="Revenue research", stop_reason="coverage",
            sources=[
                Source(id="S1", url="https://example.com/filing", title="Company filing", published_at="2025-11-01"),
                Source(id="S2", url="https://example.com/release", title="Earnings release"),
            ],
            findings=[
                Finding(id="F1", task_question="Revenue", claim="Revenue grew.", evidence="Growth reported.", source_id="S1"),
                Finding(id="F2", task_question="Revenue", claim="Growth accelerated.", evidence="Growth comparison reported.", source_id="S1"),
                Finding(id="F3", task_question="Revenue", claim="Revenue grew.", evidence="Growth independently reported.", source_id="S2"),
            ],
            verification=Verification(sufficient=True, supported_finding_ids=["F1", "F2", "F3"],
                                      conflicts=[], gaps=[], follow_up_tasks=[]),
        )

    def draft(self, ids):
        return ReportDraft(title="Revenue", sections=[ReportSection(
            title="Findings", paragraphs=[ReportParagraph(text="Revenue grew.", finding_ids=ids)],
        )])

    def test_inline_reference_and_source_metadata_survive_rendering(self):
        report = render(self.state(), self.draft(["F1"]))
        self.assertIn("Revenue grew. [S1: Company filing](<https://example.com/filing>)", report)
        self.assertIn("## Sources", report)
        self.assertIn("publication date: 2025-11-01; retrieved:", report)

    def test_multiple_sources_are_linked_once_per_paragraph(self):
        report = render(self.state(), self.draft(["F1", "F2", "F3", "F1"]))
        paragraph = next(line for line in report.splitlines() if line.startswith("Revenue grew."))
        self.assertEqual(paragraph.count("[S1:"), 1)
        self.assertEqual(paragraph.count("[S2:"), 1)
        self.assertIn("[S2: Earnings release](<https://example.com/release>)", paragraph)

    def test_missing_source_prevents_report_generation(self):
        state = self.state()
        state.sources = []
        backend = Mock()
        with self.assertRaises(ResponseError):
            write(backend, state)
        backend.structured.assert_not_called()

    def test_renderer_rejects_missing_references(self):
        for ids in ([], ["F999"], ["F1", "F999"]):
            with self.subTest(ids=ids), self.assertRaises(ResponseError):
                render(self.state(), self.draft(ids))
        state = self.state()
        state.sources = []
        with self.assertRaises(ResponseError):
            render(state)
