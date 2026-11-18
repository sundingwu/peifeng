import { downloadUrl } from "../api/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useMemo, useState, type ReactNode } from "react";

// ---------------------------------------------------------------- icons
/**
 * 内联图标：全部用 `currentColor` 描边，跟着文字颜色走。
 *
 * 不引图标库：这里只需要十来个 16px 线性图标，内联 SVG 更可控，也不会多一份运行时依赖。
 */

type Props = { size?: number; className?: string };

/** 品牌标识：风纹（「培风」取《庄子》"而后乃今培风"）。 */
export function WindMark({ size = 15, className }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M4 7.5h9.2a2.6 2.6 0 1 0-2.6-2.6" />
      <path d="M4 12.5h13" />
      <path d="M4 17.5h6.4a2.6 2.6 0 1 1-2.6 2.6" />
    </svg>
  );
}

/** 页面空态的标记：稍大一点的风纹。 */
export function WindMarkLarge({ size = 24, className }: Props) {
  return <WindMark size={size} className={className} />;
}

export function SunIcon({ size = 14, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" className={className} aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4" />
    </svg>
  );
}

export function MoonIcon({ size = 14, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" className={className} aria-hidden="true">
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />
    </svg>
  );
}

/** 跟随系统：半明半暗的圆。 */
export function AutoIcon({ size = 14, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className={className} aria-hidden="true">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 3.5v17a8.5 8.5 0 0 0 0-17Z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function FolderIcon({ size = 12, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7.5A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5Z" />
    </svg>
  );
}

export function ChevronIcon({ size = 10, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="m9 5 7 7-7 7" />
    </svg>
  );
}

export function ArrowDownIcon({ size = 12, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M12 4v15M6 13l6 6 6-6" />
    </svg>
  );
}

/** 文件：按扩展名换色的小块，用于产出文件 chip。 */
export function FileIcon({ size = 12, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

export function StopIcon({ size = 10, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

export function SendIcon({ size = 12, className }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d="M12 19V5M6 11l6-6 6 6" />
    </svg>
  );
}

/** Task categories share the same compact outline language. */
export function TaskIcon({ kind, size = 20 }: { kind: "document" | "designer" | "folder" | "code" | "slides"; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {kind === "document" && <><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z" /><path d="M14 3v5h5M9 12h6M9 16h4" /></>}
      {kind === "designer" && <><path d="M4 3v17h17M8 15v-4M13 15V7M18 15V5" /></>}
      {kind === "folder" && <><path d="M3 7h6l2 2h10l-2 11H3Z" /><path d="M3 7V5h6l2 2h7v2" /></>}
      {kind === "code" && <><path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" /></>}
      {kind === "slides" && <><rect x="3" y="4" width="18" height="12" rx="1.5" /><path d="M12 16v4M8.5 20h7" /></>}
    </svg>
  );
}


// ---------------------------------------------------------------- FileChips
/**
 * 产出文件：任务结果里的交付文件，点击按附件下载。
 *
 * 服务端只接受文件名（不接受路径），所以这里先把路径里的文件名取出来再拼下载地址。
 */

/** 扩展名 → chip 上的小标签（没有映射时显示大写扩展名）。 */
const EXT_LABEL: Record<string, string> = {
  docx: "DOCX",
  doc: "DOC",
  xlsx: "XLSX",
  xls: "XLS",
  pptx: "PPTX",
  ppt: "PPT",
  pdf: "PDF",
  md: "MD",
  txt: "TXT",
  csv: "CSV",
};

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

export function FileChips({ files }: { files: string[] }) {
  if (files.length === 0) return null;
  return (
    <>
      <div className="file-row-label">产出文件 · 点击下载</div>
      <div className="files">
        {files.map((path) => {
          const name = fileName(path);
          const ext = (name.split(".").pop() || "").toLowerCase();
          return (
            <a key={path} className="file-chip" href={downloadUrl(path)} title={`下载 ${path}`}>
              <FileIcon />
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
              <span className="ext">{EXT_LABEL[ext] ?? ext.toUpperCase()}</span>
            </a>
          );
        })}
      </div>
    </>
  );
}


// ---------------------------------------------------------------- Markdown
/**
 * 模型输出的 Markdown 渲染。
 *
 * 为什么要它：模型的回复本身就是 Markdown（标题、列表、粗体、表格、代码块），
 * 之前用 `white-space: pre-wrap` 直接铺纯文本，`**粗体**`、`- 列表` 的符号全都露在页面上。
 *
 * 两个细节：
 * - **流式增量不渲染 Markdown**：每个 delta 都重建一次 Markdown 树既慢又会让未闭合的
 *   `**` / ``` 闪来闪去，所以流式期间按纯文本铺（保留换行），结束后一次性渲染。
 * - **不渲染原始 HTML**：react-markdown 默认就是安全的（html 不解析），无需额外处理。
 */

export function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const body = (text ?? "").trim();
  if (!body) return null;

  // 流式期间：纯文本 + 保留换行，避免反复重建 markdown 树
  if (streaming) return <p className="md-stream">{text}</p>;

  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // 外链新开窗口（本地应用里也可能出现 http 链接）
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          // 代码块包一层容器：长行横向滚动，不撑破气泡
          pre: ({ children }) => <pre className="md-pre">{children}</pre>,
          // GFM 任务列表的复选框不可交互
          input: ({ checked }) => <input type="checkbox" checked={Boolean(checked)} readOnly />,
          // 表格外层允许横向滚动（列多的表在窄栏里也能看）
          table: ({ children }) => (
            <div className="md-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}


// ---------------------------------------------------------------- JsonView
/**
 * 前端**唯一**的 JSON 展示组件：一段"可能是 JSON"的文本进来，统一做三件事——
 * **缩进、着色、折叠**。
 *
 * 刻意**不判断这是哪种协议**（Planner / worker / 评审者）：那是内核的结构，会变；
 * 界面只负责把 JSON 显示好看，不做语义特判。所以：
 * - 字符串值里的 `\n` 会**展开成真实换行**（否则整段 Markdown 回复挤在一行、还带转义符号）；
 * - 但 `| 表格 |` 与 `**粗体**` 不会渲染成富文本——想看渲染后的正文，那是任务结果卡片的事。
 *
 * 后端不提供"按路径下钻 JSON"的工具：会话明细是每步一个十来 KB 的小文件，整份读即可。
 * **读取归 read，美化归这里**。
 */

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
