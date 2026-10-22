"""多 agent 共享工作台。

画图员和审查员各自通过工具读写这张工作台，agent 之间**不直接交换内部数据**。
这样有两条互不干扰的通路：

- 消息：负责沟通（谁说了什么、谁对谁有意见）
- 工作台：负责存放产物（当前是哪一版、上一版长什么样、最近查出什么问题）

工作台本身是确定性代码，不参与对话。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import defusedxml.ElementTree as ET
import httpx

from agents.code_generator import regenerate_patch
# 图标解析的内部函数：复用 resolve_icons 的三级回退，避免重复实现一套查询逻辑。
from agents.icon_resolver import _load_index, _resolve_one
from agents.validator import validate
from utils.icons import embed_icon_symbols, enforce_resolved_icons
from utils.svg import extract_svg
from utils.validation import (
    check_bounds,
    check_canvas_dimensions,
    check_element_overlaps,
    check_overlap,
    check_svg_syntax,
    check_text_completeness,
    normalize_connection_label_positions,
)


class Studio:
    """一次生成任务里的共享工作台。"""

    def __init__(
        self,
        *,
        task: str,
        content: list[dict],
        structure: dict,
        style: dict,
        icons: list[dict],
        resolved_icons: list[dict],
        on_version: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.task = task
        self.content = content
        self.structure = structure
        self.style = style
        self.icons = icons
        # 复制一份：画图员中途补查的图标会追加进来
        self.resolved_icons = list(resolved_icons)
        # 版本历史，最后一项即当前版本
        self.versions: list[dict[str, Any]] = []
        # 最近一次检查的结果，供打补丁时作为反馈
        self.last_issues: dict[str, Any] = {}
        # 每产生一个新版本就通知一次（UI 实时预览用）
        self._on_version = on_version
        self._icon_index: dict | None = None

    def _record_version(self, svg: str, note: str, repair: str) -> dict[str, Any]:
        """记录一个新版本，并通知外部。"""
        entry: dict[str, Any] = {
            "version": len(self.versions) + 1,
            "svg": svg,
            "note": note,
            "repair": repair,
        }
        self.versions.append(entry)
        if self._on_version is not None:
            try:
                self._on_version(dict(entry))
            except Exception:  # 回调失败不能影响协作本身
                logging.warning("[Studio] 版本回调执行失败", exc_info=True)
        return entry

    # ------------------------------------------------------------------
    # 版本状态
    # ------------------------------------------------------------------

    @property
    def current_svg(self) -> str:
        return self.versions[-1]["svg"] if self.versions else ""

    @property
    def previous_svg(self) -> str:
        return self.versions[-2]["svg"] if len(self.versions) >= 2 else ""

    def _post_process(self, raw_svg: str) -> str:
        """确定性后处理：注入图标、回填图标槽位、归位连线标签。

        与 orchestrator 里对每次生成的 SVG 所做的处理完全一致。
        """
        svg = embed_icon_symbols(raw_svg, self.resolved_icons, self.style)
        svg = enforce_resolved_icons(
            svg, self.resolved_icons, self.structure.get("stages", [])
        )
        svg = normalize_connection_label_positions(
            svg, self.structure.get("connections", [])
        )
        return svg

    # ------------------------------------------------------------------
    # 画图员的工具
    # ------------------------------------------------------------------

    async def lookup_icon(self, query: str, stage_id: str, kind: str = "general") -> str:
        """按名字查图标并登记到指定 stage。"""
        normalized_kind = "brand" if str(kind).strip().lower() == "brand" else "general"
        if self._icon_index is None:
            self._icon_index = _load_index()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                data = await _resolve_one(
                    client, query=query, kind=normalized_kind, index=self._icon_index
                )
        except Exception as exc:  # 网络问题不应中断协作
            logging.warning("[Studio] 图标查询失败：%s", exc)
            return f"查询「{query}」失败（{exc}）。请改用简单几何图形代替，不要留空。"

        if str(data.get("source")) == "fallback":
            return (
                f"没有找到「{query}」对应的精确图标。"
                "请用简单几何图形代替这个元素，不要硬塞占位符。"
            )

        icon = dict(data)
        icon.setdefault(
            "palette_mode",
            "monochrome" if "currentColor" in str(icon.get("body", "")) else "hardcoded",
        )
        icon.setdefault(
            "render_mode",
            "brand_badge"
            if normalized_kind == "brand" and icon.get("source") == "fallback"
            else "symbol",
        )
        icon.update(
            {
                "id": f"icon-extra-{len(self.resolved_icons)}",
                "label": query,
                "query": query,
                "kind": normalized_kind,
                "stage_id": stage_id,
                "visual_index": 0,
            }
        )
        self.resolved_icons.append(icon)
        return (
            f"已登记图标 {icon.get('collection')}:{icon.get('name')}"
            f"（许可 {icon.get('license', 'unknown')}），归属 stage {stage_id}。"
            "提交时它会自动注入，你只需在该节点留出图标位置。"
        )

    async def submit_svg(self, svg_code: str, note: str = "") -> str:
        """提交一个完整版本，自动做图标注入等后处理。"""
        raw = extract_svg(str(svg_code or "")) or str(svg_code or "").strip()
        if "<svg" not in raw:
            return (
                "没有从你的消息里找到 <svg> 元素。"
                "请把完整的 SVG 代码放进 ```svg 代码块后重新提交。"
            )
        processed = self._post_process(raw)
        self._record_version(processed, str(note or "").strip(), "submit")
        return (
            f"已接收第 {len(self.versions)} 版（{len(processed)} 字符）。"
            "请用一两句话说明这一版改了什么，然后等审查员的意见。"
        )

    async def revise_svg(self, instructions: str) -> str:
        """在当前版本上打局部补丁，比重画整张快。"""
        if not self.current_svg:
            return "还没有可修改的版本。请先用 submit_svg 提交第一版。"
        feedback = dict(self.last_issues)
        feedback["retry_hint"] = str(instructions or "")
        try:
            patched = await regenerate_patch(
                self.task,
                self.content,
                self.structure,
                self.style,
                self.current_svg,
                feedback,
                self.icons,
            )
        except Exception as exc:
            logging.warning("[Studio] 补丁应用失败：%s", exc)
            return f"打补丁失败（{exc}）。请重新输出完整 SVG 并用 submit_svg 提交。"
        processed = self._post_process(patched)
        self._record_version(processed, str(instructions or "").strip(), "patch")
        return f"补丁已应用，现在是第 {len(self.versions)} 版。请说明你改了什么。"

    # ------------------------------------------------------------------
    # 审查员的工具
    # ------------------------------------------------------------------

    def quick_check(self) -> str:
        """只跑确定性检查，不调用视觉模型，很快。"""
        svg = self.current_svg
        if not svg:
            return "当前还没有提交任何版本，没有可检查的内容。"

        width = int(self.structure.get("width", 1200) or 1200)
        height = int(self.structure.get("height", 800) or 800)
        stages = self.structure.get("stages", [])
        connections = self.structure.get("connections", [])

        syntax_errors = check_svg_syntax(svg)
        root = None
        if not syntax_errors and svg.strip():
            try:
                root = ET.fromstring(svg)
            except ET.ParseError:
                pass  # check_svg_syntax 已捕获

        blockers: list[str] = list(syntax_errors)
        # 画布偏大可以缩放显示，不算阻断；偏小可能被裁切，必须拦下
        blockers += [
            error
            for error in check_canvas_dimensions(svg, width, height, root=root)
            if "实际为" in error
        ]
        blockers += check_bounds(svg, width, height, root=root)
        blockers += check_text_completeness(svg, root=root)
        blockers += check_overlap(stages)
        blockers += check_element_overlaps(
            svg,
            stages,
            connections,
            self.structure.get("header"),
            root=root,
        )

        self.last_issues = {"syntax_errors": syntax_errors, "hard_errors": blockers}

        if not blockers:
            return (
                "确定性检查通过：画布尺寸、边界越界、文字完整性、元素重叠都没有问题。"
                "可以再调 inspect_with_vision 看排版观感。"
            )
        shown = "\n".join(f"- {item}" for item in blockers[:12])
        more = f"\n（另有 {len(blockers) - 12} 条未列出）" if len(blockers) > 12 else ""
        return f"发现 {len(blockers)} 处硬性错误：\n{shown}{more}"

    async def inspect_with_vision(self) -> str:
        """完整校验，含视觉模型评估，慢一些。"""
        svg = self.current_svg
        if not svg:
            return "当前还没有提交任何版本，没有可检查的内容。"
        try:
            result = await validate(
                svg,
                self.task,
                self.content,
                self.structure,
                self.style,
                self.resolved_icons,
            )
        except Exception as exc:
            logging.exception("[Studio] 完整校验执行失败：%s", exc)
            return f"完整校验执行失败（{exc}）。请先依据 quick_check 的结果判断。"

        self.last_issues = result
        parts: list[str] = []

        hard_errors = result.get("hard_errors", [])
        if hard_errors:
            listed = "\n".join(f"- {item}" for item in hard_errors[:10])
            parts.append(f"硬性错误：\n{listed}")

        defects = result.get("visual_defects", [])
        defect_lines: list[str] = []
        for item in defects[:8]:
            if not isinstance(item, dict):
                continue
            defect_lines.append(
                f"- {item.get('location', '画面位置')}：{item.get('issue', '')}"
                f"（原因：{item.get('reason', '')}；建议：{item.get('fix', '')}）"
            )
        if defect_lines:
            parts.append("视觉模型指出：\n" + "\n".join(defect_lines))

        feedback = str(result.get("visual_feedback", "") or "").strip()
        if feedback:
            parts.append(f"视觉模型整体评价：{feedback}")

        if result.get("visual_status") == "unavailable":
            parts.append("提示：视觉模型本次不可用，以上只有确定性检查结果。")

        if not parts:
            parts.append("确定性检查和视觉检查都没有发现问题。")
        return "\n\n".join(parts)
