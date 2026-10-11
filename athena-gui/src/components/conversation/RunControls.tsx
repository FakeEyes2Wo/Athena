import type { PipelineViewModel } from "../../types/ui";
import { formatMetric, phaseLabel, statusLabel } from "../../lib/labels";
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

/** Run status chip, progress summary, hypothesis mode and pause/continue/stop controls. */
export function RunControls({ viewModel, active, onPause, onResume, onStop, onToggleMode }: RunControlsProps) {
  const { phase, status, manual, rightRail } = viewModel;
  const running = status === "running" && active;
  const paused = status === "paused" && active;
  const resumable = paused || viewModel.resumeAvailable;
  const terminal = status === "idle" || status === "completed" || status === "error" || !active;
  const attempts = rightRail.searchAttempts;
  const limit = rightRail.searchLimit;

  return (
    <div className={styles.bar}>
      <span className={styles.chip} data-status={status} title={`${phase || "idle"} · ${status}`}>
        {running ? <Icon name="loader" size={13} className="icon--spin" /> : <span className={styles.dot} />}
        <strong>{phaseLabel(phase)}</strong>
        <span className={styles.sep}>·</span>
        {statusLabel(status)}
      </span>

      <span className={styles.metrics}>
        {(attempts > 0 || limit > 0) && (
          <span title="搜索尝试次数 / 上限">
            尝试 <b>{attempts}</b>
            {limit > 0 ? ` / ${limit}` : ""}
          </span>
        )}
        <span title="当前最佳主指标">
          最佳 <b className={rightRail.bestPrimary != null ? styles.best : undefined}>{formatMetric(rightRail.bestPrimary)}</b>
        </span>
      </span>

      <button
        type="button"
        className={styles.mode}
        onClick={onToggleMode}
        aria-pressed={manual}
        title={manual ? "手动选择假设：点击切换为自动" : "自动选择假设：点击切换为手动"}
      >
        {manual ? "手动" : "自动"}
      </button>

      <div className={styles.actions}>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onPause} disabled={!running} title="暂停研究">
          <Icon name="pause" size={13} /> 暂停
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onResume} disabled={!resumable} title="继续研究">
          <Icon name="play" size={13} /> 继续
        </button>
        <button
          type="button"
          className="btn btn--danger-ghost btn--sm"
          onClick={onStop}
          disabled={terminal}
          title="停止研究"
        >
          <Icon name="stop" size={13} /> 停止
        </button>
      </div>
    </div>
  );
}
