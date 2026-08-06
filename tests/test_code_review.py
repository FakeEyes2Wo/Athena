"""Tests for deterministic code diff review boundaries."""

import pytest

from athena.code import review


def test_review_approved_normal_diff() -> None:
    """A declared in-scope model change is approved."""
    diff = "diff --git a/model.py b/model.py\n+def train():\n+    pass\n"
    verdict = review.review_diff(
        diff, allowed_files={"model.py"}, declared_dependencies=set()
    )
    assert verdict.action == "approve"


@pytest.mark.parametrize("protected_path", ["eval.py", "splits.json"])
def test_review_rejects_protected_evaluation_changes(protected_path: str) -> None:
    """Evaluation code and frozen splits cannot be changed."""
    diff = f"diff --git a/{protected_path} b/{protected_path}\n+tampered\n"
    verdict = review.review_diff(
        diff, allowed_files={protected_path}, declared_dependencies=set()
    )
    assert verdict.action == "reject"
    assert verdict.reasons


def test_review_requires_revision_for_scope_escape() -> None:
    """A changed file outside the declared scope requires revision."""
    diff = "diff --git a/outside.py b/outside.py\n+print('escape')\n"
    verdict = review.review_diff(
        diff, allowed_files={"model.py"}, declared_dependencies=set()
    )
    assert verdict.action == "revise"
    assert any("outside.py" in reason for reason in verdict.reasons)


def test_review_requires_revision_for_undeclared_dependency() -> None:
    """A newly imported third-party package must be declared."""
    verdict = review.review_diff(
        "+import xgboost",
        allowed_files={"model.py"},
        declared_dependencies=set(),
    )
    assert verdict.action == "revise"
    assert any("xgboost" in reason for reason in verdict.reasons)


def test_review_allows_declared_dependency() -> None:
    """A declared third-party import does not create review feedback."""
    verdict = review.review_diff(
        "+from xgboost import XGBClassifier",
        allowed_files={"model.py"},
        declared_dependencies={"xgboost"},
    )
    assert verdict.action == "approve"


def test_review_requires_revision_for_multiline_undeclared_dependency() -> None:
    """A multi-line import is parsed as one Python statement for dependency review."""
    diff = "\n".join(
        [
            "+from xgboost import (",
            "+    XGBClassifier,",
            "+)",
        ]
    )
    verdict = review.review_diff(
        diff,
        allowed_files={"model.py"},
        declared_dependencies=set(),
    )

    assert verdict.action == "revise"
    assert any("xgboost" in reason for reason in verdict.reasons)


@pytest.mark.parametrize(
    "diff",
    [
        "+def train():\n+    import xgboost",
        "+import xgboost\n+broken = (",
    ],
)
def test_review_recovers_imports_from_partial_added_code(diff: str) -> None:
    """Indentation or an unrelated incomplete fragment cannot hide an import."""
    verdict = review.review_diff(
        diff,
        allowed_files={"model.py"},
        declared_dependencies=set(),
    )

    assert verdict.action == "revise"
    assert any("xgboost" in reason for reason in verdict.reasons)
