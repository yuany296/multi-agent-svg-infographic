"""StyleDesigner —— 为教学型信息图设计统一视觉语言。"""

from utils.config import get_model_client
from utils.llm import call_llm_json
from utils.svg import default_style, normalize_style


_SYSTEM_PROMPT = """你是一名视觉样式设计专家。根据信息图内容、结构类型和可用图标，设计适合 SVG 教学示意图的样式。

输出合法 JSON 对象：
{
  "background": "#ffffff",
  "palette": ["#2563eb", "#0f766e", "#d97706", "#64748b"],
  "text_colors": {
    "title": "#0f172a",
    "body": "#334155",
    "muted": "#64748b"
  },
  "fonts": {
    "title": "Microsoft YaHei, Arial, sans-serif",
    "body": "Microsoft YaHei, Arial, sans-serif"
  },
  "stroke": {"color": "#333333", "width": 2},
  "section_style": {
    "fill": "#f8fafc",
    "stroke": "#94a3b8",
    "stroke_width": 2,
    "dash": "6 4"
  },
  "node_style": {
    "fill": "#ffffff",
    "stroke_width": 2,
    "radius": 12
  },
  "arrow_style": {"color": "#333333", "width": 3},
  "feedback_arrow_style": {"color": "#c2410c", "width": 2, "dash": "8 6"},
  "annotation_style": {"fill": "#475569", "font_size": 16},
  "icon_style": {
    "general_color_mode": "theme",
    "brand_color_mode": "official_when_available_else_theme",
    "container": "node-inline",
    "size": 64
  },
  "section_tokens": [
    {"name": "blue", "fill": "#eff6ff", "stroke": "#2563eb", "accent": "#1d4ed8"},
    {"name": "teal", "fill": "#f0fdfa", "stroke": "#0f766e", "accent": "#0f766e"},
    {"name": "amber", "fill": "#fffbeb", "stroke": "#d97706", "accent": "#b45309"},
    {"name": "slate", "fill": "#f8fafc", "stroke": "#64748b", "accent": "#475569"}
  ],
  "accent_tokens": {
    "primary": "#2563eb",
    "success": "#16a34a",
    "warning": "#f59e0b",
    "danger": "#e11d48",
    "neutral": "#475569"
  },
  "arrow_tokens": {
    "normal": {"stroke": "#334155", "width": 3, "dash": ""},
    "secondary": {"stroke": "#64748b", "width": 2, "dash": "6 5"},
    "feedback": {"stroke": "#e11d48", "width": 2, "dash": "8 6"}
  },
  "typography_scale": {
    "title": 30,
    "subtitle": 16,
    "section_title": 22,
    "node_title": 18,
    "body": 14,
    "caption": 12
  },
  "spacing_scale": {
    "canvas_margin": 20,
    "section_gap_ratio": 0.04,
    "module_gap_ratio": 0.03,
    "panel_padding": 16
  },
  "style_notes": "整体视觉风格说明，并简要说明 palette 中各颜色的用途"
}

颜色规则：

* section_tokens 是分区/模块的唯一色彩事实源：fill 用低饱和浅色，stroke/accent 用更清晰的高对比主题色。
* 主题色最多使用 4 种，优先使用蓝、青、琥珀、灰；不要同时使用粉色、紫色、荧光绿等互相争抢注意力的颜色。
* accent_tokens 只用于关键节点、重要数值和局部强调，不要让整张图都使用同一个强调色。
* arrow_tokens.normal 用于主流程，arrow_tokens.secondary 用于弱关系，arrow_tokens.feedback 用于反馈闭环。
* typography_scale 和 spacing_scale 必须给出数值 token，后续生成器按 token 执行，不再依赖模糊审美描述。
* 文字颜色必须与 section_tokens.fill 有足够对比度，标题使用深色，正文使用深灰色，注释使用次要灰色。
* 避免复杂渐变、强阴影、高饱和荧光色、大面积纯黑色和单一色相铺满全图。
* 每个面板只选择一个 section token 作为主色，其他颜色仅作为小面积图标、箭头或关键结果强调。

"""


async def design(
    task: str,
    content: list[dict],
    structure: dict,
    icons: list[dict] | None = None,
) -> dict:
    """为信息图设计配色、字体、箭头、分区和图标样式。"""
    client = get_model_client()
    user_prompt = (
        f"任务需求：{task}\n"
        f"内容分析结果：{content}\n"
        f"结构规划结果：{structure}\n"
        f"可用图标摘要：{icons or []}\n"
        "请输出统一的教学示意图样式 JSON。"
    )
    result = await call_llm_json(client, _SYSTEM_PROMPT, user_prompt)
    if isinstance(result, dict) and result:
        return normalize_style(result)
    return default_style()
