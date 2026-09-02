import type { HumanReply, HumanRequest, UIMessage } from "../types/ui";

/**
 * Pure helpers for rendering the task-understanding conversation.
 *
 * These functions only build stable message identities and display text; they
 * do not touch React state. Keeping them pure makes the hook simpler and makes
 * edge cases (deduplication, stale events, cross-session races) testable.
 */

/** Stable chat id for a clarification question. */
export function clarificationQuestionId(requestId: string): string {
  return `clarify-q-${requestId}`;
}

/** Stable chat id for a user's submitted clarification reply. */
export function clarificationReplyId(requestId: string): string {
  return `clarify-a-${requestId}`;
}

/** Stable chat id for a server-generated timeout/cancelled note. */
export function clarificationOutcomeId(requestId: string): string {
  return `clarify-o-${requestId}`;
}

/** Render a pending human request as an Athena clarification message. */
export function renderClarificationQuestion(request: HumanRequest): UIMessage {
  const choices =
    Array.isArray(request.choices) && request.choices.length > 0
      ? `\n可选：${request.choices.map((choice) => choice.label).join(" / ")}`
      : "";
  return {
    id: clarificationQuestionId(request.request_id),
    role: "athena",
    kind: "text",
    content: `${request.prompt.trim()}${choices}`,
    source: "clarification",
  };
}

/** Render a user reply as a chat user message. */
export function renderClarificationReply(requestId: string, reply: HumanReply): UIMessage {
  const content =
    reply.kind === "choice"
      ? `选择：${reply.value}`
      : reply.kind === "text"
        ? reply.text
        : "跳过";
  return {
    id: clarificationReplyId(requestId),
    role: "user",
    kind: "text",
    content,
  };
}

/** Render a server-generated timeout/cancelled outcome as an Athena note. */
export function renderClarificationOutcome(
  requestId: string,
  outcome: { kind?: string } | null | undefined,
): UIMessage | null {
  const text =
    outcome?.kind === "timeout"
      ? "该问题已超时"
      : outcome?.kind === "cancelled"
        ? "该问题已取消"
        : null;
  if (!text) return null;
  return {
    id: clarificationOutcomeId(requestId),
    role: "athena",
    kind: "text",
    content: text,
    source: "clarification",
  };
}

/**
 * True when a conversation already contains a representation of this request.
 * This prevents duplicates from:
 * - live `human_request` events racing with low-frequency `human_pending` polling;
 * - a reply being appended twice (the user path and a later settled event);
 * - stale events from a previous session with the same request id.
 */
export function hasClarificationMessage(
  messages: UIMessage[],
  requestId: string,
  kind: "question" | "reply" | "outcome",
): boolean {
  const id =
    kind === "question"
      ? clarificationQuestionId(requestId)
      : kind === "reply"
        ? clarificationReplyId(requestId)
        : clarificationOutcomeId(requestId);
  return messages.some((message) => message.id === id);
}
