import type { LogEntry } from "../../hooks/usePipeline";
import { Icon } from "../common/Icon";
import styles from "./LogDrawer.module.css";

interface LogDrawerProps {
  entries: LogEntry[];
  onClose(): void;
  onClear(): void;
}

function formatTime(at: number): string {
  const d = new Date(at);
  return `${d.getHours().toString().padStart(2, "0")}:${d
    .getMinutes()
    .toString()
    .padStart(2, "0")}:${d.getSeconds().toString().padStart(2, "0")}`;
}

function meta(entry: LogEntry): string {
  const parts = [entry.source, entry.channel, entry.plan, entry.tool].filter(
    (v): v is string => Boolean(v),
  );
  return parts.length ? parts.join(" · ") : "";
}

/** 右侧运行日志抽屉：按时间倒序展示后端推送的 state/output 原始事件。 */
export function LogDrawer({ entries, onClose, onClear }: LogDrawerProps) {
  const ordered = [...entries].reverse();

  return (
    <aside className={styles.drawer} role="complementary" aria-label="运行日志">
      <header className={styles.header}>
        <h2>运行日志</h2>
        <div className={styles.headerActions}>
          <button type="button" className={styles.clear} onClick={onClear} title="清空日志">
            清空
          </button>
          <button type="button" className={styles.close} onClick={onClose} aria-label="关闭日志">
            <Icon name="close" size={16} />
          </button>
        </div>
      </header>
      <div className={styles.body}>
        {ordered.length === 0 ? (
          <p className={styles.empty}>暂无日志事件。启动任务后这里会显示 state/output 流。</p>
        ) : (
          <ul className={styles.list}>
            {ordered.map((entry) => (
              <li key={entry.id} className={styles.item}>
                <div className={styles.itemHead}>
                  <span className={`${styles.kind} ${entry.kind === "state" ? styles.kindState : ""}`}>
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
