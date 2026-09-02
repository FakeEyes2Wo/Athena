import { act, fireEvent, render, screen } from "@testing-library/react";
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

    expect(screen.getByRole("button", { name: /confirm and start/i })).toBeDisabled();
    act(() => {
      fireEvent.click(screen.getByRole("checkbox", { name: /acknowledge unresolved/i }));
    });
    expect(screen.getByRole("button", { name: /confirm and start/i })).toBeEnabled();
  });

  it("passes the acknowledgement flag to onConfirm", async () => {
    const confirm = vi.fn(async (_acknowledge: boolean) => undefined);
    render(
      <IntentPreviewCard preview={criticalPreview} onConfirm={confirm} onRevise={noop} onRetry={noop} onCancel={noop} />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /acknowledge unresolved/i }));
      fireEvent.click(screen.getByRole("button", { name: /confirm and start/i }));
      await Promise.resolve();
    });
    expect(confirm).toHaveBeenCalledWith(true);
  });

  it("shows Retry and Cancel for a FAILED draft", () => {
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

    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
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

    expect(screen.getByText(/Revision 4/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm and start/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /revise/i })).not.toBeInTheDocument();
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

    const reviseButton = screen.getByRole("button", { name: "Revise" });
    expect(reviseButton).toBeDisabled();
    const textarea = screen.getByLabelText(/revise with instruction/i);
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
