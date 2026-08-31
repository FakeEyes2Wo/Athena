import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import type { UIMessage } from "../../types/ui";

/** 计数用的 ErrorCard 替身：一条消息行重新渲染一次，这里就 +1。 */
const rendered = vi.hoisted(() => ({ count: 0 }));

vi.mock("../cards/ErrorCard", () => ({
  ErrorCard: ({ content }: { content: string }) => {
    rendered.count += 1;
    return <div>{content}</div>;
  },
}));

import { MessageList } from "../conversation/MessageList";

function makeMessages(count: number, kind: UIMessage["kind"] = "text"): UIMessage[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `m-${i}`,
    role: "athena" as const,
    kind,
    content: `消息 ${i}`,
    source: "agent",
    plan: "evaluator",
  }));
}

describe("MessageList", () => {
  beforeEach(() => {
    rendered.count = 0;
  });

  it("says so when the backend truncated the replayed history", () => {
    renderUi(
      <MessageList
        messages={makeMessages(2)}
        truncatedCount={21791}
        onStartRun={vi.fn()}
      />,
    );

    expect(
      screen.getByText("更早的 21791 条记录未载入（仍完整保存在磁盘上）"),
    ).toBeInTheDocument();
  });

  it("stays quiet when the whole transcript came through", () => {
    renderUi(<MessageList messages={makeMessages(2)} onStartRun={vi.fn()} />);

    expect(screen.queryByText(/未载入/)).not.toBeInTheDocument();
  });

  it("only re-renders the message that changed", () => {
    // 流式输出每来一个 delta 就更新一次 view model，而 reducer 只替换变了的那一条：
    // 消息行不 memo 的话，每个 delta 都要把整张列表重新走一遍。长会话下这是主要的
    // 渲染开销，所以这里直接数消息行被调用了多少次。
    const onStartRun = vi.fn();
    const messages = makeMessages(500, "error");
    const { rerender } = renderUi(
      <MessageList messages={messages} onStartRun={onStartRun} />,
    );
    expect(rendered.count).toBe(500);

    rendered.count = 0;
    const next = [...messages];
    next[next.length - 1] = { ...next[next.length - 1], content: "改了最后一条" };
    rerender(<MessageList messages={next} onStartRun={onStartRun} />);

    expect(rendered.count).toBe(1);
    expect(screen.getByText("改了最后一条")).toBeInTheDocument();
  });
});
