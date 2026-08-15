/**
 * ``request_user_input`` 工具 — LLM 需要澄清时向用户提问并等待回答（移植 user_input.py）。
 */

import { BaseTool } from "../../tool.js"
import { ToolContext, ToolSpec } from "../../tool-types.js"

export class RequestUserInputTool extends BaseTool {
  readonly spec = new ToolSpec(
    "request_user_input",
    "当用户意图不明确、缺少关键信息或需要澄清时，向用户提出一个问题。传入具体、可回答的问题，不要臆测用户意图。",
    {
      type: "object",
      properties: {
        prompt: { type: "string", description: "向用户提出的澄清问题" },
      },
      required: ["prompt"],
      additionalProperties: false,
    }
  )

  async execute(
    input: Record<string, unknown>,
    ctx: ToolContext
  ): Promise<string> {
    if (ctx.askUser === null) {
      throw new Error("ask_user 未注入，无法请求用户输入")
    }
    const answer = await ctx.askUser(input["prompt"] as string)
    return answer !== null ? answer : "[用户未在超时内回答]"
  }
}
