import { useEffect, useRef, useState } from "react";
import type { ClarificationPreview, UIMessage, UIMessagePreview } from "../../types/ui";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";
import { ErrorCard } from "../cards/ErrorCard";
import { Icon } from "../common/Icon";
import styles from "./MessageList.module.css";

interface MessageListProps {
  messages: UIMessage[];
  onConfirmPreview(acknowledgeUnresolved: boolean): Promise<void> | void;
  onRevisePreview(instruction: string): Promise<void> | void;
  onRetryPreview(): Promise<void> | void;
  onCancelPreview(): Promise<void> | void;
}

/** True for parallel Ideator lanes (plan ids like ``ideator-1`` or ``ideator-<round>-1``). */
function ideatorNumber(plan?: string): number | null {
  if (!plan) return null;
  const match = /^ideator-(?:\d+-)?([1-9][0-9]*)$/.exec(plan);
  return match ? Number(match[1]) : null;
}

type MessageSegment =
  | { kind: "ordinary"; items: UIMessage[] }
  | { kind: "ideator"; lane: number; items: UIMessage[] }
  | { kind: "clarification"; items: UIMessage[] };

function activeClarificationPreviewIndex(messages: UIMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const msg = messages[index];
    if (msg.kind !== "intent-preview" || !msg.preview || !("draftId" in msg.preview)) continue;
    return msg.preview.status === "CLARIFYING" || msg.preview.status === "CONFIRMING"
      ? index
      : -1;
  }
  return -1;
}

/** Group ideator lanes and backend output emitted during active task understanding. */
function segmentMessages(messages: UIMessage[]): MessageSegment[] {
  const segments: MessageSegment[] = [];
  const clarificationPreviewIndex = activeClarificationPreviewIndex(messages);

  for (const [index, msg] of messages.entries()) {
    const lane = ideatorNumber(msg.plan);
    const last = segments[segments.length - 1];

    if (
      clarificationPreviewIndex >= 0 &&
      index > clarificationPreviewIndex &&
      msg.role !== "user" &&
      msg.kind !== "intent-preview"
    ) {
      if (last?.kind === "clarification") {
        last.items.push(msg);
      } else {
        segments.push({ kind: "clarification", items: [msg] });
      }
    } else if (lane !== null && last?.kind === "ideator" && last.lane === lane) {
      last.items.push(msg);
    } else if (lane !== null) {
      segments.push({ kind: "ideator", lane, items: [msg] });
    } else {
      segments.push({ kind: "ordinary", items: [msg] });
    }
  }
  return segments;
}

function toClarificationPreview(
  preview: UIMessagePreview,
  started: boolean,
): ClarificationPreview {
  if ("draftId" in preview) return preview;
  return {
    draftId: "",
    revision: 0,
    status: started ? "RUNNING" : "READY_FOR_CONFIRMATION",
    understanding: {
      title: preview.title ?? "",
      dataset: preview.dataset ?? null,
      target: preview.target ?? null,
      task_type: preview.task_type ?? "other",
      primary_metric: preview.primary_metric ?? null,
      direction: preview.direction ?? null,
      evaluation_plan: preview.evaluation_plan ?? null,
    },
    answers: [],
    unresolved: [],
    failure: null,
  };
}

/** A single trajectory record (user / supervisor / agent / tool / error). */
function TrajectoryItem({ msg, onConfirmPreview, onRevisePreview, onRetryPreview, onCancelPreview }: {
  msg: UIMessage;
  onConfirmPreview: MessageListProps["onConfirmPreview"];
  onRevisePreview: MessageListProps["onRevisePreview"];
  onRetryPreview: MessageListProps["onRetryPreview"];
  onCancelPreview: MessageListProps["onCancelPreview"];
}) {
  if (msg.kind === "intent-preview" && msg.preview) {
    const preview = toClarificationPreview(msg.preview, msg.started === true);
    // While the authoritative draft is still being built, keep the chat focused
    // on the task-understanding conversation. The decision card appears only
    // once the draft is ready/failed/running (or for legacy previews).
    if ("draftId" in msg.preview && preview.status === "CLARIFYING") {
      return (
        <article className={`${styles["message-bubble"]} ${styles["message-bubble--supervisor"]}`}>
          <span className={`${styles.marker} ${styles["marker--supervisor"]}`}>任务理解</span>
          {msg.content || "任务理解中…"}
        </article>
      );
    }
    return (
      <IntentPreviewCard
        preview={preview}
        onConfirm={onConfirmPreview}
        onRevise={onRevisePreview}
        onRetry={onRetryPreview}
        onCancel={onCancelPreview}
      />
    );
  }
  if (msg.kind === "error") {
    return <ErrorCard content={msg.content} />;
  }
  if (msg.role === "user") {
    return (
      <article className={`${styles["message-bubble"]} ${styles["message-bubble--user"]}`}>
        {msg.content}
      </article>
    );
  }
  if (msg.source === "tool") {
    const label = msg.tool || "tool";
    return (
      <details className={styles["tool-output"]}>
        <summary>
          {label} · {msg.channel ?? "output"}
        </summary>
        <pre className="code-block">{msg.content}</pre>
      </details>
    );
  }
  if (msg.tool) {
    return (
      <article className={styles["tool-call"]}>
        <div className={styles["tool-call__head"]}>
          <Icon name="wrench" size={13} /> {msg.tool}
        </div>
        <pre className="code-block">{msg.content}</pre>
      </article>
    );
  }

  const supervisor = msg.source === "supervisor";
  return (
    <article
      className={`${styles["message-bubble"]} ${
        supervisor ? styles["message-bubble--supervisor"] : styles["message-bubble--agent"]
      }`}
    >
      <span className={`${styles.marker} ${supervisor ? styles["marker--supervisor"] : styles["marker--agent"]}`}>
        {supervisor ? "监督者" : "Agent"}
      </span>
      {msg.content}
    </article>
  );
}

/** Renders chat messages, intent preview cards, and the trajectory (agent/tool/supervisor/ideator). */
export function MessageList({
  messages,
  onConfirmPreview,
  onRevisePreview,
  onRetryPreview,
  onCancelPreview,
}: MessageListProps) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const segments = segmentMessages(messages);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;

    const update = () => {
      setShowJumpToBottom(el.scrollHeight - el.scrollTop - el.clientHeight > 120);
    };

    update();
    el.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      el.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [messages.length]);

  const scrollToBottom = () => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  return (
    <div className={styles["message-list"]} role="log" aria-live="polite" ref={listRef}>
      {segments.map((segment, index) => {
        if (segment.kind === "ordinary") {
          return segment.items.map((msg) => (
            <TrajectoryItem
              key={msg.id}
              msg={msg}
              onConfirmPreview={onConfirmPreview}
              onRevisePreview={onRevisePreview}
              onRetryPreview={onRetryPreview}
              onCancelPreview={onCancelPreview}
            />
          ));
        }
        if (segment.kind === "clarification") {
          return (
            <section
              key={`clarification-${index}`}
              className={styles["clarification-activity"]}
              role="log"
              aria-label="任务理解过程"
              aria-live="polite"
            >
              <div className={styles["clarification-activity__title"]}>任务理解过程</div>
              {segment.items.map((msg) => (
                <TrajectoryItem
                  key={msg.id}
                  msg={msg}
                  onConfirmPreview={onConfirmPreview}
                  onRevisePreview={onRevisePreview}
                  onRetryPreview={onRetryPreview}
                  onCancelPreview={onCancelPreview}
                />
              ))}
            </section>
          );
        }
        return (
          <section key={`ideator-${segment.lane}-${index}`} className={styles["ideator-lane"]}>
            <div className={styles["ideator-lane__title"]}>Ideator {segment.lane}</div>
            {segment.items.map((msg) => (
              <TrajectoryItem
                key={msg.id}
                msg={msg}
                onConfirmPreview={onConfirmPreview}
                onRevisePreview={onRevisePreview}
                onRetryPreview={onRetryPreview}
                onCancelPreview={onCancelPreview}
              />
            ))}
          </section>
        );
      })}
      {showJumpToBottom && (
        <button
          type="button"
          className={styles["jump-to-bottom"]}
          onClick={scrollToBottom}
          aria-label="返回底部"
          title="返回底部"
        >
          <Icon name="chevronDown" size={16} />
          <span>返回底部</span>
        </button>
      )}
    </div>
  );
}
