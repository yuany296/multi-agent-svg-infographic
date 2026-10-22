"""
LLM 调用封装：每个 agent 函数不再关心 AutoGen 细节和 JSON 修复。
"""

from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

from utils.svg import extract_json


async def call_llm(
    client: OpenAIChatCompletionClient,
    system: str,
    user: str,
) -> str:
    """
    用 AutoGen AssistantAgent 发送一次请求，返回模型的原始文本输出。

    参数:
        client: 模型客户端
        system: system message
        user: 用户消息
    返回:
        模型回复的原始文本
    """
    agent = AssistantAgent(
        name="Agent",
        model_client=client,
        system_message=system,
    )
    response = await agent.on_messages(
        [TextMessage(content=user, source="user")],
        CancellationToken(),
    )
    return response.chat_message.content


async def call_llm_json(
    client: OpenAIChatCompletionClient,
    system: str,
    user: str,
    retry: bool = True,
) -> Any:
    """
    调用 LLM 并确保返回合法 JSON。

    如果首次输出不是合法 JSON 且 retry=True，会自动追加一条修复 prompt 再试一次。

    参数:
        client: 模型客户端
        system: system message
        user: 用户消息
        retry: 解析失败时是否自动重试一次
    返回:
        解析后的 Python 对象（dict / list / 等）
    """
    raw = await call_llm(client, system, user)
    parsed = extract_json(raw)
    if parsed:
        return parsed

    # 重试一次：告诉模型输出合法 JSON
    if retry:
        repair_prompt = (
            "你上一次输出的内容不是合法的 JSON，请重新输出，"
            "仅输出合法的 JSON，不要附加任何解释。\n"
            f"原始请求：{user}"
        )
        raw2 = await call_llm(client, system, repair_prompt)
        parsed2 = extract_json(raw2)
        if parsed2:
            return parsed2

    return {}
