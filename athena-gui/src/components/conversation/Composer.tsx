import { FormEvent, useState } from "react";
import { Button, Textarea } from "@primer/react";
import { PaperAirplaneIcon } from "@primer/octicons-react";
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
    <form className={styles.composer} onSubmit={(event) => void handleSubmit(event)}>
      <Textarea
        block
        resize="vertical"
        rows={3}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="描述你的 ML 任务..."
        aria-label="描述你的 ML 任务"
      />
      <div className={styles.footer}>
        <span className={styles.hint}>支持命令：/pause /resume /stop /manual /auto /select &lt;id&gt; /help</span>
        <Button type="submit" variant="primary" trailingVisual={PaperAirplaneIcon} disabled={disabled}>
          发送
        </Button>
      </div>
    </form>
  );
}
