"""Deterministic review of generated code diffs."""

import io
import shlex
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

_PROTECTED_FILES = {"eval.py", "eval_spec.json", ".gitignore"}


@dataclass
class ReviewVerdict:
    action: Literal["approve", "revise", "reject"]
    reasons: list[str] = field(default_factory=list)


def _changed_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) >= 4 and parts[3] != "/dev/null":
                paths.add(parts[3].removeprefix("b/"))
        elif line.startswith("+++ b/"):
            paths.add(line[6:])
    return paths


def _added_dependencies(diff: str) -> set[str]:
    dependencies: set[str] = set()
    source = "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    mode: Literal["from", "import"] | None = None
    expect_module = False
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NEWLINE:
                mode = None
                expect_module = False
            elif mode == "import" and token.string == ",":
                expect_module = True
            elif token.type != tokenize.NAME:
                continue
            elif mode == "from":
                if expect_module:
                    dependencies.add(token.string)
                    expect_module = False
                elif token.string == "import":
                    mode = None
            elif mode == "import":
                if expect_module:
                    dependencies.add(token.string)
                    expect_module = False
            elif token.string in {"from", "import"}:
                mode = token.string
                expect_module = True
    except (IndentationError, tokenize.TokenError):
        # 源码无法完整 tokenize（语法错误）→ 返回已收集的依赖子集
        pass
    return dependencies


def review_diff(
    diff: str,
    *,
    allowed_files: set[str],
    declared_dependencies: set[str],
) -> ReviewVerdict:
    paths = _changed_paths(diff)
    protected = sorted(
        path for path in paths if PurePosixPath(path).name in _PROTECTED_FILES
    )
    if protected:
        return ReviewVerdict(
            action="reject",
            reasons=[
                f"protected evaluation file changed: {path}" for path in protected
            ],
        )

    reasons = [
        f"file outside declared scope: {path}" for path in sorted(paths - allowed_files)
    ]
    dependencies = _added_dependencies(diff)
    undeclared = (
        dependencies - declared_dependencies - sys.stdlib_module_names - {"athena"}
    )
    reasons.extend(
        f"undeclared dependency: {dependency}" for dependency in sorted(undeclared)
    )
    return ReviewVerdict(
        action="revise" if reasons else "approve",
        reasons=reasons,
    )
