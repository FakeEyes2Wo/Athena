import { describe, expect, it, vi } from "vitest";
import { act, screen, fireEvent, within } from "@testing-library/react";
import { composerModeOf } from "../conversation/ConversationPane";
import { renderUi } from "../../test/render";
import { ConversationPane } from "../conversation/ConversationPane";
import {
  createEmptyPipelineViewModel,
  type ClarificationStatus,
  type UIMessage,
} from "../../types/ui";

function clarificationPreview(
  status: ClarificationStatus,
  id: string,
  draftId = id,
  optimisticScopeId?: string,
): UIMessage {
  return {
    id,
    role: "athena",
    kind: "intent-preview",
    content: status === "CLARIFYING" ? "任务理解中…" : "任务理解完成",
    started: status === "RUNNING",
    preview: {
      draftId,
      optimisticScopeId,
      revision: 1,
      status,
      understanding: {
        title: "预测流失",
        dataset: "churn.csv",
        target: "churned",
        task_type: "classification",
        primary_metric: "f1",
        direction: "maximize",
        evaluation_plan: "holdout f1",
      },
      answers: [],
      unresolved: [],
      failure: null,
    },
  };
}

function taskUnderstandingOutput(
  id: string,
  content: string,
  scopeId: string,
  overrides: Partial<Pick<UIMessage, "source" | "tool" | "channel">> = {},
): UIMessage {
  return {
    id,
    role: "athena",
    kind: "text",
    content,
    source: "agent",
    sessionId: "default",
    scope: "task_understanding",
    scopeId,
    ...overrides,
  };
}

function pipelineWithMessages(messages: UIMessage[]) {
  return {
    viewModel: { ...createEmptyPipelineViewModel(), messages },
    runActive: false,
    sendPrompt: vi.fn(),
    startRun: vi.fn(),
  } as never;
}

describe("ConversationPane", () => {
  it("renders the task title and messages from the pipeline view model", () => {
    const sendPrompt = vi.fn().mockResolvedValue(undefined);
    const startRun = vi.fn().mockResolvedValue(undefined);

    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "user-1",
            role: "user" as const,
            kind: "text" as const,
            content: "analyze this CSV",
          },
          {
            id: "preview-1",
            role: "athena" as const,
            kind: "intent-preview" as const,
            content: "任务类型: classification · 主指标: f1_macro",
            preview: {
              title: "图像分类 · f1_macro",
              dataset: "train.csv (tabular)",
              target: "label: multiclass",
              task_type: "classification",
              primary_metric: "f1_macro",
              direction: "maximize",
              evaluation_plan: "f1_macro over a held-out split",
              needs_configuration: true,
            },
          },
        ],
      },
      sendPrompt,
      startRun,
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByText("analyze this CSV")).toBeInTheDocument();
    expect(screen.getByText("任务类型")).toBeInTheDocument();
    expect(screen.getByText("分类")).toBeInTheDocument();
    expect(screen.getByText("f1_macro · 越大越好")).toBeInTheDocument();
  });

  it("calls startRun when the confirm button is clicked on an intent preview card", async () => {
    const sendPrompt = vi.fn().mockResolvedValue(undefined);
    const startRun = vi.fn().mockResolvedValue(undefined);

    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "preview-1",
            role: "athena" as const,
            kind: "intent-preview" as const,
            content: "任务类型: classification · 主指标: f1_macro",
            preview: {
              title: "图像分类 · f1_macro",
              dataset: "train.csv (tabular)",
              target: "label: multiclass",
              task_type: "classification",
              primary_metric: "f1_macro",
              direction: "maximize",
              evaluation_plan: "f1_macro over a held-out split",
              needs_configuration: true,
            },
          },
        ],
      },
      sendPrompt,
      startRun,
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认并开始研究" }));
    });
    expect(startRun).toHaveBeenCalledTimes(1);
  });

  it("disables pause/stop for a phantom RUNNING state with no real activity", () => {
    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        status: "running" as const,
        phase: "SEARCH",
      },
      runActive: false,
      sendPrompt: vi.fn(),
      startRun: vi.fn(),
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByRole("button", { name: "暂停" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  });

  it("enables pause/stop when a real run is active", () => {
    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        status: "running" as const,
        phase: "SEARCH",
        plans: [{ id: "hyp-1" }],
      },
      runActive: true,
      sendPrompt: vi.fn(),
      startRun: vi.fn(),
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByRole("button", { name: "暂停" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  });

  it("enables Continue for an interrupted task advertised by the backend", () => {
    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        status: "error" as const,
        phase: "PREPARE",
        resumeAvailable: true,
        resumeReason: "failed",
      },
      runActive: true,
      sendPrompt: vi.fn(),
      startRun: vi.fn(),
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByRole("button", { name: "继续" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "暂停" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  });

  it("shows a started state instead of the confirm button once confirmed", () => {
    const sendPrompt = vi.fn().mockResolvedValue(undefined);
    const startRun = vi.fn().mockResolvedValue(undefined);

    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "preview-1",
            role: "athena" as const,
            kind: "intent-preview" as const,
            content: "任务类型: classification · 主指标: f1_macro",
            started: true,
            preview: {
              title: "图像分类 · f1_macro",
              dataset: "train.csv (tabular)",
              target: "label: multiclass",
              task_type: "classification",
              primary_metric: "f1_macro",
              direction: "maximize",
              evaluation_plan: "f1_macro over a held-out split",
              needs_configuration: true,
            },
          },
        ],
      },
      sendPrompt,
      startRun,
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByText(/已启动/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认并开始研究" })).not.toBeInTheDocument();
  });

  it("shows backend output inside the live task-understanding activity while clarifying", () => {
    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "preview-clarifying",
            role: "athena" as const,
            kind: "intent-preview" as const,
            content: "任务理解中…",
            preview: {
              draftId: "draft-1",
              revision: 1,
              status: "CLARIFYING" as const,
              understanding: {
                title: "",
                dataset: null,
                target: null,
                task_type: "other",
                primary_metric: null,
                direction: null,
                evaluation_plan: null,
              },
              answers: [],
              unresolved: [],
              failure: null,
            },
          },
          {
            id: "agent-stream-1",
            role: "athena" as const,
            kind: "text" as const,
            content: "正在分析数据结构",
            source: "agent",
          },
          {
            id: "tool-call-1",
            role: "athena" as const,
            kind: "text" as const,
            content: '{"path":"train.csv"}',
            source: "agent",
            tool: "inspect_dataset",
          },
          {
            id: "tool-output-1",
            role: "athena" as const,
            kind: "text" as const,
            content: "rows=891",
            source: "tool",
            tool: "inspect_dataset",
            channel: "stdout",
          },
          {
            id: "tool-error-1",
            role: "athena" as const,
            kind: "text" as const,
            content: "validation failed",
            source: "tool",
            tool: "inspect_dataset",
            channel: "stderr",
          },
        ],
      },
      runActive: false,
      sendPrompt: vi.fn(),
      startRun: vi.fn(),
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    const activity = screen.getByRole("log", { name: "任务理解过程" });
    expect(within(activity).getByText("正在分析数据结构")).toBeInTheDocument();
    expect(within(activity).getByText("inspect_dataset")).toBeInTheDocument();
    expect(within(activity).getByText("rows=891")).toBeInTheDocument();
    expect(within(activity).getByText(/stderr/)).toBeInTheDocument();
    expect(within(activity).getByText("validation failed")).toBeInTheDocument();
    expect(screen.getByText("任务理解中…")).toBeInTheDocument();
    expect(screen.getAllByText("正在分析数据结构")).toHaveLength(1);
  });

  it("groups canonical task-understanding output by draft scope", () => {
    const preview = clarificationPreview("CLARIFYING", "preview-canonical", "draft-1");
    const matching = taskUnderstandingOutput(
      "canonical-output",
      "canonical scoped output",
      "draft-1",
    );

    renderUi(<ConversationPane pipeline={pipelineWithMessages([preview, matching])} />);

    expect(
      within(screen.getByRole("log", { name: "任务理解过程" })).getByText(
        "canonical scoped output",
      ),
    ).toBeInTheDocument();
  });

  it("groups only the latched scope while the preview is optimistic", () => {
    const preview = clarificationPreview(
      "CLARIFYING",
      "preview-optimistic",
      "",
      "draft-a",
    );
    const accepted = taskUnderstandingOutput("optimistic-a", "latched output", "draft-a");
    const rejected = taskUnderstandingOutput("optimistic-b", "other draft output", "draft-b");

    renderUi(
      <ConversationPane pipeline={pipelineWithMessages([preview, accepted, rejected])} />,
    );

    const activity = screen.getByRole("log", { name: "任务理解过程" });
    expect(within(activity).getByText("latched output")).toBeInTheDocument();
    expect(within(activity).queryByText("other draft output")).not.toBeInTheDocument();
    expect(screen.getByText("other draft output")).toBeInTheDocument();
  });

  it("uses the canonical draft instead of stale or unrelated scope ids", () => {
    const preview = clarificationPreview(
      "CLARIFYING",
      "preview-strict",
      "draft-1",
      "draft-stale",
    );
    const matching = taskUnderstandingOutput("strict-match", "current draft", "draft-1");
    const stale = taskUnderstandingOutput("strict-stale", "stale latch", "draft-stale");
    const unrelated = taskUnderstandingOutput("strict-other", "unrelated draft", "draft-other");

    renderUi(
      <ConversationPane
        pipeline={pipelineWithMessages([preview, matching, stale, unrelated])}
      />,
    );

    const activity = screen.getByRole("log", { name: "任务理解过程" });
    expect(within(activity).getByText("current draft")).toBeInTheDocument();
    expect(within(activity).queryByText("stale latch")).not.toBeInTheDocument();
    expect(within(activity).queryByText("unrelated draft")).not.toBeInTheDocument();
    expect(screen.getByText("stale latch")).toBeInTheDocument();
    expect(screen.getByText("unrelated draft")).toBeInTheDocument();
  });

  it("keeps scoped history ordinary when no clarification preview is active", () => {
    const history = taskUnderstandingOutput(
      "scoped-history",
      "restored scoped history",
      "draft-history",
    );

    renderUi(<ConversationPane pipeline={pipelineWithMessages([history])} />);

    expect(screen.queryByRole("log", { name: "任务理解过程" })).not.toBeInTheDocument();
    expect(screen.getByText("restored scoped history")).toBeInTheDocument();
  });

  it("retains positional grouping for legacy unscoped output", () => {
    const preview = clarificationPreview("CLARIFYING", "preview-legacy", "draft-legacy");
    const legacy: UIMessage = {
      id: "legacy-output",
      role: "athena",
      kind: "text",
      content: "legacy task output",
      source: "agent",
    };

    renderUi(<ConversationPane pipeline={pipelineWithMessages([preview, legacy])} />);

    expect(
      within(screen.getByRole("log", { name: "任务理解过程" })).getByText(
        "legacy task output",
      ),
    ).toBeInTheDocument();
  });

  it("renders matching task-understanding tool output inside the activity details", () => {
    const preview = clarificationPreview("CONFIRMING", "preview-tool", "draft-tool");
    const toolOutput = taskUnderstandingOutput(
      "tool-output",
      "tool understanding result",
      "draft-tool",
      { source: "tool", tool: "report_task_understanding", channel: "stdout" },
    );

    renderUi(<ConversationPane pipeline={pipelineWithMessages([preview, toolOutput])} />);

    const activity = screen.getByRole("log", { name: "任务理解过程" });
    const content = within(activity).getByText("tool understanding result");
    expect(content.closest("details")).toBeInTheDocument();
    expect(within(activity).getByText(/report_task_understanding/)).toBeInTheDocument();
  });

  it("preserves the live activity region when a user message is inserted before it", () => {
    const preview = clarificationPreview("CLARIFYING", "preview-stable");
    const firstOutput: UIMessage = {
      id: "stable-output-1",
      role: "athena",
      kind: "text",
      content: "正在读取列信息",
      source: "agent",
    };
    const secondOutput: UIMessage = {
      id: "stable-output-2",
      role: "athena",
      kind: "text",
      content: "正在推断目标列",
      source: "supervisor",
    };
    const userReply: UIMessage = {
      id: "user-inserted",
      role: "user",
      kind: "text",
      content: "目标列是 churned",
    };

    const { rerender } = renderUi(
      <ConversationPane pipeline={pipelineWithMessages([preview, firstOutput, secondOutput])} />,
    );
    const initialActivity = screen.getByRole("log", { name: "任务理解过程" });

    rerender(
      <ConversationPane
        pipeline={pipelineWithMessages([preview, userReply, firstOutput, secondOutput])}
      />,
    );

    const updatedActivity = screen.getByRole("log", { name: "任务理解过程" });
    expect(updatedActivity).toBe(initialActivity);
    expect(within(updatedActivity).getByText("正在读取列信息")).toBeInTheDocument();
    expect(within(updatedActivity).getByText("正在推断目标列")).toBeInTheDocument();
    expect(within(updatedActivity).queryByText("目标列是 churned")).not.toBeInTheDocument();
    expect(screen.getByText("目标列是 churned")).toBeInTheDocument();
  });

  it("keeps interleaved user messages outside separate activity segments", () => {
    const preview = clarificationPreview("CLARIFYING", "preview-interleaved");
    const beforeReply: UIMessage = {
      id: "output-before-reply",
      role: "athena",
      kind: "text",
      content: "需要确认目标列",
      source: "supervisor",
    };
    const userReply: UIMessage = {
      id: "user-reply",
      role: "user",
      kind: "text",
      content: "使用 churned",
    };
    const afterReply: UIMessage = {
      id: "output-after-reply",
      role: "athena",
      kind: "text",
      content: "继续检查评价指标",
      source: "agent",
    };

    renderUi(
      <ConversationPane
        pipeline={pipelineWithMessages([preview, beforeReply, userReply, afterReply])}
      />,
    );

    const activities = screen.getAllByRole("log", { name: "任务理解过程" });
    expect(activities).toHaveLength(2);
    expect(within(activities[0]).getByText("需要确认目标列")).toBeInTheDocument();
    expect(within(activities[0]).queryByText("继续检查评价指标")).not.toBeInTheDocument();
    expect(within(activities[1]).getByText("继续检查评价指标")).toBeInTheDocument();
    expect(activities.every((activity) => !activity.contains(screen.getByText("使用 churned")))).toBe(true);
  });

  it("lets a later ready preview supersede an earlier active preview", () => {
    const earlierPreview = clarificationPreview("CLARIFYING", "preview-earlier");
    const laterPreview = clarificationPreview("READY_FOR_CONFIRMATION", "preview-later");
    const earlierOutput: UIMessage = {
      id: "earlier-output",
      role: "athena",
      kind: "text",
      content: "旧任务理解输出",
      source: "agent",
    };
    const laterOutput: UIMessage = {
      id: "later-output",
      role: "athena",
      kind: "text",
      content: "研究阶段输出",
      source: "agent",
    };

    renderUi(
      <ConversationPane
        pipeline={pipelineWithMessages([earlierPreview, earlierOutput, laterPreview, laterOutput])}
      />,
    );

    expect(screen.queryByRole("log", { name: "任务理解过程" })).not.toBeInTheDocument();
    expect(screen.getByText("旧任务理解输出")).toBeInTheDocument();
    expect(screen.getByText("研究阶段输出")).toBeInTheDocument();
  });

  it("keeps grouping backend output while the task understanding is confirming", () => {
    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "preview-confirming",
            role: "athena" as const,
            kind: "intent-preview" as const,
            content: "任务理解已确认",
            preview: {
              draftId: "draft-1",
              revision: 2,
              status: "CONFIRMING" as const,
              understanding: {
                title: "预测流失",
                dataset: "churn.csv",
                target: "churned",
                task_type: "classification",
                primary_metric: "f1",
                direction: "maximize",
                evaluation_plan: "holdout f1",
              },
              answers: [],
              unresolved: [],
              failure: null,
            },
          },
          {
            id: "supervisor-confirming-1",
            role: "athena" as const,
            kind: "text" as const,
            content: "正在提交确认结果",
            source: "supervisor",
          },
        ],
      },
      runActive: false,
      sendPrompt: vi.fn(),
      startRun: vi.fn(),
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(
      within(screen.getByRole("log", { name: "任务理解过程" })).getByText("正在提交确认结果"),
    ).toBeInTheDocument();
  });

  it.each(["READY_FOR_CONFIRMATION", "RUNNING"] as const)(
    "does not relabel later research output as task understanding when preview is %s",
    (status) => {
      const pipeline = {
        viewModel: {
          ...createEmptyPipelineViewModel(),
          messages: [
            {
              id: `preview-${status}`,
              role: "athena" as const,
              kind: "intent-preview" as const,
              content: "任务理解完成",
              started: status === "RUNNING",
              preview: {
                draftId: "draft-1",
                revision: 2,
                status,
                understanding: {
                  title: "预测流失",
                  dataset: "churn.csv",
                  target: "churned",
                  task_type: "classification",
                  primary_metric: "f1",
                  direction: "maximize",
                  evaluation_plan: "holdout f1",
                },
                answers: [],
                unresolved: [],
                failure: null,
              },
            },
            {
              id: `research-${status}`,
              role: "athena" as const,
              kind: "text" as const,
              content: "开始搜索候选方案",
              source: "agent",
            },
          ],
        },
        runActive: status === "RUNNING",
        sendPrompt: vi.fn(),
        startRun: vi.fn(),
      } as const;

      renderUi(<ConversationPane pipeline={pipeline as never} />);

      expect(screen.queryByRole("log", { name: "任务理解过程" })).not.toBeInTheDocument();
      expect(screen.getByText("开始搜索候选方案")).toBeInTheDocument();
    },
  );
});

const pendingRequest = {
  request_id: "req-1",
  session_id: "default",
  scope_id: "draft-1",
  scope_kind: "clarification",
  prompt: "目标列是哪一个？",
  choices: [
    { label: "churned", value: "churned" },
    { label: "label", value: "label" },
  ],
  allow_custom: true,
  allow_skip: true,
  created_at: "2026-10-10T10:00:00Z",
  expires_at: "2099-01-01T00:00:00Z",
};

function routedPipeline(overrides: Record<string, unknown> = {}) {
  return {
    viewModel: {
      ...createEmptyPipelineViewModel(),
      messages: [{ id: "user-1", role: "user" as const, kind: "text" as const, content: "predict churn" }],
    },
    status: "IDLE",
    runActive: false,
    humanRequests: [],
    sendPrompt: vi.fn().mockResolvedValue(undefined),
    startRun: vi.fn(),
    confirmDraft: vi.fn().mockResolvedValue(undefined),
    reviseDraft: vi.fn().mockResolvedValue(undefined),
    retryDraft: vi.fn().mockResolvedValue(undefined),
    cancelDraft: vi.fn().mockResolvedValue(undefined),
    replyToHumanRequest: vi.fn().mockResolvedValue(undefined),
    stopRun: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function typeAndSend(text: string) {
  const box = screen.getByRole("textbox", { name: "消息" });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
  return box;
}

describe("ConversationPane composer routing", () => {
  it("derives the composer mode from the session state", () => {
    const base = routedPipeline();
    expect(composerModeOf(base as never)).toBe("task");
    expect(composerModeOf({ ...base, humanRequests: [pendingRequest] } as never)).toBe("answer");
    expect(
      composerModeOf({ ...base, humanRequests: [{ ...pendingRequest, allow_custom: false }] } as never),
    ).toBe("choice-only");
    expect(composerModeOf({ ...base, status: "READY_FOR_CONFIRMATION" } as never)).toBe("revise");
    expect(composerModeOf({ ...base, status: "CLARIFYING" } as never)).toBe("busy");
    expect(
      composerModeOf({ ...base, runActive: true, viewModel: { ...base.viewModel, status: "running" } } as never),
    ).toBe("guide");
    expect(
      composerModeOf({ ...base, runActive: true, viewModel: { ...base.viewModel, status: "completed" } } as never),
    ).toBe("ended");
  });

  it("answers a pending question with the composer text", async () => {
    const pipeline = routedPipeline({ humanRequests: [pendingRequest], status: "CLARIFYING" });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByRole("region", { name: "需要你的确认" })).toHaveTextContent("目标列是哪一个？");
    await act(async () => {
      typeAndSend("churned 列");
    });
    expect(pipeline.replyToHumanRequest).toHaveBeenCalledWith("req-1", { kind: "text", text: "churned 列" });
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });

  it("answers with a choice from the question panel", async () => {
    const pipeline = routedPipeline({ humanRequests: [pendingRequest], status: "CLARIFYING" });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "label" }));
    });
    expect(pipeline.replyToHumanRequest).toHaveBeenCalledWith("req-1", { kind: "choice", value: "label" });
  });

  it("turns composer input into a revision while the draft awaits confirmation", async () => {
    const pipeline = routedPipeline({ status: "READY_FOR_CONFIRMATION" });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    await act(async () => {
      typeAndSend("主指标改为 AUC");
    });
    expect(pipeline.reviseDraft).toHaveBeenCalledWith("主指标改为 AUC");
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });

  it("locks the composer while Athena is understanding the task", () => {
    renderUi(<ConversationPane pipeline={routedPipeline({ status: "CLARIFYING" }) as never} />);
    expect(screen.getByRole("textbox", { name: "消息" })).toBeDisabled();
  });

  it("offers a new session once this session's research has ended", () => {
    const onNewSession = vi.fn();
    const pipeline = routedPipeline({
      runActive: true,
      viewModel: { ...routedPipeline().viewModel, status: "completed", phase: "COMPLETED" },
    });
    renderUi(<ConversationPane pipeline={pipeline as never} onNewSession={onNewSession} />);

    expect(screen.getByRole("textbox", { name: "消息" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "新建会话" }));
    expect(onNewSession).toHaveBeenCalledOnce();
  });

  it("sends a new task without blocking the composer on the long clarification call", async () => {
    let finish!: () => void;
    const sendPrompt = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const pipeline = routedPipeline({ sendPrompt });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    let box!: HTMLElement;
    await act(async () => {
      box = typeAndSend("predict churn with macro F1");
    });
    expect(sendPrompt).toHaveBeenCalledWith("predict churn with macro F1");
    expect(box).toHaveValue("");
    // The send button is not stuck in a pending state while clarification runs.
    fireEvent.change(box, { target: { value: "next" } });
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    await act(async () => finish());
  });

  it("keeps Shift+Enter as a newline and ignores Enter during IME composition", () => {
    const pipeline = routedPipeline();
    renderUi(<ConversationPane pipeline={pipeline as never} />);
    const box = screen.getByRole("textbox", { name: "消息" });

    fireEvent.change(box, { target: { value: "第一行" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });

  it("routes /stop through the confirming stop handler", async () => {
    const onStopRun = vi.fn();
    const pipeline = routedPipeline({
      runActive: true,
      viewModel: { ...routedPipeline().viewModel, status: "running", phase: "SEARCH" },
    });
    renderUi(<ConversationPane pipeline={pipeline as never} onStopRun={onStopRun} />);

    await act(async () => {
      typeAndSend("/stop");
    });
    expect(onStopRun).toHaveBeenCalledOnce();
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });

  it("suggests slash commands and completes the highlighted one", () => {
    const pipeline = routedPipeline({
      runActive: true,
      viewModel: { ...routedPipeline().viewModel, status: "running", phase: "SEARCH" },
    });
    renderUi(<ConversationPane pipeline={pipeline as never} />);
    const box = screen.getByRole("textbox", { name: "消息" });

    fireEvent.change(box, { target: { value: "/se" } });
    expect(screen.getByRole("listbox", { name: "命令" })).toHaveTextContent("/select");
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box).toHaveValue("/select ");
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });

  it("fills the composer from an example prompt instead of sending it", () => {
    const pipeline = routedPipeline({ viewModel: createEmptyPipelineViewModel() });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    fireEvent.click(screen.getByRole("button", { name: /回归预测/ }));
    expect((screen.getByRole("textbox", { name: "描述你的研究任务" }) as HTMLTextAreaElement).value).toContain(
      "RMSE",
    );
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });
});

describe("ConversationPane welcome", () => {
  it("fills the composer from an example and moves focus into it", async () => {
    const pipeline = routedPipeline({ viewModel: createEmptyPipelineViewModel() });
    renderUi(<ConversationPane pipeline={pipeline as never} />);

    fireEvent.click(screen.getByRole("button", { name: "表格二分类" }));

    const box = screen.getByRole("textbox", { name: "描述你的研究任务" }) as HTMLTextAreaElement;
    expect(box.value).toContain("二分类");
    expect(box).toHaveFocus();
    expect(box.selectionStart).toBe(box.value.length);
    expect(pipeline.sendPrompt).not.toHaveBeenCalled();
  });
});
