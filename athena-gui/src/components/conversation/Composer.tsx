import { FormEvent, useState } from "react";
import { Icon } from "../common/Icon";
import styles from "./Composer.module.css";

interface ComposerProps {
  onSend(message: string): Promise<void>;
  disabled?: boolean;
}

/** Message input area with a textarea and send button. */
export function Composer({ onSend, disabled }: ComposerProps) {
  const [value, setValue] = useState("");

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    const next = value;
    setValue("");
    await onSend(next);
  }

  return (
    <form className={styles["composer"]} onSubmit={(event) => void handleSubmit(event)}>
      <textarea
        className={styles["composer__input"]}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="描述你的 ML 任务..."
        aria-label="描述你的 ML 任务"
        rows={3}
      />
      <button className={styles["composer__send"]} type="submit" disabled={disabled}>
        发送 <Icon name="send" size={14} />
      </button>
      <div className={styles["composer__hint"]}>
        支持命令：/pause /resume /stop /manual /auto /select &lt;id&gt; /help
      </div>
    </form>
  );
}
