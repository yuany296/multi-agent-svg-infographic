"""Streamlit 交互界面入口。"""

import asyncio
from datetime import datetime
import html
import logging
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from agents import generate_infographic


FINAL_ASSESSMENT_TASKS = (
    {
        "title": "任务 1：大语言模型",
        "prompt": "大语言模型的基本原理",
    },
    {
        "title": "任务 2：词向量",
        "prompt": "通俗易懂地解释词向量（Word Embedding）的基本概念",
    },
    {
        "title": "任务 3：中山大学",
        "prompt": "中山大学的发展历程",
    },
    {
        "title": "任务 4：咖啡生产链",
        "prompt": (
            "绘制 SVG 流程图，展示从一颗咖啡豆到一杯咖啡的完整生产链"
            "（种植、采摘、烘焙、研磨、冲煮）"
        ),
    },
    {
        "title": "任务 5：视频数量比较",
        "prompt": (
            "YouTube has 10 times more videos than TikTok, "
            "TikTok has 2 times more than Kuaishou"
        ),
    },
)

SVG_OUTPUT_ROOT = Path(__file__).resolve().parent / "output" / "svg"

# 同时输出到控制台，方便排查多 Agent 流水线中的中间结果
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

# 浅色简约主题，去掉默认的图标和侧栏感
st.set_page_config(
    page_title="多智能体 SVG 信息图生成器",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 用一点 CSS 让整体更干净、留白更舒服
st.markdown(
    """
    <style>
        .block-container { padding-top: 2.5rem; padding-bottom: 3rem; max-width: none; }
        h1 { font-weight: 600; letter-spacing: -0.02em; }
        .stTextArea textarea { border-radius: 8px; }
        .stButton > button { border-radius: 8px; }
        .stDownloadButton > button { border-radius: 8px; width: 100%; }
        .png-btn {
            display: inline-block;
            width: 100%;
            padding: 0.5rem 0.75rem;
            margin-top: 0.5rem;
            border: 1px solid rgba(49,51,63,0.2);
            border-radius: 8px;
            background: #ffffff;
            color: #31333f;
            font-size: 0.875rem;
            font-weight: 400;
            cursor: pointer;
            text-align: center;
        }
        .png-btn:hover { border-color: #31333f; color: #31333f; }
    </style>
    """,
    unsafe_allow_html=True,
)

# 前端 JS：把 SVG 字符串画到 canvas 再导出 PNG，零依赖、保真度与浏览器一致
_PNG_EXPORT_SCRIPT = """
<script>
function downloadSvgAsPng(svgSrcId, filename) {
    const src = document.getElementById(svgSrcId);
    if (!src) { return; }
    let svgText = src.textContent;

    // 浏览器用 Image 对象加载 SVG 必须要有 xmlns，部分 LLM 输出可能遗漏
    if (!svgText.includes('xmlns="http://www.w3.org/2000/svg"') &&
        !svgText.includes("xmlns='http://www.w3.org/2000/svg'")) {
        svgText = svgText.replace(/(<svg\b)([^>]*>)/i, '$1 xmlns="http://www.w3.org/2000/svg"$2');
    }

    // 优先从 viewBox 或 width/height 属性解析真实尺寸，避免 naturalWidth 为 0
    let w = 1200, h = 800;
    const vb = svgText.match(/viewBox=["']([\\d\\.\\-\\s]+)["']/i);
    if (vb) {
        const parts = vb[1].trim().split(/[\\s,]+/);
        if (parts.length === 4) { w = parseFloat(parts[2]); h = parseFloat(parts[3]); }
    }
    const wm = svgText.match(/<svg[^>]*\\swidth=["']([\\d.]+)/i);
    const hm = svgText.match(/<svg[^>]*\\sheight=["']([\\d.]+)/i);
    if (wm) { w = parseFloat(wm[1]); }
    if (hm) { h = parseFloat(hm[1]); }
    const blob = new Blob([svgText], {type: 'image/svg+xml;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = function () {
        if (img.naturalWidth) { w = img.naturalWidth; }
        if (img.naturalHeight) { h = img.naturalHeight; }
        const scale = 2;  // 2 倍像素密度，保证插入 LaTeX 后清晰
        const canvas = document.createElement('canvas');
        canvas.width = w * scale;
        canvas.height = h * scale;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (b) {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(b);
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);
        }, 'image/png');
    };
    img.onerror = function () { URL.revokeObjectURL(url); alert('PNG 导出失败：SVG 无法渲染'); };
    img.src = url;
}
</script>
"""


def _new_run_id(prefix: str) -> str:
    """生成不会覆盖历史结果的运行目录名。"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-{timestamp}"


def _visual_status_notice(
    validation: dict,
    *,
    subject: str = "",
) -> tuple[str, str]:
    """把视觉检查三态映射为统一、温和的前端提示。"""
    prefix = f"{subject}：" if subject else ""
    visual_status = str(validation.get("visual_status", ""))
    if visual_status == "passed":
        model = str(validation.get("visual_model", "")).strip()
        attempts = int(validation.get("visual_attempts", 0) or 0)
        detail = f"（{model}，调用 {attempts} 次）" if model else ""
        return "success", f"{prefix}视觉检查通过{detail}。"
    if visual_status == "rejected":
        return "warning", f"{prefix}视觉检查发现可改进项，系统已保留当前最佳结果。"
    if visual_status == "unavailable":
        return (
            "warning",
            f"{prefix}视觉检查暂不可用，已保留当前最佳结果；基础校验结果仍可查看。",
        )
    return "", ""


def _save_svg(
    svg_code: str,
    *,
    run_id: str,
    filename: str,
) -> tuple[str | None, str | None]:
    """把非空 SVG 保存到运行目录，返回绝对路径与错误信息。"""
    if not svg_code or not svg_code.strip():
        return None, "SVG 内容为空，未写入文件。"

    try:
        run_dir = SVG_OUTPUT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / filename
        output_path.write_text(svg_code, encoding="utf-8")
        return str(output_path.resolve()), None
    except OSError as exc:
        logging.exception("SVG 保存失败：%s/%s", run_id, filename)
        return None, str(exc)


def _save_iteration_svg(
    svg_code: str,
    *,
    run_id: str,
    prefix: str,
    attempt: int,
) -> None:
    """每轮 SVG 生成后立刻落盘，不等待 pipeline 结束。"""
    if not svg_code or not svg_code.strip():
        return
    try:
        run_dir = SVG_OUTPUT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        it_path = run_dir / f"{prefix}-attempt-{attempt}.svg"
        if not it_path.exists():
            it_path.write_text(svg_code, encoding="utf-8")
    except OSError:
        pass


def _persist_result(result: dict, *, run_id: str, filename: str) -> dict:
    """幂等保存一个结果；已有且仍存在的文件不会重复写入。"""
    saved_path = result.get("svg_path")
    if saved_path and Path(saved_path).is_file():
        return result

    svg_path, save_error = _save_svg(
        result.get("svg", ""),
        run_id=run_id,
        filename=filename,
    )
    result["svg_path"] = svg_path
    result["save_error"] = save_error
    # 保存中间报告 JSON
    if svg_path:
        json_path = Path(svg_path).with_suffix(".json")
        if not json_path.exists():
            try:
                json_path.write_text(
                    _build_intermediate_report(result),
                    encoding="utf-8",
                )
                result["report_path"] = str(json_path)
            except OSError:
                pass
        # 保存每轮迭代的 SVG 和 JSON
        run_dir = Path(svg_path).parent
        for it in (result.get("iterations") or []):
            attempt = it.get("attempt", 0)
            it_svg = it.get("svg", "")
            if it_svg:
                prefix = Path(filename).stem  # e.g. "01" or "single-infographic"
                it_path = run_dir / f"{prefix}-attempt-{attempt}.svg"
                if not it_path.exists():
                    try:
                        it_path.write_text(it_svg, encoding="utf-8")
                    except OSError:
                        pass
    return result


def _recover_session_results() -> None:
    """补存代码更新前已在当前 Streamlit 会话中生成的结果。"""
    result = st.session_state.get("result")
    if isinstance(result, dict):
        run_id = st.session_state.setdefault(
            "single_run_id",
            _new_run_id("recovered-single"),
        )
        _persist_result(
            result,
            run_id=run_id,
            filename="single-infographic.svg",
        )

    batch_results = st.session_state.get("batch_results")
    if isinstance(batch_results, list) and batch_results:
        run_id = st.session_state.setdefault(
            "batch_run_id",
            _new_run_id("recovered-batch"),
        )
        for index, item in enumerate(batch_results, start=1):
            item_result = item.get("result") if isinstance(item, dict) else None
            if isinstance(item_result, dict):
                _persist_result(
                    item_result,
                    run_id=run_id,
                    filename=f"{index:02d}.svg",
                )
        st.session_state["batch_results"] = batch_results


def _download_buttons(
    svg_code: str,
    *,
    component_key: str,
    filename_stem: str,
) -> None:
    """生成 SVG 下载按钮 + PNG 下载按钮（PNG 由前端 JS 用浏览器渲染导出）。"""
    # 隐藏元素：存一份转义后的 SVG 原文，供 JS 读取后转 PNG
    svg_escaped = html.escape(svg_code)
    source_id = f"svg-src-{component_key}"
    components.html(
        f"""
        <div id="{source_id}" style="display:none;">{svg_escaped}</div>
        {_PNG_EXPORT_SCRIPT}
        <button class="png-btn" onclick="downloadSvgAsPng('{source_id}', '{filename_stem}.png')">
            下载 PNG 图片
        </button>
        """,
        height=56,
    )
    # SVG 下载用原生组件（服务端直接给文件）
    st.download_button(
        label="下载 SVG 文件",
        data=svg_code.encode("utf-8"),
        file_name=f"{filename_stem}.svg",
        mime="image/svg+xml",
        key=f"download-svg-{component_key}",
    )


_VERSION_STATUS_LABELS = {
    "initial": "初始版",
    "retry": "重试版",
    "relayout": "重排版",
    "submit": "提交版",
    "patch": "补丁版",
}


async def _run_pipeline(task: str, *, run_id: str, status=None) -> dict:
    intermediate_svgs: list[dict] = []

    def on_progress(event):
        event_type = event.get("type")

        # 协作过程提示：画图员在思考、查图标、审查员在检查……
        # 没有这些的话，从点击到第一版产出之间会有好几分钟一片空白。
        if event_type == "activity":
            if status:
                status.update(label=f"协作中 —— {event['text']}")
            return

        if event_type != "svg":
            return

        intermediate_svgs.append(event)
        _save_iteration_svg(
            event["svg"],
            run_id=run_id,
            prefix="single-infographic",
            attempt=event["attempt"],
        )
        if status:
            label = _VERSION_STATUS_LABELS.get(
                str(event.get("status", "")), str(event.get("status", ""))
            )
            status.write(f"**第 {event['attempt']} 版 SVG 已生成（{label}）**")
            status.image(event["svg"], width=600)

    result = await generate_infographic(task, progress_callback=on_progress)

    # 如果有多轮，在 status 里汇总
    if status and len(intermediate_svgs) > 1:
        n = len(intermediate_svgs)
        status.write(f"---\n共生成 **{n}** 轮 SVG，最终轮提交。")

    return result


def _create_batch_live_slots(tasks: tuple[dict[str, str], ...]) -> list[dict]:
    """为批量任务创建实时 SVG 预览占位。"""
    st.subheader("批量实时中间 SVG")
    slots: list[dict] = []
    for index, task_item in enumerate(tasks, start=1):
        with st.expander(f"{index}. {task_item['title']}", expanded=index == 1):
            st.caption(task_item["prompt"])
            state = st.empty()
            latest = st.empty()
            history = st.container()
            slots.append({"state": state, "latest": latest, "history": history})
    return slots


def _render_batch_live_svg(slot: dict, task_title: str, event: dict) -> None:
    """把某一题的最新 SVG 和历史轮次写入实时预览区。"""
    label = (
        f"第 {event['attempt']} 轮"
        + ("（初始版）" if event["status"] == "initial" else f"（{event['status']}）")
    )
    slot["state"].info(f"{task_title}：{label}")
    with slot["latest"].container():
        st.markdown(f"**最新 SVG：{label}**")
        st.image(event["svg"], width=650)
    with slot["history"]:
        st.markdown(f"**{label}**")
        st.image(event["svg"], width=500)


async def _run_batch(
    tasks: tuple[dict[str, str], ...],
    progress,
    status,
    run_id: str,
    live_slots: list[dict] | None = None,
) -> list[dict]:
    """顺序生成一组任务；单题失败时保留错误并继续后续任务。"""
    results: list[dict] = []
    total = len(tasks)

    for index, task_item in enumerate(tasks, start=1):
        live_slot = live_slots[index - 1] if live_slots and index - 1 < len(live_slots) else None
        if live_slot:
            live_slot["state"].info(f"{task_item['title']}：等待生成...")
        status.write(
            f"⏳ {task_item['title']}（{index}/{total}）："
            "正在执行多 Agent 流程，校验失败时会自动重画。"
        )
        # 每道题独立收集中间 SVG
        task_svgs: list[dict] = []
        task_prefix = f"{index:02d}"
        def on_task_progress(
            event,
            svgs=task_svgs,
            slot=live_slot,
            title=task_item["title"],
            _rid=run_id,
            _pfx=task_prefix,
        ):
            event_type = event.get("type")

            # 协作过程提示：让这一题的实时区在等待期间也有反馈
            if event_type == "activity":
                if slot:
                    slot["state"].info(f"{title}：{event['text']}")
                return

            if event_type != "svg":
                return

            svgs.append(event)
            _save_iteration_svg(
                event["svg"],
                run_id=_rid,
                prefix=_pfx,
                attempt=event["attempt"],
            )
            status_label = _VERSION_STATUS_LABELS.get(
                str(event.get("status", "")), str(event.get("status", ""))
            )
            label = f"第 {event['attempt']} 版 SVG 已生成（{status_label}）"
            status.write(f"{title}：{label}")
            if slot:
                _render_batch_live_svg(slot, title, event)
            else:
                status.image(event["svg"], width=500)
        try:
            result = await generate_infographic(
                task_item["prompt"],
                progress_callback=on_task_progress,
            )
            _persist_result(
                result,
                run_id=run_id,
                filename=f"{index:02d}.svg",
            )
            results.append({**task_item, "result": result, "error": None})
            validation = result.get("validation", {})
            if validation.get("passed"):
                status.write(f"✅ {task_item['title']}：生成并校验通过。")
                if live_slot:
                    live_slot["state"].success(f"{task_item['title']}：生成并校验通过。")
            else:
                notice_level, notice_message = _visual_status_notice(
                    validation,
                    subject=task_item["title"],
                )
                message = notice_message or f"{task_item['title']}：生成完成，但最终校验未通过。"
                status.write(f"⚠️ {message}")
                if live_slot:
                    live_slot["state"].warning(message)
        except Exception as exc:
            logging.exception("批量任务生成失败：%s", task_item["title"])
            results.append({**task_item, "result": None, "error": str(exc)})
            status.write(f"❌ {task_item['title']}：{exc}")
            if live_slot:
                live_slot["state"].error(f"{task_item['title']}：{exc}")
        # 每题结束就做会话检查点，后续任务卡住或失败时仍可恢复已有结果。
        st.session_state["batch_results"] = list(results)
        progress.progress(index / total)

    return results


def _build_intermediate_report(result: dict) -> str:
    """把中间结果打包成一个结构化的 JSON 字符串，方便导出后给 AI 分析。"""
    import json

    report = {
        "analysis": result.get("analysis", result.get("content")),
        "icon_resolver": {
            "diagnostics": result.get("icon_diagnostics"),
            "icons": result.get("icons"),
        },
        "structure_planner_raw": result.get("structure_raw"),
        "layout_engine_coordinates": result.get("structure"),
        "style_designer": result.get("style"),
        "validator": result.get("validation"),
        "stop_reason": result.get("stop_reason", ""),
        "conversation": result.get("conversation", []),
        "iterations": [
            {
                "attempt": it.get("attempt"),
                "status": it.get("status"),
                "svg_length": len(it.get("svg", "")),
                "passed": it.get("passed"),
                "syntax_errors": it.get("syntax_errors", []),
                "hard_errors": it.get("hard_errors", []),
                "visual_feedback": it.get("visual_feedback", ""),
                "visual_veto": it.get("visual_veto", False),
                "visual_defects": it.get("visual_defects", []),
                "visual_status": it.get("visual_status", "unavailable"),
                "visual_source": it.get("visual_source", "none"),
                "visual_error": it.get("visual_error", ""),
                "visual_attempts": it.get("visual_attempts", 0),
                "visual_model": it.get("visual_model", ""),
                "visual_elapsed_ms": it.get("visual_elapsed_ms", 0),
                "issues": it.get("issues", []),
                "resolved_issue_ids": it.get("resolved_issue_ids", []),
                "remaining_issue_ids": it.get("remaining_issue_ids", []),
                "new_issue_ids": it.get("new_issue_ids", []),
                "candidate_comparison": it.get("candidate_comparison", {}),
                "selected_as_best": it.get("selected_as_best", False),
                "replacement_reason": it.get("replacement_reason", ""),
            }
            for it in (result.get("iterations") or [])
        ],
    }
    return json.dumps(report, ensure_ascii=False, indent=2)


def _tool_names(content) -> list:
    """从工具调用消息里取出工具名，便于界面显示。"""
    if not isinstance(content, list):
        return []
    return [
        str(item.get("name"))
        for item in content
        if isinstance(item, dict) and item.get("name")
    ]


def _render_conversation(result: dict) -> None:
    """展示「画图员 ⇄ 审查员」两个 agent 的完整对话记录。

    这是协作模式的核心证据：谁说了什么、谁调用了什么工具、谁做的决定。
    旧流程没有这份记录。
    """
    conversation = result.get("conversation") or []
    if not conversation:
        st.caption("本次生成没有协作记录（可能走的还是旧的固定重试流程）。")
        return

    stop_reason = str(result.get("stop_reason", "") or "")
    if stop_reason:
        st.caption(f"结束原因：{stop_reason}")

    role_names = {"Painter": "画图员", "Reviewer": "审查员", "user": "开场简报"}
    for message in conversation:
        if not isinstance(message, dict):
            continue
        kind = str(message.get("type", ""))
        source = str(message.get("source", ""))
        role = role_names.get(source, source)
        content = message.get("content", "")

        if kind == "ThoughtEvent":
            with st.expander(f"· {role}的思考（点击展开）", expanded=False):
                st.caption(str(content))
        elif kind == "TextMessage":
            st.markdown(f"**{role}**")
            st.write(str(content))
        elif kind == "ToolCallRequestEvent":
            names = _tool_names(content)
            if names:
                st.caption(f"{role} → 调用工具：{', '.join(names)}")
        elif kind == "ToolCallExecutionEvent":
            names = _tool_names(content)
            if "approve" in names:
                st.success(f"{role} 宣布交付，协作结束。")
            elif names:
                preview = str(content)[:160]
                st.caption(f"{role} ← 工具返回：{preview}")


def _render_intermediate_results(result: dict) -> None:
    """展示单个任务的五阶段中间结果。"""
    with st.expander("查看中间结果"):
        st.download_button(
            label="📥 下载中间结果 (JSON)",
            data=_build_intermediate_report(result).encode("utf-8"),
            file_name="intermediate-report.json",
            mime="application/json",
            key=f"download-intermediate-{id(result)}",
            use_container_width=True,
        )
        (
            tab_conversation,
            tab_content,
            tab_icons,
            tab_structure,
            tab_layout,
            tab_style,
            tab_validation,
            tab_iterations,
        ) = st.tabs(
            ["协作记录", "内容分析", "图标解析", "结构规划", "布局计算", "样式设计", "校验结果", "迭代轮次"]
        )

        with tab_conversation:
            _render_conversation(result)

        with tab_content:
            st.json(result.get("analysis", result.get("content")))

        with tab_icons:
            st.markdown("**IconResolver 命中来源（不产生 LLM 调用）**")
            st.json(result.get("icon_diagnostics", {}))
            st.json(result.get("icons", []))

        with tab_structure:
            st.markdown("**StructurePlanner 原始输出（布局意图，不含坐标）**")
            st.json(result.get("structure_raw"))

        with tab_layout:
            st.markdown("**LayoutEngine 计算后的实际坐标**")
            st.json(result.get("structure"))

        with tab_style:
            st.json(result.get("style"))

        with tab_validation:
            validation = result.get("validation", {})
            notice_level, notice_message = _visual_status_notice(validation)
            if notice_level == "success":
                st.success(notice_message)
            elif notice_level == "warning":
                st.warning(notice_message)
            visual_defect_lines = []
            for defect in validation.get("visual_defects", []):
                if not isinstance(defect, dict):
                    continue
                visual_defect_lines.append(
                    f"- {defect.get('location', '画面位置')}：{defect.get('issue', '视觉问题')}"
                    f"；原因：{defect.get('reason', '')}；建议：{defect.get('fix', '')}"
                )
            if visual_defect_lines:
                st.warning("视觉检查发现以下可改进项：\n\n" + "\n".join(visual_defect_lines))
            if validation.get("syntax_errors"):
                st.error("语法错误：")
                for err in validation["syntax_errors"]:
                    st.write(f"- {err}")
            if validation.get("hard_errors"):
                st.error("硬性坐标错误（代码已检测）：")
                for err in validation["hard_errors"]:
                    st.write(f"- {err}")
            if not validation.get("syntax_errors") and not validation.get("hard_errors"):
                st.success("无语法错误与硬性坐标错误。")
            st.json(validation)

        with tab_iterations:
            its = result.get("iterations") or []
            if not its:
                st.caption("无迭代记录")
            else:
                selected_its = [it for it in its if it.get("selected_as_best", False)]
                display_its = selected_its or its[:1]
                if len(display_its) < len(its):
                    st.caption(f"已隐藏 {len(its) - len(display_its)} 个未入选候选稿；完整记录仍保留在中间 JSON 中。")
                labels = [f"第 {it['attempt']} 轮 ({it['status']})" for it in display_its]
                it_tabs = st.tabs(labels)
                for idx, it_tab in enumerate(it_tabs):
                    with it_tab:
                        svg = display_its[idx].get("svg", "")
                        if svg and svg.strip():
                            try:
                                st.image(svg, width=700)
                            except Exception as exc:
                                st.error(f"预览失败：{exc}")
                        else:
                            st.caption("SVG 为空")


def _render_result(
    result: dict,
    *,
    component_key: str,
    filename_stem: str,
    show_heading: bool = True,
) -> None:
    """展示一个完整生成结果，供单题模式和批量模式共同复用。"""
    svg_code = result.get("svg", "")
    validation = result.get("validation", {})

    if validation.get("passed"):
        st.success("校验通过，已生成最终 SVG。")
    else:
        st.warning("已达到最大迭代次数，SVG 可能仍存在小问题。")

    if show_heading:
        st.subheader("生成的信息图")

    if svg_code and svg_code.strip():
        try:
            # Streamlit 1.30+ 原生支持内联 SVG，并会自动转为 data URI。
            st.image(svg_code)
        except Exception as exc:
            st.error(f"SVG 网页预览失败：{exc}")
    else:
        st.warning("SVG 内容为空，无法在网页中预览。")

    if result.get("save_error"):
        st.error(f"SVG 自动保存失败：{result['save_error']}")
    elif result.get("svg_path"):
        st.caption(f"已自动保存：`{result['svg_path']}`")

    if svg_code:
        _download_buttons(
            svg_code,
            component_key=component_key,
            filename_stem=filename_stem,
        )

    _render_intermediate_results(result)


def _clear_results() -> None:
    """清除单题、批量生成结果以及单题输入。"""
    for key in (
        "result",
        "batch_results",
        "single_run_id",
        "batch_run_id",
        "task",
    ):
        st.session_state.pop(key, None)


def main() -> None:
    # 代码更新前已经生成、但尚未落盘的当前会话结果会在这里自动补存。
    _recover_session_results()

    st.title("多智能体 SVG 信息图生成系统")
    from utils.visual_eval import QWEN_VL_API_KEY, QWEN_VL_MODEL
    if QWEN_VL_API_KEY:
        st.caption(f"视觉检查：已配置（{QWEN_VL_MODEL}）；检查结果会用于后续迭代优化。")
    else:
        st.warning(
            "视觉检查暂未配置：可设置 QWEN_VL_KEY 或 QWEN_KEY；"
            "任务仍会生成结果并展示基础校验信息。"
        )
    st.markdown(
        "输入任务需求，系统将依次调用 "
        "**ContentAnalyzer → IconResolver → StructurePlanner → LayoutEngine → "
        "StyleDesigner → CodeGenerator → IconEmbedder → Validator** 生成 SVG。"
    )

    generation_mode = st.radio(
        "生成模式",
        ("单题生成", "期末五题批量生成"),
        horizontal=True,
    )

    task = ""
    if generation_mode == "单题生成":
        task = st.text_area(
            "任务需求",
            placeholder="例如：制作一张手机充电步骤的信息图",
            height=120,
            key="task",
        )
    else:
        st.markdown("一次点击将按顺序生成以下五个期末测试任务：")
        for task_item in FINAL_ASSESSMENT_TASKS:
            st.markdown(f"- **{task_item['title']}**：{task_item['prompt']}")
        st.caption(
            "任务会顺序执行，某一题失败不会中断其他题目；"
            "每题校验失败时最多重试 2 次，因此整批生成可能需要几分钟。"
        )

    col1, col2 = st.columns([1, 6])
    with col1:
        button_label = "生成 SVG" if generation_mode == "单题生成" else "生成五题"
        generate_clicked = st.button(button_label, type="primary")
    with col2:
        st.button("清空", on_click=_clear_results)

    # 生成
    if generate_clicked:
        if generation_mode == "单题生成":
            if not task or not task.strip():
                st.warning("请输入任务需求。")
            else:
                with st.status("正在生成 SVG，过程中会逐步展示中间结果...", expanded=True) as status:
                    try:
                        run_id = _new_run_id("single")
                        st.session_state["single_run_id"] = run_id
                        result = asyncio.run(_run_pipeline(task, run_id=run_id, status=status))
                        _persist_result(
                            result,
                            run_id=run_id,
                            filename="single-infographic.svg",
                        )
                        st.session_state["result"] = result
                        status.update(
                            label="生成完成！",
                            state="complete",
                            expanded=True,
                        )
                    except Exception as exc:
                        st.error(f"生成失败：{exc}")
                        st.session_state.pop("result", None)
        else:
            run_id = _new_run_id("batch")
            st.session_state["batch_run_id"] = run_id
            st.session_state["batch_results"] = []
            progress = st.progress(0.0)
            live_slots = _create_batch_live_slots(FINAL_ASSESSMENT_TASKS)
            with st.status("正在生成期末五题...", expanded=True) as status:
                batch_results = asyncio.run(
                    _run_batch(
                        FINAL_ASSESSMENT_TASKS,
                        progress,
                        status,
                        run_id,
                        live_slots,
                    )
                )
                succeeded = sum(not item["error"] for item in batch_results)
                status.update(
                    label=f"批量生成完成：{succeeded}/{len(batch_results)} 个任务成功。",
                    state="complete" if succeeded == len(batch_results) else "error",
                    expanded=True,
                )
            st.session_state["batch_results"] = batch_results

    # 展示结果
    if generation_mode == "单题生成":
        result = st.session_state.get("result")
        if result:
            _render_result(
                result,
                component_key="single",
                filename_stem="infographic",
            )
    else:
        batch_results = st.session_state.get("batch_results", [])
        if batch_results:
            st.subheader("五题生成结果")
            result_tabs = st.tabs([item["title"] for item in batch_results])
            for index, (tab, item) in enumerate(
                zip(result_tabs, batch_results),
                start=1,
            ):
                with tab:
                    st.caption(item["prompt"])
                    if item["error"]:
                        st.error(f"生成失败：{item['error']}")
                        continue
                    _render_result(
                        item["result"],
                        component_key=f"batch-{index}",
                        filename_stem=f"final-task-{index}",
                        show_heading=False,
                    )


if __name__ == "__main__":
    main()
