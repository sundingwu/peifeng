/**
 * 产出文件：任务结果里的交付文件，点击按附件下载。
 *
 * 服务端只接受文件名（不接受路径），所以这里先把路径里的文件名取出来再拼下载地址。
 */
import { downloadUrl } from "../api/client";
import { FileIcon } from "./icons";

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
            <a key={path} className="file-chip" href={downloadUrl(name)} title={`下载 ${path}`}>
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
