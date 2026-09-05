import { describe, expect, it } from "vitest"
import { AgentMessage } from "../../src/agent/types.js"
import { RunSession } from "../../src/agent/session.js"
import { ContextManager } from "../../src/memory/context-manager.js"

describe("RunSession", () => {
  it("session view exposes runtime contract", () => {
    const memory = new ContextManager()
    const mailbox = [new AgentMessage("user", "hi", [])]
    const runtime = {}
    const sess = new RunSession("a1", runtime, "art:ctx", memory, mailbox)

    expect(sess.agentId).toBe("a1")
    expect(sess.runtime).toBe(runtime)
    expect(sess.contextRef).toBe("art:ctx")
    expect(sess.memory).toBe(memory)

    const unread = sess.receiveMessages()
    expect(unread.map((m) => m.content)).toEqual(["hi"])
    unread.length = 0
    // receive_messages 只读不删：失败 turn 的未读消息须保留待重试
    expect(sess.receiveMessages().map((m) => m.content)).toEqual(["hi"])
    sess.checkpoint() // 提交消费 → 清空 mailbox
    expect(sess.receiveMessages()).toEqual([])
  })
})
