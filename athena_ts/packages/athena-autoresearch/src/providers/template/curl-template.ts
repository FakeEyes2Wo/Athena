import { execFile } from "node:child_process"
import { copyFileSync, existsSync, mkdirSync } from "node:fs"
import { promisify } from "node:util"
import { join } from "node:path"
import type { TemplateDir, TemplateProvider } from "../types.js"

const execFileAsync = promisify(execFile)

export interface CurlTemplateOptions {
  cacheRoot: string
  registry: Record<string, { url: string; mainFile: string; rootDir: string }>
  /** 测试注入点；默认用系统 curl。 */
  fetchImpl?: (url: string, dest: string) => Promise<void>
}

export class CurlTemplateProvider implements TemplateProvider {
  readonly id = "curl" as const
  readonly version = "0.1.0"
  readonly capabilities = ["paper.template"]

  constructor(private readonly opts: CurlTemplateOptions) {}

  async fetch(venue: string): Promise<TemplateDir> {
    const entry = this.opts.registry[venue]
    if (!entry) throw new Error(`template not registered: ${venue}`)

    const current = join(this.opts.cacheRoot, venue, "current")
    const cache = join(this.opts.cacheRoot, venue, "cache")
    mkdirSync(join(this.opts.cacheRoot, venue), { recursive: true })

    const tmp = join(this.opts.cacheRoot, venue, `template-${Date.now()}.zip`)
    try {
      await this.download(entry.url, tmp)
      mkdirSync(current, { recursive: true })
      // 未内置解压器；把 zip 原样放入 current，后续实现解压。
      this.copyFile(tmp, join(current, "template.zip"))
      if (existsSync(cache)) {
        // 简单覆盖：实现期替换为 rm -rf 后再复制
      }
      mkdirSync(cache, { recursive: true })
      this.copyFile(tmp, join(cache, "template.zip"))
      return {
        root: current,
        source: "network",
        mainFile: entry.mainFile,
        styleFiles: [],
        editableFiles: [entry.mainFile, "sections"],
      }
    } catch (error) {
      if (existsSync(join(cache, "template.zip"))) {
        return {
          root: cache,
          source: "cache",
          mainFile: entry.mainFile,
          styleFiles: [],
          editableFiles: [entry.mainFile, "sections"],
        }
      }
      throw new Error(`template fetch failed for ${venue}: ${String(error)}`)
    }
  }

  list(): string[] {
    return Object.keys(this.opts.registry)
  }

  private async download(url: string, dest: string): Promise<void> {
    if (this.opts.fetchImpl) {
      await this.opts.fetchImpl(url, dest)
      return
    }
    try {
      await execFileAsync("curl", ["-L", "--fail", "--silent", "--show-error", url, "-o", dest], {
        timeout: 120_000,
      })
    } catch (error) {
      const err = error as NodeJS.ErrnoException & { stderr?: string }
      throw new Error(err.stderr ?? err.message)
    }
  }

  private copyFile(from: string, to: string): void {
    copyFileSync(from, to)
  }
}
