import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { UIMessage } from "../../../types/ui";
import { MessageList } from "../MessageList";

function transcript(count: number): UIMessage[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `m${i}`,
    role: "athena" as const,
    kind: "text" as const,
    content: `line ${i}`,
  }));
}

const noop = () => undefined;

function renderList(messages: UIMessage[]) {
  return render(
    <MessageList
      messages={messages}
      onConfirmPreview={noop}
      onRevisePreview={noop}
      onRetryPreview={noop}
      onCancelPreview={noop}
    />,
  );
}

describe("MessageList windowing", () => {
  it("renders short transcripts in full", () => {
    renderList(transcript(5));
    expect(screen.getByText("line 0")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /显示更早的消息/ })).not.toBeInTheDocument();
  });

  it("mounts only the tail of a long restored transcript and pages earlier rows in", () => {
    renderList(transcript(500));
    expect(screen.getByText("line 499")).toBeInTheDocument();
    expect(screen.getByText("line 380")).toBeInTheDocument();
    expect(screen.queryByText("line 379")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "显示更早的消息（还有 380 条）" }));

    expect(screen.getByText("line 260")).toBeInTheDocument();
    expect(screen.queryByText("line 259")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "显示更早的消息（还有 260 条）" })).toBeInTheDocument();
  });

  it("shows a shorter transcript in full after a long one", () => {
    const { rerender } = renderList(transcript(500));
    rerender(
      <MessageList
        messages={transcript(3)}
        onConfirmPreview={noop}
        onRevisePreview={noop}
        onRetryPreview={noop}
        onCancelPreview={noop}
      />,
    );
    expect(screen.getByText("line 0")).toBeInTheDocument();
    expect(screen.getByText("line 2")).toBeInTheDocument();
  });
});

describe("MessageList task-understanding notices", () => {
  const failedCard: UIMessage = {
    id: "card",
    role: "athena",
    kind: "intent-preview",
    content: "",
    preview: {
      draftId: "clarify_1",
      revision: 0,
      status: "FAILED",
      understanding: {
        title: "t",
        dataset: null,
        target: null,
        task_type: "other",
        primary_metric: null,
        direction: null,
        evaluation_plan: null,
      },
      answers: [],
      unresolved: [],
      failure: { code: "provider_failure", message: "clarification generation failed", retryable: true },
    },
  };
  const notice = (scopeId: string): UIMessage => ({
    id: `notice-${scopeId}`,
    role: "athena",
    kind: "error",
    channel: "error",
    content: "Task understanding failed. Retry to continue.",
    scope: "task_understanding",
    scopeId,
  });

  it("folds a failure notice into the card for the same draft", () => {
    renderList([failedCard, notice("clarify_1")]);
    expect(screen.queryByText("Task understanding failed. Retry to continue.")).not.toBeInTheDocument();
  });

  it("keeps a notice that has no card to live in", () => {
    renderList([failedCard, notice("clarify_other")]);
    expect(screen.getByText("Task understanding failed. Retry to continue.")).toBeInTheDocument();
  });
});
