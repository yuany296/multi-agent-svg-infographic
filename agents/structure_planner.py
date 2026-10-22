"""StructurePlanner —— 根据内容语义规划信息图结构与连接关系。"""

from utils.config import get_model_client
from utils.llm import call_llm_json
from utils.svg import log_intermediate


_SYSTEM_PROMPT = """你是一名信息图结构规划专家。根据内容分析结果，规划 SVG 的整体结构。

你只负责决定怎么组织版面，不要输出节点的绝对坐标 x/y/w/h；后续代码会计算坐标。

输出合法 JSON 对象：
{
  "canvas_preset": "wide / standard / square / tall / custom",
  "width": 1400,
  "height": 720,
  "diagram_type": "flow / mechanism / system / compare / timeline / cards",
  "layout": "horizontal / vertical / 1x3 / 2x2 / 2x3",
  "layout_style": "horizontal-flow / vertical-flow / flow-mechanism / mechanism-storyboard / two-section-mechanism / system-map / parallel-tracks / nested-section / comparison-grid / timeline / card-grid",
  "section_layout": "horizontal / vertical / 2x2 / 2x3 / focus-right",
  "margin": 20,
  "sections": [
    {
      "id": "section_0",
      "title": "分区标题",
      "layout": "horizontal-sequence / vertical-stack / left-right / grid / freeform",
      "notes": "分区组织方式"
    }
  ],
  "stages": [
    {
      "id": 0,
      "title": "节点标题",
      "section": "所属分区",
      "block_type": "process_node / image_step / module_box / text_box / decision_box / probability_box / annotation / result_node",
      "layout": "icon-node / mini-scene / module-card / side-note",
      "visual_brief": "内容分析提供的自由文本视觉意图",
      "role": "start / middle / end / support / feedback / result",
      "notes": "节点中的主要视觉元素"
    }
  ],
  "connections": [
    {"from": 0, "to": 1, "type": "arrow", "label": "可选短标签"}
  ]
}

布局规则：
1. flow：3-6 个节点优先 horizontal + horizontal-flow，必须包含顺序 connections，禁止普通卡片网格。
2. timeline：优先 horizontal + timeline，节点按时间顺序连接。
3. mechanism：只有单条主流程（输入→处理→输出、embedding→模块→结果）且没有反馈、迭代、概率空间或阶段内小场景时，才优先 horizontal + flow-mechanism。
4. mechanism-storyboard：当任务是在解释某个系统/模型/算法如何工作，内容天然是 3-8 个可折行阶段，且每个阶段适合画样本、概率空间、迭代缩略图、评估/反馈小场景时，优先 layout_style=mechanism-storyboard、layout=section-aware-rows、canvas_preset=wide；有训练/推理等大分区时必须保留 sections，模板会按 section 自动分行。
5. system：优先 2x2/2x3 + system-map；如果是教学式讲解系统运作机制且每个大阶段内都有小场景，也可以使用 mechanism-storyboard。
6. compare：优先 1x3/2x2 + comparison-grid。
7. 只有普通介绍才允许 cards + card-grid。
8. connections 必须逐项复制内容分析中的 connections，不得新增、删除、改向或改写 label；feedback_arrow 必须保留。
9. stages 数量、id 与内容分析完全一致，并保留 section、block_type、role、metric、visual_brief；兼容输入中遗留的 scene_type，但不得把它改写为固定构图。倍数关系不得拆成额外 annotation stage。
10. 不要输出 x、y、w、h；只输出 JSON。
11. mechanism/system 有多个 sections 时必须填写 section_layout：2-3 个顺序阶段优先 horizontal，4 个普通分区用 2x2；当右侧主流程需要纵向展开、左下还有反馈或品控区时使用 focus-right，并把 section 间留白视为箭头走廊。
12. sections[].layout 只描述该分区内部 stage 的排法；section_layout 描述分区容器之间的排法，两者不得混用；mechanism-storyboard 中可以不填 sections，因为 panel 已由 stage 表达。
13. 画布优先用 canvas_preset：横向 flow/timeline、mechanism-storyboard、三列 mechanism/system 和 focus-right 使用 wide（1400x720）；普通卡片使用 standard（1200x800）；只有明确纵向内容才使用 tall。禁止所有任务都固定成 1200x800。
14. 解释类 mechanism/system 若包含 3-8 个阶段，并出现训练样本、概率空间、迭代生成、评估反馈、前后对比或阶段内小图画，使用 mechanism-storyboard；简单线性链路才使用 flow-mechanism；两个并行流程使用 parallel-tracks，外层模块内还有子流程使用 nested-section。
15. compare 类只规划图表布局；倍数关系保留为 connection.label，后续用贴近箭头曲线的 callout 表达，不要规划成会穿过柱子的关系节点。
"""


async def plan(task: str, analysis: dict) -> dict:
    """根据内容节点规划信息图类型、分区、布局与连接。"""
    client = get_model_client()
    user_prompt = (
        f"任务需求：{task}\n"
        f"内容分析结果：{analysis}\n"
        "请规划信息图结构，不要输出绝对坐标。stages 与 connections 必须原样保留内容分析中的语义数据，"
        "你只补充画布、分区、布局和节点视觉组织说明。"
    )
    result = await call_llm_json(client, _SYSTEM_PROMPT, user_prompt)
    if isinstance(result, dict):
        log_intermediate("StructurePlanner 输出", result)
        return result
    return {
        "canvas_preset": "standard",
        "width": 1200,
        "height": 800,
        "diagram_type": "cards",
        "layout": "1x3",
        "layout_style": "card-grid",
        "margin": 20,
        "sections": [],
        "stages": [],
        "connections": [],
    }
