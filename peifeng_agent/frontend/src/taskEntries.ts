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
    id: "official_docx", name: "公文排版", description: "内容已经写好，只按公文格式排版：不联网、不调用模型",
    deliverable: "排版后的 .docx（标题二号小标宋、正文三号仿宋、行距 28 磅、GB/T 9704 页边距）",
    offline: "official_docx", example: "示例_公文稿件.md",
    fields: [
      { key: "source", label: "内容稿件", type: "file", required: true, placeholder: "按示例格式写的 .md / .txt" },
    ],
  },
  {
    id: "ppt_pages", name: "PPT 套版", description: "每页写什么已经定了，只按页面稿套成演示稿：不联网、不调用模型",
    deliverable: "演示稿 .pptx：封面 + 每个「## 页标题」一页",
    offline: "ppt_pages", example: "示例_PPT页面稿.md",
    fields: [
      { key: "source", label: "页面稿", type: "file", required: true, placeholder: "按示例格式写的 .md / .txt" },
    ],
  },
  {
    id: "offline_chart", name: "数据绘图", description: "数据已经整理好，只按选定的风格出图：不联网、不调用模型",
    deliverable: "一张 .png 图表（柱状图 / 条形图 / 折线图 / 饼图）",
    offline: "chart_png", example: "示例_实验数据.xlsx",
    fields: [
      { key: "source", label: "数据文件", type: "file", required: true, placeholder: "第一列是分类、其余是数值" },
      { key: "chart", label: "图型", placeholder: "柱状图（默认）/ 条形图 / 折线图 / 饼图" },
      { key: "metric", label: "图主题", placeholder: "可不填，默认用文件名与数值列名" },
    ],
  },
];

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
