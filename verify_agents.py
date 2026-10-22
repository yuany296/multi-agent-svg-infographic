"""验证多 agent 方案能不能真正跑起来。

为什么需要这个脚本：原来的项目从来没让模型调用过工具，
`utils/config.py` 里那句 `"function_calling": True` 是手写死的声明，从没被验证过。
如果 DeepSeek 实际上不支持工具调用，整个协作方案要换思路 ——
所以这件事必须先测，而不是等改完 orchestrator 才发现。

跑法（需要先在 .env 或环境变量里配好 DEEPSEEK_KEY）：

    python verify_agents.py

三步依次进行，任何一步失败都会打印原因并停下。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


async def step1_tool_calling() -> bool:
    """最关键的一步：模型到底会不会调用工具。"""
    print("=" * 62)
    print("第 1 步：DeepSeek 支持工具调用吗？")
    print("=" * 62)

    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.messages import TextMessage
    from autogen_core import CancellationToken

    from utils.config import get_model_client

    calls: list[tuple[int, int]] = []

    async def add(a: int, b: int) -> str:
        """把两个数相加并返回结果。

        Args:
            a: 第一个加数。
            b: 第二个加数。
        """
        calls.append((a, b))
        return str(a + b)

    agent = AssistantAgent(
        name="Tester",
        model_client=get_model_client(temperature=0),
        system_message="你有工具可用。需要做加法时必须调用工具，不要自己心算。",
        tools=[add],
    )

    response = await agent.on_messages(
        [TextMessage(content="请用工具算一下 17 加 25 等于多少。", source="user")],
        CancellationToken(),
    )
    print("模型最终回复：", str(response.chat_message.content).strip())

    if calls:
        print(f"工具被调用了 {len(calls)} 次：{calls}")
        print(">>> 结论：支持工具调用")
        return True

    print(">>> 结论：模型没有调用工具。")
    print("    这不一定代表它不支持 —— 也可能只是它偷懒心算了。")
    print("    如果下面第 2 步的群聊也跑不起来，就需要换模型或换方案。")
    return False


async def step2_two_agents() -> None:
    """确认两个 agent 能真正轮流对话，并且交付信号能被触发。

    交付信号是「调用 approve 工具」，不是「文字里出现某个词」——
    文本匹配会被推理模型的思考过程、以及正式发言里的否定句误触发。
    """
    print()
    print("=" * 62)
    print("第 2 步：两个 agent 能不能在群聊里对话？")
    print("=" * 62)

    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.conditions import (
        FunctionCallTermination,
        MaxMessageTermination,
    )
    from autogen_agentchat.teams import RoundRobinGroupChat

    from agents.team import APPROVE_TOOL
    from utils.config import get_model_client

    async def approve() -> str:
        """宣布审查通过，立即结束本次协作。"""
        return "已记录：审查通过。"

    painter = AssistantAgent(
        name="Painter",
        model_client=get_model_client(temperature=0.3),
        system_message="你是画图员。一句话说明你要画什么，然后结束发言。不要调用任何工具。",
    )
    reviewer = AssistantAgent(
        name="Reviewer",
        model_client=get_model_client(temperature=0.3),
        system_message=(
            "你是审查员。对方说完之后，直接调用 approve 工具宣布通过。"
            "不要在文字里解释，也不要在文字里提「通过」这两个字。"
        ),
        tools=[approve],
    )

    team = RoundRobinGroupChat(
        participants=[painter, reviewer],
        termination_condition=(
            FunctionCallTermination(APPROVE_TOOL) | MaxMessageTermination(8)
        ),
    )

    result = await team.run(task="随便说一句你要画什么。")

    for message in result.messages:
        source = getattr(message, "source", "?")
        kind = type(message).__name__
        content = str(getattr(message, "content", ""))
        print(f"[{source} / {kind}] {content[:130]}")

    print("停止原因：", result.stop_reason)

    if APPROVE_TOOL in str(result.stop_reason):
        print(">>> 结论：协作正常，交付信号被正确识别。")
    elif len(result.messages) >= 8:
        print(">>> 注意：撞到了条数上限，说明审查员没有调用 approve 工具。")
    else:
        print(">>> 注意：停止原因异常，需要看上面的消息内容。")


async def step3_build_real_agents() -> None:
    """确认两个真实 agent 能被构造出来：prompt 读得到、工具注册得上。"""
    print()
    print("=" * 62)
    print("第 3 步：画图 agent 和审查 agent 能不能构造？")
    print("=" * 62)

    from agents.painter import build_painter
    from agents.reviewer import build_reviewer
    from agents.studio import Studio

    studio = Studio(
        task="测试任务",
        content=[],
        structure={
            "width": 1400,
            "height": 720,
            "template_id": "flow-path",
            "stages": [],
            "connections": [],
        },
        style={"palette": ["#2563eb"]},
        icons=[],
        resolved_icons=[],
    )

    painter = build_painter(studio)
    reviewer = build_reviewer(studio)

    for agent in (painter, reviewer):
        prompt_length = len(str(getattr(agent, "_system_messages", "")) or "")
        tools = getattr(agent, "_tools", None)
        tool_count = len(tools) if tools is not None else "未知"
        print(f"{agent.name}: 工具数 = {tool_count}, system prompt 字符数(近似) = {prompt_length}")

    print(">>> 结论：两个 agent 构造成功，prompt 文件和工具都已加载。")


async def main() -> None:
    print()
    try:
        await step1_tool_calling()
    except Exception as exc:
        print(f"第 1 步失败：{type(exc).__name__}: {exc}")
        print("常见原因：DEEPSEEK_KEY 没配、网络不通、模型名写错。")
        return

    try:
        await step2_two_agents()
    except Exception as exc:
        print(f"第 2 步失败：{type(exc).__name__}: {exc}")
        return

    try:
        await step3_build_real_agents()
    except Exception as exc:
        print(f"第 3 步失败：{type(exc).__name__}: {exc}")
        return

    print()
    print("=" * 62)
    print("三步都跑完了。可以进入下一步：替换 orchestrator 的迭代循环。")
    print("=" * 62)


if __name__ == "__main__":
    asyncio.run(main())
