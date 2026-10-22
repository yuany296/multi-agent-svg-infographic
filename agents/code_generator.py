"""CodeGenerator —— 生成结构化教学信息图 SVG。"""

from utils.config import get_model_client
from utils.llm import call_llm
from utils.svg import apply_unified_diff, extract_svg


_SYSTEM_PROMPT = """你是一名 SVG 教学示意图代码生成专家。根据内容分析、结构规划、样式和图标清单生成完整静态 SVG。

【输出要求】
1. 只输出 ```svg 代码块中的 <svg>...</svg>，width/height 必须是数值，禁止 100%/auto
2. 不使用外部图片、CSS、字体或 URL；文字使用 Microsoft YaHei, Arial, sans-serif
3. 画面应是流程图、机制图、系统图、对比图、时间线或分镜面板，不是普通文字卡片

【图画优先：教学解释必须先看图再读字】
- mechanism/system 的 storyboard panel 中，scene_area 是主要解释区，必须绘制真实图形关系；文字只做短标签，不能用彩色文字条代替图画。
- 每个 scene 至少包含 3 个可辨识的图形元素，并让图形占 scene_area 的主要视觉面积。
- 场景图形应在 scene_area 内居中并使用至少约 60% 的宽度与高度；不得只挤在顶部一小块后留下大面积空白。
- 禁止把 scene_area 画成 2-4 个铺满宽度、只写一个词的彩色圆角矩形；如果使用 rect，只能作为结构容器或图形底托，并且必须和其他有意义的形状组合。
- 依据每个节点的 visual_brief、标题、说明和可用图标自由选择视觉隐喻；不得因为术语相同而复用固定图案。

【色彩与视觉风格】
- 色彩清爽统一：以白底、浅蓝、浅青、浅琥珀和灰色为主，主题色不超过 4 种；每个面板只使用一个主色，其他颜色只作小面积强调
- 禁止彩虹式配色、粉紫色同时出现、荧光色、强渐变和大面积高饱和填充；section_tokens 是唯一的分区配色来源
- 标题深色(#111827)，正文中灰(#374151)，注释浅灰(#6b7280)，高对比度确保清晰

【排版与留白】
- 总标题 24-34px bold，节点标题 18-22px semibold，说明 12-16px regular
- 画布边缘留 32-48px padding，模块间距 80-120px，内容与边框 >=12px

【布局边界】
- stage.x/y/w/h 是安全区，非 cards 类型禁止直接把安全区画成大白色矩形
- structure.header 是总标题区；每个 stage 的 slots 是内容槽位，互不重叠
- 非 cards 类型用 stage.node_geometry 绘制节点主体，连接线可跨安全区
- 箭头连接节点边缘，连接标签在箭头旁（偏移 8-12px），不压在线上

【模板契约】
- structure.template_id / template_slots / render_contract 是硬约束
- 非 cards 必须按模板槽位绘制：
  . flow-path -> 从左到右排列，相邻节点间距 80-100px
    每个 stage 按 node_shape 画唯一节点主体：circle-icon 画圆形，terminator-pill 画胶囊，decision-diamond 画菱形；内部图标+标题+短说明
    flow-path 的 slots.icon 图形必须直接画在节点主体内，禁止再画 80-90px 的圆角方形图标底板，避免方块遮挡圆形节点
    SVG 绘制顺序必须先画节点主体/白底/描边，再画 data-role="icon" 图标组；禁止在图标组之后再画会覆盖图标的大块不透明 rect/circle/path
    图标组使用 transform="translate(icon_x, icon_y)" 定位，内部路径/形状全部用 (0,0) 相对坐标
    禁止画 pipeline 背景粗线/管道线；节点间用独立箭头连接即可
    禁止画虚线安全区边框、编号小圆点、多余的装饰框；只保留节点主体一个层次
  . timeline-axis -> 水平时间轴 + 事件卡片，stage 是 milestone
    axis 只绘制主线和 14-18px marker 点；marker 点只负责定位/连接，内部禁止放图标、年份或文字
    每个 milestone 必须按 event_card_slot/card_slot 绘制事件卡片
    title 写"年份+事件名称"（如"1924 创校"），scene 只画图形不写年份文字，description 展开说明但不重复年份
    有 slots.scene 时：卡片上半部 scene 区域绘制组合图形（每个 scene >=3 个图形元素，占 scene 槽 >=60%），下方放 title/description/metric
    无 slots.scene 时：图标放 icon_slot（>=24x24），标题/说明放 title/description/metric 槽
    禁止把 safe_area 画成整列大色块或虚线框，禁止把图标放到轴点上、年份圆章内或卡片外
  . comparison-chart -> 图表区 + comparison-bar 比例条
	    柱子直接使用 stage.bar_slot 的精确坐标（x/y/w/h），必须加 class="comparison-bar"；严禁自己按 metric.value 换算高度
	    倍数连接从低柱指向高柱（乘法语义：小×N=大），用二次贝塞尔曲线（Q）从低柱顶部右侧弯向高柱顶部左侧
	    连接标签紧贴曲线、放在弧顶正上方 6-12px，必须作为 connection.label_slot 内容；严禁将标签放在画布顶部或柱子顶部远处
	    y 轴刻度必须使用各 stage metric.label 中的实际值作为标注（如 1x/2x/20x），禁止自行编造均匀刻度；基准线必须与 metric 值一致
  . flow-mechanism -> 机制主流程，反馈箭头用弯曲虚线
  . mechanism-storyboard -> 分镜面板：panel_rect 画大面板，scene_area 画小场景
  . section-module-map -> 分区容器，stage 是模块/矩阵/管道/状态块

【箭头规则】
- 箭头长度约为节点宽度/高度的 1/3 到 1/2，不要过长
- 主流程用实线，反馈/循环用虚线，并行/关联用曲线
- 每条连接: <g class="semantic-connection" data-from="..." data-to="..." data-type="..." data-label="...">
- 有 connection.label 时，必须在该连接组内放且只放一个 <g data-role="connection-label">；标签文字和可选底托必须完全落在 connection.label_slot 内。
- connection.label_slot 的 x/y 是左上角坐标；如果使用 translate(x,y)，标签背景应从局部 (0,0) 开始，不能把 slot 的左上角当成标签中心。
- mechanism-storyboard 的 template_slots.label_lanes 是标签专用通道：文字只能绘制在对应通道和 label_slot 中，必须离所属箭头 8-24px，绝不能放进 header、panel 或画布顶部空白区。
- connection.label 为空时，禁止生成连接标签文字或 data-role="connection-label" 组。
- 必须在 <defs> 中定义箭头 marker

【元素标记】
- 节点: <g class="stage-node" data-stage-id="...">，标题、说明、数值和场景分别用 data-role="title|description|metric|scene"；仅存在 slots.icon 时才创建独立 data-role="icon" 组；stage 没有 metric 或 metric 为 null 时禁止绘制 data-role="metric" 组
- **坐标规则（严禁违反）**：任何带 data-role 的 <g> 组（icon、scene、title、description、metric）必须使用 transform="translate(x, y)" 定位，组内所有元素严格使用相对 (0,0) 的局部坐标；禁止在组内使用全局绝对坐标
- **严禁使用 clip-path**：禁止在 <defs> 中定义 clipPath 或在任何元素上引用 clip-path，它会导致 transform+全局坐标叠加时内容被裁切
- text 显式 font-size，长文本用 tspan 换行，禁止省略号(...)
- icon 图形填满 slot 主要区域(>=60%)；深色背景上加浅色底托
- 图标层级规则：所有节点背景、白色底板、描边和装饰底托必须出现在 data-role="icon" 之前；图标组之后只能画文字、细连接线或不会覆盖图标的小标记
- 有 slots.icon/icon_slot 的图标必须填满主要区域且尺寸至少 24x24px；timeline-axis 图标必须在事件卡片内，marker 点内不得放图标、年份或文字
- **【强制】场景内元素零遮挡**：图标、文字、装饰图形三者之间必须保持至少 6px 间距。装饰性图形（星星、徽章、圆点）不能覆盖或遮挡任何文字、数值或图标。同一 scene 内所有元素的包围盒不得重叠。如空间紧张，优先缩小装饰而非挤压文字。

【mechanism-storyboard 专项】
- visual_brief 说明场景的语义目标，可自由设计对应图形，不得把它解释为固定模板。
- 不是每个 stage 都必须画流程；只有原文确实表达连续步骤或转换链时才使用内部箭头
- scene_area 以图形为主，文字只作短标签；不得自行补”步骤2/步骤3/样本2/样本3/第2步”等占位文案
- 通用视觉元素必须作为 scene_area 内同一幅图的组成部分，禁止额外画独立小图标卡、角标或图例来重复场景内容。
- kind=general / icon_query 只是启发主场景构图，不是必须显示的角标；禁止在 scene 四角或边缘叠加 eye、target、network、file 等独立小图标。
- 至少一个 scene 含 >=3 个内部元素
- **【一票否决】scene 内元素禁止重叠或遮挡**：图标不能压住文字，文字不能互相覆盖，星星/徽章/装饰不能遮挡标题或数值。每个元素必须有独立的视觉空间，间距至少 6-8px。如果你不确定是否重叠，宁可扩大间距或缩小元素。

【图标】
- kind=brand 且 render_mode=symbol -> 必须在独立 icon 槽使用 <use href="#图标id">
- kind=brand 且 render_mode=brand_badge -> 在 icon 槽内绘制中性的文字徽章并加 data-brand-badge="图标id"；禁止 <use>、data-generated-icon 或任何品牌 Logo 轮廓
- kind=general 的 symbol 和 llm_general 都是场景素材提示：可在 scene_area 内组合使用，也可直接按 visual_brief 自由绘制更贴切的整体图形；禁止为每个通用素材另画独立图标卡。
- 如果 kind=general 已经按 visual_brief 自绘出可见图形，不要再叠加 <use>；缺少可见图形时才使用对应 <use> 兜底，且不得放成四角/边缘角标。
- 只有在单独绘制一个通用图标时才加 data-generated-icon="图标id"；不要为 scene 中的每个形状强制加标记。
- 不要创建 <symbol>、或任何 id 以 "icon-" 开头的元素（包括 <g id="icon-...">）；所有这些 id 由后处理器管理，你只需要用 <use href="#icon-..."> 引用即可
- 图标稳定性优先：已解析的 symbol 图标在同一任务的重试中不得替换；data-generated-icon 必须保持同一节点语义，但可根据视觉反馈自由改进图形。

【语义连接】
- structure.connections 是唯一允许的箭头清单，不得新增
- mechanism/system 的分区容器按 structure.sections 精确绘制，区间留白 64-80px 作为箭头走廊
"""

_PATCH_SYSTEM_PROMPT = """你是一名 SVG 教学示意图代码修复专家。你只输出可应用到上一轮 SVG 的单文件 unified diff。

要求：
1. 只输出 ```diff 代码块，不要输出完整 SVG，不要解释。
2. diff 文件名固定使用 infographic.svg。
3. 只修改校验反馈明确点名的问题，未点名的文字、图标、版式、配色、箭头路径必须保持不变。
4. hunk 上下文必须逐字来自上一轮 SVG，保证本地补丁器可以直接应用。
"""


def _build_prompt(
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    icons: list[dict] | None = None,
) -> str:
    """组装生成提示词；图标只传紧凑元数据，不传 SVG path/body。"""
    cells = structure.get("stages", [])
    sections = structure.get("sections", [])
    corridors = structure.get("connection_corridors", [])
    template_id = structure.get("template_id", "card-grid")
    template_slots = structure.get("template_slots", {})
    render_contract = structure.get("render_contract", {})
    area_label = "cell" if template_id == "card-grid" else "safe_area"
    cell_summary = "\n".join(
        f"- 节点 {s.get('id', i)}（{s.get('title', '')}）: "
        f"{area_label}=x={s.get('x')}, y={s.get('y')}, w={s.get('w')}, h={s.get('h')}; "
        f"render_as={s.get('render_as')}; node_shape={s.get('node_shape')}; "
        f"visual_brief={s.get('visual_brief', s.get('scene_type', ''))}; panel_rect={s.get('panel_rect', {})}; "
        f"scene_area={s.get('scene_area', {})}; "
        f"timeline_card={s.get('timeline_card', {})}; "
        f"node_geometry={s.get('node_geometry')}; anchor_points={s.get('anchor_points')}; "
        f"bar_slot={s.get('bar_slot')}; slots={s.get('slots', {})}; "
        f"short_description={s.get('short_description', '')}"
        for i, s in enumerate(cells)
    )
    icon_summary = [
        {
            "id": icon.get("id"),
            "label": icon.get("label"),
            "query": icon.get("query", ""),
            "kind": icon.get("kind"),
            "stage_id": icon.get("stage_id"),
            "visual_index": icon.get("visual_index"),
            "source": icon.get("source"),
            "render_mode": icon.get("render_mode", "symbol"),
            "palette_mode": icon.get("palette_mode", "monochrome"),
            "color": icon.get("color", ""),
            "fallback_reason": icon.get("fallback_reason", ""),
        }
        for icon in (icons or [])
    ]
    section_summary = "\n".join(
        f"- 分区 {s.get('id', i)}（{s.get('title', '')}）: "
        f"x={s.get('x')}, y={s.get('y')}, w={s.get('w')}, h={s.get('h')}; "
        f"内部布局={s.get('layout', 'grid')}"
        for i, s in enumerate(sections)
    )
    return (
        f"任务需求：{task}\n"
        f"内容分析：{content}\n"
        f"结构规划：{structure}\n"
        f"模板 ID：{template_id}\n"
        f"模板槽位 template_slots：{template_slots}\n"
        f"模板渲染契约 render_contract：{render_contract}\n"
        f"总标题区域：{structure.get('header', {})}\n"
        f"分区实际边界（严格使用）：\n{section_summary}\n"
        f"连接走廊（跨分区箭头优先经过）：{corridors}\n"
        f"节点安全区域与主体几何：\n{cell_summary}\n"
        f"样式设计：{style}\n"
        f"图标素材摘要（brand 必须使用对应 id；general 仅作场景素材提示）：{icon_summary}\n"
        "请生成完整 SVG。非 cards 节点内容留在 safe_area 内，主体按 node_geometry 绘制；连接线可跨 safe_area。"
    )


async def generate(
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    icons: list[dict] | None = None,
) -> str:
    """生成完整 SVG，symbol 将由 IconEmbedder 后处理注入。"""
    client = get_model_client()
    prompt = _build_prompt(task, content, structure, style, icons)
    raw = await call_llm(client, _SYSTEM_PROMPT, prompt)
    return extract_svg(raw)


def _build_repair_feedback_parts(feedback: dict) -> tuple[list[str], list[str]]:
    parts: list[str] = []
    icon_related_errors: list[str] = []
    syntax_errors = feedback.get("syntax_errors", [])
    hard_errors = feedback.get("hard_errors", [])
    has_blocking_errors = bool(syntax_errors or hard_errors)
    if feedback.get("stage_regeneration"):
        target_ids = ", ".join(str(item) for item in feedback.get("target_stage_ids", []))
        parts.append(
            f"【目标 stage 局部重生成】只允许重建 stage {target_ids} 的 stage-node 内部；"
            "diff 不得修改其他 stage、全局标题、画布、defs、主连接或样式。"
        )
    for err in syntax_errors:
        parts.append(f"- 语法错误：{err}")
    for err in hard_errors:
        parts.append(f"- 硬性错误：{err}")
        if any(keyword in str(err).lower() for keyword in ("图标", "icon", "symbol", "use", "data-generated-icon")):
            icon_related_errors.append(str(err))
    if feedback.get("visual_feedback") and not has_blocking_errors:
        parts.append(f"视觉评估：{feedback['visual_feedback']}")
    visual_defects = [
        item for item in feedback.get("visual_defects", [])
        if isinstance(item, dict)
    ]
    if visual_defects and not has_blocking_errors:
        parts.append("视觉模型一票否决项（必须逐条修复）：")
        for defect in visual_defects:
            location = str(defect.get("location", "画面位置"))
            issue = str(defect.get("issue", "视觉问题"))
            reason = str(defect.get("reason", ""))
            fix = str(defect.get("fix", ""))
            parts.append(f"- {location}：{issue}；原因：{reason}；修复：{fix}")
    if feedback.get("retry_hint") and not has_blocking_errors:
        parts.append(f"修改建议：{feedback['retry_hint']}")
    # 按优先级分组传入 suggestions（LLM 之前看不到这些关键反馈）
    suggestions = [] if has_blocking_errors else feedback.get("suggestions", [])
    if isinstance(suggestions, list) and suggestions:
        overlap_items = [s for s in suggestions if isinstance(s, str) and ("重叠" in s or "遮挡" in s)]
        missing_conn = [s for s in suggestions if isinstance(s, str) and "缺少" in s and ("连接" in s or "箭头" in s)]
        density_items = [s for s in suggestions if isinstance(s, str) and ("仅占" in s or "阈值" in s)]
        centering_items = [s for s in suggestions if isinstance(s, str) and ("偏左" in s or "偏右" in s or "居中" in s)]
        overflow_items = [s for s in suggestions if isinstance(s, str) and "溢出" in s]
        reported = set(overlap_items + missing_conn + density_items + centering_items + overflow_items)
        other_items = [s for s in suggestions if isinstance(s, str) and s not in reported]
        if overlap_items:
            parts.append("【必须优先修复】布局重叠/遮挡：")
            parts.extend(f"- {s}" for s in overlap_items[:3])
        if missing_conn:
            parts.append("【必须修复】缺失语义连接：")
            parts.extend(f"- {s}" for s in missing_conn[:5])
        if density_items:
            parts.append("【需要修复】场景图形密度不足：")
            parts.extend(f"- {s}" for s in density_items[:3])
        if centering_items:
            parts.append("【需要修复】场景图形未居中：")
            parts.extend(f"- {s}" for s in centering_items[:3])
        if overflow_items:
            parts.append("【需要修复】元素溢出画布：")
            parts.extend(f"- {s}" for s in overflow_items[:3])
        if other_items and len(overlap_items + missing_conn + density_items + overflow_items) < 8:
            parts.append("【其他建议】：")
            parts.extend(f"- {s}" for s in other_items[:5])
    if has_blocking_errors:
        parts.append(
            "当前轮是阻断错误修复轮：只修改上面明确列出的语法/硬性错误；"
            "不得顺带优化 suggestions、视觉密度、场景居中、配色或未点名元素。"
        )
    parts.append(
        "优先修复顺序：先修布局重叠/遮挡 → 再补缺失连接 → 再解决密度不足和溢出 → 最后优化其他。"
        "只有反馈明确点名的元素才修改；上一轮 SVG 的画布尺寸、卡片位置、配色和未点名文字必须保留。"
    )
    parts.append(
        "语义锚点锁定：除非校验明确指出文字或某个图形需要修复，否则必须逐字保留上一轮 SVG 的总标题、阶段标题、说明、数值和连接标签；"
        "每个已有 stage 的 icon/scene 图形也必须保留其主要造型、图形数量和 data-generated-icon 标记。"
    )
    if icon_related_errors:
        parts.append(
            "图标修复边界：仅修复上述明确有问题的图标引用、尺寸、对比度或槽位；"
            "其他 data-role='icon' 组必须保留上一轮的图形语义和主体造型。"
            "如果错误是“解析图标未使用”或“data-generated-icon 未声明”，只把对应 data-role='icon' 组内部替换为"
            " <use href='#icon-id'>，保留原来的图标容器位置、底托、卡片、文字和连线，不得重画整张图。"
        )
    else:
        parts.append(
            "远程图标锁定：上一轮所有 <use href='#icon-*'> 引用和 data-brand-badge 徽章不得替换；"
            "通用 data-generated-icon 图形可依据视觉反馈重新设计，但必须保留原图标 id 标记，并继续表达同一节点语义。"
        )
    return parts, icon_related_errors


async def regenerate_patch(
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    previous_svg: str,
    feedback: dict,
    icons: list[dict] | None = None,
) -> str:
    """让 LLM 只输出 diff，并在本地应用到上一轮 SVG。"""
    client = get_model_client()
    prompt = _build_prompt(task, content, structure, style, icons)
    feedback_parts, _ = _build_repair_feedback_parts(feedback)
    parts = [
        "上一轮 SVG 未通过校验，请只输出修改补丁，不要重新输出完整 SVG。",
        "补丁格式必须是 unified diff：",
        "```diff",
        "--- infographic.svg",
        "+++ infographic.svg",
        "@@ -起始行,行数 +起始行,行数 @@",
        " 上下文行",
        "-删除行",
        "+新增行",
        "```",
        "如果需要修改多处，只输出多个 hunk；不要省略 hunk 上下文。",
        *feedback_parts,
        f"上一轮 SVG：\n```svg\n{previous_svg}\n```",
    ]
    raw = await call_llm(
        client,
        _PATCH_SYSTEM_PROMPT,
        prompt + "\n\n" + "\n".join(parts),
    )
    return apply_unified_diff(previous_svg, raw)


async def regenerate(
    task: str,
    content: list[dict],
    structure: dict,
    style: dict,
    previous_svg: str,
    feedback: dict,
    icons: list[dict] | None = None,
) -> str:
    """根据 Validator 反馈重新生成完整 SVG。"""
    client = get_model_client()
    prompt = _build_prompt(task, content, structure, style, icons)
    parts = ["上一轮 SVG 未通过校验，请修复后重新生成完整 SVG："]
    feedback_parts, _ = _build_repair_feedback_parts(feedback)
    parts.extend(feedback_parts)
    parts.append(
        f"上一轮 SVG：\n```svg\n{previous_svg}\n```\n"
        "重新输出完整 ```svg 代码块，不要只返回修改说明。"
    )
    raw = await call_llm(
        client,
        _SYSTEM_PROMPT,
        prompt + "\n\n" + "\n".join(parts),
    )
    return extract_svg(raw)
