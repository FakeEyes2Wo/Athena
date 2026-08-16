import { execFile } from "node:child_process"
import { promisify } from "node:util"
import type { StageContext } from "../../core/pipeline-types.js"
import type { CompilerProvider, CompileResult } from "../types.js"

const execFileAsync = promisify(execFile)

const COMMANDS: Record<"latexmk" | "tectonic" | "pdflatex", [string, string[]]> = {
  latexmk: ["latexmk", ["-pdf", "-interaction=nonstopmode", "-halt-on-error"]],
  tectonic: ["tectonic", ["-X", "compile", "main.tex"]],
  pdflatex: ["pdflatex", ["-interaction=nonstopmode", "-halt-on-error", "main.tex"]],
}

export class LocalTexCompiler implements CompilerProvider {
  readonly id = "local" as const
  readonly version = "0.1.0"
  readonly capabilities = ["paper.compile"]

  constructor(
    private readonly compiler: "latexmk" | "tectonic" | "pdflatex" = "latexmk",
  ) {}

  async compile(ctx: StageContext, paperDir: string): Promise<CompileResult> {
    const [command, baseArgs] = COMMANDS[this.compiler]
    let ok = true
    let logText: string
    try {
      const { stdout, stderr } = await execFileAsync(command, baseArgs, {
        cwd: paperDir,
        timeout: 300_000,
      })
      logText = `stdout:\n${stdout}\nstderr:\n${stderr}`
    } catch (error) {
      const err = error as NodeJS.ErrnoException & { stdout?: string; stderr?: string }
      if (err.code === "ENOENT") {
        throw new Error(`TeX compiler not found: ${command}`)
      }
      ok = false
      logText = `stdout:\n${err.stdout ?? ""}\nstderr:\n${err.stderr ?? err.message}`
    }
    const logRef = await ctx.artifacts.putText(logText)
    return { ok, logRef, consoleText: logText, compiler: this.compiler }
  }
}
