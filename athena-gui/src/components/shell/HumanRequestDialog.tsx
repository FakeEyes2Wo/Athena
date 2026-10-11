import { useEffect, useRef, useState } from "react";
import { ActionList, Button, Dialog, Flash, TextInput } from "@primer/react";
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

/** Modal asking the human to answer the supervisor's outstanding question. */
export function HumanRequestDialog({
  request,
  onReply,
  settling = false,
  error = null,
}: HumanRequestDialogProps) {
  const [answer, setAnswer] = useState("");
  const [errorVisible, setErrorVisible] = useState<string | null>(null);
  const submittingRef = useRef(false);
  const firstOptionRef = useRef<HTMLElement | null>(null);
  const textRef = useRef<HTMLInputElement>(null);
  const skipRef = useRef<HTMLButtonElement>(null);
  const escapeSkipRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    setAnswer("");
    setErrorVisible(null);
    submittingRef.current = false;
  }, [request?.request_id]);

  // Primer Dialog 的背景遮罩点击也会回调 onClose("escape")，所以不用 onClose 判断 Esc；
  // 改在捕获阶段监听 Esc 键，先于 Primer 的处理器（它会 preventDefault 并中断后续处理器）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (event.key === "Escape") escapeSkipRef.current?.();
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, []);

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

  if (!request) {
    escapeSkipRef.current = null;
    return null;
  }

  const choices = request.choices ?? [];
  const showText = request.allow_custom !== false;
  const showSkip = request.allow_skip !== false;
  const allDisabled = settling || submittingRef.current;
  const visibleError = error ?? errorVisible;
  // Primer Dialog 不会自动把焦点移进弹窗：优先聚焦第一个选项，其次输入框，最后 Skip。
  const initialFocusRef = choices.length > 0 ? firstOptionRef : showText ? textRef : skipRef;

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

  // 弹窗只能通过回答关闭：不渲染关闭按钮，点击背景遮罩无效；Esc 仅在允许跳过时等同 Skip。
  escapeSkipRef.current = showSkip && !allDisabled ? skip : null;

  return (
    <Dialog
      key={request.request_id}
      width="large"
      onClose={() => undefined}
      initialFocusRef={initialFocusRef}
      renderHeader={({ dialogLabelId }) => (
        <Dialog.Header>
          <Dialog.Title id={dialogLabelId}>需要你的确认</Dialog.Title>
        </Dialog.Header>
      )}
    >
      <p className={styles.prompt}>{request.prompt}</p>

      {choices.length > 0 && (
        <div
          className={styles.choices}
          ref={(node) => {
            firstOptionRef.current = node?.querySelector<HTMLElement>('[role="option"]') ?? null;
          }}
        >
          <ActionList role="listbox" aria-label="Options" selectionVariant="single">
            {choices.map((choice, index) => (
              <ActionList.Item
                key={choice.value}
                role="option"
                selected={false}
                disabled={allDisabled}
                onSelect={() => choose(choice)}
              >
                <ActionList.LeadingVisual>
                  <span aria-hidden="true">{index + 1}</span>
                </ActionList.LeadingVisual>
                {choice.label}
              </ActionList.Item>
            ))}
          </ActionList>
        </div>
      )}

      <div className={styles.actions}>
        {showText && (
          <>
            <TextInput
              ref={textRef}
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
            <Button onClick={submitText} disabled={allDisabled || !answer.trim()}>
              Reply
            </Button>
          </>
        )}
        {showSkip && (
          <Button ref={skipRef} variant="invisible" onClick={skip} disabled={allDisabled}>
            Skip
          </Button>
        )}
      </div>

      {visibleError && (
        <Flash variant="danger" className={styles.error}>
          <span role="alert">{visibleError}</span>
        </Flash>
      )}
    </Dialog>
  );
}
