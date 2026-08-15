import type { UIMessage } from "../../types/ui";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";
import { ErrorCard } from "../cards/ErrorCard";
import { Icon } from "../common/Icon";
import styles from "./MessageList.module.css";

interface MessageListProps {
  messages: UIMessage[];
  onStartRun(task?: string, messageId?: string): Promise<void>;
}

/** True for parallel Ideator lanes (plan ids like ``ideator-1`` or ``ideator-<round>-1``). */
function ideatorNumber(plan?: string): number | null {
  if (!plan) return null;
  const match = /^ideator-(?:\d+-)?([1-9][0-9]*)$/.exec(plan);
  return match ? Number(match[1]) : null;
}

/** Group consecutive ideator-lane messages into one block per lane set. */
function segmentMessages(messages: UIMessage[]): Array<{ lane: number | null; items: UIMessage[] }> {
  const segments: Array<{ lane: number | null; items: UIMessage[] }> = [];
  for (const msg of messages) {
    const lane = ideatorNumber(msg.plan);
    const last = segments[segments.length - 1];
    if (lane !== null && last && last.lane === lane) {
      last.items.push(msg);
    } else {
      segments.push({ lane, items: [msg] });
    }
  }
  return segments;
}

/** A single trajectory record (user / supervisor / agent / tool / error). */
function TrajectoryItem({ msg, onStartRun }: { msg: UIMessage; onStartRun: MessageListProps["onStartRun"] }) {
  if (msg.kind === "intent-preview" && msg.preview) {
    return (
      <IntentPreviewCard
        preview={msg.preview}
        started={msg.started === true}
        onConfirm={() => onStartRun(msg.task, msg.id)}
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
export function MessageList({ messages, onStartRun }: MessageListProps) {
  const segments = segmentMessages(messages);

  return (
    <div className={styles["message-list"]} role="log" aria-live="polite">
      {segments.map((segment, index) => {
        if (segment.lane === null) {
          return segment.items.map((msg) => (
            <TrajectoryItem key={msg.id} msg={msg} onStartRun={onStartRun} />
          ));
        }
        return (
          <section key={`ideator-${segment.lane}-${index}`} className={styles["ideator-lane"]}>
            <div className={styles["ideator-lane__title"]}>Ideator {segment.lane}</div>
            {segment.items.map((msg) => (
              <TrajectoryItem key={msg.id} msg={msg} onStartRun={onStartRun} />
            ))}
          </section>
        );
      })}
    </div>
  );
}
