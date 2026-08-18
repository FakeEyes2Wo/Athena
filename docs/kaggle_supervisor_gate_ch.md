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
| ideator | SEARCH | `kaggle_list_notebooks`, `kaggle_get_notebook`, `kaggle_list_discussions`, `kaggle_get_discussion` |
| plan | SEARCH | `kaggle_list_notebooks`, `kaggle_get_notebook`, `kaggle_list_discussions`, `kaggle_get_discussion` |
| kaggle_handoff | SEARCH | notebook + discussion 四个算子，外加 web_search/web_fetch 回退 |
| general | SEARCH | 全部算子 |
| data / validate | SEARCH / VALIDATE | 无 |

`kaggle_handoff` Agent 在 SEARCH 首个 ideation 轮前运行一次，把 discussion 与
notebook 证据写成 EDA 工作区的 `KAGGLE_HANDOFF.md` 和机器可读的
`KAGGLE_EVIDENCE.json`（含 notebook ref/version 与 discussion ref 溯源），并
通过 Agent mailbox 把完成信息投递给每个 Ideator，供所有 Ideator Agent 使用。

## Handoff 来源开关

`state.handoff_sources` 控制 Ideation 使用哪些 handoff 来源，前端可经
`settings_set` 配置：

```json
{"patch": {"handoff_sources": ["kaggle", "literature"]}}
```

- `"kaggle"`：启用 Kaggle Handoff Agent；
- `"literature"`：复用现有 `corpus_ref`，向 Ideator 投递文献语料信息；
- `[]`：关闭所有 handoff，Ideator 仅使用本地 EDA / baseline。
- 已生成的 handoff ref 记录在 `state.handoff_refs`（source -> artifact ref），
  断点续传时复用。

工具集经 `runtime.kaggle_tools(agent_type)` 按需解析；plan agent 因注册早于任务理解，
用 callable 惰性求值。

## 组合根

`build_kaggle_stack(download_root, artifacts, client, download)` → `KaggleStack`；
`build_kaggle_tools(stack, names)` 按名字子集构建工具表。
