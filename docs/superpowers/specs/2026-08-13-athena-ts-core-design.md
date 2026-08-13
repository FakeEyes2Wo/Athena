# athena-ts 重写设计（M0：工程底座）

日期：2026-08-13
状态：已批准（brainstorming 流程）

## 1. 背景与目标

把 Athena（Python，src/athena + src/athena_tui，约 30k LOC + 27k 测试）完整复刻到 TypeScript/Node，运行于 `athena_ts/`，目标是**摆脱 Python 运行时**——编排、TUI、agent 框架全部跑在 Node 上。

**执行边界**（已确认）：SEARCH/VALIDATE 阶段 LLM 生成的数据科学实验脚本仍以 Python 子进程（`uv run <entrypoint> --request … --output …`）执行，保留 Python 数据科学生态。`script_runner.py` 的 Bundle 契约原样移植为 TS 端 `uv` 子进程调用。

**方案**（已确认）：A —— 契约先行 + 自底向上保真移植。所有 pydantic 模型先翻译为 zod schema（契约层），再按 M0→M5 顺序逐阶段实现；每阶段以现有 pytest 作为行为 spec 对拍验证。

**技术栈**（已确认）：Node.js ≥22 LTS · npm workspaces · TypeScript 5.x strict + NodeNext(ESM) · **zod**（模型校验）· **cordis v4（rc.8，锁定精确版本）** 作为 DI/服务/生命周期组合根 · vitest（对拍 pytest）。

## 2. 总体拆解（M0–M5）

| 阶段 | 子项目 | 覆盖的 Python 源码 | 内容 | 依赖 |
|---|---|---|---|---|
| **M0** | `@athena/core` | `core/`（除 `core/agent/`、`core/tool*.py`）、`utils/` 通用工具 | 工程底座：zod 模型层、workspace/git/artifact 服务（cordis）、组合根骨架、错误/重试/ID | — |
| **M1** | `@athena/agent` | `core/agent/*`、`memory/*`、`utils/single_turn_chat.py` | provider（OpenAI 兼容）、agent runtime（turn/phase/events）、tool registry、session、context/compaction/rollout | M0 |
| **M2** | `@athena/research` | `research/*`、`agents/*`、`execution/*` | ResearchRuntime、supervisor 全流程（PREPARE→SEARCH→VALIDATE）、research 事件、script_runner（uv 子进程）、execution monitor | M0, M1 |
| **M3** | `@athena/cli` | `athena_tui/*`、`cli.py`、`src/main.py` | 终端 UI（照 athena_tui 复刻）、Athena-cli、headless 入口 | M2 |
| **M4** | `@athena/server` | `app_server/*` | server/client/protocol/transport/lifecycle/thread_manager（对接 athena-gui） | M2 |
| **M5** | `@athena/papers` | `research/paper_*`、`retrieval/*`、`agents/ideator` | 论文流水线（markdown/rag/scout/source/survey）、检索 | M0, M1 |

每阶段各自一个 spec → plan → 实现 → 对拍（pytest 当行为 spec）。

## 3. 技术栈决策（M0 落地，全项目沿用）

- **运行时/工具**：Node.js ≥22 LTS；npm workspaces（根 `package.json` `workspaces: ["packages/*"]`）；TypeScript 5.x，`strict: true`，`module/nodeNext`，`target ES2022`。
- **模型层**：zod（pydantic 替代），纯函数层，**不依赖 cordis**。
- **DI/生命周期**：`cordis`（锁定 `4.0.0-rc.8`，后续升级显式评估）。承担 Python 侧"ResearchRuntime 唯一组合根"的职责。
- **测试**：vitest（pytest 替代）。服务测试内构造 `new Context()` 注册服务。
- **git**：系统 `git` 子进程（`node:child_process.execFile`），对齐 Python 的 `asyncio.create_subprocess_exec("git", …)`。
- **ID**：`node:crypto.randomBytes`，复刻 `new_id(prefix)` 的 `{prefix}_{12hex}` 格式与 prefix 校验。
- **命名**：Python snake_case 文件 → kebab-case `.ts`；类 PascalCase、函数/方法 camelCase；zod schema 以 `*Schema` 命名，类型 `z.infer`。
- 不引入 UI/其他运行时第三方依赖（TUI 库选择延迟到 M3 spec）。

### cordis v4 风险与降级预案
v4 处于 rc（`4.0.0-rc.8`），API 可能漂移。措施：
- 精确锁定版本；所有 cordis 交互集中在 `src/services/` 与 `src/app.ts`，模型层零依赖。
- 若 v4 API 大改导致不可用，降级路径为 v3（Koishi 经典 DI）或替换为轻量手写 service locator——该降级仅影响组合层，不影响 zod 模型层。

## 4. cordis v4 架构模式（全项目适用）

| Python 概念 | cordis v4 对应 |
|---|---|
| `ResearchRuntime`（唯一组合根） | 根 `Context` / `App`（`new Context()`），`ctx.plugin(...)` 组装 |
| 依赖注入（构造器传入） | `Service` 子类 + `static inject`（或 `ctx.using`）声明依赖 |
| 生命周期（pause/resume/stop） | `app.start()/stop()`；`Service` 的 `start()/stop()` 生命周期钩子 |
| `RuntimeEvents`（OutputEvent/StateEvent） | `ctx.on()/emit()/parallel()`（异步事件） |
| supervisor 的 per-experiment worktree 作用域 | `ctx.isolate(keys)` 服务隔离 |
| 各阶段（prepare/search/validate） | cordis 插件 |

服务注册形态（示例，M0 落地验证）：
```ts
import { Context, Service } from 'cordis'

class GitWorkspace extends Service {
  constructor(ctx: Context) {
    super(ctx, 'gitWorkspace', false)  // (ctx, 服务名, 是否立即可用)
  }
  static inject = ['workspace', 'artifacts']
  async create(spec: ProjectSpec): Promise<GitWorkBranch> { /* _git → execFile */ }
  async commit(msg: string, files: string[]): Promise<CommitHash> { /* … */ }
}
// app.ts：ctx.plugin(GitWorkspace) …；ctx.gitWorkspace.… 访问
```

## 5. M0 范围与目录

**范围内**：`src/athena/core/` 中除 `core/agent/`、`core/tool.py`、`core/tool_types.py` 之外的全部（模型与基础设施服务）。
**范围外**：agent 运行时与工具执行语义（M1，含 `core/tool*.py`、`utils/single_turn_chat.py`）、research 引擎（M2）、TUI/CLI（M3）、server（M4）、论文流水线（M5）。

```
athena_ts/
  package.json                     # workspaces: ["packages/*"]
  tsconfig.base.json
  vitest.workspace.ts
  packages/
    athena-core/                   # ← M0（本 spec）
      package.json                 # name: @athena/core
      tsconfig.json
      src/
        models/                    # 纯 zod，无 cordis 依赖
          contracts.ts
          research-models.ts
          thread-models.ts
          research-tree.ts
        services/                  # cordis Service 子类
          workspace.ts
          git-workspace.ts
          artifact-store.ts
          persistence.ts           # atomicWriteJson 工具
        app.ts                     # createAthenaApp(): 根 Context + 注册基础服务
        errors.ts                  # AthenaError 层级
        retry.ts
        id.ts
      test/                        # vitest
        models/  services/  app.test.ts
    athena-agent/   athena-research/   athena-cli/   athena-server/   athena-papers/
                                  # M1–M5 占位（空 package，仅占位）
```

## 6. 模型翻译规范（zod 约定）

| pydantic | zod / TS |
|---|---|
| `BaseModel` | `z.object({...})` + `export type X = z.infer<typeof XSchema>` |
| `Field(description="…")` | `.describe("…")` |
| `Field(default_factory=list)` | `.default([])` |
| `Field(default=None)` | `.nullable().default(null)` |
| `Field(ge=0)` / `ge=N` | `.min(N)` |
| `Field(allow_inf_nan=False)` | `.finite()` |
| `Literal["A","B"]` / `StrEnum` | `z.literal("A").or(z.literal("B"))`；判别联合用 `z.discriminatedUnion` |
| `Annotated[str, StringConstraints(pattern=r"\S")]`（NonBlankText） | `z.string().regex(/\S/)` |
| `dict` | `z.record(...)` |
| `ValidationError` | `ZodError` → 统一包装为 `AthenaValidationError` |
| `to_dict()/from_dict()` | `JSON.stringify` / `JSON.parse` + `Schema.parse()` |
| `save(path)/load(path)` | 写/读 JSON 文件 + `Schema.parse()` 校验 |
| `Model.dict()`（含 extra 字段丢弃） | `.parse()` 后结构即定型，超集字段拒绝（strict object） |

翻译原则：
- 每个 Python 模型文件 → 一个 TS 模块文件，导出同名 schema 与类型。
- 校验失败语义对齐：pydantic 丢弃未知字段 → zod `z.object(...)` 默认 strip；若 Python 侧 `model_config` 声明 `extra='forbid'` 才用 `.strict()`（以源码为准逐一对拍）。
- `research-tree.ts` 为纯数据结构（非 cordis 服务），实现 `ResearchTree` 全部方法（add_hypothesis/get/pending/transition/complete/attach_artifact/set_sota/best/to_dict/from_dict/save/load），行为以 pytest 对拍。

## 7. 模块清单与映射（M0）

| Python 源 | LOC | TS 模块 | 要点 |
|---|---|---|---|
| `core/contracts.py` | 58 | `models/contracts.ts` | NonBlankText / ArtifactRef / CommitHash、`newId(prefix)`、EventEnvelope、ErrorRecord、ArtifactStore interface |
| `core/research_models.py` | 77 | `models/research-models.ts` | Hypothesis、HypothesisBatch、ExperimentPlan、EvalResult、ComparisonVerdict 等 |
| `core/thread_models.py` | 24 | `models/thread-models.ts` | AthenaThread、AthenaTurn |
| `core/research_tree.py` | 547 | `models/research-tree.ts` | ExperimentStatus、Experiment、ResearchTree 全方法 + JSON save/load |
| `core/artifact_store.py` | 118 | `services/artifact-store.ts` | `digestRef(data)`（sha256）、LocalArtifactStore（put/get bytes/text） |
| `core/workspace.py` | 86 | `services/workspace.ts` | workspace 结构与接口（目录规划、数据/代码/报告分区） |
| `core/git_workspace.py` | 506 | `services/git-workspace.ts` | 系统 git 子进程、worktree 管理、diff/commit/restore、review marker、reconcile |
| `core/persistence.py` | 24 | `services/persistence.ts` | `atomicWriteJson`（tmp + fsync + os.replace 原子写） |
| `core/retry.py` | 98 | `retry.ts` | 重试 + 退避（对齐 ErrorRecord 的 retry/degrade/fatal 分级语义） |
| `core/tool.py` + `core/tool_types.py` | 277 | **归 M1** | BaseTool/ToolRegistry/@tool 与 tool_types 纯数据容器，与 agent 事件模型耦合，随 M1 移植 |
| `utils/single_turn_chat.py` | 134 | **归 M1** | 单轮聊天工具（LLM 调用），与 provider 耦合 |

> `core/tool*.py`、`utils/single_turn_chat.py` 与 agent 运行时强耦合，统一推迟到 M1 移植，避免 M0 依赖 agent。M0 仅覆盖 `core/` 的模型与基础设施服务。

## 8. 错误处理与重试

- `errors.ts`：`AthenaError` 基类 + 子类（`AthenaValidationError`、`AthenaNotFoundError`、`AthenaIntegrityError`、`AthenaClosedError` 等），映射 Python 的 `ValueError / FileNotFoundError / OSError` 语义。
- `retry.ts`：指数退避重试；分级对齐 `ErrorRecord.severity`（`retry` / `degrade` / `fatal`）。
- cordis 插件加载错误、服务未注册访问，用 cordis 原生告警 + `AthenaError` 包装。

## 9. 测试与验收（M0）

**移植的 pytest → vitest 对拍**：
| pytest | vitest | 说明 |
|---|---|---|
| `tests/test_core_model_contracts.py` | `test/models/contracts.test.ts` | zod schema 校验契约 |
| `tests/test_core_public_api.py` | `test/models/public-api.test.ts` | 公开 API 面 |
| `tests/test_research_tree_v2.py` | `test/models/research-tree.test.ts` | 树序列化/反序列化，用 `test/fixtures/research_tree_v2.json` 对拍 |
| `test/unit/test_git_workspace.py` | `test/services/git-workspace.test.ts` | git 服务（`new Context()` + 注册） |
| `test/unit/research/test_research_tree_scheduling.py` | `test/models/research-tree-scheduling.test.ts` | 树的调度语义（priority/patience/order） |

**fixtures**：拷贝 `test/fixtures/research_tree_v2.json` 到 `athena_ts/packages/athena-core/test/fixtures/`。

**验收标准**：
1. `npm test`（vitest）全绿。
2. 对拍：同一输入（含 fixture 与手工构造用例）在 TS 端输出与 Python pytest 断言一致。
3. `createAthenaApp()` 返回的根 Context 上可访问 `ctx.workspace / ctx.gitWorkspace / ctx.artifacts` 三个基础服务。
4. 无 Python 依赖：`athena-core` 包 `npm test` 不需要 Python 环境（git 子进程除外，属系统工具）。

## 10. 风险与开放项

- **cordis v4 rc**：API 漂移风险，见 §3 降级预案；实现首个服务时锁定实际签名并记录到 spec 注释。
- **zod 与 pydantic 语义差异**：`extra` 字段策略、`allow_inf_nan`、数字精度等——逐模型对拍 pytest 断言，不盲从翻译。
- **git 子进程**：Windows 环境 git 行为差异（路径、换行），测试覆盖 Windows。
- **`research-tree-scheduling` 测试归属**：树在 M0，但调度语义可能隐含 supervisor 约定——若测试依赖 M2 模块，则迁至 M2 spec。
- M1–M5 spec 各自在对应里程碑启动时编写。

## 11. M0 实施增补（2026-08-13 实现记录）

M0 实现完成（`athena_ts/packages/athena-core`，vitest 79 全绿、`tsc --noEmit` 无错误）。以下为实施中相对本 spec/plan 的偏差，均不影响 zod 模型层或行为对拍。

### 范围增补
- `models/research-data-models.ts`（`research/data_models.py` 79 LOC）并入 M0 模型层（支撑 `test_core_model_contracts` 对拍）。
- `tests/test_core_public_api.py` 实际只测 M1 符号（`core.__all__`/`agent.__all__`）→ 移至 M1。
- `test/unit/test_artifact_store.py`、`test/unit/test_retry.py`（M0 子集）补充进 M0 对拍清单。

### cordis@4.0.0-rc.8 偏差（仅组合层）
- **打包与 NodeNext 不兼容**：rc.8 的 `lib/` 只有打包后的 `index.js` + 各子模块 `.d.ts`（无 `lib/context.js` 等子模块 `.js`），`import { Context } from "cordis"` 在 `moduleResolution: NodeNext` 下被判定为"仅类型"。**故 `tsconfig.base.json` 采用 `module: ESNext` + `moduleResolution: Bundler`**（运行时仍输出合法 Node ESM，`.js` 扩展名 import 不变）。这是对 §3 锁定 `NodeNext` 的唯一偏离。
- **服务注册 API**：rc.8 的 `Service` 构造函数为 `(ctx, name)`（2 参，非 spec §4 示例的 3 参 immediate）；服务经 `ctx.provide(name, value)` 在插件 fiber 内注册，且需 `await ctx.plugin(...)` 后可用。故 `createAthenaApp()` 为 **async**（返回 `Promise<Context>`），三服务为普通类 `LocalArtifactStore`/`LocalGitWorkspace` + `workspace` 对象，非 `Service` 子类。

### zod v4 语义偏差
- **`z.number()` 在 v4 始终拒绝 `NaN`/`Infinity`**（`ZodNumber.isFinite` 硬编码为 true，`.finite()` 为 no-op）。因此：
  - `Hypothesis.priority` 的 `allow_inf_nan=False` 语义由基类拒绝（消息为 `expected number` 而非 pydantic 的 `finite`）；`research-tree-scheduling` 对拍改断言 `expected number`。
  - Python `EvalResult.primary`（`float`，允许 inf）在 TS 侧仍为 `z.number()`（拒绝 inf），故 `complete_experiment` 的 `math.isfinite` 手动检查在 TS 里前置拦截（测试以纯对象构造 `successfulEval(primary=Infinity)` 绕过 schema，使 `Number.isFinite` 检查抛原文 `evaluation primary metric must be finite`）。`Experiment` superRefine 中的 finite 分支因 schema 前置拒绝成为死代码，保留以对齐 Python 结构。
- **`z.record` 需 2 参**（`z.record(key, value)`，无 1 参重载），映射 `dict[str, …]` 时显式写 `z.record(z.string(), …)`。

### 其他
- **`TimeoutError` 非 Node 全局**（Node 24 无此全局）：在 `services/retry.ts` 内定义并导出，对齐 Python 内建 `TimeoutError`。
- **`atomicWriteJson` fsync**：Windows 上只读 fd 的 `fsyncSync` 抛 EPERM，改为打开写句柄（`openSync(tmp, "w")` → `writeFileSync(fd, …)` → `fsyncSync(fd)`）。
- **git-workspace 测试**：Windows git 默认 `core.autocrlf` 会在 `restore` 时改写 LF，测试 repo 增加 `git config core.autocrlf false`（断言不变）。
