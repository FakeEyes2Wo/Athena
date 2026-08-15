import type { PipelineViewModel } from "../../types/ui";
import { Icon } from "../common/Icon";
import styles from "./RunControls.module.css";

interface RunControlsProps {
  viewModel: PipelineViewModel;
  onPause(): void;
  onResume(): void;
  onStop(): void;
  onToggleMode(): void;
}

const STATUS_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "暂停",
  completed: "已完成",
  error: "错误",
};

/** Phase/status + runtime controls bar (pause/resume/stop + auto/manual). */
export function RunControls({ viewModel, onPause, onResume, onStop, onToggleMode }: RunControlsProps) {
  const { phase, status, manual, rightRail } = viewModel;
  const running = status === "running";
  const paused = status === "paused";
  const terminal = status === "idle" || status === "completed" || status === "error";
  const best = rightRail.bestPrimary != null ? rightRail.bestPrimary.toFixed(4) : "--";

  return (
    <div className={styles.bar}>
      <div className={styles.status}>
        {running ? (
          <Icon name="loader" size={14} className="icon--spin" />
        ) : (
          <Icon name="sparkles" size={14} />
        )}
        <strong className={styles.phase}>{phase || "idle"}</strong>
        <span className={styles.muted}>· {STATUS_LABEL[status] ?? status} ·</span>
        <button
          className={`${styles.mode}${manual ? ` ${styles.modeActive}` : ""}`}
          onClick={onToggleMode}
          title="切换自动/手动假设选择"
        >
          {manual ? "手动" : "自动"}
        </button>
        <span className={styles.muted}>
          预算 {rightRail.budgetRemaining} · 最佳 {best}
        </span>
      </div>
      <div className={styles.actions}>
        <button className="btn btn--sm" onClick={onPause} disabled={!running} title="暂停搜索">
          暂停
        </button>
        <button className="btn btn--sm" onClick={onResume} disabled={!paused} title="继续搜索">
          继续
        </button>
        <button className="btn btn--sm btn--ghost" onClick={onStop} disabled={terminal} title="停止研究">
          停止
        </button>
      </div>
    </div>
  );
}
