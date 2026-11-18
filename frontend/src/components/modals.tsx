import { Button, Input, Modal, Popover } from "antd";
import { useEffect, useState, useMemo } from "react";
import { endpoints, get } from "../api/client";
import type { TaskEntry } from "../taskEntries";
import { useQuery } from "@tanstack/react-query";

// ---------------------------------------------------------------- AskModal
/**
 * 澄清弹层：对应内核的 `await_input` 事件。
 *
 * 计划不再需要用户确认（Planner 给出计划即执行），所以这里只剩一种形态：
 * 需要补充信息时按问题逐条收集回答。回答经 `POST /api/input` 回传，内核据此继续任务。
 */

export function AskModal({
  pending,
  onAnswer,
}: {
  pending: Record<string, unknown> | null;
  onAnswer: (id: string, value: unknown) => void;
}) {
  const [answers, setAnswers] = useState<string[]>([]);
  // 已提交就锁住按钮：服务端快照回来之前也不让人重复点，视觉上也表明"已经交了"
  const [submitted, setSubmitted] = useState(false);

  const id = String(pending?.id ?? "");
  const questions = (pending?.questions as { question: string; options?: string[] }[] | undefined) ?? [];

  useEffect(() => {
    setAnswers(questions.map(() => ""));
    setSubmitted(false);
  }, [id, questions.length]);

  if (!pending) return null;

  /** 提交一次即锁定；空答案也照样回传（内核记成「未选择」）。 */
  const submit = (value: unknown) => {
    if (submitted) return;
    setSubmitted(true);
    onAnswer(id, value);
  };

  return (
    <Modal
      open
      title="需要你补充信息"
      width={560}
      okText={submitted ? "已提交…" : "提交"}
      confirmLoading={submitted}
      okButtonProps={{ disabled: submitted }}
      cancelButtonProps={{ disabled: submitted }}
      maskClosable={false}
      onOk={() => submit({ answers: questions.map((_, index) => answers[index] ?? "") })}
      onCancel={() => submit({ answers: [] })}
      cancelText="跳过"
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: "var(--text-dim)" }}>
          规划者需要这些信息才能继续；留空的项会按「未选择」处理。
        </p>
        {questions.map((item, index) => (
          <div key={index} className="ask-question">
            <span className="q">{item.question}</span>
            <Input
              value={answers[index] ?? ""}
              onChange={(event) => {
                const next = [...answers];
                next[index] = event.target.value;
                setAnswers(next);
              }}
              placeholder="输入回答，或点下面的快捷选项"
            />
            {(item.options?.length ?? 0) > 0 && (
              <div className="ask-options">
                {item.options?.map((option) => (
                  <Button
                    key={option}
                    size="small"
                    type={answers[index] === option ? "primary" : "default"}
                    onClick={() => {
                      const next = [...answers];
                      next[index] = option;
                      setAnswers(next);
                    }}
                  >
                    {option}
                  </Button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </Modal>
  );
}


// ---------------------------------------------------------------- TaskEntryModal
/** 任务入口表单：收集需求后交由 Planner 正常规划。 */


export function TaskEntryModal({
  entry,
  onCancel,
  onSubmit,
}: {
  entry: TaskEntry | null;
  onCancel: () => void;
  onSubmit: (entry: TaskEntry, values: Record<string, string>) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);

  // 每次打开都按模板的默认值重置：绝不把上一个模板的内容带进来
  useEffect(() => {
    if (!entry) return;
    const initial: Record<string, string> = {};
    for (const field of entry.fields) initial[field.key] = field.default ?? "";
    setValues(initial);
    setSubmitted(false);
  }, [entry]);

  const missing = useMemo(() => {
    if (!entry) return [];
    return entry.fields
      .filter((field) => field.required && !(values[field.key] ?? "").trim())
      .map((field) => field.label);
  }, [entry, values]);

  if (!entry) return null;

  return (
    <Modal
      open
      title={entry.name}
      onCancel={onCancel}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={onCancel}>
          取消
        </Button>,
        <Button
          key="run"
          type="primary"
          disabled={missing.length > 0 || submitted}
          onClick={() => {
            setSubmitted(true);
            onSubmit(entry, values);
          }}
        >
          开始任务
        </Button>,
      ]}
    >
      <p className="preset-hint">{entry.description}</p>
      {entry.fields.map((field) => (
        <div key={field.key} className="preset-field">
          <label className="preset-label">
            {field.label}
            {field.required ? <span className="preset-required"> *</span> : null}
          </label>
          {field.type === "file" || field.type === "dir" ? (
            <WorkspacePicker
              kind={field.type}
              value={values[field.key] ?? ""}
              placeholder={field.placeholder}
              onChange={(next) => setValues((current) => ({ ...current, [field.key]: next }))}
            />
          ) : field.multiline ? (
            <Input.TextArea aria-label={field.label} value={values[field.key] ?? ""} placeholder={field.placeholder}
              autoSize={{minRows: 3, maxRows: 7}} onChange={(event) => setValues((current) => ({...current, [field.key]: event.target.value}))} />
          ) : (
            <Input aria-label={field.label}
              value={values[field.key] ?? ""}
              placeholder={field.placeholder ?? ""}
              onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))}
            />
          )}
        </div>
      ))}
      {missing.length > 0 && <p className="preset-missing">还需填写：{missing.join("、")}</p>}
    </Modal>
  );
}


// ---------------------------------------------------------------- WorkspacePicker
/**
 * 项目内选择器：给快捷模板挑"素材文件"或"要整理的目录"。
 *
 * 只浏览**项目工作目录内部**、返回的也是相对路径——worker的工具就是按相对路径读的，
 * 手填绝对路径反而容易出错（也会跳出项目目录）。浏览逻辑在 `GET /api/workspace/entries`。
 */


type Item = { name: string; path: string; size?: number };
type Listing = { path: string; parent: string; dirs: Item[]; files: Item[] };

export function WorkspacePicker({
  kind,
  value,
  placeholder,
  onChange,
}: {
  /** file：只能选文件；dir：只能选目录（也可以选当前目录本身） */
  kind: "file" | "dir";
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [browsing, setBrowsing] = useState("");

  const listing = useQuery<Listing>({
    queryKey: ["workspace", browsing],
    queryFn: () => get<Listing>(endpoints.workspaceEntries, { path: browsing }),
    // 只有展开时才拉：不给用户增添无用请求
    enabled: open,
    staleTime: 0,
  });

  const data = listing.data;
  const crumbs = browsing ? browsing.split("/") : [];

  /** 选中并收起；值用相对路径（worker的工具认这个写法）。 */
  const pick = (path: string) => {
    onChange(path);
    setOpen(false);
  };

  const panel = (
    <div className="ws-picker">
      <div className="ws-crumbs">
        <button type="button" className="ws-crumb" onClick={() => setBrowsing("")}>
          项目目录
        </button>
        {crumbs.map((name, index) => (
          <button
            key={`${name}-${index}`}
            type="button"
            className="ws-crumb"
            onClick={() => setBrowsing(crumbs.slice(0, index + 1).join("/"))}
          >
            / {name}
          </button>
        ))}
        {data?.parent ? (
          <Button size="small" type="link" onClick={() => setBrowsing(data.parent)}>
            上一级
          </Button>
        ) : null}
      </div>

      {listing.isLoading && <p className="ws-empty">加载中…</p>}
      {listing.isError && <p className="ws-empty">这个目录读不到，换一个看看</p>}

      {data && data.dirs.length === 0 && data.files.length === 0 && (
        <p className="ws-empty">这一层是空的</p>
      )}

      {data?.dirs.map((item) => (
        <div key={item.path} className="ws-row">
          <button type="button" className="ws-name" onClick={() => setBrowsing(item.path)}>
            📁 {item.name}
          </button>
          {kind === "dir" && (
            <Button size="small" type="link" onClick={() => pick(item.path)}>
              选这个
            </Button>
          )}
        </div>
      ))}

      {data?.files.map((item) => (
        <div key={item.path} className="ws-row">
          {kind === "file" ? (
            <button type="button" className="ws-name" onClick={() => pick(item.path)}>
              📄 {item.name}
            </button>
          ) : (
            <span className="ws-name ws-muted">📄 {item.name}</span>
          )}
        </div>
      ))}

      {kind === "dir" && (
        <div className="ws-pick-here">
          <Button size="small" onClick={() => pick(browsing)}>
            选当前这一层
          </Button>
        </div>
      )}
    </div>
  );

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setBrowsing("");
      }}
      trigger="click"
      placement="bottomLeft"
      content={panel}
    >
      <div className="ws-field">
        <Input
          readOnly
          value={value}
          placeholder={placeholder ?? (kind === "dir" ? "点这里选目录" : "点这里选文件")}
          onClick={() => setOpen(true)}
          style={{ cursor: "pointer" }}
        />
        <Button size="small" onClick={() => setOpen((current) => !current)}>
          选择
        </Button>
      </div>
    </Popover>
  );
}
