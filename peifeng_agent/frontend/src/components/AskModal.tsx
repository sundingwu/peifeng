/**
 * 澄清弹层：对应内核的 `await_input` 事件。
 *
 * 计划不再需要用户确认（Planner 给出计划即执行），所以这里只剩一种形态：
 * 需要补充信息时按问题逐条收集回答。回答经 `POST /api/input` 回传，内核据此继续任务。
 */
import { Button, Input, Modal } from "antd";
import { useEffect, useState } from "react";

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
