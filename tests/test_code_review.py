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


@pytest.mark.parametrize("protected_path", ["eval.py", "eval_spec.json", ".gitignore"])
def test_review_rejects_protected_evaluation_changes(protected_path: str) -> None:
    """Evaluation code, spec, and ignore rules cannot be changed."""
    diff = f"diff --git a/{protected_path} b/{protected_path}\n+tampered\n"
    verdict = review.review_diff(
        diff, allowed_files={protected_path}, declared_dependencies=set()
    )
    assert verdict.action == "reject"
    assert verdict.reasons


@pytest.mark.parametrize("aux_path", ["models/stack.py", "config.json"])
def test_review_approves_auxiliary_paths_in_scope(aux_path: str) -> None:
    """A declared auxiliary file inside the generated tree is approved."""
    diff = f"diff --git a/{aux_path} b/{aux_path}\n+def build():\n+    pass\n"
    verdict = review.review_diff(
        diff, allowed_files={aux_path}, declared_dependencies=set()
    )
    assert verdict.action == "approve"


def test_review_does_not_miss_deletion_paths() -> None:
    """A deletion header (--- a/old.py, +++ /dev/null) still reports the path."""
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "index e929141..0000000\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        '-print("old")\n'
    )
    approved = review.review_diff(
        diff, allowed_files={"old.py"}, declared_dependencies=set()
    )
    assert approved.action == "approve"

    outside = review.review_diff(
        diff, allowed_files={"model.py"}, declared_dependencies=set()
    )
    assert outside.action == "revise"
    assert any("old.py" in reason for reason in outside.reasons)


def test_review_rejects_protected_deletion() -> None:
    """Deleting a protected evaluation file is rejected like any change."""
    diff = (
        "diff --git a/eval.py b/eval.py\n"
        "deleted file mode 100644\n"
        "--- a/eval.py\n"
        "+++ /dev/null\n"
    )
    verdict = review.review_diff(
        diff, allowed_files={"eval.py"}, declared_dependencies=set()
    )
    assert verdict.action == "reject"
    assert any("eval.py" in reason for reason in verdict.reasons)


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
