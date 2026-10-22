"""
模型客户端配置 —— 惰性创建，不会在 import 时就 crash。
不同 agent 可以通过 temperature 参数获得不同风格的客户端。
"""

import os
from typing import Optional

from dotenv import load_dotenv
from autogen_core.models import ModelFamily, ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")


def _build_model_info() -> ModelInfo:
    """为 DeepSeek 等非 OpenAI 模型构造必要的 model_info。"""
    return {
        "family": ModelFamily.UNKNOWN,
        "function_calling": True,
        "json_output": True,
        "vision": False,
        "structured_output": False,
    }


def get_model_client(temperature: float = 0.7) -> OpenAIChatCompletionClient:
    """
    获取模型客户端实例。
    每个 agent 应独立获取以支持不同的 temperature/thinking 配置。
    """
    return create_model_client(temperature)


def create_model_client(temperature: float = 0.7) -> OpenAIChatCompletionClient:
    """
    每次调用都新建一个客户端实例。
    适合需要不同 temperature 的场景（如 validator 用 0.3，generator 用 0.7）。
    """
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "环境变量 DEEPSEEK_KEY 未设置。"
            "请通过系统环境变量或 .env 文件提供 DeepSeek API Key。"
        )
    return OpenAIChatCompletionClient(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        model_info=_build_model_info(),
        model=DEEPSEEK_MODEL,
        temperature=temperature,
    )
