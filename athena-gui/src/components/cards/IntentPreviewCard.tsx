import { useEffect, useId, useState } from "react";
import type { ClarificationPreview } from "../../types/ui";
import { DIRECTION_LABEL, taskTypeLabel } from "../../lib/labels";
import { Icon } from "../common/Icon";
import styles from "./IntentPreviewCard.module.css";

interface IntentPreviewCardProps {
  preview: ClarificationPreview;
  onConfirm(acknowledgeUnresolved: boolean): Promise<void> | void;
  onRevise(instruction: string): Promise<void> | void;
  onRetry(): Promise<void> | void;
  onCancel(): Promise<void> | void;
  /** True when a confirmation/revise/retry/cancel is already in flight. */
  busy?: boolean;
}

const MAX_REVISION_LENGTH = 4000;

const STATUS_META: Record<string, { label: string; tone: string }> = {
  CLARIFYING: { label: "理解中", tone: "warning" },
  READY_FOR_CONFIRMATION: { label: "待确认", tone: "accent" },
  CONFIRMING: { label: "启动中", tone: "warning" },
  RUNNING: { label: "已启动", tone: "success" },
  FAILED: { label: "失败", tone: "danger" },
  IDLE: { label: "已取消", tone: "neutral" },
};

/** Understanding fields as the backend names them in ``unresolved``. */
const FIELD_LABEL: Record<string, string> = {
  title: "任务名称",
  dataset: "数据集",
  target: "目标",
  task_type: "任务类型",
  primary_metric: "主指标",
  direction: "优化方向",
  evaluation_plan: "评估方案",
};

const REASON_LABEL: Record<string, string> = {
  "not supplied": "未提供",
  "not determined": "未确定",
};

/** Actionable explanations for known clarification failure codes. */
const FAILURE_HINT: Record<string, string> = {
  provider_failure: "无法调用模型服务。请在「设置 → 模型连接」中检查服务商、API Key 与 Base URL，然后重试。",
};

const OUTCOME_LABEL: Record<string, string> = {
  skip: "已跳过",
  timeout: "超时未答",
  cancelled: "已取消",
};

function Field({ label, value, wide }: { label: string; value: string | null; wide?: boolean }) {
  return (
    <div className={`${styles.field}${wide ? ` ${styles.fieldWide}` : ""}`}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{value?.trim() ? value : "—"}</span>
    </div>
  );
}

/** Task-understanding card: the decision gate before research (PREPARE) starts. */
export function IntentPreviewCard({
  preview,
  onConfirm,
  onRevise,
  onRetry,
  onCancel,
  busy = false,
}: IntentPreviewCardProps) {
  const [revisionText, setRevisionText] = useState("");
  const [revising, setRevising] = useState(false);
  const [acknowledgeUnresolved, setAcknowledgeUnresolved] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const reviseId = useId();

  const pending = busy || pendingAction !== null;
  const hasCriticalUnresolved = preview.unresolved.some((item) => item.critical);
  const confirmationDisabled = pending || (hasCriticalUnresolved && !acknowledgeUnresolved);
  const revisionTextInvalid = !revisionText.trim() || revisionText.length > MAX_REVISION_LENGTH;

  // Local gate state is per displayed revision; reset when the revision changes.
  useEffect(() => {
    setRevisionText("");
    setRevising(false);
    setAcknowledgeUnresolved(false);
    setPendingAction(null);
  }, [preview.draftId, preview.revision]);

  const runAction = async (name: string, action: () => Promise<void> | void) => {
    setPendingAction(name);
    try {
      await action();
    } catch {
      // The pipeline reports failures in the conversation; keep the card usable.
    } finally {
      setPendingAction(null);
    }
  };

  const handleRevise = () => {
    const instruction = revisionText.trim();
    if (!instruction || instruction.length > MAX_REVISION_LENGTH) return;
    void runAction("revise", async () => {
      await onRevise(instruction);
      setRevisionText("");
      setRevising(false);
    });
  };

  const { understanding } = preview;
  const isReady = preview.status === "READY_FOR_CONFIRMATION";
  const isFailed = preview.status === "FAILED";
  const isRunning = preview.status === "RUNNING";
  const isCancelled = preview.status === "IDLE";
  const isConfirming = preview.status === "CONFIRMING" || pendingAction === "confirm";
  // While clarifying (or after a provider failure) every field is trivially
  // "unresolved"; the list only matters once there is something to confirm.
  const showUnresolved = preview.unresolved.length > 0 && !isFailed && preview.status !== "CLARIFYING";
  const understood = Boolean(
    understanding.dataset || understanding.target || understanding.primary_metric || understanding.evaluation_plan,
  );
  const failureHint = preview.failure ? FAILURE_HINT[preview.failure.code] : undefined;
  const status = STATUS_META[preview.status] ?? { label: preview.status, tone: "neutral" };
  const metric =
    understanding.primary_metric || understanding.direction
      ? `${understanding.primary_metric ?? "—"} · ${
          understanding.direction ? DIRECTION_LABEL[understanding.direction] ?? understanding.direction : "—"
        }`
      : null;

  return (
    <section
      className={`${styles.card}${isRunning ? ` ${styles.cardStarted}` : ""}${isCancelled ? ` ${styles.cardMuted}` : ""}`}
      aria-label="任务理解"
    >
      <header className={styles.header}>
        <span className={styles.icon}>
          <Icon name="target" size={16} />
        </span>
        <div className={styles.heading}>
          <span className={styles.kicker}>任务理解</span>
          <span className={styles.title}>{understanding.title?.trim() || "未命名任务"}</span>
        </div>
        <span className={`badge badge--${status.tone}`}>{status.label}</span>
      </header>

      {isReady && <p className={styles.lead}>请核对 Athena 对任务的理解，确认无误后开始研究。</p>}

      {(understood || !isFailed) && (
        <div className={styles.grid}>
          <Field label="数据集" value={understanding.dataset} />
          <Field label="目标" value={understanding.target} />
          <Field label="任务类型" value={taskTypeLabel(understanding.task_type)} />
          <Field label="主指标" value={metric} />
          <Field label="评估方案" value={understanding.evaluation_plan} wide />
        </div>
      )}

      {preview.answers.length > 0 && (
        <details className={styles.qa}>
          <summary>
            <Icon name="chevronRight" size={14} className={styles.chevron} />
            澄清问答（{preview.answers.length}）
          </summary>
          <div className={styles.qaList}>
            {preview.answers.map((answer) => (
              <div key={answer.request_id} className={styles.qaRow}>
                <span className={styles.question}>{answer.question}</span>
                <span className={styles.answer}>
                  {OUTCOME_LABEL[answer.outcome] ?? answer.choice_label ?? answer.value ?? ""}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}

      {showUnresolved && (
        <div className="callout callout--warning">
          <Icon name="warning" size={15} />
          <div className="callout__body">
            <p className="callout__title">尚未确定的信息</p>
            <ul className={styles.unresolvedList}>
              {preview.unresolved.map((item) => (
                <li key={`${item.field}-${item.reason}`}>
                  <span className={styles.unresolvedField} title={item.field}>
                    {FIELD_LABEL[item.field] ?? item.field}
                  </span>
                  {item.critical && <span className="badge badge--danger">关键</span>}
                  <span className={styles.unresolvedReason}>{REASON_LABEL[item.reason] ?? item.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {isFailed && preview.failure && (
        <div className="callout callout--error">
          <Icon name="alert" size={15} />
          <div className="callout__body">
            <p className="callout__title">任务理解失败</p>
            <p style={{ margin: 0 }}>{failureHint ?? preview.failure.message}</p>
            <p className={styles.failureCode}>
              {preview.failure.code}
              {failureHint && preview.failure.message ? ` · ${preview.failure.message}` : ""}
            </p>
          </div>
        </div>
      )}

      {isReady && revising && (
        <div className={styles.revise}>
          <label className={styles.reviseLabel} htmlFor={reviseId}>
            修改意见
          </label>
          <textarea
            id={reviseId}
            className="textarea"
            value={revisionText}
            onChange={(event) => setRevisionText(event.target.value)}
            maxLength={MAX_REVISION_LENGTH}
            rows={3}
            autoFocus
            placeholder="例如：目标列应为 churned，主指标改为 AUC"
          />
          <div className={styles.reviseActions}>
            <span className={styles.hint}>Athena 会根据你的意见重新理解任务</span>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setRevising(false)} disabled={pending}>
              收起
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={handleRevise}
              disabled={pending || revisionTextInvalid}
            >
              提交修改
            </button>
          </div>
        </div>
      )}

      <footer className={styles.footer}>
        <span className={styles.meta}>版本 {preview.revision}</span>
        {isRunning ? (
          <span className={styles.started}>
            <Icon name="success" size={14} /> 已确认，研究已启动
          </span>
        ) : isConfirming ? (
          <span className={styles.working}>
            <Icon name="loader" size={14} className="icon--spin" /> 正在启动研究…
          </span>
        ) : pending ? (
          <span className={styles.working}>
            <Icon name="loader" size={14} className="icon--spin" /> 正在处理…
          </span>
        ) : isCancelled ? (
          <span className={styles.meta}>已取消，可以在下方输入新的任务描述</span>
        ) : null}
      </footer>

      {isFailed && (
        <div className={styles.actions}>
          <button type="button" className="btn btn--ghost" onClick={() => void runAction("cancel", onCancel)} disabled={pending}>
            取消
          </button>
          <button type="button" className="btn btn--primary" onClick={() => void runAction("retry", onRetry)} disabled={pending}>
            <Icon name="retry" size={14} /> 重试
          </button>
        </div>
      )}

      {isReady && (
        <div className={styles.actions}>
          {hasCriticalUnresolved && (
            <label className={styles.acknowledge}>
              <input
                type="checkbox"
                checked={acknowledgeUnresolved}
                onChange={(event) => setAcknowledgeUnresolved(event.target.checked)}
                disabled={pending}
              />
              <span>我已了解未确定项，仍然开始</span>
            </label>
          )}
          <button type="button" className="btn btn--ghost" onClick={() => void runAction("cancel", onCancel)} disabled={pending}>
            取消
          </button>
          {!revising && (
            <button type="button" className="btn" onClick={() => setRevising(true)} disabled={pending}>
              <Icon name="pencil" size={14} /> 修改
            </button>
          )}
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void runAction("confirm", () => onConfirm(acknowledgeUnresolved))}
            disabled={confirmationDisabled}
          >
            {isConfirming ? "启动中…" : "确认并开始研究"}
          </button>
        </div>
      )}
    </section>
  );
}
