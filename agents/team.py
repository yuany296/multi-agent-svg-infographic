"""把画图员和审查员组成一个会对话的团队。

这是「多 agent」真正发生的地方：两个人轮流发言，彼此看得见对方说的话，
不需要 Python 在中间转述。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import FunctionCallTermination, MaxMessageTermination
from autogen_agentchat.teams import RoundRobinGroupChat

from agents.code_generator import _build_prompt
from agents.painter import build_painter
from agents.reviewer import build_reviewer
from agents.studio import Studio

# 宣布交付的工具名。审查员调用它，就代表这一版可以交付、协作结束。
#
# 为什么用「调用工具」而不是「文字里出现某个词」：
# 文本匹配太容易误判 —— 推理模型的思考过程里会提到它，正式发言的否定句里
# 也会提到它（「我不能说【审核通过】」）。实测这两种情况都会让系统误判成
# 通过并提前结束协作。工具调用没有这个歧义。
APPROVE_TOOL = "approve"

# 兜底上限。AutoGen 默认只数正式发言，不数思考过程和工具事件。
DEFAULT_MAX_MESSAGES = 20


def build_team(
    studio: Studio,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> RoundRobinGroupChat:
    """构造「画图 ⇄ 审查」轮流发言的团队。"""
    return RoundRobinGroupChat(
        participants=[build_painter(studio), build_reviewer(studio)],
        termination_condition=(
            FunctionCallTermination(APPROVE_TOOL)
            | MaxMessageTermination(max_messages)
        ),
    )


def build_kickoff(
    task: str,
    *,
    analysis: dict,
    structure: dict,
    style: dict,
    icons: list[dict],
) -> str:
    """把前置阶段的全部结论缝成一段开场简报，作为群聊的第一条消息。

    前置阶段每个环节只跑一次、结论冻结，所以它们不需要进群聊；
    它们的产出通过这段简报进入对话。

    简报直接复用 code_generator 的提示词组装逻辑，确保坐标、模板槽位、
    渲染契约这些硬约束一条不漏地交到画图员手上。
    """
    brief = _build_prompt(
        task,
        analysis.get("stages", []),
        structure,
        style,
        icons,
    )
    return "\n".join(
        [
            "【协作说明】",
            "你是这个团队的画图员，产出后由审查员检查，你们会来回几轮，直到它认可为止。",
            "下面给出的坐标、模板槽位、渲染契约都是硬约束，请严格按它们绘制，不要自己发明位置。",
            "",
            brief,
            "",
            "请据此产出第一版，用 submit_svg 提交；提交后用一两句话说明你画了什么。",
        ]
    )


async def run_studio(
    studio: Studio,
    kickoff: str,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    on_activity: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """启动协作，一直跑到审查员宣布交付或用完轮次。

    on_activity 会收到一句句的过程描述（谁在做什么），
    这样界面在等待几分钟的过程中不会一片空白。

    返回的 messages 是完整协作记录，可以直接拿给界面显示。
    """
    team = build_team(studio, max_messages=max_messages)
    messages: list[dict[str, Any]] = []
    stop_reason = ""

    async for event in team.run_stream(task=kickoff):
        if isinstance(event, TaskResult):
            stop_reason = str(event.stop_reason)
            break
        message = _message_to_dict(event)
        if not message:
            continue
        messages.append(message)
        if on_activity is not None:
            description = _describe_activity(message)
            if description:
                try:
                    on_activity(description)
                except Exception:  # 界面回调失败不能影响协作
                    logging.warning("[team] 过程回调执行失败", exc_info=True)

    return {
        "svg": studio.current_svg,
        "versions": studio.versions,
        "messages": messages,
        "stop_reason": stop_reason,
        "last_issues": studio.last_issues,
    }


_ROLE_LABELS = {"Painter": "画图员", "Reviewer": "审查员", "user": "开场简报"}

_TOOL_LABELS = {
    "lookup_icon": "查图标",
    "submit_svg": "提交一个新版本",
    "revise_svg": "打补丁修改",
    "quick_check": "跑确定性检查",
    "inspect_with_vision": "做视觉检查",
    "approve": "宣布交付",
}


def _describe_activity(message: dict[str, Any]) -> str:
    """把一条消息翻译成一句人能看懂的进度描述；不重要的事件返回空串。"""
    kind = str(message.get("type", ""))
    source = str(message.get("source", ""))
    role = _ROLE_LABELS.get(source, source)

    if kind == "ToolCallRequestEvent":
        content = message.get("content")
        items = content if isinstance(content, list) else []
        names = [
            str(item.get("name"))
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]
        if names:
            label = _TOOL_LABELS.get(names[0], names[0])
            return f"{role} → {label}"
        return ""

    if kind == "TextMessage" and source in {"Painter", "Reviewer"}:
        return f"{role}发言完毕"

    return ""


def _message_to_dict(message: Any) -> dict[str, Any]:
    """把消息对象转成可序列化的字典，便于界面显示与落盘。"""
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    source = getattr(message, "source", None)
    if source is None:
        return {}
    return {
        "type": type(message).__name__,
        "source": source,
        "content": str(getattr(message, "content", "")),
    }
