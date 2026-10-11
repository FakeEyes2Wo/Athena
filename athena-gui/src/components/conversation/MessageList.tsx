import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import type { ClarificationPreview, UIMessage, UIMessagePreview } from "../../types/ui";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../common/Icon";
import { Markdown } from "../common/Markdown";
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
  | { kind: "ordinary"; items: [UIMessage] }
  | { kind: "ideator"; lane: number; items: UIMessage[] }
  | { kind: "clarification"; items: UIMessage[] };

interface ActiveClarificationPreview {
  index: number;
  preview: ClarificationPreview;
}

function activeClarificationPreview(messages: UIMessage[]): ActiveClarificationPreview | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const msg = messages[index];
    if (msg.kind !== "intent-preview" || !msg.preview || !("draftId" in msg.preview)) continue;
    return msg.preview.status === "CLARIFYING" || msg.preview.status === "CONFIRMING"
      ? { index, preview: msg.preview }
      : null;
  }
  return null;
}

function belongsToTaskUnderstanding(message: UIMessage, preview: ClarificationPreview): boolean {
  if (message.scope === "task_understanding") {
    const acceptedScopeId = preview.draftId || preview.optimisticScopeId;
    return Boolean(acceptedScopeId) && message.scopeId === acceptedScopeId;
  }

  return message.sessionId === undefined && message.scope === undefined && message.scopeId === undefined;
}

function clarificationSegmentKey(segment: Extract<MessageSegment, { kind: "clarification" }>): string {
  const first = segment.items[0];
  const id = first?.id?.trim();
  if (id) return `clarification-${id}`;

  // UIMessage.id is required, but retain a deterministic, index-free fallback
  // for malformed restored records so nearby inserts do not remount this log.
  const fingerprint = [
    first?.role,
    first?.kind,
    first?.source,
    first?.channel,
    first?.tool,
    first?.plan,
    first?.content,
  ];
  return `clarification-fallback-${JSON.stringify(fingerprint)}`;
}

/**
 * Task-understanding failure notices duplicate the failure the intent card for
 * the same draft already shows (and go stale once it is retried); keep only
 * notices that have no card to live in.
 */
function withoutCardDuplicates(messages: UIMessage[]): UIMessage[] {
  const carded = new Set<string>();
  for (const msg of messages) {
    if (msg.kind === "intent-preview" && msg.preview && "draftId" in msg.preview && msg.preview.draftId) {
      carded.add(msg.preview.draftId);
    }
  }
  if (carded.size === 0) return messages;
  return messages.filter(
    (msg) =>
      !(
        (msg.kind === "error" || msg.channel === "error") &&
        msg.scope === "task_understanding" &&
        msg.scopeId !== undefined &&
        carded.has(msg.scopeId)
      ),
  );
}

/** Group ideator lanes and backend output emitted during active task understanding. */
function segmentMessages(allMessages: UIMessage[]): MessageSegment[] {
  const segments: MessageSegment[] = [];
  const messages = withoutCardDuplicates(allMessages);
  const clarification = activeClarificationPreview(messages);

  for (const [index, msg] of messages.entries()) {
    const lane = ideatorNumber(msg.plan);
    const last = segments[segments.length - 1];

    if (
      clarification !== null &&
      index > clarification.index &&
      msg.role !== "user" &&
      msg.kind !== "intent-preview" &&
      belongsToTaskUnderstanding(msg, clarification.preview)
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

function toClarificationPreview(preview: UIMessagePreview, started: boolean): ClarificationPreview {
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

/** Who "speaks" a message; consecutive messages from one speaker share a label. */
function speakerOf(msg: UIMessage): string {
  if (msg.role === "user") return "user";
  if (msg.kind !== "text") return msg.kind;
  if (msg.source === "tool" || msg.channel === "stdout" || msg.channel === "stderr" || msg.tool) return "tool";
  if (msg.source === "agent") return `agent:${msg.plan ?? ""}`;
  return "athena";
}

function oneLine(text: string, max = 96): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

interface TrajectoryItemProps {
  msg: UIMessage;
  showLabel: boolean;
  onConfirmPreview: MessageListProps["onConfirmPreview"];
  onRevisePreview: MessageListProps["onRevisePreview"];
  onRetryPreview: MessageListProps["onRetryPreview"];
  onCancelPreview: MessageListProps["onCancelPreview"];
}

/** A single trajectory record (user / Athena / agent / tool / error). */
const TrajectoryItem = memo(
  function TrajectoryItem({
    msg,
    showLabel,
    onConfirmPreview,
    onRevisePreview,
    onRetryPreview,
    onCancelPreview,
  }: TrajectoryItemProps) {
    if (msg.kind === "intent-preview" && msg.preview) {
      const preview = toClarificationPreview(msg.preview, msg.started === true);
      // While the authoritative draft is still being built, keep the chat focused
      // on the task-understanding conversation. The decision card appears only
      // once the draft is ready/failed/running (or for legacy previews).
      if ("draftId" in msg.preview && preview.status === "CLARIFYING") {
        return (
          <div className={styles.thinking} role="status">
            <Icon name="loader" size={15} className="icon--spin" />
            <span>{msg.content || "任务理解中…"}</span>
          </div>
        );
      }
      return (
        <div className={styles.cardRow}>
          <IntentPreviewCard
            preview={preview}
            onConfirm={onConfirmPreview}
            onRevise={onRevisePreview}
            onRetry={onRetryPreview}
            onCancel={onCancelPreview}
          />
        </div>
      );
    }
    if (msg.kind === "error") {
      return (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={16} />
          <div className="callout__body">
            <p className="callout__title">出错了</p>
            <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{msg.content}</p>
          </div>
        </div>
      );
    }
    if (msg.role === "user") {
      return (
        <div className={styles.userRow}>
          <article className={styles.userBubble}>{msg.content}</article>
        </div>
      );
    }
    if (msg.source === "tool" || msg.channel === "stdout" || msg.channel === "stderr") {
      const label = msg.tool || "tool";
      const channel = msg.channel ?? "output";
      return (
        <details className={styles.tool} data-channel={channel}>
          <summary>
            <Icon name="terminal" size={14} />
            <span className={styles.toolSummary}>
              {label} · {channel}
            </span>
            <Icon name="chevronRight" size={14} className={styles.toolChevron} />
          </summary>
          <pre className="code-block">{msg.content}</pre>
        </details>
      );
    }
    if (msg.tool) {
      return (
        <details className={styles.tool}>
          <summary>
            <Icon name="wrench" size={14} />
            <span className={styles.toolName}>{msg.tool}</span>
            <span className={styles.toolPreview}>{oneLine(msg.content)}</span>
            <Icon name="chevronRight" size={14} className={styles.toolChevron} />
          </summary>
          <pre className="code-block">{prettyJson(msg.content)}</pre>
        </details>
      );
    }

    const agent = msg.source === "agent";
    return (
      <article className={styles.athena}>
        {showLabel && (
          <div className={styles.speaker}>
            {agent ? (
              <span className={styles.agentAvatar}>
                <Icon name="llm" size={13} />
              </span>
            ) : (
              <BrandMark size={20} />
            )}
            <span className={styles.speakerName}>{agent ? "Agent" : "Athena"}</span>
            {agent && msg.plan && <span className={styles.speakerMeta}>{msg.plan}</span>}
          </div>
        )}
        <Markdown className={styles.prose}>{msg.content}</Markdown>
      </article>
    );
  },
  (previous, next) => {
    if (previous.msg !== next.msg || previous.showLabel !== next.showLabel) return false;
    if (next.msg.kind !== "intent-preview") return true;
    return (
      previous.onConfirmPreview === next.onConfirmPreview &&
      previous.onRevisePreview === next.onRevisePreview &&
      previous.onRetryPreview === next.onRetryPreview &&
      previous.onCancelPreview === next.onCancelPreview
    );
  },
);

const STICK_THRESHOLD = 80;
/** Long runs produce thousands of rows; render a tail window and page earlier ones in. */
const PAGE_SEGMENTS = 120;
const MAX_RENDERED_SEGMENTS = PAGE_SEGMENTS * 2;

/** Conversation transcript with intent cards, tool blocks, ideator lanes and auto-follow. */
export function MessageList({
  messages,
  onConfirmPreview,
  onRevisePreview,
  onRetryPreview,
  onCancelPreview,
}: MessageListProps) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const lastIdRef = useRef<string | undefined>(undefined);
  const columnRef = useRef<HTMLDivElement | null>(null);
  const userToggleRef = useRef(false);
  const restoreHeightRef = useRef<number | null>(null);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const segments = segmentMessages(messages);

  // Segments hidden above the rendered window. Adjusted during render so a
  // freshly restored 3000-row transcript never mounts in full.
  const [hiddenState, setHidden] = useState(0);
  let hidden = hiddenState;
  if (hidden > segments.length) {
    hidden = Math.max(0, segments.length - PAGE_SEGMENTS);
  } else if (segments.length - hidden > MAX_RENDERED_SEGMENTS && stickRef.current) {
    hidden = segments.length - PAGE_SEGMENTS;
  }
  if (hidden !== hiddenState) setHidden(hidden);
  const rendered = hidden > 0 ? segments.slice(hidden) : segments;

  const onScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickRef.current = distance < STICK_THRESHOLD;
    setShowJumpToBottom(distance > 160);
  }, []);

  // Follow new output while the reader is at the bottom; a new user message always follows.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const last = messages[messages.length - 1];
    if (last && last.id !== lastIdRef.current && last.role === "user") stickRef.current = true;
    lastIdRef.current = last?.id;
    if (stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Keep the reader's place when earlier messages are paged in above it.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el || restoreHeightRef.current === null) return;
    el.scrollTop += el.scrollHeight - restoreHeightRef.current;
    restoreHeightRef.current = null;
  }, [hidden]);

  // Content can grow without a new message (fonts, Markdown, streamed tool
  // output) and the viewport can shrink (the composer dock grows); stay pinned
  // unless the reader just expanded a block themselves.
  useEffect(() => {
    const el = listRef.current;
    const column = columnRef.current;
    if (!el || !column || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (userToggleRef.current) {
        userToggleRef.current = false;
        onScroll();
        return;
      }
      if (stickRef.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(column);
    observer.observe(el);
    return () => observer.disconnect();
  }, [onScroll]);

  useEffect(() => {
    window.addEventListener("resize", onScroll);
    return () => window.removeEventListener("resize", onScroll);
  }, [onScroll]);

  const showEarlier = () => {
    const el = listRef.current;
    if (el) restoreHeightRef.current = el.scrollHeight;
    setHidden(Math.max(0, hidden - PAGE_SEGMENTS));
  };

  const markUserToggle = (event: MouseEvent) => {
    if (!(event.target as HTMLElement).closest("summary")) return;
    userToggleRef.current = true;
    window.setTimeout(() => {
      userToggleRef.current = false;
    }, 250);
  };

  const scrollToBottom = () => {
    const el = listRef.current;
    if (!el) return;
    stickRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  let previousSpeaker: string | null = null;
  const renderItem = (msg: UIMessage) => {
    const speaker = speakerOf(msg);
    const showLabel = speaker !== previousSpeaker;
    previousSpeaker = speaker;
    return (
      <TrajectoryItem
        key={msg.id}
        msg={msg}
        showLabel={showLabel}
        onConfirmPreview={onConfirmPreview}
        onRevisePreview={onRevisePreview}
        onRetryPreview={onRetryPreview}
        onCancelPreview={onCancelPreview}
      />
    );
  };

  return (
    <div className={styles.wrapper}>
      <div
        className={styles.scroller}
        role="log"
        aria-live="polite"
        aria-label="对话记录"
        ref={listRef}
        onScroll={onScroll}
        onClickCapture={markUserToggle}
      >
        <div className={styles.column} ref={columnRef}>
          {hidden > 0 && (
            <button type="button" className={styles.earlier} onClick={showEarlier}>
              <Icon name="chevronUp" size={14} />
              显示更早的消息（还有 {segments.slice(0, hidden).reduce((n, segment) => n + segment.items.length, 0)} 条）
            </button>
          )}
          {rendered.map((segment, offset) => {
            const index = hidden + offset;
            if (segment.kind === "ordinary") {
              // One message per ordinary segment; render it unwrapped so its id
              // key survives the window shifting (no remounts, <details> keep state).
              return renderItem(segment.items[0]);
            }
            if (segment.kind === "clarification") {
              previousSpeaker = null;
              const items = segment.items.map(renderItem);
              previousSpeaker = null;
              return (
                <ActivityGroup
                  key={clarificationSegmentKey(segment)}
                  title="任务理解过程"
                  count={segment.items.length}
                  active
                >
                  {items}
                </ActivityGroup>
              );
            }
            previousSpeaker = null;
            const items = segment.items.map(renderItem);
            previousSpeaker = null;
            return (
              <details key={`ideator-${segment.lane}-${index}`} className={styles.lane} open>
                <summary>
                  <Icon name="lightbulb" size={14} />
                  <span className={styles.laneTitle}>Ideator {segment.lane}</span>
                  <span className={styles.laneCount}>{segment.items.length} 条</span>
                  <Icon name="chevronRight" size={14} className={styles.toolChevron} />
                </summary>
                <div className={styles.laneBody}>{items}</div>
              </details>
            );
          })}
        </div>
      </div>
      {showJumpToBottom && (
        <button
          type="button"
          className={styles.jump}
          onClick={scrollToBottom}
          aria-label="返回底部"
          title="返回底部"
        >
          <Icon name="chevronDown" size={16} />
        </button>
      )}
    </div>
  );
}

/** Collapsible live activity block (task understanding progress). */
function ActivityGroup({
  title,
  count,
  active,
  children,
}: {
  title: string;
  count: number;
  active: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section className={styles.activity} role="log" aria-label={title} aria-live="polite">
      <button
        type="button"
        className={styles.activityHeader}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {active ? <Icon name="loader" size={14} className="icon--spin" /> : <Icon name="sparkles" size={14} />}
        <span className={styles.activityTitle}>{title}</span>
        <span className={styles.laneCount}>{count} 条</span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={14} />
      </button>
      <div className={styles.activityBody} hidden={!open}>
        {children}
      </div>
    </section>
  );
}
