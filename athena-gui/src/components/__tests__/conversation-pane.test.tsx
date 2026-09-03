import { describe, expect, it, vi } from "vitest";
import { act, screen, fireEvent, within } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ConversationPane } from "../conversation/ConversationPane";
import {
  createEmptyPipelineViewModel,
  type ClarificationStatus,
  type UIMessage,
} from "../../types/ui";

function clarificationPreview(status: ClarificationStatus, id: string): UIMessage {
  return {
    id,
    role: "athena",
    kind: "intent-preview",
    content: status === "CLARIFYING" ? "任务理解中…" : "任务理解完成",
    started: status === "RUNNING",
    preview: {
      draftId: id,
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
    expect(screen.getByText("classification")).toBeInTheDocument();
    expect(screen.getByText("f1_macro · maximize")).toBeInTheDocument();
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
      fireEvent.click(screen.getByRole("button", { name: /确认并启动/i }));
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
    expect(screen.queryByRole("button", { name: /确认并启动/i })).not.toBeInTheDocument();
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
