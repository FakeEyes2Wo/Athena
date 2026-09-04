"""Turn user-supplied dataset arguments into the platform's split contract.

Both entry points -- ``Athena-cli run`` and ``Athena-tui`` -- have to answer the
same two questions before a runtime is built: which file (if any) the platform
should split itself, and what absolute path an agent will see in its prompt.
Getting either wrong is silent, so the rules live here once rather than in each
front end.
"""

from pathlib import Path

__all__ = ["dataset_path_for_prompt", "platform_split_dataset"]


def platform_split_dataset(data: str | Path | None, target: str | None) -> Path | None:
    """Return the CSV the platform should split itself, or None.

    ``--data`` is free-form: a CSV, a directory of images, a Kaggle URL. Only a
    local CSV with a named target can be split by ``materialize_csv_split``;
    handing it anything else raises inside PREPARE. So the platform-owned split
    turns on exactly when the arguments describe one, and stays off otherwise --
    which is the historical behaviour for every other shape of input.

    A caller that needs the split (grouped rows, where a row-level shuffle leaks
    silently and only makes the score look better) should report what this
    returned rather than assume it.
    """
    if not target or data is None:
        return None
    path = Path(data)
    if path.suffix.lower() != ".csv" or not path.is_file():
        return None
    return path.resolve()


def dataset_path_for_prompt(data: str | Path) -> str:
    """Absolutize a local dataset path before it goes into the task text.

    ``--data`` is resolved against the front end's working directory, but every
    agent runs with its own workspace (or the project root) as cwd. A relative
    path therefore means two different things at the two ends, and the agent's
    end is the one that is wrong.

    Real run (2026-08-29): ``--data ../data/TESS-SF/windows/model_input.csv``
    passed the CLI's existence pre-check, then the first agent spent its whole
    turn budget hunting for it -- ``../data/...`` from the project root resolves
    to a sibling of the project, not of the CLI. Non-paths (a Kaggle URL) are
    passed through untouched.
    """
    path = Path(data)
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return str(data)
