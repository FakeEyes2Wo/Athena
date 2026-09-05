/**
 * BaseTool、ToolRegistry 与 ``tool()`` 工厂（移植 ``core/tool.py``）。
 *
 * Python 的 ``@tool`` 装饰器依赖运行时函数签名/类型注解/docstring 内省；TS 无
 * 内省能力，故 ``tool(fn, opts?)`` 以显式 ``name``/``description``/``inputSchema``
 * 表达，``name`` 缺省取 ``fn.name``，``inputSchema`` 缺省为空对象 schema。
 */

import { CancelledError } from "./agent/types.js"
import {
  TOOL_BEGIN,
  TOOL_END,
  TOOL_ERROR,
  ToolContext,
  ToolResult,
  ToolSpec,
} from "./tool-types.js"

async function noopEmit(_k: string, _r: string, _d?: Record<string, unknown> | null): Promise<void> {
  void 0
}

/** 工具执行函数：接收参数 dict，返回原始数据或 ToolResult。 */
export type ToolFn = (
  input: Record<string, unknown>
) => Promise<unknown> | unknown

/** ``tool()`` 工厂的可选元数据（等价 ``@tool`` 的显式参数）。 */
export interface ToolOptions {
  name?: string
  description?: string
  inputSchema?: Record<string, unknown>
  concurrencySafe?: boolean
}

export abstract class BaseTool {
  /** 子类需定义 ``spec`` + ``execute()``（返回原始数据，非 ToolResult）。 */
  abstract readonly spec: ToolSpec

  /** 执行工具逻辑，返回原始数据（非 ToolResult）。 */
  abstract execute(
    input: Record<string, unknown>,
    ctx: ToolContext
  ): Promise<unknown> | unknown

  /** 同步入口（TS 无 asyncio.run，退化为 async）：用独立取消令牌执行一次调用。 */
  invoke(input: Record<string, unknown> = {}): Promise<ToolResult> {
    const ctx = new ToolContext("", "sync", noopEmit, new AbortController().signal)
    return this.ainvoke(ctx, input)
  }

  /** 生命周期：开始 → 执行 → 结束/错误。 */
  async ainvoke(
    ctx: ToolContext,
    input: Record<string, unknown>
  ): Promise<ToolResult> {
    const eventData: Record<string, unknown> = { tool: ctx.toolName }
    const path = input["path"]
    if (typeof path === "string") eventData["path"] = path
    await ctx.emit(TOOL_BEGIN, `ev:${ctx.callId}:begin`, eventData)
    let result: ToolResult
    try {
      const raw = await this.execute(input, ctx)
      result = raw instanceof ToolResult ? raw : new ToolResult(raw)
    } catch (exc) {
      if (exc instanceof CancelledError) {
        await ctx.emit(TOOL_ERROR, `ev:${ctx.callId}:error`, null)
        throw exc
      }
      const message = exc instanceof Error
        ? `${exc.constructor.name}: ${exc.message}`
        : `${typeof exc}: ${String(exc)}`
      result = new ToolResult(
        { traceback: exc instanceof Error ? exc.stack : undefined },
        false,
        message
      )
    }
    await ctx.emit(
      result.success ? TOOL_END : TOOL_ERROR,
      `ev:${ctx.callId}:end`,
      eventData
    )
    return result
  }
}

/**
 * 把执行函数包装为 ``BaseTool`` 实例（等价 ``@tool`` 装饰器）。
 *
 * Python 由函数签名自动推导 input_schema；TS 无法内省，故未显式给出
 * ``inputSchema`` 时使用空对象 schema。``name`` 缺省取 ``fn.name``，
 * ``description`` 缺省取 ``fn.name``（TS 函数无 docstring）。
 */
export function tool(fn: ToolFn, opts: ToolOptions = {}): BaseTool {
  const spec = new ToolSpec(
    opts.name ?? fn.name,
    opts.description ?? fn.name,
    opts.inputSchema ?? { type: "object", properties: {}, required: [] },
    opts.concurrencySafe ?? true
  )

  class DecoratedTool extends BaseTool {
    readonly spec = spec
    execute(
      input: Record<string, unknown>,
      _ctx: ToolContext
    ): Promise<unknown> | unknown {
      return fn(input)
    }
  }

  return new DecoratedTool()
}

export class ToolRegistry {
  private tools = new Map<string, BaseTool>()

  /** 注册工具；重名报错，按名称维持排序。 */
  register(t: BaseTool): void {
    if (this.tools.has(t.spec.name)) {
      throw new Error(`Tool '${t.spec.name}' already registered`)
    }
    this.tools.set(t.spec.name, t)
    this.tools = new Map([...this.tools].sort(([, a], [, b]) =>
      a.spec.name.localeCompare(b.spec.name)
    ))
  }

  /** 按名称取工具；未注册报错。 */
  resolve(name: string): BaseTool {
    const t = this.tools.get(name)
    if (t === undefined) throw new Error(`Tool '${name}' not found`)
    return t
  }

  /** 按名称排序的工具 spec 列表。 */
  get specs(): ToolSpec[] {
    return [...this.tools.values()].map((t) => t.spec)
  }

  get size(): number {
    return this.tools.size
  }

  has(name: string): boolean {
    return this.tools.has(name)
  }
}
