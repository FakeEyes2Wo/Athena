import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ClarificationPreview } from "../../../types/ui";
import { IntentPreviewCard } from "../IntentPreviewCard";

const noop = vi.fn(async () => undefined);

const criticalPreview: ClarificationPreview = {
  draftId: "draft-1",
  revision: 2,
  status: "READY_FOR_CONFIRMATION",
  understanding: {
    title: "Predict churn",
    dataset: null,
    target: null,
    task_type: "classification",
    primary_metric: null,
    direction: null,
    evaluation_plan: null,
  },
  answers: [],
  unresolved: [{ field: "target", reason: "not supplied", critical: true }],
  failure: null,
};

function readyPreview(overrides: Partial<ClarificationPreview> = {}): ClarificationPreview {
  return { ...criticalPreview, ...overrides };
}

describe("IntentPreviewCard", () => {
  it("requires acknowledgement before confirming critical unresolved items", async () => {
    const confirm = vi.fn(async (_acknowledge: boolean) => undefined);
    render(
      <IntentPreviewCard preview={criticalPreview} onConfirm={confirm} onRevise={noop} onRetry={noop} onCancel={noop} />,
    );

    expect(screen.getByRole("button", { name: "确认并开始研究" })).toBeDisabled();
    act(() => {
      fireEvent.click(screen.getByRole("checkbox", { name: /已了解未确定项/ }));
    });
    expect(screen.getByRole("button", { name: "确认并开始研究" })).toBeEnabled();
  });

  it("passes the acknowledgement flag to onConfirm", async () => {
    const confirm = vi.fn(async (_acknowledge: boolean) => undefined);
    render(
      <IntentPreviewCard preview={criticalPreview} onConfirm={confirm} onRevise={noop} onRetry={noop} onCancel={noop} />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /已了解未确定项/ }));
      fireEvent.click(screen.getByRole("button", { name: "确认并开始研究" }));
      await Promise.resolve();
    });
    expect(confirm).toHaveBeenCalledWith(true);
  });

  it("shows retry and cancel for a FAILED draft", () => {
    render(
      <IntentPreviewCard
        preview={readyPreview({
          status: "FAILED",
          failure: { code: "provider_unavailable", message: "LLM down", retryable: true },
        })}
        onConfirm={noop}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
    expect(screen.getByText("LLM down")).toBeInTheDocument();
  });

  it("is read-only while RUNNING and displays the committed revision", () => {
    render(
      <IntentPreviewCard
        preview={readyPreview({ status: "RUNNING", revision: 4, unresolved: [] })}
        onConfirm={noop}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    expect(screen.getByText("已启动")).toBeInTheDocument();
    expect(screen.getByText(/版本 4/)).toBeInTheDocument();
    expect(screen.getByText(/研究已启动/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认并开始研究" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "修改" })).not.toBeInTheDocument();
  });

  it("requires a trimmed revision instruction before calling onRevise", async () => {
    const revise = vi.fn(async () => undefined);
    render(
      <IntentPreviewCard
        preview={readyPreview({ unresolved: [] })}
        onConfirm={noop}
        onRevise={revise}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "修改" }));
    });
    const reviseButton = screen.getByRole("button", { name: "提交修改" });
    expect(reviseButton).toBeDisabled();
    const textarea = screen.getByLabelText("修改意见");
    act(() => {
      fireEvent.change(textarea, { target: { value: "  add target  " } });
    });
    expect(reviseButton).toBeEnabled();
    await act(async () => {
      fireEvent.click(reviseButton);
      await Promise.resolve();
    });
    expect(revise).toHaveBeenCalledWith("add target");
  });
});

describe("IntentPreviewCard presentation", () => {
  it("localizes task type and metric direction", () => {
    render(
      <IntentPreviewCard
        preview={readyPreview({
          unresolved: [],
          understanding: {
            ...criticalPreview.understanding,
            task_type: "regression",
            primary_metric: "rmse",
            direction: "minimize",
          },
        })}
        onConfirm={noop}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    expect(screen.getByText("回归")).toBeInTheDocument();
    expect(screen.getByText("rmse · 越小越好")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认并开始研究" })).toBeEnabled();
  });

  it("keeps the card usable when an action rejects", async () => {
    const confirm = vi.fn(async () => {
      throw new Error("backend down");
    });
    render(
      <IntentPreviewCard
        preview={readyPreview({ unresolved: [] })}
        onConfirm={confirm}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认并开始研究" }));
      await Promise.resolve();
    });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "确认并开始研究" })).toBeEnabled();
  });
});

describe("IntentPreviewCard wording", () => {
  it("names unresolved fields and reasons in Chinese", () => {
    render(
      <IntentPreviewCard preview={readyPreview()} onConfirm={noop} onRevise={noop} onRetry={noop} onCancel={noop} />,
    );
    const list = screen.getByRole("list");
    expect(within(list).getByText("目标")).toBeInTheDocument();
    expect(within(list).getByText("未提供")).toBeInTheDocument();
    expect(screen.queryByText("not supplied")).not.toBeInTheDocument();
  });

  it("explains a provider failure instead of listing every field as unresolved", () => {
    render(
      <IntentPreviewCard
        preview={readyPreview({
          status: "FAILED",
          understanding: { ...criticalPreview.understanding, task_type: "other" },
          unresolved: [
            { field: "dataset", reason: "not supplied", critical: true },
            { field: "primary_metric", reason: "not supplied", critical: true },
          ],
          failure: { code: "provider_failure", message: "clarification generation failed", retryable: true },
        })}
        onConfirm={noop}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );
    expect(screen.getByText(/设置 → 模型连接/)).toBeInTheDocument();
    expect(screen.queryByText("尚未确定的信息")).not.toBeInTheDocument();
    expect(screen.queryByText("数据集")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
