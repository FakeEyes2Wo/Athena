import { useCallback, useEffect, useState } from "react";
import { ActionList, Button, Flash, Label, type LabelColorOptions } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  traceGet,
  tracesList,
  type TraceDetail,
  type TraceMessage,
  type TraceRole,
  type TraceSummary,
} from "../lib/tauri-bridge";
import { CodeBlock } from "./common/CodeBlock";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./LLMIOPanel.module.css";

/** 消息角色 → Primer Label 配色。 */
const ROLE_VARIANT: Record<TraceRole, LabelColorOptions> = {
  system: "secondary",
  user: "accent",
  assistant: "success",
  tool: "attention",
};

function formatUpdatedAt(mtime: number): string {
  if (!mtime) return "—";
  return new Date(mtime * 1000).toLocaleString();
}

function TraceBubble({ message }: { message: TraceMessage }) {
  const isToolCall = message.kind === "tool_call";
  const isToolReturn = message.kind === "tool_return";
  const argsJson = message.args ? JSON.stringify(message.args, null, 2) : null;

  return (
    <div className={styles.bubble}>
      <div className={styles.bubbleHead}>
        <Label variant={ROLE_VARIANT[message.role] ?? "secondary"}>{message.role}</Label>
        <span className={styles.seq}>seq {message.seq}</span>
      </div>

      {isToolCall ? (
        <details className={styles.details}>
          <summary>{message.content}</summary>
          {argsJson && <CodeBlock>{argsJson}</CodeBlock>}
        </details>
      ) : isToolReturn ? (
        <details className={styles.details}>
          <summary>工具返回</summary>
          <CodeBlock>{message.content}</CodeBlock>
        </details>
      ) : (
        <div className={styles.bubbleBody}>{message.content}</div>
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
    <Panel
      fill
      title={`LLM 输入 / 输出轨迹 (${traces.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {traces.length === 0 ? (
        <EmptyState icon="llm" message="暂无 agent 轨迹。运行搜索产生 rollout 后此处可回放 LLM I/O。" />
      ) : (
        <div className={styles.layout}>
          <div className={styles.list}>
            <ActionList aria-label="Agent 轨迹">
              {traces.map((trace) => (
                <ActionList.Item
                  key={trace.agent_id}
                  active={trace.agent_id === selectedId}
                  onSelect={() => setSelectedId(trace.agent_id)}
                >
                  <span className={styles.agentId}>{trace.agent_id}</span>
                  <ActionList.Description variant="block">
                    {trace.messages} 条 · {formatUpdatedAt(trace.updated_at)}
                  </ActionList.Description>
                </ActionList.Item>
              ))}
            </ActionList>
          </div>

          <div className={styles.pane}>
            {loading && <p className={styles.muted}>加载中…</p>}
            {!loading && !detail && <p className={styles.muted}>选择一个 agent 查看轨迹。</p>}
            {detail && detail.messages.length === 0 && <p className={styles.muted}>该 agent 暂无消息。</p>}
            {detail?.messages.map((message, index) => (
              <TraceBubble key={`${message.seq}-${index}`} message={message} />
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}
