#!/usr/bin/env python3
"""Athena 代码规范预检查（docs/代码规范.md 的可机检 MUST 规则）。

规则：
1. 导入必须在文件开头 —— 禁止函数体内 import / from ... import
   （带 ``# 延迟导入避免循环依赖`` 注释的除外）
2. 不使用 ``from __future__ import annotations``
3. 公开函数/方法/类必须有 docstring（``_`` 开头与 dunder 方法豁免）
4. 禁止 ASCII 装饰分隔线（``# ═══`` / ``# ───`` / ``# ---`` / ``# ----``）
5. 禁止裸 ``except:``（不指定异常类型）
6. 特定异常处理器必须包含触发条件注释（注释含义仍需人工审查）

退出码 0 = 全部通过；非 0 = 违规并打印 ``file:line`` 与规则编号。
只依赖标准库，供 pre-commit local hook 调用。
"""

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 短 Unicode 横线也是装饰；普通的单横线列表注释不受影响。
SEPARATOR_RE = re.compile(r"^\s*#\s*(?:[─━═]{2,}|[-=]{3,})")
DELAYED_IMPORT_COMMENT = "延迟导入避免循环依赖"

FINDINGS: list[str] = []


def _report(path: Path, lineno: int, rule: str, message: str) -> None:
    FINDINGS.append(f"{path}:{lineno}: [{rule}] {message}")


def check_separators(path: Path, lines: list[str]) -> None:
    """Report decorative comment dividers while allowing ordinary list comments."""
    for token in tokenize.generate_tokens(io.StringIO("\n".join(lines)).readline):
        if token.type == tokenize.COMMENT and SEPARATOR_RE.match(token.string):
            _report(path, token.start[0], "R4", "ASCII 装饰分隔线（# --- / # ═══ 等）")


def check_future_imports(path: Path, tree: ast.AST) -> None:
    """Report future imports prohibited by the project's Python baseline."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            _report(
                path, node.lineno, "R2", "不允许 from __future__ import annotations"
            )


def check_bare_except(path: Path, tree: ast.AST) -> None:
    """Reject handlers that also swallow process exit and cancellation signals."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            _report(path, node.lineno, "R5", "裸 except:（必须指定异常类型）")


def check_exception_comments(path: Path, tree: ast.AST, lines: list[str]) -> None:
    """Require a trigger comment at the start of each specific exception handler."""
    comment_lines = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO("\n".join(lines)).readline)
        if token.type == tokenize.COMMENT
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            continue
        # Only comments attached to this handler count; strings and preceding
        # try-block comments cannot explain why this particular branch runs.
        if not comment_lines.intersection(range(node.lineno, node.body[0].lineno + 1)):
            _report(path, node.lineno, "R6", "特定异常缺少触发条件注释")


def check_function_body_imports(path: Path, tree: ast.AST, lines: list[str]) -> None:
    """Report each local import once, except explicitly documented import cycles."""
    seen: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            position = (child.lineno, child.col_offset)
            if position in seen:
                continue
            seen.add(position)
            span = lines[child.lineno - 1 : child.end_lineno]
            if any(DELAYED_IMPORT_COMMENT in line for line in span):
                continue  # 循环依赖显式豁免
            _report(path, child.lineno, "R1", "函数体内 import（必须放在文件开头）")


def check_docstrings(path: Path, tree: ast.AST) -> None:
    """Require useful nonempty documentation on publicly named definitions."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_"):
            continue
        if not (ast.get_docstring(node) or "").strip():
            kind = "类" if isinstance(node, ast.ClassDef) else "函数"
            _report(path, node.lineno, "R3", f"公开{kind}缺少 docstring：{node.name}")


def _py_files(paths: list[str]) -> list[Path]:
    targets: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            p = ROOT / raw
        try:
            candidates = sorted(p.rglob("*.py")) if p.is_dir() else [p]
            if not candidates or not p.exists():
                _report(p, 1, "R0", "检查目标不存在或目录不包含 Python 文件")
                continue
            for candidate in candidates:
                if not candidate.is_file() or candidate.suffix != ".py":
                    _report(candidate, 1, "R0", "检查目标不是 Python 源文件")
                    continue
                identity = candidate.resolve()
                if identity not in seen:
                    seen.add(identity)
                    targets.append(candidate)
        except OSError as exc:
            # 目录不可访问或路径解析失败时不能把未检查的目标记为通过。
            _report(p, 1, "R0", f"无法枚举检查目标：{exc}")
    return targets


def check_file(path: Path) -> None:
    """Check a source file and fail explicitly when it cannot be read or parsed."""
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        # 文件不可读或不是 UTF-8 时，没有足够内容完成规范检查。
        _report(path, 1, "R0", f"无法读取 Python 源文件：{exc}")
        return
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # 无效 Python 无法形成 AST，必须阻断而不是跳过检查。
        _report(path, exc.lineno or 1, "R0", f"Python 语法错误：{exc.msg}")
        return
    # Parse before token checks so malformed literals produce a single R0 failure.
    check_separators(path, lines)
    check_future_imports(path, tree)
    check_bare_except(path, tree)
    check_exception_comments(path, tree, lines)
    check_function_body_imports(path, tree, lines)
    check_docstrings(path, tree)


def main(argv: list[str]) -> int:
    """Check explicit targets or production source; strict mode also rejects R3."""
    FINDINGS.clear()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce-docstrings", action="store_true")
    parser.add_argument("paths", nargs="*", help="Python files or directories")
    args = parser.parse_args(argv)
    files = _py_files(args.paths or [str(ROOT / "src")])
    for path in files:
        check_file(path)
    FINDINGS.sort()

    # 未完成检查及硬规则始终阻断，严格模式同时阻断公开 API 文档缺失。
    blocking = [f for f in FINDINGS if args.enforce_docstrings or " [R3]" not in f]
    for finding in FINDINGS:
        print(finding)
    if blocking:
        print(
            f"\n{len(blocking)} 处检查失败，阻断提交；规则详见 docs/代码规范.md。",
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
