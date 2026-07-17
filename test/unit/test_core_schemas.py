"""Unit tests for the Hypothesis.package_ref field (task4 addition)."""

import unittest

from pydantic import ValidationError

from athena.core.schemas import Hypothesis


def _hypothesis_kwargs(**overrides) -> dict:
    defaults = dict(
        node_id="idea-1",
        status="DRAFTED",
        payload_ref="inline://idea-1",
        statement="s",
        intervention="i",
        expected_effect="e",
        package_ref="inline://idea-1",
    )
    defaults.update(overrides)
    return defaults


class HypothesisPackageRefTest(unittest.TestCase):
    def test_package_ref_is_required(self) -> None:
        kwargs = _hypothesis_kwargs()
        del kwargs["package_ref"]
        with self.assertRaises(ValidationError):
            Hypothesis(**kwargs)

    def test_package_ref_is_stored(self) -> None:
        hypothesis = Hypothesis(**_hypothesis_kwargs())
        self.assertEqual("inline://idea-1", hypothesis.package_ref)
