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
};

export const TASK_ENTRIES: TaskEntry[] = [
  {
    id: "ppt", name: "PPT生成", description: "依据主题与素材生成可编辑演示文稿", deliverable: "可编辑的 .pptx 演示文稿",
    fields: [
      { key: "topic", label: "演示主题", required: true, placeholder: "请输入演示主题" },
      { key: "materials", label: "参考素材", type: "file" },
      { key: "requirements", label: "具体要求", multiline: true, placeholder: "受众、页数、内容结构与视觉风格" },
    ],
  },
  {
    id: "word", name: "Word生成", description: "依据内容要求生成规范的 Word 文档", deliverable: "可编辑的 .docx 文档",
    fields: [
      { key: "topic", label: "文档主题", required: true, placeholder: "请输入文档主题" },
      { key: "materials", label: "参考素材", type: "file" },
      { key: "requirements", label: "具体要求", multiline: true, placeholder: "文档类型、篇幅、内容结构与格式要求" },
    ],
  },
  {
    id: "research_chart", name: "科研绘图", description: "依据研究内容与数据制作科研图件", deliverable: "符合要求的科研图件，适用时附可复现绘图脚本",
    fields: [
      { key: "topic", label: "绘图主题", required: true, placeholder: "请输入图件主题" },
      { key: "materials", label: "数据或参考素材", type: "file" },
      { key: "requirements", label: "绘图要求", multiline: true, placeholder: "图件类型、数据说明、标注、尺寸与输出格式" },
    ],
  },
  {
    id: "offline_chart", name: "表格出图", description: "读本地表格直接画图，不联网、不调用模型",
    deliverable: "一张 .png 图表", offline: "chart_png",
    fields: [
      { key: "source", label: "数据文件", type: "file", required: true },
      { key: "chart", label: "图型", placeholder: "柱状图 / 条形图 / 折线图 / 饼图（默认柱状图）" },
      { key: "metric", label: "图主题", placeholder: "可不填，默认用文件名与数值列名" },
    ],
  },
  {
    id: "offline_table", name: "表格清洗汇总", description: "去重去空、按第一列分组求和，不联网、不调用模型",
    deliverable: "一个 .xlsx（汇总页 + 明细页）", offline: "table_xlsx",
    fields: [
      { key: "source", label: "数据文件", type: "file", required: true },
      { key: "output", label: "输出文件名", placeholder: "默认 汇总表.xlsx" },
    ],
  },
  {
    id: "offline_archive", name: "按类型归档", description: "把目录里的文件按扩展名归到子目录并出清单，不联网",
    deliverable: "归档目录 + 归档清单.md", offline: "archive_files",
    fields: [
      { key: "target", label: "要整理的目录", placeholder: "项目目录下的子目录名，如 散落文件", required: true },
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
