# M0 @athena/core 工程底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `athena_ts/` 用 TypeScript 完整移植 Python `src/athena/core/`（模型层 + workspace/git/artifact 基础设施服务 + 组合根骨架），以 zod + cordis v4 重写，并移植对应 pytest 为 vitest 对拍。

**Architecture:** 模型层为纯 zod schema（无 cordis 依赖）；基础设施（LocalArtifactStore / LocalGitWorkspace）为可直接构造的普通类（测试与 Python 一致）；`createAthenaApp()` 用 cordis v4 根 `Context` 注册这三个服务作为组合根骨架。行为以 pytest 为准逐一对拍。

**Tech Stack:** Node ≥22 · npm workspaces · TypeScript 5.x strict + NodeNext(ESM) · zod · cordis@4.0.0-rc.8（锁定）· vitest · 系统 git 子进程（`execFile`）。

## Global Constraints

- **运行时**：Node ≥22 LTS；npm workspaces 根 `package.json` `workspaces: ["packages/*"]`；TS `strict: true`、`module: NodeNext`、`target: ES2022`、ESM。
- **模型层零 cordis**：`src/models/*` 只依赖 zod，不 import cordis。
- **cordis 锁定**：`cordis@4.0.0-rc.8`（精确版本，不许 `^`）。只出现在 `services/` 与 `app.ts`。
- **git 调用**：一律 `execFile("git", ["-C", cwd, ...args], { maxBuffer: 100 * 1024 * 1024 })`，`check` 默认 true、失败抛 `GitWorkspaceError`（消息 = Python `GitWorkspaceError(f"git {...} 失败: {stderr[:200]}")` 的等价文本）。
- **ID**：`node:crypto.randomBytes(6).toString("hex")` 复刻 `newId(prefix)`（12 hex）；prefix 校验与 Python 一致（非空、含至少一个字母数字、其余仅字母数字下划线，否则 `throw new TypeError(...)`——Python 抛 `ValueError`，统一 `AthenaValidationError`）。
- **错误消息对拍**：pytest 用正则匹配子串（如 `"status"`、`"worktree"`、`"mapping key"`、`"cycle"`、`"already has an experiment"`）。所有 `ResearchTree.from_dict` / `add_*` / `transition_*` / `Experiment` 校验的抛错消息必须包含 Python 源码里的原文字串（逐行照抄 `src/athena/core/research_tree.py`、`workspace.py`、`artifact_store.py` 的 raise 文案）。
- **zod 校验错误**：统一经 `parseOrThrow(schema, data)` → 抛 `AthenaValidationError`，消息格式 `"<dotted field path>: <zod message>"`（zod v4 `issue.path` 用 `.` 连接），使"字段名子串匹配"（如 pytest 的 `match="status"`、`match="worktree"`）成立。zod `superRefine` 产生的自定义 issue 用 Python 同款文案作 `message`。
- **序列化**：`model_dump(mode="json")` 语义 → `toJSON<T>(x): T`（`JSON.parse(JSON.stringify(x))`，深拷贝 + 去 undefined）。`save/load` 用 `services/persistence.ts` 的 `atomicWriteJson`（tmp + fsync + rename，`newline="\n"`、`indent=2`、`ensure_ascii=false` 等价物）。
- **命名**：文件 kebab-case；schema 变量 `XxxSchema` + `export type Xxx = z.infer<typeof XxxSchema>`；函数/方法 camelCase；类 PascalCase。
- **测试**：vitest；每任务 TDD（先写失败测试 → 跑失败 → 实现 → 跑过 → commit）。异步测试直接用 `async` 函数。临时目录用 `fs.mkdtemp` + `afterEach` 清理（等价 `tmp_path`/`TemporaryDirectory`）。
- **fixture**：`test/fixtures/research_tree_v2.json` 拷入 `packages/athena-core/test/fixtures/`。
- **对拍范围**：M0 只移植下列 pytest 文件的 M0 子集（见各任务）。`tests/test_core_public_api.py`（仅测 M1 符号 `core.__all__`/`agent.__all__`）与 `test_retry.py` 的 LLM 流重连部分（Agent/StreamEvent/ToolRegistry）**归 M1**。

## File Structure

```
athena_ts/
  package.json                  # workspaces
  tsconfig.base.json
  vitest.workspace.ts
  packages/athena-core/
    package.json                # name @athena/core, type module
    tsconfig.json
    src/
      id.ts                     # newId(prefix)
      errors.ts                 # AthenaError 层级 + parseOrThrow + formatZodError
      models/
        contracts.ts            # NonBlankText/ArtifactRef/CommitHash、EventEnvelope、ErrorRecord、ArtifactStore interface
        thread-models.ts        # AthenaThread、AthenaTurn
        research-models.ts      # Hypothesis、HypothesisBatch、ExperimentPlan、EvalResult、ComparisonVerdict
        research-data-models.ts # DataCard、ColumnSummary、DataProfile、MetricSpec、TaskMetaData
        research-tree.ts        # ExperimentStatus、Experiment、ResearchTree（含 superRefine 校验）
      services/
        workspace.ts            # GitWorkBranch、GitDiff、GitWorkspaceError、resolveWorkspacePath、BinaryDiffWriter、GitWorkspace interface
        persistence.ts          # atomicWriteJson
        artifact-store.ts       # digestRef/digestFromRef、LocalArtifactStore
        retry.ts                # isTransientError、retryAsync
        git-workspace.ts        # LocalGitWorkspace
      app.ts                    # createAthenaApp()：cordis 根 Context + 注册 3 服务
      index.ts                  # M0 公开面导出
    test/
      fixtures/research_tree_v2.json
      id.test.ts  errors.test.ts
      models/contracts.test.ts  models/research-models.test.ts
      models/research-tree.test.ts  models/research-tree-scheduling.test.ts
      services/retry.test.ts  services/artifact-store.test.ts  services/persistence.test.ts
      services/git-workspace.test.ts  app.test.ts
    athena-agent/  athena-research/  athena-cli/  athena-server/  athena-papers/   # 占位 package
```

Python 源 → TS 映射（各任务内展开）：`contracts.py(58)`、`thread_models.py(24)`、`research_models.py(77)`、`research/data_models.py(79)`、`research_tree.py(547)`、`workspace.py(86)`、`persistence.py(24)`、`artifact_store.py(118)`、`retry.py(98)`、`git_workspace.py(506)`。

---

### Task 1: 脚手架 monorepo + @athena/core package

**Files:**
- Create: `athena_ts/package.json`, `athena_ts/tsconfig.base.json`, `athena_ts/vitest.workspace.ts`, `athena_ts/.gitignore`
- Create: `athena_ts/packages/athena-core/package.json`, `athena_ts/packages/athena-core/tsconfig.json`
- Test: `athena_ts/packages/athena-core/test/smoke.test.ts`

**Interfaces:**
- Consumes: —（首任务）
- Produces: 可 `npm install` + `tsc --noEmit` + `vitest run` 的最小工作区；后续任务在此包内加文件。

- [ ] **Step 1: 建根文件**

`athena_ts/package.json`:
```json
{
  "name": "athena-ts",
  "private": true,
  "type": "module",
  "workspaces": ["packages/*"],
  "scripts": {
    "build": "tsc --build packages/athena-core",
    "test": "vitest run"
  },
  "devDependencies": {
    "typescript": "^5.5.0",
    "vitest": "^2.1.0"
  }
}
```

`athena_ts/tsconfig.base.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "skipLibCheck": true,
    "declaration": true,
    "sourceMap": true,
    "isolatedModules": true
  }
}
```

`athena_ts/vitest.workspace.ts`:
```ts
export default ["packages/*"]
```

`athena_ts/.gitignore`:
```
node_modules/
dist/
```

- [ ] **Step 2: 建包文件**

`athena_ts/packages/athena-core/package.json`:
```json
{
  "name": "@athena/core",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "exports": { ".": "./dist/index.js" },
  "scripts": {
    "build": "tsc",
    "test": "vitest run"
  },
  "dependencies": {
    "cordis": "4.0.0-rc.8",
    "zod": "^4.0.0"
  },
  "devDependencies": {
    "@types/node": "^22.0.0"
  }
}
```

`athena_ts/packages/athena-core/tsconfig.json`:
```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": { "rootDir": "src", "outDir": "dist" },
  "include": ["src"]
}
```

- [ ] **Step 3: 写冒烟测试**

`athena_ts/packages/athena-core/test/smoke.test.ts`:
```ts
import { describe, expect, it } from "vitest"

describe("workspace scaffold", () => {
  it("runs vitest in the package", () => {
    expect(1 + 1).toBe(2)
  })
})
```

- [ ] **Step 4: 安装并验证**

Run:
```bash
cd athena_ts && npm install --legacy-peer-deps && npx tsc --noEmit -p packages/athena-core && npm test
```
Expected: `npm install` 成功（cordis rc 的 peerDeps `@cordisjs/plugin-*` 经 `--legacy-peer-deps` 跳过）；`tsc` 无错误；`vitest` 1 passed。

> 若 cordis@4.0.0-rc.8 安装失败（rc 下线/peer 冲突），如实记录并停止，不要用其它版本替代——这是 spec 的锁定约束。

- [ ] **Step 5: Commit**

```bash
cd athena_ts && git add . && git commit -m "feat(athena-ts): scaffold npm workspace + @athena/core package"
```

---

### Task 2: id.ts + errors.ts + contracts.ts + tests

**Files:**
- Create: `packages/athena-core/src/id.ts`
- Create: `packages/athena-core/src/errors.ts`
- Create: `packages/athena-core/src/models/contracts.ts`
- Test: `packages/athena-core/test/id.test.ts`, `test/errors.test.ts`, `test/models/contracts.test.ts`

**Interfaces:**
- Consumes: —
- Produces:
  - `newId(prefix: string): string`
  - `AthenaError`（`name` + `message`）、`AthenaValidationError`、`AthenaNotFoundError`、`AthenaIntegrityError`、`AthenaClosedError`
  - `parseOrThrow<T>(schema: z.ZodType<T>, data: unknown): T`
  - `toJSON<T>(x: T): T`
  - `NonBlankText` schema、`ArtifactRef`、`CommitHash`（zod alias）、`EventEnvelopeSchema`、`ErrorRecordSchema`、`ArtifactStore` interface
  - `EventEnvelope`、`ErrorRecord` 类型

- [ ] **Step 1: 写失败测试**

`test/id.test.ts`:
```ts
import { describe, expect, it } from "vitest"
import { newId } from "../src/id.js"

describe("newId", () => {
  it("prepends prefix and 12 hex chars", () => {
    const id = newId("run")
    expect(id.startsWith("run_")).toBe(true)
    expect(id.slice(4)).toMatch(/^[0-9a-f]{12}$/)
  })
  it("rejects invalid prefixes", () => {
    expect(() => newId("")).toThrow(/alphanumeric/)
    expect(() => newId("bad prefix")).toThrow(/alphanumeric/)
    expect(() => newId("bad-prefix")).toThrow(/alphanumeric/)
  })
})
```

`test/errors.test.ts`:
```ts
import { describe, expect, it } from "vitest"
import { AthenaValidationError } from "../src/errors.js"

describe("AthenaValidationError", () => {
  it("carries name and message", () => {
    const err = new AthenaValidationError("boom")
    expect(err.name).toBe("AthenaValidationError")
    expect(err.message).toBe("boom")
    expect(err).toBeInstanceOf(Error)
  })
})
```

`test/models/contracts.test.ts`（移植 `tests/test_core_model_contracts.py` 的契约部分）:
```ts
import { describe, expect, it } from "vitest"
import { newId } from "../../src/id.js"
import { EventEnvelopeSchema } from "../../src/models/contracts.js"
import { AthenaThreadSchema } from "../../src/models/thread-models.js"

describe("canonical contracts construct real domain records", () => {
  it("newId and EventEnvelope", () => {
    expect(newId("run").startsWith("run_")).toBe(true)
    const envelope = EventEnvelopeSchema.parse({
      kind: "experiment.started",
      source: "orchestrator",
      payload: { ref: "artifact://input" },
      state_version: 0,
    })
    expect(envelope.payload.ref).toBe("artifact://input")
  })
  it("thread and turn", () => {
    const thread = AthenaThreadSchema.parse({
      thread_id: "thread-1",
      session_id: "session-1",
      status: "running",
      context_ref: "artifact://context",
    })
    const turn = new (await import("../../src/models/thread-models.js")).AthenaTurn(
      // placeholder, replaced in Task 3
    )
  })
})
```

> 注意：`thread-models.ts` 在 Task 3 才创建。Task 2 的 contracts.test.ts 若引用 thread-models 会编译失败——**把 thread 相关断言留到 Task 3**，Task 2 的测试只覆盖 `newId`/`EventEnvelope`。上面测试文件以"不含 thread 引用"的版本为准（`AthenaThreadSchema` 的 import 移到 Task 3 的测试里）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd athena_ts && npx vitest run packages/athena-core/test`（先只跑存在的文件）
Expected: `newId` / `EventEnvelopeSchema` 未定义，失败。

- [ ] **Step 3: 实现 id.ts**

`src/id.ts`:
```ts
import { randomBytes } from "node:crypto"

/** 生成唯一 ID，格式 ``{prefix}_{12位随机hex}``；prefix 校验与 Python new_id 一致。 */
export function newId(prefix: string): string {
  if (!prefix || !/^[A-Za-z0-9_]+$/.test(prefix) || !/[A-Za-z0-9]/.test(prefix.replace(/_/g, ""))) {
    throw new TypeError(
      "prefix must contain at least one alphanumeric character and may otherwise contain only alphanumeric characters or underscores"
    )
  }
  return `${prefix}_${randomBytes(6).toString("hex")}`
}
```
> Python 判据：`not prefix.replace("_", "").isalnum()` → prefix 去掉下划线后必须全部是字母数字且非空。上式 `[A-Za-z0-9_]+` 全字符集 + 去掉下划线后 `/[A-Za-z0-9]/` 存在字母数字，等价。

- [ ] **Step 4: 实现 errors.ts**

`src/errors.ts`:
```ts
import type { z } from "zod"

/** 领域错误基类——映射 Python ValueError/FileNotFoundError/OSError 语义的顶层封装。 */
export class AthenaError extends Error {
  constructor(message: string) {
    super(message)
    this.name = new.target.name
  }
}

export class AthenaValidationError extends AthenaError {}
export class AthenaNotFoundError extends AthenaError {}
export class AthenaIntegrityError extends AthenaError {}
export class AthenaClosedError extends AthenaError {}

/** 把 zod issue 路径渲染为点号串，使消息含字段名（对齐 pytest 的 match="字段名"）。 */
export function formatZodError(error: z.ZodError): string {
  const issue = error.issues[0]
  const path = issue?.path?.join(".") ?? ""
  return `${path ? path + ": " : ""}${issue?.message ?? "validation failed"}`
}

/** 解析 schema；失败抛 AthenaValidationError，消息含字段路径。 */
export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown): T {
  const result = schema.safeParse(data)
  if (result.success) return result.data
  throw new AthenaValidationError(formatZodError(result.error))
}

/** model_dump(mode="json") 等价：深拷贝 + 去 undefined + JSON 安全化。 */
export function toJSON<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
```

- [ ] **Step 5: 实现 models/contracts.ts**

`src/models/contracts.ts`:
```ts
import { z } from "zod"

/** 至少含一个非空白字符的文本。pydantic: Annotated[str, StringConstraints(pattern=r"\S")] */
export const NonBlankText = z.string().regex(/\S/)
export type NonBlankText = z.infer<typeof NonBlankText>

export const ArtifactRef = NonBlankText
export type ArtifactRef = z.infer<typeof ArtifactRef>

export const CommitHash = NonBlankText
export type CommitHash = z.infer<typeof CommitHash>

/** 事件信封——事件类型、来源、载荷与状态版本号。 */
export const EventEnvelopeSchema = z.object({
  kind: z.string(),
  source: z.string(),
  payload: z.record(z.unknown()),
  state_version: z.number().int().min(0),
})
export type EventEnvelope = z.infer<typeof EventEnvelopeSchema>

/** 错误记录——严重级别、错误码、消息、重试次数。 */
export const ErrorRecordSchema = z.object({
  severity: z.enum(["retry", "degrade", "fatal"]),
  code: z.string(),
  message: z.string(),
  retry_count: z.number().int().min(0),
})
export type ErrorRecord = z.infer<typeof ErrorRecordSchema>

/** 论文工具与工作流共享的最小异步 artifact 契约（等价 ArtifactStore Protocol）。 */
export interface ArtifactStore {
  putBytes(data: Uint8Array): Promise<ArtifactRef>
  getBytes(ref: ArtifactRef): Promise<Uint8Array>
  putText(text: string): Promise<ArtifactRef>
  getText(ref: ArtifactRef): Promise<string>
}
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd athena_ts && npx vitest run packages/athena-core/test/id.test.ts packages/athena-core/test/errors.test.ts packages/athena-core/test/models/contracts.test.ts`
Expected: 全 PASS。

- [ ] **Step 7: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/id.ts packages/athena-core/src/errors.ts packages/athena-core/src/models/contracts.ts packages/athena-core/test/id.test.ts packages/athena-core/test/errors.test.ts packages/athena-core/test/models/contracts.test.ts && git commit -m "feat(athena-core): contracts, errors, newId (zod)"
```

---

### Task 3: thread-models + research-data-models + research-models + tests

**Files:**
- Create: `src/models/thread-models.ts`, `src/models/research-data-models.ts`, `src/models/research-models.ts`
- Modify: `test/models/contracts.test.ts`（补 thread 断言）
- Test: `test/models/research-models.test.ts`

**Interfaces:**
- Consumes: `NonBlankText`、`ArtifactRef`（Task 2）
- Produces:
  - `AthenaThreadSchema`/`AthenaTurnSchema`；`HypothesisSchema`/`Hypothesis`、`HypothesisBatchSchema`、`ExperimentPlanSchema`、`EvalResultSchema`、`ComparisonVerdictSchema`；`DataCardSchema`、`ColumnSummarySchema`、`DataProfileSchema`、`MetricSpecSchema`、`TaskMetaDataSchema`
  - `HypothesisStatus = "PROPOSED" | "SUPPORTED" | "REFUTED" | "REJECTED"`
  - `newHypothesis(input): Hypothesis`（构造后未赋 id/order，由 ResearchTree 指派——Task 4）

- [ ] **Step 1: 写失败测试**（移植 `tests/test_core_model_contracts.py` 全部 3 个测试）

`test/models/research-models.test.ts`:
```ts
import { describe, expect, it } from "vitest"
import { ComparisonVerdictSchema, EvalResultSchema, ExperimentPlanSchema, HypothesisSchema } from "../../src/models/research-models.js"
import { MetricSpecSchema, TaskMetaDataSchema } from "../../src/models/research-data-models.js"

describe("domain models retain validation and serialization", () => {
  it("hypothesis, plan, eval, verdict", () => {
    const hypothesis = HypothesisSchema.parse({
      statement: "Use stronger regularization",
      intervention: "Increase weight decay",
      expected_effect: "Improve validation accuracy",
    })
    const plan = ExperimentPlanSchema.parse({
      kind: "search",
      change: "Increase weight decay",
      run_config_ref: "artifact://run-config",
      budget: { trials: 1 },
      acceptance_rule: "Primary metric improves",
    })
    const evaluation = EvalResultSchema.parse({
      experiment_id: "experiment-1",
      primary: 0.8,
      per_sample: "artifact://samples",
    })
    const verdict = ComparisonVerdictSchema.parse({ winner: "candidate", p_value: 0.01 })
    expect(hypothesis.status).toBe("PROPOSED")
    expect(plan.kind).toBe("search")
    expect(evaluation.primary).toBe(0.8)
    expect(verdict.winner).toBe("candidate")
  })

  it("evaluation protocol models live in research-data-models", () => {
    const metric = MetricSpecSchema.parse({ name: "accuracy", direction: "maximize" })
    const meta = TaskMetaDataSchema.parse({
      task_type: "classification",
      data_type: "tabular",
      target_vars: ["y"],
      primary_metric: metric,
    })
    expect(meta.primary_metric.direction).toBe("maximize")
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run packages/athena-core/test/models/research-models.test.ts`
Expected: import 失败（模块不存在）。

- [ ] **Step 3: 实现 thread-models.ts**

`src/models/thread-models.ts`:
```ts
import { z } from "zod"
import { ArtifactRef } from "./contracts.js"

/** 对话线程——对应一个 Agent 会话。 */
export const AthenaThreadSchema = z.object({
  thread_id: z.string(),
  session_id: z.string(),
  status: z.string(),
  context_ref: ArtifactRef,
})
export type AthenaThread = z.infer<typeof AthenaThreadSchema>

/** 单轮对话——请求引用与可选执行结果引用。 */
export const AthenaTurnSchema = z.object({
  turn_id: z.string(),
  thread_id: z.string(),
  request_ref: ArtifactRef,
  status: z.string(),
  result_ref: ArtifactRef.nullable().default(null),
})
export type AthenaTurn = z.infer<typeof AthenaTurnSchema>
```

- [ ] **Step 4: 实现 research-data-models.ts**

`src/models/research-data-models.ts`（移植 `research/data_models.py`）:
```ts
import { z } from "zod"
import { ArtifactRef, NonBlankText } from "./contracts.js"

/** 数据集卡片——数据集指纹、schema 与划分。 */
export const DataCardSchema = z.object({
  dataset_ref: ArtifactRef,
  fingerprint: z.string(),
  schema_ref: ArtifactRef,
  split_manifest_ref: ArtifactRef.nullable().default(null),
})
export type DataCard = z.infer<typeof DataCardSchema>

/** 单列统计摘要。 */
export const ColumnSummarySchema = z.object({
  name: z.string(),
  dtype: z.string(),
  missing_rate: z.number().default(0.0),
  n_unique: z.number().nullable().default(null),
  sample_values: z.array(z.string()).default([]),
  processing: z.string().default(""),
})
export type ColumnSummary = z.infer<typeof ColumnSummarySchema>

/** 数据集画像。 */
export const DataProfileSchema = z.object({
  row_count: z.number(),
  col_count: z.number(),
  columns: z.array(ColumnSummarySchema).default([]),
  missing_rate: z.number().default(0.0),
  task_type_hint: z.string().default(""),
  target_col: z.string().nullable().default(null),
  issue_summary: z.string().default(""),
})
export type DataProfile = z.infer<typeof DataProfileSchema>

/** 评估指标规格——名称与优化方向。 */
export const MetricSpecSchema = z.object({
  name: NonBlankText,
  direction: z.enum(["maximize", "minimize"]),
})
export type MetricSpec = z.infer<typeof MetricSpecSchema>

/** 任务元数据。 */
export const TaskMetaDataSchema = z.object({
  task_type: z.string(),
  data_type: z.string(),
  target_vars: z.array(z.string()).default([]),
  primary_metric: MetricSpecSchema,
  constraints: z.array(z.string()).default([]),
})
export type TaskMetaData = z.infer<typeof TaskMetaDataSchema>
```

- [ ] **Step 5: 实现 research-models.ts**

`src/models/research-models.ts`（移植 `research_models.py`）:
```ts
import { z } from "zod"
import { ArtifactRef, NonBlankText } from "./contracts.js"

export const HypothesisStatus = ["PROPOSED", "SUPPORTED", "REFUTED", "REJECTED"] as const
export type HypothesisStatus = (typeof HypothesisStatus)[number]

/** 可通过实验验证或证伪的机器学习假设。 */
export const HypothesisSchema = z.object({
  statement: NonBlankText,
  intervention: NonBlankText,
  expected_effect: NonBlankText,
  status: z.enum(HypothesisStatus).default("PROPOSED"),
  evidence_refs: z.array(ArtifactRef).default([]),
  id: z.string().nullable().default(null),
  parent_id: z.string().nullable().default(null),
  supersedes: z.array(z.string()).default([]),
  priority: z.number().finite().default(1000.0),
  order: z.number().int().min(0).nullable().default(null),
  patience: z.number().int().min(0).default(0),
  turn_limit: z.number().int().min(0).nullable().default(null),
  sources: z.array(z.string()).default([]),
})
export type Hypothesis = z.infer<typeof HypothesisSchema>

/** 结构化 Ideator 输出：一组可验证假设。 */
export const HypothesisBatchSchema = z.object({
  hypotheses: z.array(HypothesisSchema).default([]),
})
export type HypothesisBatch = z.infer<typeof HypothesisBatchSchema>

/** 实验计划。 */
export const ExperimentPlanSchema = z.object({
  kind: z.string(),
  change: z.string(),
  rubrics: z.array(z.string()).default([]),
  run_config_ref: ArtifactRef,
  budget: z.record(z.unknown()),
  acceptance_rule: z.string(),
})
export type ExperimentPlan = z.infer<typeof ExperimentPlanSchema>

/** 在预测上运行 eval 的输出。 */
export const EvalResultSchema = z.object({
  experiment_id: z.string(),
  primary: z.number(),
  secondary: z.record(z.number()).default({}),
  per_sample: ArtifactRef,
})
export type EvalResult = z.infer<typeof EvalResultSchema>

/** 两个实验的两两比较。 */
export const ComparisonVerdictSchema = z.object({
  winner: z.enum(["baseline", "candidate", "tie"]),
  p_value: z.number(),
})
export type ComparisonVerdict = z.infer<typeof ComparisonVerdictSchema>
```

> 注意 `EvalResult.primary` 用 `z.number()`（允许 inf 传入，由 Experiment 校验层负责 finite 检查，与 Python `EvalResult` 无 `allow_inf_nan=False` 而 `Experiment._validate_terminal_payload` 检查 `isfinite(primary)` 一致）。

- [ ] **Step 6: 补 thread 断言到 contracts.test.ts**

修改 `test/models/contracts.test.ts`：把第 2 个测试补全为：
```ts
import { AthenaThreadSchema, AthenaTurnSchema } from "../../src/models/thread-models.js"
// ...
it("thread and turn", () => {
  const thread = AthenaThreadSchema.parse({
    thread_id: "thread-1",
    session_id: "session-1",
    status: "running",
    context_ref: "artifact://context",
  })
  const turn = AthenaTurnSchema.parse({
    turn_id: "turn-1",
    thread_id: thread.thread_id,
    request_ref: "artifact://request",
    status: "running",
  })
  expect(turn.thread_id).toBe(thread.thread_id)
})
```

- [ ] **Step 7: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/models`
Expected: contracts + research-models 全 PASS。

- [ ] **Step 8: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/models/thread-models.ts packages/athena-core/src/models/research-data-models.ts packages/athena-core/src/models/research-models.ts packages/athena-core/test && git commit -m "feat(athena-core): research/thread/data model schemas (zod)"
```

---

### Task 4: research-tree.ts（模型 + ResearchTree 类）+ 对拍测试

**Files:**
- Create: `src/models/research-tree.ts`
- Create: `test/fixtures/research_tree_v2.json`（拷贝）
- Test: `test/models/research-tree.test.ts`（移植 `tests/test_research_tree_v2.py`）、`test/models/research-tree-scheduling.test.ts`（移植 `test/unit/research/test_research_tree_scheduling.py`）

**Interfaces:**
- Consumes: `Hypothesis`/`ExperimentPlan`/`EvalResult`/`ComparisonVerdict`（Task 3）、`GitWorkBranch`（Task 5 会建 `services/workspace.ts`——**本任务先建一个最小 `GitWorkBranch` zod schema 供 tree 使用，Task 5 扩展**）、`atomicWriteJson`（Task 5 会建 `services/persistence.ts`——本任务先建该文件的最小实现或内联，Task 5 正式化）
- Produces:
  - `ExperimentStatus`（`PENDING|RUNNING|SUCCEEDED|FAILED|CANCELLED`）
  - `ExperimentSchema`（含 superRefine 跨字段校验）、`Experiment`
  - `SAVE_VERSION = 3`、`_LOAD_VERSIONS = {2, 3}`
  - `class ResearchTree`：`addHypothesis/getHypothesis/pendingHypotheses/updateHypothesisStatus/addExperiment/getExperiment/rootExperimentIds/listChildren/listDescendants/experimentPath/hypothesesPath/experimentForHypothesis/experiments(kind?)/activeHypotheses/transitionExperiment/completeExperiment/attachArtifact/setSota/bestExperimentId/toDict/fromDict/save/load`

**依赖前置**（避免循环依赖）：本任务在 `research-tree.ts` 内**临时定义** `GitWorkBranch` 的最小 zod schema（`path/branch/base_commit` 三字段，仅 `NonBlankText`），并临时定义 `atomicWriteJson`；Task 5 将其迁移到 `services/workspace.ts` / `services/persistence.ts` 并让 `research-tree.ts` 改为 import。**迁移时保持 `Experiment.gitwork` 行为不变**（`from_dict` 里 `gitwork.path === ""` 要触发 `"worktree"` 子串错误——见下）。

**翻译要点（照抄 `research_tree.py`，逐行对应）：**
1. `_parent_chain`：循环 → 抛 `AthenaValidationError("experiment parent cycle")`（测试 match `"cycle"`）。
2. `add_hypothesis`：重复 id → `"duplicate hypothesis id: {id}"`；未知父实验 → `KeyError`（vitest 用 `expect(...).toThrow`，消息 `"unknown parent experiment id: {id}"`）；order 指派逻辑照抄（`max(existing order)+1` / 重复 order 报 `"duplicate hypothesis order: {order}"`）；重校验 via `parseOrThrow(HypothesisSchema, {...hyp, id, order})`。
3. `add_experiment`：照抄（非空白 id、重复 id、未知假设 KeyError、假设已有实验 `"already has an experiment"`、父实验 KeyError、父不匹配 `"does not match hypothesis parent"`）。
4. `transition_experiment`/`complete_experiment`/`attach_artifact`/`set_sota`/`best_experiment_id`：照抄，状态经 `_ALLOWED_TRANSITIONS` 表。
5. `_validate_supersedes`/`active_hypotheses`：照抄（`"supersedes contains duplicate hypothesis ids"`、`"supersedes cannot include the child hypothesis"`、`"supersedes ids must be on the selected parent experiment path"`、`"child parent experiment does not match selected parent experiment"`）。
6. `to_dict`：返回 `{ version, sota_id, hypotheses: {...toJSON(h)}, experiments: {...toJSON(e)} }`。
7. `from_dict`：照抄全部校验（版本 `"unsupported research tree version: {version}"`；顶层字段 `"invalid research tree top-level fields: expected ..."`；映射类型检查；hypothesis key/order 校验 `"hypothesis mapping keys must be nonblank strings"`/`"hypothesis id does not match mapping key: {id}"`/`"duplicate hypothesis order: {order}"`/`"research tree v3 hypothesis order is required"`；experiment eval id `"evaluation experiment id does not match mapping key: {id}"`；children 构建/父链校验；`set_sota` 复用）。
8. `Experiment` superRefine（`_validate_terminal_payload` 等价）：
   - `gitwork.path`/`gitwork.branch` 空白 → `"experiment worktree path and branch must be nonblank"`（测试 match `"worktree"`）
   - SUCCEEDED → eval 必需（`"successful experiment requires evaluation"`）、`Number.isFinite(primary)`（`"successful experiment primary metric must be finite"`）、`per_sample` 非空白（`"successful experiment requires per-sample evidence"`）
   - 非 SUCCEEDED 但有 eval/verdict → `"only successful experiments may contain evaluation results"`
   - FAILED → error 非空白（`"failed experiment requires a nonblank error"`）；非 FAILED 有 error → `"only failed experiments may contain an error"`
   - **注意**：`invalid_artifact` 测试把 `artifacts.logs = ""` 期望 match `"artifacts"`——zod 里 `ArtifactRef = NonBlankText` 校验失败时字段路径是 `experiments.exp_child.artifacts.logs`，经 `parseOrThrow` 格式化为 `"experiments.exp_child.artifacts.logs: ..."`，含 `"artifacts"`。✓

- [ ] **Step 1: 拷贝 fixture**

```bash
mkdir -p athena_ts/packages/athena-core/test/fixtures
cp test/fixtures/research_tree_v2.json athena_ts/packages/athena-core/test/fixtures/
```

- [ ] **Step 2: 写失败测试（移植 test_research_tree_v2.py）**

`test/models/research-tree.test.ts`——完整移植，要点：
- 构造器 helpers `makeHypothesis`/`makeExperiment`/`successfulEval`/`complete` 照抄 pytest。
- `test_experiment_has_only_canonical_v2_fields` → 改为：`Object.keys(ExperimentSchema.shape)` 断言字段集 `["parent_id","hypothesis_id","commit","plan","gitwork","status","eval","verdict","artifacts","error"]` 且无 `"id"`/`"hypothesis"`。
- 错误断言：pytest `pytest.raises(ValueError, match="...")` → `expect(fn).toThrow(/.../)`；`KeyError, match="..."` → `expect(fn).toThrow(/.../)`（TS 无 KeyError，统一 `AthenaValidationError`/`AthenaError`）。
- `test_v2_fixture_loads_and_migrates_with_derived_children`：`tree.load(fixturePath)` → `expect(tree.bestExperimentId()).toBe("exp_baseline")`；`expect(tree.listChildren("exp_baseline")).toEqual(["exp_child"])`；`expect(JSON.stringify(tree.toDict())).not.toContain("children_ids")`；`tree.save(target)` 后读回 `version === 3`、orders 0/1、`load(target).toDict()` toEqual `tree.toDict()`。
- `test_failed_atomic_replace_keeps_last_valid_file`：vitest 里 `vi.spyOn(fs, "renameSync").mockImplementation(() => { throw new Error("disk full") })` → `expect(() => tree.save(target)).toThrow(/disk full/)`，断言 target 内容未变、目录只剩 target。→ 依赖 Task 5 的 `persistence.ts`；本任务先跳过此测试（`it.skip`），Task 5 打开。

- [ ] **Step 3: 写失败测试（移植 test_research_tree_scheduling.py）**

`test/models/research-tree-scheduling.test.ts`——完整移植 `_hypothesis`/`_experiment`/`_tree_with_path` helpers 与全部用例。注意：
- `_hypothesis(..., priority=math.inf)` → `expect(() => HypothesisSchema.parse({...priority: Infinity})).toThrow(/finite/)`（zod `.finite()` 报 "Expected finite number"）。
- `_experiment` 用 dict 形式构造（含嵌套 `plan`/`gitwork` dict）→ `parseOrThrow(ExperimentSchema, {...})`。
- 迁移/顺序指派用例照抄。

- [ ] **Step 4: 跑测试确认失败**

Run: `npx vitest run packages/athena-core/test/models/research-tree.test.ts packages/athena-core/test/models/research-tree-scheduling.test.ts`
Expected: 模块不存在/大量失败。

- [ ] **Step 5: 实现 research-tree.ts**

`src/models/research-tree.ts`（~600 LOC，逐行移植 `research_tree.py`；结构见上"翻译要点"，先内联最小 `GitWorkBranch` schema 与 `atomicWriteJson`）。关键骨架：
```ts
import { z } from "zod"
import { parseOrThrow, toJSON, AthenaValidationError } from "../errors.js"
import { HypothesisSchema, ExperimentPlanSchema, EvalResultSchema, ComparisonVerdictSchema, Hypothesis } from "./research-models.js"
import { CommitHash, ArtifactRef } from "./contracts.js"

// 临时最小 GitWorkBranch（Task 5 迁移到 services/workspace.ts）
const GitWorkBranchSchema = z.object({
  path: z.string(),
  branch: z.string(),
  base_commit: CommitHash,
})
type GitWorkBranch = z.infer<typeof GitWorkBranchSchema>

export const SAVE_VERSION = 3
const LOAD_VERSIONS = new Set([2, SAVE_VERSION])

export const ExperimentStatus = ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] as const
export type ExperimentStatus = (typeof ExperimentStatus)[number]

export const ExperimentSchema = z.object({
  parent_id: z.string().nullable().default(null),
  hypothesis_id: z.string(),
  commit: CommitHash,
  plan: ExperimentPlanSchema,
  gitwork: GitWorkBranchSchema,
  status: z.enum(ExperimentStatus).default("PENDING"),
  eval: EvalResultSchema.nullable().default(null),
  verdict: ComparisonVerdictSchema.nullable().default(null),
  artifacts: z.record(ArtifactRef).default({}),
  error: z.string().nullable().default(null),
}).superRefine((exp, ctx) => {
  if (!exp.gitwork.path.trim() || !exp.gitwork.branch.trim()) {
    ctx.addIssue({ code: "custom", message: "experiment worktree path and branch must be nonblank" })
  }
  if (exp.status === "SUCCEEDED") {
    if (exp.eval === null) ctx.addIssue({ code: "custom", message: "successful experiment requires evaluation" })
    else {
      if (!Number.isFinite(exp.eval.primary)) ctx.addIssue({ code: "custom", message: "successful experiment primary metric must be finite" })
      if (!exp.eval.per_sample.trim()) ctx.addIssue({ code: "custom", message: "successful experiment requires per-sample evidence" })
    }
  } else if (exp.eval !== null || exp.verdict !== null) {
    ctx.addIssue({ code: "custom", message: "only successful experiments may contain evaluation results" })
  }
  if (exp.status === "FAILED") {
    if (exp.error === null || !exp.error.trim()) ctx.addIssue({ code: "custom", message: "failed experiment requires a nonblank error" })
  } else if (exp.error !== null) {
    ctx.addIssue({ code: "custom", message: "only failed experiments may contain an error" })
  }
})
export type Experiment = z.infer<typeof ExperimentSchema>

const ALLOWED_TRANSITIONS: Record<ExperimentStatus, ReadonlySet<ExperimentStatus>> = {
  PENDING: new Set(["RUNNING", "CANCELLED"]),
  RUNNING: new Set(["SUCCEEDED", "FAILED", "CANCELLED"]),
  SUCCEEDED: new Set(),
  FAILED: new Set(),
  CANCELLED: new Set(),
}

export class ResearchTree {
  private _hypotheses = new Map<string, Hypothesis>()
  private _experiments = new Map<string, Experiment>()
  private _childrenIndex = new Map<string, string[]>()
  private _sotaId: string | null = null

  // …全部方法照抄 research_tree.py（见翻译要点），
  // 其中 Hypothesis/Experiment 的"重校验更新"一律 `parseOrThrow(Schema, {...old, ...update})`，
  // 抛错文案逐字照抄 Python。
}
```
> `ctx.addIssue({ code: "custom", ... })`：zod v4 支持 `z.customError`/`issue` 构造。若 v4 rc 语法不符，用 `.superRefine` 直接 `ctx.addIssue({ code: z.ZodIssueCode.custom, message })`（Task 1 冒烟后先跑一个最小 superRefine 样例验证语法）。

- [ ] **Step 6: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/models/research-tree.test.ts packages/athena-core/test/models/research-tree-scheduling.test.ts`
Expected: 除 `it.skip` 的原子写测试外全 PASS。任何消息不匹配的断言 → 修正实现文案与 Python 源码逐字对齐。

- [ ] **Step 7: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/models/research-tree.ts packages/athena-core/test/models packages/athena-core/test/fixtures && git commit -m "feat(athena-core): ResearchTree v2/v3 graph (zod + superRefine)"
```

---

### Task 5: workspace.ts + persistence.ts + retry.ts + 测试

**Files:**
- Create: `src/services/workspace.ts`, `src/services/persistence.ts`, `src/services/retry.ts`
- Modify: `src/models/research-tree.ts`（`GitWorkBranch` 改为 import `services/workspace.ts`；`save/load` 改用 `services/persistence.ts`；打开 Task 4 的 `it.skip` 原子写测试）
- Test: `test/services/retry.test.ts`（移植 test_retry.py 的 M0 子集）、`test/services/persistence.test.ts`

**Interfaces:**
- Consumes: `ArtifactRef`/`CommitHash`（Task 2）
- Produces:
  - `GitWorkBranchSchema`/`GitWorkBranch`、`GitDiffSchema`/`GitDiff`（`ref: ArtifactRef; paths: string[]`——Python 是 tuple，TS 用 `string[]`）、`class GitWorkspaceError extends AthenaError`、`resolveWorkspacePath(root, rel): string`、`type BinaryDiffWriter = (content: Buffer) => Promise<ArtifactRef> | ArtifactRef`、`interface GitWorkspace`（init/create/diff/commit/restorePaths/remove）
  - `atomicWriteJson(path, payload): string`
  - `isTransientError(excOrMsg: unknown): boolean`、`retryAsync<T>(fn, opts?): Promise<T>`

- [ ] **Step 1: 写失败测试**

`test/services/retry.test.ts`（移植 test_retry.py 前 4 个测试）:
```ts
import { describe, expect, it } from "vitest"
import { isTransientError, retryAsync } from "../../src/services/retry.js"

describe("isTransientError classifies", () => {
  it("whitelist transient vs business/auth", () => {
    expect(isTransientError(new Error("timed out"))).toBe(true)   // message token
    expect(isTransientError(new TimeoutError("timed out"))).toBe(true)
    expect(isTransientError("APITimeoutError: Request timed out")).toBe(true)
    expect(isTransientError("openai.RateLimitError: Error code: 429")).toBe(true)
    expect(isTransientError("InternalServerError: Error code: 503")).toBe(true)
    expect(isTransientError("database is locked")).toBe(true)
    expect(isTransientError("connection reset")).toBe(true)
    expect(isTransientError(new TypeError("contract violation"))).toBe(false)
    expect(isTransientError("AuthError: invalid api key")).toBe(false)
  })
})

describe("retryAsync", () => {
  it("retries transient then succeeds", async () => {
    let calls = 0
    const flaky = async () => { calls += 1; if (calls < 3) throw new TimeoutError("timed out"); return "ok" }
    const result = await retryAsync(flaky, { attempts: 4, baseDelay: 0 })
    expect(result).toBe("ok"); expect(calls).toBe(3)
  })
  it("raises on non-transient", async () => {
    let calls = 0
    const bad = async () => { calls += 1; throw new TypeError("contract violation") }
    await expect(retryAsync(bad, { attempts: 4, baseDelay: 0 })).rejects.toThrow(/contract violation/)
    expect(calls).toBe(1)
  })
  it("gives up after exhaustion", async () => {
    const always = async () => { throw new TimeoutError("timed out") }
    await expect(retryAsync(always, { attempts: 2, baseDelay: 0 })).rejects.toThrow()
  })
})
```

`test/services/persistence.test.ts`:
```ts
import { mkdtempSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { atomicWriteJson } from "../../src/services/persistence.js"

describe("atomicWriteJson", () => {
  let temp: string
  afterEach(() => { vi.restoreAllMocks() })
  it("writes indent-2 utf8 json with trailing newline", () => {
    temp = mkdtempSync(join(tmpdir(), "athena-persist-"))
    const target = join(temp, "nested", "state.json")
    atomicWriteJson(target, { a: 1, 中文: "值" })
    const text = readFileSync(target, "utf-8")
    expect(text).toContain('  "a": 1')
    expect(text).toContain('"中文": "值"')
    expect(text.endsWith("\n")).toBe(true)
  })
  it("failed replace keeps last valid file", () => {
    temp = mkdtempSync(join(tmpdir(), "athena-persist-"))
    const target = join(temp, "research_tree.json")
    writeFileSync(target, '{"last":"valid"}', "utf-8")
    const rename = vi.spyOn(require("node:fs"), "renameSync")
    rename.mockImplementation(() => { throw new Error("disk full") })
    expect(() => atomicWriteJson(target, { new: true })).toThrow(/disk full/)
    expect(readFileSync(target, "utf-8")).toBe('{"last":"valid"}')
    expect(readdirSync(temp)).toEqual(["research_tree.json"])
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run packages/athena-core/test/services`
Expected: 模块不存在。

- [ ] **Step 3: 实现 workspace.ts**

`src/services/workspace.ts`:
```ts
import { z } from "zod"
import path from "node:path"
import { AthenaError } from "../errors.js"
import { ArtifactRef, CommitHash } from "../models/contracts.js"

export type BinaryDiffWriter = (content: Buffer) => Promise<ArtifactRef> | ArtifactRef

/** 解析 workspace 相对路径并拒绝越界（等价 resolve_workspace_path）。 */
export function resolveWorkspacePath(root: string, rel: string): string {
  const resolvedRoot = path.resolve(root)
  const candidate = path.resolve(root, rel)
  if (candidate !== resolvedRoot && !candidate.startsWith(resolvedRoot + path.sep)) {
    throw new AthenaError(`path escapes workspace: ${rel}`)
  }
  return candidate
}

/** Git workspace 操作无法完成时抛出的领域错误。 */
export class GitWorkspaceError extends AthenaError {}

/** 一个 Git worktree 的路径、分支与基线提交。 */
export const GitWorkBranchSchema = z.object({
  path: z.string(),
  branch: z.string(),
  base_commit: CommitHash,
})
export type GitWorkBranch = z.infer<typeof GitWorkBranchSchema>

/** 内容寻址的二进制 diff 与从基线变更的路径。 */
export const GitDiffSchema = z.object({
  ref: ArtifactRef,
  paths: z.array(z.string()),
})
export type GitDiff = z.infer<typeof GitDiffSchema>

/** 隔离 Git worktree 的抽象操作（等价 GitWorkspace ABC）。 */
export interface GitWorkspace {
  init(repoPath?: string, initialFile?: string, initialContent?: string): Promise<CommitHash>
  create(baseCommit: CommitHash, branch: string, opts?: { name?: string }): Promise<GitWorkBranch>
  diff(workspace: GitWorkBranch): Promise<GitDiff>
  commit(workspace: GitWorkBranch, approvedDiff: GitDiff, message: string): Promise<CommitHash>
  restorePaths(workspace: GitWorkBranch, paths: string[]): Promise<void>
  remove(workspace: GitWorkBranch, opts?: { deleteBranch?: boolean; force?: boolean }): Promise<void>
}
```

> Python 的 `GitWorkspaceError` 继承 `RuntimeError`、`resolve_workspace_path` 抛 `ValueError`——TS 统一 `AthenaError` 基类（保持 `.name` 为具体子类以便断言）。

- [ ] **Step 4: 实现 persistence.ts**

`src/services/persistence.ts`:
```ts
import fs from "node:fs"
import path from "node:path"

/** 原子写 JSON（tmp + fsync + replace），等价 persistence.atomic_write_json。 */
export function atomicWriteJson(target: string, payload: unknown): string {
  fs.mkdirSync(path.dirname(target), { recursive: true })
  const temporary = path.join(path.dirname(target), `${path.basename(target)}.tmp`)
  try {
    fs.writeFileSync(temporary, JSON.stringify(payload, null, 2) + "\n", { encoding: "utf-8" })
    const fd = fs.openSync(temporary, "r")
    try { fs.fsyncSync(fd) } finally { fs.closeSync(fd) }
    fs.renameSync(temporary, target)
  } catch (err) {
    fs.rmSync(temporary, { force: true })
    throw err
  }
  return target
}
```

- [ ] **Step 5: 实现 retry.ts**

`src/services/retry.ts`:
```ts
// 瞬时错误消息特征（大小写不敏感），逐字照抄 _TRANSIENT_TOKENS。
const TRANSIENT_TOKENS = [
  "timeout", "timed out", "connection reset", "connection refused", "connection error",
  "connection dropped", "remote protocol", "broken pipe", "rate limit", "too many requests",
  "error code: 429", "error code: 5", "status 429", "status 5", "5xx",
  "internal server error", "service unavailable", "bad gateway", "api connection error",
  "api timeout", "apiconnectionerror", "apitimeouterror", "ratelimiterror",
  "database is locked", "database is busy", "operationalerror",
] as const

/** Node 网络层瞬时错误 code 白名单（等价 _TRANSIENT_EXC_TYPES 的类型判断）。 */
const TRANSIENT_CODES = new Set([
  "ECONNRESET", "ECONNREFUSED", "ECONNABORTED", "ETIMEDOUT", "EPIPE", "ENOTFOUND", "EAI_AGAIN",
])

/** 判定异常（或异常文本）是否属于可自动重试的瞬时基础设施错误。 */
export function isTransientError(excOrMsg: unknown): boolean {
  if (excOrMsg instanceof Error) {
    const err = excOrMsg as Error & { code?: unknown; status?: unknown; status_code?: unknown }
    if (err instanceof TimeoutError) return true
    if (typeof err.code === "string" && TRANSIENT_CODES.has(err.code)) return true
    const status = typeof err.status === "number" ? err.status : typeof err.status_code === "number" ? err.status_code : null
    if (status !== null && (status === 429 || status >= 500)) return true
    const text = `${err.name}: ${err.message}`.toLowerCase()
    return TRANSIENT_TOKENS.some((t) => text.includes(t))
  }
  const text = String(excOrMsg).toLowerCase()
  return TRANSIENT_TOKENS.some((t) => text.includes(t))
}

export interface RetryOptions {
  attempts?: number
  baseDelay?: number
  maxDelay?: number
  classify?: (error: Error) => boolean
}

/** 指数退避 + jitter 重试；仅对瞬时错误重试，最后一次失败原样抛出。 */
export async function retryAsync<T>(
  fn: () => Promise<T>,
  { attempts = 4, baseDelay = 1.0, maxDelay = 8.0, classify = isTransientError }: RetryOptions = {},
): Promise<T> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await fn()
    } catch (err) {
      const error = err as Error
      if (attempt === attempts - 1 || !classify(error)) throw error
      const delay = Math.min(maxDelay, baseDelay * 2 ** attempt) * (0.5 + Math.random())
      await new Promise((resolve) => setTimeout(resolve, delay))
    }
  }
  throw new Error("unreachable")
}
```

> Python `random.random()` ∈ [0,1) → `0.5 + Math.random()` ∈ [0.5, 1.5) 一致。

- [ ] **Step 6: 迁移 research-tree 的 GitWorkBranch / atomicWriteJson**

修改 `src/models/research-tree.ts`：
- 删除内联 `GitWorkBranchSchema`，改为 `import { GitWorkBranchSchema } from "../services/workspace.js"`。
- `save` 改为 `import { atomicWriteJson } from "../services/persistence.js"; return atomicWriteJson(path, this.toDict())`。
- 打开 Task 4 的 `it.skip` 原子写测试；若 `vi.spyOn(require("node:fs"), "renameSync")` 方式对 ESM 无效，改用 `vi.mock` 或把 `persistence.ts` 的 rename 抽成可注入函数（`vi.spyOn(persistenceModule, "atomicWriteJson")` 中拦截）——以通过为准。

- [ ] **Step 7: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/services packages/athena-core/test/models`
Expected: retry + persistence + 原子写测试 + research-tree 全 PASS。

- [ ] **Step 8: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/services packages/athena-core/src/models/research-tree.ts packages/athena-core/test && git commit -m "feat(athena-core): workspace contracts, atomic persistence, transient retry"
```

---

### Task 6: artifact-store.ts + 测试

**Files:**
- Create: `src/services/artifact-store.ts`
- Test: `test/services/artifact-store.test.ts`（移植 `test/unit/test_artifact_store.py`）

**Interfaces:**
- Consumes: `ArtifactRef`（Task 2）、`AthenaNotFoundError`/`AthenaIntegrityError`（Task 2）
- Produces:
  - `digestRef(data: Uint8Array): ArtifactRef`（`"sha256:" + sha256hex`）
  - `digestFromRef(ref: ArtifactRef): string`（不匹配 `^sha256:([0-9a-f]{64})$` → 抛 `InvalidArtifactRefError`）
  - `InvalidArtifactRefError`、`ArtifactNotFoundError`、`ArtifactIntegrityError`（分别继承 `AthenaValidationError`/`AthenaNotFoundError`/`AthenaIntegrityError`）
  - `class LocalArtifactStore`：`constructor(root)`、`pathFor(ref)`、`putBytes/getBytes/putText/getText`

**翻译要点：**
- `putBytes`：内容寻址 + 幂等。写临时文件于目标同目录（`path + ".tmp-" + random`），fsync 后 rename；rename `PermissionError`（Windows 并发）→ 校验既有内容摘要一致则接受。`pathFor` = `root/digest.slice(0,2)/digest.slice(2)`。
- 并发幂等测试用 `Promise.all`（20 个 `putBytes(b"same")`）→ 断言 1 个唯一 ref、`pathFor` 文件存在。
- `getBytes` 缺失 → `ArtifactNotFoundError`；内容被篡改（`pathFor(ref)` 写 corrupt）→ `ArtifactIntegrityError`。
- 非法 ref（`"sha256:../../outside"`）在 `pathFor` 前被 `digestFromRef` 拦截 → `InvalidArtifactRefError`。

- [ ] **Step 1: 写失败测试**

`test/services/artifact-store.test.ts`（完整移植）:
```ts
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import {
  ArtifactIntegrityError, ArtifactNotFoundError, InvalidArtifactRefError, LocalArtifactStore,
} from "../../src/services/artifact-store.js"

async function withStore<T>(fn: (store: LocalArtifactStore, root: string) => Promise<T>): Promise<T> {
  const root = mkdtempSync(join(tmpdir(), "athena-art-"))
  try { return await fn(new LocalArtifactStore(root), root) }
  finally { rmSync(root, { recursive: true, force: true }) }
}

describe("LocalArtifactStore", () => {
  it("bytes and text round trip", async () => {
    await withStore(async (store) => {
      const bytesRef = await store.putBytes(Buffer.from([0x00, 0x70, 0x61, 0x70, 0x65, 0x72]))
      const textRef = await store.putText("论文 text")
      expect(Buffer.from(await store.getBytes(bytesRef)).toString("binary")).toBe("\x00paper")
      expect(await store.getText(textRef)).toBe("论文 text")
    })
  })
  it("content addressing is idempotent under concurrency", async () => {
    await withStore(async (store) => {
      const refs = await Promise.all(Array.from({ length: 20 }, () => store.putBytes(Buffer.from("same"))))
      expect(new Set(refs).size).toBe(1)
      expect(existsSync(store.pathFor(refs[0]!))).toBe(true)
    })
  })
  it("rejects invalid reference before path resolution", async () => {
    await withStore(async (store) => {
      await expect(store.getBytes("sha256:../../outside")).rejects.toThrow(InvalidArtifactRefError)
    })
  })
  it("missing and corrupt artifacts are distinct", async () => {
    await withStore(async (store) => {
      const missing = "sha256:" + "0".repeat(64)
      await expect(store.getBytes(missing)).rejects.toThrow(ArtifactNotFoundError)
      const ref = await store.putBytes(Buffer.from("valid"))
      writeFileSync(store.pathFor(ref), "corrupt")
      await expect(store.getBytes(ref)).rejects.toThrow(ArtifactIntegrityError)
    })
  })
})
```
> `Buffer.from([0x00,...]).toString("binary")` 复刻 `b"\x00paper"` 字节级比较；或直接 `expect(await store.getBytes(bytesRef)).toEqual(Buffer.from([0x00,0x70,0x61,0x70,0x65,0x72]))`（vitest 对 Buffer toEqual 逐字节）。用后者更稳。

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run packages/athena-core/test/services/artifact-store.test.ts`
Expected: 模块不存在。

- [ ] **Step 3: 实现 artifact-store.ts**

`src/services/artifact-store.ts`:
```ts
import { createHash, randomBytes } from "node:crypto"
import { mkdirSync, writeFileSync, renameSync, readFileSync, existsSync, openSync, closeSync, fsyncSync, unlinkSync } from "node:fs"
import { join, resolve } from "node:path"
import { AthenaIntegrityError, AthenaNotFoundError, AthenaValidationError } from "../errors.js"
import type { ArtifactRef } from "../models/contracts.js"

const REF_PREFIX = "sha256:"
const REF_PATTERN = /^sha256:([0-9a-f]{64})$/

export class InvalidArtifactRefError extends AthenaValidationError {}
export class ArtifactNotFoundError extends AthenaNotFoundError {}
export class ArtifactIntegrityError extends AthenaIntegrityError {}

export function digestRef(data: Uint8Array): ArtifactRef {
  return REF_PREFIX + createHash("sha256").update(data).digest("hex")
}

export function digestFromRef(ref: ArtifactRef): string {
  const match = REF_PATTERN.exec(ref)
  if (!match) throw new InvalidArtifactRefError(`Invalid artifact reference: ${ref}`)
  return match[1]!
}

/** 本地分片式内容寻址存储（文件放 root/ab/cdef…，同内容幂等）。 */
export class LocalArtifactStore {
  private readonly root: string
  private constructorFree = true // 占位，见下
  constructor(root: string) {
    this.root = resolve(root)
    mkdirSync(this.root, { recursive: true })
  }
  pathFor(ref: ArtifactRef): string {
    const digest = digestFromRef(ref)
    return join(this.root, digest.slice(0, 2), digest.slice(2))
  }
  async putBytes(data: Uint8Array): Promise<ArtifactRef> {
    const ref = digestRef(data)
    const target = this.pathFor(ref)
    if (existsSync(target)) {
      if (digestRef(readFileSync(target)) !== ref) throw new ArtifactIntegrityError(`Artifact digest mismatch: ${ref}`)
      return ref
    }
    mkdirSync(join(this.root, ref.slice(7, 9)), { recursive: true })
    const temporary = join(this.root, ref.slice(7, 9), `.artifact-${randomBytes(8).toString("hex")}`)
    try {
      const fd = openSync(temporary, "wx")
      try { writeFileSync(fd, data); fsyncSync(fd) } finally { closeSync(fd) }
      try { renameSync(temporary, target) }
      catch (err) {
        // Windows 并发写已创建目标 → 接受摘要一致的既有内容
        if (!existsSync(target) || digestRef(readFileSync(target)) !== ref) throw err
      }
    } finally {
      try { unlinkSync(temporary) } catch { /* ignore */ }
    }
    return ref
  }
  async getBytes(ref: ArtifactRef): Promise<Uint8Array> {
    const target = this.pathFor(ref)
    let data: Buffer
    try { data = readFileSync(target) }
    catch {
      throw new ArtifactNotFoundError(`Artifact not found: ${ref}`)
    }
    if (digestRef(data) !== ref) throw new ArtifactIntegrityError(`Artifact digest mismatch: ${ref}`)
    return data
  }
  async putText(text: string): Promise<ArtifactRef> {
    return this.putBytes(Buffer.from(text, "utf-8"))
  }
  async getText(ref: ArtifactRef): Promise<string> {
    return Buffer.from(await this.getBytes(ref)).toString("utf-8")
  }
}
```
> 删除无用的 `constructorFree` 占位字段。`ref.slice(7, 9)` = digest 前 2 hex = 分片目录。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/services/artifact-store.test.ts`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/services/artifact-store.ts packages/athena-core/test/services/artifact-store.test.ts && git commit -m "feat(athena-core): content-addressed LocalArtifactStore"
```

---

### Task 7: git-workspace.ts（LocalGitWorkspace）+ 测试

**Files:**
- Create: `src/services/git-workspace.ts`
- Test: `test/services/git-workspace.test.ts`（移植 `test/unit/test_git_workspace.py` 全部用例）

**Interfaces:**
- Consumes: `GitWorkspace`/`GitWorkBranch`/`GitDiff`/`GitWorkspaceError`/`resolveWorkspacePath`（Task 5）、`ArtifactRef`/`CommitHash`（Task 2）
- Produces: `class LocalGitWorkspace implements GitWorkspace`
  - `constructor(repoPath, worktreeRoot, diffWriter: BinaryDiffWriter)`
  - 方法：`init/create/diff/commit/restorePaths/remove` + 内部 `_findWorktree/_getState/_resolveCommit/_validateBranch/_reviewRef/_resolveReviewMarker/_reconcileInstalledReview/_recordCommit/_git`

**翻译要点（逐行对应 `git_workspace.py`）：**
1. 状态类：`_Review`（artifact/sha256/tree/head/empty/pendingCommit）、`_Committed`（artifact/commit）、`_WorkspaceState`（workspace/review/committed）→ TS `interface`。
2. `_states: Map<string, _WorkspaceState>`，key = `path.resolve(ws.path)`（Python `dict[Path, ...]`，Path 哈希按解析路径）。
3. 互斥锁：`async with self._lock` → 实现一个最小 `Mutex`（promise 链）：
   ```ts
   class Mutex {
     private tail: Promise<unknown> = Promise.resolve()
     async run<T>(fn: () => Promise<T>): Promise<T> {
       const prev = this.tail
       let release!: () => void
       this.tail = new Promise((r) => { release = r })
       await prev
       try { return await fn() } finally { release() }
     }
   }
   ```
   每个加锁方法体包进 `this._lock.run(async () => { ... })`。
4. `_git`：
   ```ts
   import { execFile } from "node:child_process"
   import { promisify } from "node:util"
   const execFileP = promisify(execFile)
   async _git(args: string[], opts: { cwd?: string; check?: boolean } = {}): Promise<Buffer> {
     const cwd = opts.cwd ?? this.repo
     const { stdout } = await execFileP("git", ["-C", cwd, ...args], { encoding: "buffer", maxBuffer: 100 * 1024 * 1024 })
     return Buffer.from(stdout)
   }
   ```
   `check=false` 时抑制抛错：用 `try/catch`，`opts.check === false` 则捕获后返回空 Buffer（Python 返回 stdout 且不检查 returncode）。**必须保持 `check` 语义**：`rev-parse --verify ... check=false` 期望失败时返回空串。
   > Python 的 `_git` 在 `check && returncode != 0` 时抛 `GitWorkspaceError`。TS 里 `execFileP` 失败即 reject → `check=false` 时 catch 并返回 `Buffer.alloc(0)`。
5. `_validateBranch`：照抄拒绝规则（`""`、前缀 `-`/`/`/`.`、后缀 `/`/`.`/`.lock`、`..`、`@{`、空白或 `~^:?*[\` 字符）。`\` 在正则需转义。
6. `inspect.isawaitable` → `result instanceof Promise ? await result : result`（diff writer 可能同步返回 ArtifactRef）。
7. `is_relative_to`：`const r = path.resolve(root); const c = path.resolve(p); return c === r || c.startsWith(r + sep)`。
8. `create` 幂等/恢复/重建逻辑、`diff` 的"上一轮暂存树再 diff 还原删除路径"、`commit` 的 review 校验 + `commit-tree` + `refs/athena/reviews/{branch}` marker、`restorePaths` 的越界校验 + `ls-tree`/`restore`/`rm --cached`、`remove` 的 dirty 检查 + `worktree remove`——全部照抄。
9. **测试桩（mock）**：Python 测试替换 `self.manager._git`（`uncertain_git`/`flaky_git`）→ vitest 用 `vi.spyOn(manager as any, "_git").mockImplementation(...)`，恢复用 `mockRestore()`。测试通过构造 `LocalGitWorkspace(repo, worktreeRoot, writer)`（不含 cordis）直接驱动真实 git 仓库。

**测试移植注意：**
- 每个用例：`mkdtemp` 建 repo + worktree_root；`git init/config/seed/commit` 与 Python setUp 一致；`afterEach` 逆序 `remove(workspace, {deleteBranch:true, force:true})` + `rmSync(temp, recursive)`。
- `base_commit` 用 helper `git(repo, ...args)`（同步 `execFileSync`）。
- 12 个 `asyncTest*` + 2 个顶层 `test_*`（`test_diff_reports_paths_including_deletions`、`test_init_does_not_walk_up_to_parent_repo`）全部移植。
- 断言：`assertIn(b"...", artifacts[ref])` → `expect(artifacts[ref]!.includes(Buffer.from("..."))).toBe(true)`；`assertEqual(git show ...stdout, ...)` → `expect(gitShow(...)).toBe("...")`。

- [ ] **Step 1: 写失败测试（完整移植 test_git_workspace.py）**

`test/services/git-workspace.test.ts`——按"翻译要点"与"测试移植注意"完整移植全部用例。helper 首行：
```ts
import { execFileSync } from "node:child_process"
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, readFileSync, existsSync, cpSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve, sep } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { LocalGitWorkspace } from "../../src/services/git-workspace.js"
import { GitDiffSchema, GitWorkBranchSchema, GitWorkspaceError } from "../../src/services/workspace.js"

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf-8" }).trim()
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run packages/athena-core/test/services/git-workspace.test.ts`
Expected: 模块不存在。

- [ ] **Step 3: 实现 git-workspace.ts**

`src/services/git-workspace.ts`（~550 LOC，逐行移植；见翻译要点 1-9）。方法签名与 Python 一一对应。每个 `git` 错误路径的消息照抄（`"git {...} 失败: {stderr[:200]}"` → `git ${args.join(" ")} 失败: ${stderr.slice(0,200)}`）。`GitWorkspaceError("branch 已存在但无 worktree：…")` 等中文文案照抄。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/services/git-workspace.test.ts`
Expected: 全 PASS。若个别 git 行为（Windows 差异）不符，调整实现以对齐 pytest 语义而非放宽断言。

- [ ] **Step 5: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/services/git-workspace.ts packages/athena-core/test/services/git-workspace.test.ts && git commit -m "feat(athena-core): LocalGitWorkspace over system git"
```

---

### Task 8: app.ts（cordis 组合根）+ 服务注册 spike + 测试

**Files:**
- Create: `src/app.ts`、`src/services/athena-services.ts`（薄 cordis Service 包装）
- Test: `test/app.test.ts`

**Interfaces:**
- Consumes: `LocalArtifactStore`（Task 6）、`LocalGitWorkspace`（Task 7）、`Context`/`Service`（cordis）
- Produces:
  - `interface AthenaAppConfig { workspaceRoot: string; repoPath: string; worktreeRoot: string; diffWriter?: BinaryDiffWriter }`
  - `createAthenaApp(config: AthenaAppConfig): Context`——返回根 `Context`，其上可访问 `ctx.workspace`、`ctx.artifacts`、`ctx.gitWorkspace`
  - `AthenaWorkspaceService`/`ArtifactStoreService`/`GitWorkspaceService extends Service`

- [ ] **Step 1: cordis v4 注册 API spike**

先写一个最小 spike 脚本验证 cordis rc.8 的服务注册/访问语法，确认后再写正式实现。要点：
- `import { Context, Service } from "cordis"` 是否可用。
- `Service` 子类 `constructor(ctx, opts)` 里 `super(ctx, "name", true)`（第三个参数 immediate）语义。
- `ctx.plugin(ServiceClass, config)` 后 `ctx.name` 是否可访问；是否需要 `await ctx.start()`。
- 若 `Service` 子类 + `static inject` 不生效，尝试 `ctx.service(name)` + `ctx.provide()`/`ctx.set()`。
记录 spike 结论到本任务注释，再实现。

`src/services/athena-services.ts`（薄包装，业务逻辑全在普通类）:
```ts
import { Context, Service } from "cordis"
import { LocalArtifactStore } from "./artifact-store.js"
import { LocalGitWorkspace } from "./git-workspace.js"
import type { BinaryDiffWriter, GitWorkspace } from "./workspace.js"

export interface AthenaServicesConfig {
  workspaceRoot: string
  repoPath: string
  worktreeRoot: string
  diffWriter?: BinaryDiffWriter
}

export class ArtifactStoreService extends Service {
  constructor(ctx: Context, config: AthenaServicesConfig) {
    super(ctx, "artifacts", true)
    this.artifacts = new LocalArtifactStore(config.workspaceRoot)
  }
  declare artifacts: LocalArtifactStore
  putBytes(data: Uint8Array) { return this.artifacts.putBytes(data) }
  getBytes(ref: string) { return this.artifacts.getBytes(ref) }
  putText(text: string) { return this.artifacts.putText(text) }
  getText(ref: string) { return this.artifacts.getText(ref) }
}

export class GitWorkspaceService extends Service implements GitWorkspace {
  private git: LocalGitWorkspace
  constructor(ctx: Context, config: AthenaServicesConfig) {
    super(ctx, "gitWorkspace", true)
    this.git = new LocalGitWorkspace(config.repoPath, config.worktreeRoot, config.diffWriter ?? ((b) => `artifact://git-diff/${Buffer.from(b).toString("hex")}`))
  }
  init(repoPath?: string, initialFile?: string, initialContent?: string) { return this.git.init(repoPath, initialFile, initialContent) }
  create(baseCommit: string, branch: string, opts?: { name?: string }) { return this.git.create(baseCommit, branch, opts) }
  diff(workspace: any) { return this.git.diff(workspace) }
  commit(workspace: any, approvedDiff: any, message: string) { return this.git.commit(workspace, approvedDiff, message) }
  restorePaths(workspace: any, paths: string[]) { return this.git.restorePaths(workspace, paths) }
  remove(workspace: any, opts?: { deleteBranch?: boolean; force?: boolean }) { return this.git.remove(workspace, opts) }
}

// workspace 服务：M0 先暴露 workspaceRoot 解析 + 目录规划约定（workspace.py 的核心），
// 详见 Step 2 实现细节。
```
> `Service` 服务名 `"artifacts"`/`"gitWorkspace"` 对应 spec 的 `ctx.artifacts`/`ctx.gitWorkspace`。spike 若证明该形态不行，用 cordis 原生注册 API 改写（`ctx.service(...)` + provider），保持对外访问名不变。

- [ ] **Step 2: 实现 app.ts**

`src/app.ts`:
```ts
import { Context } from "cordis"
import { ArtifactStoreService, GitWorkspaceService, AthenaWorkspaceService, AthenaServicesConfig } from "./services/athena-services.js"

/** 组合根：创建根 Context 并注册基础服务（workspace / artifacts / gitWorkspace）。 */
export function createAthenaApp(config: AthenaServicesConfig): Context {
  const ctx = new Context()
  ctx.plugin(AthenaWorkspaceService, config)
  ctx.plugin(ArtifactStoreService, config)
  ctx.plugin(GitWorkspaceService, config)
  return ctx
}
```
> `AthenaWorkspaceService` 实现 `workspace.py` 的目录规划约定（`data/`、`code/`、`report/` 等分区）与 `resolveWorkspacePath`；M0 先落 `resolveWorkspacePath` 复用 + 目录常量，分区深化留 M2。

- [ ] **Step 3: 写测试**

`test/app.test.ts`:
```ts
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { createAthenaApp } from "../src/app.js"

describe("createAthenaApp", () => {
  it("exposes workspace, artifacts, gitWorkspace on the root context", async () => {
    const root = mkdtempSync(join(tmpdir(), "athena-app-"))
    try {
      const ctx = createAthenaApp({
        workspaceRoot: join(root, "ws"),
        repoPath: join(root, "repo"),
        worktreeRoot: join(root, "worktrees"),
      })
      // cordis immediate 服务应可直接访问；若需 start 则加 await ctx.start()
      expect(ctx.artifacts).toBeTruthy()
      expect(ctx.gitWorkspace).toBeTruthy()
      expect(ctx.workspace).toBeTruthy()
      const ref = await ctx.artifacts.putText("hello")
      expect(await ctx.artifacts.getText(ref)).toBe("hello")
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run packages/athena-core/test/app.test.ts`
Expected: PASS（若 cordis 需要 `await ctx.start()`，在测试里补上；若 immediate 服务不可访问，调整注册方式后重跑）。

- [ ] **Step 5: Commit**

```bash
cd athena_ts && git add packages/athena-core/src/app.ts packages/athena-core/src/services/athena-services.ts packages/athena-core/test/app.test.ts && git commit -m "feat(athena-core): createAthenaApp cordis composition root"
```

---

### Task 9: index.ts 公开面 + 全量验证 + 收尾

**Files:**
- Create: `src/index.ts`
- Modify: —（若需）

- [ ] **Step 1: 写 index.ts**

`src/index.ts`（M0 公开面——只导出本包内已实现符号；`core/agent`、`tool` 等 M1 符号不在此导出）:
```ts
export { newId } from "./id.js"
export { AthenaError, AthenaValidationError, AthenaNotFoundError, AthenaIntegrityError, AthenaClosedError, formatZodError, parseOrThrow, toJSON } from "./errors.js"
export * from "./models/contracts.js"
export * from "./models/thread-models.js"
export * from "./models/research-models.js"
export * from "./models/research-data-models.js"
export { ExperimentStatus, ExperimentSchema, ResearchTree, SAVE_VERSION } from "./models/research-tree.js"
export * from "./services/workspace.js"
export { atomicWriteJson } from "./services/persistence.js"
export { isTransientError, retryAsync } from "./services/retry.js"
export { digestRef, digestFromRef, LocalArtifactStore, InvalidArtifactRefError, ArtifactNotFoundError, ArtifactIntegrityError } from "./services/artifact-store.js"
export { LocalGitWorkspace } from "./services/git-workspace.js"
export { createAthenaApp } from "./app.js"
```

- [ ] **Step 2: 全量验证**

Run:
```bash
cd athena_ts && npx tsc --noEmit -p packages/athena-core && npm test
```
Expected: `tsc` 无错误；vitest 全绿（含 models/services/app 全部用例）。
对拍：M0 覆盖的 pytest（test_core_model_contracts、test_research_tree_v2、test_research_tree_scheduling、test_artifact_store、test_retry M0 子集、test_git_workspace）在 TS 端同输入同输出/同错误消息。

- [ ] **Step 3: 更新 spec 备注（范围增补）**

在 `docs/superpowers/specs/2026-08-13-athena-ts-core-design.md` 追加一节"M0 实施增补"，记录：
- `models/research-data-models.ts`（research/data_models.py 79 LOC）并入 M0 模型层（支撑 test_core_model_contracts 对拍）。
- `test_core_public_api.py` 实际只测 M1 符号 → 移到 M1。
- `test/unit/test_artifact_store.py`、`test/unit/test_retry.py`（M0 子集）补充进 M0 对拍清单。

- [ ] **Step 4: Commit**

```bash
cd athena_ts && git add docs/superpowers/specs/2026-08-13-athena-ts-core-design.md packages/athena-core/src/index.ts && git commit -m "feat(athena-core): M0 public surface + spec notes"
```

---

## Self-Review

（已核对，见 spec §5/§7/§9 映射；执行时若发现偏差按 Task 内"Expected"修正并记录到 Task 9 Step 3。）
