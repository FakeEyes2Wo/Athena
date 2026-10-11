import { Button, Heading, IconButton, Label } from "@primer/react";
import { XIcon } from "@primer/octicons-react";
import type { LogEntry } from "../../hooks/usePipeline";
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

/** 右侧运行日志侧栏：按时间倒序展示后端推送的 state/output 原始事件。 */
export function LogDrawer({ entries, onClose, onClear }: LogDrawerProps) {
  const ordered = [...entries].reverse();

  return (
    <aside className={styles.drawer} role="complementary" aria-label="运行日志">
      <header className={styles.header}>
        <Heading as="h2" variant="small">运行日志</Heading>
        <div className={styles.headerActions}>
          <Button size="small" variant="invisible" onClick={onClear}>
            清空
          </Button>
          <IconButton icon={XIcon} variant="invisible" size="small" aria-label="关闭日志" onClick={onClose} />
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
                  <Label variant={entry.kind === "state" ? "accent" : "secondary"} size="small">
                    {entry.kind}
                  </Label>
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
