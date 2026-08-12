import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_headless.py"


def _load_headless():
    assert SCRIPT.is_file(), "headless runner script is missing"
    spec = importlib.util.spec_from_file_location("run_headless", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_forces_auto_mode_without_dataset_defaults(monkeypatch) -> None:
    headless = _load_headless()
    seen: list[list[str]] = []
    monkeypatch.setattr(
        headless,
        "cli_main",
        lambda argv: seen.append(argv) or 0,
    )

    assert headless.main(["--project", "p", "--data", "d", "--target", "y"]) == 0
    assert seen == [
        [
            "run",
            "--mode",
            "auto",
            "--project",
            "p",
            "--data",
            "d",
            "--target",
            "y",
        ]
    ]
