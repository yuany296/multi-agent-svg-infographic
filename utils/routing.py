"""Deterministic orthogonal routing helpers for SVG layout templates.

The layout templates decide where semantic nodes live.  This module only
chooses connection paths and label slots that avoid known content boxes.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_point(point: dict[str, Any] | Sequence[float] | Point) -> Point:
    if isinstance(point, dict):
        return (_to_float(point.get("x")), _to_float(point.get("y")))
    return (_to_float(point[0]), _to_float(point[1]))


def point_dict(point: Point) -> dict[str, int]:
    return {"x": int(round(point[0])), "y": int(round(point[1]))}


def slot_bounds(slot: dict[str, Any]) -> Bounds:
    x = _to_float(slot.get("x"))
    y = _to_float(slot.get("y"))
    w = max(0.0, _to_float(slot.get("w", slot.get("width", 0))))
    h = max(0.0, _to_float(slot.get("h", slot.get("height", 0))))
    return (x, y, x + w, y + h)


def bounds_to_slot(bounds: Bounds) -> dict[str, int]:
    left, top, right, bottom = bounds
    return {
        "x": int(round(left)),
        "y": int(round(top)),
        "w": max(1, int(round(right - left))),
        "h": max(1, int(round(bottom - top))),
    }


def expand_bounds(bounds: Bounds, padding: float) -> Bounds:
    left, top, right, bottom = bounds
    return (left - padding, top - padding, right + padding, bottom + padding)


def clamp_bounds(bounds: Bounds, canvas_w: float, canvas_h: float) -> Bounds:
    left, top, right, bottom = bounds
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    left = max(0.0, min(left, max(0.0, canvas_w - width)))
    top = max(0.0, min(top, max(0.0, canvas_h - height)))
    return (left, top, left + width, top + height)


def boxes_overlap(first: Bounds, second: Bounds) -> bool:
    return not (
        first[2] <= second[0]
        or first[0] >= second[2]
        or first[3] <= second[1]
        or first[1] >= second[3]
    )


def segment_hits_bounds(p1: Point, p2: Point, bounds: Bounds) -> bool:
    """Return True when an orthogonal segment passes through a box interior."""
    x1, y1 = p1
    x2, y2 = p2
    left, top, right, bottom = bounds
    eps = 1e-6

    if abs(y1 - y2) < eps:
        y = y1
        if not (top + eps < y < bottom - eps):
            return False
        seg_left = min(x1, x2)
        seg_right = max(x1, x2)
        overlap_left = max(seg_left, left)
        overlap_right = min(seg_right, right)
        if overlap_right - overlap_left <= eps:
            return False
        if abs(overlap_left - x1) < eps and abs(overlap_right - x1) < eps:
            return False
        if abs(overlap_left - x2) < eps and abs(overlap_right - x2) < eps:
            return False
        return True

    if abs(x1 - x2) < eps:
        x = x1
        if not (left + eps < x < right - eps):
            return False
        seg_top = min(y1, y2)
        seg_bottom = max(y1, y2)
        overlap_top = max(seg_top, top)
        overlap_bottom = min(seg_bottom, bottom)
        if overlap_bottom - overlap_top <= eps:
            return False
        if abs(overlap_top - y1) < eps and abs(overlap_bottom - y1) < eps:
            return False
        if abs(overlap_top - y2) < eps and abs(overlap_bottom - y2) < eps:
            return False
        return True

    return False


def segment_axis(p1: Point, p2: Point) -> str:
    if abs(p1[1] - p2[1]) < 1e-6:
        return "horizontal"
    if abs(p1[0] - p2[0]) < 1e-6:
        return "vertical"
    return "other"


def port_axis(port: str | None) -> str | None:
    if not port:
        return None
    port = port.lower()
    if port in {"left", "right"}:
        return "horizontal"
    if port in {"top", "bottom"}:
        return "vertical"
    return None


def offset_point(point: Point, port: str | None, distance: float) -> Point:
    if not port:
        return point
    x, y = point
    port = port.lower()
    if port == "left":
        return (x - distance, y)
    if port == "right":
        return (x + distance, y)
    if port == "top":
        return (x, y - distance)
    if port == "bottom":
        return (x, y + distance)
    return point


def route_length(points: Sequence[Point]) -> float:
    return sum(abs(x1 - x2) + abs(y1 - y2) for (x1, y1), (x2, y2) in zip(points, points[1:]))


def route_uses_lane(points: Sequence[Point], value: float, axis: str, tolerance: float = 1.0) -> bool:
    if axis == "x":
        return any(abs(x - value) <= tolerance for x, _ in points)
    return any(abs(y - value) <= tolerance for _, y in points)


def collision_count(points: Sequence[Point], obstacles: Sequence[Bounds]) -> int:
    return sum(
        1
        for p1, p2 in zip(points, points[1:])
        for obstacle in obstacles
        if segment_hits_bounds(p1, p2, obstacle)
    )


def route_is_orthogonal(points: Sequence[Point]) -> bool:
    return all(segment_axis(p1, p2) != "other" for p1, p2 in zip(points, points[1:]))


def route_score(
    points: Sequence[Point],
    hint_x: Sequence[float],
    hint_y: Sequence[float],
    source_port: str | None,
    target_port: str | None,
) -> float:
    length = route_length(points)
    bends = max(0, len(points) - 2)
    score = length + bends * 22
    if len(points) >= 2 and source_port:
        first_axis = segment_axis(points[0], points[1])
        if first_axis != port_axis(source_port):
            score += 180
    if len(points) >= 2 and target_port:
        last_axis = segment_axis(points[-2], points[-1])
        if last_axis != port_axis(target_port):
            score += 180
    for lane in hint_x:
        if route_uses_lane(points, lane, "x"):
            score -= 28
    for lane in hint_y:
        if route_uses_lane(points, lane, "y"):
            score -= 28
    return score


def simplify_points(points: Sequence[Point]) -> list[Point]:
    simplified: list[Point] = []
    for x, y in points:
        point = (round(float(x), 2), round(float(y), 2))
        if simplified and point == simplified[-1]:
            continue
        simplified.append(point)

    collapsed: list[Point] = []
    for point in simplified:
        if len(collapsed) < 2:
            collapsed.append(point)
            continue
        x0, y0 = collapsed[-2]
        x1, y1 = collapsed[-1]
        x2, y2 = point
        if (x0 == x1 == x2) or (y0 == y1 == y2):
            collapsed[-1] = point
        else:
            collapsed.append(point)
    return collapsed


def collect_zone_obstacles(
    zones: Iterable[dict[str, Any]],
    *,
    exclude_stage_ids: set[str] | None = None,
) -> list[Bounds]:
    excludes = exclude_stage_ids or set()
    obstacles: list[Bounds] = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        if str(zone.get("stage_id")) in excludes:
            continue
        slot = zone.get("slot")
        if isinstance(slot, dict):
            obstacles.append(slot_bounds(slot))
            continue
        if all(key in zone for key in ("x", "y")) and ("w" in zone or "width" in zone):
            obstacles.append(slot_bounds(zone))
    return obstacles


def build_orthogonal_route(
    start: Point,
    end: Point,
    obstacles: Sequence[Bounds],
    *,
    canvas_w: float,
    canvas_h: float,
    source_port: str | None = None,
    target_port: str | None = None,
    hint_x: Sequence[float] | None = None,
    hint_y: Sequence[float] | None = None,
    routing_padding: float = 18.0,
    port_clearance: float | None = None,
) -> list[Point]:
    sx, sy = start
    ex, ey = end
    source_port = (source_port or "").strip().lower() or None
    target_port = (target_port or "").strip().lower() or None
    port_clearance = port_clearance if port_clearance is not None else max(16.0, routing_padding * 0.9)
    inner_start = offset_point(start, source_port, port_clearance)
    inner_end = offset_point(end, target_port, port_clearance)
    ssx, ssy = inner_start
    eex, eey = inner_end
    hint_x = [_to_float(value) for value in (hint_x or [])]
    hint_y = [_to_float(value) for value in (hint_y or [])]
    expanded = [expand_bounds(bounds, routing_padding) for bounds in obstacles]
    lane_x = sorted(
        {
            ssx,
            eex,
            round((ssx + eex) / 2, 2),
            *hint_x,
            *[bounds[0] for bounds in expanded],
            *[bounds[2] for bounds in expanded],
        }
    )
    lane_y = sorted(
        {
            ssy,
            eey,
            round((ssy + eey) / 2, 2),
            *hint_y,
            *[bounds[1] for bounds in expanded],
            *[bounds[3] for bounds in expanded],
        }
    )

    if expanded:
        left_rail = max(12.0, min(bounds[0] for bounds in expanded) - 24)
        right_rail = min(canvas_w - 12.0, max(bounds[2] for bounds in expanded) + 24)
        top_rail = max(12.0, min(bounds[1] for bounds in expanded) - 24)
        bottom_rail = min(canvas_h - 12.0, max(bounds[3] for bounds in expanded) + 24)
    else:
        left_rail = max(12.0, min(ssx, eex) - 48)
        right_rail = min(canvas_w - 12.0, max(ssx, eex) + 48)
        top_rail = max(12.0, min(ssy, eey) - 48)
        bottom_rail = min(canvas_h - 12.0, max(ssy, eey) + 48)

    candidates: list[list[Point]] = [
        [start, inner_start, inner_end, end],
        [start, inner_start, (eex, ssy), inner_end, end],
        [start, inner_start, (ssx, eey), inner_end, end],
        [start, inner_start, ((ssx + eex) / 2, ssy), ((ssx + eex) / 2, eey), inner_end, end],
        [start, inner_start, (ssx, (ssy + eey) / 2), (eex, (ssy + eey) / 2), inner_end, end],
        [start, inner_start, (left_rail, ssy), (left_rail, eey), inner_end, end],
        [start, inner_start, (right_rail, ssy), (right_rail, eey), inner_end, end],
        [start, inner_start, (ssx, top_rail), (eex, top_rail), inner_end, end],
        [start, inner_start, (ssx, bottom_rail), (eex, bottom_rail), inner_end, end],
    ]
    for x in lane_x:
        if 0 <= x <= canvas_w:
            candidates.append([start, inner_start, (x, ssy), (x, eey), inner_end, end])
    for y in lane_y:
        if 0 <= y <= canvas_h:
            candidates.append([start, inner_start, (ssx, y), (eex, y), inner_end, end])
    for x in hint_x:
        for y in hint_y:
            if 0 <= x <= canvas_w and 0 <= y <= canvas_h:
                candidates.append([start, inner_start, (x, ssy), (x, y), (eex, y), inner_end, end])

    default_route = simplify_points([start, inner_start, (eex, ssy), inner_end, end])
    default_coll = collision_count(default_route, expanded)
    default_raw_coll = collision_count(default_route, obstacles)
    default_length = route_length(default_route)
    best_route: list[Point] | None = None
    best_score = float("inf")
    best_fallback: list[Point] | None = None
    best_fallback_coll = float("inf")
    best_fallback_score = float("inf")

    for candidate in candidates:
        route = simplify_points(candidate)
        coll = collision_count(route, expanded)
        score = route_score(route, hint_x, hint_y, source_port, target_port)
        if coll == 0 and route_is_orthogonal(route) and score < best_score:
            best_score = score
            best_route = route
        elif route_is_orthogonal(route):
            length = route_length(route)
            raw_coll = collision_count(route, obstacles)
            if (
                coll < default_coll
                and raw_coll <= default_raw_coll
                and length <= default_length
                and (coll < best_fallback_coll or (coll == best_fallback_coll and score < best_fallback_score))
            ):
                best_fallback_coll = coll
                best_fallback_score = score
                best_fallback = route

    return best_route or best_fallback or default_route


def _label_size(label: str) -> tuple[float, float]:
    text = str(label or "").strip()
    wide_chars = sum(1 for char in text if ord(char) > 127)
    narrow_chars = max(0, len(text) - wide_chars)
    width = min(220.0, max(54.0, wide_chars * 15.0 + narrow_chars * 8.0 + 22.0))
    return width, 28.0


def label_size(label: str) -> tuple[float, float]:
    """返回连接标签的统一布局尺寸，供模板预留专用通道。"""
    return _label_size(label)


def _path_midpoint(points: Sequence[Point]) -> Point:
    if not points:
        return (0.0, 0.0)
    if len(points) == 1:
        return points[0]
    total = route_length(points)
    if total <= 0:
        return points[len(points) // 2]
    half = total / 2
    walked = 0.0
    for first, second in zip(points, points[1:]):
        segment_len = route_length([first, second])
        if walked + segment_len >= half and segment_len > 0:
            ratio = (half - walked) / segment_len
            return (
                first[0] + (second[0] - first[0]) * ratio,
                first[1] + (second[1] - first[1]) * ratio,
            )
        walked += segment_len
    return points[-1]


def _label_candidates(points: Sequence[Point], label_w: float, label_h: float, gap: float) -> list[Bounds]:
    candidates: list[Bounds] = []
    for first, second in zip(points, points[1:]):
        axis = segment_axis(first, second)
        mx = (first[0] + second[0]) / 2
        my = (first[1] + second[1]) / 2
        if axis == "horizontal":
            candidates.extend(
                [
                    (mx - label_w / 2, my - label_h - gap, mx + label_w / 2, my - gap),
                    (mx - label_w / 2, my + gap, mx + label_w / 2, my + label_h + gap),
                    (mx + gap, my - label_h / 2, mx + gap + label_w, my + label_h / 2),
                    (mx - label_w - gap, my - label_h / 2, mx - gap, my + label_h / 2),
                ]
            )
        elif axis == "vertical":
            candidates.extend(
                [
                    (mx + gap, my - label_h / 2, mx + gap + label_w, my + label_h / 2),
                    (mx - label_w - gap, my - label_h / 2, mx - gap, my + label_h / 2),
                    (mx - label_w / 2, my - label_h - gap, mx + label_w / 2, my - gap),
                    (mx - label_w / 2, my + gap, mx + label_w / 2, my + label_h + gap),
                ]
            )

    midpoint = _path_midpoint(points)
    candidates.extend(
        [
            (midpoint[0] - label_w / 2, midpoint[1] - label_h - gap, midpoint[0] + label_w / 2, midpoint[1] - gap),
            (midpoint[0] + gap, midpoint[1] - label_h / 2, midpoint[0] + gap + label_w, midpoint[1] + label_h / 2),
            (midpoint[0] - label_w / 2, midpoint[1] + gap, midpoint[0] + label_w / 2, midpoint[1] + label_h + gap),
        ]
    )
    return candidates


def _path_hits_label(points: Sequence[Point], label_box: Bounds, padding: float = 2.0) -> bool:
    expanded = expand_bounds(label_box, padding)
    return any(segment_hits_bounds(first, second, expanded) for first, second in zip(points, points[1:]))


def choose_label_slot(
    points: Sequence[Point],
    label: str,
    *,
    canvas_w: float,
    canvas_h: float,
    obstacles: Sequence[Bounds] = (),
    label_obstacles: list[Bounds] | None = None,
    gap: float = 12.0,
) -> dict[str, int]:
    label_w, label_h = _label_size(label)
    label_obstacles = label_obstacles if label_obstacles is not None else []
    avoid = list(obstacles) + list(label_obstacles)
    midpoint = _path_midpoint(points)
    best: Bounds | None = None
    best_score = float("inf")

    candidates = _label_candidates(points, label_w, label_h, gap)
    midpoint = _path_midpoint(points)
    if obstacles:
        min_top = min(obstacle[1] for obstacle in obstacles)
        max_bottom = max(obstacle[3] for obstacle in obstacles)
        candidates.extend(
            [
                (
                    midpoint[0] - label_w / 2,
                    min_top - label_h - gap,
                    midpoint[0] + label_w / 2,
                    min_top - gap,
                ),
                (
                    midpoint[0] - label_w / 2,
                    max_bottom + gap,
                    midpoint[0] + label_w / 2,
                    max_bottom + gap + label_h,
                ),
            ]
        )
    candidates.extend(
        [
            (midpoint[0] - label_w / 2, gap, midpoint[0] + label_w / 2, gap + label_h),
            (
                midpoint[0] - label_w / 2,
                canvas_h - label_h - gap,
                midpoint[0] + label_w / 2,
                canvas_h - gap,
            ),
        ]
    )

    for candidate in candidates:
        clamped = clamp_bounds(candidate, canvas_w, canvas_h)
        collisions = sum(1 for obstacle in avoid if boxes_overlap(expand_bounds(clamped, 3), expand_bounds(obstacle, 2)))
        path_hit = 1 if _path_hits_label(points, clamped, 3) else 0
        center_x = (clamped[0] + clamped[2]) / 2
        center_y = (clamped[1] + clamped[3]) / 2
        distance = abs(center_x - midpoint[0]) + abs(center_y - midpoint[1])
        score = collisions * 10000 + path_hit * 6000 + distance
        if score < best_score:
            best_score = score
            best = clamped

    if best is None:
        best = clamp_bounds(
            (midpoint[0] - label_w / 2, midpoint[1] - label_h - gap, midpoint[0] + label_w / 2, midpoint[1] - gap),
            canvas_w,
            canvas_h,
        )
    label_obstacles.append(best)
    return bounds_to_slot(best)


def path_kind(points: Sequence[Point], connection_type: str) -> str:
    if connection_type == "feedback_arrow":
        return "curve"
    if len(points) <= 2:
        return "line"
    return "elbow"


def route_connection(
    *,
    start: dict[str, Any] | Sequence[float],
    end: dict[str, Any] | Sequence[float],
    reserved_zones: Sequence[dict[str, Any]],
    canvas_w: float,
    canvas_h: float,
    connection_type: str = "arrow",
    label: str = "",
    source_id: Any = None,
    target_id: Any = None,
    source_port: str | None = "right",
    target_port: str | None = "left",
    hint_x: Sequence[float] | None = None,
    hint_y: Sequence[float] | None = None,
    label_obstacles: list[Bounds] | None = None,
    routing_padding: float = 18.0,
) -> dict[str, Any]:
    start_point = _as_point(start)
    end_point = _as_point(end)
    excludes = {str(source_id), str(target_id)}
    routing_obstacles = collect_zone_obstacles(reserved_zones, exclude_stage_ids=excludes)
    label_avoid = collect_zone_obstacles(reserved_zones)
    route = build_orthogonal_route(
        start_point,
        end_point,
        routing_obstacles,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        source_port=source_port,
        target_port=target_port,
        hint_x=hint_x,
        hint_y=hint_y,
        routing_padding=routing_padding,
    )
    expanded_obstacles = [expand_bounds(bounds, routing_padding) for bounds in routing_obstacles]
    path = {
        "kind": path_kind(route, connection_type),
        "points": [point_dict(point) for point in route],
        "dash": "8 6" if connection_type == "feedback_arrow" else ("6 5" if connection_type == "dashed_arrow" else ""),
    }
    result: dict[str, Any] = {
        "path": path,
        "source_port": source_port or "",
        "target_port": target_port or "",
        "route_score": round(route_score(route, [*_to_float_list(hint_x)], [*_to_float_list(hint_y)], source_port, target_port), 2),
        "collision_count": collision_count(route, expanded_obstacles),
    }
    if str(label or "").strip():
        result["label_slot"] = choose_label_slot(
            route,
            str(label),
            canvas_w=canvas_w,
            canvas_h=canvas_h,
            obstacles=label_avoid,
            label_obstacles=label_obstacles,
        )
    return result


def _to_float_list(values: Sequence[float] | None) -> list[float]:
    return [_to_float(value) for value in (values or [])]
