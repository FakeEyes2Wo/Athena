import { FormEvent, useState } from "react";

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
    <form className="composer" onSubmit={(event) => void handleSubmit(event)}>
      <textarea
        className="composer__input"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="描述你的 ML 任务..."
        rows={3}
      />
      <button className="composer__send" type="submit" disabled={disabled}>
        发送
      </button>
    </form>
  );
}
