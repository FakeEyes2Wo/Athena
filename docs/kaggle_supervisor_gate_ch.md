# Kaggle 接入与 Supervisor 人类门

Status: current
Owner: Athena maintainers
Source of truth: `src/athena/kaggle/`, `src/athena/research/supervisor/supervisor.py`, `src/athena/agents/supervisor_agent.py`

## 目标

让 Athena 自动接入 Kaggle 竞赛（下载 / EDA / 检索 / SOTA），并把所有「需要人类拍板」的
决策点收敛到 **SupervisorAgent 单出口**：下载与否、预算是否追加、是否进入 VALIDATE。

## 模式

复用 `auto_validate`：

- `True`（auto，CLI/headless 默认）：零提问，SEARCH 预算用尽自动进入 VALIDATE。
- `False`（交互）：预算用尽停在 `WAITING`，触发 SEARCH 完成门。

## Supervisor 单出口

| 关注点 | 机制 |
|---|---|
| 假设监控 | 只读工具 `read_hypotheses`：pending 假设 + 当前 SOTA + attempts/search_limit |
| 下载门 | `configure_kaggle(enabled, download)`：`download=False` 时不本地下载；kaggle 工具读 `stack.download` |
| 预算门 + VALIDATE 门 | 合并为 SEARCH 完成门 `_budget_gate()`：supervisor 读 `read_hypotheses`，用 answer 问「+N 次 / validate / stop」 |

预算只有一个旋钮 `search_limit`，追加 = `search_limit += N`（N 由人类给具体数字）。
`configure_search` 在 `SEARCH+WAITING` 下会把状态切回 `RUNNING` 并恢复 SEARCH。

## 人类交互：异步 message 循环

不新造同步 ask_user 桥。supervisor 在 answer 里提问并结束 turn，人类下一条 message
回答，supervisor 再调 `configure_search` / `set_phase_decision`。

## 每个 Agent 的最少 Kaggle 工具

`AGENT_KAGGLE_TOOLS`（`wiring.py`）：

| Agent | 阶段 | 工具 |
|---|---|---|
| evaluator | PREPARE | `kaggle_get_competition`, `kaggle_download_data` |
| prepare | PREPARE | `kaggle_run` |
| ideator | SEARCH | `kaggle_list_notebooks` |
| plan | SEARCH | `kaggle_list_notebooks` |
| general | SEARCH | 全部 5 个 |
| data / validate | SEARCH / VALIDATE | 无 |

工具集经 `runtime.kaggle_tools(agent_type)` 按需解析；plan agent 因注册早于任务理解，
用 callable 惰性求值。

## 组合根

`build_kaggle_stack(download_root, artifacts, client, download)` → `KaggleStack`；
`build_kaggle_tools(stack, names)` 按名字子集构建工具表。
