import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HumanReply, HumanRequest } from "../../../types/ui";
import { HumanRequestPanel } from "../HumanRequestPanel";

const requestOne: HumanRequest = {
  request_id: "req-1",
  session_id: "s-1",
  scope_id: "draft-1",
  scope_kind: "clarification",
  prompt: "Choose metric",
  choices: [
    { label: "F1", value: "f1" },
    { label: "AUC", value: "auc" },
  ],
  allow_custom: true,
  allow_skip: true,
  created_at: "2026-09-01T10:00:00Z",
  expires_at: "2099-09-01T10:02:00Z",
};

describe("HumanRequestPanel", () => {
  it("has an accessible name and shows the prompt", () => {
    render(<HumanRequestPanel request={requestOne} onReply={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要你的确认" })).toHaveTextContent("Choose metric");
    expect(screen.getByText("选择一项，或在下方输入框中输入其他回答")).toBeInTheDocument();
  });

  it("submits a choice", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestPanel request={requestOne} onReply={reply} />);

    fireEvent.click(screen.getByRole("button", { name: "AUC" }));
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "auc" });
  });

  it("submits a skip only when skipping is allowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const view = render(<HumanRequestPanel request={requestOne} onReply={reply} />);

    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(reply).toHaveBeenCalledWith({ kind: "skip" });

    view.rerender(
      <HumanRequestPanel request={{ ...requestOne, request_id: "req-2", allow_skip: false }} onReply={reply} />,
    );
    expect(screen.queryByRole("button", { name: "跳过" })).not.toBeInTheDocument();
  });

  it("supports arrow-key navigation and number shortcuts between choices", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestPanel request={requestOne} onReply={reply} />);

    const first = screen.getByRole("button", { name: "F1" });
    const second = screen.getByRole("button", { name: "AUC" });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(second).toHaveFocus();
    fireEvent.keyDown(second, { key: "ArrowUp" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "2" });
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "auc" });
  });

  it("disables all actions while settling", () => {
    render(<HumanRequestPanel request={requestOne} onReply={vi.fn()} settling />);
    expect(screen.getByRole("button", { name: "F1" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "AUC" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过" })).toBeDisabled();
  });

  it("submits exactly one reply per request even on rapid double clicks", () => {
    let resolveReply: (() => void) | undefined;
    const reply = vi.fn(
      (_reply: HumanReply) =>
        new Promise<void>((resolve) => {
          resolveReply = () => resolve();
        }),
    );
    render(<HumanRequestPanel request={requestOne} onReply={reply} />);

    const choiceButton = screen.getByRole("button", { name: "F1" });
    fireEvent.click(choiceButton);
    fireEvent.click(choiceButton);
    expect(reply).toHaveBeenCalledTimes(1);
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "f1" });
    resolveReply?.();
  });

  it("shows a rejected reply and the transport error without dismissing", async () => {
    const reply = vi.fn(async () => {
      throw new Error("该问题已失效");
    });
    const view = render(<HumanRequestPanel request={requestOne} onReply={reply} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "F1" }));
    });
    expect(screen.getByRole("alert")).toHaveTextContent("该问题已失效");

    view.rerender(<HumanRequestPanel request={requestOne} onReply={reply} error="无法获取待确认问题" />);
    expect(screen.getByRole("alert")).toHaveTextContent("无法获取待确认问题");
  });

  it("resets local errors when the request changes", async () => {
    const reply = vi.fn(async () => {
      throw new Error("stale");
    });
    const view = render(<HumanRequestPanel request={requestOne} onReply={reply} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "F1" }));
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();

    view.rerender(<HumanRequestPanel request={{ ...requestOne, request_id: "req-2" }} onReply={reply} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("counts down to the request expiry", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-09-01T10:00:00Z"));
      render(<HumanRequestPanel request={{ ...requestOne, expires_at: "2026-09-01T10:02:00Z" }} onReply={vi.fn()} />);
      expect(screen.getByText("2:00 后超时")).toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(61_000);
      });
      expect(screen.getByText("0:59 后超时")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("asks for a choice when free text is not allowed", () => {
    render(<HumanRequestPanel request={{ ...requestOne, allow_custom: false }} onReply={vi.fn()} />);
    expect(screen.getByText("请选择一项回答")).toBeInTheDocument();
  });
});
