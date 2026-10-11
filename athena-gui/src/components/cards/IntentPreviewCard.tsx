import { useEffect, useState } from "react";
import { Button, Checkbox, FormControl, Label, Spinner, Textarea, type LabelColorOptions } from "@primer/react";
import type { ClarificationPreview } from "../../types/ui";
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
const READY = "READY_FOR_CONFIRMATION";
const FAILED = "FAILED";

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{value?.trim() ? value : "—"}</span>
    </div>
  );
}

/** 澄清草稿状态 → Primer Label 配色。 */
const STATUS_VARIANT: Record<string, LabelColorOptions> = {
  IDLE: "secondary",
  CLARIFYING: "attention",
  READY_FOR_CONFIRMATION: "accent",
  CONFIRMING: "accent",
  RUNNING: "success",
  FAILED: "danger",
};

function StatusLabel({ status }: { status: string }) {
  return <Label variant={STATUS_VARIANT[status] ?? "secondary"}>{status.replace(/_/g, " ")}</Label>;
}

/** Supervisor-style task understanding card; the decision gate before PREPARE. */
export function IntentPreviewCard({
  preview,
  onConfirm,
  onRevise,
  onRetry,
  onCancel,
  busy = false,
}: IntentPreviewCardProps) {
  const [revisionText, setRevisionText] = useState("");
  const [acknowledgeUnresolved, setAcknowledgeUnresolved] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  const pending = busy || pendingAction !== null;
  const hasCriticalUnresolved = preview.unresolved.some((item) => item.critical);
  const confirmationDisabled = pending || (hasCriticalUnresolved && !acknowledgeUnresolved);
  const revisionTextInvalid = !revisionText.trim() || revisionText.length > MAX_REVISION_LENGTH;

  // Local gate state is per displayed revision; reset when the revision changes.
  useEffect(() => {
    setRevisionText("");
    setAcknowledgeUnresolved(false);
    setPendingAction(null);
  }, [preview.draftId, preview.revision]);

  const runAction = async (name: string, action: () => Promise<void> | void) => {
    setPendingAction(name);
    try {
      await action();
    } finally {
      setPendingAction(null);
    }
  };

  const handleConfirm = () => {
    void runAction("confirm", () => onConfirm(acknowledgeUnresolved));
  };

  const handleRevise = () => {
    const instruction = revisionText.trim();
    if (!instruction || instruction.length > MAX_REVISION_LENGTH) return;
    void runAction("revise", async () => {
      await onRevise(instruction);
      setRevisionText("");
    });
  };

  const handleRetry = () => {
    void runAction("retry", () => onRetry());
  };

  const handleCancel = () => {
    void runAction("cancel", () => onCancel());
  };

  const isReady = preview.status === READY;
  const isFailed = preview.status === FAILED;
  const isRunning = preview.status === "RUNNING";
  const isConfirming = preview.status === "CONFIRMING" || pendingAction === "confirm";

  return (
    <section className={`${styles.card}${isRunning ? ` ${styles.cardStarted}` : ""}`}>
      <header className={styles.header}>
        <Icon name="sparkles" size={15} />
        <span className={styles.title}>{preview.understanding.title?.trim() || "任务理解"}</span>
        <StatusLabel status={preview.status} />
      </header>

      <div className={styles.grid}>
        <Field label="数据集" value={preview.understanding.dataset} />
        <Field label="目标" value={preview.understanding.target} />
        <Field label="任务类型" value={preview.understanding.task_type} />
        <Field
          label="主指标"
          value={
            preview.understanding.primary_metric || preview.understanding.direction
              ? `${preview.understanding.primary_metric ?? "—"} · ${preview.understanding.direction ?? "—"}`
              : null
          }
        />
        <Field label="评估方案" value={preview.understanding.evaluation_plan} />
      </div>

      {preview.answers.length > 0 && (
        <div className={styles.qa}>
          <div className={styles.qaTitle}>澄清问答</div>
          {preview.answers.map((answer) => (
            <div key={answer.request_id} className={styles.qaRow}>
              <span className={styles.question}>{answer.question}</span>
              <span className={styles.answer}>
                {answer.outcome === "skip"
                  ? "跳过"
                  : answer.outcome === "timeout"
                    ? "超时"
                    : answer.outcome === "cancelled"
                      ? "已取消"
                      : answer.choice_label ?? answer.value ?? ""}
              </span>
            </div>
          ))}
        </div>
      )}

      {preview.unresolved.length > 0 && (
        <div className={styles.unresolved}>
          <div className={styles.unresolvedTitle}>未解决项</div>
          {preview.unresolved.map((item) => (
            <div key={`${item.field}-${item.reason}`} className={styles.unresolvedRow}>
              <Label variant={item.critical ? "danger" : "attention"}>
                {item.field}
                {item.critical ? " · 关键" : ""}
              </Label>
              <span className={styles.unresolvedReason}>{item.reason}</span>
            </div>
          ))}
        </div>
      )}

      <footer className={styles.footer}>
        <div className={styles.meta}>
          <span>Revision {preview.revision}</span>
          {preview.failure && <span className={styles.failure}> {preview.failure.code}</span>}
        </div>

        {isRunning ? (
          <span className={styles.running}>
            <Spinner size="small" srText={null} /> 任务运行中…
            <Label variant="success">已启动</Label>
          </span>
        ) : isConfirming || (isReady && pending) ? (
          <span className={styles.running}>
            <Spinner size="small" srText={null} /> 正在处理…
          </span>
        ) : null}
      </footer>

      {isFailed && (
        <div className={styles.actions}>
          <Button variant="primary" onClick={handleRetry} disabled={pending}>
            Retry
          </Button>
          <Button onClick={handleCancel} disabled={pending}>
            Cancel
          </Button>
        </div>
      )}

      {isReady && (
        <div className={styles.actions}>
          <div className={styles.revise}>
            <FormControl id={`revise-${preview.draftId || "preview"}`}>
              <FormControl.Label>Revise with instruction</FormControl.Label>
              <Textarea
                block
                value={revisionText}
                onChange={(event) => setRevisionText(event.target.value)}
                maxLength={MAX_REVISION_LENGTH}
                rows={2}
                placeholder="Optional revision instruction"
              />
            </FormControl>
            <div className={styles.reviseActions}>
              {hasCriticalUnresolved && (
                <FormControl disabled={pending}>
                  <Checkbox
                    checked={acknowledgeUnresolved}
                    onChange={(event) => setAcknowledgeUnresolved(event.target.checked)}
                  />
                  <FormControl.Label>Acknowledge unresolved items</FormControl.Label>
                </FormControl>
              )}
              <Button variant="primary" onClick={handleConfirm} disabled={confirmationDisabled}>
                {isConfirming ? "Confirming…" : "确认并启动 (Confirm and start)"}
              </Button>
              <Button onClick={handleRevise} disabled={pending || revisionTextInvalid}>
                Revise
              </Button>
              <Button variant="invisible" onClick={handleCancel} disabled={pending}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}

      {preview.status === "CLARIFYING" && !isReady && !isFailed && !isRunning && (
        <p className={styles.hint}>任务理解中… 完成后将显示确认与启动。</p>
      )}
    </section>
  );
}
