"""
SVG 验证函数集 —— 语法、坐标、图标引用、语义连接、分区布局等确定性检查。

这些函数不依赖 LLM，只做 XML/数值处理。
"""

import math
import re
import unicodedata
import xml.etree.ElementTree as StdET
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET


def _resolve_root(svg_code: str, root: Element | None) -> Element | None:
    """内部辅助：优先使用已解析的 root，否则从 svg_code 解析。"""
    return root if root is not None else (ET.fromstring(svg_code) if svg_code.strip() else None)


def check_svg_syntax(svg_code: str) -> list[str]:
    """
    使用 defusedxml 检查 SVG 是否为合法 XML。

    返回错误信息列表；空列表表示语法正确。
    """
    errors: list[str] = []
    if not svg_code.strip():
        errors.append("SVG 代码为空")
        return errors
    try:
        ET.fromstring(svg_code)
    except ET.ParseError as e:
        errors.append(f"XML 解析错误：{e}")
    except Exception as e:
        errors.append(f"未知语法错误：{e}")
    return errors


def check_canvas_dimensions(
    svg_code: str,
    expected_width: int,
    expected_height: int,
    root: Element | None = None,
) -> list[str]:
    """确保根 SVG 使用确定性数值尺寸，避免 100% 高宽被宿主容器拉伸。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    viewbox = str(root.get("viewBox", "")).replace(",", " ").split()
    try:
        viewbox_values = [float(value) for value in viewbox]
    except ValueError:
        viewbox_values = []
    expected_viewbox = [0.0, 0.0, float(expected_width), float(expected_height)]
    if len(viewbox_values) != 4 or any(
        abs(actual - expected) > 0.1
        for actual, expected in zip(viewbox_values, expected_viewbox)
    ):
        errors.append(
            f"根 SVG viewBox 必须为 '0 0 {expected_width} {expected_height}'"
        )

    for attribute, expected in (("width", expected_width), ("height", expected_height)):
        raw = str(root.get(attribute, "")).strip()
        try:
            actual = float(raw.replace("px", ""))
        except ValueError:
            errors.append(f"根 SVG {attribute} 必须是数值 {expected}，不能使用 {raw!r}")
            continue
        if abs(actual - expected) > 0.1:
            errors.append(f"根 SVG {attribute} 应为 {expected}，实际为 {raw}")
    return errors


def check_semantic_connections(
    svg_code: str,
    expected: list[dict],
    root: Element | None = None,
) -> list[str]:
    """核对 SVG 中显式标记的语义连接与结构规划完全一致。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    expected_map: dict[tuple[str, str, str], str] = {}
    for item in expected:
        if not isinstance(item, dict) or "from" not in item or "to" not in item:
            continue
        key = (
            str(item.get("from")),
            str(item.get("to")),
            str(item.get("type", "arrow")),
        )
        expected_map[key] = str(item.get("label", "")).strip()

    actual_counts: dict[tuple[str, str, str], int] = {}
    connection_descendants: set[int] = set()
    for group in root.iter():
        tag = group.tag.split("}")[-1]
        classes = str(group.get("class", "")).split()
        if tag != "g" or "semantic-connection" not in classes:
            continue
        key = (
            str(group.get("data-from", "")),
            str(group.get("data-to", "")),
            str(group.get("data-type", "")),
        )
        actual_counts[key] = actual_counts.get(key, 0) + 1
        descendants = list(group.iter())
        connection_descendants.update(id(elem) for elem in descendants)
        label = str(group.get("data-label", "")).strip()
        expected_label = expected_map.get(key)
        if expected_label is not None and label != expected_label:
            errors.append(
                f"连接 {key[0]}→{key[1]} 的 data-label 应为 {expected_label!r}，实际为 {label!r}"
            )
        if expected_label:
            text_values = [
                "".join(elem.itertext()).strip()
                for elem in descendants
                if elem.tag.split("}")[-1] == "text"
            ]
            if expected_label not in text_values:
                errors.append(f"连接 {key[0]}→{key[1]} 缺少可见标签 {expected_label!r}")
        connector_shapes = [
            elem
            for elem in descendants
            if elem.tag.split("}")[-1] in {"line", "path"}
            and any(elem.get(name) for name in ("marker-start", "marker-mid", "marker-end"))
        ]
        if not connector_shapes:
            errors.append(f"连接 {key[0]}→{key[1]} 组内缺少带 marker 的 line/path")

    for key in expected_map:
        count = actual_counts.get(key, 0)
        if count == 0:
            errors.append(f"缺少语义连接 {key[0]}→{key[1]}（{key[2]}）")
        elif count > 1:
            errors.append(f"语义连接 {key[0]}→{key[1]}（{key[2]}）重复 {count} 次")
    for key in actual_counts:
        if key not in expected_map:
            errors.append(f"存在未声明的语义连接 {key[0]}→{key[1]}（{key[2]}）")

    for elem in root.iter():
        if id(elem) in connection_descendants:
            continue
        marker = (
            elem.get("marker-start")
            or elem.get("marker-mid")
            or elem.get("marker-end")
            or ""
        )
        if marker and "internal-arrow" in marker:
            continue  # 内部场景箭头、装饰性小箭头允许使用 internal-arrow marker
        if any(elem.get(name) for name in ("marker-start", "marker-mid", "marker-end")):
            tag = elem.tag.split("}")[-1]
            errors.append(f"<{tag}> 在 semantic-connection 之外使用了箭头 marker")
    return errors


def check_section_layout(
    svg_code: str,
    expected: list[dict],
    root: Element | None = None,
) -> list[str]:
    """校验每个大分区都有独立容器，并使用布局引擎给出的精确边界。"""
    if not svg_code.strip() or not expected:
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    groups: dict[str, list[Element]] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        if "section-container" not in str(elem.get("class", "")).split():
            continue
        section_id = str(elem.get("data-section-id", ""))
        groups.setdefault(section_id, []).append(elem)

    errors: list[str] = []
    tolerance = 8.0
    for index, section in enumerate(expected):
        section_id = str(section.get("id", index))
        matches = groups.get(section_id, [])
        if not matches:
            errors.append(
                f"分区 {section_id} 缺少 <g class='section-container' data-section-id='{section_id}'>"
            )
            continue
        if len(matches) > 1:
            errors.append(f"分区 {section_id} 存在 {len(matches)} 个重复 section-container group")

        expected_box = tuple(float(section.get(key, 0)) for key in ("x", "y", "w", "h"))
        matching_rect = False
        for elem in matches[0].iter():
            if elem.tag.split("}")[-1] != "rect":
                continue
            try:
                actual_box = tuple(
                    float(elem.get(key, 0)) for key in ("x", "y", "width", "height")
                )
            except (TypeError, ValueError):
                continue
            if all(abs(actual - wanted) <= tolerance for actual, wanted in zip(actual_box, expected_box)):
                matching_rect = True
                break
        if not matching_rect:
            errors.append(
                f"分区 {section_id} 缺少与规划边界一致的背景 rect：应为 x/y/w/h={expected_box}"
            )

    undeclared = sorted(section_id for section_id in groups if section_id not in {
        str(section.get("id", index)) for index, section in enumerate(expected)
    })
    for section_id in undeclared:
        errors.append(f"存在未声明的分区容器 {section_id}")
    return errors


def _has_template_mark(root: Element, *marks: str) -> bool:
    wanted = set(marks)
    for elem in root.iter():
        classes = set(str(elem.get("class", "")).split())
        template_role = str(elem.get("data-template-role", "")).strip()
        template_mark = str(elem.get("data-template-mark", "")).strip()
        bar_role = str(elem.get("data-bar-role", "")).strip()
        data_role = str(elem.get("data-role", "")).strip()
        if (
            classes & wanted
            or template_role in wanted
            or template_mark in wanted
            or bar_role in wanted
            or data_role in wanted
        ):
            return True
    return False


def _stage_node_groups(root: Element) -> dict[str, list[Element]]:
    groups: dict[str, list[Element]] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        if "stage-node" not in str(elem.get("class", "")).split():
            continue
        groups.setdefault(str(elem.get("data-stage-id", "")), []).append(elem)
    return groups


def _element_has_template_mark(elem: Element, mark: str) -> bool:
    return (
        mark in set(str(elem.get("class", "")).split())
        or str(elem.get("data-template-role", "")).strip() == mark
        or str(elem.get("data-template-mark", "")).strip() == mark
        or str(elem.get("data-bar-role", "")).strip() == mark
        or str(elem.get("data-role", "")).strip() == mark
    )


def _semantic_connection_groups(root: Element) -> set[tuple[str, str, str]]:
    groups: set[tuple[str, str, str]] = set()
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        if "semantic-connection" not in str(elem.get("class", "")).split():
            continue
        groups.add(
            (
                str(elem.get("data-from", "")),
                str(elem.get("data-to", "")),
                str(elem.get("data-type", "")),
            )
        )
    return groups


def check_comparison_bar_slots(
    svg_code: str,
    structure: dict,
    root: Element | None = None,
) -> list[str]:
    """校验 compare 柱状图必须严格使用 LayoutEngine 给出的 bar_slot。"""
    if str(structure.get("diagram_type", "")).strip().lower() != "compare":
        return []
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    stage_groups = _stage_node_groups(root)
    tolerance = 2.0
    for stage in structure.get("stages", []):
        if not isinstance(stage, dict) or not isinstance(stage.get("metric"), dict):
            continue
        bar_slot = stage.get("bar_slot")
        if not isinstance(bar_slot, dict):
            continue
        stage_id = str(stage.get("id"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        bar_boxes: list[tuple[float, float, float, float]] = []
        for elem in groups[0].iter():
            if _element_has_template_mark(elem, "comparison-bar"):
                bbox = _bbox_of_group(elem)
                if bbox is not None:
                    bar_boxes.append(bbox)
        if not bar_boxes:
            errors.append(f"对比图阶段 {stage_id} 缺少 comparison-bar 比例柱")
            continue
        expected = _slot_box(bar_slot)
        if not any(
            all(abs(actual - target) <= tolerance for actual, target in zip(bbox, expected))
            for bbox in bar_boxes
        ):
            expected_text = (
                f"x={bar_slot.get('x')}, y={bar_slot.get('y')}, "
                f"w={bar_slot.get('w')}, h={bar_slot.get('h')}"
            )
            actual_text = ", ".join(
                f"x={box[0]:.0f}, y={box[1]:.0f}, w={box[2] - box[0]:.0f}, h={box[3] - box[1]:.0f}"
                for box in bar_boxes[:3]
            )
            errors.append(
                f"对比图阶段 {stage_id} 的 comparison-bar 未按 bar_slot 绘制：期望 {expected_text}，实际 {actual_text}"
            )
    return errors


def _storyboard_panel_groups(root: Element) -> dict[str, list[Element]]:
    groups: dict[str, list[Element]] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        classes = set(str(elem.get("class", "")).split())
        if (
            "storyboard-panel" not in classes
            and elem.get("data-template-role") != "storyboard-panel"
            and elem.get("data-render-as") != "storyboard-panel"
        ):
            continue
        groups.setdefault(str(elem.get("data-stage-id", "")), []).append(elem)
    return groups


def _storyboard_scene_groups(panel_group: Element) -> list[Element]:
    scenes: list[Element] = []
    for elem in panel_group.iter():
        if elem is panel_group or elem.tag.split("}")[-1] != "g":
            continue
        classes = set(str(elem.get("class", "")).split())
        if (
            "storyboard-scene" in classes
            or elem.get("data-role") == "scene"
            or elem.get("data-template-role") == "storyboard-scene"
        ):
            scenes.append(elem)
    return scenes


def _storyboard_scene_item_count(scene_group: Element) -> int:
    drawable_tags = {
        "circle",
        "ellipse",
        "line",
        "path",
        "polygon",
        "polyline",
        "rect",
        "text",
        "use",
    }
    return sum(
        1
        for elem in scene_group.iter()
        if elem is not scene_group and elem.tag.split("}")[-1] in drawable_tags
    )


def _storyboard_scene_balance_errors(stage_id: str, scene_groups: list[Element]) -> list[str]:
    """分镜 scene 应以图画为主，文字辅助说明。"""
    errors: list[str] = []
    drawable_tags = {
        "circle",
        "ellipse",
        "line",
        "path",
        "polygon",
        "polyline",
        "rect",
        "use",
    }
    for scene_group in scene_groups:
        visible_texts = [
            "".join(elem.itertext()).strip()
            for elem in scene_group.iter()
            if elem is not scene_group
            and elem.tag.split("}")[-1] == "text"
            and _bbox_of_element(elem) is not None
            and "".join(elem.itertext()).strip()
        ]
        non_text_drawables = sum(
            1
            for elem in scene_group.iter()
            if elem is not scene_group and elem.tag.split("}")[-1] in drawable_tags
        )
        if len(visible_texts) > 8:
            errors.append(f"模板契约：mechanism-storyboard 阶段 {stage_id} 的 scene 文字标签过多")
        long_labels = [text for text in visible_texts if len(text) > 12]
        if len(long_labels) > 4:
            errors.append(f"模板契约：mechanism-storyboard 阶段 {stage_id} 的 scene 长句文字较多，建议减少")
        if not non_text_drawables and visible_texts:
            errors.append(f"模板契约：mechanism-storyboard 阶段 {stage_id} 的 scene 缺少图形元素")
    return errors


def _iter_transformed_elements(
    root: Element,
) -> list[tuple[Element, tuple[float, float, float, float] | None]]:
    items: list[tuple[Element, tuple[float, float, float, float] | None]] = []

    def visit(node: Element, parent_matrix: tuple[float, float, float, float, float, float]) -> None:
        matrix = _multiply_transform(parent_matrix, _parse_transform(str(node.get("transform", ""))))
        bbox = _bbox_of_element(node)
        items.append((node, _transform_box(bbox, matrix) if bbox is not None else None))
        for child in list(node):
            visit(child, matrix)

    visit(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    return items


def _storyboard_scene_badge_icon_errors(stage_id: str, scene_groups: list[Element]) -> list[str]:
    errors: list[str] = []
    reported: set[str] = set()
    for scene_group in scene_groups:
        scene_bbox = _bbox_of_group(scene_group)
        if scene_bbox is None:
            continue
        scene_w = scene_bbox[2] - scene_bbox[0]
        scene_h = scene_bbox[3] - scene_bbox[1]
        if scene_w <= 0 or scene_h <= 0:
            continue
        x_edge = max(48.0, scene_w * 0.28)
        y_edge = max(32.0, scene_h * 0.22)
        for elem, bbox in _iter_transformed_elements(scene_group):
            if elem is scene_group or elem.tag.split("}")[-1] != "use" or bbox is None:
                continue
            href = elem.get("href") or elem.get("{http://www.w3.org/1999/xlink}href") or ""
            icon_id = str(elem.get("data-generated-icon", "")).strip()
            if not icon_id and not href.startswith("#icon-"):
                continue
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            if width > 54 or height > 54:
                continue
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            near_x_edge = cx <= scene_bbox[0] + x_edge or cx >= scene_bbox[2] - x_edge
            near_y_edge = cy <= scene_bbox[1] + y_edge or cy >= scene_bbox[3] - y_edge
            if not (near_x_edge and near_y_edge):
                continue
            key = icon_id or href
            if key in reported:
                continue
            reported.add(key)
            errors.append(
                f"模板契约：mechanism-storyboard 阶段 {stage_id} 的 scene 存在独立图标角标 {key}；"
                "通用图标必须融入主图形，不能作为四角/边缘装饰叠加"
            )
    return errors


def check_storyboard_icon_badges(
    svg_code: str,
    structure: dict,
    root: Element | None = None,
) -> list[str]:
    """禁止 mechanism-storyboard 在 scene 四角叠加通用小图标角标。"""
    if str(structure.get("template_id", "")).strip().lower() != "mechanism-storyboard":
        return []
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    panel_groups = _storyboard_panel_groups(root)
    stage_ids = [
        str(item.get("stage_id"))
        for item in (structure.get("template_slots", {}) or {}).get("panels", [])
        if isinstance(item, dict) and item.get("stage_id") is not None
    ]
    if not stage_ids:
        stage_ids = [
            str(stage.get("id"))
            for stage in structure.get("stages", [])
            if isinstance(stage, dict) and stage.get("id") is not None
        ]
    for stage_id in stage_ids:
        for panel_group in panel_groups.get(stage_id, []):
            errors.extend(
                _storyboard_scene_badge_icon_errors(
                    stage_id,
                    _storyboard_scene_groups(panel_group),
                )
            )
    return errors


def check_scene_density(
    svg_code: str,
    stages: list[dict],
    root: Element | None = None,
) -> list[str]:
    """检查 storyboard panel 的 scene 区域是否被图形充分填充。

    scene 内所有可见绘图元素的包围盒与 scene_area 对比：
    宽度或高度覆盖率 < 50% 时报告低密度。
    """
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    drawable_tags = {
        "circle", "ellipse", "line", "path", "polygon",
        "polyline", "rect", "use",
    }
    stage_groups = _stage_node_groups(root)
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        scene_area = stage.get("scene_area")
        if not isinstance(scene_area, dict):
            continue
        stage_id = str(stage.get("id", "?"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        # 找 panel 内的 scene 元素
        scene_elems: list[Element] = []
        for panel_elem in groups:
            scene_elems.extend(_storyboard_scene_groups(panel_elem))
        if not scene_elems:
            continue

        sa_w = float(scene_area.get("w", 0))
        sa_h = float(scene_area.get("h", 0))
        if sa_w <= 0 or sa_h <= 0:
            continue

        for scene_elem in scene_elems:
            boxes = [
                bbox
                for child, bbox in _iter_transformed_elements(scene_elem)
                if child is not scene_elem
                and child.tag.split("}")[-1] in drawable_tags
                and bbox is not None
            ]
            if not boxes:
                errors.append(
                    f"stage '{stage_id}' 的 scene 区域无可见图形元素"
                )
                continue
            union = _union_boxes(boxes)
            if union is None:
                continue
            content_w = union[2] - union[0]
            content_h = union[3] - union[1]
            w_ratio = content_w / sa_w
            h_ratio = content_h / sa_h
            if w_ratio < 0.50:
                errors.append(
                    f"stage '{stage_id}' scene 图形宽度仅占 {w_ratio:.0%}（{content_w:.0f}/{sa_w:.0f}px），"
                    f"低于 50% 阈值，请水平扩展图形或增加元素以填充 scene 区域"
                )
            if h_ratio < 0.50:
                errors.append(
                    f"stage '{stage_id}' scene 图形高度仅占 {h_ratio:.0%}（{content_h:.0f}/{sa_h:.0f}px），"
                    f"低于 50% 阈值，请垂直扩展图形以填充 scene 区域"
                )
            # 检查水平居中：内容中心偏离 scene 中心超过 25% 宽度时报告
            content_cx = (union[0] + union[2]) / 2
            sa_center_x = float(scene_area.get("x", 0)) + sa_w / 2
            offset_ratio = abs(content_cx - sa_center_x) / sa_w if sa_w > 0 else 0
            if offset_ratio > 0.25:
                direction = "偏左" if content_cx < sa_center_x else "偏右"
                errors.append(
                    f"stage '{stage_id}' scene 图形{direction}，"
                    f"内容中心偏离场景中心 {offset_ratio:.0%}，"
                    f"请水平居中图形以平衡画面"
                )
            # 垂直偏移比低密度更直接地对应“图案挤在卡片顶部/底部”。
            # 超过 scene 高度的 20% 时确定性报告，供 Validator 阻断并触发最小修复。
            content_cy = (union[1] + union[3]) / 2
            sa_center_y = float(scene_area.get("y", 0)) + sa_h / 2
            vertical_offset_ratio = abs(content_cy - sa_center_y) / sa_h if sa_h > 0 else 0
            if vertical_offset_ratio > 0.20:
                direction = "偏上" if content_cy < sa_center_y else "偏下"
                errors.append(
                    f"stage '{stage_id}' scene 图形{direction}，"
                    f"内容中心偏离场景中心 {vertical_offset_ratio:.0%}，"
                    f"请垂直居中图形；只平移或等比扩展该 scene，不得改动卡片、文字或其他 stage"
                )
            # 检查场景内容在面板中的垂直占比：内容太薄导致大面积空白
            panel_h = float(stage.get("panel_rect", {}).get("h") or stage.get("h", 0))
            if panel_h > 0:
                fill_ratio = content_h / panel_h
                if fill_ratio < 0.35:
                    errors.append(
                        f"stage '{stage_id}' 场景图形高度仅占面板的 {fill_ratio:.0%}"
                        f"（{content_h:.0f}/{panel_h:.0f}px），"
                        f"面板内大面积空白，请垂直扩展图形使其更充实"
                    )
    return errors


def _check_storyboard_contract(
    root: Element,
    structure: dict,
) -> list[str]:
    errors: list[str] = []
    contract = structure.get("render_contract", {})
    template_slots = structure.get("template_slots", {})
    panels = [
        item for item in template_slots.get("panels", [])
        if isinstance(item, dict)
    ]
    min_panels = int(contract.get("min_panels", 3) or 3)
    min_scene_items = int(contract.get("min_scene_items_in_any_panel", 3) or 3)
    min_connections = int(contract.get("min_global_connections", 2) or 0)

    if len(panels) < min_panels:
        errors.append(f"模板契约：mechanism-storyboard 至少需要 {min_panels} 个 panel")
    if not _has_template_mark(root, "mechanism-storyboard"):
        errors.append("模板契约：mechanism-storyboard 缺少 mechanism-storyboard 整体标记")

    panel_groups = _storyboard_panel_groups(root)
    stage_by_id = {str(stage.get("id")): stage for stage in structure.get("stages", []) if isinstance(stage, dict)}
    max_scene_items = 0
    for panel in panels:
        stage_id = str(panel.get("stage_id", ""))
        matches = panel_groups.get(stage_id, [])
        if not matches:
            errors.append(
                f"模板契约：mechanism-storyboard 阶段 {stage_id} 缺少 storyboard-panel group"
            )
            continue
        if len(matches) > 1:
            errors.append(
                f"模板契约：mechanism-storyboard 阶段 {stage_id} 存在重复 storyboard-panel group"
            )
        panel_group = matches[0]
        stage = stage_by_id.get(stage_id, {})
        if isinstance((stage.get("slots") or {}).get("icon"), dict):
            direct_icon_groups = [
                child
                for child in list(panel_group)
                if child.tag.split("}")[-1] == "g" and child.get("data-role") == "icon"
            ]
            if not direct_icon_groups:
                errors.append(
                    f"模板契约：mechanism-storyboard 阶段 {stage_id} 的 icon 必须是 panel 的独立图标组，不能嵌套在 scene 内"
                )
        scene_groups = _storyboard_scene_groups(panel_group)
        if not scene_groups:
            errors.append(
                f"模板契约：mechanism-storyboard 阶段 {stage_id} 缺少 storyboard-scene / data-role='scene'"
            )
            continue
        errors.extend(_storyboard_scene_balance_errors(stage_id, scene_groups))
        errors.extend(_storyboard_scene_badge_icon_errors(stage_id, scene_groups))
        max_scene_items = max(
            max_scene_items,
            *(_storyboard_scene_item_count(group) for group in scene_groups),
        )

    if panels and max_scene_items < min_scene_items:
        errors.append(
            f"模板契约：mechanism-storyboard 至少一个 scene 需要包含 {min_scene_items} 个以上内部小元素"
        )

    global_connections = [
        item for item in template_slots.get("global_connections", [])
        if isinstance(item, dict)
    ]
    feedback_connections = [
        item for item in template_slots.get("feedback_connections", [])
        if isinstance(item, dict)
    ]
    if len(global_connections) + len(feedback_connections) < min_connections:
        errors.append(
            f"模板契约：mechanism-storyboard 至少需要 {min_connections} 条全局/反馈连接"
        )
    if any(str(item.get("type")) == "feedback_arrow" for item in structure.get("connections", [])) and not feedback_connections:
        errors.append("模板契约：mechanism-storyboard 存在 feedback_arrow 语义但缺少反馈箭头槽")
    return errors


def _large_rect_card_ratio(root: Element, stages: list[dict]) -> float:
    if len(stages) < 2:
        return 0.0
    stage_groups = _stage_node_groups(root)
    large_cards = 0
    for stage in stages:
        stage_id = str(stage.get("id"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        stage_w = float(stage.get("w", 0) or 0)
        stage_h = float(stage.get("h", 0) or 0)
        for elem in groups[0].iter():
            if elem.tag.split("}")[-1] != "rect":
                continue
            try:
                rect_w = float(elem.get("width", 0))
                rect_h = float(elem.get("height", 0))
            except (TypeError, ValueError):
                continue
            if stage_w <= 0 or stage_h <= 0:
                continue
            fills_most_cell = rect_w >= stage_w * 0.62 and rect_h >= stage_h * 0.42
            visually_card_sized = rect_w >= 150 and rect_h >= 96
            if fills_most_cell and visually_card_sized:
                large_cards += 1
                break
    return large_cards / max(1, len(stages))


def _timeline_axis_is_visible(root: Element, structure: dict) -> bool:
    if _has_template_mark(root, "timeline-axis"):
        return True
    try:
        width = float(structure.get("width") or root.get("width") or 0)
    except (TypeError, ValueError):
        width = 0.0
    viewbox = str(root.get("viewBox", "")).replace(",", " ").split()
    if width <= 0 and len(viewbox) == 4:
        try:
            width = float(viewbox[2])
        except ValueError:
            width = 0.0
    min_len = max(160.0, width * 0.35)
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag != "line":
            continue
        try:
            x1 = float(elem.get("x1", 0))
            y1 = float(elem.get("y1", 0))
            x2 = float(elem.get("x2", 0))
            y2 = float(elem.get("y2", 0))
        except (TypeError, ValueError):
            continue
        if abs(y1 - y2) <= 4 and abs(x2 - x1) >= min_len:
            return True
    return False


def _safe_area_frame_errors(root: Element, stages: list[dict]) -> list[str]:
    """非 cards 中禁止把 stage safe_area 直接画成节点外框。"""
    errors: list[str] = []
    stage_groups = _stage_node_groups(root)
    for stage in stages:
        stage_id = str(stage.get("id"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        safe = stage.get("safe_area") if isinstance(stage.get("safe_area"), dict) else stage
        try:
            sx = float(safe.get("x", 0))
            sy = float(safe.get("y", 0))
            sw = float(safe.get("w", 0))
            sh = float(safe.get("h", 0))
        except (TypeError, ValueError):
            continue
        if sw <= 0 or sh <= 0:
            continue
        for elem in groups[0].iter():
            if elem.tag.split("}")[-1] != "rect":
                continue
            try:
                rx = float(elem.get("x", 0))
                ry = float(elem.get("y", 0))
                rw = float(elem.get("width", 0))
                rh = float(elem.get("height", 0))
            except (TypeError, ValueError):
                continue
            nearly_same_box = (
                abs(rx - sx) <= 6
                and abs(ry - sy) <= 6
                and abs(rw - sw) <= 8
                and abs(rh - sh) <= 8
            )
            fills_safe_area = (
                rw >= sw * 0.88
                and rh >= sh * 0.82
                and rx <= sx + sw * 0.08
                and ry <= sy + sh * 0.08
            )
            if nearly_same_box or fills_safe_area:
                errors.append(
                    f"模板契约：阶段 {stage_id} 直接用 safe_area 绘制大矩形外框，非 cards 类型应使用 node_geometry 绘制节点主体"
                )
                break
    return errors


def check_template_contract(
    svg_code: str,
    structure: dict,
    root: Element | None = None,
) -> list[str]:
    """校验模板化布局的最低结构，防止非 cards 图退回卡片网格。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    diagram_type = str(structure.get("diagram_type", "cards")).strip().lower()
    stages = [stage for stage in structure.get("stages", []) if isinstance(stage, dict)]
    connections = [
        connection
        for connection in structure.get("connections", [])
        if isinstance(connection, dict)
    ]
    stage_groups = _stage_node_groups(root)
    connection_groups = _semantic_connection_groups(root)
    template_id = str(structure.get("template_id", "")).strip().lower()

    for stage in stages:
        stage_id = str(stage.get("id"))
        if stage_id not in stage_groups:
            errors.append(f"模板契约：阶段 {stage_id} 缺少 stage-node group")
    for connection in connections:
        key = (
            str(connection.get("from")),
            str(connection.get("to")),
            str(connection.get("type", "arrow")),
        )
        if key not in connection_groups:
            errors.append(f"模板契约：缺少 semantic-connection {key[0]}→{key[1]}（{key[2]}）")

    if diagram_type == "cards":
        return errors
    if template_id == "mechanism-storyboard":
        errors.extend(_check_storyboard_contract(root, structure))
        errors.extend(_check_actual_connection_paths(root, structure))
        return errors

    is_timeline = diagram_type == "timeline" or template_id == "timeline-axis"
    if not is_timeline:
        errors.extend(_safe_area_frame_errors(root, stages))

        card_ratio = _large_rect_card_ratio(root, stages)
        if card_ratio > 0.7:
            errors.append(
                "模板契约：非 cards 类型中大部分 stage 被绘制成铺满 cell 的大矩形卡片，缺少对应图类型的视觉结构"
            )

    if diagram_type == "flow":
        if len(stages) > 1 and not connections:
            errors.append("模板契约：flow 类型必须有顺序语义连接")
        if len(stages) > 1 and not (_has_template_mark(root, "flow-path") or connection_groups):
            errors.append("模板契约：flow 类型缺少主流程路径 flow-path 或语义箭头")
    elif diagram_type == "timeline":
        if not _timeline_axis_is_visible(root, structure):
            errors.append("模板契约：timeline 类型缺少 timeline-axis 时间轴")
    elif diagram_type == "compare":
        has_chart = _has_template_mark(root, "comparison-chart", "comparison-table")
        if not has_chart:
            errors.append("模板契约：compare 类型缺少 comparison-chart 或 comparison-table 图表区域")
        metric_count = sum(1 for stage in stages if isinstance(stage.get("metric"), dict))
        if metric_count and not _has_template_mark(root, "comparison-bar"):
            errors.append("模板契约：compare 类型有 metric.value 时必须绘制 comparison-bar 比例条")
        errors.extend(check_comparison_bar_slots(svg_code, structure, root=root))
    elif template_id == "flow-mechanism":
        if not _has_template_mark(root, "flow-mechanism"):
            errors.append("模板契约：flow-mechanism 类型缺少 flow-mechanism 主流程标记")
    elif diagram_type in {"mechanism", "system"} and structure.get("sections"):
        if not _has_template_mark(root, "section-container"):
            errors.append("模板契约：mechanism/system 类型缺少 section-container 分区容器")
    return errors


def check_overlap(stages: list[dict]) -> list[str]:
    """检测 stage 矩形之间是否有重叠，返回可读的错误列表。"""
    errors: list[str] = []
    rects = [
        (s.get("id", i), s.get("x", 0), s.get("y", 0), s.get("w", 0), s.get("h", 0))
        for i, s in enumerate(stages)
    ]
    for i in range(len(rects)):
        id1, x1, y1, w1, h1 = rects[i]
        for j in range(i + 1, len(rects)):
            id2, x2, y2, w2, h2 = rects[j]
            if x1 < x2 + w2 and x1 + w1 > x2 and y1 < y2 + h2 and y1 + h1 > y2:
                errors.append(
                    f"阶段 {id1}（x={x1}, y={y1}, w={w1}, h={h1}）与阶段 {id2}（x={x2}, y={y2}, w={w2}, h={h2}）重叠"
                )
    return errors


def _estimate_text_width(text: str, font_size: float) -> float:
    """按 Unicode 字符宽度估算文本：中日韩字符约 1em，拉丁字符约 0.6em。"""
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.33
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            units += 1.0
        else:
            units += 0.6
    return max(font_size, units * font_size)


def _bbox_of_element(elem: Element) -> tuple[float, float, float, float] | None:
    """计算单个 SVG 元素的边界框 (xmin, ymin, xmax, ymax)。

    目前支持常见基本形状；带 transform 的 group 暂不做精确解析。
    """
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
    style = str(elem.get("style", "")).replace(" ", "").lower()
    if (
        elem.get("display") == "none"
        or elem.get("visibility") in {"hidden", "collapse"}
        or "display:none" in style
        or "visibility:hidden" in style
    ):
        return None
    try:
        if float(elem.get("opacity", "1")) <= 0:
            return None
    except ValueError:
        pass
    try:
        if tag == "rect":
            x = float(elem.get("x", 0))
            y = float(elem.get("y", 0))
            w = float(elem.get("width", 0))
            h = float(elem.get("height", 0))
            return x, y, x + w, y + h
        if tag == "circle":
            cx = float(elem.get("cx", 0))
            cy = float(elem.get("cy", 0))
            r = float(elem.get("r", 0))
            return cx - r, cy - r, cx + r, cy + r
        if tag == "ellipse":
            cx = float(elem.get("cx", 0))
            cy = float(elem.get("cy", 0))
            rx = float(elem.get("rx", 0))
            ry = float(elem.get("ry", 0))
            return cx - rx, cy - ry, cx + rx, cy + ry
        if tag == "line":
            x1 = float(elem.get("x1", 0))
            y1 = float(elem.get("y1", 0))
            x2 = float(elem.get("x2", 0))
            y2 = float(elem.get("y2", 0))
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        if tag == "path":
            values = [
                float(value)
                for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", elem.get("d", ""))
            ]
            if len(values) >= 2:
                xs = values[0::2]
                ys = values[1::2]
                return min(xs), min(ys), max(xs), max(ys)
        if tag in {"polygon", "polyline"}:
            points = _parse_points_attribute(str(elem.get("points", "")))
            if points:
                xs = [point["x"] for point in points]
                ys = [point["y"] for point in points]
                return min(xs), min(ys), max(xs), max(ys)
        if tag == "text":
            fill = str(elem.get("fill", "black")).strip().lower()
            stroke = str(elem.get("stroke", "none")).strip().lower()
            try:
                fill_opacity = float(elem.get("fill-opacity", "1"))
            except ValueError:
                fill_opacity = 1.0
            if (fill == "none" or fill_opacity <= 0) and stroke in {"", "none"}:
                return None
            x = float(elem.get("x", 0))
            y = float(elem.get("y", 0))
            size = float(elem.get("font-size", "16").replace("px", ""))
            ascent = size * 0.82
            descent = size * 0.22
            anchor = elem.get("text-anchor", "start")
            tspans = [child for child in elem if child.tag.split("}")[-1] == "tspan"]
            if tspans:
                line_boxes: list[tuple[float, float, float, float]] = []
                current_y = y
                for tspan in tspans:
                    line_x = float(tspan.get("x", x))
                    if tspan.get("y") is not None:
                        current_y = float(tspan.get("y", current_y))
                    elif tspan.get("dy") is not None:
                        raw_dy = str(tspan.get("dy", "0")).strip().lower()
                        if raw_dy.endswith("em"):
                            current_y += float(raw_dy[:-2]) * size
                        else:
                            current_y += float(raw_dy.replace("px", ""))
                    text = "".join(tspan.itertext()).strip()
                    estimated_width = _estimate_text_width(text, size)
                    if anchor == "middle":
                        xmin = line_x - estimated_width / 2
                    elif anchor == "end":
                        xmin = line_x - estimated_width
                    else:
                        xmin = line_x
                    line_boxes.append(
                        (xmin, current_y - ascent, xmin + estimated_width, current_y + descent)
                    )
                return (
                    min(box[0] for box in line_boxes),
                    min(box[1] for box in line_boxes),
                    max(box[2] for box in line_boxes),
                    max(box[3] for box in line_boxes),
                )
            text = "".join(elem.itertext()).strip()
            estimated_width = _estimate_text_width(text, size)
            if anchor == "middle":
                xmin = x - estimated_width / 2
            elif anchor == "end":
                xmin = x - estimated_width
            else:
                xmin = x
            return xmin, y - ascent, xmin + estimated_width, y + descent
        if tag == "use":
            x = float(elem.get("x", 0))
            y = float(elem.get("y", 0))
            width = float(elem.get("width", 0))
            height = float(elem.get("height", 0))
            return x, y, x + width, y + height
    except (ValueError, TypeError):
        return None
    return None


def check_text_completeness(
    svg_code: str,
    root: Element | None = None,
) -> list[str]:
    """禁止用省略号代表未写完的正文内容。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    markers = ("...", "…", "⋯")
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "text":
            continue
        if _bbox_of_element(elem) is None:
            continue
        text = "".join(elem.itertext()).strip()
        if any(marker in text for marker in markers):
            errors.append(f"文本内容使用省略号截断：{text!r}")
    return errors


def _multiply_transform(
    parent: tuple[float, float, float, float, float, float],
    local: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    pa, pb, pc, pd, pe, pf = parent
    la, lb, lc, ld, le, lf = local
    return (
        pa * la + pc * lb,
        pb * la + pd * lb,
        pa * lc + pc * ld,
        pb * lc + pd * ld,
        pa * le + pc * lf + pe,
        pb * le + pd * lf + pf,
    )


def _parse_transform(value: str) -> tuple[float, float, float, float, float, float]:
    """Parse common SVG transforms used by generated groups and labels."""
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, raw_args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", value or ""):
        args = [
            float(item)
            for item in re.findall(
                r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?",
                raw_args,
            )
        ]
        local = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        if name == "translate" and args:
            local = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) > 1 else 0.0)
        elif name == "scale" and args:
            sy = args[1] if len(args) > 1 else args[0]
            local = (args[0], 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "matrix" and len(args) == 6:
            local = tuple(args)  # type: ignore[assignment]
        elif name == "rotate" and args:
            angle = math.radians(args[0])
            rotation = (math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle), 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                local = _multiply_transform(
                    _multiply_transform((1.0, 0.0, 0.0, 1.0, cx, cy), rotation),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                local = rotation
        matrix = _multiply_transform(matrix, local)
    return matrix


def _transform_box(
    box: tuple[float, float, float, float],
    matrix: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = matrix
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    transformed = [(a * x + c * y + e, b * x + d * y + f) for x, y in corners]
    return (
        min(point[0] for point in transformed),
        min(point[1] for point in transformed),
        max(point[0] for point in transformed),
        max(point[1] for point in transformed),
    )


def _bbox_of_group(elem: Element) -> tuple[float, float, float, float] | None:
    """合并 group 内可静态计算的前景元素边界。"""
    boxes: list[tuple[float, float, float, float]] = []

    def visit(node: Element, parent_matrix: tuple[float, float, float, float, float, float]) -> None:
        matrix = _multiply_transform(parent_matrix, _parse_transform(str(node.get("transform", ""))))
        bbox = _bbox_of_element(node)
        if bbox is not None:
            boxes.append(_transform_box(bbox, matrix))
        for child in list(node):
            visit(child, matrix)

    visit(elem, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def normalize_connection_label_positions(
    svg_code: str,
    connections: list[dict] | None,
) -> str:
    """Move only connection-label groups into their declared top-left slots."""
    if not svg_code.strip() or not connections:
        return svg_code
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError:
        return svg_code

    groups: dict[tuple[str, str, str], Element] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        if "semantic-connection" not in str(elem.get("class", "")).split():
            continue
        key = (
            str(elem.get("data-from", "")),
            str(elem.get("data-to", "")),
            str(elem.get("data-type", "arrow")),
        )
        groups[key] = elem

    changed = False
    for connection in connections:
        if not isinstance(connection, dict) or not str(connection.get("label", "")).strip():
            continue
        slot = connection.get("label_slot")
        if not isinstance(slot, dict):
            continue
        try:
            slot_x = float(slot["x"])
            slot_y = float(slot["y"])
            slot_w = float(slot["w"])
            slot_h = float(slot["h"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (
            str(connection.get("from", "")),
            str(connection.get("to", "")),
            str(connection.get("type", "arrow")),
        )
        group = groups.get(key)
        if group is None:
            continue
        label_groups = [
            elem
            for elem in group.iter()
            if elem.tag.split("}")[-1] == "g" and elem.get("data-role") == "connection-label"
        ]
        if len(label_groups) != 1:
            continue
        label_group = label_groups[0]
        bbox = _bbox_of_group(label_group)
        if bbox is None:
            continue
        target_x = slot_x + max(0.0, (slot_w - (bbox[2] - bbox[0])) / 2)
        target_y = slot_y + max(0.0, (slot_h - (bbox[3] - bbox[1])) / 2)
        dx = target_x - bbox[0]
        dy = target_y - bbox[1]
        if abs(dx) < 0.1 and abs(dy) < 0.1:
            continue

        transform = str(label_group.get("transform", ""))
        match = re.search(
            r"translate\(\s*([-+]?(?:\d*\.\d+|\d+))\s*(?:[, ]\s*([-+]?(?:\d*\.\d+|\d+)))?\s*\)",
            transform,
        )
        if match:
            tx = float(match.group(1)) + dx
            ty = float(match.group(2) or 0.0) + dy
            replacement = f"translate({tx:.2f}, {ty:.2f})"
            label_group.set("transform", transform[:match.start()] + replacement + transform[match.end():])
        else:
            prefix = f"translate({dx:.2f}, {dy:.2f})"
            label_group.set("transform", f"{prefix} {transform}".strip())
        changed = True

    if not changed:
        return svg_code
    if root.tag.startswith("{"):
        StdET.register_namespace("", root.tag[1:].split("}", 1)[0])
    return StdET.tostring(root, encoding="unicode")


def _union_boxes(
    boxes: list[tuple[float, float, float, float]]
) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _box_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float = 2.0,
) -> bool:
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] >= inner[2] - tolerance
        and outer[3] >= inner[3] - tolerance
    )


def _outside_slot(
    bbox: tuple[float, float, float, float],
    slot: dict,
    tolerance: float = 8.0,
) -> bool:
    x, y, w, h = (float(slot.get(key, 0)) for key in ("x", "y", "w", "h"))
    xmin, ymin, xmax, ymax = bbox
    return (
        xmin < x - tolerance
        or ymin < y - tolerance
        or xmax > x + w + tolerance
        or ymax > y + h + tolerance
    )


def _boxes_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    tolerance: float = 8.0,
) -> bool:
    overlap_w = min(first[2], second[2]) - max(first[0], second[0])
    overlap_h = min(first[3], second[3]) - max(first[1], second[1])
    return overlap_w > tolerance and overlap_h > tolerance


def _slot_box(slot: dict) -> tuple[float, float, float, float]:
    x, y, w, h = (float(slot.get(key, 0)) for key in ("x", "y", "w", "h"))
    return x, y, x + w, y + h


def _box_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return max(0.0, width) * max(0.0, height)


def _inside_stage_box(
    bbox: tuple[float, float, float, float],
    stage: dict,
    tolerance: float = 8.0,
) -> bool:
    for key in ("timeline_card", "safe_area"):
        slot = stage.get(key)
        if isinstance(slot, dict) and not _outside_slot(bbox, slot, tolerance=tolerance):
            return True
    fallback = {
        "x": stage.get("x", 0),
        "y": stage.get("y", 0),
        "w": stage.get("w", 0),
        "h": stage.get("h", 0),
    }
    return not _outside_slot(bbox, fallback, tolerance=tolerance)


def _stage_container_bbox(stage_group: Element) -> tuple[float, float, float, float] | None:
    rect_boxes = [
        bbox
        for elem in stage_group.iter()
        if elem is not stage_group and elem.tag.split("}")[-1] == "rect"
        for bbox in [_bbox_of_element(elem)]
        if bbox is not None
    ]
    if not rect_boxes:
        return None
    return max(rect_boxes, key=_box_area)


def _expanded_slot_box(slot: dict, padding: float = 0.0) -> tuple[float, float, float, float]:
    box = _slot_box(slot)
    return box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding


def _segment_crosses_box(
    first: dict[str, float],
    second: dict[str, float],
    box: tuple[float, float, float, float],
) -> bool:
    x1, y1 = float(first.get("x", 0)), float(first.get("y", 0))
    x2, y2 = float(second.get("x", 0)), float(second.get("y", 0))
    segment_box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if (
        segment_box[2] < box[0]
        or segment_box[0] > box[2]
        or segment_box[3] < box[1]
        or segment_box[1] > box[3]
    ):
        return False
    if x1 == x2:
        return box[0] < x1 < box[2] and max(y1, y2) > box[1] and min(y1, y2) < box[3]
    if y1 == y2:
        return box[1] < y1 < box[3] and max(x1, x2) > box[0] and min(x1, x2) < box[2]
    return True


def _point_in_box(point: dict[str, float], box: tuple[float, float, float, float]) -> bool:
    x, y = float(point.get("x", 0)), float(point.get("y", 0))
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _point_near_box_edge(
    point: dict[str, float],
    box: tuple[float, float, float, float],
    tolerance: float = 10.0,
) -> bool:
    x, y = float(point.get("x", 0)), float(point.get("y", 0))
    if not (box[0] - tolerance <= x <= box[2] + tolerance and box[1] - tolerance <= y <= box[3] + tolerance):
        return False
    return (
        abs(x - box[0]) <= tolerance
        or abs(x - box[2]) <= tolerance
        or abs(y - box[1]) <= tolerance
        or abs(y - box[3]) <= tolerance
    )


def _is_endpoint_exit_segment(
    first: dict[str, float],
    second: dict[str, float],
    box: tuple[float, float, float, float],
    *,
    at_source: bool,
) -> bool:
    endpoint = first if at_source else second
    other = second if at_source else first
    if not _point_near_box_edge(endpoint, box):
        return False
    x, y = float(endpoint.get("x", 0)), float(endpoint.get("y", 0))
    ox, oy = float(other.get("x", 0)), float(other.get("y", 0))
    distances = {
        "left": abs(x - box[0]),
        "right": abs(x - box[2]),
        "top": abs(y - box[1]),
        "bottom": abs(y - box[3]),
    }
    side = min(distances, key=distances.get)
    if side == "left":
        return ox <= box[0]
    if side == "right":
        return ox >= box[2]
    if side == "top":
        return oy <= box[1]
    return oy >= box[3]


def _parse_points_attribute(raw: str) -> list[dict[str, float]]:
    values = [
        float(value)
        for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", raw)
    ]
    return [
        {"x": values[index], "y": values[index + 1]}
        for index in range(0, len(values) - 1, 2)
    ]


def _parse_path_points(raw: str) -> list[dict[str, float]]:
    tokens = re.findall(r"[MmLlHhVvCcQqSsTtZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", raw)
    points: list[dict[str, float]] = []
    index = 0
    command = ""
    current = (0.0, 0.0)
    subpath_start = (0.0, 0.0)

    def is_command(token: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]", token))

    def has_numbers(count: int) -> bool:
        return index + count <= len(tokens) and all(
            not is_command(tokens[index + offset])
            for offset in range(count)
        )

    def read_number() -> float:
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    def add_point(x: float, y: float, relative: bool = False) -> None:
        nonlocal current
        if relative:
            x += current[0]
            y += current[1]
        current = (x, y)
        points.append({"x": x, "y": y})

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1
        if not command:
            index += 1
            continue
        relative = command.islower()
        upper = command.upper()
        if upper == "M":
            first = True
            while has_numbers(2):
                x, y = read_number(), read_number()
                add_point(x, y, relative)
                if first:
                    subpath_start = current
                    first = False
                    command = "l" if relative else "L"
        elif upper == "L":
            while has_numbers(2):
                add_point(read_number(), read_number(), relative)
        elif upper == "H":
            while has_numbers(1):
                x = read_number() + (current[0] if relative else 0.0)
                add_point(x, current[1])
        elif upper == "V":
            while has_numbers(1):
                y = read_number() + (current[1] if relative else 0.0)
                add_point(current[0], y)
        elif upper == "C":
            while has_numbers(6):
                x1, y1 = read_number(), read_number()
                x2, y2 = read_number(), read_number()
                x, y = read_number(), read_number()
                if relative:
                    x1, y1 = x1 + current[0], y1 + current[1]
                    x2, y2 = x2 + current[0], y2 + current[1]
                    x, y = x + current[0], y + current[1]
                points.extend([{"x": x1, "y": y1}, {"x": x2, "y": y2}])
                add_point(x, y)
        elif upper in {"Q", "S"}:
            while has_numbers(4):
                x1, y1 = read_number(), read_number()
                x, y = read_number(), read_number()
                if relative:
                    x1, y1 = x1 + current[0], y1 + current[1]
                    x, y = x + current[0], y + current[1]
                points.append({"x": x1, "y": y1})
                add_point(x, y)
        elif upper == "T":
            while has_numbers(2):
                add_point(read_number(), read_number(), relative)
        elif upper == "Z":
            add_point(subpath_start[0], subpath_start[1])
            command = ""
        else:
            index += 1
    return points


def _semantic_connection_points(group: Element) -> list[dict[str, float]]:
    for elem in group.iter():
        if elem is group:
            continue
        tag = elem.tag.split("}")[-1]
        if tag == "line":
            try:
                return [
                    {"x": float(elem.get("x1", 0)), "y": float(elem.get("y1", 0))},
                    {"x": float(elem.get("x2", 0)), "y": float(elem.get("y2", 0))},
                ]
            except (TypeError, ValueError):
                continue
        if tag == "polyline":
            points = _parse_points_attribute(str(elem.get("points", "")))
            if len(points) >= 2:
                return points
        if tag == "path":
            points = _parse_path_points(str(elem.get("d", "")))
            if len(points) >= 2:
                return points
    return []


def _check_actual_connection_paths(root: Element, structure: dict) -> list[str]:
    template_slots = structure.get("template_slots", {}) if isinstance(structure.get("template_slots"), dict) else {}
    zones = [item for item in template_slots.get("reserved_content_zones", []) if isinstance(item, dict)]
    if not zones:
        return []
    errors: list[str] = []
    for group in root.iter():
        if group.tag.split("}")[-1] != "g":
            continue
        if "semantic-connection" not in str(group.get("class", "")).split():
            continue
        points = _semantic_connection_points(group)
        if len(points) < 2:
            continue
        actual_slots = {
            "reserved_content_zones": zones,
            "connection_paths": [
                {
                    "from": group.get("data-from", ""),
                    "to": group.get("data-to", ""),
                    "type": group.get("data-type", "arrow"),
                    "path": {"points": points},
                }
            ],
        }
        errors.extend(
            "实际 SVG " + error
            for error in _check_connection_path_zones(actual_slots)
        )
    return errors


def _check_connection_path_zones(template_slots: dict) -> list[str]:
    errors: list[str] = []
    paths = [item for item in template_slots.get("connection_paths", []) if isinstance(item, dict)]
    zones = [item for item in template_slots.get("reserved_content_zones", []) if isinstance(item, dict)]
    if not paths or not zones:
        return errors
    for path_item in paths:
        path = path_item.get("path") if isinstance(path_item.get("path"), dict) else path_item
        points = [point for point in path.get("points", []) if isinstance(point, dict)]
        if len(points) < 2:
            continue
        segments = list(zip(points, points[1:]))
        for zone in zones:
            slot = zone.get("slot")
            if not isinstance(slot, dict):
                continue
            box = _expanded_slot_box(slot, float(zone.get("padding", 0) or 0))
            zone_stage = str(zone.get("stage_id"))
            for segment_index, (first, second) in enumerate(segments):
                if (
                    zone_stage == str(path_item.get("from"))
                    and segment_index == 0
                    and _is_endpoint_exit_segment(first, second, box, at_source=True)
                ):
                    continue
                if (
                    zone_stage == str(path_item.get("to"))
                    and segment_index == len(segments) - 1
                    and _is_endpoint_exit_segment(first, second, box, at_source=False)
                ):
                    continue
                if _segment_crosses_box(first, second, box):
                    errors.append(
                        f"连接 {path_item.get('from')}→{path_item.get('to')} 穿过阶段 {zone.get('stage_id')} 的 {zone.get('role')} 避让区"
                    )
                    break
    return errors


def _check_connection_label_slots(template_slots: dict) -> list[str]:
    errors: list[str] = []
    paths = [item for item in template_slots.get("connection_paths", []) if isinstance(item, dict)]
    zones = [item for item in template_slots.get("reserved_content_zones", []) if isinstance(item, dict)]
    previous_labels: list[tuple[float, float, float, float]] = []
    for path_item in paths:
        label_slot = path_item.get("label_slot")
        if not isinstance(label_slot, dict):
            continue
        path = path_item.get("path") if isinstance(path_item.get("path"), dict) else path_item
        points = [point for point in path.get("points", []) if isinstance(point, dict)]
        if len(points) < 2:
            continue
        label_box = _expanded_slot_box(label_slot, 4)
        for first, second in zip(points, points[1:]):
            if _segment_crosses_box(first, second, label_box):
                errors.append(
                    f"连接 {path_item.get('from')}→{path_item.get('to')} 的标签槽压住箭头路径，应放到箭头旁边"
                )
                break
        for zone in zones:
            slot = zone.get("slot")
            if not isinstance(slot, dict):
                continue
            zone_box = _expanded_slot_box(slot, float(zone.get("padding", 0) or 0))
            if _boxes_overlap(label_box, zone_box):
                errors.append(
                    f"连接 {path_item.get('from')}→{path_item.get('to')} 的标签槽进入阶段 {zone.get('stage_id')} 的 {zone.get('role')} 避让区"
                )
                break
        for previous in previous_labels:
            if _boxes_overlap(label_box, previous):
                errors.append(
                    f"连接 {path_item.get('from')}→{path_item.get('to')} 的标签槽与其他连接标签重叠"
                )
                break
        previous_labels.append(label_box)
    return errors


def _label_path_distance(
    label_box: tuple[float, float, float, float],
    points: list[dict],
) -> float:
    """返回标签框到正交连接路径的最短距离。"""
    best = float("inf")
    for first, second in zip(points, points[1:]):
        x1, y1 = float(first.get("x", 0)), float(first.get("y", 0))
        x2, y2 = float(second.get("x", 0)), float(second.get("y", 0))
        if y1 == y2:
            horizontal_gap = max(0.0, label_box[0] - max(x1, x2), min(x1, x2) - label_box[2])
            vertical_gap = max(0.0, label_box[1] - y1, y1 - label_box[3])
        elif x1 == x2:
            horizontal_gap = max(0.0, label_box[0] - x1, x1 - label_box[2])
            vertical_gap = max(0.0, label_box[1] - max(y1, y2), min(y1, y2) - label_box[3])
        else:
            continue
        best = min(best, (horizontal_gap**2 + vertical_gap**2) ** 0.5)
    return best


def check_connection_label_layout(structure: dict) -> list[str]:
    """验证标签槽被模板安放在连接旁的专用通道中。"""
    template_slots = structure.get("template_slots", {}) if isinstance(structure.get("template_slots"), dict) else {}
    errors = _check_connection_label_slots(template_slots)
    header = structure.get("header") if isinstance(structure.get("header"), dict) else None
    header_box = _slot_box(header) if header else None
    paths = [item for item in template_slots.get("connection_paths", []) if isinstance(item, dict)]
    path_by_key = {
        (str(item.get("from")), str(item.get("to")), str(item.get("type", "arrow"))): item
        for item in paths
    }
    lanes = [item for item in template_slots.get("label_lanes", []) if isinstance(item, dict)]
    lane_by_key = {
        (str(item.get("from")), str(item.get("to")), str(item.get("type", "arrow"))): item
        for item in lanes
    }
    is_storyboard = str(structure.get("template_id", "")).strip().lower() == "mechanism-storyboard"

    for connection in structure.get("connections", []):
        if not isinstance(connection, dict) or not str(connection.get("label", "")).strip():
            continue
        key = (str(connection.get("from")), str(connection.get("to")), str(connection.get("type", "arrow")))
        label_slot = connection.get("label_slot")
        if not isinstance(label_slot, dict):
            errors.append(f"连接 {key[0]}→{key[1]} 缺少标签槽")
            continue
        label_box = _slot_box(label_slot)
        if header_box and _boxes_overlap(label_box, header_box, tolerance=0):
            errors.append(f"连接 {key[0]}→{key[1]} 的标签槽进入总标题区")
        lane = lane_by_key.get(key)
        if is_storyboard and lane is None:
            errors.append(f"连接 {key[0]}→{key[1]} 缺少专用标签通道")
        if lane is not None and isinstance(lane.get("slot"), dict):
            if not _box_contains(_slot_box(lane["slot"]), label_box, tolerance=1):
                errors.append(f"连接 {key[0]}→{key[1]} 的标签槽越出专用标签通道")
        path_item = path_by_key.get(key)
        if path_item is not None:
            path = path_item.get("path") if isinstance(path_item.get("path"), dict) else path_item
            points = [point for point in path.get("points", []) if isinstance(point, dict)]
            if len(points) >= 2 and _label_path_distance(label_box, points) > 24:
                errors.append(f"连接 {key[0]}→{key[1]} 的标签槽离箭头过远")
    return errors


def is_connection_label_render_error(error: str) -> bool:
    """识别由最终 SVG 渲染位置导致的连接标签错误。"""
    message = str(error)
    return message.startswith("连接 ") and ("标签" in message or "connection-label" in message)


def check_layout_quality(structure: dict) -> list[str]:
    """检查布局契约中的通用视觉质量：主体占用、紧凑度与标签槽避让。"""
    errors: list[str] = []
    template_id = str(structure.get("template_id", "")).strip().lower()
    diagram_type = str(structure.get("diagram_type", "")).strip().lower()
    width = float(structure.get("width", 1200) or 1200)
    height = float(structure.get("height", 800) or 800)
    header = structure.get("header") if isinstance(structure.get("header"), dict) else {}
    content_top = float(header.get("y", 0)) + float(header.get("h", 0))
    content_h = max(1.0, height - content_top)
    template_slots = structure.get("template_slots", {}) if isinstance(structure.get("template_slots"), dict) else {}

    if template_id == "mechanism-storyboard":
        panel_boxes = [
            _slot_box(item.get("panel_rect", {}))
            for item in template_slots.get("panels", [])
            if isinstance(item, dict) and isinstance(item.get("panel_rect"), dict)
        ]
        if panel_boxes:
            union = (
                min(box[0] for box in panel_boxes),
                min(box[1] for box in panel_boxes),
                max(box[2] for box in panel_boxes),
                max(box[3] for box in panel_boxes),
            )
            quality_targets = template_slots.get("quality_targets", {}) if isinstance(template_slots.get("quality_targets"), dict) else {}
            occupancy = _box_area(union) / max(1.0, width * height)
            occupancy_min = float(quality_targets.get("occupancy_min", 0.60))
            occupancy_max = float(quality_targets.get("occupancy_max", 0.85))
            if union[2] - union[0] < width * 0.82 or union[3] - union[1] < content_h * 0.58 or occupancy < occupancy_min:
                errors.append("主体占用不足：教学分镜 panel 未覆盖主要内容区域")
            if occupancy > occupancy_max + 0.08:
                errors.append("主体占用过满：教学分镜缺少必要留白")

            min_gap = float(quality_targets.get("min_module_gap", min(width, height) * 0.03))
            for index, box in enumerate(panel_boxes):
                for other in panel_boxes[index + 1:]:
                    same_row_overlap = min(box[3], other[3]) - max(box[1], other[1])
                    same_col_overlap = min(box[2], other[2]) - max(box[0], other[0])
                    if same_row_overlap > 4:
                        horizontal_gap = max(other[0] - box[2], box[0] - other[2])
                        if 0 <= horizontal_gap < min_gap:
                            errors.append("模块间距不足：同一行 panel 之间缺少呼吸感")
                    if same_col_overlap > 4:
                        vertical_gap = max(other[1] - box[3], box[1] - other[3])
                        if 0 <= vertical_gap < min_gap:
                            errors.append("模块间距不足：上下 panel 之间缺少呼吸感")

        rows = [item for item in template_slots.get("rows", []) if isinstance(item, dict)]
        panels = [item for item in template_slots.get("panels", []) if isinstance(item, dict)]
        panel_by_id = {str(item.get("stage_id")): item for item in panels}
        for row in rows:
            stage_ids = [str(item) for item in row.get("stage_ids", [])]
            row_panels = [
                panel_by_id[stage_id]
                for stage_id in stage_ids
                if stage_id in panel_by_id and isinstance(panel_by_id[stage_id].get("panel_rect"), dict)
            ]
            if len(row_panels) != len(stage_ids):
                errors.append(f"分镜行 {row.get('row_index')} 的 stage_ids 与 panels 不一致")
                continue
            if not row_panels:
                continue
            y_values = [float(panel["panel_rect"].get("y", 0)) for panel in row_panels]
            h_values = [float(panel["panel_rect"].get("h", 0)) for panel in row_panels]
            if max(y_values) - min(y_values) > 3 or max(h_values) - min(h_values) > 4:
                errors.append(f"分镜行 {row.get('row_index')} 内 panel 未对齐")
            if int(row.get("columns", len(stage_ids))) != len(stage_ids):
                errors.append(f"分镜行 {row.get('row_index')} 的 columns 与 stage 数不一致")

    if diagram_type == "flow":
        path = template_slots.get("path") if isinstance(template_slots.get("path"), dict) else {}
        points = [item for item in path.get("points", []) if isinstance(item, dict)]
        if len(points) >= 5:
            gaps = [
                abs(float(points[index + 1].get("x", 0)) - float(points[index].get("x", 0)))
                for index in range(len(points) - 1)
            ]
            if gaps and max(gaps) > 230:
                errors.append("流程布局过松：相邻阶段中心距过大，箭头会显得过长")
        for stage in structure.get("stages", []):
            if not isinstance(stage, dict):
                continue
            geometry = stage.get("node_geometry") if isinstance(stage.get("node_geometry"), dict) else {}
            if stage.get("node_shape") == "circle-icon" and min(float(geometry.get("w", 0)), float(geometry.get("h", 0))) < 90:
                errors.append(f"流程节点过小：阶段 {stage.get('id')} 的圆形节点不足以承载清晰图标")

    if diagram_type == "compare":
        chart = template_slots.get("chart") if isinstance(template_slots.get("chart"), dict) else {}
        item_count = max(1, len([stage for stage in structure.get("stages", []) if isinstance(stage, dict)]))
        chart_w = float(chart.get("w", 0) or 0)
        if chart_w > min(width * 0.72, max(420.0, item_count * 240.0)):
            errors.append("对比图过宽：柱子间距会被拉长，图表应更紧凑居中")
        chart_box = _slot_box(chart) if chart else None
        protected_slots: list[tuple[object, str, tuple[float, float, float, float]]] = []
        template_items = template_slots.get("items") if isinstance(template_slots.get("items"), list) else []
        for item in template_items:
            if not isinstance(item, dict):
                continue
            for role in ("bar_slot", "metric_slot"):
                slot = item.get(role)
                if isinstance(slot, dict):
                    protected_slots.append((item.get("stage_id"), role, _expanded_slot_box(slot, 4)))
        for connection in structure.get("connections", []):
            if not isinstance(connection, dict) or not connection.get("label"):
                continue
            label_slot = connection.get("label_slot")
            if not isinstance(label_slot, dict):
                errors.append(f"对比关系 {connection.get('from')}→{connection.get('to')} 缺少独立标签槽")
                continue
            label_box = _slot_box(label_slot)
            if chart_box and label_box[1] < chart_box[1] + 6:
                errors.append(f"对比关系 {connection.get('from')}→{connection.get('to')} 标签槽离图表顶部过近，应贴近倍数箭头")
            if chart and label_box[3] > float(chart.get("baseline", chart_box[3] if chart_box else height)) - 8:
                errors.append(f"对比关系 {connection.get('from')}→{connection.get('to')} 标签槽过低，容易遮挡柱子或图标")
            for stage_id, role, protected_box in protected_slots:
                if _boxes_overlap(label_box, protected_box, tolerance=0):
                    errors.append(
                        f"对比关系 {connection.get('from')}→{connection.get('to')} 标签槽遮挡阶段 {stage_id} 的 {role}"
                    )
                    break

    stage_slots: list[tuple[object, str, tuple[float, float, float, float]]] = []
    for stage in structure.get("stages", []):
        if not isinstance(stage, dict):
            continue
        for role, slot in (stage.get("slots") or {}).items():
            if isinstance(slot, dict) and role in {"icon", "title", "description", "metric"}:
                stage_slots.append((stage.get("id"), role, _slot_box(slot)))
    for index, (stage_id, role, box) in enumerate(stage_slots):
        for other_stage, other_role, other_box in stage_slots[index + 1:]:
            if _boxes_overlap(box, other_box):
                errors.append(f"布局槽位重叠：阶段 {stage_id} 的 {role} 与阶段 {other_stage} 的 {other_role}")
    errors.extend(_check_connection_path_zones(template_slots))
    errors.extend(check_connection_label_layout(structure))
    return errors


def check_icon_legibility(
    svg_code: str,
    stages: list[dict],
    root: Element | None = None,
) -> list[str]:
    """检查图标存在且尺寸足够，避免 fallback 图标小到无法辨识。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    stage_groups = _stage_node_groups(root)
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id"))
        has_brand_visual = any(
            isinstance(visual, dict) and str(visual.get("kind", "")).lower() == "brand"
            for visual in stage.get("visuals", [])
        )
        # 分镜中的通用视觉元素属于 scene，不需要再额外占一个独立图标位。
        slots = stage.get("slots") or {}
        needs_icon = (
            has_brand_visual
            or "icon" in slots
            or stage.get("node_shape") != "storyboard-panel" and bool(stage.get("visuals")) and "scene" not in slots
        )
        if not needs_icon:
            continue
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        icon_groups = [
            elem
            for elem in groups[0].iter()
            if elem.tag.split("}")[-1] == "g" and elem.get("data-role") == "icon"
        ]
        if not icon_groups:
            errors.append(f"阶段 {stage_id} 有视觉语义但缺少 data-role='icon' 图标组")
            continue
        bbox = _icon_foreground_bbox(icon_groups[0])
        if bbox is None:
            errors.append(f"阶段 {stage_id} 的图标无法计算可见边界")
            continue
        slot = (stage.get("slots") or {}).get("icon")
        if isinstance(slot, dict):
            slot_min = min(float(slot.get("w", 0)), float(slot.get("h", 0)))
            if stage.get("node_shape") == "storyboard-panel":
                required = max(36.0, min(64.0, slot_min * 0.60))
            else:
                required = max(20.0, min(56.0, slot_min * 0.45))
        else:
            required = 28.0
        if bbox[2] - bbox[0] < required or bbox[3] - bbox[1] < required:
            errors.append(f"阶段 {stage_id} 的图标过小：可见边界 {bbox}，至少需要约 {required:.0f}px")
    return errors


def check_icon_occlusion(
    svg_code: str,
    stages: list[dict],
    root: Element | None = None,
) -> list[str]:
    """检查图标是否被同一 stage 内后绘制的不透明背景遮住。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    stage_groups = _stage_node_groups(root)
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        stage_group = groups[0]
        rendered_items = _iter_transformed_elements(stage_group)
        render_order = {id(elem): index for index, (elem, _) in enumerate(rendered_items)}
        icon_groups = [
            elem
            for elem in stage_group.iter()
            if elem.tag.split("}")[-1] == "g" and elem.get("data-role") == "icon"
        ]
        for icon_group in icon_groups:
            icon_bbox = _icon_foreground_bbox_from_transformed_items(icon_group, rendered_items)
            if icon_bbox is None:
                continue
            icon_area = _box_area(icon_bbox)
            if icon_area <= 0:
                continue
            icon_ids = {id(elem) for elem in icon_group.iter()}
            start = render_order.get(id(icon_group))
            if start is None:
                continue
            for elem, bbox in rendered_items[start + 1:]:
                if id(elem) in icon_ids or bbox is None:
                    continue
                if not _element_has_opaque_fill(elem):
                    continue
                overlap_area = _intersection_area(icon_bbox, bbox)
                if overlap_area / icon_area < 0.25:
                    continue
                tag = elem.tag.split("}")[-1]
                errors.append(
                    f"阶段 {stage_id} 的图标被后绘制的 <{tag}> 遮挡："
                    f"图标边界 {icon_bbox}，遮挡元素边界 {bbox}。"
                    "请先画节点背景，再画 data-role='icon' 图标组。"
                )
                break
    return errors


def _icon_foreground_bbox_from_transformed_items(
    icon_group: Element,
    rendered_items: list[tuple[Element, tuple[float, float, float, float] | None]],
) -> tuple[float, float, float, float] | None:
    icon_ids = {id(elem) for elem in icon_group.iter()}
    use_boxes = [
        bbox
        for elem, bbox in rendered_items
        if id(elem) in icon_ids
        and elem is not icon_group
        and elem.tag.split("}")[-1] == "use"
        and bbox is not None
    ]
    if use_boxes:
        return _union_boxes(use_boxes)

    drawable_tags = {"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"}
    drawable_boxes = [
        bbox
        for elem, bbox in rendered_items
        if id(elem) in icon_ids
        and elem is not icon_group
        and elem.tag.split("}")[-1] in drawable_tags
        and bbox is not None
    ]
    if len(drawable_boxes) > 1:
        foreground_boxes: list[tuple[float, float, float, float]] = []
        for index, box in enumerate(drawable_boxes):
            other_union = _union_boxes(
                [other for other_index, other in enumerate(drawable_boxes) if other_index != index]
            )
            if (
                other_union is not None
                and _box_contains(box, other_union, tolerance=3.0)
                and _box_area(box) >= _box_area(other_union) * 1.35
            ):
                continue
            foreground_boxes.append(box)
        if foreground_boxes:
            drawable_boxes = foreground_boxes
    return _union_boxes(drawable_boxes)


def _element_has_opaque_fill(elem: Element) -> bool:
    tag = elem.tag.split("}")[-1]
    if tag in _NON_RENDERED_CONTAINER_TAGS or tag not in {"rect", "circle", "ellipse", "path", "polygon", "polyline"}:
        return False
    raw_fill = _style_color(elem, "fill").strip().lower()
    if raw_fill in {"none", "transparent"}:
        return False
    for attribute in ("opacity", "fill-opacity"):
        try:
            if float(elem.get(attribute, "1")) < 0.55:
                return False
        except ValueError:
            continue
    return True


def _icon_foreground_bbox(icon_group: Element) -> tuple[float, float, float, float] | None:
    """优先计算真实图标主体，避免背景底托把小图标伪装成合格。"""
    use_boxes = [
        bbox
        for elem in icon_group.iter()
        if elem is not icon_group and elem.tag.split("}")[-1] == "use"
        for bbox in [_bbox_of_element(elem)]
        if bbox is not None
    ]
    if use_boxes:
        return (
            min(box[0] for box in use_boxes),
            min(box[1] for box in use_boxes),
            max(box[2] for box in use_boxes),
            max(box[3] for box in use_boxes),
        )

    drawable_tags = {"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"}
    drawable_boxes = [
        bbox
        for elem in icon_group.iter()
        if elem is not icon_group and elem.tag.split("}")[-1] in drawable_tags
        for bbox in [_bbox_of_element(elem)]
        if bbox is not None
    ]
    if len(drawable_boxes) > 1:
        foreground_boxes: list[tuple[float, float, float, float]] = []
        for index, box in enumerate(drawable_boxes):
            other_union = _union_boxes(
                [other for other_index, other in enumerate(drawable_boxes) if other_index != index]
            )
            if (
                other_union is not None
                and _box_contains(box, other_union, tolerance=3.0)
                and _box_area(box) >= _box_area(other_union) * 1.35
            ):
                continue
            foreground_boxes.append(box)
        if foreground_boxes:
            drawable_boxes = foreground_boxes
    if drawable_boxes:
        return (
            min(box[0] for box in drawable_boxes),
            min(box[1] for box in drawable_boxes),
            max(box[2] for box in drawable_boxes),
            max(box[3] for box in drawable_boxes),
        )
    return _bbox_of_group(icon_group)


def _style_color(elem: Element, attribute: str) -> str:
    style = str(elem.get("style", ""))
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        if name.strip().lower() == attribute:
            return value.strip()
    return str(elem.get(attribute, "")).strip()


def _parse_color(raw: str) -> tuple[int, int, int] | None:
    value = raw.strip().lower()
    if not value or value in {"none", "transparent", "currentcolor"} or value.startswith("url("):
        return None
    named = {
        "black": "#000000",
        "white": "#ffffff",
        "red": "#ff0000",
        "green": "#008000",
        "blue": "#0000ff",
        "gray": "#808080",
        "grey": "#808080",
    }
    value = named.get(value, value)
    if value.startswith("#"):
        hex_value = value[1:]
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        if len(hex_value) == 6:
            try:
                return tuple(int(hex_value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
            except ValueError:
                return None
    return None


def _relative_luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for value in color:
        normalized = value / 255.0
        channels.append(normalized / 12.92 if normalized <= 0.03928 else ((normalized + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(first)
    l2 = _relative_luminance(second)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def check_style_quality(style: dict) -> list[str]:
    """检查样式 token 是否能支撑鲜明柔和且清晰的教学图。"""
    if not isinstance(style, dict):
        return ["样式结果不是 JSON 对象"]
    errors: list[str] = []
    section_tokens = style.get("section_tokens")
    if not isinstance(section_tokens, list) or len(section_tokens) < 3:
        errors.append("样式缺少至少 3 个 section_tokens，无法区分主要模块")
        return errors

    fills: list[tuple[int, int, int]] = []
    strokes: list[tuple[int, int, int]] = []
    for index, token in enumerate(section_tokens):
        if not isinstance(token, dict):
            errors.append(f"section_tokens[{index}] 不是对象")
            continue
        fill = _parse_color(str(token.get("fill", "")))
        stroke = _parse_color(str(token.get("stroke", token.get("accent", ""))))
        if fill is None or stroke is None:
            errors.append(f"section_tokens[{index}] 缺少可解析的 fill/stroke")
            continue
        fills.append(fill)
        strokes.append(stroke)
        if _contrast_ratio(stroke, fill) < 1.8:
            errors.append(f"section_tokens[{index}] 的描边与底色区分度不足")

    unique_fills = {fill for fill in fills}
    if len(unique_fills) < min(3, len(fills)):
        errors.append("section_tokens 的 fill 颜色过少，容易变成单一色块")

    title_color = _parse_color(str((style.get("text_colors") or {}).get("title", "")))
    body_color = _parse_color(str((style.get("text_colors") or {}).get("body", "")))
    for fill in fills[:4]:
        if title_color and _contrast_ratio(title_color, fill) < 4.0:
            errors.append("标题颜色与 section 浅底色对比度不足")
            break
        if body_color and _contrast_ratio(body_color, fill) < 3.0:
            errors.append("正文颜色与 section 浅底色对比度不足")
            break

    for key in ("accent_tokens", "arrow_tokens", "typography_scale", "spacing_scale"):
        if not isinstance(style.get(key), dict):
            errors.append(f"样式缺少 {key} token")
    return errors


def _element_colors(elem: Element) -> list[tuple[int, int, int]]:
    colors: list[tuple[int, int, int]] = []
    for attribute in ("fill", "stroke"):
        color = _parse_color(_style_color(elem, attribute))
        if color is not None:
            colors.append(color)
    return colors


def check_icon_contrast(
    svg_code: str,
    stages: list[dict],
    root: Element | None = None,
) -> list[str]:
    """检查图标前景与所在背景的对比度，避免融入背景。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    stage_groups = _stage_node_groups(root)
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id"))
        groups = stage_groups.get(stage_id, [])
        if not groups:
            continue
        icon_groups = [
            elem
            for elem in groups[0].iter()
            if elem.tag.split("}")[-1] == "g" and elem.get("data-role") == "icon"
        ]
        if not icon_groups:
            continue
        icon_group = icon_groups[0]
        icon_bbox = _bbox_of_group(icon_group)
        if icon_bbox is None:
            continue
        icon_ids = {id(elem) for elem in icon_group.iter()}
        icon_colors = [
            color
            for elem in icon_group.iter()
            for color in _element_colors(elem)
        ]
        if not icon_colors:
            continue

        center_x = (icon_bbox[0] + icon_bbox[2]) / 2
        center_y = (icon_bbox[1] + icon_bbox[3]) / 2
        candidates: list[tuple[float, tuple[int, int, int]]] = []
        for elem in groups[0].iter():
            if id(elem) in icon_ids:
                continue
            bbox = _bbox_of_element(elem)
            if bbox is None:
                continue
            if not (bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]):
                continue
            area = _box_area(bbox)
            if area < _box_area(icon_bbox) * 0.8:
                continue
            for color in _element_colors(elem):
                candidates.append((area, color))
        if not candidates:
            candidates.append((1.0, (255, 255, 255)))
        background = min(candidates, key=lambda item: item[0])[1]
        best = max(_contrast_ratio(color, background) for color in icon_colors)
        if best < 2.2:
            errors.append(f"阶段 {stage_id} 的图标与背景对比度不足，容易融入背景")
    return errors


def _infer_connection_label_bbox(group: Element) -> tuple[float, float, float, float] | None:
    """兼容旧 SVG：标签存在但没有 data-role='connection-label' 包裹时推断其边界。"""
    label_boxes: list[tuple[float, float, float, float]] = []
    label_tags = {"rect", "text"}
    for elem in group.iter():
        if elem is group:
            continue
        tag = elem.tag.split("}")[-1]
        if tag not in label_tags:
            continue
        bbox = _bbox_of_element(elem)
        if bbox is not None:
            label_boxes.append(bbox)
    return _union_boxes(label_boxes)


def check_element_overlaps(
    svg_code: str,
    stages: list[dict],
    connections: list[dict],
    header: dict | None = None,
    root: Element | None = None,
) -> list[str]:
    """检查节点前景槽位、前景元素碰撞及连接标签遮挡。"""
    if not svg_code.strip():
        return []
    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError:
        return []
    if root is None:
        return []

    errors: list[str] = []
    expected_stages = {str(stage.get("id")): stage for stage in stages}
    stage_groups: dict[str, list[Element]] = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "g":
            continue
        if "stage-node" not in str(elem.get("class", "")).split():
            continue
        stage_id = str(elem.get("data-stage-id", ""))
        stage_groups.setdefault(stage_id, []).append(elem)

    role_boxes: list[tuple[str, str, tuple[float, float, float, float]]] = []
    allowed_roles = {"icon", "title", "description", "metric", "scene", "comparison-bar"}
    for stage_id, stage in expected_stages.items():
        groups = stage_groups.get(stage_id, [])
        if not groups:
            errors.append(f"阶段 {stage_id} 缺少 <g class='stage-node' data-stage-id='{stage_id}'>")
            continue
        if len(groups) > 1:
            errors.append(f"阶段 {stage_id} 存在 {len(groups)} 个重复 stage-node group")
        stage_group = groups[0]
        seen_roles: set[str] = set()
        for elem in stage_group.iter():
            role = str(elem.get("data-role", "")).strip()
            if not role:
                continue
            if elem.tag.split("}")[-1] != "g" and role not in {"comparison-bar"}:
                errors.append(f"阶段 {stage_id} 的 data-role={role!r} 必须标记在 <g> 上")
                continue
            if role not in allowed_roles:
                errors.append(f"阶段 {stage_id} 使用了未知 data-role={role!r}")
                continue
            if role in seen_roles:
                errors.append(f"阶段 {stage_id} 的 data-role={role!r} 重复")
                continue
            seen_roles.add(role)
            bbox = _bbox_of_group(elem)  # 已包含 transform 转换，返回全局坐标
            if bbox is None:
                payload_keys = {
                    "icon": "visuals",
                    "description": "description",
                    "metric": "metric",
                }
                payload_key = payload_keys.get(role)
                if payload_key and not stage.get(payload_key):
                    continue
                errors.append(f"阶段 {stage_id} 的 {role} 组无法计算可见边界")
                continue
            slot = (stage.get("slots") or {}).get(role)
            if isinstance(slot, dict):
                inside_actual_stage_container = False
                if role == "icon":
                    container_bbox = _stage_container_bbox(stage_group)
                    inside_actual_stage_container = (
                        container_bbox is not None and _box_contains(container_bbox, bbox, tolerance=8.0)
                    )
                if (
                    _outside_slot(bbox, slot)
                    and not (role == "icon" and (_inside_stage_box(bbox, stage) or inside_actual_stage_container))
                ):
                    errors.append(
                        f"阶段 {stage_id} 的 {role} 元素越出槽位：元素边界 {bbox}，槽位 {slot}"
                    )
            role_boxes.append((stage_id, role, bbox))

    # _bbox_of_group 已将 transform 坐标转为全局，可以直接比较
    for index, (stage_id, role, bbox) in enumerate(role_boxes):
        for other_stage, other_role, other_bbox in role_boxes[index + 1:]:
            if _boxes_overlap(bbox, other_bbox):
                errors.append(
                    f"前景元素重叠：阶段 {stage_id} 的 {role} 与阶段 {other_stage} 的 {other_role}"
                )

    expected_connections = {
        (
            str(item.get("from")),
            str(item.get("to")),
            str(item.get("type", "arrow")),
        ): item
        for item in connections
        if isinstance(item, dict)
    }
    render_order = {id(elem): index for index, elem in enumerate(root.iter())}
    for group in root.iter():
        if group.tag.split("}")[-1] != "g":
            continue
        if "semantic-connection" not in str(group.get("class", "")).split():
            continue
        key = (
            str(group.get("data-from", "")),
            str(group.get("data-to", "")),
            str(group.get("data-type", "")),
        )
        connection = expected_connections.get(key)
        label_groups = [
            elem
            for elem in group.iter()
            if elem.tag.split("}")[-1] == "g"
            and elem.get("data-role") == "connection-label"
        ]
        # 即使连接不在规划数据中，只要 SVG 实际画出了标签，也必须检查其可见性。
        if not label_groups and not (connection and connection.get("label")):
            continue
        if len(label_groups) != 1:
            errors.append(
                f"连接 {key[0]}→{key[1]} 必须有且仅有一个 data-role='connection-label' group"
            )
            continue
        label_bbox = _bbox_of_group(label_groups[0])  # 已包含 transform 转换
        label_slot = connection.get("label_slot") if connection else None
        if label_bbox is None:
            errors.append(f"连接 {key[0]}→{key[1]} 的标签无法计算边界")
            continue
        if isinstance(label_slot, dict) and _outside_slot(label_bbox, label_slot):
            errors.append(
                f"连接 {key[0]}→{key[1]} 标签越出 label_slot：元素边界 {label_bbox}，槽位 {label_slot}"
            )
        for stage_id, role, bbox in role_boxes:
            if _boxes_overlap(label_bbox, bbox):
                errors.append(
                    f"连接 {key[0]}→{key[1]} 标签遮挡阶段 {stage_id} 的 {role}"
                )
        label_order = render_order.get(id(label_groups[0]), -1)
        for stage_id, groups in stage_groups.items():
            for stage_group in groups:
                if render_order.get(id(stage_group), -1) <= label_order:
                    continue
                container_bbox = _stage_container_bbox(stage_group)
                if container_bbox is not None and _boxes_overlap(label_bbox, container_bbox):
                    errors.append(
                        f"连接 {key[0]}→{key[1]} 标签被后绘制的阶段 {stage_id} 节点背景遮挡"
                    )
                    break

    header_groups = [
        elem
        for elem in root.iter()
        if elem.tag.split("}")[-1] == "g" and elem.get("data-role") == "header"
    ]
    if header and header_groups:
        header_bbox = _bbox_of_group(header_groups[0])
        if header_bbox is not None and _outside_slot(header_bbox, header):
            errors.append(f"总标题越出 header 槽位：元素边界 {header_bbox}，槽位 {header}")
    return errors


_NON_RENDERED_CONTAINER_TAGS = {
    "defs",
    "symbol",
    "marker",
    "clipPath",
    "mask",
    "pattern",
    "linearGradient",
    "radialGradient",
    "filter",
    "style",
    "script",
    "title",
    "desc",
}


def _iter_rendered_element_boxes(
    root: Element,
):
    """遍历可渲染元素并返回累计所有祖先 transform 后的全局边界。"""
    def visit(
        elem: Element,
        parent_matrix: tuple[float, float, float, float, float, float],
        hidden: bool,
    ):
        tag = elem.tag.split("}")[-1]
        current_hidden = hidden or (elem is not root and tag in _NON_RENDERED_CONTAINER_TAGS)
        matrix = _multiply_transform(parent_matrix, _parse_transform(str(elem.get("transform", ""))))
        if not current_hidden:
            bbox = _bbox_of_element(elem)
            yield elem, (_transform_box(bbox, matrix) if bbox is not None else None)
        for child in list(elem):
            yield from visit(child, matrix, current_hidden)

    yield from visit(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), False)


def check_bounds(
    svg_code: str,
    canvas_w: int,
    canvas_h: int,
    root: Element | None = None,
) -> list[str]:
    """检查 SVG 中可见元素是否超出画布边界。

    返回错误列表；空列表表示无溢出。
    """
    errors: list[str] = []
    if not svg_code.strip():
        errors.append("SVG 代码为空")
        return errors

    try:
        root = _resolve_root(svg_code, root)
    except ET.ParseError as e:
        errors.append(f"XML 解析错误，无法检查边界：{e}")
        return errors
    if root is None:
        errors.append("SVG 代码为空或无法解析")
        return errors

    for elem, bbox in _iter_rendered_element_boxes(root):
        if bbox is None:
            continue
        xmin, ymin, xmax, ymax = bbox
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if xmin < 0 or ymin < 0 or xmax > canvas_w or ymax > canvas_h:
            errors.append(
                f"<{tag}> 元素溢出画布：边界 ({xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f})，"
                f"画布尺寸 {canvas_w}×{canvas_h}"
            )
    return errors
