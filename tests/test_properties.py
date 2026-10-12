"""Generated schema and shared-budget invariants; deterministic Hypothesis profile."""

import unittest
from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
from deepsearchagent.budget import RunBudget, token_bound
from deepsearchagent.config import Config
from deepsearchagent.llm import BudgetExceeded
from deepsearchagent.models import EvidencePlan, Source
from support_fixtures import plan

settings.register_profile("qa", max_examples=60, derandomize=True, deadline=None)
settings.load_profile("qa")


class PropertyTests(unittest.TestCase):
    @given(
        st.lists(
            st.tuples(st.integers(1, 1000), st.integers(1, 1000)),
            min_size=1,
            max_size=30,
        )
    )
    def test_reservations_and_settlements_never_spend_writer_reserve(self, calls):
        budget = RunBudget(
            Config(max_tokens=10000, writer_reserve_tokens=2000, max_calls=40)
        )
        reservations = []
        for inp, out in calls:
            try:
                reservations.append(budget.reserve_model(inp, out, (1, 2)))
            except BudgetExceeded:
                pass
            self.assertLessEqual(budget.tokens + budget.pending_tokens, 8000)
            self.assertGreaterEqual(budget.pending_tokens, 0)
        for reservation in reversed(reservations):
            budget.settle(reservation, {"usage_observed": False})
        self.assertEqual(budget.pending_tokens, 0)
        writer = budget.reserve_model(1000, 1000, (1, 2), writing=True)
        budget.settle(writer, {"usage_observed": False})
        self.assertLessEqual(budget.tokens, 10000)

    @given(st.text())
    def test_utf8_input_bound_includes_non_latin_characters(self, text):
        # Pydantic/JSON boundaries do not accept unpaired surrogate code points.
        if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            return
        self.assertGreaterEqual(token_bound(text), len(text))

    @given(
        st.sampled_from(["javascript", "data", "file", "ftp"]),
        st.text(alphabet="abcdef0123", min_size=1),
    )
    def test_non_http_source_protocols_are_rejected(self, scheme, host):
        with self.assertRaises(ValidationError):
            Source(id="S1", title="x", url=f"{scheme}://{host}/doc")

    @given(st.integers(0, 14), st.integers(0, 14))
    def test_duplicate_query_ids_are_rejected(self, first, second):
        if first == second:
            return
        data = plan().model_dump()
        data["search_queries"][first]["id"] = data["search_queries"][second]["id"]
        with self.assertRaises(ValidationError):
            EvidencePlan.model_validate(data)
