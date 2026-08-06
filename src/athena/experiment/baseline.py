"""Compatibility import for baseline workflow."""

from athena.workflows.prepare.baseline import BaselineDraft, create_baseline

__all__ = ["BaselineDraft", "create_baseline"]

if __name__ == "__main__":
    print("create_baseline function loaded.")
