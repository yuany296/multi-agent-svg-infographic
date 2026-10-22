"""确定性布局模板。

这些模板不生成 SVG，只把通用 cell 坐标增强成图类型专属的绘制契约。
"""

from __future__ import annotations

from typing import Any

from utils.routing import label_size, route_connection


def _clamp(value: float, low: float, high: float) -> int:
    return int(round(max(low, min(high, value))))


def _slot(x: float, y: float, w: float, h: float) -> dict[str, int]:
    return {
        "x": int(round(x)),
        "y": int(round(y)),
        "w": max(1, int(round(w))),
        "h": max(1, int(round(h))),
    }


def _center(stage: dict[str, Any]) -> tuple[int, int]:
    return (
        int(stage.get("x", 0)) + int(stage.get("w", 0)) // 2,
        int(stage.get("y", 0)) + int(stage.get("h", 0)) // 2,
    )


def _anchor_points(cx: int, cy: int, w: int, h: int) -> dict[str, dict[str, int]]:
    half_w = max(1, w // 2)
    half_h = max(1, h // 2)
    return {
        "left": {"x": cx - half_w, "y": cy},
        "right": {"x": cx + half_w, "y": cy},
        "top": {"x": cx, "y": cy - half_h},
        "bottom": {"x": cx, "y": cy + half_h},
        "center": {"x": cx, "y": cy},
    }


def _short_text(text: str, max_len: int = 12) -> str:
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    for separator in ("。", "；", ";", "，", ",", "、"):
        first_part = text.split(separator, 1)[0].strip()
        if 2 <= len(first_part) <= max_len:
            return first_part
    return text[:max_len].rstrip()


def _block_render(stage: dict[str, Any], diagram_type: str) -> tuple[str, str]:
    block_type = str(stage.get("block_type", "")).strip().lower()
    title = str(stage.get("title", "")).lower()
    visuals = " ".join(
        str(item.get("icon_query", "") or item.get("label", "")).lower()
        for item in stage.get("visuals", [])
        if isinstance(item, dict)
    )
    semantic_text = f"{title} {visuals}"

    if diagram_type == "compare":
        return "bar_item", "bar-column"
    if diagram_type == "timeline":
        return "timeline_milestone", "milestone-dot"
    if diagram_type == "flow":
        if block_type == "decision_box":
            return "decision_node", "decision-diamond"
        if stage.get("role") in {"start", "end"}:
            return "pipeline_step", "terminator-pill"
        return "pipeline_step", "circle-icon"

    if "attention" in semantic_text or "注意力" in semantic_text or "matrix" in semantic_text or "矩阵" in semantic_text:
        return "attention_matrix", "matrix-grid"
    if "token" in semantic_text or "分词" in semantic_text or "词元" in semantic_text:
        return "token_strip", "token-strip"
    if block_type == "probability_box" or "概率" in semantic_text:
        return "probability_bars", "probability-bars"
    if block_type == "decision_box":
        return "state_gate", "decision-diamond"
    if block_type == "result_node" or stage.get("role") == "result":
        return "result_node", "output-badge"
    if "database" in semantic_text or "store" in semantic_text or "存储" in semantic_text:
        return "storage_node", "storage-cylinder"
    if block_type == "text_box":
        return "text_block", "compact-note"
    if block_type == "module_box":
        return "module_block", "module-block"
    return "icon_node", "icon-node"


def _apply_stage_template(
    stage: dict[str, Any],
    *,
    cx: int,
    cy: int,
    shape_w: int,
    shape_h: int,
    render_as: str,
    node_shape: str,
) -> None:
    stage["render_as"] = stage.get("render_as") or render_as
    stage["node_shape"] = stage.get("node_shape") or node_shape
    stage["node_geometry"] = {
        "cx": cx,
        "cy": cy,
        "w": shape_w,
        "h": shape_h,
    }
    stage["anchor_points"] = _anchor_points(cx, cy, shape_w, shape_h)


def _set_safe_area(
    stage: dict[str, Any],
    *,
    cx: int,
    cy: int,
    safe_w: int,
    safe_h: int,
    width: int,
    height: int,
) -> dict[str, int]:
    x = _clamp(cx - safe_w / 2, 0, max(0, width - safe_w))
    y = _clamp(cy - safe_h / 2, 0, max(0, height - safe_h))
    area = {"x": x, "y": y, "w": safe_w, "h": safe_h}
    stage.update(area)
    stage["safe_area"] = dict(area)
    return area


def _even_centers(count: int, *, x1: int, x2: int, y: int) -> list[dict[str, int]]:
    if count <= 0:
        return []
    if count == 1:
        return [{"x": (x1 + x2) // 2, "y": y}]
    step = (x2 - x1) / max(1, count - 1)
    return [{"x": int(round(x1 + step * index)), "y": y} for index in range(count)]


def _quality_targets(width: int, height: int) -> dict[str, float | int]:
    short_side = min(width, height)
    return {
        "occupancy_min": 0.60,
        "occupancy_max": 0.85,
        "min_module_gap": int(round(short_side * 0.03)),
        "min_section_gap": int(round(short_side * 0.04)),
        "arrow_length_ratio_min": 0.30,
        "arrow_length_ratio_max": 0.55,
    }


def _reserved_content_zones(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    for stage in stages:
        for role, slot in (stage.get("slots") or {}).items():
            if role in {"icon", "title", "description", "metric", "scene"} and isinstance(slot, dict):
                zones.append(
                    {
                        "stage_id": stage.get("id"),
                        "role": role,
                        "slot": slot,
                        "padding": 8 if role in {"icon", "metric", "scene"} else 6,
                    }
                )
    return zones


def _segment_crosses_box(
    first: dict[str, float],
    second: dict[str, float],
    box: tuple[float, float, float, float],
) -> bool:
    """简易线段-矩形相交检测，用于标签槽避让验证。"""
    bx, by, bw, bh = box
    x1, y1 = float(first.get("x", 0)), float(first.get("y", 0))
    x2, y2 = float(second.get("x", 0)), float(second.get("y", 0))
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    # 线段包围盒与矩形无交集
    if max_x <= bx or min_x >= bx + bw or max_y <= by or min_y >= by + bh:
        return False
    # 任一端点在矩形内
    if bx <= x1 <= bx + bw and by <= y1 <= by + bh:
        return True
    if bx <= x2 <= bx + bw and by <= y2 <= by + bh:
        return True
    # 垂直线段穿过矩形（x 在矩形 x 范围内，y 跨过矩形 y 范围）
    if abs(x1 - x2) < 0.5 and bx <= x1 <= bx + bw:
        if min_y <= by + bh and max_y >= by:
            return True
    # 水平线段穿过矩形
    if abs(y1 - y2) < 0.5 and by <= y1 <= by + bh:
        if min_x <= bx + bw and max_x >= bx:
            return True
    return False


def _resolve_label_path_overlaps(
    connection_paths: list[dict[str, Any]],
    label_lanes: list[dict[str, Any]],
) -> None:
    """检测并修正与箭头路径重叠的标签槽。

    对于垂直方向(row-gap)的连接，标签槽 x 中心可能与箭头 x
    完全重合，导致渲染后标签压住箭头。此函数将重叠的标签槽
    偏移到箭头一侧。
    """
    path_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path_item in connection_paths:
        key = (
            str(path_item.get("from", "")),
            str(path_item.get("to", "")),
            str(path_item.get("type", "arrow")),
        )
        path_by_key[key] = path_item

    for lane in label_lanes:
        key = (
            str(lane.get("from", "")),
            str(lane.get("to", "")),
            str(lane.get("type", "arrow")),
        )
        path_item = path_by_key.get(key)
        if path_item is None:
            continue
        label_slot = lane.get("label_slot")
        if not isinstance(label_slot, dict):
            continue
        path = path_item.get("path") if isinstance(path_item.get("path"), dict) else path_item
        points = [p for p in path.get("points", []) if isinstance(p, dict)]
        if len(points) < 2:
            continue

        lx = float(label_slot.get("x", 0))
        ly = float(label_slot.get("y", 0))
        lw = float(label_slot.get("w", 0))
        lh = float(label_slot.get("h", 0))
        label_box = (lx, ly, lw, lh)

        overlap = False
        for first, second in zip(points, points[1:]):
            if _segment_crosses_box(first, second, label_box):
                overlap = True
                break
        if not overlap:
            continue

        # 对于垂直箭头（所有点的 x 相近），偏移到箭头右侧
        x_variance = max(
            abs(float(p.get("x", 0)) - float(points[0].get("x", 0)))
            for p in points
        )
        if x_variance < 20:
            new_x = float(points[0].get("x", 0)) + 16
        else:
            # 水平箭头：标签放在箭头路径上方
            new_x = float(lx)

        lane["label_slot"]["x"] = int(new_x)
        if isinstance(path_item.get("label_slot"), dict):
            path_item["label_slot"]["x"] = int(new_x)


def _line_connection_path(start: dict[str, int], end: dict[str, int], connection_type: str = "arrow") -> dict[str, Any]:
    return {
        "kind": "line",
        "points": [
            {"x": int(start.get("x", 0)), "y": int(start.get("y", 0))},
            {"x": int(end.get("x", 0)), "y": int(end.get("y", 0))},
        ],
        "dash": "6 5" if connection_type == "dashed_arrow" else "",
    }


def _route_connection_payload(
    connection: dict[str, Any],
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    start: dict[str, int],
    end: dict[str, int],
    reserved_zones: list[dict[str, Any]],
    width: int,
    height: int,
    label_obstacles: list[tuple[float, float, float, float]],
    source_port: str = "right",
    target_port: str = "left",
    hint_x: list[float] | None = None,
    hint_y: list[float] | None = None,
    label_slot: dict[str, int] | None = None,
    path_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    connection_type = str(connection.get("type", "arrow"))
    if path_override is None:
        routed = route_connection(
            start=start,
            end=end,
            reserved_zones=reserved_zones,
            canvas_w=width,
            canvas_h=height,
            connection_type=connection_type,
            # 有模板专用标签通道时，禁止路由器回退到画布顶部寻找空位。
            label="" if label_slot is not None else str(connection.get("label", "")),
            source_id=source.get("id"),
            target_id=target.get("id"),
            source_port=source_port,
            target_port=target_port,
            hint_x=hint_x,
            hint_y=hint_y,
            label_obstacles=label_obstacles,
        )
    else:
        routed = {
            "path": path_override,
            "source_port": source_port,
            "target_port": target_port,
            "route_score": 0,
            "collision_count": 0,
        }
    payload = {
        "from": connection.get("from"),
        "to": connection.get("to"),
        "type": connection.get("type", "arrow"),
        "start": start,
        "end": end,
        "path": routed["path"],
        "source_port": routed.get("source_port", source_port),
        "target_port": routed.get("target_port", target_port),
        "route_score": routed.get("route_score", 0),
        "collision_count": routed.get("collision_count", 0),
    }
    if label_slot is not None:
        connection["label_slot"] = dict(label_slot)
        payload["label_slot"] = dict(label_slot)
    elif "label_slot" in routed:
        connection["label_slot"] = routed["label_slot"]
        payload["label_slot"] = routed["label_slot"]
    elif isinstance(connection.get("label_slot"), dict):
        payload["label_slot"] = connection["label_slot"]
    return payload


def _direct_flow_layout(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any]:
    path_y = content_y + int(content_h * 0.42)
    path_margin = max(90, margin * 4)
    path_span = width - path_margin * 2
    if len(stages) >= 5:
        path_span = min(path_span, 190 * (len(stages) - 1))
        path_margin = max(path_margin, int((width - path_span) / 2))
    centers = _even_centers(
        len(stages),
        x1=path_margin,
        x2=width - path_margin,
        y=path_y,
    )
    nodes: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        cx = centers[index]["x"]
        cy = centers[index]["y"]
        render_as, node_shape = _block_render(stage, "flow")
        shape_w, shape_h = 104, 104
        if node_shape == "decision-diamond":
            shape_w, shape_h = 120, 96
        elif node_shape == "terminator-pill":
            shape_w, shape_h = 148, 70
        safe_w = 190 if len(stages) <= 6 else 164
        safe_h = 248
        safe = _set_safe_area(
            stage,
            cx=cx,
            cy=cy + 28,
            safe_w=safe_w,
            safe_h=safe_h,
            width=width,
            height=height,
        )
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=shape_w,
            shape_h=shape_h,
            render_as=render_as,
            node_shape=node_shape,
        )
        stage["short_description"] = _short_text(stage.get("description", ""), 12)
        stage["slots"] = {
            "icon": _slot(cx - 44, cy - 44, 88, 88),
            "title": _slot(cx - min(80, safe_w / 2 - 8), cy + shape_h / 2 + 22, min(160, safe_w - 16), 28),
        }
        if stage["short_description"]:
            stage["slots"]["description"] = _slot(cx - min(86, safe_w / 2 - 4), cy + shape_h / 2 + 56, min(172, safe_w - 8), 42)
        if stage.get("metric"):
            stage["slots"]["metric"] = _slot(cx - 60, safe["y"] + safe["h"] - 36, 120, 24)
        nodes.append(
            {
                "stage_id": stage.get("id", index),
                "cx": cx,
                "cy": cy,
                "safe_area": safe,
                "node_shape": stage["node_shape"],
                "render_as": stage["render_as"],
                "geometry": stage["node_geometry"],
                "anchor_points": stage["anchor_points"],
                "label_slot": stage["slots"].get("title"),
                "caption_slot": stage["slots"].get("description"),
                "icon_slot": stage["slots"].get("icon"),
            }
        )

    stage_by_id = {stage.get("id", index): stage for index, stage in enumerate(stages)}
    reserved_zones = _reserved_content_zones(stages)
    label_obstacles: list[tuple[float, float, float, float]] = []
    arrows: list[dict[str, Any]] = []
    for connection in connections:
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        start = source["anchor_points"]["right"]
        end = target["anchor_points"]["left"]
        arrows.append(
            _route_connection_payload(
                connection,
                source,
                target,
                start=start,
                end=end,
                reserved_zones=reserved_zones,
                width=width,
                height=height,
                label_obstacles=label_obstacles,
                source_port="right",
                target_port="left",
                hint_y=[start.get("y", 0)],
            )
        )
    first = nodes[0] if nodes else {"cx": path_margin, "cy": path_y}
    last = nodes[-1] if nodes else {"cx": width - path_margin, "cy": path_y}
    return {
        "template_id": "flow-path",
        "template_slots": {
            "path": {
                "orientation": "horizontal",
                "x1": first["cx"],
                "y1": first["cy"],
                "x2": last["cx"],
                "y2": last["cy"],
                "points": [{"stage_id": node["stage_id"], "x": node["cx"], "y": node["cy"]} for node in nodes],
            },
            "nodes": nodes,
            "arrows": arrows,
            "connection_paths": arrows,
            "reserved_content_zones": reserved_zones,
            "quality_targets": _quality_targets(width, height),
        },
        "render_contract": {
            "required_groups": ["stage-node", "semantic-connection"],
            "required_template_marks": ["flow-path"],
            "allowed_stage_shapes": ["circle-icon", "decision-diamond", "terminator-pill"],
            "forbid_uniform_large_cards": True,
            "safe_area_is_not_background": True,
        },
    }


def _timeline_card_slots(
    card: dict[str, int],
    icon_size: int,
    stage: dict[str, Any] | None = None,
) -> dict[str, dict[str, int]]:
    """为 timeline 事件卡片划分槽位；有 visuals 时预留上半部 scene 区域。"""
    has_visuals = bool(stage and stage.get("visuals")) if stage else False
    padding = 12
    inner_x = card["x"] + padding
    inner_w = max(60, card["w"] - padding * 2)

    if has_visuals:
        # 卡片上半是 scene 图形区，下半是紧凑的文字区
        title_h = 24
        metric_h = 24
        desc_h = 36
        top_pad = 10
        scene_gap = 8
        title_gap = 4
        metric_gap = 6
        reserved_text_h = title_h + title_gap + desc_h + metric_gap + metric_h
        max_scene_h = max(64, card["h"] - top_pad - scene_gap - reserved_text_h - padding)
        scene_h = int(max(64, min(104, max_scene_h, card["h"] * 0.45)))
        scene = _slot(inner_x, card["y"] + top_pad, inner_w, scene_h)
        text_top = scene["y"] + scene["h"] + scene_gap
        title = _slot(inner_x, text_top, inner_w, title_h)
        desc_y = title["y"] + title["h"] + title_gap
        metric_y = card["y"] + card["h"] - padding - metric_h
        desc_bottom = min(metric_y - metric_gap, desc_y + desc_h)
        return {
            "scene": scene,
            "title": title,
            "description": _slot(inner_x, desc_y, inner_w, max(12, desc_bottom - desc_y)),
            "metric": _slot(inner_x, metric_y, inner_w, metric_h),
        }

    # 无 visuals：传统 icon + 文字布局
    icon = _slot(card["x"] + 12, card["y"] + 14, icon_size, icon_size)
    title = _slot(
        card["x"] + icon_size + 22,
        card["y"] + 14,
        max(60, card["w"] - icon_size - 36),
        30,
    )
    metric = _slot(card["x"] + 14, card["y"] + card["h"] - 38, card["w"] - 28, 30)
    description_y = max(card["y"] + 56, icon["y"] + icon["h"] + 6)
    description_bottom = metric["y"] - 8
    description_h = max(12, description_bottom - description_y)
    return {
        "icon": icon,
        "title": title,
        "description": _slot(card["x"] + 14, description_y, card["w"] - 28, description_h),
        "metric": metric,
    }


def _timeline_icon_size(card: dict[str, int]) -> int:
    """Use the largest icon that still leaves a non-overlapping text band."""
    max_without_overlap = max(32, card["h"] - 86)
    return int(max(32, min(64, card["w"] * 0.30, card["h"] * 0.48, max_without_overlap)))


def _direct_timeline_layout(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any]:
    axis_y = content_y + content_h // 2
    # 先以常规内缩估算卡片尺寸，再为首尾卡片预留半张卡片的安全边距。
    # 否则首尾 marker 虽在画布内，居中放置的 event_card 会伸到画布外。
    provisional_x1 = margin + 88
    provisional_x2 = width - margin - 88
    spacing = (provisional_x2 - provisional_x1) / max(1, len(stages) - 1) if len(stages) > 1 else 220
    card_w = int(max(176, min(230, spacing * 0.78)))
    card_edge_gap = 12
    axis_inset = max(88, card_w // 2 + card_edge_gap)
    max_axis_inset = max(1, (width - margin * 2 - 1) // 2)
    axis_inset = min(axis_inset, max_axis_inset)
    axis_x1 = margin + axis_inset
    axis_x2 = width - margin - axis_inset
    centers = _even_centers(len(stages), x1=axis_x1, x2=axis_x2, y=axis_y)
    has_visual_cards = any(bool(stage.get("visuals")) for stage in stages)
    if has_visual_cards:
        card_h = int(max(188, min(220, content_h * 0.35)))
    else:
        card_h = int(max(122, min(156, content_h * 0.30)))
    milestones: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        cx = centers[index]["x"]
        cy = axis_y
        label_top = index % 2 == 0
        if label_top:
            card_y = max(content_y + 16, axis_y - card_h - 42)
        else:
            card_y = min(content_y + content_h - card_h - 16, axis_y + 42)
        card = _slot(cx - card_w / 2, card_y, card_w, card_h)
        render_as, node_shape = _block_render(stage, "timeline")
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=28,
            shape_h=28,
            render_as=render_as,
            node_shape=node_shape,
        )
        icon_size = _timeline_icon_size(card)
        card_slots = _timeline_card_slots(card, icon_size, stage=stage)
        stage["short_description"] = _short_text(stage.get("description", ""), 18)
        stage.update(
            {
                "x": card["x"],
                "y": card["y"],
                "w": card["w"],
                "h": card["h"],
                "safe_area": dict(card),
                "timeline_card": dict(card),
            }
        )
        stage["slots"] = {}
        if "scene" in card_slots:
            stage["slots"]["scene"] = card_slots["scene"]
        else:
            stage["slots"]["icon"] = card_slots["icon"]
        stage["slots"]["title"] = card_slots["title"]
        stage["slots"]["metric"] = card_slots["metric"]
        if stage["short_description"]:
            stage["slots"]["description"] = card_slots["description"]
        milestones.append(
            {
                "stage_id": stage.get("id", index),
                "x": cx,
                "y": cy,
                "safe_area": card,
                "card_slot": card,
                "event_card_slot": card,
                "marker_slot": _slot(cx - 14, cy - 14, 28, 28),
                "icon_slot": stage["slots"].get("icon"),
                "scene_slot": stage["slots"].get("scene"),
                "node_shape": node_shape,
                "render_as": render_as,
                "label_position": "top" if label_top else "bottom",
                "label_slot": stage["slots"].get("title"),
                "caption_slot": stage["slots"].get("description"),
                "metric_slot": stage["slots"].get("metric"),
            }
        )
    return {
        "template_id": "timeline-axis",
        "template_slots": {
            "axis": {"x1": axis_x1, "y1": axis_y, "x2": axis_x2, "y2": axis_y},
            "milestones": milestones,
            "arrows": [
                {"from": item.get("from"), "to": item.get("to"), "type": item.get("type", "arrow")}
                for item in connections
            ],
            "reserved_content_zones": _reserved_content_zones(stages),
            "quality_targets": _quality_targets(width, height),
        },
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": ["timeline-axis"],
            "allowed_stage_shapes": ["milestone-dot"],
            "forbid_uniform_large_cards": True,
            "safe_area_is_not_background": True,
        },
        "minimum_canvas_height": content_y + card_h * 2 + 116 + margin if has_visual_cards else height,
    }


def _direct_compare_layout(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any]:
    values: list[float] = []
    for stage in stages:
        metric = stage.get("metric") if isinstance(stage.get("metric"), dict) else {}
        try:
            values.append(float(metric.get("value", 0)))
        except (TypeError, ValueError):
            values.append(0.0)
    scale_max = max(values) if any(value > 0 for value in values) else max(1.0, float(len(stages) or 1))
    count = max(1, len(stages))
    usable_bar_h = max(160, min(360, content_h - 220))
    if float(scale_max).is_integer() and 2 <= scale_max <= 80:
        scale_units = int(scale_max)
        usable_bar_h = max(160, (usable_bar_h // scale_units) * scale_units)
    available_w = max(1, width - margin * 2 - 168)
    compact_w = max(360, min(available_w, count * 190))
    chart_x = margin + 84 + max(0, (available_w - compact_w) / 2)
    chart = {
        "type": "bar" if any(value > 0 for value in values) else "comparison-table",
        "x": chart_x,
        "y": content_y + 42,
        "w": compact_w,
        "h": usable_bar_h + 42,
    }
    chart["baseline"] = chart["y"] + chart["h"]
    chart["scale_max"] = scale_max
    band_w = chart["w"] / count
    safe_w = int(max(106, min(190, band_w * 0.68)))
    items: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        value = values[index] if index < len(values) else 0.0
        cx = int(chart["x"] + band_w * index + band_w / 2)
        bar_h = int(round((value / scale_max) * (chart["h"] - 42))) if scale_max > 0 else 0
        bar_w = int(max(28, min(86, band_w * 0.42)))
        bar_slot = _slot(cx - bar_w / 2, chart["baseline"] - bar_h, bar_w, max(2, bar_h))
        safe = _set_safe_area(
            stage,
            cx=cx,
            cy=chart["y"] + chart["h"] // 2 + 34,
            safe_w=safe_w,
            safe_h=chart["h"] + 112,
            width=width,
            height=height,
        )
        render_as, node_shape = _block_render(stage, "compare")
        _apply_stage_template(
            stage,
            cx=cx,
            cy=chart["baseline"] - max(8, bar_h // 2),
            shape_w=bar_w,
            shape_h=max(20, bar_h),
            render_as=render_as,
            node_shape=node_shape,
        )
        stage["bar_slot"] = bar_slot
        stage["slots"] = {
            "metric": _slot(cx - safe_w * 0.36, max(chart["y"], bar_slot["y"] - 34), safe_w * 0.72, 28),
            "icon": _slot(cx - 22, chart["baseline"] + 10, 44, 44),
            "title": _slot(cx - safe_w * 0.48, chart["baseline"] + 58, safe_w * 0.96, 30),
        }
        if stage.get("description"):
            stage["short_description"] = _short_text(stage.get("description", ""), 14)
            stage["slots"]["description"] = _slot(cx - safe_w * 0.48, chart["baseline"] + 90, safe_w * 0.96, 38)
        items.append(
            {
                "stage_id": stage.get("id", index),
                "value": value,
                "label": (stage.get("metric") or {}).get("label", ""),
                "safe_area": safe,
                "bar_slot": bar_slot,
                "icon_slot": stage["slots"]["icon"],
                "title_slot": stage["slots"]["title"],
                "metric_slot": stage["slots"]["metric"],
            }
        )
    item_by_id = {item["stage_id"]: item for item in items}
    relationship_labels: list[dict[str, Any]] = []
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict) or not connection.get("label"):
            continue
        source = item_by_id.get(connection.get("from"))
        target = item_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        source_bar = source["bar_slot"]
        target_bar = target["bar_slot"]
        sx = source_bar["x"] + source_bar["w"] / 2
        tx = target_bar["x"] + target_bar["w"] / 2
        slot_w = 76
        lower_top_y = max(source_bar["y"], target_bar["y"])
        bar_gap = abs(tx - sx)
        arc_lift = min(148, max(76, bar_gap * 0.58))
        arc_top_y = max(chart["y"] + 44, lower_top_y - arc_lift)
        slot_y = _clamp(
            arc_top_y - 34 - index * 8,
            chart["y"] + 8,
            chart["baseline"] - 124,
        )
        slot = _slot(
            max(chart["x"], min((sx + tx) / 2 - slot_w / 2, chart["x"] + chart["w"] - slot_w)),
            slot_y,
            slot_w,
            26,
        )
        connection["label_slot"] = slot
        relationship_labels.append(
            {
                "from": connection.get("from"),
                "to": connection.get("to"),
                "type": connection.get("type", "arrow"),
                "label": connection.get("label", ""),
                "label_slot": slot,
            }
        )
    return {
        "template_id": "comparison-chart",
        "template_slots": {
            "chart": chart,
            "items": items,
            "relationship_labels": relationship_labels,
            "connection_paths": relationship_labels,
            "reserved_content_zones": _reserved_content_zones(stages),
            "quality_targets": _quality_targets(width, height),
            "baseline": {"x1": chart["x"], "y1": chart["baseline"], "x2": chart["x"] + chart["w"], "y2": chart["baseline"]},
        },
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": ["comparison-chart"],
            "bar_role": "comparison-bar",
            "forbid_uniform_large_cards": True,
            "safe_area_is_not_background": True,
        },
    }


def _direct_flow_mechanism_layout(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any]:
    path_y = content_y + int(content_h * 0.44)
    path_margin = max(100, margin * 5)
    centers = _even_centers(len(stages), x1=path_margin, x2=width - path_margin, y=path_y)
    nodes: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        cx = centers[index]["x"]
        cy = centers[index]["y"]
        render_as, node_shape = _block_render(stage, "mechanism")
        shape_w, shape_h = 118, 74
        if render_as in {"attention_matrix", "probability_bars"}:
            shape_w, shape_h = 142, 96
        elif render_as == "token_strip":
            shape_w, shape_h = 148, 58
        elif node_shape == "output-badge":
            shape_w, shape_h = 112, 68
        safe = _set_safe_area(
            stage,
            cx=cx,
            cy=cy + 30,
            safe_w=190 if len(stages) <= 6 else 160,
            safe_h=230,
            width=width,
            height=height,
        )
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=shape_w,
            shape_h=shape_h,
            render_as=render_as,
            node_shape=node_shape,
        )
        stage["short_description"] = _short_text(stage.get("description", ""), 12)
        stage["slots"] = {
            "icon": _slot(cx - shape_w / 2, cy - shape_h / 2, shape_w, shape_h),
            "title": _slot(cx - 84, cy + shape_h / 2 + 18, 168, 30),
        }
        if stage["short_description"]:
            stage["slots"]["description"] = _slot(cx - 90, cy + shape_h / 2 + 52, 180, 42)
        if stage.get("metric"):
            stage["slots"]["metric"] = _slot(cx - 60, safe["y"] + safe["h"] - 36, 120, 24)
        nodes.append(
            {
                "stage_id": stage.get("id", index),
                "cx": cx,
                "cy": cy,
                "safe_area": safe,
                "render_as": stage["render_as"],
                "node_shape": stage["node_shape"],
                "geometry": stage["node_geometry"],
                "anchor_points": stage["anchor_points"],
            }
        )

    stage_by_id = {stage.get("id", index): stage for index, stage in enumerate(stages)}
    reserved_zones = _reserved_content_zones(stages)
    label_obstacles: list[tuple[float, float, float, float]] = []
    arrows: list[dict[str, Any]] = []
    feedback_arrows: list[dict[str, Any]] = []
    for connection in connections:
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        if connection.get("type") == "feedback_arrow":
            start = source["anchor_points"]["bottom"]
            end = target["anchor_points"]["bottom"]
            payload = _route_connection_payload(
                connection,
                source,
                target,
                start=start,
                end=end,
                reserved_zones=reserved_zones,
                width=width,
                height=height,
                label_obstacles=label_obstacles,
                source_port="bottom",
                target_port="bottom",
                hint_y=[path_y + 116],
            )
            feedback_arrows.append(payload)
        else:
            start = source["anchor_points"]["right"]
            end = target["anchor_points"]["left"]
            payload = _route_connection_payload(
                connection,
                source,
                target,
                start=start,
                end=end,
                reserved_zones=reserved_zones,
                width=width,
                height=height,
                label_obstacles=label_obstacles,
                source_port="right",
                target_port="left",
                hint_y=[start.get("y", 0)],
            )
            arrows.append(payload)
    first = nodes[0] if nodes else {"cx": path_margin, "cy": path_y}
    last = nodes[-1] if nodes else {"cx": width - path_margin, "cy": path_y}
    return {
        "template_id": "flow-mechanism",
        "template_slots": {
            "main_path": {
                "x1": first["cx"],
                "y1": first["cy"],
                "x2": last["cx"],
                "y2": last["cy"],
                "points": [{"stage_id": node["stage_id"], "x": node["cx"], "y": node["cy"]} for node in nodes],
            },
            "nodes": nodes,
            "arrows": arrows,
            "feedback_arrows": feedback_arrows,
            "connection_paths": arrows + feedback_arrows,
            "reserved_content_zones": reserved_zones,
            "feedback_lane": _slot(path_margin, path_y + 96, width - path_margin * 2, 88),
            "quality_targets": _quality_targets(width, height),
        },
        "render_contract": {
            "required_groups": ["stage-node", "semantic-connection"],
            "required_template_marks": ["flow-mechanism"],
            "forbid_uniform_large_cards": True,
            "safe_area_is_not_background": True,
        },
    }


def _visual_brief(stage: dict[str, Any]) -> str:
    """返回自由文本视觉意图，兼容旧字段但不恢复任何固定场景模板。"""
    explicit = str(stage.get("visual_brief", "")).strip()
    if explicit:
        return explicit
    legacy = str(stage.get("scene_type", "")).strip()
    if legacy:
        return legacy
    parts = [str(stage.get(key, "")).strip() for key in ("title", "description")]
    for visual in stage.get("visuals", []):
        if isinstance(visual, dict):
            parts.append(str(visual.get("label") or visual.get("icon_query") or "").strip())
    return "；".join(part for part in parts if part)


def _has_brand_visual(stage: dict[str, Any]) -> bool:
    """品牌标识保留独立槽位；通用图形统一由场景本身承载。"""
    return any(
        isinstance(visual, dict) and str(visual.get("kind", "")).strip().lower() == "brand"
        for visual in stage.get("visuals", [])
    )


def _panel_connection_points(source: dict[str, Any], target: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    source_anchors = source.get("anchor_points", {})
    target_anchors = target.get("anchor_points", {})
    sx, sy, sw, sh = (int(source.get(key, 0)) for key in ("x", "y", "w", "h"))
    tx, ty, tw, th = (int(target.get(key, 0)) for key in ("x", "y", "w", "h"))
    if sx + sw <= tx:
        return source_anchors.get("right", {}), target_anchors.get("left", {})
    if tx + tw <= sx:
        return source_anchors.get("left", {}), target_anchors.get("right", {})
    if sy + sh <= ty:
        return source_anchors.get("bottom", {}), target_anchors.get("top", {})
    if ty + th <= sy:
        return source_anchors.get("top", {}), target_anchors.get("bottom", {})
    return source_anchors.get("center", {}), target_anchors.get("center", {})


def _panel_connection_ports(source: dict[str, Any], target: dict[str, Any]) -> tuple[str, str]:
    sx, sy, sw, sh = (int(source.get(key, 0)) for key in ("x", "y", "w", "h"))
    tx, ty, tw, th = (int(target.get(key, 0)) for key in ("x", "y", "w", "h"))
    if sx + sw <= tx:
        return "right", "left"
    if tx + tw <= sx:
        return "left", "right"
    if sy + sh <= ty:
        return "bottom", "top"
    if ty + th <= sy:
        return "top", "bottom"
    return "right", "left"


def _storyboard_stage_weight(stage: dict[str, Any]) -> float:
    title_len = len(str(stage.get("title", "") or ""))
    desc_len = len(str(stage.get("description", "") or ""))
    text_count = len([item for item in stage.get("texts", []) if str(item).strip()])
    weight = 1.0 + min(0.18, title_len / 80.0) + min(0.14, desc_len / 160.0)
    if text_count >= 3:
        weight += 0.06
    if stage.get("visuals"):
        weight += 0.04
    return max(0.88, min(1.24, weight))


def _storyboard_max_cols(outer_w: int, gap: int) -> int:
    min_panel_w = 220
    return max(1, min(5, int((outer_w + gap) // (min_panel_w + gap))))


def _balanced_chunks(indices: list[int], max_cols: int) -> list[list[int]]:
    if not indices:
        return []
    max_cols = max(1, max_cols)
    row_count = max(1, (len(indices) + max_cols - 1) // max_cols)
    base = len(indices) // row_count
    remainder = len(indices) % row_count
    chunks: list[list[int]] = []
    cursor = 0
    for row_index in range(row_count):
        size = base + (1 if row_index < remainder else 0)
        chunks.append(indices[cursor:cursor + size])
        cursor += size
    return chunks


def _storyboard_rows(
    stages: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    outer_w: int,
    gap: int,
) -> list[dict[str, Any]]:
    max_cols = _storyboard_max_cols(outer_w, gap)
    stage_section_titles = [str(stage.get("section", "main") or "main") for stage in stages]
    section_defs = [
        section
        for section in sections
        if isinstance(section, dict)
        and str(section.get("title", "main") or "main").strip().lower() not in {"", "main"}
    ]
    title_to_indices: dict[str, list[int]] = {}
    for index, title in enumerate(stage_section_titles):
        title_to_indices.setdefault(title, []).append(index)

    explicit_titles = [str(section.get("title", "main") or "main") for section in section_defs]
    uses_meaningful_sections = bool(section_defs) and (
        any(len(title_to_indices.get(title, [])) > 1 for title in explicit_titles)
        or len(section_defs) < len(stages)
    )

    rows: list[dict[str, Any]] = []
    if uses_meaningful_sections:
        consumed: set[int] = set()
        for section_index, section in enumerate(section_defs):
            title = str(section.get("title", "main") or "main")
            indices = title_to_indices.get(title, [])
            if not indices:
                continue
            chunks = _balanced_chunks(indices, max_cols)
            for subrow_index, chunk in enumerate(chunks):
                rows.append(
                    {
                        "section_id": section.get("id", f"section_{section_index}"),
                        "section_title": title,
                        "subrow_index": subrow_index,
                        "subrow_count": len(chunks),
                        "indices": chunk,
                    }
                )
            consumed.update(indices)
        remaining = [index for index in range(len(stages)) if index not in consumed]
        for chunk in _balanced_chunks(remaining, max_cols):
            rows.append(
                {
                    "section_id": "storyboard_extra",
                    "section_title": "main",
                    "subrow_index": 0,
                    "subrow_count": 1,
                    "indices": chunk,
                }
            )
    else:
        chunks = _balanced_chunks(list(range(len(stages))), max(1, min(4, max_cols)))
        for row_index, chunk in enumerate(chunks):
            rows.append(
                {
                    "section_id": f"storyboard_row_{row_index}",
                    "section_title": "main",
                    "subrow_index": row_index,
                    "subrow_count": len(chunks),
                    "indices": chunk,
                }
            )

    return [row for row in rows if row.get("indices")]


def _weighted_row_slots(
    row: dict[str, Any],
    stages: list[dict[str, Any]],
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    gap: int,
    gaps: list[int] | None = None,
) -> list[dict[str, int]]:
    indices = row.get("indices", [])
    count = max(1, len(indices))
    row_gaps = list(gaps or [])
    if len(row_gaps) != max(0, count - 1):
        row_gaps = [gap] * max(0, count - 1)
    usable_w = max(1, w - sum(row_gaps))
    if count == 1:
        panel_w = min(usable_w, max(usable_w * 0.56, 360))
        return [_slot(x + (usable_w - panel_w) / 2, y, panel_w, h)]

    weights = [_storyboard_stage_weight(stages[index]) for index in indices]
    total_weight = max(0.01, sum(weights))
    avg_w = usable_w / count
    widths = [max(avg_w * 0.76, usable_w * weight / total_weight) for weight in weights]
    scale = usable_w / max(1.0, sum(widths))
    widths = [width * scale for width in widths]

    slots: list[dict[str, int]] = []
    cursor_x = x
    for index, width_value in enumerate(widths):
        if index == len(widths) - 1:
            width_value = max(1, x + w - cursor_x)
        slots.append(_slot(cursor_x, y, width_value, h))
        if index < len(row_gaps):
            cursor_x += width_value + row_gaps[index]
    return slots


def _storyboard_connection_path(
    source: dict[str, Any],
    target: dict[str, Any],
    start: dict[str, int],
    end: dict[str, int],
    connection_type: str,
) -> dict[str, Any]:
    sx, sy = int(start.get("x", 0)), int(start.get("y", 0))
    tx, ty = int(end.get("x", 0)), int(end.get("y", 0))
    same_row = source.get("storyboard_row") == target.get("storyboard_row")
    if connection_type == "feedback_arrow":
        lift = max(54, abs(sy - ty) // 2 + 36)
        return {
            "kind": "curve",
            "points": [
                {"x": sx, "y": sy},
                {"x": sx, "y": sy + lift},
                {"x": tx, "y": ty + lift},
                {"x": tx, "y": ty},
            ],
            "dash": "8 6",
        }
    if same_row:
        return {"kind": "line", "points": [{"x": sx, "y": sy}, {"x": tx, "y": ty}], "dash": ""}
    mid_y = sy + (ty - sy) // 2
    return {
        "kind": "elbow",
        "points": [
            {"x": sx, "y": sy},
            {"x": sx, "y": mid_y},
            {"x": tx, "y": mid_y},
            {"x": tx, "y": ty},
        ],
        "dash": "",
    }


_LABEL_LANE_GAP = 12
_LABEL_LANE_EDGE = 8


def _connection_key(connection: dict[str, Any]) -> tuple[Any, Any, str]:
    return (
        connection.get("from"),
        connection.get("to"),
        str(connection.get("type", "arrow")),
    )


def _storyboard_label_lane_plan(
    rows: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    base_gap: int,
) -> tuple[dict[tuple[Any, Any, str], dict[str, Any]], dict[tuple[int, int], int], list[int], int]:
    """在绘制 panel 前，为每条带标签的分镜连接预留专用通道。"""
    position_by_stage_id: dict[Any, tuple[int, int]] = {}
    for row_index, row in enumerate(rows):
        for position, stage_index in enumerate(row.get("indices", [])):
            stage_id = stages[stage_index].get("id", stage_index)
            position_by_stage_id[stage_id] = (row_index, position)

    plans: dict[tuple[Any, Any, str], dict[str, Any]] = {}
    horizontal_gaps: dict[tuple[int, int], int] = {}
    row_bands: dict[int, list[tuple[Any, Any, str]]] = {}
    top_bands: list[tuple[Any, Any, str]] = []

    for connection in connections:
        if not isinstance(connection, dict) or not str(connection.get("label", "")).strip():
            continue
        key = _connection_key(connection)
        source_position = position_by_stage_id.get(connection.get("from"))
        target_position = position_by_stage_id.get(connection.get("to"))
        if source_position is None or target_position is None:
            continue
        source_row, source_column = source_position
        target_row, target_column = target_position
        label_w, label_h = label_size(str(connection.get("label", "")))
        plan: dict[str, Any] = {
            "label_w": int(round(label_w)),
            "label_h": int(round(label_h)),
            "source_row": source_row,
            "target_row": target_row,
            "source_column": source_column,
            "target_column": target_column,
        }

        if connection.get("type") != "feedback_arrow" and source_row == target_row:
            boundary = min(source_column, target_column)
            plan.update({"kind": "horizontal-gap", "row_index": source_row, "boundary": boundary})
            horizontal_gaps[(source_row, boundary)] = max(
                horizontal_gaps.get((source_row, boundary), base_gap),
                int(round(label_w)) + _LABEL_LANE_GAP * 2,
            )
        else:
            # 同行反馈优先使用该行上方的行间通道；第一行则在标题与 panel 之间扩展顶端通道。
            if source_row == target_row and source_row == 0:
                plan.update({"kind": "top-feedback", "band_index": len(top_bands)})
                top_bands.append(key)
            else:
                lane_index = max(0, min(source_row, target_row if source_row != target_row else source_row - 1))
                plan.update({"kind": "row-gap", "row_gap_index": lane_index, "band_index": len(row_bands.get(lane_index, []))})
                row_bands.setdefault(lane_index, []).append(key)
        plans[key] = plan

    row_gaps = [base_gap] * max(0, len(rows) - 1)
    for row_gap_index, keys in row_bands.items():
        if 0 <= row_gap_index < len(row_gaps):
            band_height = sum(plans[key]["label_h"] + _LABEL_LANE_GAP * 2 for key in keys)
            row_gaps[row_gap_index] = max(row_gaps[row_gap_index], band_height + _LABEL_LANE_EDGE * 2)
    top_height = 0
    if top_bands:
        top_height = sum(plans[key]["label_h"] + _LABEL_LANE_GAP * 2 for key in top_bands) + _LABEL_LANE_EDGE * 2
    return plans, horizontal_gaps, row_gaps, top_height


def _lane_path(
    start: dict[str, int],
    end: dict[str, int],
    *,
    lane_y: int,
    connection_type: str,
) -> dict[str, Any]:
    """让跨行/反馈连接沿已分配的标签通道水平通过。"""
    points = [
        {"x": int(start.get("x", 0)), "y": int(start.get("y", 0))},
        {"x": int(start.get("x", 0)), "y": lane_y},
        {"x": int(end.get("x", 0)), "y": lane_y},
        {"x": int(end.get("x", 0)), "y": int(end.get("y", 0))},
    ]
    compact_points = [points[0]]
    for point in points[1:]:
        if point != compact_points[-1]:
            compact_points.append(point)
    return {
        "kind": "curve" if connection_type == "feedback_arrow" else "elbow",
        "points": compact_points,
        "dash": "8 6" if connection_type == "feedback_arrow" else ("6 5" if connection_type == "dashed_arrow" else ""),
    }


# 分镜面板的目标高度参数。
# 场景图是面板主体，高度与面板宽度成比例 —— 太扁画不出层次，太高则浪费。
_STORYBOARD_SCENE_ASPECT = 0.62
_STORYBOARD_SCENE_MIN_H = 170
_STORYBOARD_SCENE_MAX_H = 320


def _storyboard_panel_target_height(panel_w: float, has_description: bool) -> float:
    """估算一个分镜面板的合理高度：贴合内容，而不是撑满可用空间。

    面板的纵向结构是「上留白 + 标题 + 间隙 + 场景图 + 间隙 + 说明 + 下留白」，
    其中场景图是主体。按内容撑高之后，卡片内部就不会留下大片空白 ——
    多余的空白留在画布层面（整组面板居中），而不是塞进每个面板里。

    旧做法是直接按权重均分可用高度，画布一大面板就被撑大，
    而实际绘制的图形只占一半，于是卡片里出现明显留白。
    """
    pad = max(14.0, min(22.0, panel_w / 18))
    title_h = max(34.0, min(48.0, panel_w / 5))
    caption_h = 38.0 if has_description else 28.0
    scene_h = _clamp(
        panel_w * _STORYBOARD_SCENE_ASPECT,
        _STORYBOARD_SCENE_MIN_H,
        _STORYBOARD_SCENE_MAX_H,
    )
    return pad + title_h + 10 + scene_h + 10 + caption_h + 12


def _direct_mechanism_storyboard_layout(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
    sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """教学分镜模板：大 panel 承载阶段，scene_area 承载阶段内小图画。"""
    count = len(stages)
    outer_x = margin
    outer_w = max(1, width - margin * 2)
    gap = max(30, min(46, int(min(width, height) * 0.045), margin * 2))
    rows = _storyboard_rows(stages, sections or [], outer_w=outer_w, gap=gap)
    if not rows and count:
        rows = [{"section_id": "storyboard_row_0", "section_title": "main", "subrow_index": 0, "subrow_count": 1, "indices": list(range(count))}]

    label_plans, horizontal_gap_requirements, row_gaps, top_label_height = _storyboard_label_lane_plan(
        rows,
        stages,
        connections,
        base_gap=gap,
    )
    # 第一行反馈的标签需要独立通道，不能挤进 header 或让路由器放到画布顶部。
    outer_y = content_y + top_label_height
    outer_h = max(1, content_h - top_label_height)

    row_count = max(1, len(rows))
    usable_h = max(1, outer_h - sum(row_gaps))

    # 先按内容估算每行需要多高，而不是直接把可用空间均分。
    # 均分会让面板被画布撑大，而实际绘制的图形只占一半，卡片内部就出现大片空白。
    row_target_h: list[float] = []
    for row_index, row in enumerate(rows):
        column_count = max(1, len(row.get("indices", [])))
        gaps_total = sum(
            horizontal_gap_requirements.get((row_index, boundary), gap)
            for boundary in range(max(0, column_count - 1))
        )
        panel_w = max(160.0, (outer_w - gaps_total) / column_count)
        has_description = any(
            stages[index].get("description")
            for index in row.get("indices", [])
            if 0 <= index < len(stages)
        )
        row_target_h.append(
            _storyboard_panel_target_height(panel_w, bool(has_description))
        )

    # 标签通道不能以牺牲面板可读性为代价：画布最小高度按「内容实际需要的高度」算，
    # 不够时由 LayoutEngine 放大画布后重算。
    minimum_canvas_height = (
        content_y + top_label_height + sum(row_gaps) + sum(row_target_h) + margin
    )
    minimum_canvas_width = width
    for row_index, row in enumerate(rows):
        column_count = len(row.get("indices", []))
        if column_count <= 1:
            continue
        required_inner_width = column_count * 300 + sum(
            horizontal_gap_requirements.get((row_index, boundary), gap)
            for boundary in range(column_count - 1)
        )
        minimum_canvas_width = max(minimum_canvas_width, required_inner_width + margin * 2)

    row_weights = [1.0 + min(0.16, max(0, len(row.get("indices", [])) - 3) * 0.04) for row in rows]
    total_row_weight = max(0.01, sum(row_weights))

    desired_total_h = sum(row_target_h) + sum(row_gaps)
    if desired_total_h <= outer_h:
        # 内容比可用空间小：按目标高度排布，整组垂直居中。
        # 空白留在画布上（上下均匀），而不是塞进每个面板里。
        row_heights = list(row_target_h)
        cursor_y = outer_y + (outer_h - desired_total_h) / 2
    else:
        # 画布还没放大到够高（LayoutEngine 下一轮会重算），先按权重均分并把末行补齐
        row_heights = [
            usable_h * row_weights[index] / total_row_weight
            for index in range(row_count)
        ]
        cursor = outer_y
        for index in range(row_count):
            if index == row_count - 1:
                row_heights[index] = max(1.0, outer_y + outer_h - cursor)
            cursor += row_heights[index] + (
                row_gaps[index] if index < len(row_gaps) else 0
            )
        cursor_y = outer_y

    panel_rects: dict[int, dict[str, int]] = {}
    computed_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        row_h = row_heights[row_index]
        row_slots = _weighted_row_slots(
            row,
            stages,
            x=outer_x,
            y=int(round(cursor_y)),
            w=outer_w,
            h=int(round(row_h)),
            gap=gap,
            gaps=[
                horizontal_gap_requirements.get((row_index, boundary), gap)
                for boundary in range(max(0, len(row.get("indices", [])) - 1))
            ],
        )
        indices = row.get("indices", [])
        for stage_index, rect in zip(indices, row_slots):
            panel_rects[int(stage_index)] = rect
        computed_rows.append(
            {
                "row_index": row_index,
                "section_id": row.get("section_id"),
                "section_title": row.get("section_title"),
                "subrow_index": row.get("subrow_index", 0),
                "subrow_count": row.get("subrow_count", 1),
                "stage_ids": [stages[index].get("id", index) for index in indices],
                "columns": len(indices),
                "rect": _slot(outer_x, cursor_y, outer_w, row_h),
                "gap_after": row_gaps[row_index] if row_index < len(row_gaps) else 0,
            }
        )
        cursor_y += row_h + (row_gaps[row_index] if row_index < len(row_gaps) else 0)

    panels: list[dict[str, Any]] = []
    storyboard_sections: list[dict[str, Any]] = []
    reserved_content_zones: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        rect = panel_rects.get(index, _slot(outer_x, outer_y, outer_w, outer_h))
        # 与 _storyboard_panel_target_height 保持一致：标题高度按面板宽度算。
        # 面板高度现在是估算出来的，再用它反推标题长度会自相矛盾。
        title_h = max(34, min(48, rect["w"] // 5))
        caption_h = 38 if stage.get("description") else 28
        pad = max(14, min(22, rect["w"] // 18, rect["h"] // 12))
        title_slot = _slot(rect["x"] + pad, rect["y"] + 14, rect["w"] - pad * 2, title_h)
        caption_slot = _slot(rect["x"] + pad, rect["y"] + rect["h"] - caption_h - 12, rect["w"] - pad * 2, caption_h)
        scene_y = title_slot["y"] + title_slot["h"] + 10
        content_h_available = max(1, caption_slot["y"] - scene_y - 10)
        content_area = _slot(rect["x"] + pad, scene_y, rect["w"] - pad * 2, content_h_available)
        icon_slot: dict[str, int] | None = None
        if _has_brand_visual(stage):
            icon_size = int(max(54, min(86, rect["w"] * 0.22, content_area["h"] * 0.52)))
            icon_gap = max(12, min(18, rect["w"] // 24))
            if content_area["w"] - icon_size - icon_gap >= 120:
                icon_slot = _slot(
                    content_area["x"],
                    content_area["y"] + max(0, (content_area["h"] - icon_size) / 2),
                    icon_size,
                    icon_size,
                )
                scene_area = _slot(
                    content_area["x"] + icon_size + icon_gap,
                    content_area["y"],
                    content_area["w"] - icon_size - icon_gap,
                    content_area["h"],
                )
            else:
                icon_slot = _slot(
                    content_area["x"] + max(0, (content_area["w"] - icon_size) / 2),
                    content_area["y"],
                    icon_size,
                    icon_size,
                )
                scene_area = _slot(
                    content_area["x"],
                    content_area["y"] + icon_size + icon_gap,
                    content_area["w"],
                    max(1, content_area["h"] - icon_size - icon_gap),
                )
        else:
            # 通用视觉元素是场景的组成部分，不能再挤占一个独立图标角标。
            scene_area = dict(content_area)
        # 限制 scene 最大宽度，避免单面板行产生过宽但无法填充的场景区
        max_scene_w = max(300, int(scene_area["h"] * 3.5))
        if scene_area["w"] > max_scene_w:
            offset = (scene_area["w"] - max_scene_w) // 2
            scene_area["x"] += offset
            scene_area["w"] = max_scene_w
        visual_brief = _visual_brief(stage)
        cx = rect["x"] + rect["w"] // 2
        cy = rect["y"] + rect["h"] // 2
        row_index = next((row["row_index"] for row in computed_rows if stage.get("id", index) in row.get("stage_ids", [])), 0)
        stage_payload: dict[str, Any] = {
                "x": rect["x"],
                "y": rect["y"],
                "w": rect["w"],
                "h": rect["h"],
                "safe_area": dict(content_area),
                "panel_rect": dict(rect),
                "content_area": dict(content_area),
                "scene_area": dict(scene_area),
                "visual_brief": visual_brief,
                "render_as": "storyboard_panel",
                "node_shape": "storyboard-panel",
                "storyboard_row": row_index,
                "short_description": _short_text(stage.get("description", ""), 18),
                "slots": {
                    "title": title_slot,
                    "scene": scene_area,
                    "description": caption_slot,
                },
            }
        if icon_slot is not None:
            stage_payload["icon_slot"] = dict(icon_slot)
            stage_payload["slots"]["icon"] = icon_slot
        stage.update(stage_payload)
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=rect["w"],
            shape_h=rect["h"],
            render_as="storyboard_panel",
            node_shape="storyboard-panel",
        )
        stage_id = stage.get("id", index)
        panel_payload: dict[str, Any] = {
                "stage_id": stage_id,
                "panel_rect": rect,
                "title_slot": title_slot,
                "content_area": content_area,
                "scene_area": scene_area,
                "caption_slot": caption_slot,
                "visual_brief": visual_brief,
                "anchor_points": stage["anchor_points"],
                "row_index": row_index,
            }
        if icon_slot is not None:
            panel_payload["icon_slot"] = icon_slot
        panels.append(panel_payload)
        reserved_content_zones.extend(
            [
                {"stage_id": stage_id, "role": "title", "slot": title_slot, "padding": 6},
                {"stage_id": stage_id, "role": "scene", "slot": scene_area, "padding": 8},
                {"stage_id": stage_id, "role": "description", "slot": caption_slot, "padding": 6},
            ]
        )
        if icon_slot is not None:
            reserved_content_zones.append({"stage_id": stage_id, "role": "icon", "slot": icon_slot, "padding": 8})
        storyboard_sections.append(
            {
                "id": f"storyboard_{stage_id}",
                "title": stage.get("section") or stage.get("title", f"阶段{index + 1}"),
                "stage_id": stage_id,
                "row_index": row_index,
            }
        )

    stage_by_id = {stage.get("id", index): stage for index, stage in enumerate(stages)}
    label_lanes: list[dict[str, Any]] = []
    label_lane_by_key: dict[tuple[Any, Any, str], dict[str, Any]] = {}
    row_by_index = {int(row.get("row_index", index)): row for index, row in enumerate(computed_rows)}
    for connection in connections:
        key = _connection_key(connection)
        plan = label_plans.get(key)
        if plan is None:
            continue
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        label_w = int(plan["label_w"])
        label_h = int(plan["label_h"])
        kind = str(plan["kind"])
        if kind == "horizontal-gap":
            row = row_by_index.get(int(plan["row_index"]))
            indices = row.get("stage_ids", []) if row else []
            boundary = int(plan["boundary"])
            if row is None or boundary < 0 or boundary + 1 >= len(indices):
                continue
            left = stage_by_id.get(indices[boundary])
            right = stage_by_id.get(indices[boundary + 1])
            if not left or not right:
                continue
            lane = _slot(
                left["x"] + left["w"],
                left["y"],
                max(1, right["x"] - (left["x"] + left["w"])),
                min(left["h"], right["h"]),
            )
            desired_y = source["y"] + source["h"] // 2 - label_h - _LABEL_LANE_GAP
            label_y = _clamp(
                desired_y,
                lane["y"] + _LABEL_LANE_EDGE,
                lane["y"] + lane["h"] - label_h - _LABEL_LANE_EDGE,
            )
            label_slot = _slot(lane["x"] + (lane["w"] - label_w) / 2, label_y, label_w, label_h)
            lane_y = source["y"] + source["h"] // 2
        else:
            if kind == "top-feedback":
                lane_y_start = content_y
                band_index = int(plan["band_index"])
            else:
                row = row_by_index.get(int(plan["row_gap_index"]))
                if row is None:
                    continue
                lane_y_start = row["rect"]["y"] + row["rect"]["h"]
                band_index = int(plan["band_index"])
            band_height = label_h + _LABEL_LANE_GAP * 2
            band_y = lane_y_start + _LABEL_LANE_EDGE + band_index * band_height
            lane = _slot(outer_x, band_y - _LABEL_LANE_EDGE, outer_w, band_height + _LABEL_LANE_EDGE * 2)
            source_center = source["x"] + source["w"] / 2
            target_center = target["x"] + target["w"] / 2
            label_slot = _slot(
                _clamp((source_center + target_center) / 2 - label_w / 2, outer_x + _LABEL_LANE_EDGE, outer_x + outer_w - label_w - _LABEL_LANE_EDGE),
                band_y,
                label_w,
                label_h,
            )
            lane_y = band_y + label_h + _LABEL_LANE_GAP
        lane_payload = {
            "from": connection.get("from"),
            "to": connection.get("to"),
            "type": connection.get("type", "arrow"),
            "kind": kind,
            "slot": lane,
            "label_slot": label_slot,
            "path_y": lane_y,
        }
        label_lanes.append(lane_payload)
        label_lane_by_key[key] = lane_payload

    global_connections: list[dict[str, Any]] = []
    feedback_connections: list[dict[str, Any]] = []
    connection_paths: list[dict[str, Any]] = []
    label_obstacles: list[tuple[float, float, float, float]] = []
    for connection in connections:
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        connection_type = str(connection.get("type", "arrow"))
        label_lane = label_lane_by_key.get(_connection_key(connection))
        path_override: dict[str, Any] | None = None
        if label_lane is not None and label_lane["kind"] != "horizontal-gap":
            same_row = source.get("storyboard_row") == target.get("storyboard_row")
            if same_row or int(source.get("storyboard_row", 0)) > int(target.get("storyboard_row", 0)):
                start = source["anchor_points"]["top"]
                end = target["anchor_points"]["bottom" if not same_row else "top"]
                source_port, target_port = "top", ("bottom" if not same_row else "top")
            else:
                start = source["anchor_points"]["bottom"]
                end = target["anchor_points"]["top"]
                source_port, target_port = "bottom", "top"
            path_override = _lane_path(
                start,
                end,
                lane_y=int(label_lane["path_y"]),
                connection_type=connection_type,
            )
            hint_x = None
            hint_y = [int(label_lane["path_y"])]
        elif connection_type == "feedback_arrow":
            same_row = source.get("storyboard_row") == target.get("storyboard_row")
            if same_row:
                start = source["anchor_points"]["top"]
                end = target["anchor_points"]["top"]
                source_port, target_port = "top", "top"
                lane_y = max(margin, min(start.get("y", 0), end.get("y", 0)) - 58)
            else:
                start = source["anchor_points"]["bottom"]
                end = target["anchor_points"]["bottom"]
                source_port, target_port = "bottom", "bottom"
                lane_y = min(height - margin, max(start.get("y", 0), end.get("y", 0)) + 58)
            hint_x: list[float] | None = None
            hint_y: list[float] | None = [lane_y]
        else:
            start, end = _panel_connection_points(source, target)
            source_port, target_port = _panel_connection_ports(source, target)
            same_row = source.get("storyboard_row") == target.get("storyboard_row")
            hint_x = [int((start.get("x", 0) + end.get("x", 0)) / 2)]
            hint_y = [start.get("y", 0)] if same_row else [int((start.get("y", 0) + end.get("y", 0)) / 2)]
        payload = _route_connection_payload(
            connection,
            source,
            target,
            start=start,
            end=end,
            reserved_zones=reserved_content_zones,
            width=width,
            height=height,
            label_obstacles=label_obstacles,
            source_port=source_port,
            target_port=target_port,
            hint_x=hint_x,
            hint_y=hint_y,
            label_slot=label_lane.get("label_slot") if label_lane else None,
            path_override=path_override,
        )
        connection_paths.append(payload)
        if connection.get("type") == "feedback_arrow":
            feedback_connections.append(payload)
        else:
            global_connections.append(payload)

    # 修正与箭头路径重叠的标签槽（row-gap 垂直连接容易在同一个 x 中心重叠）
    _resolve_label_path_overlaps(connection_paths, label_lanes)

    min_gap = int(round(min(width, height) * 0.03))
    return {
        "template_id": "mechanism-storyboard",
        "minimum_canvas_width": minimum_canvas_width,
        "minimum_canvas_height": minimum_canvas_height,
        "template_slots": {
            "variant": "section-aware-rows",
            "sections": storyboard_sections,
            "rows": computed_rows,
            "panels": panels,
            "label_lanes": label_lanes,
            "connection_paths": connection_paths,
            "reserved_content_zones": reserved_content_zones,
            "global_connections": global_connections,
            "feedback_connections": feedback_connections,
            "feedback_lane": _slot(outer_x + int(outer_w * 0.68), outer_y + int(outer_h * 0.55), int(outer_w * 0.28), int(outer_h * 0.32)),
            "quality_targets": {
                "occupancy_min": 0.60,
                "occupancy_max": 0.85,
                "min_module_gap": min_gap,
                "min_section_gap": int(round(min(width, height) * 0.04)),
                "arrow_length_ratio_min": 0.30,
                "arrow_length_ratio_max": 0.55,
            },
        },
        "render_contract": {
            "required_groups": ["stage-node", "storyboard-panel", "semantic-connection"],
            "required_template_marks": ["mechanism-storyboard"],
            "min_panels": min(3, count),
            "min_scene_items_in_any_panel": 3,
            "min_global_connections": min(2, max(0, count - 1)),
            "forbid_uniform_large_cards": True,
            "safe_area_is_not_background": False,
        },
    }


def compute_direct_layout_template(
    *,
    diagram_type: str,
    layout_style: str,
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    sections: list[dict[str, Any]] | None = None,
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any] | None:
    """直接计算由模板接管的布局；返回 None 表示继续走网格/分区路径。"""
    normalized_style = (layout_style or "").strip().lower().replace("_", "-")
    if normalized_style == "mechanism-storyboard" and diagram_type in {"mechanism", "system"}:
        return _direct_mechanism_storyboard_layout(
            stages,
            connections,
            width=width,
            height=height,
            margin=margin,
            content_y=content_y,
            content_h=content_h,
            sections=sections or [],
        )
    if normalized_style == "flow-mechanism":
        return _direct_flow_mechanism_layout(
            stages,
            connections,
            width=width,
            height=height,
            margin=margin,
            content_y=content_y,
            content_h=content_h,
        )
    if diagram_type == "flow":
        return _direct_flow_layout(
            stages,
            connections,
            width=width,
            height=height,
            margin=margin,
            content_y=content_y,
            content_h=content_h,
        )
    if diagram_type == "timeline":
        return _direct_timeline_layout(
            stages,
            connections,
            width=width,
            height=height,
            margin=margin,
            content_y=content_y,
            content_h=content_h,
        )
    if diagram_type == "compare":
        return _direct_compare_layout(
            stages,
            connections,
            width=width,
            height=height,
            margin=margin,
            content_y=content_y,
            content_h=content_h,
        )
    return None


def _flow_template(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    margin: int,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        x, y, w, h = (int(stage.get(key, 0)) for key in ("x", "y", "w", "h"))
        cx = x + w // 2
        cy = y + max(72, int(h * 0.42))
        shape_size = max(64, min(112, w // 2, h // 3))
        render_as, node_shape = _block_render(stage, "flow")
        if node_shape == "decision-diamond":
            shape_w, shape_h = shape_size + 28, shape_size
        elif node_shape == "terminator-pill":
            shape_w, shape_h = shape_size + 48, max(44, shape_size // 2)
        else:
            shape_w = shape_h = shape_size
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=shape_w,
            shape_h=shape_h,
            render_as=render_as,
            node_shape=node_shape,
        )
        icon_w = min(shape_w, shape_h, 76)
        stage["slots"] = {
            "icon": _slot(cx - icon_w / 2, cy - icon_w / 2, icon_w, icon_w),
            "title": _slot(x + 12, cy + shape_h / 2 + 16, w - 24, 30),
            "description": _slot(x + 16, cy + shape_h / 2 + 52, w - 32, min(58, max(28, y + h - cy - shape_h / 2 - 60))),
        }
        if stage.get("metric"):
            stage["slots"]["metric"] = _slot(x + w * 0.18, y + h - 48, w * 0.64, 30)
        nodes.append(
            {
                "stage_id": stage.get("id", index),
                "cx": cx,
                "cy": cy,
                "w": shape_w,
                "h": shape_h,
                "node_shape": stage["node_shape"],
                "render_as": stage["render_as"],
                "label_slot": stage["slots"].get("title"),
                "caption_slot": stage["slots"].get("description"),
                "icon_slot": stage["slots"].get("icon"),
            }
        )

    stage_by_id = {stage.get("id", index): stage for index, stage in enumerate(stages)}
    reserved_zones = _reserved_content_zones(stages)
    label_obstacles: list[tuple[float, float, float, float]] = []
    arrows: list[dict[str, Any]] = []
    for connection in connections:
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        source_anchor = source["anchor_points"]["right"]
        target_anchor = target["anchor_points"]["left"]
        arrows.append(
            _route_connection_payload(
                connection,
                source,
                target,
                start=source_anchor,
                end=target_anchor,
                reserved_zones=reserved_zones,
                width=width,
                height=height,
                label_obstacles=label_obstacles,
                source_port="right",
                target_port="left",
                hint_y=[source_anchor.get("y", 0)],
            )
        )

    first = nodes[0] if nodes else {"cx": margin, "cy": height // 2}
    last = nodes[-1] if nodes else {"cx": width - margin, "cy": height // 2}
    return {
        "template_id": "flow-path",
        "template_slots": {
            "path": {
                "orientation": "horizontal",
                "x1": first["cx"],
                "y1": first["cy"],
                "x2": last["cx"],
                "y2": last["cy"],
                "points": [{"stage_id": node["stage_id"], "x": node["cx"], "y": node["cy"]} for node in nodes],
            },
            "nodes": nodes,
            "arrows": arrows,
            "connection_paths": arrows,
            "reserved_content_zones": reserved_zones,
            "quality_targets": _quality_targets(width, height),
            "legend": _slot(width - margin - 180, height - margin - 72, 160, 48),
        },
        "render_contract": {
            "required_groups": ["stage-node", "semantic-connection"],
            "required_template_marks": ["flow-path"],
            "allowed_stage_shapes": ["circle-icon", "decision-diamond", "terminator-pill"],
            "forbid_uniform_large_cards": True,
        },
    }


def _timeline_template(
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    *,
    content_y: int,
    content_h: int,
    width: int,
    margin: int,
) -> dict[str, Any]:
    axis_y = content_y + content_h // 2
    nodes: list[dict[str, Any]] = []
    axis_x1 = margin + 88
    axis_x2 = width - margin - 88
    for index, stage in enumerate(stages):
        x, y, w, h = (int(stage.get(key, 0)) for key in ("x", "y", "w", "h"))
        cx = x + w // 2
        label_top = index % 2 == 0
        render_as, node_shape = _block_render(stage, "timeline")
        card_w = max(150, min(w - 16, 220))
        # 兼容旧网格模板：即使 cell 靠近画布边缘，事件卡也必须完整可见。
        card_edge_gap = 12
        card_w = min(card_w, max(1, width - margin * 2 - card_edge_gap * 2))
        card_center_left = margin + card_edge_gap + card_w / 2
        card_center_right = width - margin - card_edge_gap - card_w / 2
        cx = _clamp(cx, card_center_left, max(card_center_left, card_center_right))
        _apply_stage_template(
            stage,
            cx=cx,
            cy=axis_y,
            shape_w=36,
            shape_h=36,
            render_as=render_as,
            node_shape=node_shape,
        )
        card_h = max(118, min(150, int(h * 0.42)))
        if label_top:
            card_y = max(y + 8, axis_y - card_h - 42)
        else:
            card_y = min(y + h - card_h - 8, axis_y + 42)
        card = _slot(cx - card_w / 2, card_y, card_w, card_h)
        icon_size = _timeline_icon_size(card)
        card_slots = _timeline_card_slots(card, icon_size, stage=stage)
        title_slot = card_slots["title"]
        desc_slot = card_slots["description"]
        stage.update(
            {
                "x": card["x"],
                "y": card["y"],
                "w": card["w"],
                "h": card["h"],
                "safe_area": dict(card),
                "timeline_card": dict(card),
            }
        )
        stage["slots"] = {
            "title": title_slot,
            "description": desc_slot,
            "metric": card_slots["metric"],
        }
        if "scene" in card_slots:
            stage["slots"]["scene"] = card_slots["scene"]
        else:
            stage["slots"]["icon"] = card_slots["icon"]
        nodes.append(
            {
                "stage_id": stage.get("id", index),
                "x": cx,
                "y": axis_y,
                "card_slot": card,
                "event_card_slot": card,
                "marker_slot": _slot(cx - 18, axis_y - 18, 36, 36),
                "icon_slot": stage["slots"].get("icon", stage["slots"].get("scene")),
                "node_shape": node_shape,
                "render_as": render_as,
                "label_position": "top" if label_top else "bottom",
                "label_slot": title_slot,
                "caption_slot": desc_slot,
                "metric_slot": stage["slots"]["metric"],
            }
        )

    return {
        "template_id": "timeline-axis",
        "template_slots": {
            "axis": {"x1": axis_x1, "y1": axis_y, "x2": axis_x2, "y2": axis_y},
            "milestones": nodes,
            "arrows": [
                {"from": item.get("from"), "to": item.get("to"), "type": item.get("type", "arrow")}
                for item in connections
            ],
            "reserved_content_zones": _reserved_content_zones(stages),
        },
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": ["timeline-axis"],
            "allowed_stage_shapes": ["milestone-dot"],
            "forbid_uniform_large_cards": True,
        },
    }


def _compare_template(
    stages: list[dict[str, Any]],
    *,
    content_y: int,
    content_h: int,
    width: int,
    margin: int,
) -> dict[str, Any]:
    values: list[float] = []
    for stage in stages:
        metric = stage.get("metric") if isinstance(stage.get("metric"), dict) else {}
        try:
            values.append(float(metric.get("value", 0)))
        except (TypeError, ValueError):
            values.append(0.0)
    scale_max = max(values) if any(value > 0 for value in values) else max(1.0, float(len(stages) or 1))
    count = max(1, len(stages))
    available_w = max(1, width - margin * 2 - 144)
    compact_w = max(360, min(available_w, count * 190))
    chart_x = margin + 72 + max(0, (available_w - compact_w) / 2)
    chart = {
        "type": "bar" if any(value > 0 for value in values) else "comparison-table",
        "x": chart_x,
        "y": content_y + 46,
        "w": compact_w,
        "h": max(180, min(402, content_h - 170)),
    }
    if float(scale_max).is_integer() and 2 <= scale_max <= 80:
        scale_units = int(scale_max)
        bar_area_h = max(160, (int(chart["h"] - 42) // scale_units) * scale_units)
        chart["h"] = bar_area_h + 42
    chart["baseline"] = chart["y"] + chart["h"]
    chart["scale_max"] = scale_max
    items: list[dict[str, Any]] = []
    band_w = chart["w"] / count
    for index, stage in enumerate(stages):
        value = values[index] if index < len(values) else 0.0
        cx = int(chart["x"] + band_w * index + band_w / 2)
        bar_h = int(round((value / scale_max) * (chart["h"] - 42))) if scale_max > 0 else 0
        bar_w = int(max(28, min(86, band_w * 0.42)))
        bar_slot = _slot(cx - bar_w / 2, chart["baseline"] - bar_h, bar_w, max(2, bar_h))
        render_as, node_shape = _block_render(stage, "compare")
        _apply_stage_template(
            stage,
            cx=cx,
            cy=chart["baseline"] - max(8, bar_h // 2),
            shape_w=bar_w,
            shape_h=max(20, bar_h),
            render_as=render_as,
            node_shape=node_shape,
        )
        stage["bar_slot"] = bar_slot
        stage["slots"] = {
            "metric": _slot(cx - band_w * 0.35, max(chart["y"], bar_slot["y"] - 34), band_w * 0.7, 28),
            "icon": _slot(cx - 22, chart["baseline"] + 10, 44, 44),
            "title": _slot(cx - band_w * 0.42, chart["baseline"] + 58, band_w * 0.84, 30),
        }
        if stage.get("description"):
            stage["slots"]["description"] = _slot(cx - band_w * 0.42, chart["baseline"] + 90, band_w * 0.84, 38)
        items.append(
            {
                "stage_id": stage.get("id", index),
                "value": value,
                "label": (stage.get("metric") or {}).get("label", ""),
                "bar_slot": bar_slot,
                "icon_slot": stage["slots"]["icon"],
                "title_slot": stage["slots"]["title"],
                "metric_slot": stage["slots"]["metric"],
            }
        )

    return {
        "template_id": "comparison-chart",
        "template_slots": {
            "chart": chart,
            "items": items,
            "baseline": {"x1": chart["x"], "y1": chart["baseline"], "x2": chart["x"] + chart["w"], "y2": chart["baseline"]},
        },
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": ["comparison-chart"],
            "bar_role": "comparison-bar",
            "forbid_uniform_large_cards": True,
        },
    }


def _mechanism_template(
    stages: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    modules: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        x, y, w, h = (int(stage.get(key, 0)) for key in ("x", "y", "w", "h"))
        cx, cy = _center(stage)
        render_as, node_shape = _block_render(stage, str(stage.get("diagram_type", "mechanism")))
        shape_w = max(56, min(w - 16, 170))
        shape_h = max(42, min(h - 16, 92))
        if render_as in {"attention_matrix", "probability_bars"}:
            shape_w = max(92, min(w - 16, 190))
            shape_h = max(72, min(h - 16, 124))
        _apply_stage_template(
            stage,
            cx=cx,
            cy=cy,
            shape_w=shape_w,
            shape_h=shape_h,
            render_as=render_as,
            node_shape=node_shape,
        )
        modules.append(
            {
                "stage_id": stage.get("id", index),
                "section": stage.get("section", "main"),
                "render_as": stage["render_as"],
                "node_shape": stage["node_shape"],
                "safe_area": _slot(x, y, w, h),
                "geometry": stage["node_geometry"],
                "anchor_points": stage["anchor_points"],
            }
        )
    return {
        "template_id": "section-module-map" if sections else "module-map",
        "template_slots": {
            "sections": sections,
            "modules": modules,
            "reserved_content_zones": _reserved_content_zones(stages),
            "quality_targets": _quality_targets(width, height),
        },
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": ["section-container"] if sections else [],
            "forbid_uniform_large_cards": True,
        },
    }


def _card_template(stages: list[dict[str, Any]]) -> dict[str, Any]:
    for stage in stages:
        stage.setdefault("render_as", "card_item")
        stage.setdefault("node_shape", "rounded-card")
    return {
        "template_id": "card-grid",
        "template_slots": {"cards": [{"stage_id": stage.get("id"), "cell": _slot(stage.get("x", 0), stage.get("y", 0), stage.get("w", 0), stage.get("h", 0))} for stage in stages]},
        "render_contract": {
            "required_groups": ["stage-node"],
            "required_template_marks": [],
            "forbid_uniform_large_cards": False,
        },
    }


def apply_layout_template(
    *,
    diagram_type: str,
    stages: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    width: int,
    height: int,
    margin: int,
    content_y: int,
    content_h: int,
) -> dict[str, Any]:
    """把已计算的 cell 坐标增强为图类型专属模板契约。"""
    if diagram_type == "flow":
        return _flow_template(stages, connections, width=width, height=height, margin=margin)
    if diagram_type == "timeline":
        return _timeline_template(stages, connections, content_y=content_y, content_h=content_h, width=width, margin=margin)
    if diagram_type == "compare":
        return _compare_template(stages, content_y=content_y, content_h=content_h, width=width, margin=margin)
    if diagram_type in {"mechanism", "system"}:
        return _mechanism_template(stages, sections, width=width, height=height)
    return _card_template(stages)
