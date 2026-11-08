/** 任务入口表单：收集需求后交由 Planner 正常规划。 */
import { Button, Input, Modal } from "antd";
import { useEffect, useMemo, useState } from "react";

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
