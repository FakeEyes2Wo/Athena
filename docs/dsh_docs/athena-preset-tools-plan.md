# Athena 预设工具（athena_tree / athena_graph / athena_rank / athena_snapshot）— 实施计划

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: 为 DSH Agent 预设 `athena-research` 提供只读研究状态工具（树摘要 / 图预览 / 排序 / 快照）
Source of truth: DSH 预设机制（`agentPresets` 服务、`editing-cordis-compositions` skill、shipped `standard`/`cordis` 预设）、
`src/athena/gui/graph.py`、`src/athena/research/supervisor/ranker.py`、`src/athena/core/research_tree.py`、`src/athena/research/runtime.py`

## 1. 目标与成功标准

为 `athena-research` 预设提供**只读**、**零逻辑重复**的研究状态查看能力，让 Agent 在会话内可复现地一键得到：

1. **树摘要**（`athena_tree`）：SOTA id、hypotheses/experiments 数量与状态分布、关键不变量是否满足。
2. **图预览**（`athena_graph`）：hypothesis graph 的 Mermaid `graph TD`（lineage 实线、supersedes 虚线、SOTA 高亮）。
3. **权威排序**（`athena_rank`）：待选假设按 `ranker.Selector.rank`（权威）排序，而非 `gui.graph.rank_pending`（显示捷径）。
4. **研究快照**（`athena_snapshot`）：SOTA + 预算余量 + phase/status + 待选假设数 + 开放 HumanRequest 的一页概览。

**核心原则**：DSH 侧**不复制任何图算法 / 排序 / 研究业务逻辑**——所有计算 shell 出 canonical Python（`gui.graph` / `ranker.Selector` / `ResearchTree`），DSH 只做「读 + 格式化 + 呈现」。规范 owner 仍是 Python 侧（`athena.gui.graph`、`athena.research.supervisor.ranker`、`athena.core.research_tree`）。

**成功标准**：
- `standingKeyFor('athena-research')` 干净挂载（无「未激活 / 服务冲突 / 包缺失」）。
- 真实会话（`athena-research`）能稳定跑出四处输出，且与 canonical Python 函数结果**逐字段一致**（抽查验证）。
- 工具全程只读、不写 `.athena/*`、不 publish 任何 Service、不改研究状态。

## 2. 背景：当前实现的精确状态（含证据）

### 2.1 DSH 预设机制

| # | 事实 | 证据 |
|---|---|---|
| P1 | `agentPresets` 服务暴露 `list / resolve / read / copy / remove / standingKeyFor`；`copy()` 是唯一 authoring write、host 侧执行、无需沙箱升级；`standingKeyFor(id)` 是挂载校验（组合插件子树，失败即报错） | `agentPresets` Service 契约（`cordis_inspect_query` host/Service/listService → `agentPresets`）；`editing-cordis-compositions` skill「Authoring a preset」「Verifying a change」 |
| P2 | 用户预设根 `${DSH_HOME:-$HOME/.dsh}/.agent-presets/<id>/`；本机 `DSH_HOME=C:\Users\80163\.dsh`，当前**无** `.agent-presets/` 目录（无已作者预设） | `editing-cordis-compositions` skill；本会话 `pwsh` 勘察 |
| P3 | shipped 预设 `standard`/`cordis`/`minimal`/`code` 在部署 config 下（`...\node_modules\@deepseek-ai\dsh\config\agent-presets\`），**只读不可改**，只可 `copy` 后改副本 | `editing-cordis-compositions` skill「Off-limits」 |
| P4 | 预设组成：`agent.cordis.yml`（persona `@deepseek-ai/dsh-persona` 的 `text`；`skill-filesystem` 的 `customSkillDirs` 用 `baseUrl` 指向预设内 `skills/`）+ `preset.yml`（name/description）+ `skills/<name>/SKILL.md` | `standard/agent.cordis.yml`（persona 24-33、skill-filesystem 83-84）；`cordis/agent.cordis.yml`（persona 17-29 `text: |-`、skill-filesystem `customSkillDirs` 255-259、tool-skill 261-262） |
| P5 | tool row 一律用 `@deepseek-ai/dsh-*` 包名；**预设内不能直接放自定义宿主代码包**。自定义工具两条路：① Dynamic Cordis Plugin（临时、重启即失）；② skill 内联 recipe 用已有 `read`/`pwsh` 调 canonical Python | `cordis/agent.cordis.yml`（tool-cordis 245-246 是自指工具集）；`editing-cordis-compositions` skill；`cordis-plugin-development` skill |

### 2.2 Athena 侧 canonical 函数与状态

| # | 事实 | 证据 |
|---|---|---|
| G1 | `gui/graph.py` 纯函数：`build_hypothesis_graph`（18-82）、`topological_order`（85-108）、`cycle_detect`（138-159）、`lineage`（162-170）、`active_hypotheses`（173-188）、`descendants`（191-193）、`best_path`（196-207）、`rank_pending`（210-221）、`supersedes_closure`（224-240）、`refutation_reachability`（243-253）、`run_algorithm`（304-309） | `src/athena/gui/graph.py` |
| G2 | 权威排序 `Selector.rank`（`-score, order`）、`_score`（157-173）、`deduplicate`（74-93）、`RankConfig`（96-106）；`Selector` 构造 `Selector(policy, config=None)` | `src/athena/research/supervisor/ranker.py` |
| G3 | `research_tree.json` version=3，顶层 `{version, sota_id, hypotheses, experiments}`；`ResearchTree.load(path)` / `.to_dict()` / `.best_experiment_id()` / `.pending_hypotheses()` | `src/athena/core/research_tree.py`（SAVE_VERSION 23、to_dict 396-409、load 545-551） |
| G4 | 持久化状态：`.athena/state.json`（`ResearchState`）+ `.athena/research_tree.json`；runtime 在 `research/runtime.py` 装配 | `src/athena/research/runtime.py`（state_path 85、tree_path 86、tree load 122-126） |

> 关键坑位：`rank_pending`（G1，`gui/graph.py:210-221`）按 `(priority, order)` **升序**，与 `Selector.rank` 的权威降序**相反**——见 `search-selection-upgrade-plan.md` 任务 A。本计划中 `athena_rank` **必须用 `Selector.rank`**；任何用 `rank_pending` 的「快览」都要标注其继承该 bug，直到任务 A 合并。

## 3. 阶段一：建立 `athena-research` 预设（持久，必做）

### 任务 T1 — 复制 `standard` 并注入研究图纪律 + 内联预览 recipe

**改动文件**（均在用户预设根，工作区外，写编辑需 escalation——见 §8）：

1. **`copy('standard', 'athena-research', 'Athena 研究 Agent')`**
   - 经临时 Host 插件注入 `agentPresets` 并注册 `preset_copy` 工具（`editing-cordis-compositions` skill 的标准做法：`cordis_define` + `cordis_run`，用完 `cordis_undefine`）。`copy()` 本身 host 侧执行、无需升级。
   - 用 `agentPresets.resolve('athena-research')` 返回的**实际路径**定位后续编辑目标（不猜路径）。

2. **`agent.cordis.yml` — 改 persona `text`**（`@deepseek-ai/dsh-persona`）
   - 保留 `standard` 的工具面与 `{{model}}`/`{{cwd}}` 模板，把 persona 换成「Athena 研究 Agent」身份，并追加「研究图纪律」小节：
     - 阶段机 `TASK_CONFIGURE → PREPARE → SEARCH → VALIDATE → COMPLETED`；
     - 单一 SOTA、lineage 森林 / supersedes 只指祖先 / 两者无环；
     - 假设状态只由真实实验结果推进；prose 不改 hypothesis/experiment/sota；
     - 排序走 `Selector`（prior + strength + novelty − cost）、相似度 ≥0.8 去重、`order` 稳定断序；
     - 证据可溯（evidence_refs / sources）、可证伪、增量。

3. **`agent.cordis.yml` — 给 `skill-filesystem` 加 `customSkillDirs`**
   - 复制 `cordis/agent.cordis.yml:255-259` 的模式（`standard` 的 skill-filesystem 无 `customSkillDirs`）：
     ```yaml
     - id: skill-filesystem
       name: '@deepseek-ai/dsh-skill-filesystem'
       config:
         customSkillDirs:
           - !!js "process.getBuiltinModule('node:url').fileURLToPath(new URL('skills/', baseUrl))"
     ```
   - 目的：让预设自带的 `skills/athena-workflow/SKILL.md` 随预设被发现（本地根解析到预设目录）。

4. **新增 `skills/athena-workflow/SKILL.md` — 内联预览 recipe**
   - frontmatter：`name: athena-workflow`、`description`（何时用：在 Athena 项目里按 PREPARE→SEARCH→VALIDATE 做研究 / 需要看树、图、排序、快照时）。
   - 正文含**四段可复制的 shell recipe**（DSH 只读，调 canonical Python，见 §4 的 shell-out 示例）：
     - 树摘要（`athena_tree` recipe）：`ResearchTree.load('.athena/research_tree.json')` → 打印 SOTA、数量、状态分布；
     - 图预览（`athena_graph` recipe）：`build_hypothesis_graph` → 格式化成 Mermaid `graph TD`；
     - 权威排序（`athena_rank` recipe）：`Selector(EloPolicy()).rank(tree, tree.pending_hypotheses())`；
     - 快照（`athena_snapshot` recipe）：读 `state.json` + `research_tree.json` → SOTA + phase/status + 预算 + pending 数。
   - 显式标注：`rank_pending` 是显示捷径、继承方向 bug（见 §2.2 坑位）；权威排序只用 `Selector.rank`。
   - 显式标注前置条件：`uv run python` 须在**项目根**（`.athena/` 所在处）运行，且 `athena` 包可导入（`uv sync` 已按 `[tool.hatch.build.targets.wheel] packages=["src/athena", ...]` 以 editable 安装）。

5. **`preset.yml` — 重写 name/description**
   ```yaml
   name: Athena 研究 Agent
   description: 遵循 Athena prompt 驱动 ReAct + Supervisor 编排工作流（PREPARE→SEARCH→VALIDATE）的研究/编码 Agent；附带 athena-workflow 工作流 skill 与只读研究状态查看 recipe。
   ```

**验证**：
- 临时插件注册 `preset_validate` 工具调 `agentPresets.standingKeyFor('athena-research')`，无异常返回 = 组合可挂载。
- `agentPresets.list()` 中该预设 `trust=user`、`path` 指向用户根。

**验收**：
- [ ] T1：`standingKeyFor('athena-research')` 干净；`athena-workflow` 出现在该预设会话的 skill 目录；persona 含研究图纪律。

## 4. 阶段二：recipe 工具化（决策 + 可选临时工具）

### 任务 T2 — 把 recipe 工具化为只读工具（先决策，再按需挂 Dynamic 工具）

**两方案对比（决策点，见 §8）**：

| 维度 | 方案 A：skill 内联 recipe（T1 已含） | 方案 B：Dynamic Cordis Plugin 临时工具 |
|---|---|---|
| 持久性 | ✅ 随预设持久（重启仍在） | ❌ 重启即失，需每次会话重挂 |
| 工具形态 | 无真工具 schema；Agent 读 SKILL.md 手动跑 `pwsh` | 真工具（`athena_tree`/`athena_graph`/`athena_rank`/`athena_snapshot` 带 schema、可被 Tool 卡片渲染） |
| 逻辑重复 | ❌ 无（直接 shell 出 canonical Python） | ❌ 无（同样 shell 出 canonical Python，只做调用封装） |
| 维护 | 无代码、纯文档 | 每次会话需 `cordis_define`+`cordis_run`；技术失败需 `cordis_inspect_self` 修复 |
| 服务 realm | 无服务，无需 isolate | 无服务（只 `harness.registerTool`），无需 isolate |

**推荐（决策结论）**：**方案 A 为默认与权威**（持久、零维护、是「Agent 模式」本体）；**方案 B 作为本会话临时便利**，按需挂载，二者 shell 出**同一 canonical Python**，不引入任何逻辑重复。

**若启用方案 B**，改动与验证（临时、不落盘）：
- `cordis_define`（Host 半）注册四个只读工具，`execute` 内用 `harness` 提供的 shell 能力（或复用 `tool-pwsh`/`ctx.get('shell')`，以 `cordis_inspect_query` 确认为准）执行 §4.1 的 shell-out，返回纯文本/JSON。
- 工具**只读**：不写 `.athena/*`、不 publish Service、不 `inject` 任何硬依赖（用 `ctx.get('...')` 处理可选服务）。
- `cordis_run` 激活；技术失败时 `cordis_inspect_self(pluginId, packageId)` 读源码修复同 Plugin，不另建替代。
- 会话结束/不再需要时 `cordis_undefine`（探针类临时能力，不留存）。

**验收**：
- [ ] T2：四工具各跑通一次且输出与 canonical Python 一致；确认全程只读（`.athena/*` mtime 不变）。

### 4.1 shell-out 模板（两种方案共用，写进 SKILL.md 或工具 execute）

```bash
# athena_tree —— 树摘要
uv run python -c "
from athena.core.research_tree import ResearchTree
t = ResearchTree.load('.athena/research_tree.json')
print('SOTA:', t.best_experiment_id())
print('hypotheses:', len(t.hypotheses()), '| experiments:', len(t.experiments()))
from collections import Counter
print('hyp status:', Counter(h.status for h in t.hypotheses()))
print('exp status:', Counter(e.status for e in t.experiments()))
"

# athena_graph —— Mermaid 预览（thin 格式化，不重写图算法）
uv run python -c "
from athena.core.research_tree import ResearchTree
from athena.gui.graph import build_hypothesis_graph
t = ResearchTree.load('.athena/research_tree.json')
g = build_hypothesis_graph(t)
sota = t.best_experiment_id()
print('graph TD')
for n in g['nodes']:
    label = (n['statement'] or '')[:24]
    mark = '[' + (n['status'] or '?') + ']'
    if n['sota']: mark += '(SOTA)'
    print(f\"  {n['id']}[{label}]{mark}\")
for e in g['edges']:
    style = '-->' if e['kind']=='lineage' else '-.->'
    print(f\"  {e['source']} {style} {e['target']}\")
"

# athena_rank —— 权威排序（Selector，非 rank_pending）
uv run python -c "
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.ranker import Selector
from athena.research.supervisor.policy import EloPolicy
t = ResearchTree.load('.athena/research_tree.json')
for h in Selector(EloPolicy()).rank(t, t.pending_hypotheses()):
    print(h.id, 'prio=', h.priority, '|', h.statement[:60])
"

# athena_snapshot —— 一页快照（state.json + tree.json）
uv run python -c "
import json
from pathlib import Path
from athena.core.research_tree import ResearchTree
s = json.loads(Path('.athena/state.json').read_text(encoding='utf-8'))
t = ResearchTree.load('.athena/research_tree.json')
print('phase:', s.get('phase'), '| status:', s.get('status'))
print('search_limit:', s.get('search_limit'), '| concurrency:', s.get('concurrency'))
print('SOTA:', t.best_experiment_id())
print('pending hypotheses:', len(t.pending_hypotheses()))
"
```

> 说明：Mermaid 标签 / 高亮的格式化逻辑**只属于呈现层**（DSH 侧或上面的 thin Python 段），不触碰 `build_hypothesis_graph` 的节点/边语义。`ResearchState` 字段以 `src/athena/research/supervisor/state.py` 为准，快照脚本按需取叶子字段，不整体 dump 对象。

## 5. 依赖与顺序

```
T1（预设本体：copy + persona + customSkillDirs + SKILL.md + preset.yml）
 └─→ T2（recipe 工具化：先做 A/B 决策，再按需挂 Dynamic 工具）
```

- T1 是唯一持久产物，T2 完全依赖 T1 的 SKILL.md recipe（工具只是 recipe 的封装）。
- T1 内部顺序：`copy` → 改 persona → 改 skill-filesystem → 写 SKILL.md → 改 preset.yml → `standingKeyFor`。
- 与 `search-selection-upgrade-plan.md` 任务 A 的交叉依赖：**无硬阻塞**（本计划 `athena_rank` 直接用 `Selector.rank` 绕开 bug）；任务 A 合并后，SKILL.md 中「rank_pending 方向 bug」标注可降级为已修复。

## 6. 验证命令（全量）

```bash
# （无独立 pytest 目标——本计划产物是 DSH 预设，验证靠挂载校验 + 真实会话抽查）

# 1. 挂载校验（临时插件 preset_validate → agentPresets.standingKeyFor）
#    期望返回 'mounted OK'，无 "did not activate" / "published process-global service" / "Cannot find package"

# 2. canonical Python 可导入性冒烟（在项目根）
uv run python -c "from athena.gui.graph import build_hypothesis_graph; from athena.research.supervisor.ranker import Selector; print('ok')"

# 3. 若本计划之外触碰了 Python（本计划不碰），才需要：
uv run pytest -q tests test/unit
```

## 7. 验收标准（逐任务）

- [ ] T1-复制：`agentPresets.resolve('athena-research')` 返回用户根下的组合文件路径；`trust=user`。
- [ ] T1-persona：persona `text` 含阶段机 + 单一 SOTA + 图不变量 + Selector 排序纪律。
- [ ] T1-skill：`skills/athena-workflow/SKILL.md` 存在、frontmatter 合法、四段 recipe 可复制执行。
- [ ] T1-挂载：`standingKeyFor('athena-research')` 无异常。
- [ ] T2-决策：文档明确「方案 A 默认 / 方案 B 临时」及持久性取舍。
- [ ] T2-工具：若挂 Dynamic 工具，四工具输出与 canonical Python 一致；全程只读（`.athena/*` 未变）。

## 8. 决策点与风险

- **工具落点（显式决策）**：skill 内联 recipe（方案 A）为**默认且持久**——它是「Agent 模式」本体、随预设持久、零维护；Dynamic Cordis Plugin（方案 B）为**本会话临时便利**、重启即失。**推荐：T1 必做（A）；T2 按需（B），且 B 的四个工具 shell 出同一 canonical Python，不复制逻辑。**
- **shell 出 canonical Python 而不复制逻辑**：一律 `uv run python -c "from athena.gui.graph import ... / from athena.research.supervisor.ranker import Selector / from athena.core.research_tree import ResearchTree"`；DSH 侧只读文件 + 格式化。**禁止**在 JS/skill 里重写 `build_hypothesis_graph`、`Selector._score`、`deduplicate` 或 `rank_pending` 的排序键。
- **`rank_pending` 方向 bug 交叉引用**：`athena_rank` 必须走 `Selector.rank`（权威）；任何 `rank_pending` 快览都标注继承 `search-selection-upgrade-plan.md` 任务 A 的 bug，直到该任务合并。
- **沙箱 / 审批**：预设根 `${DSH_HOME}/.agent-presets/` 在**会话工作区之外**，`workspace-write` 下编辑副本文件会被拒；`copy()` 本身 host 侧无需升级，但**后续 `write`/`edit` 需 escalation**。本会话审批已禁用（`approval prompts are disabled`），故 T1 的实际写编辑须由具备 escalation 的会话/委派方执行；本计划只产出文档。
- **服务 realm 规则**：四个工具只读、只 `harness.registerTool`、**不 publish 任何 Service**，故**无需 isolate realm**；也不 `inject` 硬依赖（可选服务用 `ctx.get()`）。
- **导入前提**：`uv run python -c` 须在项目根运行且 `athena` 已 editable 安装；SKILL.md 须写明该前置条件，否则 recipe 报 `ModuleNotFoundError`。
- **只读纪律**：工具不得写 `.athena/*`、不得提交事实、不得改 ResearchTree；任何「查看」都要以 `.athena/*` mtime 不变为准验收。

## 9. 规范引用

- DSH 预设作者化 / 挂载校验 / 平面与 realm 规则：`editing-cordis-compositions` skill（`copy` 唯一 authoring write、`standingKeyFor` 校验、`isolate` 规则）。
- 动态工具（若启用方案 B）：`cordis-plugin-development` skill + `cordis_inspect_list/query`（先查 Service/Builtin 再写 Host 半）。
- 图算法 / 排序 canonical owner：`src/athena/gui/graph.py`、`src/athena/research/supervisor/ranker.py`、`src/athena/core/research_tree.py`（规范 owner 声明见 `docs/README.md`）。
- 排序方向 bug 交叉引用：`docs/dsh_docs/search-selection-upgrade-plan.md` 任务 A。
- 研究状态模型：`src/athena/research/supervisor/state.py`（`ResearchState`）、`src/athena/research/runtime.py`（`.athena/state.json` + `.athena/research_tree.json` 装配）。
