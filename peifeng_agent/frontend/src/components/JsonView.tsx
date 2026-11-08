/**
 * 前端**唯一**的 JSON 展示组件：一段"可能是 JSON"的文本进来，统一做三件事——
 * **缩进、着色、折叠**。
 *
 * 刻意**不判断这是哪种协议**（Planner / worker / 评审者）：那是内核的结构，会变；
 * 界面只负责把 JSON 显示好看，不做语义特判。所以：
 * - 字符串值里的 `\n` 会**展开成真实换行**（否则整段 Markdown 回复挤在一行、还带转义符号）；
 * - 但 `| 表格 |` 与 `**粗体**` 不会渲染成富文本——想看渲染后的正文，那是任务结果卡片的事。
 *
 * 后端的"解析 JSON"（取字段）由 `read` 的 `json_path` 负责（支持 `*` 通配与 `|` 多路径）：
 * **解析归 read，美化归这里**。
 */
import { useMemo, useState, type ReactNode } from "react";

/** 折叠时最多显示多少行 */
const COLLAPSED_LINES = 24;

/** 一行 JSON 里的着色片段：键 / 字符串 / 数字 / true·false·null */
const TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** 这段文本是不是 JSON（调用方据此决定用本组件还是 Markdown）。 */
export function looksLikeJson(text: string): boolean {
  const trimmed = (text ?? "").trim();
  return (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))
  );
}

export function JsonView({ text, maxLines = COLLAPSED_LINES }: { text: string; maxLines?: number }) {
  const lines = useMemo(() => formatJson(text), [text]);
  const [open, setOpen] = useState(false);

  // 不是 JSON：原样等宽显示，不做任何加工
  if (lines === null) return <pre className="json-plain">{text || "（无参数）"}</pre>;

  const folded = !open && lines.length > maxLines;
  const shown = folded ? lines.slice(0, maxLines) : lines;

  return (
    <div className="json-view">
      <pre className="json-body">
        {shown.map((line, index) => (
          <div key={index} className="json-line">
            {highlight(line.text, line.continuation)}
          </div>
        ))}
        {folded && <div className="json-line json-more">…（还有 {lines.length - maxLines} 行）</div>}
      </pre>
      {lines.length > maxLines && (
        <button type="button" className="json-toggle" onClick={() => setOpen((value) => !value)}>
          {open ? "收起" : `展开全部 ${lines.length} 行`}
        </button>
      )}
    </div>
  );
}

/** 一行 JSON：`continuation` 表示它是多行字符串的续行（仍属于某字符串值）。 */
type JsonLine = { text: string; continuation: boolean };

/** 格式化成 2 空格缩进的行；不是 JSON 返回 null。字符串值里的 `\n` 展开成真实换行。 */
function formatJson(text: string): JsonLine[] | null {
  const trimmed = (text ?? "").trim();
  if (!trimmed || !(trimmed.startsWith("{") || trimmed.startsWith("["))) return null;
  let pretty: string;
  try {
    const value = JSON.parse(trimmed);
    if (value === null || typeof value !== "object") return null;
    pretty = JSON.stringify(value, null, 2);
  } catch {
    return null;
  }
  return expandMultiline(pretty.split("\n"));
}

/** 把 JSON.stringify 里的字面 `\n` 还原成真实换行，续行按值起始列缩进。 */
function expandMultiline(lines: string[]): JsonLine[] {
  const out: JsonLine[] = [];
  for (const line of lines) {
    if (!line.includes("\\n")) {
      out.push({ text: line, continuation: false });
      continue;
    }
    const indent = line.match(/^[ \t]*/)?.[0] ?? "";
    const parts = line.split("\\n");
    parts.forEach((part, index) => {
      out.push({
        text: index === 0 ? part : `${indent}  ${part}`,
        continuation: index > 0,
      });
    });
  }
  return out;
}

/** 把一行 JSON 拆成带颜色的片段（键、字符串、数字、字面量）。 */
function highlight(line: string, continuation: boolean): ReactNode[] {
  // 多行字符串的续行：整体按字符串值着色，不再切 token
  if (continuation) return [<span key="c" className="jt-str">{line.replace(/^\s+/, "")}</span>];

  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of line.matchAll(TOKEN)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(line.slice(cursor, index));
    const [raw, quoted, colon, literal, number] = match;
    if (quoted) {
      nodes.push(
        <span key={`${index}-s`} className={colon ? "jt-key" : "jt-str"}>
          {quoted}
        </span>,
      );
      if (colon) nodes.push(colon);
    } else if (literal) {
      nodes.push(
        <span key={`${index}-l`} className="jt-lit">
          {literal}
        </span>,
      );
    } else if (number) {
      nodes.push(
        <span key={`${index}-n`} className="jt-num">
          {number}
        </span>,
      );
    } else {
      nodes.push(raw);
    }
    cursor = index + raw.length;
  }
  if (cursor < line.length) nodes.push(line.slice(cursor));
  return nodes;
}
