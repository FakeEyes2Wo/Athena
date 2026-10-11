import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HumanReply, HumanRequest } from "../../../types/ui";
import { HumanRequestDialog } from "../HumanRequestDialog";

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
  expires_at: "2026-09-01T10:02:00Z",
};

describe("HumanRequestDialog", () => {
  it("focuses the first option on open and restores focus when the request closes", async () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();

    const view = render(<HumanRequestDialog request={requestOne} onReply={reply} />);
    await waitFor(() => expect(screen.getByRole("option", { name: "F1" })).toHaveFocus());

    view.rerender(<HumanRequestDialog request={null} onReply={reply} />);
    await waitFor(() => expect(trigger).toHaveFocus());
    trigger.remove();
  });

  it("focuses the text box when a request has no choices", async () => {
    render(<HumanRequestDialog request={{ ...requestOne, choices: [] }} onReply={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Free text answer" })).toHaveFocus());
  });

  it("resets the typed answer when the request changes", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const view = render(<HumanRequestDialog request={requestOne} onReply={reply} />);
    const textbox = screen.getByRole("textbox", { name: "Free text answer" });
    fireEvent.change(textbox, { target: { value: "macro F1" } });
    expect(textbox).toHaveValue("macro F1");

    view.rerender(<HumanRequestDialog request={{ ...requestOne, request_id: "req-2" }} onReply={reply} />);
    expect(screen.getByRole("textbox", { name: "Free text answer" })).toHaveValue("");
  });

  it("has an accessible dialog name and no dismiss-only close button", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "需要你的确认" });
    // 弹窗只能通过回答关闭：除 Reply / Skip 外没有其他按钮（包括 Primer 默认的关闭按钮）。
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Reply", "Skip"]);
  });

  it("skips on Escape when skipping is allowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(reply).toHaveBeenCalledTimes(1);
    expect(reply).toHaveBeenCalledWith({ kind: "skip" });
  });

  it("ignores a backdrop click even when skipping is allowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    const backdrop = screen.getByRole("dialog").parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop);
    fireEvent.click(backdrop);
    expect(reply).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("does not skip on Escape when skipping is disallowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={{ ...requestOne, allow_skip: false }} onReply={reply} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(reply).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("moves between options with the arrow keys", async () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);

    const first = screen.getByRole("option", { name: "F1" });
    const second = screen.getByRole("option", { name: "AUC" });
    await waitFor(() => expect(first).toHaveFocus());
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(second).toHaveFocus();
    fireEvent.keyDown(second, { key: "ArrowUp" });
    expect(first).toHaveFocus();
  });

  it("makes every action unavailable while settling", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} settling />);
    expect(screen.getByRole("option", { name: "F1" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("option", { name: "AUC" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("textbox", { name: "Free text answer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reply" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skip" })).toBeDisabled();
  });

  it("submits exactly one reply per request even on rapid double clicks", () => {
    let resolveReply: (() => void) | undefined;
    const reply = vi.fn(
      (_reply: HumanReply) =>
        new Promise<void>((resolve) => {
          resolveReply = () => resolve();
        }),
    );
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    const choice = screen.getByRole("option", { name: "F1" });
    fireEvent.click(choice);
    fireEvent.click(choice);
    expect(reply).toHaveBeenCalledTimes(1);
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "f1" });
    resolveReply?.();
  });

  it("submits a typed text reply", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Free text answer" }), { target: { value: "macro F1" } });
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(reply).toHaveBeenCalledWith({ kind: "text", text: "macro F1" });
  });

  it("shows a transport error without closing the dialog", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} error="gateway disconnected" />);
    expect(screen.getByRole("alert")).toHaveTextContent("gateway disconnected");
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("keeps the dialog open and shows the failure when submitting a reply fails", async () => {
    const reply = vi.fn(async (_reply: HumanReply) => {
      throw new Error("reply rejected");
    });
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.click(screen.getByRole("option", { name: "F1" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("reply rejected");
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });
});
