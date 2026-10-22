"""
LayoutEngine —— 把 StructurePlanner 的布局意图计算为确定性像素坐标。

不依赖 LLM，仅做数值计算。
"""

import math
import re
from typing import Any

from utils.layout_templates import apply_layout_template, compute_direct_layout_template

_CANVAS_MIN_W = 760
_CANVAS_MIN_H = 520
_CANVAS_MAX_W = 2600
_CANVAS_MAX_H = 2200

_LAYOUT_FEEDBACK_KEYWORDS = (
    "布局",
    "槽位",
    "主体占用",
    "模块间距",
    "过满",
    "过密",
    "过空",
    "过松",
    "溢出画布",
    "重叠",
    "避让区",
    "标签槽",
    "section",
    "safe_area",
    "panel",
)


def _parse_layout_intent(intent: str, stage_count: int) -> tuple[int, int]:
    """把布局意图解析成 (行数, 列数)。

    支持的意图格式："1x3"、"2x2"、"vertical"、"horizontal"、
    "grid_1x3"、"grid_2x2"、"vertical_flow"、"horizontal_flow" 等。
    若无法识别，则按 stage 数量给出默认网格。
    """
    intent = (intent or "").strip().lower()

    # 直接识别 rows x cols
    m = re.match(r"(?:grid[_-])?(\d+)x(\d+)", intent)
    if m:
        return int(m.group(1)), int(m.group(2))

    if intent in ("vertical", "vertical_flow", "col", "column"):
        return stage_count, 1
    if intent in ("horizontal", "horizontal_flow", "row"):
        return 1, stage_count

    # 默认按数量推断
    if stage_count <= 3:
        return 1, stage_count
    if stage_count <= 4:
        return 2, 2
    if stage_count <= 6:
        return 2, 3
    if stage_count <= 9:
        return 3, 3
    return (stage_count + 2) // 3, 3


def _diagram_type(structure: dict, content: list[dict]) -> str:
    """从结构或内容节点推断信息图类型。"""
    declared = str(structure.get("diagram_type", "")).strip().lower()
    if declared:
        return declared
    for item in content:
        candidate = str(item.get("diagram_type", "")).strip().lower()
        if candidate:
            return candidate
    return "cards"


def _normalize_connections(
    structure: dict,
    content: list[dict],
    diagram_type: str,
    semantic_connections: list[dict] | None = None,
) -> list[dict]:
    """以分析器语义为准过滤连接；flow/timeline 缺失时按顺序兜底。"""
    allowed_types = {"arrow", "dashed_arrow", "feedback_arrow"}
    stage_ids = [item.get("id", index) for index, item in enumerate(content)]
    valid_ids = set(stage_ids)
    raw_connections = (
        semantic_connections
        if semantic_connections is not None
        else structure.get("connections", [])
    )
    connections: list[dict] = []
    seen: set[tuple[object, object, str]] = set()
    if isinstance(raw_connections, list):
        for item in raw_connections:
            if not isinstance(item, dict):
                continue
            source = item.get("from")
            target = item.get("to")
            connection_type = str(item.get("type", "arrow")).strip().lower()
            if source not in valid_ids or target not in valid_ids or source == target:
                continue
            if connection_type not in allowed_types:
                continue
            key = (source, target, connection_type)
            if key in seen:
                continue
            normalized = {"from": source, "to": target, "type": connection_type}
            label = str(item.get("label", "")).strip()
            if label:
                normalized["label"] = label
            connections.append(normalized)
            seen.add(key)

    if connections or diagram_type not in {"flow", "timeline"}:
        return connections

    normalized: list[dict] = []
    for index in range(max(0, len(content) - 1)):
        relation = str(content[index].get("relation_to_next", "arrow"))
        if relation == "none":
            continue
        normalized.append(
            {
                "from": stage_ids[index],
                "to": stage_ids[index + 1],
                "type": relation if relation else "arrow",
            }
        )
    return normalized


def _grid_cells(
    indices: list[int],
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    rows: int,
    cols: int,
    gap: int,
    weights: list[float] | None = None,
) -> dict[int, tuple[int, int, int, int]]:
    """在指定矩形中为节点计算网格 cell。

    当 rows == 1 且提供 weights 时，按权重分配列宽（内容多的 stage 拿更宽空间），
    否则等分。
    """
    if not indices:
        return {}
    rows = max(1, rows)
    cols = max(1, cols)
    usable_h = max(1, height - gap * (rows - 1))
    cells: dict[int, tuple[int, int, int, int]] = {}

    # 单行 + 有权重 → 按比例分配列宽
    if rows == 1 and weights and len(weights) == len(indices):
        total_weight = max(0.01, sum(max(0.01, w) for w in weights))
        total_usable_w = max(1, width - gap * (len(indices) - 1))
        current_x = x
        for i, (stage_index, w) in enumerate(zip(indices, weights)):
            w = max(0.01, w)
            if i < len(indices) - 1:
                cell_w = max(1, int(w / total_weight * total_usable_w))
            else:
                cell_w = max(1, x + width - current_x)
            cells[stage_index] = (current_x, y, cell_w, usable_h)
            current_x += cell_w + gap
        return cells

    # 等分（默认）
    usable_w = max(1, width - gap * (cols - 1))
    cell_w = max(1, usable_w // cols)
    cell_h = max(1, usable_h // rows)
    for position, stage_index in enumerate(indices):
        row, col = divmod(position, cols)
        cells[stage_index] = (
            x + col * (cell_w + gap),
            y + row * (cell_h + gap),
            cell_w,
            cell_h,
        )
    return cells


def _section_rects(
    section_count: int,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    layout: str,
    gap: int,
) -> dict[int, tuple[int, int, int, int]]:
    """先划分互不相连的大分区，再由各分区自行安排内部 stage。"""
    if section_count <= 0:
        return {}

    normalized = (layout or "").strip().lower().replace("_", "-")
    if normalized in {"focus-right", "sidebar-right", "right-sidebar"} and section_count == 4:
        # 参考教学机制图的编辑式构图：右侧主流程略窄，左上两区刻意不等宽。
        right_w = max(1, int(width * 0.27))
        left_w = max(1, width - right_w - gap)
        top_h = max(1, int((height - gap) * 0.44))
        bottom_h = max(1, height - top_h - gap)
        first_w = max(1, int((left_w - gap) * 0.56))
        return {
            0: (x, y, first_w, top_h),
            1: (x + first_w + gap, y, left_w - first_w - gap, top_h),
            2: (x + left_w + gap, y, right_w, height),
            3: (x, y + top_h + gap, left_w, bottom_h),
        }

    if normalized in {"vertical", "vertical-flow", "column"}:
        rows, cols = section_count, 1
    elif normalized in {"horizontal", "horizontal-flow", "row"}:
        rows, cols = 1, section_count
    elif normalized:
        rows, cols = _parse_layout_intent(normalized, section_count)
    elif section_count <= 3:
        # 横向画布中优先把阶段做成彼此独立的列，避免退化成连续的全宽卡片带。
        rows, cols = 1, section_count
    elif section_count <= 4:
        rows, cols = 2, 2
    else:
        rows, cols = _parse_layout_intent("", section_count)

    return _grid_cells(
        list(range(section_count)),
        x=x,
        y=y,
        width=width,
        height=height,
        rows=rows,
        cols=cols,
        gap=gap,
    )


def _section_stage_shape(layout: str, count: int, width: int, height: int) -> tuple[int, int]:
    """把分区自己的布局意图解析为内部 stage 的行列数。"""
    normalized = (layout or "").strip().lower().replace("_", "-")
    if normalized in {"horizontal", "horizontal-sequence", "left-right", "row"}:
        return 1, max(1, count)
    if normalized in {"vertical", "vertical-stack", "column"}:
        return max(1, count), 1
    if re.match(r"(?:grid[-])?\d+x\d+", normalized):
        return _parse_layout_intent(normalized, count)
    if count <= 1:
        return 1, 1
    if width >= height * 1.25:
        return 1, count
    if height >= width * 1.25:
        return count, 1
    cols = min(2, count)
    return (count + cols - 1) // cols, cols


def _connection_corridors(sections: list[dict]) -> list[dict]:
    """提取 section 之间无遮挡的留白走廊，供箭头和标签优先布线。"""
    corridors: list[dict] = []
    for first_index, first in enumerate(sections):
        ax, ay, aw, ah = (int(first.get(key, 0)) for key in ("x", "y", "w", "h"))
        for second_index in range(first_index + 1, len(sections)):
            second = sections[second_index]
            bx, by, bw, bh = (int(second.get(key, 0)) for key in ("x", "y", "w", "h"))
            corridor: dict | None = None

            overlap_top = max(ay, by)
            overlap_bottom = min(ay + ah, by + bh)
            if overlap_bottom > overlap_top:
                if ax + aw <= bx:
                    corridor = {
                        "axis": "horizontal", "x": ax + aw, "y": overlap_top,
                        "w": bx - (ax + aw), "h": overlap_bottom - overlap_top,
                    }
                elif bx + bw <= ax:
                    corridor = {
                        "axis": "horizontal", "x": bx + bw, "y": overlap_top,
                        "w": ax - (bx + bw), "h": overlap_bottom - overlap_top,
                    }

            overlap_left = max(ax, bx)
            overlap_right = min(ax + aw, bx + bw)
            if corridor is None and overlap_right > overlap_left:
                if ay + ah <= by:
                    corridor = {
                        "axis": "vertical", "x": overlap_left, "y": ay + ah,
                        "w": overlap_right - overlap_left, "h": by - (ay + ah),
                    }
                elif by + bh <= ay:
                    corridor = {
                        "axis": "vertical", "x": overlap_left, "y": by + bh,
                        "w": overlap_right - overlap_left, "h": ay - (by + bh),
                    }

            if corridor is None or min(corridor["w"], corridor["h"]) <= 0:
                continue
            blocked = False
            cx, cy, cw, ch = (corridor[key] for key in ("x", "y", "w", "h"))
            for blocker_index, blocker in enumerate(sections):
                if blocker_index in {first_index, second_index}:
                    continue
                sx, sy, sw, sh = (int(blocker.get(key, 0)) for key in ("x", "y", "w", "h"))
                if cx < sx + sw and cx + cw > sx and cy < sy + sh and cy + ch > sy:
                    blocked = True
                    break
            if blocked:
                continue
            corridor.update(
                {
                    "from_section": first.get("id", first_index),
                    "to_section": second.get("id", second_index),
                }
            )
            corridors.append(corridor)
    return corridors


def _estimate_text_lines(text: str, slot_width: int, font_size: int = 16) -> int:
    """粗略估算文字在给定宽度下需要几行。

    中文字符按 font_size px 宽，拉丁字符按 font_size * 0.6 宽计算。
    """
    if not text:
        return 0
    total_width = sum(
        font_size if "一" <= c <= "鿿" or "　" <= c <= "〿" else font_size * 0.6
        for c in str(text)
    )
    return max(1, math.ceil(total_width / max(1, slot_width)))


def _content_slots(
    x: int,
    y: int,
    width: int,
    height: int,
    stage: dict | None = None,
) -> dict[str, dict[str, int]]:
    """在 stage 内为前景元素划分互不重叠的槽位。

    根据实际内容动态分配空间：无 visuals 则不保留 icon 槽，
    无 metric 则不保留 metric 槽；title/description 按文字长度估算行数。
    """
    padding = max(10, min(18, width // 10, height // 20))
    gap = max(4, min(8, height // 40))
    inner_x = x + padding
    inner_y = y + padding
    inner_w = max(1, width - padding * 2)
    available = max(4, height - padding * 2)

    # 1. 根据 stage 实际内容估算每个槽位的自然高度（不超过上限）
    _MAX_H = {"icon": 72, "title": 56, "description": 110, "metric": 44}
    active: list[tuple[str, int]] = []

    if stage:
        if stage.get("visuals"):
            h = min(_MAX_H["icon"], max(32, int(available * 0.18)))
            active.append(("icon", h))

        title_text = str(stage.get("title", "") or "")
        if title_text:
            title_lines = _estimate_text_lines(title_text, inner_w, 18)
            h = min(_MAX_H["title"], max(24, title_lines * 22))
            active.append(("title", h))

        desc_text = str(stage.get("description", "") or "")
        if desc_text:
            desc_lines = _estimate_text_lines(desc_text, inner_w, 14)
            h = min(_MAX_H["description"], max(20, desc_lines * 16))
            active.append(("description", h))

        if stage.get("metric"):
            h = min(_MAX_H["metric"], max(20, int(available * 0.12)))
            active.append(("metric", h))

    # 2. 没有任何内容时使用等分兜底
    if not active:
        count = 4
        per = max(1, (available - gap * (count - 1)) // count)
        active = [("icon", per), ("title", per), ("description", per), ("metric", per)]

    # 3. 仅当总高度超出可用空间才等比缩小；从来不放大（多余空间留给 cell 内边距）
    total_gaps = gap * (len(active) - 1) if len(active) > 1 else 0
    total_needed = sum(h for _, h in active)
    available_slots = available - total_gaps

    if total_needed > available_slots > 0:
        scale = available_slots / total_needed
        active = [(role, max(1, int(h * scale))) for role, h in active]

    # 4. 放置槽位，最后一个吸收取整误差
    slots: dict[str, dict[str, int]] = {}
    cursor = inner_y
    for i, (role, slot_h) in enumerate(active):
        if i < len(active) - 1:
            slots[role] = {"x": inner_x, "y": cursor, "w": inner_w, "h": slot_h}
            cursor += slot_h + gap
        else:
            last_h = max(1, available - (cursor - inner_y))
            slots[role] = {"x": inner_x, "y": cursor, "w": inner_w, "h": last_h}

    return slots


def _connection_label_slot(
    source: dict,
    target: dict,
    *,
    canvas_w: int,
    canvas_h: int,
    label: str = "",
) -> dict[str, int]:
    """优先把连接标签放在箭头旁边的 callout 区，而不是压在线上。

    根据标签文字长度估算槽位宽度，避免标签越出槽位。
    """
    sx, sy, sw, sh = (int(source[key]) for key in ("x", "y", "w", "h"))
    tx, ty, tw, th = (int(target[key]) for key in ("x", "y", "w", "h"))
    source_right = sx + sw
    source_bottom = sy + sh
    target_right = tx + tw
    target_bottom = ty + th

    # 根据标签文字长度估算所需宽度（中文字符约 16px/字，英文约 9px/字符）
    label_len = len(label)
    est_label_w = min(200, max(60, label_len * 14))
    est_label_h = min(32, max(20, 28))
    label_gap = 12

    if source_right <= tx or target_right <= sx:
        left = source_right if source_right <= tx else target_right
        right = tx if source_right <= tx else sx
        corridor_w = max(1, right - left)
        slot_w = min(max(est_label_w, min(corridor_w, est_label_w + 20)), max(est_label_w, corridor_w))
        slot_h = est_label_h
        center_y = (max(sy, ty) + min(source_bottom, target_bottom)) // 2
        x = left + (right - left - slot_w) // 2
        y_above = center_y - slot_h - label_gap
        y_below = center_y + label_gap
        if y_above >= 0:
            y = y_above
        else:
            y = y_below
    elif source_bottom <= ty or target_bottom <= sy:
        top = source_bottom if source_bottom <= ty else target_bottom
        bottom = ty if source_bottom <= ty else sy
        slot_h = est_label_h
        slot_w = est_label_w
        center_x = (max(sx, tx) + min(source_right, target_right)) // 2
        y = top + (bottom - top - slot_h) // 2
        x_right = center_x + label_gap
        x_left = center_x - slot_w - label_gap
        if x_right + slot_w <= canvas_w:
            x = x_right
        else:
            x = x_left
    else:
        slot_w = est_label_w
        slot_h = est_label_h
        midpoint_x = (sx + sw // 2 + tx + tw // 2) // 2
        midpoint_y = (sy + sh // 2 + ty + th // 2) // 2
        x = midpoint_x - slot_w // 2
        y = midpoint_y - slot_h - label_gap

    return {
        "x": max(0, min(x, canvas_w - slot_w)),
        "y": max(0, min(y, canvas_h - slot_h)),
        "w": slot_w,
        "h": slot_h,
    }


def _round_dimension(value: float, *, step: int = 20) -> int:
    return int(math.ceil(value / step) * step)


def _clamp_dimension(value: float, *, low: int, high: int) -> int:
    return int(max(low, min(high, _round_dimension(value))))


def _text_len(value: Any) -> int:
    return len(str(value or "").strip())


def _stage_text_load(stage: dict) -> int:
    texts = stage.get("texts") if isinstance(stage.get("texts"), list) else []
    metric = stage.get("metric") if isinstance(stage.get("metric"), dict) else {}
    return (
        _text_len(stage.get("title"))
        + _text_len(stage.get("description")) * 2
        + sum(_text_len(item) for item in texts)
        + _text_len(metric.get("label"))
    )


def _content_profile(content: list[dict]) -> dict[str, float]:
    stages = [item for item in content if isinstance(item, dict)]
    count = len(stages)
    text_loads = [_stage_text_load(stage) for stage in stages]
    visual_count = sum(
        len(stage.get("visuals", []))
        for stage in stages
        if isinstance(stage.get("visuals"), list)
    )
    metric_count = sum(1 for stage in stages if stage.get("metric"))
    total_text = float(sum(text_loads))
    return {
        "count": float(count),
        "total_text": total_text,
        "avg_text": total_text / max(1, count),
        "max_text": float(max(text_loads) if text_loads else 0),
        "visual_count": float(visual_count),
        "metric_count": float(metric_count),
    }


def _base_canvas_preset(
    structure: dict,
    content: list[dict],
    diagram_type: str,
    layout_style: str,
) -> str:
    raw_sections = [
        item for item in structure.get("sections", []) if isinstance(item, dict)
    ]
    section_titles = {
        str(item.get("section", "main")) for item in content if isinstance(item, dict)
    }
    section_count = max(len(raw_sections), len(section_titles - {"main"}))
    section_layout = str(structure.get("section_layout", "")).strip().lower().replace("_", "-")

    if (
        diagram_type in {"mechanism", "system"}
        and (
            layout_style == "mechanism-storyboard"
            or _should_use_storyboard_layout(
                diagram_type,
                layout_style,
                content,
                structure.get("connections", []),
                raw_sections,
                structure,
            )
        )
    ):
        return "wide"
    if diagram_type in {"flow", "timeline"}:
        return "wide"
    if diagram_type in {"mechanism", "system"} and (
        section_count >= 3
        or section_layout in {"horizontal", "horizontal-flow", "focus-right", "sidebar-right"}
    ):
        return "wide"
    if diagram_type == "compare" and len(content) >= 3:
        return "wide"
    return "standard"


def _estimated_grid_shape(structure: dict, stage_count: int, diagram_type: str) -> tuple[int, int]:
    intent = str(structure.get("layout", ""))
    if diagram_type in {"flow", "timeline"}:
        if intent.strip().lower() in {"vertical", "vertical_flow"}:
            return max(1, stage_count), 1
        return 1, max(1, stage_count)
    return _parse_layout_intent(intent, stage_count)


def _dynamic_canvas_size(
    structure: dict,
    content: list[dict],
    *,
    diagram_type: str,
    layout_style: str,
    base_preset: str,
) -> tuple[int, int, str]:
    """按内容密度推导画布尺寸，避免内容多时挤爆、内容少时太空。"""
    presets = {
        "wide": (1400, 720),
        "standard": (1200, 800),
        "square": (960, 960),
        "tall": (960, 1200),
    }
    base_w, base_h = presets.get(base_preset, presets["standard"])
    profile = _content_profile(content)
    count = int(profile["count"])
    avg_text = profile["avg_text"]
    max_text = profile["max_text"]
    rows, cols = _estimated_grid_shape(structure, max(1, count), diagram_type)
    margin = max(8, int(structure.get("margin", 32)))
    node_gap = max(margin, 64)
    header_h = min(72, max(48, base_h // 10))

    if count <= 0:
        return base_w, base_h, base_preset

    text_w_bonus = min(140, avg_text * 1.8 + max(0, max_text - 46) * 0.5)
    text_h_bonus = min(180, avg_text * 2.2 + max(0, max_text - 60) * 0.75)

    if layout_style == "mechanism-storyboard" or (
        diagram_type in {"mechanism", "system"}
        and _should_use_storyboard_layout(
            diagram_type,
            layout_style,
            content,
            structure.get("connections", []),
            [item for item in structure.get("sections", []) if isinstance(item, dict)],
            structure,
        )
    ):
        columns = min(4, max(1, count))
        row_count = max(1, math.ceil(count / columns))
        target_w = margin * 2 + columns * (270 + text_w_bonus * 0.45) + (columns - 1) * node_gap
        target_h = margin * 2 + header_h + 28 + row_count * (250 + text_h_bonus * 0.55) + (row_count - 1) * node_gap
        width = max(base_w if count >= 4 else 980, target_w)
        height = max(620, target_h)
        return (
            _clamp_dimension(width, low=_CANVAS_MIN_W, high=_CANVAS_MAX_W),
            _clamp_dimension(height, low=_CANVAS_MIN_H, high=_CANVAS_MAX_H),
            "adaptive-wide" if width >= 1180 else "adaptive-standard",
        )

    if diagram_type == "flow":
        node_w = 180 + min(50, avg_text * 0.6)
        gap = 80
        target_w = margin * 2 + count * node_w + (count - 1) * gap
        target_w = min(target_w, 1400)  # flow 不超过 wide 画布
        target_h = 480 + min(120, avg_text * 1.5)
        return (
            _clamp_dimension(max(960, target_w), low=960, high=1400),
            _clamp_dimension(target_h, low=500, high=800),
            "adaptive-wide",
        )

    if diagram_type == "timeline":
        node_w = 160 + min(50, avg_text)
        gap = 100  # milestone 间固定间距 100px
        target_w = margin * 2 + count * node_w + (count - 1) * gap
        target_w = min(target_w, 1400)  # timeline 不超过 wide 画布
        target_h = 600 + min(160, avg_text * 1.8)
        return (
            _clamp_dimension(max(960, target_w), low=960, high=1400),
            _clamp_dimension(target_h, low=580, high=980),
            "adaptive-wide",
        )

    if diagram_type == "compare":
        target_w = margin * 2 + count * 120 + (count - 1) * 60 + 160  # 柱子+间距+图标右侧留白
        target_w = min(target_w, 1100)
        target_h = 560 + min(120, avg_text * 1.4)
        return (
            _clamp_dimension(max(900, target_w), low=900, high=1100),
            _clamp_dimension(target_h, low=520, high=780),
            "adaptive-compare",
        )

    if diagram_type in {"mechanism", "system"}:
        raw_sections = [item for item in structure.get("sections", []) if isinstance(item, dict)]
        section_titles = {str(item.get("section", "main")) for item in content if isinstance(item, dict)}
        section_count = max(len(raw_sections), len(section_titles - {"main"}), 1)
        if section_count >= 3:
            target_w = margin * 2 + section_count * (330 + text_w_bonus * 0.35) + (section_count - 1) * max(52, margin * 2)
            target_h = 680 + min(220, avg_text * 1.6)
            return (
                _clamp_dimension(max(base_w, target_w), low=1100, high=_CANVAS_MAX_W),
                _clamp_dimension(max(base_h, target_h), low=650, high=_CANVAS_MAX_H),
                "adaptive-wide",
            )
        target_w = margin * 2 + cols * (250 + text_w_bonus) + (cols - 1) * node_gap
        target_h = margin * 2 + header_h + 28 + rows * (210 + text_h_bonus) + (rows - 1) * node_gap
        return (
            _clamp_dimension(max(900, target_w), low=860, high=_CANVAS_MAX_W),
            _clamp_dimension(max(600, target_h), low=580, high=_CANVAS_MAX_H),
            "adaptive-standard",
        )

    target_cell_w = 230 + text_w_bonus
    target_cell_h = 190 + text_h_bonus
    target_w = margin * 2 + cols * target_cell_w + (cols - 1) * node_gap
    target_h = margin * 2 + header_h + 28 + rows * target_cell_h + (rows - 1) * node_gap
    sparse = count <= 2 and avg_text < 28 and profile["visual_count"] <= 1
    min_w = 800 if sparse else 960
    min_h = 540 if sparse else 640
    return (
        _clamp_dimension(max(min_w, target_w), low=_CANVAS_MIN_W, high=_CANVAS_MAX_W),
        _clamp_dimension(max(min_h, target_h), low=_CANVAS_MIN_H, high=_CANVAS_MAX_H),
        "adaptive-standard",
    )


def _canvas_size(structure: dict, content: list[dict]) -> tuple[int, int, str]:
    """按图类型与外层分区选择稳定画布比例，避免所有图都锁死为 1200×800。"""
    presets = {
        "wide": (1400, 720),
        "standard": (1200, 800),
        "square": (960, 960),
        "tall": (960, 1200),
    }
    requested = str(structure.get("canvas_preset", "")).strip().lower()
    # custom 不走直接返回——必须经过动态计算器，根据内容实际尺寸决定画布
    if requested in presets:
        base_preset = requested
    else:
        diagram_type = _diagram_type(structure, content)
        layout_style = str(structure.get("layout_style", "")).strip().lower().replace("_", "-")
        base_preset = _base_canvas_preset(structure, content, diagram_type, layout_style)
        return _dynamic_canvas_size(
            structure,
            content,
            diagram_type=diagram_type,
            layout_style=layout_style,
            base_preset=base_preset,
        )

    diagram_type = _diagram_type(structure, content)
    layout_style = str(structure.get("layout_style", "")).strip().lower().replace("_", "-")
    return _dynamic_canvas_size(
        structure,
        content,
        diagram_type=diagram_type,
        layout_style=layout_style,
        base_preset=base_preset,
    )


def _stage_weights(normalized_stages: list[dict]) -> list[float]:
    """根据每个 stage 的内容丰富度计算权重，用于非等分布局。

    权重越大 → 在单行布局中占更宽的空间。
    """
    weights: list[float] = []
    for stage in normalized_stages:
        w = 1.0  # 基准
        if stage.get("visuals"):
            w += 0.5
        title_text = str(stage.get("title", "") or "")
        if title_text:
            w += min(2.0, len(title_text) / 4.0)
        desc_text = str(stage.get("description", "") or "")
        if desc_text:
            w += min(3.0, len(desc_text) / 6.0)
        if stage.get("metric"):
            w += 0.3
        weights.append(max(0.1, w))
    return weights


def _storyboard_signal_categories(stage: dict) -> set[str]:
    """从通用语义中识别是否适合教学分镜，不依赖具体题目。"""
    categories: set[str] = set()
    if str(stage.get("visual_brief") or stage.get("scene_type") or "").strip():
        categories.add("scene")

    block_type = str(stage.get("block_type", "")).strip().lower()
    role = str(stage.get("role", "")).strip().lower()
    if block_type == "probability_box":
        categories.add("probability")
    if block_type == "decision_box" or role == "feedback":
        categories.add("feedback")

    semantic_text = " ".join(
        str(value).lower()
        for value in (
            stage.get("title", ""),
            stage.get("description", ""),
            stage.get("section", ""),
            " ".join(str(item) for item in stage.get("texts", []) if item),
        )
    )
    keyword_groups = {
        "probability": ("概率", "分布", "语义空间", "latent", "probability"),
        "iteration": ("迭代", "扩散", "去噪", "优化", "推理", "iteration", "generate"),
        "feedback": ("评估", "反馈", "品控", "检查", "判断", "问题", "quality", "feedback"),
        "sample": ("样本", "训练", "示例", "数据", "sample", "training"),
        "before-after": ("前后", "输入输出", "before", "after"),
        "pipeline": ("流程", "步骤", "pipeline", "step"),
    }
    for category, words in keyword_groups.items():
        if any(word in semantic_text for word in words):
            categories.add(category)
    return categories


def _meaningful_sections(raw_sections: list[dict]) -> list[dict]:
    return [
        section
        for section in raw_sections
        if str(section.get("title", "main")).strip().lower() not in {"", "main"}
    ]


def _should_use_storyboard_layout(
    diagram_type: str,
    layout_style: str,
    normalized_stages: list[dict],
    connections: list[dict],
    explicit_sections: list[dict],
    structure: dict,
) -> bool:
    """富机制说明优先分镜；普通线性流程继续保持流程图。"""
    normalized_style = layout_style.strip().lower().replace("_", "-")
    if normalized_style == "mechanism-storyboard":
        return True
    if diagram_type not in {"mechanism", "system"}:
        return False
    if not 3 <= len(normalized_stages) <= 8:
        return False

    section_layout = str(structure.get("section_layout", "")).strip().lower().replace("_", "-")
    if section_layout in {"focus-right", "sidebar-right", "right-sidebar"}:
        return False
    if len(_meaningful_sections(explicit_sections)) >= 2 and normalized_style not in {"flow-mechanism", "horizontal-flow"}:
        return False

    scene_count = sum(
        1
        for stage in normalized_stages
        if str(stage.get("visual_brief") or stage.get("scene_type") or "").strip()
    )
    text_scene_count = sum(
        1
        for stage in normalized_stages
        if isinstance(stage.get("texts"), list) and len([item for item in stage["texts"] if str(item).strip()]) >= 2
    )
    has_feedback_connection = any(
        str(connection.get("type", "")).strip().lower() == "feedback_arrow"
        for connection in connections
        if isinstance(connection, dict)
    )
    categories: set[str] = set()
    for stage in normalized_stages:
        categories.update(_storyboard_signal_categories(stage))

    return (
        scene_count >= 2
        or text_scene_count >= 2
        or has_feedback_connection
        or len(categories - {"pipeline"}) >= 2
    )


def _sync_template_label_slots(template_slots: dict, connections: list[dict]) -> None:
    """把连接标签槽同步到模板箭头说明中，方便 CodeGenerator 按模板布置。"""
    label_slots = {
        (
            item.get("from"),
            item.get("to"),
            str(item.get("type", "arrow")),
        ): item.get("label_slot")
        for item in connections
        if isinstance(item, dict) and item.get("label_slot")
    }
    for list_key in ("arrows", "feedback_arrows", "global_connections", "feedback_connections", "relationship_labels", "connection_paths"):
        arrows = template_slots.get(list_key)
        if not isinstance(arrows, list):
            continue
        for arrow in arrows:
            if not isinstance(arrow, dict):
                continue
            key = (
                arrow.get("from"),
                arrow.get("to"),
                str(arrow.get("type", "arrow")),
            )
            if key in label_slots:
                arrow["label_slot"] = label_slots[key]


def _ensure_storyboard_connections(
    connections: list[dict],
    normalized_stages: list[dict],
) -> list[dict]:
    """教学分镜至少需要主叙事箭头；语义缺失时按阶段顺序补最小连接。"""
    if len(normalized_stages) < 3:
        return connections
    minimum = min(2, len(normalized_stages) - 1)
    if len(connections) >= minimum:
        return connections

    completed = [dict(item) for item in connections]
    seen = {
        (item.get("from"), item.get("to"), str(item.get("type", "arrow")))
        for item in completed
        if isinstance(item, dict)
    }
    stage_ids = [stage.get("id", index) for index, stage in enumerate(normalized_stages)]
    for index in range(len(stage_ids) - 1):
        key = (stage_ids[index], stage_ids[index + 1], "arrow")
        if key in seen:
            continue
        completed.append({"from": key[0], "to": key[1], "type": "arrow"})
        seen.add(key)
        if len(completed) >= minimum:
            break
    return completed


def _use_flow_mechanism_layout(
    diagram_type: str,
    layout_style: str,
    normalized_stages: list[dict],
    raw_sections: list[dict],
    structure: dict,
) -> bool:
    """基础原理图优先走一层机制流程，避免总览/详解双层重复。"""
    normalized_style = layout_style.strip().lower().replace("_", "-")
    if normalized_style == "mechanism-storyboard":
        return False
    if normalized_style == "flow-mechanism":
        return True
    if diagram_type != "mechanism":
        return False
    if len(normalized_stages) > 6:
        return False
    section_layout = str(structure.get("section_layout", "")).strip().lower().replace("_", "-")
    if section_layout in {"focus-right", "sidebar-right", "right-sidebar"}:
        return False
    if len(_meaningful_sections(raw_sections)) >= 2:
        return False
    return True


def compute_layout(
    structure: dict,
    content: list[dict],
    semantic_connections: list[dict] | None = None,
) -> dict:
    """把 StructurePlanner 的「布局意图」解析成确定性的 stage 矩形坐标。

    输入 structure 应包含：
      - width / height：画布尺寸
      - layout：布局意图字符串（如 "1x3"、"2x2"）
      - margin（可选）：单元格间距，默认 20
      - stages：阶段元信息列表（允许不含 x/y/w/h）

    返回：与旧结构兼容的字典，每个 stage 会被补充 x/y/w/h，并标记 computed=True。
    """
    # 教学信息图保留稳定尺寸，但画布比例由内容结构决定，不再统一锁死为 1200×800。
    width, height, canvas_preset = _canvas_size(structure, content)
    margin = max(8, int(structure.get("margin", 20)))
    header_h = min(72, max(48, height // 10))
    header_gap = max(12, margin)
    content_y = margin + header_h + header_gap
    content_h = max(1, height - content_y - margin)
    node_gap = max(margin, 44)
    header = {
        "x": margin,
        "y": margin,
        "w": max(1, width - margin * 2),
        "h": header_h,
    }
    planned_stages = [
        dict(item) for item in structure.get("stages", []) if isinstance(item, dict)
    ]

    # 内容分析是节点语义的事实源；规划器只能补充 layout/notes 等视觉组织字段。
    stage_count = len(content) if content else len(planned_stages)
    planned_by_id = {
        item.get("id"): item for item in planned_stages if item.get("id") is not None
    }
    normalized_stages: list[dict] = []
    for idx in range(stage_count):
        content_item = content[idx] if idx < len(content) else {}
        content_id = content_item.get("id", idx)
        planned = planned_by_id.get(
            content_id,
            planned_stages[idx] if idx < len(planned_stages) else {},
        )
        copy = dict(planned)
        copy.update(content_item)
        copy["id"] = content_id
        copy.setdefault("title", content_item.get("title", f"阶段 {idx + 1}"))
        copy.setdefault("section", content_item.get("section", "main"))
        copy.setdefault("block_type", content_item.get("block_type", "module_box"))
        copy.setdefault("role", content_item.get("role", "middle"))
        normalized_stages.append(copy)

    diagram_type = _diagram_type(structure, content)
    intent = str(structure.get("layout", ""))
    layout_style = str(structure.get("layout_style", "card-grid"))
    raw_sections = [
        dict(item) for item in structure.get("sections", []) if isinstance(item, dict)
    ]
    explicit_sections = [dict(item) for item in raw_sections]
    declared_titles = [str(item.get("title", "main")) for item in raw_sections]
    content_titles = [str(stage.get("section", "main")) for stage in normalized_stages]
    for title in content_titles:
        if title not in declared_titles and (title != "main" or raw_sections):
            raw_sections.append(
                {"id": f"section_{len(raw_sections)}", "title": title, "layout": "grid"}
            )
            declared_titles.append(title)

    normalized_connections = _normalize_connections(
        structure,
        normalized_stages,
        diagram_type,
        semantic_connections,
    )
    promoted_to_storyboard = False
    if _should_use_storyboard_layout(
        diagram_type,
        layout_style,
        normalized_stages,
        normalized_connections,
        explicit_sections,
        structure,
    ):
        layout_style = "mechanism-storyboard"
        intent = intent or "section-aware-rows"
        promoted_to_storyboard = True
        normalized_connections = _ensure_storyboard_connections(
            normalized_connections,
            normalized_stages,
        )
    elif _use_flow_mechanism_layout(
        diagram_type,
        layout_style,
        normalized_stages,
        raw_sections,
        structure,
    ):
        layout_style = "flow-mechanism"
    if (
        diagram_type in {"mechanism", "system"}
        and layout_style.strip().lower().replace("_", "-") == "mechanism-storyboard"
    ):
        normalized_connections = _ensure_storyboard_connections(
            normalized_connections,
            normalized_stages,
        )
    if promoted_to_storyboard and not str(canvas_preset).endswith("wide"):
        width, height, canvas_preset = 1400, 720, "wide"
        header_h = min(72, max(48, height // 10))
        header_gap = max(12, margin)
        content_y = margin + header_h + header_gap
        content_h = max(1, height - content_y - margin)
        header = {
            "x": margin,
            "y": margin,
            "w": max(1, width - margin * 2),
            "h": header_h,
        }

    direct_template = compute_direct_layout_template(
        diagram_type=diagram_type,
        layout_style=layout_style,
        stages=normalized_stages,
        connections=normalized_connections,
        sections=raw_sections,
        width=width,
        height=height,
        margin=margin,
        content_y=content_y,
        content_h=content_h,
    )
    if direct_template is not None:
        required_width = min(
            _CANVAS_MAX_W,
            max(width, _round_dimension(float(direct_template.get("minimum_canvas_width", width)))),
        )
        required_height = min(
            _CANVAS_MAX_H,
            max(height, _round_dimension(float(direct_template.get("minimum_canvas_height", height)))),
        )
        if (required_width, required_height) != (width, height):
            width, height = required_width, required_height
            canvas_preset = "adaptive-wide"
            header_h = min(72, max(48, height // 10))
            header_gap = max(12, margin)
            content_y = margin + header_h + header_gap
            content_h = max(1, height - content_y - margin)
            header = {
                "x": margin,
                "y": margin,
                "w": max(1, width - margin * 2),
                "h": header_h,
            }
            direct_template = compute_direct_layout_template(
                diagram_type=diagram_type,
                layout_style=layout_style,
                stages=normalized_stages,
                connections=normalized_connections,
                sections=raw_sections,
                width=width,
                height=height,
                margin=margin,
                content_y=content_y,
                content_h=content_h,
            )
    if direct_template is not None:
        stage_by_id = {stage.get("id"): stage for stage in normalized_stages}
        for connection in normalized_connections:
            if not connection.get("label"):
                continue
            if isinstance(connection.get("label_slot"), dict):
                continue
            source = stage_by_id.get(connection.get("from"))
            target = stage_by_id.get(connection.get("to"))
            if source and target:
                connection["label_slot"] = _connection_label_slot(
                    source,
                    target,
                    canvas_w=width,
                    canvas_h=height,
                    label=connection.get("label", ""),
                )
        _sync_template_label_slots(
            direct_template["template_slots"],
            normalized_connections,
        )
        result = dict(structure)
        result.update(
            {
                "width": width,
                "height": height,
                "canvas_preset": canvas_preset,
                "diagram_type": diagram_type,
                "layout": intent,
                "layout_style": layout_style,
                "section_layout": "",
                "margin": margin,
                "computed": True,
                "header": header,
                "sections": [],
                "connection_corridors": [],
                "stages": normalized_stages,
                "connections": normalized_connections,
                "template_id": direct_template["template_id"],
                "template_slots": direct_template["template_slots"],
                "render_contract": direct_template["render_contract"],
            }
        )
        return result

    # 计算内容权重，用于非等分布局
    stage_weights = _stage_weights(normalized_stages)

    section_mode = diagram_type in {"mechanism", "system"} and len(raw_sections) >= 2
    cell_map: dict[int, tuple[int, int, int, int]] = {}
    computed_sections: list[dict] = []

    if section_mode:
        section_layout = str(structure.get("section_layout", ""))
        normalized_section_layout = section_layout.strip().lower().replace("_", "-")
        editorial_layout = normalized_section_layout in {
            "focus-right", "sidebar-right", "right-sidebar",
        }
        section_gap = max(80, margin * 3) if editorial_layout else max(64, margin * 2)
        stage_gap = max(32, margin)
        section_map = _section_rects(
            len(raw_sections),
            x=margin,
            y=content_y,
            width=max(1, width - margin * 2),
            height=content_h,
            layout=section_layout,
            gap=section_gap,
        )
        for section_index, section in enumerate(raw_sections):
            title = str(section.get("title", "main"))
            indices = [
                idx for idx, stage in enumerate(normalized_stages)
                if str(stage.get("section", "main")) == title
            ]
            section_x, section_y, section_w, section_h = section_map.get(
                section_index,
                (margin, content_y, max(1, width - margin * 2), content_h),
            )
            computed = dict(section)
            computed.update(
                {"x": section_x, "y": section_y, "w": section_w, "h": section_h}
            )
            computed_sections.append(computed)
            if indices:
                section_padding = max(16, margin)
                title_band_h = max(48, margin * 2)
                inner_x = section_x + section_padding
                inner_y = section_y + title_band_h
                inner_w = max(1, section_w - section_padding * 2)
                inner_h = max(1, section_h - title_band_h - section_padding)
                section_inner_rows, section_inner_cols = _section_stage_shape(
                    str(section.get("layout", "")),
                    len(indices),
                    inner_w,
                    inner_h,
                )
                cell_map.update(
                    _grid_cells(
                        indices,
                        x=inner_x,
                        y=inner_y,
                        width=inner_w,
                        height=inner_h,
                        rows=section_inner_rows,
                        cols=section_inner_cols,
                        gap=stage_gap,
                        weights=[stage_weights[i] for i in indices] if section_inner_rows == 1 else None,
                    )
                )
    else:
        if diagram_type in {"flow", "timeline"}:
            rows, cols = (
                (stage_count, 1)
                if intent in {"vertical", "vertical_flow"}
                else (1, max(1, stage_count))
            )
        else:
            rows, cols = _parse_layout_intent(intent, stage_count)
        cell_map = _grid_cells(
            list(range(stage_count)),
            x=margin,
            y=content_y,
            width=max(1, width - margin * 2),
            height=content_h,
            rows=rows,
            cols=cols,
            gap=node_gap,
            weights=stage_weights if rows == 1 else None,
        )

        for section in raw_sections:
            title = str(section.get("title", "main"))
            members = [
                cell_map[idx]
                for idx, stage in enumerate(normalized_stages)
                if str(stage.get("section", "main")) == title and idx in cell_map
            ]
            if not members:
                continue
            xmin = max(0, min(cell[0] for cell in members) - margin // 2)
            ymin = max(0, min(cell[1] for cell in members) - margin // 2)
            xmax = min(width, max(cell[0] + cell[2] for cell in members) + margin // 2)
            ymax = min(height, max(cell[1] + cell[3] for cell in members) + margin // 2)
            computed = dict(section)
            computed.update({"x": xmin, "y": ymin, "w": xmax - xmin, "h": ymax - ymin})
            computed_sections.append(computed)

    for idx, stage in enumerate(normalized_stages):
        x, y, cell_w, cell_h = cell_map.get(idx, (margin, margin, 1, 1))
        stage.update(
            {
                "x": x,
                "y": y,
                "w": cell_w,
                "h": cell_h,
                "slots": _content_slots(x, y, cell_w, cell_h, stage=stage),
            }
        )

    stage_by_id = {stage.get("id"): stage for stage in normalized_stages}
    for connection in normalized_connections:
        if not connection.get("label"):
            continue
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if source and target:
            connection["label_slot"] = _connection_label_slot(
                source,
                target,
                canvas_w=width,
                canvas_h=height,
            )

    template_contract = apply_layout_template(
        diagram_type=diagram_type,
        stages=normalized_stages,
        connections=normalized_connections,
        sections=computed_sections,
        width=width,
        height=height,
        margin=margin,
        content_y=content_y,
        content_h=content_h,
    )

    result = dict(structure)
    result.update(
        {
            "width": width,
            "height": height,
            "canvas_preset": canvas_preset,
            "diagram_type": diagram_type,
            "layout": intent,
            "layout_style": layout_style,
            "section_layout": str(structure.get("section_layout", "")),
            "margin": margin,
            "computed": True,
            "header": header,
            "sections": computed_sections,
            "connection_corridors": _connection_corridors(computed_sections),
            "stages": normalized_stages,
            "connections": normalized_connections,
            "template_id": template_contract["template_id"],
            "template_slots": template_contract["template_slots"],
            "render_contract": template_contract["render_contract"],
        }
    )
    return result


def _feedback_messages(validation: dict | None) -> list[str]:
    if not isinstance(validation, dict):
        return []
    messages: list[str] = []
    for key in ("layout_errors", "hard_errors", "syntax_errors"):
        raw = validation.get(key, [])
        if isinstance(raw, list):
            messages.extend(str(item) for item in raw)
    for key in ("visual_feedback", "retry_hint"):
        raw_text = str(validation.get(key, "") or "")
        if raw_text:
            messages.append(raw_text)
    return messages


def validation_needs_relayout(validation: dict | None) -> bool:
    """判断校验失败是否应先回到布局层处理。"""
    if isinstance(validation, dict) and validation.get("label_layout_errors"):
        return True
    messages = _feedback_messages(validation)
    if not messages:
        return False
    return any(
        keyword in message
        for message in messages
        for keyword in _LAYOUT_FEEDBACK_KEYWORDS
    )


def relayout_for_validation(
    structure: dict,
    content: list[dict],
    *,
    semantic_connections: list[dict] | None = None,
    validation: dict | None = None,
    attempt: int = 0,
) -> tuple[dict, bool]:
    """根据布局类校验反馈重新计算 structure。

    返回 (new_structure, changed)。changed=False 表示反馈不属于排版问题。
    """
    if not validation_needs_relayout(validation):
        return structure, False

    messages = " ".join(_feedback_messages(validation))
    width = int(structure.get("width", 1200) or 1200)
    height = int(structure.get("height", 800) or 800)
    growth = 1.12 + min(0.24, max(0, attempt) * 0.08)
    shrink = 0.90 - min(0.10, max(0, attempt) * 0.03)

    next_width = width
    next_height = height
    if any(token in messages for token in ("过空", "过松", "主体占用不足")):
        next_width = _clamp_dimension(width * shrink, low=_CANVAS_MIN_W, high=_CANVAS_MAX_W)
        next_height = _clamp_dimension(height * shrink, low=_CANVAS_MIN_H, high=_CANVAS_MAX_H)
    else:
        next_width = _clamp_dimension(width * growth, low=_CANVAS_MIN_W, high=_CANVAS_MAX_W)
        next_height = _clamp_dimension(height * growth, low=_CANVAS_MIN_H, high=_CANVAS_MAX_H)

    if (next_width, next_height) == (width, height):
        return structure, False

    relayout_seed = dict(structure)
    relayout_seed.update(
        {
            "canvas_preset": "custom",
            "width": next_width,
            "height": next_height,
            "layout_revision": int(structure.get("layout_revision", 0) or 0) + 1,
            "layout_feedback": messages[:600],
        }
    )
    return compute_layout(
        relayout_seed,
        content,
        semantic_connections=semantic_connections,
    ), True
