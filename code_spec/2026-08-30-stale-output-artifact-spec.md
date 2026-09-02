# Spec：陈旧实验产物必须被拒绝（Output Freshness）

日期：2026-08-30
状态：draft
范围：`PlanRunner.run_turn`、`Validation._execute_predictions`、实验产物打包/打分链路
目标：阻止“命令没产出任何新文件，却把 base 里的陈旧产物当本次结果打分并标 SUCCEEDED”的静默失效。

---

## 1. 问题

`experiment.json` 声明：

```json
"outputs": { "predictions": "predictions" }
```

命令执行后，`predictions/` 目录内容与 base commit 完全相同。当前框架只检查：

```python
if not predictions_dir.is_dir() or not any(p.is_file() for p in predictions_dir.rglob("*")):
    return failure("output_failed", ...)
```

于是：

- 陈旧目录被视为“有产物”；
- `pack_directory` 把陈旧字节打成 artifact；
- 可信评估器给它打分；
- 分数等于基线 → 表现为 tie；
- `complete_experiment` 仍标 `SUCCEEDED`；
- 如果陈旧产物碰巧更高，会被推上 SOTA。

这是“安静的成功”：没有错误、没有空目录、没有 NaN，只有一个看似合理的数字。

---

## 2. 核心边界

### B1. 声明的输出必须是本次运行产生的

任何被 `manifest.outputs` 声明的输出目录，在命令执行前不得保留旧内容。

运行时必须先把旧输出归档到版本目录，命令结束后原输出目录中出现的任何文件都必须来自本次命令。

### B2. `predictions` 是硬性必需输出

无论 PREPARE 还是 SEARCH / VALIDATE：

- `manifest.outputs["predictions"]` 必须存在；
- 命令结束后该目录必须存在；
- 目录内至少有一个文件；
- 目录中的文件必须是本次运行后出现的（因为旧版本已归档，原目录为空）。

### B3. `report` 在 PREPARE 中硬性必需，在 SEARCH 中保持可选

- PREPARE：`report` 必须存在且非空；
- SEARCH：`report` 可缺失，但若存在，也不得是运行前遗留的旧文件；
- VALIDATE：一般不要求 report，但若 manifest 声明了 report，同样不能是陈旧文件。

### B4. 不得依赖 git diff 作为 freshness 判据

`git diff` 只能作为“工作区是否有改动”的辅助检查，不能作为“输出是否新鲜”的充分条件：

- Agent 改了源码但没写预测，`diff.paths` 仍非空；
- 输出目录可能被 `.gitignore` 忽略，git 看不到；
- git diff 对“删除旧文件并生成同内容新文件”也不一定能反映真实新鲜度。

### B5. 归档范围只限声明的输出目录

- 只能移动 `manifest.outputs` 中声明的相对路径；
- 不得移动工作区根、源码目录、其他未声明目录；
- 路径必须通过 `resolve_workspace_path` 校验，禁止绝对路径 / `..` 逃逸；
- 旧版本统一放入 worktree 外的 `<workdir>.output-history/<version>/`，不参与打包、不覆盖新输出。

### B6. 失败必须走现有失败通道

检测到陈旧/缺失输出时：

- SEARCH / PREPARE：返回 `PlanTurnResult(kind="output_failed")`，不产生可信分数，不提交实验。
- VALIDATE：抛出明确 `ValueError`，进入现有“修复重试”或“失败反馈”通道。

### B7. 不得引入对 mtime 的强依赖

版本化归档是主判据；mtime 只可作为附加诊断，不作为唯一判据。

---

## 3. 设计选择：版本化归档输出根，而不是内容比对

### 方案 A：运行前后指纹比较

```text
before = fingerprint(predictions_dir)
run commands
after = fingerprint(predictions_dir)
if before == after: fail
```

优点：不删除可能被命令当作输入的旧文件。
缺点：

- 确定性模型可能合法地生成完全相同的输出，指纹相同会被误杀；
- 需要额外指纹实现；
- 依赖“内容必须变化”这个不一定正确的假设。

### 方案 B：运行前把旧输出按版本归档（本 spec 采用）

```text
archive(old_outputs -> <workdir>.output-history/<version>)
run commands
assert_output_roots(output_roots)
```

优点：

- 最强保证：命令结束后目录里的任何文件只可能来自本次运行；
- 实现简单、不依赖 git、不依赖 mtime；
- 旧产物不丢失，按版本保留，可回滚；
- 直接消灭“base 陈旧产物伪装成新产物”的根因；
- 与“输出目录应当是输出、不应当是输入”的语义一致。

代价：

- 命令如果试图把旧输出当输入，会失败。
  但这本身就是违规：输出目录不是输入目录。

---

## 4. 最小实现（简洁版，带版本化）

**设计原则：不引入新类、不改 manifest、不新增依赖；旧产物不物理删除，而是按版本归档。**

### 4.1 核心函数

```python
# research/output_freshness.py

import shutil
from pathlib import Path
from collections.abc import Mapping


class OutputFreshnessError(RuntimeError):
    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        super().__init__(
            f"{name} output produced no new artifact: {path}. "
            "The command exited successfully but did not write any file; "
            f"the previous version was archived under {path}.output-history."
        )


def archive_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    version: str,
) -> None:
    """运行前把旧输出按版本归档，并在原路径重建空目录。

    version 由调用方传入，例如 ``plan_id``、``f"{plan_id}-{turn}"`` 或时间戳。
    旧目录被移动到 worktree 外的 ``workdir.parent / f"{workdir.name}-output-history" / version / rel``，
    不丢失、可回滚；原路径保证只剩本次运行写的文件。
    """
    history = workdir.parent / f"{workdir.name}-output-history" / version
    for rel in outputs.values():
        root = resolve_workspace_path(workdir, rel)
        if root.exists():
            target = history / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(target))
        root.mkdir(parents=True, exist_ok=True)


def restore_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    version: str,
) -> None:
    """把某一版本的旧输出恢复到原路径（回滚用）。"""
    history = workdir.parent / f"{workdir.name}-output-history" / version
    for rel in outputs.values():
        root = resolve_workspace_path(workdir, rel)
        saved = history / rel
        if saved.exists():
            if root.exists():
                shutil.rmtree(root)
            shutil.move(str(saved), str(root))


def assert_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    required: set[str],
) -> None:
    """断言所有 required 输出目录存在且至少有一个文件。"""
    for name, rel in outputs.items():
        if name not in required:
            continue
        root = resolve_workspace_path(workdir, rel)
        if not root.is_dir() or not any(root.iterdir()):
            raise OutputFreshnessError(name, root)
```

关键点：

- `archive_output_roots` 不删除旧产物，只“版本化移出”到 worktree 外的历史目录。
- 旧版本可通过 `restore_output_roots` 回滚。
- 命令结束后原输出目录里的文件只可能来自本次运行。
- 不需要 mtime、不需要 git diff、不需要前后指纹。
- 不需要 `OutputRoots` dataclass，`manifest.outputs` 本身就是配置源。
- 历史目录不参与打包，也不应被 `manifest.outputs` 声明。

### 4.2 接入示例

`PlanRunner.run_turn`：

```python
version = f"{plan_id}-{state.turns_used}"

try:
    archive_output_roots(self.workdir, manifest.outputs, version=version)
except Exception as exc:
    return await self._failure(plan_id, "output_failed", str(exc))

for argv in manifest.commands:
    ...

required = {"predictions"}
if state.kind == "PREPARE":
    required.add("report")

try:
    assert_output_roots(self.workdir, manifest.outputs, required=required)
except OutputFreshnessError as exc:
    return await self._failure(plan_id, "output_failed", str(exc))
```

`Validation._execute_predictions`：

```python
version = f"validate-{input.validation_key}"

archive_output_roots(workdir, manifest.outputs, version=version)

for argv in manifest.commands:
    ...

assert_output_roots(workdir, manifest.outputs, required={"predictions"})
```

### 4.3 错误信息

至少包含：

- 缺失/空目录名；
- 绝对路径；
- 明确说明“命令没有生成任何新的 `<output>` 文件，可能存在陈旧产物被当作结果”。

示例：

```text
output_failed: predictions directory did not contain any new artifact:
<workdir>/predictions. The command exited successfully but produced no
prediction files; stale files from the parent commit were removed before
the run, so a non-empty directory now means this run actually wrote them.
```

---

## 5. 集成改动

### 5.1 `PlanRunner.run_turn`

当前顺序：

```python
for argv in manifest.commands:
    run(...)

predictions_dir = workdir / manifest.outputs["predictions"]
if not predictions_dir.is_dir() or not any(...):
    return failure(...)
predictions_ref = pack_directory(...)
...
```

改为：

```python
version = f"{plan_id}-{state.turns_used}"
archive_output_roots(workdir, manifest.outputs, version=version)

for argv in manifest.commands:
    run(...)

required = {"predictions"}
if state.kind == "PREPARE":
    required.add("report")
assert_output_roots(workdir, manifest.outputs, required=required)

predictions_ref = pack_directory(...)
...
```

注意：

- `archive_output_roots` 必须在第一个命令执行前调用；
- 如果归档失败，直接 `output_failed`；
- `assert_output_roots` 之后不再需要原先仅“非空”的检查，可替换。

### 5.2 `Validation._execute_predictions`

当前顺序：

```python
for argv in manifest.commands:
    execution.run(...)

predictions_dir = ...
if not predictions_dir.is_dir() or not any(...):
    raise ValueError(...)
```

改为：

```python
version = f"validate-{input.validation_key}"
archive_output_roots(workdir, manifest.outputs, version=version)

for argv in manifest.commands:
    execution.run(...)

assert_output_roots(workdir, manifest.outputs, required={"predictions"})
...
```

### 5.3 `prepare_phase` / 其他调用

`run_prepare_plan` 最终也走 `PlanRunner.run_turn`，因此 PREPARE 自动获得 freshness 保护，不需要单独改动。

### 5.4 错误恢复

VALIDATE 的 `finally` 中仍保留：

```python
await git.restore_paths(workspace, tuple(manifest.outputs.values()))
```

但顺序应在 `assert_output_roots` 之后；如果断言失败，`finally` 仍把工作区恢复到已审阅树，避免污染。

---

## 6. 严苛边界测试

### T1 陈旧产物 + 命令什么都不写 → 必须失败

- 预先在 `predictions/` 放与 base 相同文件；
- `commands` 为空或成功退出但不写文件；
- 期望：`output_failed` / `ValueError`；
- 不产生 `pack_directory`，不进入 evaluator，不标 SUCCEEDED。

### T2 命令真正写入新预测 → 必须成功

- 预先放陈旧文件；
- 命令删除旧文件并写入新 `pred.csv`；
- 期望：通过，打包的是新文件。

### T3 命令写到错误路径 → 必须失败

- 命令把结果写到 `other/`，不写 `predictions/`；
- 期望：`output_failed`，提示缺失 `predictions`。

### T4 输出目录原本不存在 → 命令创建后成功

- base 没有 `predictions/`；
- 命令创建目录并写文件；
- 期望：成功。

### T5 输出目录原本存在且命令只读不写 → 必须失败

- 命令成功退出，但没有修改 `predictions/` 下任何文件；
- 归档后原目录为空；
- 期望：失败。

### T6 报告输出行为保持

- PREPARE：
  - 缺 report → 失败；
  - 有 report → 成功。
- SEARCH：
  - 缺 report → 仍允许成功；
  - 有 report 但陈旧 → 不允许。

### T7 路径安全

- 输出路径为绝对路径 → 拒绝；
- 输出路径含 `..` → 拒绝；
- 输出路径逃逸工作区 → 拒绝。

### T8 只归档声明的输出目录

- 工作区里有 `source/`、`data/` 等其他目录；
- 归档后这些目录不能被移动或删除；
- 历史目录中能看到旧版本，且可恢复。

### T9 失败后工作区恢复

- VALIDATE 输出断言失败；
- `finally` 仍执行 `restore_paths`；
- 工作区恢复到已审阅树；
- 旧版本仍保留在历史目录中。

### T10 不依赖 git

- 输出目录被 `.gitignore` 忽略；
- `git diff` 为空；
- 仍能通过归档/断言机制判断输出新鲜度。

---

## 7. 对现有测试的影响

以下测试会因新行为改变而需要更新：

- `test_execute_predictions_packs_predictions_directory`
  - 现在必须先让命令真正写出预测，或在该测试中显式调用 `archive_output_roots`/构造真实写入；
  - 当前它预先创建 `predictions/` 且 `commands=[]`，按新规则应失败。
- `test_validation_rerun_gets_the_same_budget_search_gave_the_experiment`
  - 同样需要命令写入输出；
  - 并需适配 `CommandRequest` 执行 API。
- `test_validate_predicts_heldout`
  - 需在“复跑换靶”场景下加入“陈旧输出必须失败”的断言。

这些测试红并不是“测试过时”这么简单：它们恰好证明当前链路缺少 freshness 硬约束。

---

## 8. 实施顺序

1. 新增 `archive_output_roots` / `restore_output_roots` / `assert_output_roots` 工具函数。
2. 修改 `PlanRunner.run_turn`。
3. 修改 `Validation._execute_predictions`。
4. 更新受影响的旧测试。
5. 新增 T1–T10 边界测试。
6. 全量跑 research / validation / execution 测试。

---

## 9. 验收标准

- 任何“命令成功但没写预测”的候选都不再产生可信分数。
- 任何陈旧产物都不会进入 `pack_directory`。
- 任何陈旧产物都不会让实验标 `SUCCEEDED`。
- PREPARE 的 report 行为保持不变。
- SEARCH 的 report 可选行为保持不变。
- 版本化归档只影响声明的输出目录，旧版本可恢复。
- `git diff` / `.gitignore` 不影响 freshness 判断。
- T1–T10 全部通过。
- 既有 8 个红色测试更新后全部转绿。
