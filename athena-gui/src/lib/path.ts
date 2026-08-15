/** 取路径最后一段作为展示名；空值回退为"未选择"。 */
export function basename(path: string | null): string {
  if (!path) return "未选择";
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}
