import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import {
  ArtifactIntegrityError,
  ArtifactNotFoundError,
  InvalidArtifactRefError,
  LocalArtifactStore,
} from "../../src/services/artifact-store.js"

async function withStore<T>(fn: (store: LocalArtifactStore, root: string) => Promise<T>): Promise<T> {
  const root = mkdtempSync(join(tmpdir(), "athena-art-"))
  try {
    return await fn(new LocalArtifactStore(root), root)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

describe("LocalArtifactStore", () => {
  it("bytes and text round trip", async () => {
    await withStore(async (store) => {
      const bytesRef = await store.putBytes(Buffer.from([0x00, 0x70, 0x61, 0x70, 0x65, 0x72]))
      const textRef = await store.putText("论文 text")

      expect(await store.getBytes(bytesRef)).toEqual(Buffer.from([0x00, 0x70, 0x61, 0x70, 0x65, 0x72]))
      expect(await store.getText(textRef)).toBe("论文 text")
    })
  })

  it("content addressing is idempotent under concurrency", async () => {
    await withStore(async (store) => {
      const refs = await Promise.all(Array.from({ length: 20 }, () => store.putBytes(Buffer.from("same"))))

      expect(new Set(refs).size).toBe(1)
      expect(existsSync(store.pathFor(refs[0]!))).toBe(true)
    })
  })

  it("rejects invalid reference before path resolution", async () => {
    await withStore(async (store) => {
      await expect(store.getBytes("sha256:../../outside")).rejects.toThrow(InvalidArtifactRefError)
    })
  })

  it("missing and corrupt artifacts are distinct", async () => {
    await withStore(async (store) => {
      const missing = "sha256:" + "0".repeat(64)
      await expect(store.getBytes(missing)).rejects.toThrow(ArtifactNotFoundError)

      const ref = await store.putBytes(Buffer.from("valid"))
      writeFileSync(store.pathFor(ref), "corrupt")
      await expect(store.getBytes(ref)).rejects.toThrow(ArtifactIntegrityError)
    })
  })
})
