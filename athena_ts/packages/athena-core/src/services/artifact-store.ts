import { createHash, randomBytes } from "node:crypto"
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { join, resolve } from "node:path"
import { AthenaIntegrityError, AthenaNotFoundError, AthenaValidationError } from "../errors.js"
import type { ArtifactRef } from "../models/contracts.js"

const REF_PREFIX = "sha256:"
const REF_PATTERN = /^sha256:([0-9a-f]{64})$/

/** ArtifactRef 不符合 ``sha256:<64 hex>`` 格式。 */
export class InvalidArtifactRefError extends AthenaValidationError {}

/** 内容寻址引用在存储中不存在。 */
export class ArtifactNotFoundError extends AthenaNotFoundError {}

/** 落盘内容与引用中的 SHA-256 不一致。 */
export class ArtifactIntegrityError extends AthenaIntegrityError {}

/** 计算内容引用；例如 ``digestRef(Buffer.from("a"))`` 返回 ``sha256:ca978...``。 */
export function digestRef(data: Uint8Array): ArtifactRef {
  return REF_PREFIX + createHash("sha256").update(data).digest("hex")
}

/** 校验并取出十六进制摘要；非法引用抛出 ``InvalidArtifactRefError``。 */
export function digestFromRef(ref: ArtifactRef): string {
  const match = REF_PATTERN.exec(ref)
  if (!match) throw new InvalidArtifactRefError(`Invalid artifact reference: ${ref}`)
  return match[1]!
}

/** 本地分片式内容寻址存储（文件放 ``root/ab/cdef…``，同内容写入幂等）。 */
export class LocalArtifactStore {
  private readonly root: string

  constructor(root: string) {
    this.root = resolve(root)
    mkdirSync(this.root, { recursive: true })
  }

  /** 返回受校验引用的磁盘路径，主要用于诊断与本地工具集成。 */
  pathFor(ref: ArtifactRef): string {
    const digest = digestFromRef(ref)
    return join(this.root, digest.slice(0, 2), digest.slice(2))
  }

  async putBytes(data: Uint8Array): Promise<ArtifactRef> {
    const ref = digestRef(data)
    const target = this.pathFor(ref)
    if (existsSync(target)) {
      if (digestRef(readFileSync(target)) !== ref) {
        throw new ArtifactIntegrityError(`Artifact digest mismatch: ${ref}`)
      }
      return ref
    }
    const digest = digestFromRef(ref)
    const dir = join(this.root, digest.slice(0, 2))
    mkdirSync(dir, { recursive: true })
    const temporary = join(dir, `.artifact-${randomBytes(8).toString("hex")}`)
    try {
      const fd = openSync(temporary, "wx")
      try {
        writeFileSync(fd, data)
        fsyncSync(fd)
      } finally {
        closeSync(fd)
      }
      try {
        renameSync(temporary, target)
      } catch (err) {
        // Windows 并发写已创建目标 → 接受摘要一致的既有内容
        if (!existsSync(target) || digestRef(readFileSync(target)) !== ref) throw err
      }
    } finally {
      try {
        unlinkSync(temporary)
      } catch {
        /* temp file already renamed or never created — ignore */
      }
    }
    return ref
  }

  async getBytes(ref: ArtifactRef): Promise<Uint8Array> {
    const target = this.pathFor(ref)
    let data: Buffer
    try {
      data = readFileSync(target)
    } catch {
      // 引用合法但本地内容不存在 → 转换为稳定的存储层异常
      throw new ArtifactNotFoundError(`Artifact not found: ${ref}`)
    }
    if (digestRef(data) !== ref) {
      throw new ArtifactIntegrityError(`Artifact digest mismatch: ${ref}`)
    }
    return data
  }

  async putText(text: string): Promise<ArtifactRef> {
    return this.putBytes(Buffer.from(text, "utf-8"))
  }

  async getText(ref: ArtifactRef): Promise<string> {
    return Buffer.from(await this.getBytes(ref)).toString("utf-8")
  }
}
