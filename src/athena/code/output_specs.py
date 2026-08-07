"""Output constraints for code agent assets."""

from dataclasses import dataclass, field

@dataclass
class OutputSpec:
    """Expected output files and format constraints for a code agent run."""
    must_exist: list[str] = field(default_factory=list)
    must_not_modify: list[str] = field(default_factory=list)
    format_check: dict[str, str] = field(default_factory=dict)

CODE_AGENT_SPEC = OutputSpec(
    must_exist=["REPORT.md"],
    must_not_modify=["eval.py", "train.csv", "val.csv", "test.csv", "labels.csv"],
)

DATA_AGENT_SPEC = OutputSpec(
    must_exist=["EDA.md", "feature_process.csv"],
    must_not_modify=[],
)

PLOT_AGENT_SPEC = OutputSpec(
    must_exist=[],
    must_not_modify=[],
    format_check={"*.png": "300dpi"},
)

if __name__ == "__main__":
    print(f"CODE_AGENT_SPEC: {CODE_AGENT_SPEC}")
    print(f"DATA_AGENT_SPEC: {DATA_AGENT_SPEC}")
    print(f"PLOT_AGENT_SPEC: {PLOT_AGENT_SPEC}")
