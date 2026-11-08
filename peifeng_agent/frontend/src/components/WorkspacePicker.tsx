/**
 * 项目内选择器：给快捷模板挑"素材文件"或"要整理的目录"。
 *
 * 只浏览**项目工作目录内部**、返回的也是相对路径——worker的工具就是按相对路径读的，
 * 手填绝对路径反而容易出错（也会跳出项目目录）。浏览逻辑在 `GET /api/workspace/entries`。
 */
import { useQuery } from "@tanstack/react-query";
import { Button, Input, Popover } from "antd";
import { useState } from "react";

import { endpoints, get } from "../api/client";

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
