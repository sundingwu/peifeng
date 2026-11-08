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
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

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
