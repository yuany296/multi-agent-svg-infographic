"""
SVG / JSON 提取、默认样式、日志记录等纯工具函数。
这些函数不依赖 LLM，只做字符串/XML 处理。

拆分说明（2026-07-08）：
- 布局引擎（compute_layout 等）迁移到 utils/layout.py
- 验证函数（check_* 等）迁移到 utils/validation.py
"""

import json
import logging
import re
from typing import Any
import xml.etree.ElementTree as StdET

import defusedxml.ElementTree as SafeET


def extract_json(text: str) -> Any:
    """
    从文本中提取第一个 JSON 对象或数组并解析。

    支持 Markdown 代码块（```json ... ``` 或 ``` ... ```）和裸 JSON。
    解析失败时返回空 dict。
    """
    text = text.strip()
    if not text:
        return {}

    # 1. 尝试 Markdown 代码块
    block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    candidate = block_match.group(1) if block_match else None

    # 2. 否则尝试取第一个 { ... } 或 [ ... ]
    if candidate is None:
        brace_match = re.search(r"(\{[\s\S]*\})|(\[[\s\S]*\])", text)
        if brace_match:
            candidate = brace_match.group(0)

    if candidate is None:
        return {}

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 最后兜底：尝试把常见 trailing 逗号去掉后再解析
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}


# 合法的 XML 实体引用，不能把 &#...; &amp; &lt; 等误转义
_VALID_XML_ENTITY = re.compile(
    r"&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);"
)

# 哨兵：用极少在 SVG 中出现的 Unicode 私人区字符作占位
_SENTINEL = ""


def _escape_loose_ampersands(svg_code: str) -> str:
    """将 SVG 中不属于合法 XML 实体的裸 & 转义为 &amp;。

    LLM 可能生成 "Add & Norm"、"Q & A" 等含 & 的文本内容，
    直接交给 XML 解析器会报错。这里在提取阶段做一次安全转义。

    思路：先保护注释、CDATA、已有合法实体，再将剩余裸 & 统一转义，
    最后逐层还原。
    """
    # 1) 保护 CDATA 段（内容不参与转义）
    cdata_sections: list[str] = []

    def _save_cdata(m: re.Match) -> str:
        cdata_sections.append(m.group(0))
        return f"{_SENTINEL}CDATA_{len(cdata_sections) - 1}{_SENTINEL}"

    svg_code = re.sub(r"<!\[CDATA\[.*?\]\]>", _save_cdata, svg_code, flags=re.DOTALL)

    # 2) 保护注释
    comments: list[str] = []

    def _save_comment(m: re.Match) -> str:
        comments.append(m.group(0))
        return f"{_SENTINEL}COMMENT_{len(comments) - 1}{_SENTINEL}"

    svg_code = re.sub(r"<!--.*?-->", _save_comment, svg_code, flags=re.DOTALL)

    # 3) 保护已有的合法 XML 实体 (&amp; &lt; &#123; 等)
    entities: list[str] = []

    def _save_entity(m: re.Match) -> str:
        entities.append(m.group(0))
        return f"{_SENTINEL}ENT_{len(entities) - 1}{_SENTINEL}"

    svg_code = _VALID_XML_ENTITY.sub(_save_entity, svg_code)

    # 4) 所有剩余的 & 都是非法的裸 &，统一转义
    svg_code = svg_code.replace("&", "&amp;")

    # 5) 按逆序还原：实体 → 注释 → CDATA
    for i, entity in enumerate(entities):
        svg_code = svg_code.replace(
            f"{_SENTINEL}ENT_{i}{_SENTINEL}", entity
        )
    for i, comment in enumerate(comments):
        svg_code = svg_code.replace(
            f"{_SENTINEL}COMMENT_{i}{_SENTINEL}", comment
        )
    for i, cdata in enumerate(cdata_sections):
        svg_code = svg_code.replace(
            f"{_SENTINEL}CDATA_{i}{_SENTINEL}", cdata
        )

    return svg_code


def extract_svg(text: str) -> str:
    """
    从文本中提取 <svg>...</svg> 代码。

    支持 Markdown 代码块（```svg ... ``` 或 ``` ... ```）和裸 SVG 标签。
    提取失败时返回空字符串。
    """
    text = text.strip()
    if not text:
        return ""

    # Markdown svg 代码块
    block_match = re.search(r"```(?:svg)?\s*([\s\S]*?)\s*```", text)
    if block_match:
        candidate = block_match.group(1).strip()
        if candidate.lower().startswith("<svg"):
            return _escape_loose_ampersands(candidate)

    # 直接取 <svg ... </svg>
    svg_match = re.search(r"(<svg\b[\s\S]*?</svg>)", text, re.IGNORECASE)
    if svg_match:
        return _escape_loose_ampersands(svg_match.group(1).strip())

    return ""


def extract_unified_diff(text: str) -> str:
    """从模型输出中提取 unified diff 文本。"""
    text = text.strip()
    if not text:
        return ""

    block_match = re.search(r"```(?:diff|patch)?\s*([\s\S]*?)\s*```", text)
    if block_match:
        candidate = block_match.group(1).strip()
        if "@@" in candidate or candidate.startswith(("--- ", "diff --git ")):
            return candidate

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(("diff --git ", "--- ")):
            candidate = "\n".join(lines[index:]).strip()
            if "@@" in candidate:
                return candidate
    return text if "@@" in text and "--- " in text and "+++ " in text else ""


def apply_unified_diff(original: str, diff_text: str) -> str:
    """把单文件 unified diff 应用到字符串，失败时抛出 ValueError。"""
    patch = extract_unified_diff(diff_text)
    if not patch:
        raise ValueError("缺少 unified diff")

    original_lines = original.splitlines()
    patch_lines = patch.splitlines()
    output: list[str] = []
    original_index = 0
    patch_index = 0
    hunk_count = 0

    while patch_index < len(patch_lines):
        line = patch_lines[patch_index]
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            patch_index += 1
            continue
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if not match:
            patch_index += 1
            continue

        hunk_count += 1
        old_start = int(match.group(1))
        hunk_old_index = max(old_start - 1, 0)
        if hunk_old_index < original_index:
            raise ValueError("diff hunk 顺序重叠")
        output.extend(original_lines[original_index:hunk_old_index])
        original_index = hunk_old_index
        patch_index += 1

        while patch_index < len(patch_lines):
            hunk_line = patch_lines[patch_index]
            if hunk_line.startswith("@@ "):
                break
            if hunk_line.startswith("\\"):
                patch_index += 1
                continue
            if not hunk_line:
                prefix = " "
                payload = ""
            else:
                prefix = hunk_line[0]
                payload = hunk_line[1:]

            if prefix == " ":
                if original_index >= len(original_lines) or original_lines[original_index] != payload:
                    raise ValueError("diff 上下文与原 SVG 不匹配")
                output.append(original_lines[original_index])
                original_index += 1
            elif prefix == "-":
                if original_index >= len(original_lines) or original_lines[original_index] != payload:
                    raise ValueError("diff 删除行与原 SVG 不匹配")
                original_index += 1
            elif prefix == "+":
                output.append(payload)
            else:
                raise ValueError(f"无法识别的 diff 行: {hunk_line[:40]}")
            patch_index += 1

    if hunk_count == 0:
        raise ValueError("diff 不包含 hunk")
    output.extend(original_lines[original_index:])
    result = "\n".join(output)
    if original.endswith(("\n", "\r\n")):
        result += "\n"
    return result


def _svg_element_tag(root: Any, name: str) -> str:
    """按原 SVG 的命名空间创建元素，保持序列化后的默认命名空间。"""
    if str(root.tag).startswith("{"):
        namespace = str(root.tag)[1:].split("}", 1)[0]
        return f"{{{namespace}}}{name}"
    return name


def _numeric_slot(slot: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(slot, dict):
        return None
    try:
        x, y, w, h = (float(slot[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _svg_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _append_svg(
    parent: Any,
    root: Any,
    name: str,
    attributes: dict[str, Any],
    text: str | None = None,
) -> Any:
    element = StdET.Element(
        _svg_element_tag(root, name),
        {key: _svg_number(value) if isinstance(value, float) else str(value) for key, value in attributes.items()},
    )
    if text is not None:
        element.text = text
    parent.append(element)
    return element





def default_style() -> dict:
    """当样式设计 Agent 输出异常时的兜底配色方案。"""
    return {
        "background": "#ffffff",
        # 教学信息图使用少量、低冲突的主题色；图标仍可按品牌保留官方色。
        "palette": ["#2563eb", "#0f766e", "#d97706", "#64748b"],
        "text_colors": {
            "title": "#0f172a",
            "body": "#334155",
            "muted": "#64748b",
        },
        "fonts": {"title": "sans-serif", "body": "sans-serif"},
        "stroke": {"color": "#333333", "width": 2},
        "section_style": {
            "fill": "#f8fafc",
            "stroke": "#93c5fd",
            "stroke_width": 2,
            "dash": "6 4",
        },
        "node_style": {"fill": "#ffffff", "stroke_width": 2, "radius": 12},
        "arrow_style": {"color": "#334155", "width": 3},
        "feedback_arrow_style": {
            "color": "#c2410c",
            "width": 2,
            "dash": "8 6",
        },
        "annotation_style": {"fill": "#475569", "font_size": 16},
        "icon_style": {
            "general_color_mode": "theme",
            "brand_color_mode": "official_when_available_else_theme",
            "container": "node-inline",
            "size": 64,
        },
        "section_tokens": [
            {"name": "blue", "fill": "#eff6ff", "stroke": "#2563eb", "accent": "#1d4ed8"},
            {"name": "teal", "fill": "#f0fdfa", "stroke": "#0f766e", "accent": "#0f766e"},
            {"name": "amber", "fill": "#fffbeb", "stroke": "#d97706", "accent": "#b45309"},
            {"name": "slate", "fill": "#f8fafc", "stroke": "#64748b", "accent": "#475569"},
        ],
        "accent_tokens": {
            "primary": "#2563eb",
            "success": "#16a34a",
            "warning": "#f59e0b",
            "danger": "#e11d48",
            "neutral": "#475569",
        },
        "arrow_tokens": {
            "normal": {"stroke": "#334155", "width": 3, "dash": ""},
            "secondary": {"stroke": "#64748b", "width": 2, "dash": "6 5"},
            "feedback": {"stroke": "#c2410c", "width": 2, "dash": "8 6"},
        },
        "typography_scale": {
            "title": 30,
            "subtitle": 16,
            "section_title": 22,
            "node_title": 18,
            "body": 14,
            "caption": 12,
        },
        "spacing_scale": {
            "canvas_margin": 20,
            "section_gap_ratio": 0.04,
            "module_gap_ratio": 0.03,
            "panel_padding": 16,
        },
        "style_notes": "清爽教学配色：蓝色表示主流程，青色表示语义关联，琥珀色表示关键结果，灰色表示辅助信息；同一面板只使用一种主题色和少量强调色",
    }


def _merge_style_dict(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_style_dict(merged[key], value)
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def normalize_style(style: dict | None) -> dict:
    """补齐样式 token，避免 LLM 少字段时颜色链路断开。"""
    base = default_style()
    if not isinstance(style, dict):
        return base
    normalized = _merge_style_dict(base, style)

    # LLM 偶尔会返回蓝、绿、橙、粉、紫、青六色彩虹盘，单色都合法但组合会
    # 抢夺教学图中的语义层级。超过四种主题色或出现明显的粉紫色时，回退到
    # 稳定的教学主题；品牌图标的官方色不受这一规则影响。
    palette_values = normalized.get("palette")
    palette_list = palette_values if isinstance(palette_values, list) else []
    palette_text = {
        str(value).strip().lower()
        for value in palette_list
        if str(value).strip()
    }
    section_values = normalized.get("section_tokens")
    section_list = section_values if isinstance(section_values, list) else []
    section_names = {
        str(token.get("name", "")).strip().lower()
        for token in section_list
        if isinstance(token, dict)
    }
    forbidden_palette_markers = {
        "pink", "purple", "magenta", "violet", "fuchsia", "#ec4899", "#be185d",
        "#8b5cf6", "#6d28d9", "#a855f7", "#d946ef", "#c026d3",
    }
    if (
        not isinstance(palette_values, list)
        or len(palette_list) > 4
        or not isinstance(section_values, list)
        or len(section_list) > 4
        or palette_text.intersection(forbidden_palette_markers)
        or section_names.intersection(forbidden_palette_markers)
    ):
        normalized["palette"] = list(base["palette"])
        normalized["section_tokens"] = list(base["section_tokens"])

    section_tokens = normalized.get("section_tokens")
    if not isinstance(section_tokens, list) or len(section_tokens) < 3:
        normalized["section_tokens"] = base["section_tokens"]
    else:
        repaired = []
        fallback_tokens = base["section_tokens"]
        for index, token in enumerate(section_tokens[:4]):
            fallback = fallback_tokens[index % len(fallback_tokens)]
            if not isinstance(token, dict):
                repaired.append(fallback)
                continue
            repaired.append(_merge_style_dict(fallback, token))
        normalized["section_tokens"] = repaired

    for key in ("accent_tokens", "arrow_tokens", "typography_scale", "spacing_scale"):
        if not isinstance(normalized.get(key), dict):
            normalized[key] = base[key]
    return normalized


def log_intermediate(name: str, data: Any) -> None:
    """统一打印每个 Agent 的中间结果，便于在控制台和 UI 中排查问题。"""
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        payload = str(data)
    logging.info("[中间结果] %s\n%s", name, payload)
