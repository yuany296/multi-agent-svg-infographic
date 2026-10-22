"""审查 agent —— 团队里负责挑毛病、并决定能不能交付的那一个。

它的意见会直接出现在对话里，画图员看得见；它也可能被画图员说服而收回意见。
最后由它说出交付信号，协作才结束。
"""

from __future__ import annotations

from pathlib import Path

from autogen_agentchat.agents import AssistantAgent

from agents.studio import Studio
from utils.config import get_model_client

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "reviewer.md"


def build_reviewer(studio: Studio) -> AssistantAgent:
    """构造审查 agent。

    温度调低，因为审查需要稳定的判断，不需要发散的创意。
    """

    async def quick_check() -> str:
        """跑一遍确定性检查：画布尺寸、边界越界、元素重叠、文字完整性。

        很快，不调用视觉模型。用来先筛掉硬性错误。
        """
        return studio.quick_check()

    async def inspect_with_vision() -> str:
        """完整检查：在确定性检查之外，还会把图渲染成图片交给视觉模型评判。

        慢一些，但能看到排版观感、图标是否清晰、箭头方向是否合理这类问题。
        """
        return await studio.inspect_with_vision()

    async def approve() -> str:
        """宣布当前这一版可以交付，本次协作到此结束。

        这是唯一的交付信号 —— 它一被调用，协作立刻停止。
        只有你确认没有任何必须修改的问题时才调用它。

        如果你还不放心，或者还有问题要提，就不要调用，直接在回复里说清楚。
        """
        return "已记录：审查通过，本次协作结束。"

    return AssistantAgent(
        name="Reviewer",
        model_client=get_model_client(temperature=0.3),
        system_message=_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=[quick_check, inspect_with_vision, approve],
    )
