import fs from "node:fs"
import path from "node:path"

/** 原子写 JSON（tmp + fsync + replace），等价 persistence.atomic_write_json。 */
export function atomicWriteJson(target: string, payload: unknown): string {
  fs.mkdirSync(path.dirname(target), { recursive: true })
  const temporary = path.join(path.dirname(target), `${path.basename(target)}.tmp`)
  try {
    const fd = fs.openSync(temporary, "w")
    try {
      fs.writeFileSync(fd, JSON.stringify(payload, null, 2) + "\n", "utf-8")
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }
    fs.renameSync(temporary, target)
  } catch (err) {
    fs.rmSync(temporary, { force: true })
    throw err
  }
  return target
}
