import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  traceGet,
  tracesList,
  type TraceDetail,
  type TraceMessage,
  type TraceRole,
} from "../lib/tauri-bridge";
import { useRemoteData } from "../hooks/useRemoteData";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import styles from "./LLMIOPanel.module.css";

const ROLE_META: Record<TraceRole, { tone: string; label: string }> = {
  system: { tone: "neutral", label: "系统" },
  user: { tone: "info", label: "输入" },
  assistant: { tone: "accent", label: "模型" },
  tool: { tone: "warning", label: "工具" },
};

const LONG_TEXT = 1200;

function formatUpdatedAt(mtime: number): string {
  if (!mtime) return "—";
  const date = new Date(mtime * 1000);
  return date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function preview(text: string, max = 96): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** One-line hint of what a tool was asked to do (the shell command when there is one). */
function argsPreview(args: unknown): string {
  if (args == null) return "";
  if (typeof args === "string") return preview(args);
  if (typeof args === "object") {
    const record = args as Record<string, unknown>;
    for (const key of ["command", "path", "query"]) {
      if (typeof record[key] === "string") return preview(record[key] as string);
    }
  }
  return preview(JSON.stringify(args));
}

function TraceBubble({ message }: { message: TraceMessage }) {
  const meta = ROLE_META[message.role] ?? ROLE_META.system;
  const isToolCall = message.kind === "tool_call";
  const isToolReturn = message.kind === "tool_return";
  const collapsible = isToolCall || isToolReturn || message.role === "system" || message.content.length > LONG_TEXT;
  const argsJson =
    message.args == null ? null : typeof message.args === "string" ? message.args : JSON.stringify(message.args, null, 2);

  const head = (
    <span className={styles.head}>
      <span className={`badge badge--${meta.tone}`}>{meta.label}</span>
      {isToolCall && <span className={styles.toolName}>{message.content}</span>}
      {isToolCall && <span className={`${styles.summary} ${styles.mono}`}>{argsPreview(message.args)}</span>}
      {isToolReturn && <span className={`${styles.summary} ${styles.mono}`}>{preview(message.content) || "（空）"}</span>}
      {!isToolCall && !isToolReturn && collapsible && <span className={styles.summary}>{preview(message.content, 80)}</span>}
      <span className={styles.seq}>#{message.seq}</span>
    </span>
  );

  if (collapsible) {
    return (
      <details className={styles.bubble} data-role={message.role}>
        <summary>
          {head}
          <Icon name="chevronRight" size={14} className={styles.chevron} />
        </summary>
        {isToolCall ? (
          argsJson && <pre className="code-block">{argsJson}</pre>
        ) : (
          <pre className={styles.body}>{message.content}</pre>
        )}
      </details>
    );
  }

  return (
    <div className={styles.bubble} data-role={message.role}>
      <div className={styles.staticHead}>{head}</div>
      <div className={styles.body}>{message.content}</div>
    </div>
  );
}

/** LLM I/O trace browser: agent list + role-coded message timeline. */
export default function LLMIOPanel() {
  const { data, error, loading, refresh } = useRemoteData(() => tracesList().then((res) => res.traces), {
    follow: "phase",
  });
  const traces = useMemo(() => [...(data ?? [])].sort((a, b) => b.updated_at - a.updated_at), [data]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const visible = traces.filter((trace) => trace.agent_id.toLowerCase().includes(query.trim().toLowerCase()));

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    traceGet(selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setDetailError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  if (traces.length === 0) {
    return (
      <div className="detail-panel">
        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}
        <EmptyState
          icon={loading ? "loader" : "llm"}
          title={loading ? undefined : "暂无模型轨迹"}
          message={loading ? "加载中…" : "研究运行后，每个 Agent 与模型之间的完整输入输出都会记录在这里，便于审查与排查。"}
          action={
            !loading && (
              <button type="button" className="btn btn--sm" onClick={() => void refresh()}>
                <Icon name="refresh" size={13} /> 刷新
              </button>
            )
          }
        />
      </div>
    );
  }

  return (
    <div className={styles.layout}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarHead}>
          <div className={styles.search}>
            <Icon name="search" size={14} />
            <input
              type="search"
              placeholder="筛选 Agent"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="筛选 Agent"
            />
          </div>
          <button
            type="button"
            className="icon-btn icon-btn--sm"
            onClick={() => void refresh()}
            aria-label="刷新"
            title="刷新"
          >
            <Icon name="refresh" size={14} className={loading ? "icon--spin" : undefined} />
          </button>
        </div>
        <ul className={styles.list}>
          {visible.map((trace) => (
            <li key={trace.agent_id}>
              <button
                type="button"
                className={styles.item}
                aria-current={trace.agent_id === selectedId ? "true" : undefined}
                onClick={() => setSelectedId(trace.agent_id)}
              >
                <span className={styles.itemTitle}>{trace.agent_id}</span>
                <span className={styles.itemMeta}>
                  {trace.messages} 条 · {formatSize(trace.size_bytes)} · {formatUpdatedAt(trace.updated_at)}
                </span>
              </button>
            </li>
          ))}
          {visible.length === 0 && <li className={styles.none}>没有匹配的 Agent</li>}
        </ul>
      </aside>

      <section className={styles.timeline} aria-label="轨迹详情">
        {detailError && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{detailError}</div>
          </div>
        )}
        {!selectedId ? (
          <EmptyState icon="llm" message="从左侧选择一个 Agent，查看它与模型之间的完整对话。" />
        ) : detailLoading && !detail ? (
          <EmptyState icon="loader" message="加载轨迹中…" />
        ) : detail && detail.messages.length === 0 ? (
          <EmptyState icon="llm" message="该 Agent 暂无消息。" />
        ) : (
          <>
            {detail && (
              <div className={styles.timelineHead}>
                <span className={styles.timelineTitle}>{detail.agent_id}</span>
                <span className={styles.itemMeta}>{detail.messages.length} 条消息</span>
              </div>
            )}
            {detail?.messages.map((message, index) => (
              <TraceBubble key={`${message.seq}-${index}`} message={message} />
            ))}
          </>
        )}
      </section>
    </div>
  );
}
