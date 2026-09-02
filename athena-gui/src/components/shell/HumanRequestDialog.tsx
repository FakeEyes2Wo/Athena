import { useEffect, useMemo, useRef, useState } from "react";
import type { HumanChoice, HumanReply, HumanRequest } from "../../types/ui";
import styles from "./HumanRequestDialog.module.css";

interface HumanRequestDialogProps {
  request: HumanRequest | null;
  onReply(reply: HumanReply): Promise<void> | void;
  /** True while one reply is being submitted. */
  settling?: boolean;
  /** A transport/contract error to surface without dismissing the dialog. */
  error?: string | null;
}

const FOCUSABLE_SELECTOR =
  "button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";

/** Modal asking the human to answer the supervisor's outstanding question. */
export function HumanRequestDialog({
  request,
  onReply,
  settling = false,
  error = null,
}: HumanRequestDialogProps) {
  const [answer, setAnswer] = useState("");
  const [errorVisible, setErrorVisible] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = useMemo(() => `human-request-title-${request?.request_id ?? "none"}`, [request?.request_id]);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const submittingRef = useRef(false);
  const [activeChoiceIndex, setActiveChoiceIndex] = useState(0);

  const isOpen = request !== null;

  // Reset local state whenever the request identity changes.
  useEffect(() => {
    setAnswer("");
    setErrorVisible(null);
    setActiveChoiceIndex(0);
    submittingRef.current = false;
  }, [request?.request_id]);

  // Remember the focused element when opening and restore it when closing.
  useEffect(() => {
    if (isOpen) {
      previouslyFocused.current = document.activeElement as HTMLElement | null;
      const first =
        dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ??
        document.body;
      first.focus();
      return;
    }
    const previous = previouslyFocused.current;
    if (previous && document.contains(previous)) {
      previous.focus();
    }
    previouslyFocused.current = null;
  }, [isOpen]);

  // Trap focus and provide keyboard navigation while the dialog is open.
  useEffect(() => {
    if (!isOpen) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (request?.allow_skip && !settling && !submittingRef.current) {
          event.preventDefault();
          void submitReply({ kind: "skip" });
        }
        return;
      }

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const choices = request?.choices ?? [];
        if (choices.length > 0) {
          const current = document.activeElement;
          const buttons = Array.from(
            dialogRef.current?.querySelectorAll<HTMLButtonElement>("button[data-choice]") ?? [],
          );
          const currentIndex = buttons.findIndex((button) => button === current);
          if (currentIndex >= 0) {
            event.preventDefault();
            const delta = event.key === "ArrowDown" ? 1 : -1;
            const next = (currentIndex + delta + buttons.length) % buttons.length;
            buttons[next].focus();
            setActiveChoiceIndex(next);
          }
        }
        return;
      }

      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      event.preventDefault();
      if (event.shiftKey) {
        if (active === first) {
          last.focus();
        } else {
          const index = focusable.findIndex((element) => element === active);
          const previous = focusable[Math.max(0, (index < 0 ? 0 : index) - 1)];
          previous.focus();
        }
      } else if (active === last) {
        first.focus();
      } else {
        const index = focusable.findIndex((element) => element === active);
        const next = focusable[Math.min(focusable.length - 1, (index < 0 ? -1 : index) + 1)];
        next.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [isOpen, request, settling]);

  const submitReply = async (reply: HumanReply) => {
    if (!request || settling || submittingRef.current) return;
    if (reply.kind === "text" && !answer.trim()) return;
    submittingRef.current = true;
    try {
      await onReply(reply);
      setErrorVisible(null);
    } catch (err) {
      setErrorVisible(err instanceof Error ? err.message : String(err));
    } finally {
      submittingRef.current = false;
      if (reply.kind !== "text") setAnswer("");
    }
  };

  if (!request) return null;

  const choices = request.choices ?? [];
  const showText = request.allow_custom !== false;
  const showSkip = request.allow_skip !== false;
  const allDisabled = settling || submittingRef.current;
  const visibleError = error ?? errorVisible;

  const submitText = () => {
    if (!answer.trim()) return;
    void submitReply({ kind: "text", text: answer.trim() });
  };

  const choose = (choice: HumanChoice) => {
    void submitReply({ kind: "choice", value: choice.value });
  };

  const skip = () => {
    void submitReply({ kind: "skip" });
  };

  return (
    <div className={styles.backdrop}>
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <h2 id={titleId} className={styles.title}>需要你的确认</h2>
        <p className={styles.prompt}>{request.prompt}</p>

        {choices.length > 0 && (
          <div className={styles.choices} role="listbox" aria-label="Options">
            {choices.map((choice, index) => (
              <button
                key={choice.value}
                className={`${styles.button}${index === activeChoiceIndex ? ` ${styles.buttonActive}` : ""}`}
                type="button"
                data-choice
                onClick={() => choose(choice)}
                disabled={allDisabled}
              >
                <span aria-hidden>{index + 1}.</span> {choice.label}
              </button>
            ))}
          </div>
        )}

        <div className={styles.actions}>
          {showText && (
            <>
              <input
                className={styles.input}
                value={answer}
                onChange={(event) => setAnswer(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") submitText();
                }}
                placeholder="Other answer (optional)"
                disabled={allDisabled}
                aria-label="Free text answer"
              />
              <button
                className={styles.button}
                type="button"
                onClick={submitText}
                disabled={allDisabled || !answer.trim()}
              >
                Reply
              </button>
            </>
          )}
          {showSkip && (
            <button
              className={`${styles.button} ${styles.buttonSkip}`}
              type="button"
              onClick={skip}
              disabled={allDisabled}
            >
              Skip
            </button>
          )}
        </div>

        {visibleError && (
          <div className={styles.error} role="alert">
            {visibleError}
          </div>
        )}
      </div>
    </div>
  );
}
