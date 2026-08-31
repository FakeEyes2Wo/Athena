import { useState } from "react";
import type { HumanRequest } from "../../lib/tauri-bridge";
import styles from "./HumanRequestDialog.module.css";

interface HumanRequestDialogProps {
  requests: HumanRequest[];
  /** 提问的会话标题；只在提问来自后台会话（不是正在看的那个）时给。 */
  fromSession?: string;
  onAnswer(requestId: string, answer: string): void;
  onChoice?(requestId: string, value: string): void;
  onSkip?(requestId: string): void;
}

/** Modal asking the human to answer the supervisor's outstanding question. */
export function HumanRequestDialog({
  requests = [],
  fromSession,
  onAnswer,
  onChoice,
  onSkip,
}: HumanRequestDialogProps) {
  const [answer, setAnswer] = useState("");
  if (requests.length === 0) return null;
  const request = requests[0];

  const submit = () => {
    if (!answer.trim()) return;
    onAnswer(request.request_id, answer);
    setAnswer("");
  };

  const choose = (value: string) => {
    onChoice?.(request.request_id, value);
  };

  const skip = () => {
    onSkip?.(request.request_id);
  };

  return (
    <div className={styles.backdrop} role="dialog" aria-modal="true">
      <div className={styles.dialog}>
        <h2 className={styles.title}>需要你的确认</h2>
        {fromSession && <p className={styles.origin}>来自会话 {fromSession}</p>}
        <p className={styles.prompt}>{request.prompt}</p>

        {request.choices && request.choices.length > 0 && (
          <div className={styles.choices}>
            {request.choices.map((choice, index) => (
              <button
                key={choice.value}
                className={styles.button}
                type="button"
                onClick={() => choose(choice.value)}
              >
                {index + 1}. {choice.label}
              </button>
            ))}
          </div>
        )}

        <div className={styles.actions}>
          {request.allow_custom !== false && (
            <input
              className={styles.input}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
              placeholder="其他回答（可选）"
              autoFocus={!request.choices || request.choices.length === 0}
            />
          )}
          {request.allow_custom !== false && (
            <button className={styles.button} type="button" onClick={submit}>
              回复
            </button>
          )}
          {request.allow_skip !== false && (
            <button className={styles.button} type="button" onClick={skip}>
              跳过
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
