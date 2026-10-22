"""
视觉评估模块 —— 将 SVG 渲染为 PNG，调用视觉模型评估质量。

需要 cairosvg (pip install cairosvg) 和视觉模型 API key。
"""

import base64
import asyncio
import hashlib
import io
import json
import logging
import os
import sys
import time
from typing import Any

import httpx

# 在 Windows 上找到 conda 安装的 cairo-2.dll
if sys.platform == "win32":
    conda_root = os.path.expanduser("~/.conda")
    candidates = [
        f"{conda_root}/Library/bin",                     # base
        f"{conda_root}/envs/ai362/Library/bin",
        f"{conda_root}/envs/autogen_env/Library/bin",
    ]
    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in os.environ.get("PATH", ""):
            os.environ["PATH"] = candidate + os.pathsep + os.environ.get("PATH", "")
            break

# Qwen-VL 配置（通过环境变量设置）
QWEN_VL_API_KEY = os.getenv("QWEN_VL_KEY") or os.getenv("QWEN_KEY", "")
QWEN_VL_BASE_URL = os.getenv(
    "QWEN_VL_BASE_URL",
    "https://ws-3foak49mc3ozarzf.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
QWEN_VL_MODEL = os.getenv("QWEN_VL_MODEL", "qwen3.8-max")

# 视觉评估 prompt
_VISUAL_PROMPT = """你是一名 SVG 教学示意图质量评估专家。仔细查看这张图，输出 JSON：

{
  "passed": true/false,
  "visual_feedback": "整体评价（布局、颜色、文字层级、箭头连接等）",
  "retry_hint": "如果未通过，给出具体可执行的修改建议；通过则为空",
  "defects": [
    {
      "category": "overlap / occlusion / text / icon / arrow / density / layout / other",
      "stage_id": "对应阶段 id；全局问题为空字符串",
      "role": "title / description / icon / scene / connection / global",
      "location": "阶段标题、连接编号或画面区域",
      "issue": "可见的具体问题",
      "reason": "为什么会误导、重复或破坏可读性",
      "fix": "可执行的修复方式"
    }
  ]
}

评估维度：
1. 画布比例：内容是否填满画布？有没有过大或过小？
2. 布局结构：节点/模块间距是否合理？有没有拥挤或过于松散？
3. 颜色与对比度：配色是否统一克制（不超过 4 种主题色）？文字是否容易阅读？
4. 箭头与连接：箭头是否清晰可见？位置是否自然？有没有遮挡？
5. 文字层级：标题、正文、注释字号是否有层次？
6. 图标与图形：图标是否清晰可见？有没有被裁切或过小？机制分镜的 scene_area 是否真的画出了有意义的图形关系；如果图形只挤在场景顶部/角落、下方留下超过一半空白，或只是多个彩色文字条，应判为不通过，并要求将同一场景图形扩展、居中到主要可用区域。
7. 箭头语义（严格否决项）：面板内部箭头必须清楚地连接两个可见对象，并表达面板内部真实转换。若箭头悬空、指向空白、与主流程箭头重复表达“流向下一阶段”、方向含义不明，或与画面关系矛盾，必须 passed=false；在 defects 中写出其所在阶段、问题和删除/重连建议。
8. 一票否决：只要发现任一第 1-7 项的明显视觉错误，必须 passed=false；不得因为总体配色或布局尚可而放行。passed=true 时 defects 必须为空数组。
9. 你会同时看到整图和若干标明 stage id 的高清裁剪。必须结合裁剪检查小范围重影、文字覆盖、边框遮挡和图形互相压住；裁剪仅用于放大观察，不是额外画面。

只输出 JSON，不要解释。
"""

_PAIRWISE_PROMPT = """你是一名教学信息图视觉评审。你将看到同一任务的两个候选稿：图片 A 是当前最佳稿，图片 B 是新候选稿。

先确认两张图是否都完整表达任务中的阶段、文字和关系，再比较构图层级、可读性、留白、视觉丰富度、箭头清晰度和整体完成度。不要因为 B 更新就偏向 B，也不要把“元素更多”直接等同于更好。

只输出合法 JSON：
{
  "winner": "a / b / tie",
  "confidence": "high / medium / low",
  "reasons": ["最多三条具体原因"],
  "regressions": ["候选 B 相对 A 的退化；没有则为空"]
}

只有 B 明确优于 A 时才能选择 b；差异不明显、各有优劣或无法判断时选择 tie。
"""


def _normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """稳定视觉模型输出，确保否决原因可被后续重试直接消费。"""
    defects = result.get("defects", [])
    normalized: list[dict[str, Any]] = []
    for item in defects if isinstance(defects, list) else []:
        if not isinstance(item, dict):
            continue
        defect = dict(item)
        defect.setdefault("category", "other")
        defect.setdefault("stage_id", "")
        defect.setdefault("role", "global")
        identity = "|".join(
            str(defect.get(key, "")).strip().casefold()
            for key in ("category", "stage_id", "role", "location")
        )
        defect.setdefault("defect_id", "visual-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12])
        normalized.append(defect)
    result["defects"] = normalized
    result.setdefault("visual_feedback", "")
    result.setdefault("retry_hint", "")
    if result.get("passed") is True and normalized:
        result["passed"] = False
    return result


def svg_to_png(svg_code: str) -> bytes | None:
    """把 SVG 渲染为 PNG 字节，失败时返回 None。"""
    try:
        import cairosvg
        return cairosvg.svg2png(bytestring=svg_code.encode("utf-8"))
    except ImportError:
        logging.warning("cairosvg 未安装，无法渲染 SVG 为 PNG。pip install cairosvg")
        return None
    except Exception as exc:
        logging.warning("SVG 渲染失败：%s", exc)
        return None


def _resize_full_image(png_bytes: bytes, max_dim: int = 1200) -> bytes:
    """将整图缩放到 max_dim 以内，减少视觉模型处理时间和带宽。"""
    try:
        from PIL import Image

        source = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        w, h = source.size
        if max(w, h) <= max_dim:
            return png_bytes
        ratio = max_dim / max(w, h)
        new_size = (int(w * ratio), int(h * ratio))
        resized = source.resize(new_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        logging.info("整图已缩放：%dx%d → %dx%d", w, h, new_size[0], new_size[1])
        return buffer.getvalue()
    except Exception:
        return png_bytes


def _visual_images(
    svg_code: str,
    visual_context: dict[str, Any] | None,
) -> list[tuple[str, bytes]]:
    """生成整图及阶段高清裁剪；裁剪失败时仍保留整图。"""
    png_bytes = svg_to_png(svg_code)
    if png_bytes is None:
        return []
    # 限制整图尺寸，减少视觉模型处理时间
    full_image = _resize_full_image(png_bytes, max_dim=1200)
    images: list[tuple[str, bytes]] = [("整图", full_image)]
    try:
        from PIL import Image

        source = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        context = visual_context or {}
        canvas_w = float(context.get("canvas_width") or source.width)
        canvas_h = float(context.get("canvas_height") or source.height)
        scale_x = source.width / max(1.0, canvas_w)
        scale_y = source.height / max(1.0, canvas_h)
        # 最多发送 4 个阶段裁剪，控制请求体积
        for stage in list(context.get("stages", []))[:4]:
            if not isinstance(stage, dict):
                continue
            box = stage.get("crop_box")
            if not isinstance(box, dict):
                continue
            try:
                x1 = max(0, int(float(box.get("x", 0)) * scale_x))
                y1 = max(0, int(float(box.get("y", 0)) * scale_y))
                x2 = min(source.width, int((float(box.get("x", 0)) + float(box.get("w", 0))) * scale_x))
                y2 = min(source.height, int((float(box.get("y", 0)) + float(box.get("h", 0))) * scale_y))
            except (TypeError, ValueError):
                continue
            if x2 - x1 < 8 or y2 - y1 < 8:
                continue
            crop = source.crop((x1, y1, x2, y2))
            ratio = min(4.0, max(1.0, 640.0 / max(crop.width, crop.height)))
            if ratio > 1.0:
                crop = crop.resize((int(crop.width * ratio), int(crop.height * ratio)), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            images.append((f"stage {stage.get('id', '')} 高清裁剪", buffer.getvalue()))
    except Exception as exc:
        logging.warning("生成视觉裁剪失败，仅使用整图：%s", exc)
    return images


def _png_data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("utf-8")


async def _post_visual_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """发送兼容 OpenAI 协议的多模态请求并返回消息 JSON。"""
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{QWEN_VL_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {QWEN_VL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not resp.is_success:
            body = resp.text[:500] if resp.text else "(empty body)"
            logging.error(
                "视觉模型 HTTP %s：%s",
                resp.status_code,
                body,
            )
            resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        import re
        match = re.search(r"\{.*\}", str(content), re.DOTALL)
        if not match:
            logging.error("视觉模型返回非 JSON 内容：%s", str(content)[:500])
            raise ValueError("视觉模型返回格式异常")
        result = json.loads(match.group())
        if not isinstance(result, dict):
            raise ValueError("视觉模型返回格式异常")
        return result


def _is_retryable_visual_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ValueError, json.JSONDecodeError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


class VisualRequestFailure(RuntimeError):
    """保留视觉请求失败前的实际尝试次数。"""

    def __init__(self, cause: Exception, attempts: int):
        super().__init__(str(cause) or type(cause).__name__)
        self.cause = cause
        self.attempts = attempts


async def _post_visual_payload_with_retry(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """瞬时错误最多请求三次；配置类 4xx 立即失败。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return await _post_visual_payload(payload), attempt
        except Exception as exc:
            last_error = exc
            if attempt >= 3 or not _is_retryable_visual_error(exc):
                raise VisualRequestFailure(exc, attempt) from exc
            await asyncio.sleep(0.25 * attempt)
    raise RuntimeError(str(last_error or "视觉评估失败"))


async def visual_evaluate(
    svg_code: str,
    task: str,
    visual_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """调用视觉模型评估 SVG 渲染图的视觉质量。

    Args:
        svg_code: SVG 代码
        task: 原始任务描述

    Returns:
        包含 passed / visual_feedback / retry_hint 的字典；
        视觉模型不可用时返回 unavailable，绝不默认通过。
    """
    if not QWEN_VL_API_KEY:
        logging.info("QWEN_VL_KEY 未设置，跳过视觉评估")
        return {
            "passed": False,
            "visual_feedback": "",
            "retry_hint": "",
            "defects": [],
            "visual_source": "disabled",
            "visual_model_enabled": False,
            "visual_status": "unavailable",
            "visual_error": "QWEN_VL_KEY/QWEN_KEY 未配置",
            "visual_attempts": 0,
            "visual_model": QWEN_VL_MODEL,
            "visual_elapsed_ms": 0,
        }

    # 1. 渲染为 PNG
    visual_images = _visual_images(svg_code, visual_context)
    if not visual_images:
        return {
            "passed": False,
            "visual_feedback": "",
            "retry_hint": "SVG 渲染失败，跳过视觉评估",
            "defects": [],
            "visual_source": "render-failed",
            "visual_model_enabled": False,
            "visual_status": "unavailable",
            "visual_error": "SVG 无法渲染为 PNG",
            "visual_attempts": 0,
            "visual_model": QWEN_VL_MODEL,
            "visual_elapsed_ms": 0,
        }

    # 3. 调用 Qwen-VL
    payload = {
        "model": QWEN_VL_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"原始任务：{task}\n"
                            f"结构上下文（仅用于核对箭头与场景语义）：{json.dumps(visual_context or {}, ensure_ascii=False)}\n\n"
                            f"{_VISUAL_PROMPT}"
                        ),
                    },
                ] + [
                    part
                    for label, image in visual_images
                    for part in (
                        {"type": "text", "text": label},
                        {"type": "image_url", "image_url": {"url": _png_data_uri(image)}},
                    )
                ],
            }
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    started = time.perf_counter()
    try:
        raw_result, attempts = await _post_visual_payload_with_retry(payload)
        result = _normalize_result(raw_result)
        result.setdefault("visual_source", "qwen-vl")
        result.setdefault("visual_model_enabled", True)
        result["visual_status"] = "passed" if result.get("passed") is True else "rejected"
        result["visual_error"] = ""
        result["visual_attempts"] = attempts
        result["visual_model"] = QWEN_VL_MODEL
        result["visual_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    except Exception as exc:
        exc_msg = str(exc) or f"{type(exc).__name__}"
        attempts = exc.attempts if isinstance(exc, VisualRequestFailure) else 1
        logging.error("视觉评估调用失败：%s", exc_msg)
        return {
            "passed": False,
            "visual_feedback": "",
            "retry_hint": f"视觉评估调用失败：{exc_msg}",
            "defects": [],
            "visual_source": "qwen-vl-error",
            "visual_model_enabled": False,
            "visual_status": "unavailable",
            "visual_error": exc_msg,
            "visual_attempts": attempts,
            "visual_model": QWEN_VL_MODEL,
            "visual_elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


async def compare_visual_candidates(
    incumbent_svg: str,
    candidate_svg: str,
    task: str,
    visual_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """成对比较当前最佳稿和新候选稿；不确定或不可用时返回 tie。"""
    unavailable = {
        "winner": "tie",
        "confidence": "low",
        "reasons": [],
        "regressions": [],
        "visual_source": "disabled",
        "visual_model_enabled": False,
    }
    if not QWEN_VL_API_KEY:
        return unavailable

    incumbent_png = svg_to_png(incumbent_svg)
    candidate_png = svg_to_png(candidate_svg)
    if incumbent_png is None or candidate_png is None:
        return dict(unavailable, visual_source="render-failed", comparison_error="候选稿 PNG 渲染失败")

    payload = {
        "model": QWEN_VL_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"原始任务：{task}\n"
                        f"结构上下文：{json.dumps(visual_context or {}, ensure_ascii=False)}\n\n"
                        f"{_PAIRWISE_PROMPT}"
                    ),
                },
                {"type": "text", "text": "候选 A（当前最佳稿）："},
                {"type": "image_url", "image_url": {"url": _png_data_uri(incumbent_png)}},
                {"type": "text", "text": "候选 B（新候选稿）："},
                {"type": "image_url", "image_url": {"url": _png_data_uri(candidate_png)}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 768,
    }
    try:
        result, attempts = await _post_visual_payload_with_retry(payload)
        winner = str(result.get("winner", "tie")).strip().lower()
        confidence = str(result.get("confidence", "low")).strip().lower()
        result["winner"] = winner if winner in {"a", "b", "tie"} else "tie"
        result["confidence"] = confidence if confidence in {"high", "medium", "low"} else "low"
        result["reasons"] = [str(item) for item in result.get("reasons", []) if str(item).strip()][:3]
        result["regressions"] = [str(item) for item in result.get("regressions", []) if str(item).strip()][:3]
        result["visual_source"] = "qwen-vl-pairwise"
        result["visual_model_enabled"] = True
        result["visual_attempts"] = attempts
        result["visual_model"] = QWEN_VL_MODEL
        return result
    except Exception as exc:
        logging.error("候选稿视觉成对比较失败：%s", exc)
        attempts = exc.attempts if isinstance(exc, VisualRequestFailure) else 1
        return dict(
            unavailable,
            visual_source="qwen-vl-pairwise-error",
            comparison_error=str(exc),
            visual_attempts=attempts,
            visual_model=QWEN_VL_MODEL,
        )
