import { useEffect, useState } from "react";
import {
  FileText,
  Files,
  Search,
  CheckCircle2,
  AlertCircle,
  Clock3,
} from "lucide-react";
import { request } from "./api";
import type { Photo, Task } from "./types";
import { statuses } from "./types";
import {
  filterPhotos,
  imageStatus,
  toggleVisible,
} from "./workspacePreferences";

export function PhotoList({
  photos,
  tasks,
  active,
  selected,
  onSelect,
  onSelection,
}: {
  photos: Photo[];
  tasks: Task[];
  active: string;
  selected: string[];
  onSelect: (p: Photo) => void;
  onSelection: (ids: string[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const visible = filterPhotos(photos, tasks, search, status);
  const ids = visible.map((p) => p.id);
  const checked = ids.length > 0 && ids.every((id) => selected.includes(id));
  const hiddenSelected = selected.filter((id) => !ids.includes(id)).length;
  return (
    <>
      <div className="photo-filters">
        <label className="file-search">
          <Search size={15} />
          <input
            aria-label="搜索图片文件名"
            placeholder="搜索文件名"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <select
          aria-label="筛选图片状态"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="all">全部状态</option>
          <option value="unrecognized">未识别</option>
          <option value="processing">处理中</option>
          <option value="succeeded">已完成</option>
          <option value="failed">失败</option>
        </select>
      </div>
      <div className="photo-list-head">
        <label>
          <input
            type="checkbox"
            aria-label="选择全部图片"
            checked={checked}
            ref={(el) => {
              if (el)
                el.indeterminate =
                  !checked && ids.some((id) => selected.includes(id));
            }}
            onChange={(e) =>
              onSelection(toggleVisible(selected, ids, e.target.checked))
            }
          />
          图片{" "}
          <span>
            {visible.length} / {photos.length}
          </span>
        </label>
        <span>{selected.length ? `已选 ${selected.length}` : ""}</span>
      </div>
      {hiddenSelected > 0 && (
        <div className="selection-notice">
          含 {hiddenSelected} 张筛选外已选图片{" "}
          <button onClick={() => onSelection([])}>清空选择</button>
        </div>
      )}
      <div className="photo-list">
        {visible.map((p) => {
          const state = imageStatus(p.id, tasks);
          return (
            <div
              key={p.id}
              className={`photo-item ${p.id === active ? "active" : ""}`}
            >
              <input
                aria-label={`选择 ${p.name}`}
                type="checkbox"
                checked={selected.includes(p.id)}
                onChange={(e) =>
                  onSelection(
                    e.target.checked
                      ? [...selected, p.id]
                      : selected.filter((id) => id !== p.id),
                  )
                }
              />
              <button
                className="photo-open"
                aria-label={`打开 ${p.name}`}
                aria-current={p.id === active ? "true" : undefined}
                onClick={() => onSelect(p)}
              >
                <Thumbnail id={p.active_version} />
                <span className="photo-name">
                  <strong title={p.name}>{p.name}</strong>
                  <small className={`photo-status ${state}`}>
                    {state === "succeeded" ? (
                      <CheckCircle2 size={12} />
                    ) : state === "failed" ? (
                      <AlertCircle size={12} />
                    ) : (
                      <Clock3 size={12} />
                    )}{" "}
                    {state === "processing"
                      ? "处理中"
                      : state === "unrecognized"
                        ? "尚未识别"
                        : statuses[state] || state}
                  </small>
                </span>
              </button>
            </div>
          );
        })}
        {!visible.length && (
          <div className="sidebar-empty">
            <Files size={28} />
            <p>{photos.length ? "没有匹配的图片" : "导入你的第一份资料"}</p>
            <small>
              {photos.length ? "调整文件名或状态筛选" : "支持图片与文件夹拖入"}
            </small>
          </div>
        )}
      </div>
    </>
  );
}
function Thumbnail({ id }: { id: string }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let alive = true;
    let value = "";
    setUrl("");
    request("/versions/" + id + "/thumbnail")
      .then((r) => r.blob())
      .then((blob) => {
        value = URL.createObjectURL(blob);
        if (alive) setUrl(value);
        else URL.revokeObjectURL(value);
      })
      .catch(() => {});
    return () => {
      alive = false;
      if (value) URL.revokeObjectURL(value);
    };
  }, [id]);
  return (
    <span className="thumbnail">
      {url ? <img src={url} alt="" /> : <FileText size={20} />}
    </span>
  );
}
