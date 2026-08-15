/**
 * AgentRuntime 公开类型与契约（移植 ``core/agent/types.py``）。
 */

import type { ArtifactRef } from "@athena/core"

export type AgentPath = string[]
export type AgentId = string
export type RunId = string

export const AgentStatus = {
  STARTING: "starting",
  IDLE: "idle",
  RUNNING: "running",
  WAITING: "waiting",
  WAITING_FOR_HUMAN: "waiting_for_human",
  ERROR: "error",
  CLOSED: "closed",
} as const
export type AgentStatus = (typeof AgentStatus)[keyof typeof AgentStatus]

export const RunStatus = {
  QUEUED: "queued",
  RUNNING: "running",
  COMPLETED: "completed",
  FAILED: "failed",
  INTERRUPTED: "interrupted",
} as const
export type RunStatus = (typeof RunStatus)[keyof typeof RunStatus]

export const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set([
  RunStatus.COMPLETED,
  RunStatus.FAILED,
  RunStatus.INTERRUPTED,
])

export const ReturnWhen = {
  FIRST_COMPLETED: "first_completed",
  ALL_COMPLETED: "all_completed",
} as const
export type ReturnWhen = (typeof ReturnWhen)[keyof typeof ReturnWhen]

export const ErrorCode = {
  NOT_FOUND: "NOT_FOUND",
  PERMISSION_DENIED: "PERMISSION_DENIED",
  BUSY: "BUSY",
  CLOSED: "CLOSED",
  LIMIT_REACHED: "LIMIT_REACHED",
  BUDGET_EXHAUSTED: "BUDGET_EXHAUSTED",
  INVALID_REQUEST: "INVALID_REQUEST",
  CODEC_ERROR: "CODEC_ERROR",
  STORE_UNAVAILABLE: "STORE_UNAVAILABLE",
  INTERNAL: "INTERNAL",
} as const
export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode]

/** Agent 领域异常基类（等价 AgentError(RuntimeError)）。 */
export class AgentError extends Error {
  constructor(message = "") {
    super(message)
    this.name = "AgentError"
  }
}

/** 命令错误，带稳定 code；失败命令必须零状态变更。 */
export class AgentCommandError extends AgentError {
  readonly code: ErrorCode
  readonly retryable: boolean
  readonly details: Record<string, unknown> | null

  constructor(
    code: ErrorCode,
    message: string,
    retryable: boolean = false,
    details: Record<string, unknown> | null = null
  ) {
    super(message)
    this.name = "AgentCommandError"
    this.code = code
    this.retryable = retryable
    this.details = details
  }
}

/** 目标已有非终态 Run，followup 被拒绝。 */
export class AgentBusyError extends AgentCommandError {
  constructor(
    message: string = "agent busy",
    details: Record<string, unknown> | null = null
  ) {
    super(ErrorCode.BUSY, message, false, details)
    this.name = "AgentBusyError"
  }
}

/** Run 执行失败（FAILED 终态）后 wait() 抛出的领域异常。 */
export class AgentRunFailed extends AgentError {
  constructor(message = "") {
    super(message)
    this.name = "AgentRunFailed"
  }
}

/** Run 被中断后 wait() 抛出的领域异常。 */
export class AgentRunInterrupted extends AgentError {
  constructor(message = "") {
    super(message)
    this.name = "AgentRunInterrupted"
  }
}

/** 只追加当前 Run 事件的异步回调（等价 EventSink Protocol）。 */
export type EventSink = (
  kind: string,
  eventRef: ArtifactRef,
  data?: Record<string, unknown> | null
) => Promise<void> | void

/** 执行一个已类型化请求的协议（等价 AgentRunner Protocol）。 */
export interface AgentRunner<RequestT = unknown, ResponseT = unknown> {
  run(
    request: RequestT,
    session: unknown,
    emit: EventSink
  ): Promise<ResponseT>
}

/** 请求、响应与 artifact 之间的类型化编解码（等价 AgentCodec Protocol）。 */
export interface AgentCodec<RequestT = unknown, ResponseT = unknown> {
  encodeRequest(value: RequestT): ArtifactRef
  decodeRequest(ref: ArtifactRef): RequestT
  encodeResponse(value: ResponseT): ArtifactRef
  decodeResponse(ref: ArtifactRef): ResponseT
}

/** 请求/响应以 JSON 字符串作为 ArtifactRef（等价 JsonCodec）。 */
export class JsonCodec {
  encodeRequest(value: unknown): string {
    return JSON.stringify(value)
  }
  decodeRequest(ref: string): unknown {
    return JSON.parse(ref)
  }
  encodeResponse(value: unknown): string {
    return JSON.stringify(value)
  }
  decodeResponse(ref: string): unknown {
    return JSON.parse(ref)
  }
}

/** Agent 的不可变能力说明（等价 AgentSpec）。 */
export class AgentSpec<RequestT = unknown, ResponseT = unknown> {
  constructor(
    public readonly runner: AgentRunner<RequestT, ResponseT>,
    public readonly codec: AgentCodec<RequestT, ResponseT>
  ) {}
}

/** 投递至目标 mailbox 的消息（等价 AgentMessage）。 */
export class AgentMessage {
  constructor(
    public readonly source: AgentId | null,
    public readonly content: string,
    public readonly contextRefs: ArtifactRef[] = []
  ) {}

  get isControl(): boolean {
    return this.source === null
  }
}

/** Session journal 中的一条事件（等价 AgentEvent）。 */
export class AgentEvent {
  constructor(
    public readonly runId: RunId,
    public readonly sequence: number,
    public readonly kind: string,
    public readonly eventRef: ArtifactRef,
    public readonly data: Record<string, unknown> | null = null
  ) {}
}

/** Run 的只读摘要（等价 RunSummary）。 */
export class RunSummary {
  constructor(
    public readonly runId: RunId,
    public readonly agentId: AgentId,
    public readonly status: RunStatus,
    public readonly responseRef: ArtifactRef | null = null,
    public readonly error: string | null = null,
    public readonly reason: string | null = null
  ) {}
}

/** Agent 元数据快照（等价 AgentSnapshot）。 */
export class AgentSnapshot {
  constructor(
    public readonly agentId: AgentId,
    public readonly path: AgentPath,
    public readonly name: string,
    public readonly agentType: string,
    public readonly status: AgentStatus,
    public readonly parentId: AgentId | null,
    public readonly pendingRunId: RunId | null = null
  ) {}
}

/** wait_agent 的结果（等价 AgentWaitResult）。 */
export class AgentWaitResult {
  constructor(
    public readonly completed: Record<AgentId, RunSummary>,
    public readonly timedOut: boolean = false
  ) {}
}
