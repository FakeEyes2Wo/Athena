import { randomBytes } from "node:crypto"

/** 生成唯一 ID，格式 ``{prefix}_{12位随机hex}``；prefix 校验与 Python new_id 一致。 */
export function newId(prefix: string): string {
  if (!prefix || !/^[A-Za-z0-9_]+$/.test(prefix) || !/[A-Za-z0-9]/.test(prefix.replace(/_/g, ""))) {
    throw new TypeError(
      "prefix must contain at least one alphanumeric character and may otherwise contain only alphanumeric characters or underscores"
    )
  }
  return `${prefix}_${randomBytes(6).toString("hex")}`
}
