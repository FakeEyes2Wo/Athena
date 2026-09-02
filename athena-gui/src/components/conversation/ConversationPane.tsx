import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { WelcomeHero } from "./WelcomeHero";
import { PendingHypotheses } from "./PendingHypotheses";
import { RunControls } from "./RunControls";
import { usePipeline } from "../../hooks/usePipeline";
import styles from "./ConversationPane.module.css";

type Pipeline = ReturnType<typeof usePipeline>;

interface ConversationPaneProps {
  pipeline: Pipeline;
}

/** Main conversation area showing the header, run controls, message list, and composer. */
export function ConversationPane({ pipeline }: ConversationPaneProps) {
  const { viewModel } = pipeline;
  const hasFullPreviewActions =
    typeof pipeline.confirmDraft === "function" &&
    typeof pipeline.reviseDraft === "function" &&
    typeof pipeline.retryDraft === "function" &&
    typeof pipeline.cancelDraft === "function";

  // The real hook always exposes the full gate actions. The fallback keeps old
  // hand-written test pipelines working; production never routes through startRun.
  const onConfirmPreview = hasFullPreviewActions
    ? (acknowledge: boolean) => pipeline.confirmDraft(acknowledge)
    : () => pipeline.startRun?.();
  const onRevisePreview = hasFullPreviewActions
    ? (instruction: string) => pipeline.reviseDraft(instruction)
    : () => undefined;
  const onRetryPreview = hasFullPreviewActions
    ? () => pipeline.retryDraft()
    : () => undefined;
  const onCancelPreview = hasFullPreviewActions
    ? () => pipeline.cancelDraft()
    : () => undefined;

  return (
    <section className={styles["conversation-pane"]}>
      <RunControls
        viewModel={viewModel}
        active={pipeline.runActive}
        onPause={() => void pipeline.pauseRun()}
        onResume={() => void pipeline.resumeRun()}
        onStop={() => void pipeline.stopRun()}
        onToggleMode={() => void pipeline.toggleMode()}
      />
      {viewModel.manual && (
        <PendingHypotheses pending={viewModel.pending} onSelect={pipeline.selectHypothesis} />
      )}
      {viewModel.messages.length === 0 ? (
        <WelcomeHero onPrompt={(prompt) => void pipeline.sendPrompt(prompt)} />
      ) : (
        <MessageList
          messages={viewModel.messages}
          onConfirmPreview={onConfirmPreview}
          onRevisePreview={onRevisePreview}
          onRetryPreview={onRetryPreview}
          onCancelPreview={onCancelPreview}
        />
      )}
      <Composer onSend={pipeline.sendPrompt} />
    </section>
  );
}
