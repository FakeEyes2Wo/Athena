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
              task_type: "classification",
              data_type: "tabular",
              target_vars: ["target"],
              primary_metric: "f1_macro",
              direction: "maximize",
              needs_configuration: true,
            },
          },
        ],
      },
      sendPrompt,
      startRun,
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByText("Athena")).toBeInTheDocument();
    expect(screen.getByText("analyze this CSV")).toBeInTheDocument();
    expect(screen.getByText(/任务类型: classification/i)).toBeInTheDocument();
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
              task_type: "classification",
              data_type: "tabular",
              target_vars: ["target"],
              primary_metric: "f1_macro",
              direction: "maximize",
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
});
