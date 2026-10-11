import { useMemo, useState } from "react";
import type { LogEntry } from "../../hooks/usePipeline";
import { Icon } from "../common/Icon";
import styles from "./LogDrawer.module.css";

interface LogDrawerProps {
  entries: LogEntry[];
  onClose(): void;
  onClear(): void;
}

type Filter = "all" | "output" | "other";

function formatTime(at: number): string {
  const d = new Date(at);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((part) => part.toString().padStart(2, "0"))
    .join(":");
}

function meta(entry: LogEntry): string {
  return [entry.source, entry.channel, entry.plan, entry.tool]
    .filter((v): v is string => Boolean(v))
    .join(" · ");
}

/** Right-hand raw event log: newest first, filterable by event kind. */
export function LogDrawer({ entries, onClose, onClear }: LogDrawerProps) {
  const [filter, setFilter] = useState<Filter>("all");

  const ordered = useMemo(() => {
    const visible = entries.filter((entry) =>
      filter === "all" ? true : filter === "output" ? entry.kind === "output" : entry.kind !== "output",
    );
    return visible.reverse();
  }, [entries, filter]);

  return (
    <aside className={styles.drawer} role="complementary" aria-label="运行日志">
      <header className={styles.header}>
        <h2>运行日志</h2>
        <div className={styles.headerActions}>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onClear}
            disabled={entries.length === 0}
            title="清空日志"
          >
            清空
          </button>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭日志">
            <Icon name="close" size={16} />
          </button>
        </div>
      </header>
      <div className={styles.toolbar}>
        <div className="segmented" role="group" aria-label="筛选事件">
          {(
            [
              ["all", "全部"],
              ["output", "输出"],
              ["other", "状态与交互"],
            ] as const
          ).map(([key, label]) => (
            <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label}
            </button>
          ))}
        </div>
        <span className={styles.count}>{ordered.length} 条</span>
      </div>
      <div className={styles.body}>
        {ordered.length === 0 ? (
          <p className={styles.empty}>暂无日志事件。任务开始后，这里会实时显示后端推送的事件流。</p>
        ) : (
          <ul className={styles.list}>
            {ordered.map((entry) => (
              <li key={entry.id} className={styles.item}>
                <div className={styles.itemHead}>
                  <span className={`${styles.kind} ${entry.kind === "output" ? "" : styles.kindState}`}>
                    {entry.kind}
                  </span>
                  <span className={styles.time}>{formatTime(entry.at)}</span>
                  <span className={styles.meta}>{meta(entry)}</span>
                </div>
                {entry.text && <pre className={styles.text}>{entry.text}</pre>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
