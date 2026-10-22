"""Validator —— SVG 语法、坐标、图标引用与视觉结构校验。"""

import logging
import hashlib
import re
from typing import Any

from utils.icons import check_icon_references
from utils.validation import (
    check_bounds,
    check_canvas_dimensions,
    check_comparison_bar_slots,
    check_connection_label_layout,
    check_element_overlaps,
    check_icon_contrast,
    check_icon_legibility,
    check_icon_occlusion,
    check_scene_density,
    check_storyboard_icon_badges,
    is_connection_label_render_error,
    check_layout_quality,
    check_overlap,
    check_section_layout,
    check_semantic_connections,
    check_style_quality,
    check_svg_syntax,
    check_template_contract,
    check_text_completeness,
)


_SYSTEM_PROMPT = """你是一名 SVG 信息图质量评估专家。评估视觉合理性和结构合理性。

只输出合法 JSON：
{
  "passed": true,
  "visual_feedback": "整体评价",
  "retry_hint": "未通过时给出具体修改建议；通过则为空"
}

通用检查：
1. SVG 能正常渲染，无明显画布溢出、重叠、文字或箭头遮挡。
2. 配色协调、对比度足够，主题色不超过 4 种，贴合任务主题。
3. 多数图、少文字，视觉平衡，像清晰的教学示意图。
4. 数值可视化严格按比例。
5. 只有 structure.connections 中声明的关系可以画成箭头；每条关系的方向和 label 必须一致，网格线与坐标轴不是语义连接。
6. 每个 stage 的 title/description/metric 必须位于对应 slot；存在 icon 槽时图标也必须位于对应槽，彼此不得遮挡；连接标签不得压住节点内容。
7. 正文不能用 ...、…、⋯ 代表未写完内容；必须换行或改写成完整短句。
8. 图标必须清晰可见，不能过小，也不能与背景低对比融合。

flow 专项：
- 必须有连续箭头或主流程线，相邻阶段连接清楚，先后顺序一眼可见。
- 只有孤立矩形或没有箭头时，passed 必须为 false。

timeline 专项：
- 时间节点必须按顺序排布并由时间轴或箭头连接。

mechanism/system 专项：
- 必须有分区、模块、主流程、反馈或辅助说明等结构层次。
- 不能只是普通卡片列表，模块之间应有连接关系。
- 存在 feedback_arrow 语义时，应出现虚线或弯曲反馈箭头。

mechanism-storyboard 专项：
- 每个 panel 必须像教学分镜：有大阶段标题、scene_area 内的小图画，以及简短说明。
- visual_brief 的语义必须被可视化，不能只放一个图标或一段文字。
- scene_area 不能退化为多个只写文字的彩色宽矩形；应看到与节点语义有关的几何关系，但不得要求特定图案。
- 场景图形必须在 scene_area 内居中并覆盖主要可用面积；若图形只挤在顶部/角落、留下超过一半的空白，应判为不通过。
- 至少一个 panel 应包含多个内部小元素；全局箭头或反馈箭头要能表达阶段关系。

compare 专项：
- 对比项应共享基线和比例尺；数值比例必须正确。
- 高倍率柱状图应紧凑居中，倍数关系放在贴近箭头曲线的独立标签/callout 中，不能打断柱子、图标或数值。

排版专项：
- 节点间距足够，文字不贴边，标题层级清楚，画面不过密也不过空。

未通过时 retry_hint 必须具体可执行，例如添加 0→1→2 箭头、拆成上下分区、减少文字或补画反馈线。
只输出 JSON，不要解释。
"""


def _is_larger_canvas(error: str, expected_w: int, expected_h: int) -> bool:
    """画布实际尺寸比预期大时不算阻断错误（内容不会被裁切，只是略微缩小显示）。"""
    import re
    match = re.search(r"实际为 (\d+)", error)
    if not match:
        return False
    actual = int(match.group(1))
    return actual > expected_w and actual > 0


def _split_icon_reference_errors(errors: list[str]) -> tuple[list[str], list[str]]:
    """将会导致交付图标偏离解析结果的引用错误作为硬错误。"""
    blocking_prefixes = (
        "SVG 存在重复 id",
        "<use> 使用了外部或非法 href",
        "<use> 引用了不存在的 symbol",
        "SVG 包含外部网络资源",
        # 已解析的品牌/本地图标必须通过 <use> 引用，不能由视觉模型自行仿画后直接通过。
        "已有解析图标",
        "解析图标 ",
        "品牌图标 ",
        "品牌占位图 ",
        "data-brand-badge",
    )
    blocking: list[str] = []
    suggestions: list[str] = []
    for error in errors:
        if error.startswith(blocking_prefixes):
            blocking.append(error)
        else:
            suggestions.append(error)
    return blocking, suggestions


def _build_visual_context(content: list[dict], structure: dict) -> dict[str, Any]:
    """向视觉模型提供最小结构语义，便于判断内部箭头是否重复或悬空。"""
    stages = structure.get("stages", [])
    source_stages = stages if isinstance(stages, list) and stages else content
    return {
        "template_id": structure.get("template_id", ""),
        "canvas_width": structure.get("width", 1200),
        "canvas_height": structure.get("height", 800),
        "main_connections": [
            {
                "from": item.get("from"),
                "to": item.get("to"),
                "type": item.get("type", "arrow"),
                "label": item.get("label", ""),
            }
            for item in structure.get("connections", [])
            if isinstance(item, dict)
        ],
        "stages": [
            {
                "id": stage.get("id", index),
                "title": stage.get("title", ""),
                "visual_brief": stage.get("visual_brief", ""),
                "scene_area": stage.get("scene_area", {}),
                "crop_box": (
                    stage.get("panel_rect")
                    or stage.get("timeline_card")
                    or stage.get("safe_area")
                    or {
                        "x": stage.get("x", 0),
                        "y": stage.get("y", 0),
                        "w": stage.get("w", 0),
                        "h": stage.get("h", 0),
                    }
                ),
                "has_main_outgoing_connection": any(
                    str(edge.get("from")) == str(stage.get("id", index))
                    for edge in structure.get("connections", [])
                    if isinstance(edge, dict)
                ),
            }
            for index, stage in enumerate(source_stages)
            if isinstance(stage, dict)
        ],
    }


def _format_visual_defect(defect: dict[str, Any]) -> str:
    location = str(defect.get("location", "画面位置")).strip() or "画面位置"
    issue = str(defect.get("issue", "视觉问题")).strip() or "视觉问题"
    reason = str(defect.get("reason", "")).strip()
    fix = str(defect.get("fix", "")).strip()
    detail = f"{location}：{issue}"
    if reason:
        detail += f"；原因：{reason}"
    if fix:
        detail += f"；修复：{fix}"
    return detail


def _issue_location(message: str) -> tuple[str, str]:
    stage_match = re.search(r"(?:阶段|stage)\s+([A-Za-z0-9_.-]+)", message, re.IGNORECASE)
    role_match = re.search(r"\b(title|description|metric|icon|scene|connection-label)\b", message)
    return (
        stage_match.group(1) if stage_match else "",
        role_match.group(1) if role_match else "global",
    )


def _issue_category(message: str) -> str:
    mappings = (
        ("occlusion", ("遮挡", "被后绘制")),
        ("overlap", ("重叠", "重复引用", "同时存在自绘")),
        ("text-overflow", ("越出槽位", "溢出画布", "省略号")),
        ("icon", ("图标", "icon", "symbol", "<use>")),
        ("connection", ("连接", "箭头", "connection-label")),
        ("centering", ("偏上", "偏下", "偏左", "偏右", "居中")),
        ("density", ("密度", "仅占", "空白")),
    )
    lowered = message.lower()
    return next((category for category, words in mappings if any(word.lower() in lowered for word in words)), "validation")


def _normalise_issue_message(message: str) -> str:
    value = re.sub(r"\s+", " ", str(message)).strip()
    if any(word in value for word in ("越出槽位", "溢出画布", "偏上", "偏下", "标签")):
        value = re.split(r"[：:]", value, maxsplit=1)[0]
    return value


def _issue_bbox(message: str) -> dict[str, float] | None:
    match = re.search(
        r"(?:元素边界|文字边界)\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)",
        message,
    )
    if not match:
        return None
    x1, y1, x2, y2 = (float(value) for value in match.groups())
    return {"x": x1, "y": y1, "w": max(0.0, x2 - x1), "h": max(0.0, y2 - y1)}


def _make_issue(
    message: str,
    severity: str,
    *,
    category: str | None = None,
    stage_id: str = "",
    role: str = "",
    repair_scope: str = "element",
    bbox: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inferred_stage, inferred_role = _issue_location(message)
    category = category or _issue_category(message)
    stage_id = str(stage_id if stage_id != "" else inferred_stage)
    role = role or inferred_role
    identity = "|".join((severity, category, stage_id, role, _normalise_issue_message(message)))
    return {
        "issue_id": "issue-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
        "category": category,
        "severity": severity,
        "stage_id": stage_id,
        "role": role,
        "bbox": bbox if isinstance(bbox, dict) else _issue_bbox(str(message)),
        "message": str(message),
        "repair_scope": repair_scope,
    }


async def validate(
    svg_code: str,
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    icons: list[dict] | None = None,
) -> dict:
    """执行确定性硬检查和低温度 LLM 视觉评估。

    SVG 只解析一次，解析结果缓存给所有 check 函数共享，避免重复开销。
    """
    import defusedxml.ElementTree as ET

    canvas_w = structure.get("width", 1200)
    canvas_h = structure.get("height", 800)

    # 1. 语法检查（最基础，必须最先做）
    syntax_errors = check_svg_syntax(svg_code)

    # 2. 解析 SVG 一次，后续所有 check 复用 root
    root = None
    if not syntax_errors and svg_code.strip():
        try:
            root = ET.fromstring(svg_code)
        except ET.ParseError:
            pass  # check_svg_syntax 已捕获，不进此处

    # 3. 检查分两类：阻断错误只保留渲染/画布层面的确定性问题；
    #    语义、槽位、图标清晰度等交给视觉模型裁决，避免可用图被反复重画。
    canvas_errors = check_canvas_dimensions(svg_code, canvas_w, canvas_h, root=root)
    bounds_errors = check_bounds(svg_code, canvas_w, canvas_h, root=root)
    content_errors = check_text_completeness(svg_code, root=root)
    layout_quality_errors = check_layout_quality(structure)
    comparison_bar_errors = check_comparison_bar_slots(svg_code, structure, root=root)
    storyboard_badge_errors = check_storyboard_icon_badges(svg_code, structure, root=root)
    label_layout_errors = check_connection_label_layout(structure)
    layout_errors = check_overlap(structure.get("stages", [])) + layout_quality_errors
    style_errors = check_style_quality(style)
    element_errors = check_element_overlaps(
        svg_code,
        structure.get("stages", []),
        structure.get("connections", []),
        structure.get("header"),
        root=root,
    )
    label_render_errors = [
        error for error in element_errors if is_connection_label_render_error(error)
    ]
    text_bounds_errors = [error for error in bounds_errors if error.startswith("<text>")]
    all_slot_text_errors = [
        error
        for error in element_errors
        if any(f" {role} " in error for role in ("title", "description", "metric"))
    ]
    is_comparison_chart = (
        str(structure.get("template_id", "")).strip().lower() == "comparison-chart"
        or str(structure.get("diagram_type", "")).strip().lower() == "compare"
    )
    flexible_comparison_text_slot_errors = [
        error
        for error in all_slot_text_errors
        if is_comparison_chart
        and (" title 元素越出槽位" in error or " description 元素越出槽位" in error)
    ]
    slot_text_errors = [
        error for error in all_slot_text_errors
        if error not in flexible_comparison_text_slot_errors
    ]
    comparison_text_overlap_errors = [
        error
        for error in element_errors
        if is_comparison_chart
        and error.startswith("前景元素重叠")
        and (" title " in error or " description " in error)
    ]
    icon_quality_errors = (
        check_icon_legibility(svg_code, structure.get("stages", []), root=root)
        + check_icon_contrast(svg_code, structure.get("stages", []), root=root)
    )
    icon_occlusion_errors = check_icon_occlusion(
        svg_code,
        structure.get("stages", []),
        root=root,
    )
    icon_reference_errors = check_icon_references(
        svg_code,
        icons or [],
        root=root,
        stages=structure.get("stages", []),
    )
    icon_blockers, icon_reference_suggestions = _split_icon_reference_errors(icon_reference_errors)
    scene_density_errors = check_scene_density(
        svg_code,
        structure.get("stages", []),
        root=root,
    )
    scene_vertical_centering_errors = [
        error
        for error in scene_density_errors
        if "scene 图形偏上" in error or "scene 图形偏下" in error
    ]
    structure_suggestions = (
        check_section_layout(svg_code, structure.get("sections", []), root=root)
        + check_semantic_connections(svg_code, structure.get("connections", []), root=root)
        + check_template_contract(svg_code, structure, root=root)
        + [error for error in scene_density_errors if error not in scene_vertical_centering_errors]
    )

    # 阻断错误：即使视觉模型通过，也不应交付。
    # 画布尺寸偏差：实际偏大可以缩放显示，不算阻断；偏小可能导致裁切，必须阻断
    canvas_blockers = [
        e for e in canvas_errors
        if "实际为" in e and not _is_larger_canvas(e, canvas_w, canvas_h)
    ]
    canvas_suggestions = [e for e in canvas_errors if e not in canvas_blockers]
    hard_errors = (
        canvas_blockers
        + comparison_bar_errors
        + storyboard_badge_errors
        + scene_vertical_centering_errors
        + icon_occlusion_errors
        + icon_blockers
        + text_bounds_errors
        + slot_text_errors
        + comparison_text_overlap_errors
        + label_layout_errors
        + label_render_errors
    )

    # 建议：只作为视觉/文字模型的参考，不单独触发失败。
    suggestions = (
        [error for error in bounds_errors if error not in text_bounds_errors]
        + content_errors
        + [error for error in layout_errors if error not in label_layout_errors]
        + style_errors
        + [
            error
            for error in element_errors
            if error not in label_render_errors
            and error not in slot_text_errors
            and error not in comparison_text_overlap_errors
        ]
        + icon_quality_errors
        + icon_reference_suggestions
        + canvas_suggestions
        + structure_suggestions
    )
    if suggestions:
        logging.info("[校验建议] %d 条（不阻止通过）：", len(suggestions))
        for s in suggestions[:5]:
            logging.info("  - %s", s)

    # 4. 严格视觉评估：真实多模态不可用时禁止交付，不允许文字 LLM 冒充视觉验收。
    visual_result: dict[str, Any]
    try:
        from utils.visual_eval import visual_evaluate
        visual_result = await visual_evaluate(
            svg_code,
            task,
            _build_visual_context(content, structure),
        )
        logging.info(
            "视觉评估：status=%s source=%s attempts=%s",
            visual_result.get("visual_status"),
            visual_result.get("visual_source"),
            visual_result.get("visual_attempts"),
        )
    except Exception as exc:
        logging.exception("视觉评估未捕获异常：%s", exc)
        visual_result = {
            "passed": False,
            "visual_status": "unavailable",
            "visual_source": "validator-exception",
            "visual_model_enabled": False,
            "visual_error": str(exc),
            "visual_attempts": 0,
            "visual_model": "",
            "visual_elapsed_ms": 0,
            "visual_feedback": "",
            "retry_hint": "",
            "defects": [],
        }

    visual_defects = [
        item for item in visual_result.get("defects", []) if isinstance(item, dict)
    ] if isinstance(visual_result, dict) else []
    default_visual_status = "passed" if visual_result.get("passed") is True else "rejected"
    visual_status = str(visual_result.get("visual_status", default_visual_status))
    visual_source = str(visual_result.get("visual_source", "none"))
    visual_model_enabled = bool(
        visual_result.get("visual_model_enabled", visual_status in {"passed", "rejected"})
    )
    visual_veto = visual_status == "rejected"
    passed = (
        not syntax_errors
        and not hard_errors
        and visual_status == "passed"
        and visual_model_enabled
        and not visual_defects
        and visual_result.get("passed", False) is True
    )
    retry_hint = str(visual_result.get("retry_hint", ""))
    if visual_veto:
        veto_details = "\n".join(f"- {_format_visual_defect(item)}" for item in visual_defects)
        veto_prefix = "[视觉模型一票否决]"
        retry_hint = veto_prefix + ("\n" + veto_details if veto_details else "") + (
            "\n" + retry_hint if retry_hint else ""
        )
    if visual_status == "unavailable":
        retry_hint = (
            "[视觉验收不可用，禁止交付]\n"
            + str(visual_result.get("visual_error", "未知视觉服务错误"))
        )
    if hard_errors and not passed:
        prefix = "[硬性错误，必须优先修复]"
        retry_hint = (
            prefix + "\n"
            + "\n".join(f"- {error}" for error in hard_errors)
            + ("\n" + retry_hint if retry_hint else "")
        )
    issues = (
        [_make_issue(error, "blocking", category="syntax", repair_scope="document") for error in syntax_errors]
        + [_make_issue(error, "blocking") for error in hard_errors]
        + [_make_issue(error, "suggestion") for error in suggestions]
    )
    for defect in visual_defects:
        issue = _make_issue(
            _format_visual_defect(defect),
            "blocking",
            category=str(defect.get("category", "visual")),
            stage_id=str(defect.get("stage_id", "")),
            role=str(defect.get("role", "global")),
            repair_scope="stage" if str(defect.get("stage_id", "")) else "document",
            bbox=defect.get("bbox") if isinstance(defect.get("bbox"), dict) else None,
        )
        issue["issue_id"] = str(defect.get("defect_id") or issue["issue_id"])
        issues.append(issue)
    if visual_status == "unavailable":
        issues.append(
            _make_issue(
                str(visual_result.get("visual_error", "视觉验收不可用")),
                "blocking",
                category="visual-infrastructure",
                repair_scope="infrastructure",
            )
        )
    remaining_issue_ids = [
        str(issue["issue_id"]) for issue in issues if issue.get("severity") == "blocking"
    ]
    return {
        "passed": passed,
        "syntax_errors": syntax_errors,
        "layout_errors": layout_errors,
        "label_layout_errors": label_layout_errors,
        "label_render_errors": label_render_errors,
        "hard_errors": hard_errors,
        "suggestions": suggestions,
        "visual_feedback": visual_result.get("visual_feedback", ""),
        "retry_hint": retry_hint,
        "visual_source": visual_source,
        "visual_model_enabled": visual_model_enabled,
        "visual_veto": visual_veto,
        "visual_defects": visual_defects,
        "visual_status": visual_status,
        "visual_error": str(visual_result.get("visual_error", "")),
        "visual_attempts": int(visual_result.get("visual_attempts", 0) or 0),
        "visual_model": str(visual_result.get("visual_model", "")),
        "visual_elapsed_ms": int(visual_result.get("visual_elapsed_ms", 0) or 0),
        "issues": issues,
        "resolved_issue_ids": [],
        "remaining_issue_ids": remaining_issue_ids,
        "new_issue_ids": [],
    }
