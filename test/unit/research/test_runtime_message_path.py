"""An ordinary message to a running runtime must not crash on a missing alias.

`runtime_control.message` reads `runtime._auto_seed_task` to decide whether
the first message seeds the task. `ResearchRuntime` stores that flag on its
config and surfaces the private names through `_CONFIG_FIELDS`, where the entry
was missing -- so every human message raised

    AttributeError: 'ResearchRuntime' object has no attribute '_auto_seed_task'

Only the seeding path (`start_task`) avoided it, so a TUI run would start and
then refuse every later message, which is the entire point of the TUI.
Found on 2026-09-05 while trying to tell a live run that its baseline was
misaligned.
"""

from pathlib import Path


from athena.research import ResearchRuntime


def _runtime(tmp_path: Path, **options) -> ResearchRuntime:
    return ResearchRuntime(project_root=tmp_path, model="m", **options)


def test_auto_seed_task_is_readable_through_the_private_alias(tmp_path) -> None:
    assert _runtime(tmp_path, auto_seed_task=True)._auto_seed_task is True
    assert _runtime(tmp_path, auto_seed_task=False)._auto_seed_task is False


def test_auto_seed_task_defaults_to_false(tmp_path) -> None:
    assert _runtime(tmp_path)._auto_seed_task is False


def test_every_control_flag_the_command_path_reads_has_an_alias(tmp_path) -> None:
    """The message path touches these three; a missing one is a crash, not a bug report."""
    runtime = _runtime(tmp_path, auto_seed_task=True, auto_validate=True)

    for name in ("_auto_seed_task", "_auto_validate", "_started"):
        getattr(runtime, name)
