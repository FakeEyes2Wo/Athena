# Athena 测试指南

Status: current
Owner: Athena maintainers
Last verified: 2026-07-30
Source of truth: `pyproject.toml`, `tests/`, `test/unit/`

## 环境

```bash
uv sync
```

## 全量测试

```bash
uv run pytest -q tests test/unit
```

Windows 工作区亦可使用：

```bash
.venv/Scripts/python.exe -m pytest -q tests test/unit
```

## 分层

- `tests/`：AI4ML 领域、GUI gateway、持久化、工作流及集成测试。
- `test/unit/app_server/`：从独立 app_server 迁入的黑盒/竞态测试，不依赖 v3
  旧接口。
- `test/unit/`：Agent、Tool、Memory、GitWorkspace 与执行 handler 单元测试。
- `test/fixtures/`：跨版本持久化合同夹具。

## 门禁

新增行为至少覆盖正常路径和一个错误或降级路径。外部网络、真实模型及 Kaggle 写操作
不得成为单元测试前提；应使用依赖注入及 fake adapter。任何未解释的失败、skip 或
flaky 均阻塞交付。
