"""Compatibility imports for the canonical REPORT workflow."""

from athena.workflows.report.final_report import ReportNarrative, Reporter

__all__ = ["ReportNarrative", "Reporter"]

if __name__ == "__main__":
    reporter = Reporter()
    print(f"Reporter loaded: {reporter}")
