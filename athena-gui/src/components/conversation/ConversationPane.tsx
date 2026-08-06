import { MessageList } from "./MessageList";
import { Composer } from "./Composer";

interface ConversationPaneProps {
  pipeline: {
    viewModel: {
      messages: Array<{ id: string; kind: string; content: string; preview?: unknown }>;
    };
    sendPrompt(message: string): Promise<void>;
    startRun(preview: unknown): Promise<void>;
  };
}

/** Main conversation area showing the header, message list, and message composer. */
export function ConversationPane({ pipeline }: ConversationPaneProps) {
  return (
    <section className="conversation-pane">
      <div className="conversation-pane__header">
        <h1 className="conversation-pane__title">Athena</h1>
        <p className="conversation-pane__subtitle">对话式 ML 工作流编排</p>
      </div>
      <MessageList
        messages={pipeline.viewModel.messages as never}
        onStartRun={pipeline.startRun}
      />
      <Composer onSend={pipeline.sendPrompt} />
    </section>
  );
}
