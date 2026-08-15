/** 归一化任意 catch 到的异常为可显示的消息文本。 */
export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
