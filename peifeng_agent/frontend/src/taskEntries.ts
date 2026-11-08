import type { PresetField } from "./api/client";

export type TaskEntry = {
  id: string;
  name: string;
  description: string;
  deliverable: string;
  fields: (PresetField & { multiline?: boolean })[];
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
