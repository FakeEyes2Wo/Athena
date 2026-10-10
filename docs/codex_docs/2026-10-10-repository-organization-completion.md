# 项目目录整理完成记录

日期：2026-10-10。本次根据用户要求通过两个 subagent 阅读目录、构建入口和
Python/GUI 调用路径；未使用 superpowers skills。

## 完成内容

- 根测试入口统一为 `tests/`。旧 `test/` 仅包含 357 个 `.pyc` 缓存，已清除；
  Rust fixture include 已改为 `tests/fixtures/clarification/`。
  前端 fixture 路径经实际测试确认正确，未修改。
- `athena_ts/` 的 147 个已跟踪文件及本地目录全部删除；构建与打包移除 TS 后端
  分支和 npm 命令。Python 后端与 GUI 前端保留。
- `reproduction/` 整体移入本地 `submission/reproduction/`，原 16 个已跟踪文件
  退出 Git 索引；新增 `/submission/` 忽略规则，投稿和复现材料不进入提交。
- 9 份 `code_spec/` 历史设计移入 `docs/archive/code-spec/`；`asd.md`、`draft.md`
  改为 `docs/archive/notes/` 下的具名历史笔记，保留内容并修正 3 处路径引用。
- 新增[目录导航](../repository-layout.md)、[归档索引](../archive/README.md)和
  [冗余机制记录](../reviews/2026-10-10-redundant-mechanisms.md)，更新 README 导航，
  将 GUI 双后端设计标为历史约定。

## 验证证据

- Python：`python -m pytest -q tests/unit/core/test_human_request.py`，11 passed。
- Rust：`cargo test --manifest-path athena-gui/src-tauri/Cargo.toml --lib human_contract_fixtures_deserialize_all_valid_replies --offline`，1 passed。
- 前端：`npm --prefix athena-gui test -- src/lib/__tests__/tauri-bridge.test.ts`，10 passed。
  首次受沙箱路径访问限制，获准在沙箱外重跑后通过。
- `node --check scripts/build.cjs` 通过；Node VM 隔离验证 6 种构建路由：
  默认 Python、跳过前端/Rust、release、package，以及两种旧 TS 请求的拒绝。
  外部命令被 mock，未重制发布安装器。
- 11 份归档内容与 HEAD 比较通过，只允许 3 处目录引用替换及 Git 换行规范化。
- 迁移前后 41 个 reproduction/submission 文件 SHA-256 一致，既有材料保留。
- 既有 11 个已修改/未跟踪文本文件 SHA-256 保持不变；新目录导航、归档索引和
  冗余记录中的 25 个链接均可解析；`git diff --cached --check` 通过。
- Git 索引中 `athena_ts/`、`reproduction/`、`submission/`、`test/` 均无条目；
  `submission/` 忽略规则生效。构建入口不再引用已删除 TS 后端，实际 fixture 加载路径正确。

## 边界

冗余记录包含 factory 能力探测、命令输入重复校验和 PREPARE 注册样板三项，
同时记载不同调用边界与合并风险；本次未修改这些机制。
既有 7 个运行时代码文件修改、2 个删除、未跟踪报告/提示词/错误记录和发布产物
不纳入本次 commit。旧 GUI、error-ledger 和模块简化任务未完成的事项仍保留；
历史 TS 记录不再代表当前目录。这里清理的是当前 Git 跟踪，不重写历史提交。
用户最后明确仅整理 Git 仓库，后续不再调整 submission 内部材料。
曾尝试同步 `human_request.py` 的旧路径注释，但提交钩子触发了该文件既有的
future import/类 docstring 规范问题，因此撤回该注释修改，未夹带源码规范重构。
