# 多智能体 SVG 信息图生成系统

## 效果

**数据对比类**：柱子比例、倍数标注、品牌图标自动匹配。

![短视频平台规模对比](figures/02-comparison.png)

**概念分解类**：多面板拆解，卡片式布局，箭头表示推导关系。

![词向量概念分解](figures/03-concept-map.png)

---

## 流水线

```
内容分析 → 图标解析 → 结构规划 → 布局计算 → 样式设计 → SVG 生成 → 图标注入 → 校验
   ▲ LLM        ▲ 代码       ▲ LLM        ▲ 代码      ▲ LLM       ▲ LLM      ▲ 代码     ▲ 代码+LLM
```

8 个阶段里 **5 个由 LLM 参与、3 个是纯确定性代码**。每个阶段的输入输出都是结构化 JSON，阶段之间靠契约而不是自由文本衔接——这也是为什么单个阶段可以被单独替换、单独测试。

---

## 三个关键设计

### 1. 图标：模型只给 ID，路径由代码注入

让模型生成图标路径是灾难。这里让模型只输出 `icon_id` 和 `label`，真实路径在后处理阶段以 `<symbol>` / `<use>` 注入，同时支持品牌色和主题色。

解析链路是五级的，前一级拿不到就降级：

```
local → cache → Iconify search → SimpleIcons → fallback 占位
```

好处有两个：token 消耗大幅下降，以及图标永远不会"画歪"——它要么是真图标，要么是个明确的占位块。

### 2. 重试：模型只输出 diff，不重画整张图

校验失败时，不是让模型重新生成整张 SVG（贵，而且容易把好的部分也改坏），而是**只让它输出 unified diff 补丁**，由本地补丁器校验上下文后应用。

```
patch 校验通过  → 应用
patch 校验失败  → 回退完整重生成
```

绝大多数修复只需要改动几个坐标数字，patch 通常几十个 token 就够。

### 3. 择优：新版本必须比旧版本好，才允许替换

每一轮候选稿都会和历史最佳稿比较：重叠、越界、图标遮挡、槽位偏移，加上 Qwen-VL 的多模态视觉评估。**只有更优才替换，否则回滚。**

配合箭头正交路由和障碍物避让（零碰撞优先），保证多轮迭代不会让图越来越差——这一条是整个系统能稳定出图的前提。

---

## 快速开始

```bash
pip install -r requirements.txt

cp .env.example .env      # 填入 DeepSeek 与 Qwen-VL 的 key
streamlit run app.py
```

浏览器打开后输入需求即可生成。生成的 SVG 与中间产物落在 `output/svg/`。

**先验证模型能力**（可选但推荐）：不同模型对工具调用的支持差别很大，这个脚本会依次验证工具调用、结构化输出、多轮协作三步：

```bash
python verify_agents.py
```

---

## 目录结构

```
agents/              智能体与编排
  orchestrator.py        8 阶段流水线调度、失败重试与回滚
  content_analyzer.py    内容拆解
  structure_planner.py   结构规划
  style_designer.py      样式设计
  code_generator.py      SVG 生成与 patch 修复
  reviewer.py            审查
  icon_resolver.py       图标五级解析
  validator.py           确定性校验
  studio.py / team.py    AutoGen 智能体定义
prompts/             5 份提示词，按阶段拆分
utils/
  layout.py              坐标计算、碰撞检测、正交路由
  svg.py                 SVG 解析与 <symbol>/<use> 注入
  validation.py          重叠 / 越界 / 遮挡 校验
  visual_eval.py         Qwen-VL 视觉评估
  icons.py               图标缓存与品牌色
figures/             生成效果示例
app.py               Streamlit 界面
verify_agents.py     模型能力验证脚本
```

---

## 已知限制

- 布局模板是预置的（流程、对比、时间线、分镜几类），复杂自由布局仍会出现碰撞后降级。
- 依赖模型的内容理解能力，需求越抽象，节点拆解质量波动越大。
- 输出是静态 SVG，没有交互和动画。
- 视觉评估需要多模态模型，纯文本模型下会退化为仅规则校验。
