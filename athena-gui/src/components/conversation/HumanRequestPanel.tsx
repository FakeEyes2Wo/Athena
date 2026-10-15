import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { HumanChoice, HumanReply, HumanRequest } from "../../types/ui";
import { Icon } from "../common/Icon";
import styles from "./HumanRequestPanel.module.css";

interface HumanRequestPanelProps {
  request: HumanRequest;
  onReply(reply: HumanReply): Promise<void> | void;
  /** True while one reply is being submitted. */
  settling?: boolean;
  /** A transport/contract error to surface without dismissing the panel. */
  error?: string | null;
  /** Free-text answers are typed into the conversation composer. */
  composerAnswers?: boolean;
}

function useRemainingSeconds(expiresAt: string): number | null {
  const deadline = Date.parse(expiresAt);
  const compute = () =>
    Number.isFinite(deadline) ? Math.max(0, Math.round((deadline - Date.now()) / 1000)) : null;
  const [remaining, setRemaining] = useState(compute);
  useEffect(() => {
    setRemaining(compute());
    if (!Number.isFinite(deadline)) return;
    const timer = setInterval(() => setRemaining(compute()), 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deadline]);
  return remaining;
}

function formatRemaining(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Inline question from Athena, docked above the composer (choices, skip, countdown). */
export function HumanRequestPanel({
  request,
  onReply,
  settling = false,
  error = null,
  composerAnswers = true,
}: HumanRequestPanelProps) {
  const [localError, setLocalError] = useState<string | null>(null);
  const submittingRef = useRef(false);
  const requestEpoch = useRef(0);
  const [submitting, setSubmitting] = useState(false);
  const choicesRef = useRef<HTMLDivElement | null>(null);
  const remaining = useRemainingSeconds(request.expires_at);

  useEffect(() => {
    setLocalError(null);
    submittingRef.current = false;
    setSubmitting(false);
    return () => {
      // Replies can finish after a different question is shown or the panel closes.
      requestEpoch.current += 1;
    };
  }, [request.request_id]);

  const submit = async (reply: HumanReply) => {
    if (settling || submittingRef.current) return;
    const epoch = requestEpoch.current;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      await onReply(reply);
      if (requestEpoch.current === epoch) setLocalError(null);
    } catch (err) {
      if (requestEpoch.current === epoch) {
        setLocalError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (requestEpoch.current === epoch) {
        submittingRef.current = false;
        setSubmitting(false);
      }
    }
  };

  const choices = request.choices ?? [];
  const disabled = settling || submitting;
  const visibleError = error ?? localError;
  const allowSkip = request.allow_skip !== false;
  const allowCustom = request.allow_custom !== false;

  const onChoicesKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = Array.from(
      choicesRef.current?.querySelectorAll<HTMLButtonElement>("button[data-choice]") ?? [],
    );
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && index >= 0) {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      buttons[(index + delta + buttons.length) % buttons.length].focus();
      return;
    }
    const digit = Number(event.key);
    if (Number.isInteger(digit) && digit >= 1 && digit <= choices.length) {
      event.preventDefault();
      void submit({ kind: "choice", value: choices[digit - 1].value });
    }
  };

  return (
    <section className={styles.panel} role="region" aria-label="需要你的确认" aria-live="polite">
      <header className={styles.header}>
        <span className={styles.badge}>
          <Icon name="question" size={14} />
          需要你的确认
        </span>
        {remaining !== null && remaining <= 3600 && (
          <span
            className={`${styles.timer}${remaining <= 30 ? ` ${styles.timerUrgent}` : ""}`}
            title="超时后 Athena 会在没有你回答的情况下继续"
          >
            <Icon name="hourglass" size={12} />
            {remaining > 0 ? `${formatRemaining(remaining)} 后超时` : "即将超时"}
          </span>
        )}
      </header>
      <p className={styles.prompt}>{request.prompt}</p>

      {choices.length > 0 && (
        <div className={styles.choices} ref={choicesRef} onKeyDown={onChoicesKeyDown} role="group" aria-label="可选回答">
          {choices.map((choice: HumanChoice, index) => (
            <button
              key={choice.value}
              type="button"
              data-choice
              className={styles.choice}
              onClick={() => void submit({ kind: "choice", value: choice.value })}
              disabled={disabled}
            >
              <span className={styles.choiceIndex} aria-hidden>
                {index + 1}
              </span>
              <span className={styles.choiceLabel}>{choice.label}</span>
              <Icon name="arrowRight" size={14} className={styles.choiceArrow} />
            </button>
          ))}
        </div>
      )}

      <footer className={styles.footer}>
        <span className={styles.hint}>
          {allowCustom && composerAnswers
            ? choices.length > 0
              ? "选择一项，或在下方输入框中输入其他回答"
              : "在下方输入框中输入你的回答"
            : "请选择一项回答"}
        </span>
        {allowSkip && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void submit({ kind: "skip" })}
            disabled={disabled}
          >
            <Icon name="skip" size={13} />
            跳过
          </button>
        )}
      </footer>

      {visibleError && (
        <div className={styles.error} role="alert">
          <Icon name="alert" size={14} />
          {visibleError}
        </div>
      )}
    </section>
  );
}
