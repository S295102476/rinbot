import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowRightLeft,
  CalendarDays,
  Check,
  Code2,
  Download,
  Eye,
  FileText,
  Fingerprint,
  History,
  RefreshCw,
  RotateCcw,
  Save,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { errorMessage, useApi, write, type ListResult, type Row } from "../api";
import {
  Badge,
  Confirm,
  Empty,
  ErrorState,
  Field,
  IconButton,
  Loading,
  Modal,
  Notice,
  Timestamp,
  useUnsavedChanges,
} from "../ui";
import { PageHeading } from "./Operations";

function SwitchPersona({
  persona,
  onClose,
  onSaved,
}: {
  persona: Row;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await write("/personas/switch", {
        persona_id: persona.persona_id,
        reason,
      });
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`切换到 ${persona.name}`} onClose={onClose}>
      <form onSubmit={save}>
        <div className="modal-body">
          <Notice>{error}</Notice>
          <p>此操作将更改所有群聊的当前人格。</p>
          <Field label="切换原因">
            <textarea
              autoFocus
              required
              maxLength={300}
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
        <div className="modal-footer">
          <button className="button" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy || !reason.trim()}>
            <ArrowRightLeft size={15} />
            {busy ? "切换中…" : "确认切换"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function RevisionHistory({
  document,
  onClose,
  onRestore,
}: {
  document: Row;
  onClose: () => void;
  onRestore: () => void;
}) {
  const { data, loading, error, reload } = useApi<ListResult>(
    `/personas/documents/${document.id}/revisions`,
  );
  const [candidate, setCandidate] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  async function restore() {
    setBusy(true);
    setSaveError("");
    try {
      await write(`/personas/documents/${document.id}/restore`, {
        revision_id: candidate?.id,
        version: document.version,
      });
      onRestore();
      onClose();
    } catch (cause) {
      setSaveError(errorMessage(cause));
      setCandidate(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`版本历史 · ${document.name}`} onClose={onClose} wide>
      <div className="modal-body">
        <Notice>{saveError}</Notice>
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !data?.items.length ? (
          <Empty title="暂无历史版本" />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>版本</th>
                  <th>保存时间</th>
                  <th>管理员</th>
                  <th>原因</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <code>{row.version.slice(0, 10)}</code>
                      {row.version === document.version && (
                        <Badge kind="success">当前</Badge>
                      )}
                    </td>
                    <td>
                      <Timestamp value={row.created_at} />
                    </td>
                    <td>{row.actor}</td>
                    <td className="wrap-cell">{row.reason || "—"}</td>
                    <td>
                      <IconButton
                        label={`恢复版本 ${row.version.slice(0, 10)}`}
                        disabled={row.version === document.version}
                        onClick={() => setCandidate(row)}
                      >
                        <RotateCcw size={16} />
                      </IconButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {candidate && (
        <Confirm
          title="恢复历史版本"
          onClose={() => setCandidate(null)}
          onConfirm={restore}
          busy={busy}
          danger={false}
        >
          当前文档将替换为版本 <code>{candidate.version.slice(0, 10)}</code>
          ，现有内容将自动备份。
        </Confirm>
      )}
    </Modal>
  );
}

function DocumentEditor({ id, onDirty }: { id: string; onDirty: (value: boolean) => void }) {
  const { data, loading, error, reload } = useApi<Row>(
    `/personas/documents/${id}`,
  );
  const [content, setContent] = useState("");
  const [view, setView] = useState("edit");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);
  const [history, setHistory] = useState(false);
  const dirty = data && content !== data.content;
  useUnsavedChanges(!!dirty);
  useEffect(() => { onDirty(!!dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => {
    if (data) setContent(data.content);
  }, [data]);
  async function save() {
    setBusy(true);
    setSaveError("");
    setSaved(false);
    try {
      await write(
        `/personas/documents/${id}`,
        { content, version: data?.version, reason },
        "PUT",
      );
      setSaved(true);
      setReason("");
      reload();
    } catch (cause) {
      setSaveError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  function download() {
    const url = URL.createObjectURL(
      new Blob([data?.content || ""], { type: "text/markdown;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${data?.persona_id}-${data?.name}`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} retry={reload} />;
  if (!data) return <Empty />;
  return (
    <div className="document-editor">
      <div className="editor-heading">
        <span>
          <FileText size={17} />
          <strong>{data.path}</strong>
          {dirty && <Badge kind="warning">未保存</Badge>}
        </span>
        <div className="editor-actions">
          <div className="segmented" role="group" aria-label="文档视图">
            <button
              aria-pressed={view === "edit"}
              className={view === "edit" ? "active" : ""}
              title="编辑"
              onClick={() => setView("edit")}
            >
              <Code2 size={16} />
              <span>编辑</span>
            </button>
            <button
              aria-pressed={view === "preview"}
              className={view === "preview" ? "active" : ""}
              title="预览"
              onClick={() => setView("preview")}
            >
              <Eye size={16} />
              <span>预览</span>
            </button>
          </div>
          <IconButton label="下载文档备份" onClick={download}>
            <Download size={16} />
          </IconButton>
          <IconButton label="查看版本历史" onClick={() => setHistory(true)}>
            <History size={16} />
          </IconButton>
        </div>
      </div>
      <Notice>{saveError}</Notice>
      {saved && <Notice kind="success">文档已保存并备份</Notice>}
      {view === "edit" ? (
        <textarea
          aria-label="Markdown 文档内容"
          className="markdown-input"
          spellCheck={false}
          value={content}
          onChange={(event) => {
            setContent(event.target.value);
            setSaved(false);
          }}
        />
      ) : (
        <article className="markdown-preview">
          <ReactMarkdown>{content}</ReactMarkdown>
        </article>
      )}
      <div className="editor-footer">
        <input
          aria-label="文档修改原因"
          placeholder="修改原因（可选）"
          value={reason}
          maxLength={300}
          onChange={(event) => setReason(event.target.value)}
        />
        <span className="muted">{content.length.toLocaleString()} 字符</span>
        <button
          className="button primary"
          disabled={!dirty || busy}
          onClick={save}
        >
          <Save size={15} />
          {busy ? "保存中…" : "保存文档"}
        </button>
      </div>
      {history && (
        <RevisionHistory
          document={data}
          onClose={() => setHistory(false)}
          onRestore={reload}
        />
      )}
    </div>
  );
}

function Roster() {
  const { data, loading, error, reload } = useApi<ListResult>("/roster");
  return (
    <section className="data-section">
      <div className="section-heading">
        <div>
          <h2>
            <CalendarDays size={17} />
            值班日历
          </h2>
        </div>
        <Badge>只读</Badge>
      </div>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.items.length ? (
        <Empty title="暂无值班安排" />
      ) : (
        <div className="roster-grid">
          {data.items.map((row) => (
            <div className="roster-day" key={row.duty_date}>
              <small>{row.duty_date}</small>
              <strong>{row.persona_id}</strong>
              <Badge kind={row.applied_at ? "success" : "neutral"}>
                {row.applied_at ? "已执行" : "待值班"}
              </Badge>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SwitchHistory() {
  const { data, loading, error, reload } =
    useApi<ListResult>("/personas/history");
  return (
    <section className="data-section">
      <div className="section-heading">
        <h2>人格切换记录</h2>
      </div>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.items.length ? (
        <Empty title="暂无切换记录" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>原人格</th>
                <th>新人格</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={row.id}>
                  <td>
                    <Timestamp value={row.created_at} />
                  </td>
                  <td>{row.from_persona_id || "—"}</td>
                  <td>{row.to_persona_id || row.persona_id || "—"}</td>
                  <td className="wrap-cell">{row.note || row.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function Personas() {
  const { data, loading, error, reload } = useApi<Row>("/personas");
  const [selected, setSelected] = useState("");
  const [document, setDocument] = useState("");
  const [switching, setSwitching] = useState<Row | null>(null);
  const [tab, setTab] = useState("documents");
  const [dirty, setDirty] = useState(false);
  const canLeave = () => !dirty || window.confirm('有未保存的文档更改，确定离开？');
  const personas: Row[] = data?.items || [];
  const current =
    personas.find((persona) => persona.persona_id === selected) ||
    personas.find((persona) => persona.persona_id === data?.active_id) ||
    personas[0];
  const documents: Row[] = current?.documents || [];
  const selectedDocument =
    documents.find((row) => row.id === document) || documents[0];
  return (
    <>
      <PageHeading title="人格管理" subtitle="人格文档与值班安排">
        <IconButton label="刷新人格" onClick={() => { if (canLeave()) reload(); }}>
          <RefreshCw size={17} />
        </IconButton>
      </PageHeading>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !personas.length ? (
        <Empty title="暂无已注册人格" />
      ) : (
        <>
          <div className="persona-selector">
            {personas.map((persona) => (
              <div
                key={persona.persona_id}
                className={`persona-option ${current?.persona_id === persona.persona_id ? "selected" : ""}`}
              >
                <button
                  className="persona-select"
                  onClick={() => {
                    if (!canLeave()) return;
                    setSelected(persona.persona_id);
                    setDocument("");
                  }}
                >
                  <span className="persona-icon">
                    <Fingerprint size={23} />
                  </span>
                  <span>
                    <strong>{persona.name}</strong>
                    <small>{persona.persona_id}</small>
                  </span>
                </button>
                {data?.active_id === persona.persona_id ? (
                  <Badge kind="success">
                    <Check size={12} />
                    当前人格
                  </Badge>
                ) : (
                  <IconButton
                    label={`切换到 ${persona.name}`}
                    onClick={() => { if (canLeave()) setSwitching(persona); }}
                  >
                    <ArrowRightLeft size={17} />
                  </IconButton>
                )}
              </div>
            ))}
          </div>
          <div className="tabs">
            <button
              className={tab === "documents" ? "active" : ""}
              onClick={() => { if (canLeave()) setTab("documents"); }}
            >
              <FileText size={16} />
              人格文档
            </button>
            <button
              className={tab === "roster" ? "active" : ""}
              onClick={() => { if (canLeave()) setTab("roster"); }}
            >
              <CalendarDays size={16} />
              值班日历
            </button>
            <button
              className={tab === "history" ? "active" : ""}
              onClick={() => { if (canLeave()) setTab("history"); }}
            >
              <History size={16} />
              切换记录
            </button>
          </div>
          {tab === "documents" && (
            <div className="documents-layout">
              <aside className="document-list" aria-label="人格文档">
                {documents.map((row) => (
                  <button
                    key={row.id}
                    className={selectedDocument?.id === row.id ? "active" : ""}
                    onClick={() => { if (canLeave()) setDocument(row.id); }}
                  >
                    <FileText size={16} />
                    <span>{row.name}</span>
                    {row.shared && <small>共享</small>}
                  </button>
                ))}
              </aside>
              <div className="document-surface">
                {selectedDocument ? (
                  <DocumentEditor
                    key={selectedDocument.id}
                    id={selectedDocument.id}
                    onDirty={setDirty}
                  />
                ) : (
                  <Empty title="暂无 Markdown 文档" />
                )}
              </div>
            </div>
          )}
          {tab === "roster" && <Roster />}
          {tab === "history" && <SwitchHistory />}
        </>
      )}
      {switching && (
        <SwitchPersona
          persona={switching}
          onClose={() => setSwitching(null)}
          onSaved={reload}
        />
      )}
    </>
  );
}
