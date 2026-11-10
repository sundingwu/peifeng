/** 任务入口表单：收集需求后交由 Planner 正常规划。 */
import { Button, Input, Modal } from "antd";
import { useEffect, useMemo, useState } from "react";

import { fetchExample } from "../api/client";
import type { TaskEntry } from "../taskEntries";
import { WorkspacePicker } from "./WorkspacePicker";

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
  //: 格式示例：展开后显示该入口要求的稿件长什么样（离线排版的成败全在输入格式）
  const [example, setExample] = useState("");
  const [exampleOpen, setExampleOpen] = useState(false);

  // 每次打开都按模板的默认值重置：绝不把上一个模板的内容带进来
  useEffect(() => {
    if (!entry) return;
    const initial: Record<string, string> = {};
    for (const field of entry.fields) initial[field.key] = field.default ?? "";
    setValues(initial);
    setSubmitted(false);
    setExample("");
    setExampleOpen(false);
  }, [entry]);

  const missing = useMemo(() => {
    if (!entry) return [];
    return entry.fields
      .filter((field) => field.required && !(values[field.key] ?? "").trim())
      .map((field) => field.label);
  }, [entry, values]);

  if (!entry) return null;

  const openExample = async () => {
    if (!entry?.example) return;
    setExampleOpen(true);
    if (!example) {
      try {
        setExample(await fetchExample(entry.example));
      } catch (error) {
        setExample(`示例读取失败：${String(error)}`);
      }
    }
  };

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
      {entry.example && (
        <div className="preset-example">
          <button type="button" className="preset-example-toggle" onClick={openExample}>
            {exampleOpen ? "收起格式示例" : "查看格式示例"}
          </button>
          <span className="preset-example-hint">照着这个格式写稿件，排版才不会跑偏</span>
          {exampleOpen && (
            <pre className="preset-example-body">{example || "读取中…"}</pre>
          )}
        </div>
      )}
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
