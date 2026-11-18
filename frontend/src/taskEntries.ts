import type { PresetField } from "./api/client";

export type TaskEntry = {
  id: string;
  name: string;
  description: string;
  deliverable: string;
  fields: (PresetField & { multiline?: boolean })[];
  /**
   * 填了这个就表示该入口有**离线版**：值是与后端一致的离线预设 id。
   * 离线执行不调用任何模型（读表画图、清洗汇总、按类型归档这类规则确定的任务），
   * 断网也能跑完；没有这个字段的入口必须联网。
   */
  offline?: string;
  /** 格式示例文件名（放在项目根的 examples/ 下，弹窗里给用户看） */
  example?: string;
};

export const TASK_ENTRIES: TaskEntry[] = [
  {
    id: "paper_reading", name: "论文精读", description: "解析论文问题、方法与实验，定位原文证据",
    deliverable: "论文分析 paper-analysis.md，说明问题、关键假设、方法机制、实验依据与局限，引用原文位置；不构建知识库",
    fields: [
      { key: "topic", label: "论文与研究问题", required: true, multiline: true, placeholder: "例如：解释这篇论文的核心机制与消融实验" },
      { key: "materials", label: "论文材料", type: "file", placeholder: "选择论文或已有研究材料" },
    ],
  },
  {
    id: "literature_research", name: "文献研究", description: "检索文献与核验 DOI，整理可追溯的研究证据",
    deliverable: "研究笔记 evidence.md，记录实际查询、书目元数据、来源链接与证据范围。正文结论须阅读全文，未获得正文的项目标明待核验",
    fields: [
      { key: "topic", label: "研究问题或 DOI", required: true, multiline: true, placeholder: "例如：核验 DOI 10.1038/nature14539 的标题、作者与年份" },
      { key: "scope", label: "时间范围与筛选要求", placeholder: "可选，例如近三年、指定数据集或方法" },
    ],
  },
  {
    id: "research_chart", name: "科研绘图", description: "统计汇总、实验结果图与可编辑机制流程图",
    deliverable: "根据需求交付结果图 PNG 或可编辑 SVG。保持真实数据与拓扑关系，核对单位、样本量和图中文字",
    fields: [
      { key: "goal", label: "图的内容与要求", required: true, multiline: true, placeholder: "例如：按 method 汇总 score 并绘图，或描述流程节点与关系" },
      { key: "source", label: "实验数据或框架材料", type: "file", placeholder: "可选，CSV、XLSX 或文字材料" },
    ],
  },
  {
    id: "lncs_paper", name: "论文排版", description: "使用 Springer LNCS 官方模板，编译并核对 PDF",
    deliverable: "LNCS LaTeX 源文件、references.bib 及实际编译成功的 PDF，保留原稿事实和引用，注明编译与页面检查结果",
    fields: [
      { key: "source", label: "论文稿件", type: "file", required: true, placeholder: "选择已有稿件" },
      { key: "requirements", label: "会议与匿名要求", multiline: true, placeholder: "可选，填写目标会议、页数和匿名规则" },
    ],
  },
  {
    id: "research_ppt", name: "汇报 PPT", description: "组会、项目报告与竞赛答辩，套版并逐页检查",
    deliverable: "可编辑 report.pptx，使用项目科研精简模板，区分实测与计划，记录来源并检查渲染页面",
    fields: [
      { key: "topic", label: "汇报主题与类型", required: true, multiline: true, placeholder: "例如：10分钟科研组会，重点讲方法与实验结果" },
      { key: "materials", label: "论文或项目材料", type: "file", placeholder: "可选，选择材料文件" },
      { key: "pages", label: "总页数", default: "6页，含封面" },
    ],
  },
  {
    id: "campus_notice", name: "校园通知", description: "参考高校通知的信息组织，制作可编辑 Word",
    deliverable: "notice.docx，核对对象、时间、地点、截止时间和联系人，不添加未提供的安排、文号或印章，检查分页与版面",
    fields: [
      { key: "details", label: "通知事项", required: true, multiline: true, placeholder: "填写组织单位、主题、对象、时间地点、要求、联系人及发布日期" },
      { key: "materials", label: "已有通知材料", type: "file", placeholder: "可选，选择稿件" },
    ],
  },

  {
    id: "official_docx", name: "公文排版", description: "内容已写好，按公文格式排版出 .docx",
    deliverable: "排版后的 .docx（标题二号小标宋、正文三号仿宋、行距 28 磅、GB/T 9704 页边距）",
    offline: "official_docx", example: "示例_公文稿件.md",
    fields: [
      { key: "source", label: "内容稿件", type: "file", required: true, placeholder: "按示例格式写的 .md / .txt" },
    ],
  },
  {
    id: "ppt_pages", name: "PPT 套版", description: "页面稿已定，套成演示稿 .pptx",
    deliverable: "演示稿 .pptx：封面 + 每个「## 页标题」一页",
    offline: "ppt_pages", example: "示例_PPT页面稿.md",
    fields: [
      { key: "source", label: "页面稿", type: "file", required: true, placeholder: "按示例格式写的 .md / .txt" },
    ],
  },
  {
    id: "offline_chart", name: "数据绘图", description: "数据已整理，按选定图型出 .png",
    deliverable: "一张 .png 图表（柱状图 / 条形图 / 折线图 / 饼图）",
    offline: "chart_png", example: "示例_实验数据.xlsx",
    fields: [
      { key: "source", label: "数据文件", type: "file", required: true, placeholder: "第一列是分类、其余是数值" },
      { key: "designer", label: "图型", placeholder: "柱状图（默认）/ 条形图 / 折线图 / 饼图" },
      { key: "metric", label: "图主题", placeholder: "可不填，默认用文件名与数值列名" },
    ],
  },
];

/**
 * 主页只列**不依赖模型**的入口：离线预设直接产出文件，断网也能用。
 * 其余任务（需要规划、检索、写作）不摆卡片，直接在输入框里描述，由 Mentor 理解与分派。
 */
export const HOME_ENTRIES: TaskEntry[] = TASK_ENTRIES.filter((entry) => Boolean(entry.offline));

export function buildTaskRequest(entry: TaskEntry, values: Record<string, string>): string {
  const lines = [`任务类型：${entry.name}`];
  for (const field of entry.fields) {
    const value = (values[field.key] ?? "").trim();
    if (field.required && !value) throw new Error(`请填写${field.label}`);
    if (value) lines.push(`${field.label}：${value}`);
  }
  lines.push(`交付要求：${entry.deliverable}`);
  return lines.join("\n");
}
