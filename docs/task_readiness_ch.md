# 接真实评测任务时暴露的七个缺陷

Status: current
Owner: Athena maintainers
Last verified: 2026-08-29
Source of truth: `src/athena/cli.py`, `src/athena/research/splitter.py`,
`src/athena/research/supervisor/validation.py`,
`src/athena/core/agent/provider.py`, `src/athena/core/agent/settings.py`,
`src/athena/research/supervisor/supervisor.py`, `src/athena/research/report.py`

把 Athena 接到一个有正式提交规范的外部评测任务（太阳/恒星耀发建模闭环）上时，
以下七处在单元测试里全绿、真接任务时全都挡路。它们的共同点是**不报错**：
每一条都让 loop 继续跑完并给出一个数字，只是那个数字不再是你以为的东西。

## 一、平台数据划分从来没有执行过

`prepare_phase` 里有一整段"平台自己切分 train/search/final"的逻辑，条件是
`config.dataset_path` 与 `config.target_column` 同时非空。而 `cli._runtime_options`
只把 `--data` / `--target` 拼进**任务提示词文本**，从不把它们传给 `ResearchRuntime`
——尽管 `ResearchConfig` 早就声明了这两个字段。

于是走 CLI 的每一次运行都落在 else 分支：划分由评估器 Agent 自己做，平台对
"候选到底看没看过留出集"这件事没有任何控制权。同一条链路上的 `tolerance`
（判胜容差）与 `data_root`（远端算力分发数据集用）也是一样，永远是默认值。

修复：`_runtime_options` 补齐 `dataset_path` / `target_column` / `group_column` /
`split_seed` / `tolerance` / `data_root`。

`--data` 可以是 CSV、目录，也可以是 Kaggle URL，而 `materialize_csv_split` 只吃
带目标列的本地 CSV。所以 `_platform_split_dataset` 只在参数确实描述了这种情形时
才打开平台划分，其余形状保持原有行为，不会把一个目录喂进去炸掉 PREPARE。

## 二、划分没有分组键

`split_ids` 是纯随机行级洗牌。行与行不独立时——同一个活动区的连续帧、同一颗恒星
光变曲线切出来的窗口、同一个受试者的重复测量——同一实体的行会同时落进 train 和
final。

这类泄漏最难查的地方在于：**分数只会更好看**。没有任何一层会报错，评估器是干净的，
探针也过，只是留出集不再是留出集。

修复：`split_ids(..., groups=...)` 与 `materialize_csv_split(..., group_column=...)`，
按组洗牌、同组不拆；CLI 侧是 `--group-column`。

两个实现细节：

- 组先按键排序再洗牌，划分只取决于 seed，而不取决于 dict 的插入顺序。
- 比例是按**行数**而不是组数切的。组大小不均时按组数切会把完全错误的数据量分给
  各个 split。目标值向下取整，因此单行组的划分与旧实现逐条一致，既有项目不会漂移。

## 三、VALIDATE 复跑的超时是 SEARCH 的 1/30

`_execute_predictions` 调 `execution.run(...)` 时没传 `timeout_s`，拿的是
`ExecutionRuntime.run` 的默认值 **120 秒**；而 SEARCH 跑同一条 `experiment.json`
命令用的是 `experiment_timeout_s`（默认 **3600 秒**）。

后果：任何超过两分钟的实验——只要牵涉真训练或稍大的特征提取就都超过——
过得了 SEARCH，却在 VALIDATE 因超时挂掉，日志里看起来像候选自己坏了。

修复：`run_validation_plan(..., experiment_timeout_s=...)` 一路传到
`_execute_predictions(..., timeout_s=...)`，由 `phase_runner` 从
`state.experiment_timeout_s` 取值——复跑不该被比它要复现的那次运行更严的预算卡住。

## 四、`LLM_PROVIDER=qwen` 配好也用不了

`settings.ALLOWED_PROVIDERS` 早就含 `"qwen"`，`DEFAULT_BASE_URLS` 也配好了
DashScope 兼容端点，但 `create_provider` 没有对应分支，直接抛
`unsupported LLM_PROVIDER='qwen'`。`api_key()` 的回退链里也没有
`DASHSCOPE_API_KEY`——即使按官方文档配好环境，也只会撞上 "Missing LLM API key"。

修复：`QwenProvider`（DashScope 兼容模式就是 OpenAI Chat Completions 协议，
直接复用 `ResponsesProvider`），结构化输出按 DeepSeek 那一档走
`json_object` + schema 注入 prompt——兼容端点对 `json_schema` 的支持随模型而变，
`json_object` 是各型号都成立的那一档。

## 五、`extra_body` 用的是 DeepSeek 的字段名，却发给所有后端

```python
"extra_body": {"thinking": {"type": "disabled"}},   # 无条件
```

这是 DeepSeek 的字段名。OpenAI 对未知请求参数直接回 400；DashScope 认的是
`enable_thinking`。于是"关掉思考"这个本意只在 DeepSeek 上成立，另外两个后端
要么报错、要么被无视。

修复：`ResponsesProvider._extra_body()` 按 `provider_kind` 返回对应字段，
OpenAI 一档返回空 dict（不发 `extra_body`），开关由
`settings.enable_thinking()` 统一控制。

## 六、采样参数写死，既申报不了也调不动

`AgentConfig.temperature` 是 dataclass 里的字面量 `0.1`，没有 seed；整个进程
没有任何入口能改。外部评测规范普遍要求"可固定并申报采样参数"，写死等于两头
都不满足。

修复：`temperature` / `seed` 默认值改从 `settings` 取（`LLM_TEMPERATURE` /
`LLM_SEED`，或 `config.toml` 的 `[llm]`）。`seed` 只有显式配置了才进请求——
凭空多一个未知字段会让不认它的后端直接 400。

## 七、失败的那一轮，对 Agent 来说等于没发生

失败的 `PlanTurnResult` 一直带着 `kind` 与 `error`（manifest 不合法、命令非零
退出并附 stderr 摘要、predictions 目录为空、打分失败），但它们只进了 evidence
artifact 和事件流，**没有任何一条回到写代码的那个 Agent 面前**。

Agent 下一轮收到的仍然是 `Continue Plan …; turns used: N`，既不知道上一轮跑没跑、
也不知道为什么没跑成，只能把同一份 manifest 原样再交一次，直到 turn 预算耗尽。
同一个 `ModuleNotFoundError` 可以就这样烧掉一整条 Plan。

这是 `handoff_block` / `hypothesis_block` 那条教训的第三处落点：**`context_refs`
是死信道，能到 model 的只有 `content`**。

修复：`failure_block()` + `PlanState.last_failure`。失败摘要在
`_record_turn_failure` 里落进持久状态（失败结果按契约不带 `next_state`，
因此不会被随后的结算路径覆盖），下一轮 `_run_one_turn` 读取并**同时清空**——
反馈只该出现在紧接着的那一轮，否则早已修好的错误会一直挂在 prompt 里误导后续
每一轮。

## 附：两处不算缺陷但挡路的东西

**`uv.lock` 被 `.gitignore` 忽略了。** 应用的 lock 文件是"同样的代码、同样的环境"
唯一的保证；忽略它意味着每次 clone 都解析当天最新的版本，而那正是复现检查要排除的。
已从 `.gitignore` 移出并提交。

**最终报告只有一个分数。** `build_final_report` 原本只有 SOTA、计数、验证分数、
待选假设、实验记录。补了三节，全部是树里已有、只是没被渲染的数据：

- **迭代对照**：每个实验相对 SOTA 的增减与判定。只报一个最终分数看不出任何一步
  改动是否真的有用。
- **失败实验与原因**：缺了它，"跑 3 次成 2 次"和"跑 2 次全成"在最终材料里
  长得一模一样。
- **下一步验证方案**：把待选假设按"干预 → 预期观测"展开。光有一句 statement
  看不出该做什么实验、看到什么才算成立。
