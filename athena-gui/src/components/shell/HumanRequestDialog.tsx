import { useState } from "react";
import type { HumanRequest } from "../../lib/tauri-bridge";
import styles from "./HumanRequestDialog.module.css";

interface HumanRequestDialogProps {
  requests: HumanRequest[];
  onAnswer(requestId: string, answer: string): void;
}

/** Modal asking the human to answer the supervisor's outstanding question. */
export function HumanRequestDialog({ requests = [], onAnswer }: HumanRequestDialogProps) {
  const [answer, setAnswer] = useState("");
  if (requests.length === 0) return null;
  const request = requests[0];

  const submit = () => {
    if (!answer.trim()) return;
    onAnswer(request.request_id, answer);
    setAnswer("");
  };

  return (
    <div className={styles.backdrop} role="dialog" aria-modal="true">
      <div className={styles.dialog}>
        <h2 className={styles.title}>需要你的确认</h2>
        <p className={styles.prompt}>{request.prompt}</p>
        <div className={styles.actions}>
          <input
            className={styles.input}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            placeholder="回复（例如：done）"
            autoFocus
          />
          <button className={styles.button} type="button" onClick={submit}>
            回复
          </button>
        </div>
      </div>
    </div>
  );
}
