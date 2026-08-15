import type { TaskUnderstanding } from "../../lib/tauri-bridge";
import { Icon } from "../common/Icon";
import styles from "./IntentPreviewCard.module.css";

interface IntentPreviewCardProps {
  preview: TaskUnderstanding;
  /** True after the user confirmed and started the run. */
  started: boolean;
  onConfirm(): Promise<void>;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{value || "—"}</span>
    </div>
  );
}

/** Supervisor-style task understanding card; transitions to a "started" state after confirm. */
export function IntentPreviewCard({ preview, started, onConfirm }: IntentPreviewCardProps) {
  return (
    <section className={`${styles.card}${started ? ` ${styles.cardStarted}` : ""}`}>
      <header className={styles.header}>
        <Icon name="sparkles" size={15} />
        <span className={styles.title}>{preview.title || "任务理解"}</span>
        {started && <span className={styles.badge}>已启动</span>}
      </header>

      <div className={styles.grid}>
        <Field label="数据集" value={preview.dataset} />
        <Field label="目标" value={preview.target} />
        <Field label="任务类型" value={preview.task_type} />
        <Field label="主指标" value={`${preview.primary_metric} · ${preview.direction}`} />
        <Field label="评估方案" value={preview.evaluation_plan} />
      </div>

      {preview.needs_configuration && !started && (
        <p className={styles.hint}>建议在启动前到「设置」确认并发度 / 方向 / 容忍度。</p>
      )}

      <footer className={styles.footer}>
        {started ? (
          <span className={styles.running}>
            <Icon name="loader" size={13} className="icon--spin" /> 任务运行中…
          </span>
        ) : (
          <button className="btn btn--primary" onClick={() => void onConfirm()}>
            确认并启动
          </button>
        )}
      </footer>
    </section>
  );
}
