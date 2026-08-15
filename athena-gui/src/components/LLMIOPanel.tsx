import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  traceGet,
  tracesList,
  type TraceDetail,
  type TraceMessage,
  type TraceRole,
  type TraceSummary,
} from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import styles from "./LLMIOPanel.module.css";

/** Role → badge variant + label mapping for the trace timeline. */
const ROLE_META: Record<TraceRole, { badge: string; label: string }> = {
  system: { badge: "badge--neutral", label: "system" },
  user: { badge: "badge--info", label: "user" },
  assistant: { badge: "badge--success", label: "assistant" },
  tool: { badge: "badge--warning", label: "tool" },
};

function formatUpdatedAt(mtime: number): string {
  if (!mtime) return "—";
  return new Date(mtime * 1000).toLocaleString();
}

function TraceBubble({ message }: { message: TraceMessage }) {
  const meta = ROLE_META[message.role] ?? ROLE_META.system;
  const isToolCall = message.kind === "tool_call";
  const isToolReturn = message.kind === "tool_return";
  const argsJson = message.args ? JSON.stringify(message.args, null, 2) : null;

  return (
    <div className={styles["trace-bubble"]}>
      <div className={styles["trace-bubble__head"]}>
        <span className={`badge ${meta.badge}`}>{meta.label}</span>
        <span className={styles["trace-bubble__seq"]}>seq {message.seq}</span>
      </div>

      {isToolCall ? (
        <details style={{ cursor: "pointer" }}>
          <summary style={{ color: "var(--text-primary)", fontWeight: 500 }}>{message.content}</summary>
          {argsJson && <pre className="code-block">{argsJson}</pre>}
        </details>
      ) : isToolReturn ? (
        <details style={{ cursor: "pointer" }}>
          <summary style={{ color: "var(--text-primary)", fontWeight: 500 }}>工具返回</summary>
          <pre className="code-block">{message.content}</pre>
        </details>
      ) : (
        <div className={styles["trace-bubble__body"]}>{message.content}</div>
      )}
    </div>
  );
}

/** LLM I/O trace browser: agent list + role-colored message timeline. */
export default function LLMIOPanel() {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await tracesList();
      setTraces(res.traces);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    traceGet(selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  return (
    <div className="detail-panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div className="panel-toolbar">
        <h3>LLM 输入 / 输出轨迹 ({traces.length})</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()}>刷新</button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {traces.length === 0 ? (
        <EmptyState icon="llm" message="暂无 agent 轨迹。运行搜索产生 rollout 后此处可回放 LLM I/O。" />
      ) : (
        <div className={styles["trace-layout"]}>
          <ul className={styles["trace-list"]}>
            {traces.map((trace) => (
              <li key={trace.agent_id}>
                <button
                  className={`${styles["trace-list__item"]}${trace.agent_id === selectedId ? ` ${styles["trace-list__item--active"]}` : ""}`}
                  onClick={() => setSelectedId(trace.agent_id)}
                >
                  <span className={styles["trace-list__item-title"]}>{trace.agent_id}</span>
                  <span className={styles["trace-list__item-meta"]}>
                    {trace.messages} 条 · {formatUpdatedAt(trace.updated_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <div className={styles["trace-pane"]}>
            {loading && <p style={{ color: "var(--text-secondary)" }}>加载中…</p>}
            {!loading && !detail && <p style={{ color: "var(--text-secondary)" }}>选择一个 agent 查看轨迹。</p>}
            {detail && detail.messages.length === 0 && (
              <p style={{ color: "var(--text-secondary)" }}>该 agent 暂无消息。</p>
            )}
            {detail?.messages.map((message, index) => (
              <TraceBubble key={`${message.seq}-${index}`} message={message} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
