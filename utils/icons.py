"""把解析后的图标注入 SVG，并执行确定性的引用检查。"""

import html
import re
import xml.etree.ElementTree as StdET
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET


_SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def _is_symbol_icon(icon: dict) -> bool:
    """兼容旧调用方：未声明 render_mode 的既有图标仍是 symbol。"""
    return str(icon.get("render_mode", "symbol")) == "symbol"


def _requires_standalone_icon(icon: dict, stages: list[dict] | None) -> bool:
    """判断图标是否必须作为独立素材出现。

    mechanism-storyboard 的通用图形是场景素材而非角标；品牌与其他模板仍保持
    可验证的独立图标引用。没有布局上下文时保守地沿用旧行为。
    """
    if stages is None or str(icon.get("kind", "")).lower() != "general":
        return True
    stage_id = str(icon.get("stage_id"))
    stage = next(
        (item for item in stages if isinstance(item, dict) and str(item.get("id")) == stage_id),
        None,
    )
    if not isinstance(stage, dict):
        return True
    slots = stage.get("slots") or {}
    return "icon" in slots or "scene" not in slots


def _svg_tag(name: str, root: Element) -> str:
    """按输入 SVG 的命名空间创建元素，避免序列化时混用命名空间。"""
    if root.tag.startswith("{"):
        namespace = root.tag[1:].split("}", 1)[0]
        return f"{{{namespace}}}{name}"
    return name


def _valid_slot(slot: object) -> tuple[float, float, float, float] | None:
    """把布局槽位转换为可用于 <use> 的数值边界。"""
    if not isinstance(slot, dict):
        return None
    try:
        x = float(slot["x"])
        y = float(slot["y"])
        width = float(slot["w"])
        height = float(slot["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _format_number(value: float) -> str:
    """避免在输出 SVG 中留下无意义的 .0。"""
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _has_drawable_content(group: Element | None) -> bool:
    if group is None:
        return False
    drawable_tags = {"use", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text"}
    for elem in group.iter():
        if elem is group:
            continue
        if elem.tag.split("}")[-1] in drawable_tags:
            return True
    return False


def _stage_has_generated_icon(stage_node: Element) -> bool:
    return any(str(elem.get("data-generated-icon", "")).strip() for elem in stage_node.iter())


def _use_target(elem: Element) -> str:
    """返回本地 <use> 引用的 symbol id，外部引用返回空字符串。"""
    href = elem.get("href") or elem.get("{http://www.w3.org/1999/xlink}href") or ""
    return href[1:] if href.startswith("#") else ""


def _remove_symbol_uses(parent: Element, symbol_ids: set[str]) -> bool:
    """递归移除指定 symbol 的 <use>，包括未放入 data-role=icon 的旧引用。"""
    removed = False
    for child in list(parent):
        if child.tag.split("}")[-1] == "use" and _use_target(child) in symbol_ids:
            parent.remove(child)
            removed = True
            continue
        removed = _remove_symbol_uses(child, symbol_ids) or removed
    return removed


def _remove_generated_icon_aliases(parent: Element, aliases: set[str]) -> bool:
    """移除模型为已解析品牌另画的图形，避免与规范 symbol 叠印。"""
    removed = False
    aliases = {value.casefold() for value in aliases if value}
    for child in list(parent):
        mark = str(child.get("data-generated-icon", "")).strip().casefold()
        if mark and mark in aliases:
            parent.remove(child)
            removed = True
            continue
        removed = _remove_generated_icon_aliases(child, aliases) or removed
    return removed


def _scene_has_other_graphics(scene: Element, ignored_symbol_ids: set[str]) -> bool:
    """判断 scene 中是否已有可承担主画面的几何图形。"""
    graphic_tags = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "use"}
    for elem in scene.iter():
        if elem is scene or elem.tag.split("}")[-1] not in graphic_tags:
            continue
        if elem.tag.split("}")[-1] != "use" or _use_target(elem) not in ignored_symbol_ids:
            return True
    return False


def _is_small_scene_corner_use(elem: Element, scene: Element, stage: dict) -> bool:
    """识别 scene 四角的小型 symbol；主图或居中素材不在此处清理。"""
    try:
        x = float(elem.get("x", 0) or 0)
        y = float(elem.get("y", 0) or 0)
        width = float(elem.get("width", 0) or 0)
        height = float(elem.get("height", 0) or 0)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0 or width > 54 or height > 54:
        return False

    slot = (stage.get("slots") or {}).get("scene") or stage.get("scene_area")
    if not isinstance(slot, dict):
        return False
    try:
        scene_w = float(slot["w"])
        scene_h = float(slot["h"])
        slot_x = float(slot.get("x", 0) or 0)
        slot_y = float(slot.get("y", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return False
    if scene_w <= 0 or scene_h <= 0:
        return False

    if not str(scene.get("transform", "")).strip():
        x -= slot_x
        y -= slot_y
    cx = x + width / 2
    cy = y + height / 2
    x_edge = max(48.0, scene_w * 0.28)
    y_edge = max(32.0, scene_h * 0.22)
    return (
        (cx <= x_edge or cx >= scene_w - x_edge)
        and (cy <= y_edge or cy >= scene_h - y_edge)
    )


def _remove_storyboard_general_badges(
    root: Element,
    icons: list[dict],
    stages_by_id: dict[str, dict],
) -> bool:
    """已有主场景时，确定性移除模型在 storyboard 四角追加的通用 symbol。"""
    general_ids_by_stage: dict[str, set[str]] = {}
    for icon in icons:
        stage_id = str(icon.get("stage_id"))
        stage = stages_by_id.get(stage_id) or {}
        if (
            icon.get("id")
            and _is_symbol_icon(icon)
            and str(icon.get("kind", "")).lower() == "general"
            and stage.get("node_shape") == "storyboard-panel"
        ):
            general_ids_by_stage.setdefault(stage_id, set()).add(str(icon["id"]))

    removed = False
    for stage_node in root.iter():
        stage_id = str(stage_node.get("data-stage-id", ""))
        if (
            stage_node.tag.split("}")[-1] != "g"
            or "stage-node" not in str(stage_node.get("class", "")).split()
            or stage_id not in general_ids_by_stage
        ):
            continue
        symbol_ids = general_ids_by_stage[stage_id]
        stage = stages_by_id[stage_id]
        for scene in stage_node.iter():
            classes = str(scene.get("class", "")).split()
            if (
                scene.tag.split("}")[-1] != "g"
                or (scene.get("data-role") != "scene" and "storyboard-scene" not in classes)
                or not _scene_has_other_graphics(scene, symbol_ids)
            ):
                continue
            for parent in scene.iter():
                for child in list(parent):
                    if (
                        child.tag.split("}")[-1] == "use"
                        and _use_target(child) in symbol_ids
                        and _is_small_scene_corner_use(child, scene, stage)
                    ):
                        parent.remove(child)
                        removed = True
    return removed


def enforce_resolved_icons(
    svg_code: str,
    icons: list[dict],
    stages: list[dict],
) -> str:
    """把已解析的 symbol 图标确定性回填到各自 stage 的 icon 槽位。

    品牌 Logo 不能在重试时被重新解释，因此保持其独立图标槽位并确定性回填。
    mechanism-storyboard 中的通用图形则由场景自由组合，不能再被后处理强行塞入
    面板角标。通用 LLM 回退图标和品牌文字徽章都没有 SVG symbol。
    """
    if not svg_code.strip() or not icons:
        return svg_code

    stages_by_id = {
        str(stage.get("id")): stage
        for stage in stages
        if isinstance(stage, dict) and stage.get("id") is not None
    }
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError:
        return svg_code
    if _remove_storyboard_general_badges(root, icons, stages_by_id):
        StdET.register_namespace("", _SVG_NAMESPACE)
        svg_code = StdET.tostring(root, encoding="unicode")

    symbol_icons = [
        icon
        for icon in icons
        if icon.get("id")
        and _is_symbol_icon(icon)
        and not (
            str(icon.get("kind", "")).lower() == "general"
            and (stages_by_id.get(str(icon.get("stage_id"))) or {}).get("node_shape") == "storyboard-panel"
        )
    ]
    if not symbol_icons:
        return svg_code
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError:
        # 语法错误交给 Validator 报告，不能在这里猜测性修复原始文本。
        return svg_code
    icons_by_stage: dict[str, list[dict]] = {}
    for icon in symbol_icons:
        stage_id = str(icon.get("stage_id"))
        icons_by_stage.setdefault(stage_id, []).append(icon)
    for stage_icons in icons_by_stage.values():
        stage_icons.sort(key=lambda icon: int(icon.get("visual_index", 0) or 0))

    repaired = False
    for stage_node in root.iter():
        tag = stage_node.tag.split("}")[-1]
        classes = str(stage_node.get("class", "")).split()
        stage_id = str(stage_node.get("data-stage-id", ""))
        if tag != "g" or "stage-node" not in classes or stage_id not in icons_by_stage:
            continue
        slot = _valid_slot((stages_by_id.get(stage_id) or {}).get("slots", {}).get("icon"))
        if slot is None:
            continue
        stage_icons = icons_by_stage[stage_id]
        brand_icons = [
            icon
            for icon in stage_icons
            if str(icon.get("kind", "")).lower() == "brand"
        ]
        brand_ids = {str(icon["id"]) for icon in brand_icons}
        brand_aliases = {
            str(icon.get(key, "")).strip().casefold()
            for icon in brand_icons
            for key in ("id", "query", "icon_query", "name", "label")
            if str(icon.get(key, "")).strip()
        }
        if brand_ids:
            repaired = _remove_symbol_uses(stage_node, brand_ids) or repaired
            repaired = _remove_generated_icon_aliases(stage_node, brand_aliases) or repaired

        # 品牌自绘组可能就是旧 icon_group；清理后重新查找，避免操作已脱离树的节点。
        icon_group = next(
            (
                child
                for child in stage_node.iter()
                if child is not stage_node
                and child.tag.split("}")[-1] == "g"
                and child.get("data-role") == "icon"
            ),
            None,
        )
        if icon_group is None:
            icon_group = StdET.Element(_svg_tag("g", root), {"data-role": "icon"})
            stage_node.append(icon_group)

        if (
            all(str(icon.get("kind", "")).lower() == "general" for icon in stage_icons)
            and (_has_drawable_content(icon_group) or _stage_has_generated_icon(stage_node))
        ):
            # 通用 symbol 只是参考素材；已有自绘图形时保留模型更贴切的场景表达。
            continue

        # 既有 transform 是模型为旧图标设计的局部坐标；重置后使用布局的绝对槽位。
        icon_group.attrib.pop("transform", None)
        icon_group.attrib.pop("data-generated-icon", None)
        for child in list(icon_group):
            icon_group.remove(child)

        x, y, width, height = slot
        count = len(stage_icons)
        gap = min(12.0, max(4.0, width * 0.08)) if count > 1 else 0.0
        icon_size = min(height, (width - gap * (count - 1)) / count)
        if icon_size <= 0:
            continue
        total_width = icon_size * count + gap * (count - 1)
        start_x = x + (width - total_width) / 2
        icon_y = y + (height - icon_size) / 2
        for index, icon in enumerate(stage_icons):
            use = StdET.Element(
                _svg_tag("use", root),
                {
                    "href": f"#{icon['id']}",
                    "x": _format_number(start_x + index * (icon_size + gap)),
                    "y": _format_number(icon_y),
                    "width": _format_number(icon_size),
                    "height": _format_number(icon_size),
                },
            )
            icon_group.append(use)
        repaired = True

    if not repaired:
        return svg_code
    # 保持常见 SVG 写法，避免生成 ns0 前缀影响后续模型重试时的可读性。
    StdET.register_namespace("", _SVG_NAMESPACE)
    return StdET.tostring(root, encoding="unicode")


def _theme_color(icon: dict, style: dict | None, index: int) -> str:
    """为仍使用 currentColor 的单色图标确定一个稳定颜色。"""
    official = str(icon.get("color", "")).strip()
    if official:
        return official
    palette = (style or {}).get("palette", [])
    colors = [str(value) for value in palette if str(value).strip()] if isinstance(palette, list) else []
    if colors:
        stage_id = icon.get("stage_id", index)
        position = stage_id if isinstance(stage_id, int) else index
        return colors[position % len(colors)]
    return "#475569"


def _remove_fake_icon_defs(
    svg_code: str,
    icon_ids_to_inject: set[str],
) -> str:
    """移除 defs 中 LLM 手绘的假图标元素（非 symbol），避免阻塞真实图标注入。

    LLM 可能创建 <g id="icon-s1-v0"> 等元素来让 <use> 引用生效，
    但这些手绘图形不是真实品牌 Logo，必须清除后注入真实 symbol。
    """
    if not icon_ids_to_inject:
        return svg_code
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError:
        return svg_code

    removed = False
    for defs_elem in root.iter():
        if defs_elem.tag.split("}")[-1] != "defs":
            continue
        for child in list(defs_elem):
            child_id = str(child.get("id", "")).strip()
            if child_id in icon_ids_to_inject and child.tag.split("}")[-1] != "symbol":
                defs_elem.remove(child)
                removed = True
        break  # 只处理第一个 defs

    if not removed:
        return svg_code
    StdET.register_namespace("", _SVG_NAMESPACE)
    return StdET.tostring(root, encoding="unicode")


def embed_icon_symbols(
    svg_code: str,
    icons: list[dict],
    style: dict | None = None,
) -> str:
    """将真实图标注入 <defs>，并确定性固化单色图标颜色。"""
    if not svg_code.strip() or not icons:
        return svg_code

    # 1. 找出所有待注入的真实 symbol 图标 id
    pending_ids: set[str] = set()
    for icon in icons:
        if not _is_symbol_icon(icon):
            continue
        icon_id = str(icon.get("id", ""))
        body = str(icon.get("body", ""))
        if icon_id and body:
            pending_ids.add(icon_id)

    # 2. 移除 defs 中 LLM 手绘的假图标（<g id="icon-*"> 等非 symbol 元素）
    if pending_ids:
        svg_code = _remove_fake_icon_defs(svg_code, pending_ids)

    existing_ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', svg_code))
    symbols: list[str] = []
    for index, icon in enumerate(icons):
        if not _is_symbol_icon(icon):
            continue
        icon_id = str(icon.get("id", ""))
        body = str(icon.get("body", ""))
        viewbox = str(icon.get("viewBox", "0 0 24 24"))
        if not icon_id or not body or icon_id in existing_ids:
            continue
        if str(icon.get("palette_mode", "monochrome")) == "monochrome":
            body = re.sub(
                r"currentColor",
                _theme_color(icon, style, index),
                body,
                flags=re.IGNORECASE,
            )
        symbols.append(
            f'<symbol id="{html.escape(icon_id, quote=True)}" '
            f'viewBox="{html.escape(viewbox, quote=True)}">{body}</symbol>'
        )
        existing_ids.add(icon_id)
    if not symbols:
        return svg_code
    payload = "\n".join(symbols)
    defs_match = re.search(r"<defs\b[^>]*>", svg_code, re.IGNORECASE)
    if defs_match:
        position = defs_match.end()
        return svg_code[:position] + "\n" + payload + svg_code[position:]
    svg_match = re.search(r"<svg\b[^>]*>", svg_code, re.IGNORECASE)
    if not svg_match:
        return svg_code
    position = svg_match.end()
    return svg_code[:position] + f"\n<defs>\n{payload}\n</defs>" + svg_code[position:]


def check_icon_references(
    svg_code: str,
    icons: list[dict],
    root: Element | None = None,
    stages: list[dict] | None = None,
) -> list[str]:
    """检查 symbol/use 引用、重复 id 和外部资源。

    可传入已解析的 root 避免重复解析 SVG（见 agents/validator.py 的缓存用法）。
    """
    if not svg_code.strip():
        return []
    if root is None:
        try:
            root = ET.fromstring(svg_code)
        except ET.ParseError:
            return []

    errors: list[str] = []
    id_counts: dict[str, int] = {}
    for elem in root.iter():
        elem_id = elem.get("id")
        if elem_id:
            id_counts[elem_id] = id_counts.get(elem_id, 0) + 1
    duplicates = sorted(key for key, count in id_counts.items() if count > 1)
    if duplicates:
        errors.append(f"SVG 存在重复 id：{', '.join(duplicates[:5])}")

    known_ids = set(id_counts)
    stage_nodes = {
        str(elem.get("data-stage-id", "")): elem
        for elem in root.iter()
        if elem.tag.split("}")[-1] == "g"
        and "stage-node" in str(elem.get("class", "")).split()
        and str(elem.get("data-stage-id", "")).strip()
    }
    def _general_symbol_satisfied(icon: dict) -> bool:
        if str(icon.get("kind", "")).lower() != "general" or not _is_symbol_icon(icon):
            return False
        stage_node = stage_nodes.get(str(icon.get("stage_id")))
        if stage_node is None:
            return False
        icon_group = next(
            (
                elem
                for elem in stage_node.iter()
                if elem is not stage_node
                and elem.tag.split("}")[-1] == "g"
                and elem.get("data-role") == "icon"
            ),
            None,
        )
        return _has_drawable_content(icon_group) or _stage_has_generated_icon(stage_node)

    resolved_ids = {
        str(icon.get("id"))
        for icon in icons
        if icon.get("id")
        and _is_symbol_icon(icon)
        and _requires_standalone_icon(icon, stages)
        and not _general_symbol_satisfied(icon)
    }
    generated_expected = {
        str(icon.get("id"))
        for icon in icons
        if icon.get("id")
        and icon.get("render_mode") == "llm_general"
        and _requires_standalone_icon(icon, stages)
    }
    known_generated = {
        str(icon.get("id"))
        for icon in icons
        if icon.get("id") and icon.get("render_mode") == "llm_general"
    }
    forbidden_generated = {
        str(icon.get("id"))
        for icon in icons
        if icon.get("id") and icon.get("render_mode") == "brand_badge"
    }
    badges_expected = {
        str(icon.get("id"))
        for icon in icons
        if icon.get("id") and icon.get("render_mode") == "brand_badge"
    }
    used_resolved: set[str] = set()
    used_resolved_by_stage: dict[str, set[str]] = {}
    resolved_use_counts_by_stage: dict[str, dict[str, int]] = {}
    brand_aliases_by_stage: dict[str, list[tuple[str, set[str]]]] = {}
    for icon in icons:
        if str(icon.get("kind", "")).lower() != "brand" or not icon.get("id"):
            continue
        aliases = {
            str(icon.get(key, "")).strip().casefold()
            for key in ("id", "query", "icon_query", "name", "label")
            if str(icon.get(key, "")).strip()
        }
        brand_aliases_by_stage.setdefault(str(icon.get("stage_id")), []).append(
            (str(icon["id"]), aliases)
        )
    generated_actual: set[str] = set()
    badges_actual: set[str] = set()
    labelled_badges: set[str] = set()
    for elem in root.iter():
        generated_id = str(elem.get("data-generated-icon", "")).strip()
        if generated_id:
            generated_actual.add(generated_id)
        badge_id = str(elem.get("data-brand-badge", "")).strip()
        if badge_id:
            badges_actual.add(badge_id)
            if any(
                str(text_elem.text or "").strip()
                for text_elem in elem.iter()
                if text_elem.tag.split("}")[-1] == "text"
            ):
                labelled_badges.add(badge_id)
        tag = elem.tag.split("}")[-1]
        if tag != "use":
            continue
        href = elem.get("href") or elem.get("{http://www.w3.org/1999/xlink}href") or ""
        if not href.startswith("#"):
            errors.append(f"<use> 使用了外部或非法 href：{href or '(空)'}")
            continue
        target = href[1:]
        if target not in known_ids:
            errors.append(f"<use> 引用了不存在的 symbol：{target}")
        if target in resolved_ids:
            used_resolved.add(target)

    # 图标不仅要出现在画布上，还必须留在 IconResolver 对应的 stage 内。否则
    # 重试时交换两个品牌图标也会通过旧的“全局出现过”校验。
    for stage_node in root.iter():
        tag = stage_node.tag.split("}")[-1]
        classes = str(stage_node.get("class", "")).split()
        stage_id = str(stage_node.get("data-stage-id", ""))
        if tag != "g" or "stage-node" not in classes or not stage_id:
            continue
        for icon_id, aliases in brand_aliases_by_stage.get(stage_id, []):
            improvised = {
                str(elem.get("data-generated-icon", "")).strip()
                for elem in stage_node.iter()
                if str(elem.get("data-generated-icon", "")).strip().casefold() in aliases
            }
            if improvised:
                errors.append(
                    f"品牌图标 {icon_id} 在阶段 {stage_id} 同时存在自绘图形 "
                    f"{', '.join(sorted(improvised))}；必须只保留解析后的官方图标"
                )
        stage_counts: dict[str, int] = {}
        for icon_elem in stage_node.iter():
            if icon_elem.tag.split("}")[-1] != "use":
                continue
            target = _use_target(icon_elem)
            if target in resolved_ids:
                stage_counts[target] = stage_counts.get(target, 0) + 1
        resolved_use_counts_by_stage[stage_id] = stage_counts
        stage_uses: set[str] = set()
        for icon_group in stage_node.iter():
            if icon_group.tag.split("}")[-1] != "g" or icon_group.get("data-role") != "icon":
                continue
            for icon_elem in icon_group.iter():
                if icon_elem.tag.split("}")[-1] != "use":
                    continue
                href = icon_elem.get("href") or icon_elem.get("{http://www.w3.org/1999/xlink}href") or ""
                if href.startswith("#") and href[1:] in resolved_ids:
                    stage_uses.add(href[1:])
        used_resolved_by_stage[stage_id] = stage_uses

    if resolved_ids and not used_resolved:
        errors.append("已有解析图标，但 SVG 没有使用任何对应的 <use> 元素")
    for icon_id in sorted(resolved_ids - used_resolved):
        errors.append(f"解析图标 {icon_id} 未在 SVG 中使用，不能替换或省略已选图标")
    if stages is not None:
        for icon in icons:
            if (
                not icon.get("id")
                or not _is_symbol_icon(icon)
                or not _requires_standalone_icon(icon, stages)
                or _general_symbol_satisfied(icon)
            ):
                continue
            icon_id = str(icon["id"])
            stage_id = str(icon.get("stage_id"))
            if stage_id and icon_id not in used_resolved_by_stage.get(stage_id, set()):
                errors.append(f"解析图标 {icon_id} 未在阶段 {stage_id} 的 icon 组中使用，不能交换或替换")
            if (
                str(icon.get("kind", "")).lower() == "brand"
                and resolved_use_counts_by_stage.get(stage_id, {}).get(icon_id, 0) > 1
            ):
                count = resolved_use_counts_by_stage[stage_id][icon_id]
                errors.append(f"品牌图标 {icon_id} 在阶段 {stage_id} 重复引用 {count} 次；每个品牌只能保留一个实例")
    for icon_id in sorted(generated_expected - generated_actual):
        errors.append(f"缺失通用图标 {icon_id} 未使用 data-generated-icon 标记生成图形")
    for icon_id in sorted(forbidden_generated & generated_actual):
        errors.append(f"品牌占位图 {icon_id} 禁止由 LLM 仿画")
    for icon_id in sorted(badges_expected - badges_actual):
        errors.append(f"品牌占位图 {icon_id} 未使用 data-brand-badge 中性名称徽章")
    for icon_id in sorted(badges_actual - labelled_badges):
        errors.append(f"品牌占位图 {icon_id} 的 data-brand-badge 缺少可见名称")
    for icon_id in sorted(badges_actual - badges_expected):
        errors.append(f"data-brand-badge 引用了未声明的品牌占位图：{icon_id}")
    unknown_generated = generated_actual - known_generated
    for icon_id in sorted(unknown_generated - forbidden_generated):
        errors.append(f"data-generated-icon 引用了未声明的通用回退图标：{icon_id}")
    if re.search(r'(?:href|src)=["\']https?://|url\(\s*["\']?https?://', svg_code, re.IGNORECASE):
        errors.append("SVG 包含外部网络资源，最终结果必须完全离线")
    return errors
