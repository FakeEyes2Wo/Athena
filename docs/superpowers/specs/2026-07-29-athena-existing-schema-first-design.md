# Athena 既有 Schema 优先设计

> 日期：2026-07-29
> 状态：已确认
> 范围：Task 3、Task 4 及后续领域类型迁移

## 1. 目标

消除 `athena.core.schemas` 与各领域 `types.py` 之间的重复类体，同时保留项目已经使用的字段名、默认值、校验规则和导入路径。类型可以迁移，但迁移不得变成重新设计。

## 2. 优先级

当现有代码、旧规格与收口计划冲突时，依次采用：

1. `athena.core.schemas` 中已经存在的字段合同；
2. 其他现有生产类型的字段合同；
3. 新规格中不改变既有合同的要求。

因此：

- `MetricDef.description` 继续必填；
- `ExperimentOutcome` 继续使用 `eval`，不改名为 `eval_result`；
- `ProcessingRecord` 继续使用 `col`、`operation`、`params`、`timestamp`；
- 迁移不得顺带改变 Pydantic、dataclass、默认值、序列化结果或异常行为。

## 3. 唯一所有权

每个类型只能有一个类体。兼容模块可以导入并重导出该对象，但不得复制定义。

迁移采用单提交原子操作：移动原类体、更新消费方、保留必要的兼容导入，并以 `is` 断言证明新旧导入是同一对象。

迁移不是硬指标。若迁出会形成 `domain.types -> core.schemas -> domain.types` 的循环，当前 owner 保持不变，领域模块只从 owner 导入。不得为追求目录对称而新增共享 DTO 层、包装类或懒加载机制。

## 4. Task 3 调整

### 4.1 Evaluation

`MetricDef`、`EvalSpec`、`EvalResult`、`ComparisonVerdict` 以 `core.schemas` 的现有实现为准。

本任务删除 `evaluation/types.py` 中的重复类体，使其兼容重导出 core 中的同一对象。`evaluation` 内部代码可以继续从 `evaluation.types` 导入，以免一次扩大迁移范围，但运行时类型身份必须与 `core.schemas` 相同。

### 4.2 Data

`ProcessingRecord` 可从 `data.operations` 迁至 `data.types`，但必须原样保留现有四个字段及 dataclass 行为。`data.operations.ProcessingRecord` 保持兼容导入。

不得改为 `column`、`timestamp_utc`，也不得在迁移任务中改变时间生成方式。

### 4.3 Brainstorm

`FalsifiabilityError` 可从 `brainstorm.generate` 迁至 `brainstorm.types`。迁移仅改变所有权，不改变异常基类、触发条件或消息合同。

### 4.4 Retrieval

`PaperRef` 与 `HFModelRef` 当前没有 core 重复定义，本任务不改其字段或所有权。

## 5. Task 4 调整

`ExperimentOutcome` 以 core 现有字段 `eval`、`verdict`、`is_sota` 为准。Task 4 不创建使用 `eval_result` 的第二个模型。

若 `experiment.types` 需要公开该类型，先兼容重导出 core 对象。只有在依赖方向无环、所有消费方可在同一提交迁移时，才可把原类体迁出 core；迁移后 core 必须重导出同一对象。

新建 `Experiment` 时可以引用现有 `Hypothesis`、`ExperimentPlan` 和 `ExperimentOutcome`，不得复制它们的字段。

## 6. 测试与验收

每次所有权变更必须覆盖：

- 新旧导入路径使用 `is` 指向同一对象；
- 迁移前后的字段名与顺序一致；
- 默认值、冻结行为和 JSON schema 保持一致；
- 现有序列化与消费方测试通过；
- 静态扫描确认没有第二个同名类体；
- `git diff --check` 通过。

测试不得仅断言源码文本，也不得为新规格修改既有字段名后再证明新实现正确。

## 7. 明确不做

- 不新增统一 `shared.types` 或第二套 schema 容器；
- 不因迁移引入 DTO 转换器；
- 不同时维护 core 版与 domain 版模型；
- 不在所有权任务中清理无关格式、重命名字段或改变业务行为；
- 不以“领域目录完整”为理由增加跨文件跳转。

本设计覆盖 `2026-07-29-athena-full-closeout-design.md` 与 `2026-07-29-athena-full-closeout.md` 中关于上述既有类型字段和 owner 的冲突内容。
