/**
 * shell_command 工具：让 LLM worker 经 LocalExecutionRuntime 在 workspace 写文件/跑命令
 * （首版 strong_isolation=false，对应 Python generic_tools 的 shell 能力）。
 */

import { tool, type BaseTool } from "@athena/agent"
import { CommandResult, type ExecutionContext, type ExecutionRuntime } from "./execution.js"

/** 把一次命令执行的紧凑结果返回给 LLM（等价 CommandResult.to_dict）。 */
export function commandResultDict(result: CommandResult): Record<string, unknown> {
  const data: Record<string, unknown> = {
    ok: result.ok,
    stdout: result.stdout,
    stderr: result.stderr,
    exit_code: result.exit_code,
  }
  if (result.error !== null) data["error"] = result.error
  if (result.truncated) data["truncated"] = true
  if (result.output_ref !== null) data["output_ref"] = result.output_ref
  return data
}

export function makeShellTool(runtime: ExecutionRuntime, context: ExecutionContext): BaseTool {
  return tool(
    async (input: Record<string, unknown>) => {
      const command = String(input["command"] ?? "")
      const argv = command.trim().split(/\s+/).filter(Boolean)
      if (argv.length === 0) {
        return commandResultDict(new CommandResult(false, "", "command must be non-empty", 1))
      }
      const result = await runtime.run(context, { argv, timeout_s: 300 })
      return commandResultDict(result)
    },
    {
      name: "shell_command",
      description: "Run a shell command (argv-style, split on whitespace) in the project workspace.",
      inputSchema: {
        type: "object",
        properties: { command: { type: "string", description: "The command to run." } },
        required: ["command"],
      },
    }
  )
}
