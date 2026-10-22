"""画图 agent —— 团队里负责产出和修改 SVG 的那一个。

它不再只被「问一次」，而是会持续收到审查员的意见、自己决定怎么改，
并在每次提交后说明自己改了什么。
"""

from __future__ import annotations

from pathlib import Path

from autogen_agentchat.agents import AssistantAgent

from agents.studio import Studio
from utils.config import get_model_client

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "painter.md"


def build_painter(studio: Studio) -> AssistantAgent:
    """构造画图 agent。

    三个工具都用闭包绑定到共享工作台：agent 只需要知道「调哪个工具、传什么」，
    不需要知道工作台内部存了什么。
    """

    async def lookup_icon(query: str, stage_id: str, kind: str = "general") -> str:
        """按名字查一个图标，并登记到指定的节点上。

        Args:
            query: 图标查询词，用英文小写，例如 "coffee"、"sprout"、"youtube"。
            stage_id: 这个图标属于哪个节点的 id。
            kind: "general" 查通用图标；"brand" 查品牌图标（YouTube、TikTok 这类）。
        """
        return await studio.lookup_icon(query, stage_id=stage_id, kind=kind)

    async def submit_svg(svg_code: str, note: str = "") -> str:
        """提交一个完整版本的 SVG。系统会自动注入图标并做连线标签归位。

        Args:
            svg_code: 完整的 SVG 代码，包含 <svg> 标签。
            note: 这一版改了什么，用一两句话说明。
        """
        return await studio.submit_svg(svg_code, note=note)

    async def revise_svg(instructions: str) -> str:
        """在当前版本上打局部补丁。只改有问题的部分，比重画整张快得多。

        Args:
            instructions: 要改什么，写清楚位置和改法。
        """
        return await studio.revise_svg(instructions)

    return AssistantAgent(
        name="Painter",
        model_client=get_model_client(temperature=0.7),
        system_message=_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=[lookup_icon, submit_svg, revise_svg],
    )
