"""The style gate must reject incomplete checks and isolate repeated invocations."""

from pathlib import Path

import pytest

from scripts import check_code_style as style


@pytest.fixture(autouse=True)
def clear_findings():
    style.FINDINGS.clear()
    yield
    style.FINDINGS.clear()


def test_strict_docstrings_are_blocking(tmp_path, capsys):
    source = tmp_path / "module.py"
    source.write_text("def public():\n    return 1\n", encoding="utf-8")
    assert style.main([str(source)]) == 0
    capsys.readouterr()
    assert style.main(["--enforce-docstrings", str(source)]) == 1
    assert "[R3]" in capsys.readouterr().out


@pytest.mark.parametrize("target", ["missing.py", "missing", "readme.txt", "empty"])
def test_invalid_targets_never_pass(tmp_path, target, capsys):
    (tmp_path / "readme.txt").write_text("not Python", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    assert style.main([str(tmp_path / target)]) == 1
    assert "[R0]" in capsys.readouterr().out


@pytest.mark.parametrize("content", [b"def invalid(:\n", b"\xff\xfe\xfa"])
def test_unparseable_sources_never_pass(tmp_path, content, capsys):
    source = tmp_path / "module.py"
    source.write_bytes(content)
    assert style.main([str(source)]) == 1
    assert "[R0]" in capsys.readouterr().out


def test_unreadable_source_never_passes(tmp_path, monkeypatch, capsys):
    source = tmp_path / "module.py"
    source.touch()

    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", denied)
    assert style.main([str(source)]) == 1
    assert "[R0]" in capsys.readouterr().out


def test_repeated_runs_do_not_retain_old_findings(tmp_path, capsys):
    source = tmp_path / "module.py"
    source.write_text("from __future__ import annotations\n", encoding="utf-8")
    assert style.main([str(source)]) == 1
    capsys.readouterr()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert style.main([str(source)]) == 0
    assert capsys.readouterr().out == ""


def test_overlapping_targets_report_each_violation_once(tmp_path, capsys):
    source = tmp_path / "module.py"
    source.write_text("from __future__ import annotations\n", encoding="utf-8")
    assert style.main([str(tmp_path), str(source)]) == 1
    assert capsys.readouterr().out.count("[R2]") == 1


def test_nested_function_import_is_reported_once(tmp_path, capsys):
    source = tmp_path / "module.py"
    source.write_text(
        "def _outer():\n    def _inner():\n        import os\n", encoding="utf-8"
    )
    assert style.main([str(source)]) == 1
    assert capsys.readouterr().out.count("[R1]") == 1


def test_documented_cycle_import_remains_allowed(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        "def _factory():\n    import os  # 延迟导入避免循环依赖\n", encoding="utf-8"
    )
    assert style.main([str(source)]) == 0


@pytest.mark.parametrize("separator", ["# ── section ──", "# ═══", "# ---"])
def test_decorative_comments_are_rejected(tmp_path, separator):
    source = tmp_path / "module.py"
    source.write_text(separator + "\n", encoding="utf-8")
    assert style.main([str(source)]) == 1


def test_regular_comments_and_documented_api_pass(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        '# - A normal list item\ndef public():\n    """Publish the next event."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    assert style.main(["--enforce-docstrings", str(source)]) == 0


def test_code_examples_inside_strings_are_not_executable_violations(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        '"""Avoid these examples:\n# ---\nfrom __future__ import annotations\n"""\n',
        encoding="utf-8",
    )
    assert style.main(["--enforce-docstrings", str(source)]) == 0


def test_valid_utf8_bom_source_passes(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8-sig")
    assert style.main(["--enforce-docstrings", str(source)]) == 0


@pytest.mark.parametrize(
    "handler",
    [
        "except ValueError:\n    pass",
        "except (ValueError, TypeError):\n    pass",
        "except BaseException:\n    pass",
        "except ValueError:\n    reason = '# not a comment'",
        "# An earlier comment does not describe this handler.\nexcept ValueError:\n    pass",
    ],
)
def test_specific_handlers_require_a_trigger_comment(tmp_path, capsys, handler):
    source = tmp_path / "module.py"
    source.write_text("try:\n    int('bad')\n" + handler + "\n", encoding="utf-8")
    assert style.main([str(source)]) == 1
    assert "[R6]" in capsys.readouterr().out


@pytest.mark.parametrize(
    "handler",
    [
        "except Exception:\n    pass",
        "except ValueError:\n    # Input is not an integer; ignore the optional value.\n    pass",
        "except ValueError:  # Input is not an integer; ignore the optional value.\n    pass",
        "except ValueError:\n    pass  # Input is not an integer; ignore the optional value.",
    ],
)
def test_documented_handlers_and_plain_exception_pass(tmp_path, handler):
    source = tmp_path / "module.py"
    source.write_text("try:\n    int('bad')\n" + handler + "\n", encoding="utf-8")
    assert style.main(["--enforce-docstrings", str(source)]) == 0


@pytest.mark.parametrize("arguments, code", [(["--help"], 0), (["--unknown"], 2)])
def test_cli_arguments_are_validated(arguments, code):
    with pytest.raises(SystemExit) as exc:
        style.main(arguments)
    assert exc.value.code == code
