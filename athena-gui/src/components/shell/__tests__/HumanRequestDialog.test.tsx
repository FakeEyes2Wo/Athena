import { fireEvent, render, screen } from "@testing-library/react";
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
  it("resets text and restores focus when request changes or closes", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const requestTwo = { ...requestOne, request_id: "req-2" };
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();

    const view = render(<HumanRequestDialog request={requestOne} onReply={reply} />);
    const textbox = screen.getByRole("textbox");
    fireEvent.change(textbox, { target: { value: "macro F1" } });
    expect(textbox).toHaveValue("macro F1");
    view.rerender(<HumanRequestDialog request={requestTwo} onReply={reply} />);
    expect(screen.getByRole("textbox")).toHaveValue("");
    view.rerender(<HumanRequestDialog request={null} onReply={reply} />);
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("has an accessible dialog name", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("skips on Escape when skipping is allowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(reply).toHaveBeenCalledWith({ kind: "skip" });
  });

  it("does not skip on Escape when skipping is disallowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(
      <HumanRequestDialog request={{ ...requestOne, allow_skip: false }} onReply={reply} />,
    );

    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(reply).not.toHaveBeenCalled();
  });

  it("traps Tab focus within the dialog and cycles", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);

    const first = screen.getByRole("button", { name: "F1" });
    const second = screen.getByRole("button", { name: "AUC" });
    const textbox = screen.getByRole("textbox");
    first.focus();
    fireEvent.keyDown(first, { key: "Tab" });
    expect(second).toHaveFocus();
    fireEvent.keyDown(second, { key: "Tab" });
    expect(textbox).toHaveFocus();
    // textbox -> Skip (Reply is disabled while empty) -> wrap to first.
    fireEvent.keyDown(textbox, { key: "Tab" });
    fireEvent.keyDown(screen.getByRole("button", { name: "Skip" }), { key: "Tab" });
    expect(first).toHaveFocus();
  });

  it("supports arrow-key choice navigation", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);

    const first = screen.getByRole("button", { name: "F1" });
    const second = screen.getByRole("button", { name: "AUC" });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(second).toHaveFocus();
    fireEvent.keyDown(second, { key: "ArrowUp" });
    expect(first).toHaveFocus();
  });

  it("disables all actions while settling", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} settling />);
    expect(screen.getByRole("button", { name: "F1" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "AUC" })).toBeDisabled();
    expect(screen.getByRole("textbox")).toBeDisabled();
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

    const choiceButton = screen.getByRole("button", { name: "F1" });
    fireEvent.click(choiceButton);
    fireEvent.click(choiceButton);
    expect(reply).toHaveBeenCalledTimes(1);
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "f1" });
    resolveReply?.();
  });

  it("submits a typed text reply", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "macro F1" } });
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(reply).toHaveBeenCalledWith({ kind: "text", text: "macro F1" });
  });
});
