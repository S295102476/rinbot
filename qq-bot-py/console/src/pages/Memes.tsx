import { useEffect, useState, type FormEvent } from "react";
import {
  Check,
  Edit3,
  ImageOff,
  LoaderCircle,
  Power,
  RefreshCw,
  Save,
  Search,
  Trash2,
} from "lucide-react";
import {
  errorMessage,
  number,
  query,
  useApi,
  write,
  type ListResult,
  type Row,
} from "../api";
import {
  Badge,
  Empty,
  ErrorState,
  Field,
  IconButton,
  Loading,
  Modal,
  Notice,
  Pagination,
  Timestamp,
} from "../ui";
import { PageHeading } from "./Operations";

const emotions: Record<string, string> = {
  happy: "开心",
  sad: "难过",
  angry: "生气",
  surprised: "惊讶",
  funny: "搞笑",
  cool: "酷",
  disgusted: "嫌弃",
  confused: "困惑",
  curious: "好奇",
  calm: "平静",
  shy: "害羞",
  smug: "得意",
  neutral: "中性",
  unclassified: "未分类",
};
const statuses: Record<string, string> = {
  active: "已启用",
  disabled: "已停用",
  deleted: "已删除",
  missing: "文件缺失",
};
function MemeImage({ item }: { item: Row }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setFailed(false);
    setAttempt(0);
  }, [item.url]);
  return (
    <div className="meme-image">
      {item.url && !failed ? (
        <img
          key={`${item.url}:${attempt}`}
          loading="lazy"
          alt={item.note || item.object_name}
          src={item.url}
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="image-missing">
          <ImageOff size={26} />
          图片不可用
          {item.url && (
            <IconButton
              label={`重新加载图片 ${item.object_name}`}
              onClick={() => {
                setAttempt((value) => value + 1);
                setFailed(false);
              }}
            >
              <RefreshCw size={16} />
            </IconButton>
          )}
        </span>
      )}
    </div>
  );
}

function MemeEditor({
  items,
  onClose,
  onSaved,
}: {
  items: Row[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const batch = items.length > 1;
  const item = items[0];
  const [emotion, setEmotion] = useState(batch ? "__keep" : item.emotion || "");
  const [status, setStatus] = useState(batch ? "" : item.status);
  const [note, setNote] = useState(batch ? "" : item.note || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const patch: Row = {};
    if (emotion !== "__keep") patch.emotion = emotion;
    if (status) patch.status = status;
    if (!batch) patch.note = note;
    try {
      const result = batch
        ? await write<Row>("/memes/batch", {
            ids: items.map((row) => row.id),
            action: "update",
            patch,
          })
        : await write<Row>(`/memes/${item.id}`, patch, "PATCH");
      if (result.failed?.length) {
        onSaved();
        throw new Error(
          `${result.succeeded_ids?.length || 0} 项已保存，${result.failed.length} 项失败。${result.failed.map((row: Row) => `#${row.id}: ${typeof row.detail === "string" ? row.detail : row.detail?.message || "操作失败"}`).join("；")}`,
        );
      }
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={batch ? `批量编辑 · ${items.length} 项` : "编辑表情"}
      onClose={onClose}
    >
      <form onSubmit={save}>
        <div className="modal-body">
          <Notice>{error}</Notice>
          {!batch && (
            <div className="meme-editor-preview">
              <MemeImage item={item} />
              <div>
                <strong className="break-word">{item.object_name}</strong>
                <p className="muted">发送 {number(item.send_count)} 次</p>
                <Timestamp value={item.last_sent_at} />
              </div>
            </div>
          )}
          <div className="form-grid">
            <Field label="情绪分类">
              <select
                value={emotion}
                onChange={(event) => setEmotion(event.target.value)}
              >
                {batch && <option value="__keep">保持不变</option>}
                <option value="">未分类</option>
                {Object.entries(emotions)
                  .filter(([key]) => key !== "unclassified")
                  .map(([key, label]) => (
                    <option value={key} key={key}>
                      {label}
                    </option>
                  ))}
              </select>
            </Field>
            <Field label="状态">
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value)}
              >
                {batch && <option value="">保持不变</option>}
                <option value="active">已启用</option>
                <option value="disabled">已停用</option>
              </select>
            </Field>
            {!batch && (
              <Field label="备注">
                <textarea
                  value={note}
                  maxLength={1000}
                  rows={3}
                  onChange={(event) => setNote(event.target.value)}
                />
              </Field>
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button className="button" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy}>
            <Save size={15} />
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteMemes({
  ids,
  onClose,
  onSaved,
}: {
  ids: number[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [hard, setHard] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function remove() {
    setBusy(true);
    setError("");
    try {
      const result = await write<Row>("/memes/batch", {
        ids,
        action: "delete",
        hard,
      });
      if (result.failed?.length) {
        onSaved();
        throw new Error(
          `${result.succeeded_ids?.length || 0} 项已删除，${result.failed.length} 项失败。${result.failed.map((row: Row) => `#${row.id}: ${typeof row.detail === "string" ? row.detail : row.detail?.message || "操作失败"}`).join("；")}`,
        );
      }
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`删除 ${ids.length} 个表情`} onClose={onClose}>
      <div className="modal-body">
        <Notice>{error}</Notice>
        <p>删除后这些表情将退出可用表情池。</p>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={hard}
            onChange={(event) => {
              setHard(event.target.checked);
              setConfirmed(false);
            }}
          />
          同时永久删除存储文件
        </label>
        {hard && (
          <div className="destructive-warning">
            <p>永久删除无法撤销，原始文件将从对象存储中移除。</p>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              我确认永久删除这 {ids.length} 个文件
            </label>
          </div>
        )}
      </div>
      <div className="modal-footer">
        <button className="button" disabled={busy} onClick={onClose}>
          取消
        </button>
        <button
          className="button danger-button"
          disabled={busy || (hard && !confirmed)}
          onClick={remove}
        >
          {busy ? (
            <LoaderCircle className="spin" size={15} />
          ) : (
            <Trash2 size={15} />
          )}
          {hard ? "永久删除" : "删除表情"}
        </button>
      </div>
    </Modal>
  );
}

export function Memes() {
  const [search, setSearch] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [emotion, setEmotion] = useState("");
  const [status, setStatus] = useState("active");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number[]>([]);
  const [editing, setEditing] = useState<Row[] | null>(null);
  const [deleting, setDeleting] = useState<number[] | null>(null);
  const [actionError, setActionError] = useState("");
  const [pending, setPending] = useState<number | null>(null);
  const { data, loading, error, reload } = useApi<ListResult>(
    `/memes${query({ q: searchQuery, emotion, status, offset, limit: 24 })}`,
  );
  useEffect(() => {
    const timeout = setTimeout(() => {
      setSearchQuery(search);
      setOffset(0);
      setSelected([]);
    }, 300);
    return () => clearTimeout(timeout);
  }, [search]);
  const items = data?.items || [];
  const all =
    items.length > 0 && items.every((item) => selected.includes(item.id));
  const saved = () => {
    setSelected([]);
    reload();
  };
  async function toggle(item: Row) {
    setPending(item.id);
    setActionError("");
    try {
      await write(
        `/memes/${item.id}`,
        { status: item.status === "active" ? "disabled" : "active" },
        "PATCH",
      );
      saved();
    } catch (cause) {
      setActionError(errorMessage(cause));
    } finally {
      setPending(null);
    }
  }
  return (
    <>
      <PageHeading
        title="表情库"
        subtitle={`${data ? number(data.total) : "—"} 个表情`}
      >
        <IconButton label="刷新表情库" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
      </PageHeading>
      <Notice>{actionError}</Notice>
      <section className="data-section meme-section">
        <div className="toolbar">
          <div className="search-input">
            <Search size={16} />
            <input
              aria-label="搜索表情"
              placeholder="搜索文件名称"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <select
            aria-label="情绪筛选"
            value={emotion}
            onChange={(event) => {
              setEmotion(event.target.value);
              setOffset(0);
              setSelected([]);
            }}
          >
            <option value="">全部情绪</option>
            {Object.entries(emotions).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
          <select
            aria-label="表情状态筛选"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
              setSelected([]);
            }}
          >
            {Object.entries(statuses).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
          <span className="toolbar-spacer" />
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={all}
              onChange={(event) =>
                setSelected(
                  event.target.checked ? items.map((item) => item.id) : [],
                )
              }
            />
            选择本页
          </label>
        </div>
        {selected.length > 0 && (
          <div className="batch-bar">
            <span>
              <Check size={15} />
              已选 {selected.length} 项
            </span>
            <button
              className="button"
              onClick={() =>
                setEditing(items.filter((item) => selected.includes(item.id)))
              }
            >
              <Edit3 size={14} />
              批量编辑
            </button>
            <button
              className="button danger-text"
              onClick={() => setDeleting(selected)}
            >
              <Trash2 size={14} />
              删除
            </button>
            <button className="text-button" onClick={() => setSelected([])}>
              取消选择
            </button>
          </div>
        )}
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !items.length ? (
          <Empty title="没有匹配的表情" />
        ) : (
          <div className="meme-grid">
            {items.map((item) => (
              <article
                key={item.id}
                className={`meme-card ${selected.includes(item.id) ? "selected" : ""}`}
              >
                <label className="meme-select">
                  <input
                    type="checkbox"
                    aria-label={`选择表情 ${item.object_name}`}
                    checked={selected.includes(item.id)}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? [...selected, item.id]
                          : selected.filter((id) => id !== item.id),
                      )
                    }
                  />
                </label>
                <MemeImage item={item} />
                <div className="meme-meta">
                  <strong title={item.object_name}>{item.object_name}</strong>
                  <div>
                    <Badge kind="rose">
                      {emotions[item.emotion] || "未分类"}
                    </Badge>
                    <small>{number(item.send_count)} 次发送</small>
                  </div>
                  <div className="meme-actions">
                    <Badge
                      kind={item.status === "active" ? "success" : "neutral"}
                    >
                      {statuses[item.status] || item.status}
                    </Badge>
                    <span />
                    <IconButton
                      label={`编辑表情 ${item.id}`}
                      onClick={() => setEditing([item])}
                    >
                      <Edit3 size={15} />
                    </IconButton>
                    {item.status !== "missing" && (
                      <IconButton
                        label={
                          item.status === "active"
                            ? `停用表情 ${item.id}`
                            : `启用表情 ${item.id}`
                        }
                        disabled={pending === item.id}
                        onClick={() => toggle(item)}
                      >
                        <Power size={15} />
                      </IconButton>
                    )}
                    <IconButton
                      label={`删除表情 ${item.id}`}
                      onClick={() => setDeleting([item.id])}
                    >
                      <Trash2 size={15} />
                    </IconButton>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
        <Pagination
          offset={offset}
          limit={24}
          total={data?.total || 0}
          setOffset={(value) => {
            setOffset(value);
            setSelected([]);
          }}
        />
      </section>
      {editing && (
        <MemeEditor
          items={editing}
          onClose={() => setEditing(null)}
          onSaved={saved}
        />
      )}
      {deleting && (
        <DeleteMemes
          ids={deleting}
          onClose={() => setDeleting(null)}
          onSaved={saved}
        />
      )}
    </>
  );
}
