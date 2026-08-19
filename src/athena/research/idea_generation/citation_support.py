"""引用支持性判定：这段被引的正文，到底支不支持这条主张。

核验一路收紧过两次。第一版查"这个 paper id 在不在语料里"，太松——被引的论文确实在，
只是从没被打开。第二版改成"本轮真的用 ``paper_chunk_read`` 打开过"，拦住了贴标签式引用。
但两版查的都是**行为**，不是**内容**：一个读过《数据增强综述》再拿它去支持"两两交互
特征工程"的 Ideator，两版都拦不住，而那正是真机上实际发生的事。

这一层查内容。对每条 ``sources``，把被引论文里 Ideator 真读过的那几段正文交给模型，
问一个二选一的问题：这段话支持这条主张吗。不支持就把这条引用丢掉。

**判据刻意保守**：模棱两可算不支持。一条被误删的真引用只是少了一份可追溯性；一条被
放行的假引用会让下游把一个没有根据的干预当成有据可依的——而这条链路已经为后者付过一次
学费（``rubric_prior`` 曾经给"有引用"加 0.12 分，买到的全是装饰）。
"""

import json
import re
from dataclasses import dataclass

JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
EVIDENCE_CHARS = 2400
MAX_EVIDENCE_CHUNKS = 4

SUPPORT_PROMPT = """You are checking one citation in a research hypothesis. Decide \
whether the quoted passages actually support the claim being made — not whether the \
claim is true, and not whether the paper is on topic.

Claim:
{claim}

Intervention this hypothesis proposes:
{intervention}

Passages the proposer read from paper `{paper_id}`:
{evidence}

Answer with one JSON object and nothing else:
{{"supports": true|false, "why": "one clause"}}

Say true only if these passages give a reader a concrete reason to expect the \
intervention to help. A paper that merely shares a topic, surveys the area, or \
studies a different intervention does NOT support the claim. When you are unsure, \
answer false."""


@dataclass(frozen=True, slots=True)
class SupportVerdict:
    """一条引用的判定结果。"""

    paper_id: str
    supports: bool
    why: str = ""


def parse_verdict(paper_id: str, content: str) -> SupportVerdict:
    """解析判定回复；读不出来一律判**不支持**。

    解析失败往保守一侧倒：判错成"不支持"只丢一份可追溯性，判错成"支持"会让一个没有
    根据的干预看起来有据可依。
    """
    match = JSON_OBJECT.search(content or "")
    if match is None:
        return SupportVerdict(paper_id, False, "unparseable verdict")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return SupportVerdict(paper_id, False, "unparseable verdict")
    if not isinstance(payload, dict):
        return SupportVerdict(paper_id, False, "unparseable verdict")
    return SupportVerdict(
        paper_id,
        payload.get("supports") is True,
        str(payload.get("why", "")).strip()[:200],
    )


def format_evidence(passages: list[str]) -> str:
    """把读过的正文拼成证据段，按篇幅截断。

    取前几段而不是全部：一篇论文可能被读了十几个 chunk，整段塞进去既贵又会把判断稀释成
    "这篇论文大体上相关吗"——而那正是要避免的那个问题。
    """
    kept = [item.strip() for item in passages if item.strip()][:MAX_EVIDENCE_CHUNKS]
    if not kept:
        return "(no passages were read from this paper)"
    joined = "\n\n---\n\n".join(kept)
    if len(joined) <= EVIDENCE_CHARS:
        return joined
    return joined[:EVIDENCE_CHARS].rstrip() + " …"
