import type { StageContext } from "../../core/pipeline-types.js"
import type { CompilerProvider, CompileResult } from "../types.js"

export interface OverleafCompilerOptions {
  apiToken: string
  apiBaseUrl?: string
  projectId?: string
  fetchImpl?: typeof fetch
}

interface OverleafApiError {
  status: number
  statusText: string
}

/** Overleaf 官方 API 编译器适配（实现期以官方文档校准端点）。 */
export class OverleafCompiler implements CompilerProvider {
  readonly id = "overleaf" as const
  readonly version = "0.1.0"
  readonly capabilities = ["paper.compile", "overleaf"]

  private readonly baseUrl: string
  private readonly fetch: typeof fetch

  constructor(private readonly opts: OverleafCompilerOptions) {
    this.baseUrl = opts.apiBaseUrl ?? "https://api.overleaf.com/api/v2"
    this.fetch = opts.fetchImpl ?? fetch
  }

  async compile(ctx: StageContext, paperDir: string): Promise<CompileResult> {
    const projectId = this.opts.projectId ?? (await this.createProject(ctx.runId))
    await this.syncFiles(projectId, paperDir)
    const compileId = await this.triggerCompile(projectId)
    const logText = await this.getLogs(projectId, compileId)
    const logRef = await ctx.artifacts.putText(logText)
    const ok = !/error/i.test(logText)
    return { ok, logRef, consoleText: logText, compiler: "overleaf" }
  }

  private async createProject(name: string): Promise<string> {
    const response = await this.request("/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    })
    const data = (await response.json()) as { id?: string }
    if (!data.id) throw new Error("overleaf createProject response missing id")
    return data.id
  }

  private async syncFiles(projectId: string, paperDir: string): Promise<void> {
    const response = await this.request(`/projects/${projectId}/files`, {
      method: "PUT",
      body: JSON.stringify({ paperDir }),
    })
    if (!response.ok) throw await this.toError(response)
  }

  private async triggerCompile(projectId: string): Promise<string> {
    const response = await this.request(`/projects/${projectId}/compile`, {
      method: "POST",
      body: JSON.stringify({}),
    })
    const data = (await response.json()) as { compile_id?: string }
    if (!data.compile_id) throw new Error("overleaf compile response missing compile_id")
    return data.compile_id
  }

  private async getLogs(projectId: string, compileId: string): Promise<string> {
    const response = await this.request(`/projects/${projectId}/output/log?compile_id=${compileId}`, {
      method: "GET",
    })
    return response.text()
  }

  private async request(path: string, init: RequestInit): Promise<Response> {
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.opts.apiToken}`,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    })
    if (!response.ok) throw await this.toError(response)
    return response
  }

  private async toError(response: Response): Promise<Error> {
    const error: OverleafApiError = {
      status: response.status,
      statusText: response.statusText,
    }
    return new Error(`overleaf api ${error.status} ${error.statusText}`)
  }
}
