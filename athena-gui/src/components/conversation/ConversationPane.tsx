import { useState, type ReactNode } from "react";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { WelcomeHero } from "./WelcomeHero";
import { PendingHypotheses } from "./PendingHypotheses";
import { RunControls } from "./RunControls";
import { HumanRequestPanel } from "./HumanRequestPanel";
import { MainHeader } from "../shell/MainHeader";
import { Icon } from "../common/Icon";
import { usePipeline } from "../../hooks/usePipeline";
import type { HumanRequest } from "../../types/ui";
import styles from "./ConversationPane.module.css";

type Pipeline = ReturnType<typeof usePipeline>;

/** What the composer does with the next message, derived from session state. */
export type ComposerMode = "task" | "answer" | "choice-only" | "revise" | "busy" | "guide" | "ended";

export function composerModeOf(pipeline: Pipeline): ComposerMode {
  const request: HumanRequest | undefined = pipeline.humanRequests?.[0];
  if (request) return request.allow_custom === false ? "choice-only" : "answer";
  const clarification = pipeline.status;
  if (clarification === "READY_FOR_CONFIRMATION") return "revise";
  if (clarification === "CLARIFYING" || clarification === "CONFIRMING") return "busy";
  const { status } = pipeline.viewModel;
  const live = status === "running" || status === "paused";
  if (live && pipeline.runActive) return "guide";
  if (pipeline.runActive && (status === "completed" || status === "error")) return "ended";
  return "task";
}

const PLACEHOLDER: Record<ComposerMode, string> = {
  task: "描述你的研究任务：数据在哪里、预测什么、用什么指标评估…",
  answer: "输入你的回答，按 Enter 发送",
  "choice-only": "请在上方选择一个选项",
  revise: "输入修改意见，Athena 会据此重新理解任务",
  busy: "Athena 正在理解任务…",
  guide: "向研究过程发送指导，或输入 / 查看命令",
  ended: "本会话的研究已结束。开始新任务请新建会话",
};

const MODE_TAG: Partial<Record<ComposerMode, { icon: Parameters<typeof Icon>[0]["name"]; label: string }>> = {
  answer: { icon: "question", label: "回答 Athena 的问题" },
  revise: { icon: "pencil", label: "修改任务理解" },
  guide: { icon: "sparkles", label: "研究指导" },
};

interface ConversationPaneProps {
  pipeline: Pipeline;
  /** Session title shown in the header once the conversation has content. */
  title?: string;
  /** Global header actions (inspector / log toggles). */
  headerActions?: ReactNode;
  /** Stop with confirmation; defaults to stopping directly. */
  onStopRun?(): void;
  onNewSession?(): void;
  onOpenSettings?(): void;
  settingsVersion?: number;
}

/** Conversation view: header with run controls, transcript, question panel and composer. */
export function ConversationPane({
  pipeline,
  title,
  headerActions,
  onStopRun,
  onNewSession,
  onOpenSettings,
  settingsVersion,
}: ConversationPaneProps) {
  const { viewModel } = pipeline;
  const [draft, setDraft] = useState("");
  const [focusSignal, setFocusSignal] = useState(0);
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
  const onRetryPreview = hasFullPreviewActions ? () => pipeline.retryDraft() : () => undefined;
  const onCancelPreview = hasFullPreviewActions ? () => pipeline.cancelDraft() : () => undefined;

  const request = pipeline.humanRequests?.[0] ?? null;
  const mode = composerModeOf(pipeline);
  const stopRun = onStopRun ?? (() => void pipeline.stopRun?.());
  const showRunBar = Boolean(pipeline.runActive) || viewModel.status !== "idle" || viewModel.resumeAvailable;
  const empty = viewModel.messages.length === 0;

  async function submit(text: string) {
    const content = text.trim();
    if (!content) return;
    if (mode === "answer" && request) {
      await pipeline.replyToHumanRequest(request.request_id, { kind: "text", text: content });
      return;
    }
    if (mode === "revise") {
      // Re-understanding can take a while (and may ask questions); don't block the box.
      void Promise.resolve(onRevisePreview(content)).catch(() => undefined);
      return;
    }
    if (content.split(/\s+/)[0] === "/stop") {
      stopRun();
      return;
    }
    if (mode === "guide") {
      await pipeline.sendPrompt(content);
      return;
    }
    // A new task runs its whole clarification inside this call; keep the composer free
    // so questions can be answered meanwhile. Errors are reported in the conversation.
    void Promise.resolve(pipeline.sendPrompt(content)).catch(() => undefined);
  }

  const tag = MODE_TAG[mode];
  const composer = (
    <Composer
      value={draft}
      onChange={setDraft}
      onSubmit={submit}
      placeholder={
        mode === "ended" && viewModel.resumeAvailable
          ? "研究已中断，可点击右上角「继续」恢复；开始新任务请新建会话"
          : PLACEHOLDER[mode]
      }
      disabled={mode === "busy" || mode === "choice-only" || mode === "ended"}
      commands={mode === "task" || mode === "guide"}
      size={empty ? "lg" : "md"}
      autoFocus
      focusSignal={focusSignal}
      label={empty ? "描述你的研究任务" : "消息"}
      toolbar={
        mode === "ended" && onNewSession ? (
          <button type="button" className="btn btn--sm" onClick={onNewSession}>
            <Icon name="plus" size={13} /> 新建会话
          </button>
        ) : tag ? (
          <span className={styles.modeTag}>
            <Icon name={tag.icon} size={12} />
            {tag.label}
          </span>
        ) : (
          <span className={styles.keyHint}>
            <span className="kbd">Enter</span> 发送 · <span className="kbd">Shift</span>+<span className="kbd">Enter</span> 换行
          </span>
        )
      }
    />
  );

  return (
    <section className={styles.pane} aria-label="对话">
      <MainHeader
        title={!empty ? title : undefined}
        actions={headerActions}
      >
        {showRunBar && (
          <RunControls
            viewModel={viewModel}
            active={Boolean(pipeline.runActive)}
            onPause={() => void pipeline.pauseRun?.()}
            onResume={() => void pipeline.resumeRun?.()}
            onStop={stopRun}
            onToggleMode={() => void pipeline.toggleMode?.()}
          />
        )}
      </MainHeader>

      {viewModel.manual &&
        (viewModel.status === "running" || viewModel.status === "paused") &&
        typeof pipeline.selectHypothesis === "function" && (
        <PendingHypotheses pending={viewModel.pending} onSelect={pipeline.selectHypothesis} />
      )}

      {empty ? (
        <WelcomeHero
          composer={composer}
          onExample={(prompt) => {
            setDraft(prompt);
            setFocusSignal((n) => n + 1);
          }}
          onOpenSettings={onOpenSettings}
          settingsVersion={settingsVersion}
        />
      ) : (
        <>
          <MessageList
            messages={viewModel.messages}
            onConfirmPreview={onConfirmPreview}
            onRevisePreview={onRevisePreview}
            onRetryPreview={onRetryPreview}
            onCancelPreview={onCancelPreview}
          />
          <div className={styles.dock}>
            {request && (
              <HumanRequestPanel
                request={request}
                onReply={(reply) => pipeline.replyToHumanRequest(request.request_id, reply)}
                settling={pipeline.settlingRequestId === request.request_id}
                error={pipeline.humanPendingError}
              />
            )}
            {composer}
            <p className={styles.footnote}>
              Athena 的结论来自自动实验，使用前请核对结果、数据划分与评估方式。
            </p>
          </div>
        </>
      )}

      {empty && request && (
        <div className={styles.dock}>
          <HumanRequestPanel
            request={request}
            onReply={(reply) => pipeline.replyToHumanRequest(request.request_id, reply)}
            settling={pipeline.settlingRequestId === request.request_id}
            error={pipeline.humanPendingError}
          />
        </div>
      )}
    </section>
  );
}
