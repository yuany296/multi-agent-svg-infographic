"""编排结构分析、图标解析、布局、生成、图标注入与迭代校验。"""

import logging
import re
from copy import deepcopy
from typing import Any

import defusedxml.ElementTree as ET

from agents.content_analyzer import analyze
from agents.icon_resolver import icon_report, resolve_icons
from agents.structure_planner import plan
from agents.style_designer import design
from agents.code_generator import generate, regenerate, regenerate_patch
from agents.studio import Studio
from agents.team import build_kickoff, run_studio
from agents.validator import validate
from utils.icons import embed_icon_symbols, enforce_resolved_icons
from utils.layout import compute_layout, relayout_for_validation
from utils.svg import log_intermediate
from utils.validation import normalize_connection_label_positions
from utils.visual_eval import compare_visual_candidates

MAX_RETRIES = 2


async def _repair_svg_with_patch_fallback(
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    retry_base_svg: str,
    validation: dict[str, Any],
    icons: list[dict] | None,
    force_full: bool = False,
    force_stage_scope: bool = False,
) -> tuple[str, str]:
    """普通重试优先走 diff，patch 失败再回退完整 SVG。"""
    target_stage_ids = sorted({
        str(item.get("stage_id"))
        for item in validation.get("issues", [])
        if isinstance(item, dict)
        and item.get("severity") == "blocking"
        and str(item.get("stage_id", "")).strip()
    })
    if force_stage_scope and target_stage_ids:
        scoped_validation = deepcopy(validation)
        scoped_validation["stage_regeneration"] = True
        scoped_validation["target_stage_ids"] = target_stage_ids
        try:
            patched_svg = await regenerate_patch(
                task, content, structure, style, retry_base_svg, scoped_validation, icons
            )
            return patched_svg, "stage-scoped-patch"
        except Exception as exc:
            # 局部 diff 同样可能引用过期上下文，不能让补丁异常终止整个验证循环。
            logging.warning(
                "[CodeGenerator] 目标 stage 局部 patch 修复失败，回退完整 SVG 重生成：%s",
                exc,
            )
            fallback_svg = await regenerate(
                task, content, structure, style, retry_base_svg, scoped_validation, icons
            )
            return fallback_svg, "full-fallback"
    if force_full:
        fallback_svg = await regenerate(
            task,
            content,
            structure,
            style,
            retry_base_svg,
            validation,
            icons,
        )
        return fallback_svg, "full-after-regressions"
    try:
        patched_svg = await regenerate_patch(
            task,
            content,
            structure,
            style,
            retry_base_svg,
            validation,
            icons,
        )
        return patched_svg, "patch"
    except Exception as exc:
        if target_stage_ids:
            logging.warning("[CodeGenerator] patch 修复失败，切换目标 stage 局部重生成：%s", exc)
            scoped_validation = deepcopy(validation)
            scoped_validation["stage_regeneration"] = True
            scoped_validation["target_stage_ids"] = target_stage_ids
            try:
                patched_svg = await regenerate_patch(
                    task, content, structure, style, retry_base_svg, scoped_validation, icons
                )
                return patched_svg, "stage-scoped-patch"
            except Exception as scoped_exc:
                logging.warning(
                    "[CodeGenerator] 目标 stage 局部 patch 修复仍失败，回退完整 SVG 重生成：%s",
                    scoped_exc,
                )
                fallback_svg = await regenerate(
                    task, content, structure, style, retry_base_svg, scoped_validation, icons
                )
                return fallback_svg, "full-fallback"
        logging.warning("[CodeGenerator] patch 修复失败，回退完整 SVG 重生成：%s", exc)
        fallback_svg = await regenerate(
            task,
            content,
            structure,
            style,
            retry_base_svg,
            validation,
            icons,
        )
        return fallback_svg, "full-fallback"


def _format_ratio(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _extract_multiplier(value: Any) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|倍|times?)", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    try:
        multiplier = float(match.group(1))
    except ValueError:
        return None
    return multiplier if multiplier > 0 else None


def _metric_value(stage: dict[str, Any]) -> float | None:
    metric = stage.get("metric") if isinstance(stage.get("metric"), dict) else {}
    try:
        value = float(metric.get("value"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _normalize_compare_chain_metrics(task: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """把链式 compare 倍数折算到统一基准，连接标签仍保留两两关系。"""
    if str(analysis.get("diagram_type", "")).strip().lower() != "compare":
        return analysis
    stages = [stage for stage in analysis.get("stages", []) if isinstance(stage, dict)]
    connections = [item for item in analysis.get("connections", []) if isinstance(item, dict)]
    if len(stages) < 2 or not connections:
        return analysis

    stage_by_id = {stage.get("id", index): stage for index, stage in enumerate(stages)}
    relations: list[tuple[object, object, float]] = []
    task_lc = task.lower()
    for connection in connections:
        multiplier = _extract_multiplier(connection.get("label"))
        if multiplier is None:
            continue
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue

        source_title = str(source.get("title", "")).strip().lower()
        target_title = str(target.get("title", "")).strip().lower()
        source_value = _metric_value(source)
        target_value = _metric_value(target)
        source_is_larger = False
        target_is_larger = False

        # 用数值判断方向（最可靠）
        if source_value and target_value:
            if source_value > target_value:
                source_is_larger = True
            elif target_value > source_value:
                target_is_larger = True
        # 值相等或缺失时：对比图箭头从低柱指向高柱，target 是较大端
        if not source_is_larger and not target_is_larger:
            target_is_larger = True
        if source_is_larger:
            relations.append((source.get("id"), target.get("id"), multiplier))
        elif target_is_larger:
            relations.append((target.get("id"), source.get("id"), multiplier))

    if not relations:
        return analysis

    graph: dict[object, list[tuple[object, float]]] = {}
    for larger, smaller, multiplier in relations:
        graph.setdefault(larger, []).append((smaller, 1 / multiplier))
        graph.setdefault(smaller, []).append((larger, multiplier))

    ratios: dict[object, float] = {}
    for stage_id in stage_by_id:
        if stage_id in ratios:
            continue
        ratios[stage_id] = 1.0
        queue = [stage_id]
        while queue:
            current = queue.pop(0)
            for neighbor, factor in graph.get(current, []):
                if neighbor in ratios:
                    continue
                ratios[neighbor] = ratios[current] * factor
                queue.append(neighbor)

    positive = [value for value in ratios.values() if value > 0]
    if not positive:
        return analysis
    base = min(positive)
    changed = False
    for stage in stages:
        stage_id = stage.get("id")
        if stage_id not in ratios:
            continue
        normalized = ratios[stage_id] / base
        current = _metric_value(stage)
        if current is not None and abs(current - normalized) < 1e-6:
            continue
        metric = dict(stage.get("metric") if isinstance(stage.get("metric"), dict) else {})
        metric["value"] = int(round(normalized)) if abs(normalized - round(normalized)) < 1e-6 else round(normalized, 2)
        metric["label"] = f"{_format_ratio(normalized)}x"
        metric.setdefault("unit", None)
        stage["metric"] = metric
        changed = True
    if changed:
        log_intermediate("Compare 倍数归一化", {"metrics": [stage.get("metric") for stage in stages]})

    # 修正 compare 连接方向：倍数必须从低指向高（乘法语义）
    for connection in connections:
        multiplier = _extract_multiplier(connection.get("label"))
        if multiplier is None:
            continue
        source = stage_by_id.get(connection.get("from"))
        target = stage_by_id.get(connection.get("to"))
        if not source or not target:
            continue
        source_val = _metric_value(source) or 0
        target_val = _metric_value(target) or 0
        # 如果 from 的值 > to 的值，方向反了，交换 from/to
        if source_val > target_val:
            old_from = connection["from"]
            old_to = connection["to"]
            connection["from"] = old_to
            connection["to"] = old_from
            log_intermediate(
                "Compare 连接方向修正",
                {"label": connection.get("label"), "old": f"{old_from}→{old_to}", "new": f"{old_to}→{old_from}"},
            )
            changed = True

    return analysis


def _detect_metric_discrepancy(
    analysis: dict[str, Any],
    expected_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    """比较 ContentAnalyzer 原始指标与代码计算出的归一化指标。"""
    stages = [s for s in analysis.get("stages", []) if isinstance(s, dict)]
    connections = [c for c in analysis.get("connections", []) if isinstance(c, dict)]
    expected_by_id = {
        stage.get("id", index): stage
        for index, stage in enumerate(expected_analysis.get("stages", []))
        if isinstance(stage, dict)
    }

    discrepancies: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        sid = stage.get("id", index)
        expected = _metric_value(expected_by_id.get(sid, {}))
        if expected is None:
            continue
        original = _metric_value(stage)
        if original is None or abs(original - expected) > 0.01:
            discrepancies.append({
                "stage_id": sid,
                "title": stage.get("title", ""),
                "original_value": original,
                "normalized_value": round(expected, 2),
                "connections": [
                    {"from": c.get("from"), "to": c.get("to"), "label": c.get("label")}
                    for c in connections
                    if (c.get("from") == sid or c.get("to") == sid)
                    and _extract_multiplier(c.get("label"))
                ],
            })
    return discrepancies


async def _reconcile_compare_metrics(
    task: str,
    analysis: dict[str, Any],
    expected_analysis: dict[str, Any],
) -> dict[str, Any]:
    """发现倍数矛盾时反馈给 LLM 修正，并用代码结果校验修正值。

    LLM 不可用或修正值仍不一致时，使用代码计算结果兜底，避免错误进入制图。
    """
    discrepancies = _detect_metric_discrepancy(analysis, expected_analysis)
    if not discrepancies:
        return expected_analysis

    from utils.llm import call_llm_json
    from utils.config import get_model_client

    client = get_model_client()
    stages_summary = "\n".join(
        f"- stage {d['stage_id']} ({d['title']}): "
        f"ContentAnalyzer 估算={d['original_value']}x, "
        f"连接标签推导={d['normalized_value']}x"
        for d in discrepancies
    )
    conns_summary = "\n".join(
        f"- {c.get('from')}→{c.get('to')}: \"{c.get('label')}\""
        for c in analysis.get("connections", [])
        if isinstance(c, dict) and c.get("label")
    )

    prompt = (
        f"任务描述：{task}\n\n"
        f"以下是各阶段的倍数关系和两个不同来源的数值：\n"
        f"连接标签（来自任务描述，可信度高）：\n{conns_summary}\n\n"
        f"数值矛盾：\n{stages_summary}\n\n"
        f"请根据任务描述和代码计算出的归一化比例修正每个阶段的倍数值。"
        f"输出 JSON：{{\"metrics\": [{{\"stage_id\": ..., \"value\": ..., \"label\": \"...x\"}}]}}\n"
        f"只输出 JSON，不要解释。"
    )

    try:
        result = await call_llm_json(
            client,
            "你是一名数据一致性校验专家。根据任务描述中的倍数关系推导每个阶段的正确指标值。",
            prompt,
        )
    except Exception as exc:
        logging.warning("指标修正 LLM 调用失败，使用代码归一化结果：%s", exc)
        return expected_analysis

    reconciled = result.get("metrics", []) if isinstance(result, dict) else []
    if not reconciled:
        logging.warning("指标修正 LLM 未返回有效 metrics，使用代码归一化结果")
        return expected_analysis

    corrected = deepcopy(expected_analysis)
    stage_by_id = {s.get("id"): s for s in corrected.get("stages", []) if isinstance(s, dict)}
    expected_by_id = {
        s.get("id"): _metric_value(s)
        for s in expected_analysis.get("stages", [])
        if isinstance(s, dict)
    }
    changed = False
    for item in reconciled:
        if not isinstance(item, dict):
            continue
        sid = item.get("stage_id")
        value = item.get("value")
        label = str(item.get("label", ""))
        if sid is None or value is None:
            continue
        stage = stage_by_id.get(sid)
        if not stage:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        expected_value = expected_by_id.get(sid)
        if expected_value is None or abs(numeric_value - expected_value) > 0.01:
            logging.warning(
                "LLM 修正指标仍不一致：stage %s 返回 %s，代码计算为 %s；保留代码结果",
                sid,
                numeric_value,
                expected_value,
            )
            continue
        metric = dict(stage.get("metric") if isinstance(stage.get("metric"), dict) else {})
        metric["value"] = numeric_value
        metric["label"] = label or f"{_format_ratio(numeric_value)}x"
        metric.setdefault("unit", None)
        stage["metric"] = metric
        changed = True
        logging.info("LLM 修正指标：stage %s (%s) → %s", sid, stage.get("title", ""), metric["label"])

    if changed:
        log_intermediate("Compare 指标 LLM 裁决", {
            "discrepancies": discrepancies,
            "reconciled": reconciled,
        })
    return corrected


def _validation_error_fingerprint(value: Any) -> str:
    """把同一类布局错误的动态坐标折叠为稳定指纹。"""
    message = re.sub(r"\s+", " ", str(value or "")).strip()
    if not message:
        return message
    # 重试通常只会改变 bbox 数值；阶段、角色和错误类型才是候选比较所需的身份。
    if any(keyword in message for keyword in ("越出槽位", "溢出画布", "偏上", "偏下")):
        return re.split(r"[：:]", message, maxsplit=1)[0]
    if "连接 " in message and "标签" in message:
        return re.split(r"[：:]", message, maxsplit=1)[0]
    return message


def _validation_error_sets(validation: dict[str, Any]) -> tuple[set[str], set[str]]:
    """返回语义/结构错误和全部阻断错误，供候选稿单调比较。"""
    issues = [item for item in validation.get("issues", []) if isinstance(item, dict)]
    if issues:
        blocking = {
            str(item.get("issue_id"))
            for item in issues
            if item.get("severity") == "blocking" and item.get("issue_id")
        }
        semantic = {
            str(item.get("issue_id"))
            for item in issues
            if item.get("category") in {"syntax", "semantic", "structure"} and item.get("issue_id")
        }
        return semantic, blocking
    syntax = {str(item) for item in validation.get("syntax_errors", [])}
    structural = {
        _validation_error_fingerprint(item)
        for item in validation.get("structural_errors", [])
    }
    semantic = syntax | structural
    blocking = syntax | {
        _validation_error_fingerprint(item)
        for item in validation.get("hard_errors", [])
    }
    return semantic, blocking


def _blocking_issue_ids(validation: dict[str, Any] | None) -> set[str]:
    if not validation:
        return set()
    _, blocking = _validation_error_sets(validation)
    return blocking


_ANCHOR_DRAWABLE_WEIGHTS = {
    "path": 3,
    "polyline": 3,
    "polygon": 3,
    "circle": 2,
    "ellipse": 2,
    "line": 2,
    "use": 2,
    "rect": 1,
}


def _normalise_svg_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _svg_visual_anchor_profile(svg_code: str) -> dict[str, Any]:
    """Extract stable visual/semantic anchors without judging artistic quality."""
    profile: dict[str, Any] = {
        "stage_anchors": {},
        "texts": [],
        "text_blob": "",
        "parseable": False,
    }
    if not svg_code.strip():
        return profile
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError:
        return profile

    profile["parseable"] = True
    texts: list[str] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "text":
            continue
        value = _normalise_svg_text("".join(elem.itertext()))
        if value:
            texts.append(value)
    profile["texts"] = texts
    profile["text_blob"] = "".join(texts)

    for stage_node in root.iter():
        if stage_node.tag.split("}")[-1] != "g":
            continue
        if "stage-node" not in str(stage_node.get("class", "")).split():
            continue
        stage_id = str(stage_node.get("data-stage-id", "")).strip()
        if not stage_id:
            continue
        anchor = {
            "drawable_count": 0,
            "richness": 0,
            "tag_counts": {},
            "generated_marks": set(),
        }
        role_groups = [
            elem
            for elem in stage_node.iter()
            if elem is not stage_node
            and elem.tag.split("}")[-1] == "g"
            and elem.get("data-role") in {"icon", "scene"}
        ]
        for role_group in role_groups:
            for elem in role_group.iter():
                tag = elem.tag.split("}")[-1]
                weight = _ANCHOR_DRAWABLE_WEIGHTS.get(tag)
                if weight is not None:
                    anchor["drawable_count"] += 1
                    anchor["richness"] += weight
                    counts = anchor["tag_counts"]
                    counts[tag] = int(counts.get(tag, 0)) + 1
                mark = str(elem.get("data-generated-icon", "")).strip()
                if mark:
                    anchor["generated_marks"].add(mark)
        profile["stage_anchors"][stage_id] = anchor
    return profile


def _icon_revision_requested(validation: dict[str, Any]) -> bool:
    values: list[str] = []
    for key in (
        "syntax_errors",
        "hard_errors",
        "suggestions",
        "visual_suggestions",
        "retry_hint",
    ):
        raw = validation.get(key, [])
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
        elif raw:
            values.append(str(raw))
    for defect in validation.get("visual_defects", []):
        if isinstance(defect, dict):
            values.extend(str(defect.get(key, "")) for key in ("issue", "reason", "fix"))
    haystack = " ".join(values).lower()
    return any(
        keyword in haystack
        for keyword in ("icon", "symbol", "generated-icon", "图标", "图形", "scene", "场景")
    )


def _text_revision_requested(validation: dict[str, Any]) -> bool:
    """只有上一轮明确指出文字问题时，才允许候选改写、缩短或换行文字。"""
    values: list[str] = []
    for key in (
        "syntax_errors",
        "hard_errors",
        "suggestions",
        "visual_suggestions",
        "retry_hint",
        "visual_feedback",
    ):
        value = validation.get(key, [])
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    haystack = " ".join(values).lower()
    return any(
        keyword in haystack
        for keyword in (
            "text",
            "title",
            "description",
            "文字",
            "文本",
            "标题",
            "说明",
            "字号",
            "换行",
            "溢出",
            "越出槽位",
        )
    )


def _invalid_generated_icon_marks(validation: dict[str, Any]) -> set[str]:
    """提取上一轮已明确判为未声明/非法的 data-generated-icon 标记。"""
    values: list[str] = []
    for key in ("hard_errors", "suggestions", "visual_suggestions", "retry_hint", "visual_feedback"):
        value = validation.get(key, [])
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    invalid: set[str] = set()
    for value in values:
        if "data-generated-icon" not in value or not any(
            keyword in value for keyword in ("未声明", "非法", "不允许", "unknown", "undeclared")
        ):
            continue
        invalid.update(
            re.findall(
                r"data-generated-icon[^：:\n]*[：:]\s*([A-Za-z0-9_.-]+)",
                value,
                flags=re.IGNORECASE,
            )
        )
    return invalid


def _visual_anchor_regressions(
    incumbent_svg: str,
    candidate_svg: str,
    structure: dict[str, Any],
    repair_validation: dict[str, Any],
    *,
    candidate_is_deliverable: bool = False,
) -> list[str]:
    """Reject obvious semantic drift while allowing an explicit icon/scene repair."""
    incumbent = _svg_visual_anchor_profile(incumbent_svg)
    candidate = _svg_visual_anchor_profile(candidate_svg)
    if not incumbent.get("parseable") or not candidate.get("parseable"):
        return []

    regressions: list[str] = []
    if not _text_revision_requested(repair_validation):
        candidate_text_blob = str(candidate.get("text_blob", ""))
        for text_value in incumbent.get("texts", []):
            if len(text_value) >= 3 and text_value not in candidate_text_blob:
                regressions.append(f"保留文本丢失或被改写: {text_value[:40]}")
                if len(regressions) >= 3:
                    return regressions

    incumbent_stages = incumbent.get("stage_anchors", {})
    candidate_stages = candidate.get("stage_anchors", {})
    invalid_generated_marks = _invalid_generated_icon_marks(repair_validation)
    for stage in structure.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id", "")).strip()
        before = incumbent_stages.get(stage_id, {})
        after = candidate_stages.get(stage_id, {})
        before_count = int(before.get("drawable_count", 0) or 0)
        after_count = int(after.get("drawable_count", 0) or 0)
        before_richness = int(before.get("richness", 0) or 0)
        after_richness = int(after.get("richness", 0) or 0)
        if before_count >= 3 and after_count == 0:
            regressions.append(f"阶段 {stage_id} 的已有图形全部丢失")
        elif (
            not candidate_is_deliverable
            and before_richness >= 8
            and after_richness < max(3, int(before_richness * 0.55))
        ):
            regressions.append(f"阶段 {stage_id} 的图形复杂度明显退化")
        before_marks = set(before.get("generated_marks", set()))
        after_marks = set(after.get("generated_marks", set()))
        missing_marks = (before_marks - after_marks) - invalid_generated_marks
        if missing_marks:
            regressions.append(f"阶段 {stage_id} 的自绘图形标记丢失: {', '.join(sorted(missing_marks))}")
        if len(regressions) >= 3:
            break
    return regressions


async def _select_better_candidate(
    *,
    incumbent_svg: str | None,
    incumbent_validation: dict[str, Any] | None,
    candidate_svg: str,
    candidate_validation: dict[str, Any],
    task: str,
    structure: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    """保守选择候选稿：语义不能退化，不确定时保留 incumbent。"""
    if not incumbent_svg or not incumbent_validation:
        return True, "initial-candidate", {}

    incumbent_semantic, incumbent_blocking = _validation_error_sets(incumbent_validation)
    candidate_semantic, candidate_blocking = _validation_error_sets(candidate_validation)
    new_semantic_errors = candidate_semantic - incumbent_semantic
    if new_semantic_errors:
        return False, "rejected-semantic-regression", {
            "winner": "a",
            "confidence": "high",
            "reasons": ["新候选稿引入结构/语义错误"],
            "new_semantic_errors": sorted(new_semantic_errors),
            "visual_source": "deterministic-gate",
        }

    new_blocking_errors = candidate_blocking - incumbent_blocking
    if new_blocking_errors:
        return False, "rejected-blocking-regression", {
            "winner": "a",
            "confidence": "high",
            "reasons": ["新候选稿引入新的阻断错误"],
            "new_blocking_errors": sorted(new_blocking_errors),
            "visual_source": "deterministic-gate",
        }

    anchor_regressions = _visual_anchor_regressions(
        incumbent_svg,
        candidate_svg,
        structure,
        incumbent_validation,
        candidate_is_deliverable=bool(candidate_validation.get("passed")),
    )
    if anchor_regressions:
        return False, "rejected-visual-anchor-regression", {
            "winner": "a",
            "confidence": "high",
            "reasons": anchor_regressions,
            "regressions": anchor_regressions,
            "visual_source": "deterministic-anchor-gate",
        }

    # 检查候选稿是否减少了关键布局错误（重叠/遮挡/缺失连接）
    # 即使阻断错误总数未变，修复了这些关键问题也是净提升
    def _key_error_items(validation: dict) -> set[str]:
        suggestions = validation.get("suggestions", [])
        return {
            str(s) for s in (suggestions if isinstance(suggestions, list) else [])
            if isinstance(s, str) and (
                "重叠" in s or "遮挡" in s
                or ("缺少" in s and ("连接" in s or "箭头" in s))
                or "偏左" in s or "偏右" in s or "居中" in s
            )
        }

    if not new_blocking_errors:
        incumbent_key_errors = _key_error_items(incumbent_validation)
        candidate_key_errors = _key_error_items(candidate_validation)
        resolved = incumbent_key_errors - candidate_key_errors
        if resolved and not (candidate_key_errors - incumbent_key_errors):
            return True, "accepted-key-errors-resolved", {
                "winner": "b",
                "confidence": "high",
                "reasons": [f"关键布局错误减少：{sorted(resolved)}"],
                "resolved_errors": sorted(resolved),
                "visual_source": "deterministic-gate",
            }

    if candidate_validation.get("passed") and not incumbent_validation.get("passed"):
        return True, "accepted-now-deliverable", {
            "winner": "b",
            "confidence": "high",
            "reasons": ["候选稿从不可交付变为可交付，且没有引入新错误"],
            "visual_source": "deterministic-gate",
        }

    comparison: dict[str, Any] = {}
    if incumbent_validation.get("visual_model_enabled") and candidate_validation.get("visual_model_enabled"):
        comparison = await compare_visual_candidates(
            incumbent_svg,
            candidate_svg,
            task,
            {
                "diagram_type": structure.get("diagram_type", ""),
                "template_id": structure.get("template_id", ""),
                "stages": [
                    {"id": item.get("id"), "title": item.get("title", "")}
                    for item in structure.get("stages", [])
                    if isinstance(item, dict)
                ],
                "connections": structure.get("connections", []),
            },
        )
        if comparison.get("visual_model_enabled"):
            if comparison.get("winner") == "b" and comparison.get("confidence") in {"high", "medium"}:
                return True, "accepted-visual-pairwise", comparison
            return False, "rejected-visual-not-better", comparison

    if candidate_blocking < incumbent_blocking:
        return True, "accepted-fewer-blocking-errors", {
            "winner": "b",
            "confidence": "high",
            "reasons": ["阻断错误严格减少且未引入新错误"],
            "visual_source": "deterministic-fallback",
        }
    return False, "rejected-no-proven-improvement", {
        "winner": "tie",
        "confidence": "low",
        "reasons": ["视觉比较不可用，且阻断错误没有严格减少"],
        "visual_source": "deterministic-fallback",
    }


def _normalize_analysis(value: Any) -> dict[str, Any]:
    """兼容旧节点数组，并把分析结果收敛为稳定的顶层契约。"""
    if isinstance(value, dict):
        raw_stages = value.get("stages", [])
        raw_connections = value.get("connections", [])
        stages = [dict(item) for item in raw_stages if isinstance(item, dict)] if isinstance(raw_stages, list) else []
        connections = [dict(item) for item in raw_connections if isinstance(item, dict)] if isinstance(raw_connections, list) else []
        diagram_type = str(value.get("diagram_type") or "").strip().lower()
    elif isinstance(value, list):
        stages = [dict(item) for item in value if isinstance(item, dict)]
        connections = []
        diagram_type = ""
    else:
        stages = []
        connections = []
        diagram_type = ""

    if not diagram_type:
        diagram_type = next(
            (str(item.get("diagram_type")).strip().lower() for item in stages if item.get("diagram_type")),
            "cards",
        )
    for stage in stages:
        stage.setdefault("diagram_type", diagram_type)
    return {"diagram_type": diagram_type, "stages": stages, "connections": connections}


async def generate_infographic(
    task: str,
    progress_callback: Any | None = None,
    use_agent_team: bool = True,
) -> dict[str, Any]:
    """
    主流程：前五个阶段顺序执行，之后由「画图员 ⇄ 审查员」两个 agent 协作产出 SVG。

    参数:
        task: 用户输入的信息图生成任务
        progress_callback: 可选回调，每轮 SVG 生成后调用
            callback({"type": "svg", "attempt": int, "svg": str, "status": str})
        use_agent_team: True（默认）走双 agent 协作；False 回退到旧的固定重试循环。
            旧路径保留用于对照和排障。
    返回:
        {
            "svg": str,              # 最终 SVG 代码
            "validation": dict,      # 最后一次校验结果
            "analysis": dict,       # 顶层内容分析（stages + connections）
            "content": list,         # 兼容字段：analysis.stages
            "structure_raw": dict,   # StructurePlanner 原始布局意图
            "structure": dict,       # 经代码计算后的实际坐标
            "style": dict,           # 样式设计结果
            "icons": list,           # 不含 SVG body 的图标解析摘要
            "icon_diagnostics": dict,# local/cache/remote/fallback 统计
            "conversation": list,    # 协作模式独有：两个 agent 的完整对话记录
            "stop_reason": str,      # 协作模式独有：因为什么停止
        }
    """
    # 1. 内容分析
    analysis = _normalize_analysis(await analyze(task))
    # 对比图保留 ContentAnalyzer 原值，代码另算标准比例；不一致时反馈给 LLM 修正。
    expected_analysis = _normalize_compare_chain_metrics(task, deepcopy(analysis))
    analysis = await _reconcile_compare_metrics(task, analysis, expected_analysis)
    content = analysis["stages"]
    log_intermediate("ContentAnalyzer 输出", analysis)

    # 2. 确定性图标解析（不调用 LLM）
    resolved_icons, icon_diagnostics = await resolve_icons(content)
    icons = icon_report(resolved_icons)
    log_intermediate("IconResolver 输出", icons)

    # 3. 结构规划（只输出意图，不计算坐标）
    planned_structure = await plan(task, analysis)
    structure_raw = dict(planned_structure) if isinstance(planned_structure, dict) else {}
    # 分析器拥有语义所有权；即使规划模型漂移，也不能改写图类型和连接。
    structure_raw["diagram_type"] = analysis["diagram_type"]
    structure_raw["connections"] = [dict(item) for item in analysis["connections"]]

    # 4. 用代码把布局意图计算成确定性的 stage/section 坐标
    structure = compute_layout(
        structure_raw,
        content,
        semantic_connections=analysis["connections"],
    )
    structure.setdefault("layout_revision", 0)
    log_intermediate("LayoutEngine 计算结果", structure)

    # 5. 样式设计
    style = await design(task, content, structure, icons)
    log_intermediate("StyleDesigner 输出", style)

    # 6-8. 产出 SVG：默认交给双 agent 协作，也可回退到旧的固定重试循环
    shared = dict(
        task=task,
        analysis=analysis,
        content=content,
        structure=structure,
        structure_raw=structure_raw,
        style=style,
        icons=icons,
        resolved_icons=resolved_icons,
        icon_diagnostics=icon_diagnostics,
        progress_callback=progress_callback,
    )
    if use_agent_team:
        return await _generate_with_agent_team(**shared)
    return await _generate_with_legacy_loop(**shared)


async def _generate_with_agent_team(
    *,
    task: str,
    analysis: dict,
    content: list[dict],
    structure: dict,
    structure_raw: dict,
    style: dict,
    icons: list[dict],
    resolved_icons: list[dict],
    icon_diagnostics: dict[str, int],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """由「画图员 ⇄ 审查员」两个 agent 对话协作，产出最终 SVG。

    与旧流程的根本区别：重画几次、改哪一块、什么时候算完，
    全部由两个 agent 在对话里自己决定，而不是由这里的 Python 代码写死。
    Python 只负责在开场时把前置结论交给它们。
    """
    studio = Studio(
        task=task,
        content=content,
        structure=structure,
        style=style,
        icons=icons,
        resolved_icons=resolved_icons,
        on_version=_make_version_callback(progress_callback),
    )
    kickoff = build_kickoff(
        task,
        analysis=analysis,
        structure=structure,
        style=style,
        icons=icons,
    )
    team_result = await run_studio(
        studio,
        kickoff,
        on_activity=_make_activity_callback(progress_callback),
    )

    return {
        "iterations": _versions_to_iterations(studio.versions),
        "svg": studio.current_svg,
        "validation": await _resolve_final_validation(
            studio, task, content, structure, style, resolved_icons
        ),
        "analysis": analysis,
        "content": content,
        "structure_raw": structure_raw,
        "structure": structure,
        "style": style,
        "icons": icons,
        "icon_diagnostics": icon_diagnostics,
        "best_attempt": len(studio.versions),
        "replacement_reason": str(team_result.get("stop_reason", "")),
        "candidate_comparisons": [],
        # 协作模式独有的字段：完整对话记录，UI 可据此展示两个 agent 说了什么
        "conversation": team_result.get("messages", []),
        "stop_reason": str(team_result.get("stop_reason", "")),
    }


def _make_version_callback(progress_callback: Any | None):
    """把工作台的「新版本」事件转成 UI 的实时预览回调。"""
    if progress_callback is None:
        return None

    def _on_version(entry: dict[str, Any]) -> None:
        progress_callback({
            "type": "svg",
            "attempt": int(entry.get("version", 0) or 0),
            "svg": str(entry.get("svg", "")),
            "status": str(entry.get("repair") or "submit"),
        })

    return _on_version


def _make_activity_callback(progress_callback: Any | None):
    """把协作过程中的进度描述转成界面能显示的事件。

    没有这个的话，从点击生成到第一版产出之间会有好几分钟一片空白 ——
    画图员要先思考、查图标，再输出整张 SVG。
    """
    if progress_callback is None:
        return None

    def _on_activity(text: str) -> None:
        progress_callback({"type": "activity", "text": text})

    return _on_activity


def _versions_to_iterations(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把工作台的版本历史转成 UI 与中间结果导出的既有权格式。"""
    total = len(versions)
    return [
        {
            "attempt": int(item.get("version", index + 1) or index + 1),
            "svg": str(item.get("svg", "")),
            "status": "accepted" if index == total - 1 else "superseded",
            "repair_mode": str(item.get("repair", "")),
            "note": str(item.get("note", "")),
        }
        for index, item in enumerate(versions)
    ]


async def _resolve_final_validation(
    studio: Studio,
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    resolved_icons: list[dict],
) -> dict[str, Any]:
    """给出最终校验结论。

    审查员如果已经跑过完整校验（含视觉模型），直接复用那份结果；
    否则补跑一次，保证 UI 拿到的 passed 字段是准确的。
    """
    last_issues = studio.last_issues or {}
    if "visual_status" in last_issues:
        return last_issues
    if not studio.current_svg:
        return {"passed": False, "hard_errors": ["协作结束但没有产出任何 SVG 版本"]}
    return await validate(
        studio.current_svg,
        task,
        content,
        structure,
        style,
        resolved_icons,
    )


async def _generate_with_legacy_loop(
    *,
    task: str,
    analysis: dict,
    content: list[dict],
    structure: dict,
    structure_raw: dict,
    style: dict,
    icons: list[dict],
    resolved_icons: list[dict],
    icon_diagnostics: dict[str, int],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """旧的固定重试循环：由 Python 代码决定重画几次、改哪一块。

    保留作为对照与回退路径，默认不再使用。
    """
    raw_svg = await generate(task, content, structure, style, icons)
    svg_code = embed_icon_symbols(raw_svg, resolved_icons, style)
    svg_code = enforce_resolved_icons(svg_code, resolved_icons, structure.get("stages", []))
    svg_code = normalize_connection_label_positions(svg_code, structure.get("connections", []))
    log_intermediate("CodeGenerator 初始 SVG", svg_code[:500] + "...")

    # 收集所有迭代 SVG 供 UI 显示
    iterations: list[dict[str, Any]] = []
    _iter_index = 0

    def _record_iter(svg: str, status: str) -> dict[str, Any]:
        nonlocal _iter_index
        _iter_index += 1
        entry: dict[str, Any] = {
            "attempt": _iter_index,
            "svg": svg,
            "status": status,
            "layout_revision": int(structure.get("layout_revision", 0) or 0),
        }
        iterations.append(entry)
        return entry

    def _update_iter(entry: dict[str, Any], status: str, val: dict[str, Any]) -> None:
        entry["status"] = status
        if val:
            entry["syntax_errors"] = val.get("syntax_errors", [])
            entry["structural_errors"] = val.get("structural_errors", [])
            entry["render_errors"] = val.get("render_errors", [])
            entry["hard_errors"] = val.get("hard_errors", [])
            entry["visual_suggestions"] = val.get("visual_suggestions", val.get("suggestions", []))
            entry["visual_feedback"] = val.get("visual_feedback", "")
            entry["visual_source"] = val.get("visual_source", "none")
            entry["feedback_source"] = val.get("feedback_source", "none")
            entry["visual_veto"] = val.get("visual_veto", False)
            entry["visual_defects"] = val.get("visual_defects", [])
            entry["visual_status"] = val.get("visual_status", "unavailable")
            entry["visual_error"] = val.get("visual_error", "")
            entry["visual_attempts"] = val.get("visual_attempts", 0)
            entry["visual_model"] = val.get("visual_model", "")
            entry["visual_elapsed_ms"] = val.get("visual_elapsed_ms", 0)
            entry["issues"] = val.get("issues", [])
            entry["resolved_issue_ids"] = val.get("resolved_issue_ids", [])
            entry["remaining_issue_ids"] = val.get("remaining_issue_ids", [])
            entry["new_issue_ids"] = val.get("new_issue_ids", [])
            entry["passed"] = val.get("passed", False)

    current_iter = _record_iter(svg_code, "initial")
    if progress_callback:
        progress_callback({"type": "svg", "attempt": 1, "svg": svg_code, "status": "initial"})

    # 7. 校验 + 迭代优化
    validation: dict[str, Any] = {"passed": False}
    prev_error_signature: str | None = None
    stall_count = 0
    best_svg: str | None = None
    best_validation: dict[str, Any] | None = None
    best_structure: dict[str, Any] | None = None
    best_structure_raw: dict[str, Any] | None = None
    best_attempt = 0
    replacement_reason = ""
    candidate_comparisons: list[dict[str, Any]] = []
    consecutive_rejected_candidates = 0
    terminal_validation: dict[str, Any] | None = None

    for attempt in range(MAX_RETRIES + 1):
        candidate_validation = await validate(
            svg_code,
            task,
            content,
            structure,
            style,
            resolved_icons,
        )
        validation = candidate_validation
        log_intermediate(f"Validator 第 {attempt + 1} 次结果", candidate_validation)

        incumbent_issue_ids = _blocking_issue_ids(best_validation)
        candidate_issue_ids = _blocking_issue_ids(candidate_validation)
        candidate_validation["resolved_issue_ids"] = sorted(incumbent_issue_ids - candidate_issue_ids)
        candidate_validation["remaining_issue_ids"] = sorted(candidate_issue_ids)
        candidate_validation["new_issue_ids"] = sorted(candidate_issue_ids - incumbent_issue_ids)

        if candidate_validation.get("visual_status") == "unavailable":
            current_iter["selected_as_best"] = best_svg is None
            current_iter["replacement_reason"] = "visual-validation-unavailable"
            _update_iter(current_iter, "failed (visual unavailable)", candidate_validation)
            terminal_validation = deepcopy(candidate_validation)
            if best_svg is None:
                best_svg = svg_code
                best_structure = deepcopy(structure)
                best_structure_raw = deepcopy(structure_raw)
                best_attempt = int(current_iter["attempt"])
            break

        accepted, decision_reason, comparison = await _select_better_candidate(
            incumbent_svg=best_svg,
            incumbent_validation=best_validation,
            candidate_svg=svg_code,
            candidate_validation=candidate_validation,
            task=task,
            structure=structure,
        )
        comparison_record = {
            "attempt": current_iter["attempt"],
            "accepted": accepted,
            "replacement_reason": decision_reason,
            **comparison,
            "resolved_issue_ids": candidate_validation["resolved_issue_ids"],
            "remaining_issue_ids": candidate_validation["remaining_issue_ids"],
            "new_issue_ids": candidate_validation["new_issue_ids"],
        }
        candidate_comparisons.append(comparison_record)
        current_iter["candidate_comparison"] = comparison_record
        current_iter["selected_as_best"] = accepted
        current_iter["replacement_reason"] = decision_reason
        if accepted:
            consecutive_rejected_candidates = 0
            best_svg = svg_code
            best_validation = deepcopy(candidate_validation)
            best_structure = deepcopy(structure)
            best_structure_raw = deepcopy(structure_raw)
            best_attempt = int(current_iter["attempt"])
            replacement_reason = decision_reason
            if progress_callback and current_iter["attempt"] != 1:
                progress_callback({
                    "type": "svg",
                    "attempt": current_iter["attempt"],
                    "svg": svg_code,
                    "status": "selected",
                })
        else:
            consecutive_rejected_candidates += 1

        if candidate_validation.get("passed") and accepted:
            _update_iter(current_iter, "passed", candidate_validation)
            break
        if attempt == MAX_RETRIES:
            final_status = "failed" if accepted else f"rejected ({decision_reason})"
            _update_iter(current_iter, final_status, candidate_validation)
            break

        retry_status = (
            f"rejected ({decision_reason})"
            if not accepted
            else (
                "retry (visual veto)"
                if candidate_validation.get("visual_veto")
                else f"retry ({len(candidate_validation.get('hard_errors', []))} errors)"
            )
        )
        _update_iter(current_iter, retry_status, candidate_validation)

        if best_svg is not None and best_validation is not None:
            svg_code = best_svg
            validation = deepcopy(best_validation)
            if best_structure is not None:
                structure = deepcopy(best_structure)
            if best_structure_raw is not None:
                structure_raw = deepcopy(best_structure_raw)

        # 诊断：按类别统计错误
        hard = validation.get("hard_errors", [])
        syntax = validation.get("syntax_errors", [])
        visual = validation.get("visual_feedback", "")
        logging.info(
            "[验证循环] 第 %d 次 — 语法错误 %d 个, 硬性错误 %d 个, 视觉反馈: %s",
            attempt + 1, len(syntax), len(hard),
            visual[:120] + "..." if len(visual) > 120 else visual,
        )
        # 汇总前 3 个硬性错误方便排查
        for i, e in enumerate(hard[:3], 1):
            logging.info("  [硬性错误 %d] %s", i, e)

        relayout_structure, relayout_changed = relayout_for_validation(
            structure,
            content,
            semantic_connections=analysis["connections"],
            validation=validation,
            attempt=attempt,
        )
        if relayout_changed:
            structure = relayout_structure
            prev_error_signature = None
            stall_count = 0
            consecutive_rejected_candidates = 0
            log_intermediate(f"LayoutEngine 第 {attempt + 1} 次重排结果", structure)
            retry_base_svg = best_svg if best_svg is not None else svg_code
            raw_svg = await regenerate(
                task,
                content,
                structure,
                style,
                retry_base_svg,
                validation,
                icons,
            )
            svg_code = embed_icon_symbols(raw_svg, resolved_icons, style)
            svg_code = enforce_resolved_icons(svg_code, resolved_icons, structure.get("stages", []))
            svg_code = normalize_connection_label_positions(svg_code, structure.get("connections", []))
            log_intermediate(f"CodeGenerator 基于重排版第 {attempt + 1} 次生成 SVG", svg_code[:500] + "...")
            current_iter = _record_iter(svg_code, "relayout")
            current_iter["repair_mode"] = "full-relayout"
            if progress_callback:
                progress_callback({"type": "svg", "attempt": current_iter["attempt"], "svg": svg_code, "status": "relayout"})
            continue

        # 检测停滞：如果连续两次错误签名相同，说明 LLM 无法修复此问题，提前退出
        current_sig = "|".join(sorted(_blocking_issue_ids(validation)))
        if current_sig == prev_error_signature:
            stall_count += 1
        else:
            stall_count = 0
        prev_error_signature = current_sig
        if stall_count >= 1:
            if not _blocking_issue_ids(validation):
                logging.info("[验证循环] 候选被拒且没有可定位阻断问题，停止无目标重试。")
                break
            logging.info("[验证循环] 检测到停滞，下一轮切换目标 stage 局部重生成。")

        # 给 regenerate 传递已嵌入图标的 svg_code（与 validator 看到的相同）
        retry_base_svg = best_svg if best_svg is not None else svg_code
        svg_code, repair_mode = await _repair_svg_with_patch_fallback(
            task,
            content,
            structure,
            style,
            retry_base_svg,
            validation,
            icons,
            force_full=consecutive_rejected_candidates >= 2 and not any(
                str(item.get("stage_id", "")).strip()
                for item in validation.get("issues", [])
                if isinstance(item, dict) and item.get("severity") == "blocking"
            ),
            force_stage_scope=stall_count >= 1 or consecutive_rejected_candidates >= 2,
        )
        if repair_mode == "full-after-regressions":
            consecutive_rejected_candidates = 0
        svg_code = embed_icon_symbols(svg_code, resolved_icons, style)
        svg_code = enforce_resolved_icons(svg_code, resolved_icons, structure.get("stages", []))
        svg_code = normalize_connection_label_positions(svg_code, structure.get("connections", []))
        log_intermediate(f"CodeGenerator 第 {attempt + 1} 次重试 SVG（{repair_mode}）", svg_code[:500] + "...")
        current_iter = _record_iter(svg_code, "retry")
        current_iter["repair_mode"] = repair_mode
        if progress_callback:
            progress_callback({"type": "svg", "attempt": current_iter["attempt"], "svg": svg_code, "status": "retry"})

    final_svg = best_svg if best_svg is not None else svg_code
    final_validation = terminal_validation or (best_validation if best_validation is not None else validation)
    final_structure = best_structure if best_structure is not None else structure
    final_structure_raw = best_structure_raw if best_structure_raw is not None else structure_raw
    return {
        "iterations": iterations,
        "svg": final_svg,
        "validation": final_validation,
        "analysis": analysis,
        "content": content,
        "structure_raw": final_structure_raw,
        "structure": final_structure,
        "style": style,
        "icons": icons,
        "icon_diagnostics": icon_diagnostics,
        "best_attempt": best_attempt,
        "replacement_reason": replacement_reason,
        "candidate_comparisons": candidate_comparisons,
    }
