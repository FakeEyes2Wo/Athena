/**
 * 本地 argv 命令执行；调用方明确提供工作目录。
 */

import { execFile } from "node:child_process"
import { promisify } from "node:util"
import type { EmitEvent } from "@athena/agent"

const execFileP = promisify(execFile)

/** 命令执行结果（模型可见的紧凑视图）。 */
export class CommandResult {
  constructor(
    public readonly ok: boolean,
    public readonly stdout: string,
    public readonly stderr: string,
    public readonly exit_code: number
  ) {}
}

/** 一次命令的输入；工作目录只在这里提供。 */
export interface CommandOptions {
  argv?: string[] | null
  timeout_s?: number
  workdir: string
  emit?: EmitEvent | null
}

/** ExecutionRuntime 的最小接口（PlanRunner 只依赖 run）。 */
export interface ExecutionRuntime {
  run(opts: CommandOptions): Promise<CommandResult>
}

/** 本地执行器：用系统子进程执行 argv 命令（首版 strong_isolation=false）。 */
export class LocalExecutionRuntime implements ExecutionRuntime {
  async run(opts: CommandOptions): Promise<CommandResult> {
    const argv = opts.argv ?? []
    if (argv.length === 0) {
      return new CommandResult(false, "", "argv must be a non-empty list", 1)
    }
    if (opts.emit) {
      await opts.emit("command/started", "exec:run", { command: argv })
    }
    try {
      const { stdout, stderr } = await execFileP(argv[0]!, argv.slice(1), {
        cwd: opts.workdir,
        timeout: (opts.timeout_s ?? 120) * 1000,
        maxBuffer: 100 * 1024 * 1024,
        encoding: "utf-8",
      })
      return new CommandResult(true, String(stdout), String(stderr), 0)
    } catch (err) {
      const e = err as { code?: number | string; stderr?: string; stdout?: string }
      return new CommandResult(
        false,
        String(e.stdout ?? ""),
        String(e.stderr ?? ""),
        typeof e.code === "number" ? e.code : 1
      )
    }
  }
}
