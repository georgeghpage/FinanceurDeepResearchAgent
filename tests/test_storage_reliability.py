"""Filesystem commit faults, path boundaries and provenance regressions."""

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepsearchagent.evidence_store import EvidenceStore, atomic_write
from deepsearchagent.models import (
    EvidenceExtraction,
    EvidenceFinding,
    ResearchState,
    SearchResult,
    Source,
)
from support_fixtures import plan


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_commit_faults_preserve_previous_snapshot_and_clean_temps(self):
        target = self.root / "research_facts.md"
        atomic_write(target, "Valid 中文 snapshot")
        for boundary, failure in (
            ("os.fsync", OSError(errno.ENOSPC, "disk full")),
            ("os.replace", PermissionError("permission denied")),
            ("os.replace", OSError(errno.EBUSY, "temporarily unavailable")),
            ("os.fsync", InterruptedError("interrupted sync")),
        ):
            with self.subTest(boundary=boundary, failure=type(failure).__name__):
                with patch(
                    "deepsearchagent.evidence_store." + boundary, side_effect=failure
                ):
                    with self.assertRaises(OSError):
                        atomic_write(target, "Incomplete replacement")
                self.assertEqual(target.read_text(), "Valid 中文 snapshot")
                self.assertEqual(list(self.root.glob(".snapshot-*")), [])

    def test_partial_write_failure_preserves_previous_file(self):
        target = self.root / "state.json"
        atomic_write(target, '{"valid": true}')
        real_fdopen = os.fdopen

        class PartialWriter:
            def __init__(self, fd, *args, **kwargs):
                self.stream = real_fdopen(fd, *args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, text):
                self.stream.write(text[:5])
                raise OSError(errno.ENOSPC, "partial write")

        with patch("deepsearchagent.evidence_store.os.fdopen", PartialWriter):
            with self.assertRaises(OSError):
                atomic_write(target, '{"replacement": true}')
        self.assertEqual(json.loads(target.read_text()), {"valid": True})
        self.assertEqual(list(self.root.glob(".snapshot-*")), [])

    def test_session_id_cannot_escape_root(self):
        for sid in (
            f"../{self.root.name}-escape",
            str(self.root.parent / (self.root.name + "-absolute")),
            "a/b",
            "..",
            ".",
            "",
        ):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                EvidenceStore(ResearchState(question="q", session_id=sid), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_session_and_symlink_are_never_overwritten(self):
        state = ResearchState(question="Original question")
        store = EvidenceStore(state, self.root)
        original = Path(state.facts_path).read_bytes()
        with self.assertRaises(FileExistsError):
            EvidenceStore(state, self.root)
        self.assertEqual(Path(state.facts_path).read_bytes(), original)
        second = ResearchState(question="Another question")
        (self.root / second.session_id).symlink_to(
            store.directory, target_is_directory=True
        )
        with self.assertRaises(FileExistsError):
            EvidenceStore(second, self.root)
        self.assertEqual(Path(state.facts_path).read_bytes(), original)

    def test_unicode_large_snapshot_and_source_provenance(self):
        state = ResearchState(
            question="中文 / Русский / 한국어 ``` <script>", plan=plan()
        )
        store = EvidenceStore(state, self.root)
        query = state.plan.search_queries[0]
        text = "测量值 42 万元。 Дата 2026. 한국어. "
        result = SearchResult(
            text=text,
            sources=[
                Source(id="", title="引用", url="https://example.com/doc#fragment")
            ],
            documents=[{"url": "https://example.com/doc", "content": text * 10000}],
        )
        store.add_result(query, result)
        finding = EvidenceFinding(
            claim_id="malicious-local-id",
            requirement_id="R1",
            claim=text,
            source_id="S1",
            source_url="https://example.com/doc",
            supporting_excerpt=text,
            publication_date=None,
            evidence_status="SUPPORTED",
            limitations=[],
        )
        extraction = EvidenceExtraction(
            findings=[finding], contradictions=[], uncertainties=[]
        )
        self.assertEqual(store.merge(extraction, {"R1"}, [result], ["Q1"]), 1)
        self.assertEqual(store.merge(extraction, {"R1"}, [result], ["Q1"]), 0)
        record = state.findings[0]
        self.assertEqual(
            (record.id, record.source_id, record.query_ids), ("F1", "S1", ["Q1"])
        )
        self.assertIsNone(record.publication_date)
        saved = ResearchState.model_validate_json(
            (store.directory / "state.json").read_text()
        )
        self.assertEqual(saved.findings, state.findings)
        self.assertIn(text, Path(state.facts_path).read_text())

    def test_workers_cannot_reference_another_group_source(self):
        state = ResearchState(question="q", plan=plan())
        store = EvidenceStore(state, self.root)
        source = Source(id="", title="source", url="https://example.com/doc")
        result = SearchResult(
            text="Fact",
            sources=[source],
            documents=[{"url": source.url, "content": "Fact"}],
        )
        store.add_result(state.plan.search_queries[0], result)
        base = dict(
            claim_id="x",
            requirement_id="R1",
            claim="Fact",
            source_id="S1",
            source_url=source.url,
            supporting_excerpt="Fact",
            publication_date=None,
            evidence_status="SUPPORTED",
            limitations=[],
        )
        for change in (
            {"source_id": "S99"},
            {"requirement_id": "R2"},
            {"source_url": "https://evil.example/doc"},
        ):
            extraction = EvidenceExtraction(
                findings=[EvidenceFinding(**(base | change))],
                contradictions=[],
                uncertainties=[],
            )
            self.assertEqual(store.merge(extraction, {"R1"}, [result], ["Q1"]), 0)
        self.assertEqual(state.findings, [])
