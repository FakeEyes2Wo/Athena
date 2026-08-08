"""Policy for generated experiment trees inside one worktree."""

from pathlib import Path


class CodeExecutionError(RuntimeError):
    """生成实验代码执行/策略校验失败（原 workflows/search.code_agent 迁移至此）。"""


class GeneratedTreePolicy:
    PROTECTED = {".gitignore", "eval.py", "eval_spec.json"}
    _RUNTIME_NAMES = {"predictions.csv", "labels.csv", "eval_result.json"}

    def validate(self, root: Path, changed_paths: set[str]) -> None:
        root = root.resolve()
        entrypoint = root / "run_experiment.py"
        if not entrypoint.is_file():
            raise CodeExecutionError("generated tree is missing run_experiment.py")
        for raw in sorted(changed_paths):
            posix = raw.replace("\\", "/")
            name = Path(raw).name
            if (
                posix in self.PROTECTED
                or posix == ".athena"
                or posix.startswith(".athena/")
                or name in self._RUNTIME_NAMES
                or name.startswith("athena_logs_")
            ):
                raise CodeExecutionError(f"protected path changed: {raw}")
            candidate = root / raw
            if candidate.is_symlink():
                raise CodeExecutionError(f"symlink rejected: {raw}")
            if not candidate.resolve().is_relative_to(root):
                raise CodeExecutionError(f"escape outside worktree: {raw}")
