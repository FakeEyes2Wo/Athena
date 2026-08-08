import pytest

from athena.core.agent.session import RunSession
from athena.core.agent.types import AgentMessage
from athena.memory.context_manager import ContextManager


def test_session_view_exposes_kernel_contract():
    memory = ContextManager()
    mailbox = [AgentMessage(source="user", content="hi", context_refs=[])]
    kernel = object()
    sess = RunSession(
        agent_id="a1",
        kernel=kernel,
        context_ref="art:ctx",
        memory=memory,
        mailbox=mailbox,
    )
    assert sess.agent_id == "a1"
    assert sess.kernel is kernel
    assert sess.context_ref == "art:ctx"
    assert sess.memory.raw is memory  # BaseAgentRunner 依赖 .raw
    unread = sess.receive_messages()
    assert [m.content for m in unread] == ["hi"]
    assert sess.receive_messages() == []  # 读即清(=checkpoint)
    sess.checkpoint()  # 空操作,不抛
    with pytest.raises(AttributeError):
        sess.memory.append(None)  # 只读视图禁止写
