#!/usr/bin/env python3
"""Athena 代码规范预检查（docs/代码规范.md 的可机检 MUST 规则）。

规则：
1. 导入必须在文件开头 —— 禁止函数体内 import / from ... import
   （带 ``# 延迟导入避免循环依赖`` 注释的除外）
2. 不使用 ``from __future__ import annotations``
3. 公开函数/方法/类必须有 docstring（``_`` 开头与 dunder 方法豁免）
4. 禁止 ASCII 装饰分隔线（``# ═══`` / ``# ───`` / ``# ---`` / ``# ----``）
5. 禁止裸 ``except:``（不指定异常类型）

退出码 0 = 全部通过；非 0 = 违规并打印 ``file:line`` 与规则编号。
只依赖标准库，供 pre-commit local hook 调用。
"""

import ast
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

# 装饰分隔线：注释以 3 个以上 -/=/━/─/═ 开头（如 # ---、# ---- 标签 ----）
SEPARATOR_RE = re.compile(r"^\s*#\s*[─━═=-]{3,}")
# 裸 except 的行号（校验用）
DELAYED_IMPORT_COMMENT = "延迟导入避免循环依赖"

FINDINGS: list[str] = []


def _report(path: Path, lineno: int, rule: str, message: str) -> None:
    FINDINGS.append(f"{path}:{lineno}: [{rule}] {message}")


def _no_docstring(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    if not body:
        return True
    first = body[0]
    return not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.value.value.strip()
    )


def _is_public_skip(node: ast.AST) -> bool:
    name = node.name  # type: ignore[attr-defined]
    return name.startswith("_")


def check_separators(path: Path, lines: list[str]) -> None:
    for i, line in enumerate(lines, 1):
        if SEPARATOR_RE.match(line):
            _report(path, i, "R4", "ASCII 装饰分隔线（# --- / # ═══ 等）")


def check_future_imports(path: Path, lines: list[str]) -> None:
    for i, line in enumerate(lines, 1):
        if re.match(r"^\s*from\s+__future__\s+import", line):
            _report(path, i, "R2", "不允许 from __future__ import annotations")


def check_bare_except(path: Path, tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            _report(path, node.lineno, "R5", "裸 except:（必须指定异常类型）")


def check_function_body_imports(path: Path, tree: ast.AST, lines: list[str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                span = lines[child.lineno - 1 : child.end_lineno]
                if any(DELAYED_IMPORT_COMMENT in line for line in span):
                    continue  # 循环依赖显式豁免
                _report(path, child.lineno, "R1", "函数体内 import（必须放在文件开头）")


def check_docstrings(path: Path, tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _is_public_skip(node):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name.startswith("__") and node.name.endswith("__")
            ):
                continue  # dunder 方法豁免
            if _no_docstring(node):
                kind = "类" if isinstance(node, ast.ClassDef) else "函数"
                _report(
                    path, node.lineno, "R3", f"公开{kind}缺少 docstring：{node.name}"
                )


def _py_files(paths: list[str]) -> list[Path]:
    targets: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            p = ROOT / raw
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.py")))
        elif p.suffix == ".py":
            targets.append(p)
    return targets


def check_file(path: Path) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    lines = source.splitlines()
    check_separators(path, lines)
    check_future_imports(path, lines)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return  # 语法错误由编译/其他 hook 报告
    check_bare_except(path, tree)
    check_function_body_imports(path, tree, lines)
    check_docstrings(path, tree)


def main(argv: list[str]) -> int:
    enforce_docstrings = "--enforce-docstrings" in argv
    argv = [a for a in argv if a != "--enforce-docstrings"]
    files = _py_files(argv) if argv else sorted((ROOT / "src").rglob("*.py"))
    for path in files:
        check_file(path)
    FINDINGS.sort()

    # 硬规则（R1/R2/R4/R5）始终阻断提交；R3 docstring 默认只提示，
    # 传 --enforce-docstrings 时才阻断（存量 150+ 处，避免首次提交即卡死）。
    blocking = [f for f in FINDINGS if " [R3]" not in f]
    for finding in FINDINGS:
        print(finding)
    if blocking:
        print(
            f"\n{len(blocking)} 处硬规则违规，阻断提交；规则详见 docs/代码规范.md。",
            file=sys.stderr,
        )
        return 1
    if FINDINGS:
        print(
            f"\n{len(FINDINGS)} 处 R3 docstring 提示（未阻断）；"
            "可运行 --enforce-docstrings 强制。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
