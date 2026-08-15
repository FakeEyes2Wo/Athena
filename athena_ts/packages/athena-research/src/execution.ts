/**
 * 命令执行上下文与结果（移植 ``execution/runtime.py`` 的轻量数据容器）。
 */

import { execFile } from "node:child_process"
import { promisify } from "node:util"
import type { EmitEvent } from "@athena/agent"

const execFileP = promisify(execFile)

/** 一次命令执行的上下文：三个根路径 + 可选 experiment_id。 */
export class ExecutionContext {
  constructor(
    public readonly project_root: string,
    public readonly workspace_root: string,
    public readonly environment_root: string,
    public readonly experiment_id: string | null = null
  ) {}
}

/** 命令执行结果（模型可见的紧凑视图）。 */
export class CommandResult {
  constructor(
    public readonly ok: boolean,
    public readonly stdout: string,
    public readonly stderr: string,
    public readonly exit_code: number,
    public readonly error: string | null = null,
    public readonly truncated: boolean = false,
    public readonly output_ref: string | null = null
  ) {}
}

/** ExecutionRuntime 的最小接口（PlanRunner 只依赖 run）。 */
export interface ExecutionRuntime {
  project_root: string
  environment_root: string
  ensureEnvironment(): void
  run(
    context: ExecutionContext,
    opts: {
      argv?: string[] | null
      timeout_s?: number
      workdir?: string | null
      emit?: EmitEvent | null
    }
  ): Promise<CommandResult>
}

/** 本地执行器：用系统子进程执行 argv 命令（首版 strong_isolation=false）。 */
export class LocalExecutionRuntime implements ExecutionRuntime {
  constructor(
    public readonly project_root: string,
    public readonly environment_root: string
  ) {}

  ensureEnvironment(): void {}

  async run(
    context: ExecutionContext,
    opts: {
      argv?: string[] | null
      timeout_s?: number
      workdir?: string | null
      emit?: EmitEvent | null
    }
  ): Promise<CommandResult> {
    const argv = opts.argv ?? []
    if (argv.length === 0) {
      return new CommandResult(false, "", "argv must be a non-empty list", 1)
    }
    const cwd = opts.workdir ?? context.workspace_root
    if (opts.emit) {
      await opts.emit("command/started", "exec:run", { command: argv })
    }
    try {
      const { stdout, stderr } = await execFileP(argv[0]!, argv.slice(1), {
        cwd,
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
