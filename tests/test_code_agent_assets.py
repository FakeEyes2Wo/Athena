from pathlib import Path

import athena.core.agent as core_agent
from athena.code.output_specs import CODE_AGENT_SPEC, DATA_AGENT_SPEC, PLOT_AGENT_SPEC


def test_code_agent_assets_have_canonical_owners() -> None:
    prompt_dir = Path(core_agent.__file__).parent / "prompts"
    assert {path.name for path in prompt_dir.glob("*_agent.md")} == {
        "code_agent.md",
        "data_agent.md",
        "init_agent.md",
        "plot_agent.md",
        "report_agent.md",
    }
    assert CODE_AGENT_SPEC.must_exist == ["REPORT.md"]
    assert DATA_AGENT_SPEC.must_exist == ["EDA.md", "feature_process.csv"]
    assert PLOT_AGENT_SPEC.format_check == {"*.png": "300dpi"}
