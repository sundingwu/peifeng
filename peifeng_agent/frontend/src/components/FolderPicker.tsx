/**
 * 目录选择弹层：服务端把某一层目录的子目录列出来，页面自己渲染浏览器。
 *
 * 为什么不用系统选择框：浏览器里拿不到本机目录的绝对路径（`<input webkitdirectory>`
 * 与 `showDirectoryPicker()` 都只给相对路径），而让服务端弹系统框又要求它那边有 GUI。
 * 所以做成"浏览 + 选择"，同时保留一个可粘贴路径的输入框兜底。
 *
 * 选中的是**当前所在目录**（与系统"选择文件夹"对话框一致）：进到哪一层，就选哪一层。
 */
import { Button, Input, Modal } from "antd";
import { useCallback, useEffect, useState } from "react";

import { endpoints, get, type DirListing } from "../api/client";
import { ChevronIcon, FolderIcon } from "./icons";

export function FolderPicker({
  open,
  initial,
  onPick,
  onCancel,
}: {
  open: boolean;
  /** 打开时落在哪一层（空串表示默认位置，即当前用户桌面） */
  initial: string;
  onPick: (path: string) => void;
  onCancel: () => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const go = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      const next = await get<DirListing>(endpoints.fsDirs, path.trim() ? { path: path.trim() } : undefined);
      setListing(next);
      setTyped(next.path ?? "");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "无法读取该目录");
    } finally {
      setLoading(false);
    }
  }, []);

  // 每次打开都从调用方给的位置重新开始
  useEffect(() => {
    if (open) void go(initial);
  }, [open, initial, go]);

  const current = listing?.path ?? "";
  const entries = listing?.entries ?? [];

  return (
    <Modal
      open={open}
      title="选择文件夹"
      width={560}
      okText="选择此文件夹"
      cancelText="取消"
      okButtonProps={{ disabled: !current || loading }}
      onOk={() => onPick(current)}
      onCancel={onCancel}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div className="picker-path">
          <Input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            onPressEnter={() => void go(typed)}
            placeholder="也可以直接粘贴完整路径"
          />
          <Button onClick={() => void go(typed)}>转到</Button>
        </div>

        <div className="picker-quick">
          <Button size="small" disabled={!listing?.parent || loading} onClick={() => void go(listing?.parent ?? "")}>
            上一级
          </Button>
          {(listing?.shortcuts ?? []).map((item) => (
            <Button key={item.path} size="small" disabled={loading} onClick={() => void go(item.path)}>
              {item.name}
            </Button>
          ))}
          {(listing?.roots ?? []).map((item) => (
            <Button key={item.path} size="small" type="text" disabled={loading} onClick={() => void go(item.path)}>
              {item.name}
            </Button>
          ))}
        </div>

        {error && <div className="notice warning">{error}</div>}

        <div className="picker-list">
          {entries.length === 0 ? (
            <div className="picker-empty">
              {loading ? "读取中…" : "这个文件夹下没有子文件夹（可以直接「选择此文件夹」）"}
            </div>
          ) : (
            entries.map((item) => (
              <button key={item.path} type="button" className="picker-item" onClick={() => void go(item.path)}>
                <FolderIcon size={13} />
                {item.name}
                <span className="arrow">
                  <ChevronIcon size={9} />
                </span>
              </button>
            ))
          )}
        </div>

        <div className="picker-foot" title={current}>
          当前：{current || "—"}
        </div>
      </div>
    </Modal>
  );
}
