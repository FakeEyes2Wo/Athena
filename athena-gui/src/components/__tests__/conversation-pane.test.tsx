import { describe, expect, it, vi } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ConversationPane } from "../conversation/ConversationPane";
import { createEmptyPipelineViewModel } from "../../types/ui";

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

    fireEvent.click(screen.getByRole("button", { name: /确认并启动/i }));
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
});
