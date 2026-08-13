from unittest.mock import Mock

import pytest

from athena.core.agent.session import RunSession
from athena.core.agent.types import AgentMessage
from athena.memory.context_manager import ContextManager


def test_session_view_exposes_runtime_contract():
    memory = ContextManager()
    mailbox = [AgentMessage(source="user", content="hi", context_refs=[])]
    runtime = Mock()
    sess = RunSession(
        agent_id="a1",
        runtime=runtime,
        context_ref="art:ctx",
        memory=memory,
        mailbox=mailbox,
    )
    assert sess.agent_id == "a1"
    assert sess.runtime is runtime
    assert sess.context_ref == "art:ctx"
    assert sess.memory.raw is memory  # BaseAgentRunner 依赖 .raw
    unread = sess.receive_messages()
    assert [m.content for m in unread] == ["hi"]
    # receive_messages 只读不删：失败 turn 的未读消息须保留待重试
    assert [m.content for m in sess.receive_messages()] == ["hi"]
    sess.checkpoint()  # 提交消费 → 清空 mailbox
    assert sess.receive_messages() == []
    with pytest.raises(AttributeError):
        sess.memory.append(None)  # 只读视图禁止写
