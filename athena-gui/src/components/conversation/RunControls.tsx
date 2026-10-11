import { Button, SegmentedControl, Spinner } from "@primer/react";
import type { PipelineViewModel } from "../../types/ui";
import { Icon } from "../common/Icon";
import styles from "./RunControls.module.css";

interface RunControlsProps {
  viewModel: PipelineViewModel;
  /** True when a real run has started or produced plans/attempts/SOTA activity. */
  active: boolean;
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
export function RunControls({ viewModel, active, onPause, onResume, onStop, onToggleMode }: RunControlsProps) {
  const { phase, status, manual, rightRail } = viewModel;
  const running = status === "running" && active;
  const paused = status === "paused" && active;
  const resumable = paused || viewModel.resumeAvailable;
  const terminal = status === "idle" || status === "completed" || status === "error" || !active;
  const best = rightRail.bestPrimary != null ? rightRail.bestPrimary.toFixed(4) : "--";

  return (
    <div className={styles.bar}>
      <div className={styles.status}>
        {running ? <Spinner size="small" srText={null} /> : <Icon name="sparkles" size={14} />}
        <strong className={styles.phase}>{phase || "idle"}</strong>
        <span className={styles.muted}>· {STATUS_LABEL[status] ?? status} ·</span>
        <SegmentedControl
          aria-label="假设选择模式"
          size="small"
          onChange={(index) => {
            // 只在切到另一种模式时通知，重复点击当前模式不触发切换。
            if ((index === 1) !== manual) onToggleMode();
          }}
        >
          <SegmentedControl.Button selected={!manual}>自动</SegmentedControl.Button>
          <SegmentedControl.Button selected={manual}>手动</SegmentedControl.Button>
        </SegmentedControl>
        <span className={styles.muted}>
          预算 {rightRail.budgetRemaining} · 最佳 {best}
        </span>
      </div>
      <div className={styles.actions}>
        <Button size="small" onClick={onPause} disabled={!running}>
          暂停
        </Button>
        <Button size="small" onClick={onResume} disabled={!resumable}>
          继续
        </Button>
        <Button size="small" variant="danger" onClick={onStop} disabled={terminal}>
          停止
        </Button>
      </div>
    </div>
  );
}
