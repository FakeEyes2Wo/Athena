/** User-facing explanations for typed backend error codes (``BackendError.code``). */
const CODE_MESSAGES: Record<string, string> = {
  different_task: "当前会话已有一个进行中或待确认的任务。请先确认或取消它，或新建会话开始新任务。",
  stale_revision: "任务理解已更新，请查看最新内容后再操作。",
  draft_not_ready: "任务理解尚未完成，请稍候再试。",
  draft_not_cancellable: "该任务已开始运行，无法再取消理解。",
  unresolved_ack_required: "仍有关键未确认项，请勾选确认后再启动。",
  invalid_instruction: "修改意见不能为空。",
  wrong_session: "该任务属于其他会话，请切换回对应会话后再操作。",
  resume_unavailable: "没有可继续的中断任务。",
  expired_request: "这个问题已超时失效。",
  stale_request: "这个问题已被处理或已失效。",
  duplicate_reply: "这个问题已经回答过了。",
};

/** 归一化任意 catch 到的异常为可显示的消息文本；已知后端错误码附带中文说明。 */
export function errorMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  const code =
    typeof err === "object" && err !== null && "code" in err
      ? String((err as { code: unknown }).code)
      : undefined;
  const friendly = code ? CODE_MESSAGES[code] : undefined;
  return friendly ?? raw;
}
