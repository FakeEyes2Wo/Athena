import type { UIMessage } from "../../types/ui";
import type { TaskPreview } from "../../lib/tauri-bridge";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";
import { ErrorCard } from "../cards/ErrorCard";

interface MessageListProps {
  messages: UIMessage[];
  onStartRun(preview: TaskPreview): Promise<void>;
}

/** Renders a scrollable list of chat messages, intent preview cards, and error cards. */
export function MessageList({ messages, onStartRun }: MessageListProps) {
  return (
    <div className="message-list">
      {messages.map((msg) => {
        if (msg.kind === "intent-preview" && msg.preview) {
          return (
            <IntentPreviewCard
              key={msg.id}
              preview={msg.preview}
              onConfirm={() => onStartRun(msg.preview!)}
            />
          );
        }

        if (msg.kind === "error") {
          return <ErrorCard key={msg.id} content={msg.content} />;
        }

        return (
          <article key={msg.id} className={`message-bubble message-bubble--${msg.role}`}>
            {msg.content}
          </article>
        );
      })}
    </div>
  );
}
