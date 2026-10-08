import { useEffect, useState, type FormEvent } from "react";
import {
  Edit3,
  Eye,
  Heart,
  Plus,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
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
  Avatar,
  Badge,
  DateRange,
  defaultDates,
  Empty,
  ErrorState,
  Field,
  IconButton,
  JsonDetail,
  Loading,
  Modal,
  Notice,
  Pagination,
  Timestamp,
} from "../ui";
import { PageHeading } from "./Operations";

const kinds: Record<string, string> = {
  facts: "人物事实",
  episodes: "情节记忆",
  summaries: "群聊摘要",
  relationships: "关系状态",
  affinities: "好感度",
};
const categories: Record<string, string> = {
  identity: "身份",
  preference: "偏好",
  habit: "习惯",
  project: "项目",
  relationship: "关系",
  general: "通用",
};

function MemoryEditor({
  kind,
  scope,
  item,
  personas,
  onClose,
  onSaved,
}: {
  kind: string;
  scope: string;
  item: Row;
  personas: Row[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const creating = !item.id;
  const [values, setValues] = useState<Row>({
    group_id: "",
    user_id: "",
    persona_id: personas[0]?.persona_id || "rin",
    category: "general",
    importance: 3,
    confidence: 1,
    status: "active",
    fact: "",
    summary: "",
    message_count: 0,
    explicit_interaction_count: 0,
    last_reason: "",
    affinity_score: 0,
    end_message_id: 0,
    ...item,
  });
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (key: string, value: unknown) =>
    setValues({ ...values, [key]: value });
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (kind === "affinities") {
        await write(
          `/affinities/${values.persona_id}/${values.user_id}`,
          {
            score: Number(values.affinity_score),
            reason,
            group_id: Number(values.group_id || 0),
          },
          "PUT",
        );
      } else {
        const fields: Record<string, string[]> = {
          facts: ["fact", "category", "importance", "confidence", "status"],
          episodes: ["summary", "end_message_id"],
          summaries: ["summary"],
          relationships: [
            "message_count",
            "explicit_interaction_count",
            "last_reason",
          ],
        };
        const payload: Row = { reason };
        fields[kind].forEach((key) => {
          if (values[key] !== "") payload[key] = values[key];
        });
        if (creating) {
          if (scope === "group" || kind !== "facts")
            payload.group_id = Number(values.group_id);
          if (kind === "facts" || kind === "relationships")
            payload.user_id = Number(values.user_id);
          if (kind === "relationships") payload.persona_id = values.persona_id;
        }
        await write(
          `/memory/${kind}${creating ? "" : `/${item.id}`}${query({ scope })}`,
          payload,
          creating ? "POST" : "PATCH",
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
      title={`${creating ? "新增" : "编辑"}${kinds[kind]}`}
      onClose={onClose}
    >
      <form onSubmit={save}>
        <div className="modal-body">
          <Notice>{error}</Notice>
          <div className="form-grid two-columns">
            {(scope === "group" || kind !== "facts") && (
              <Field
                label={kind === "affinities" ? "关联群号（可选）" : "群号"}
              >
                <input
                  type="number"
                  required={kind !== "affinities"}
                  disabled={!creating && kind !== "affinities"}
                  min="1"
                  value={values.group_id}
                  onChange={(event) => set("group_id", event.target.value)}
                />
              </Field>
            )}
            {["facts", "relationships", "affinities"].includes(kind) && (
              <Field label="用户 QQ">
                <input
                  type="number"
                  required
                  min="1"
                  disabled={!creating}
                  value={values.user_id}
                  onChange={(event) => set("user_id", event.target.value)}
                />
              </Field>
            )}
            {["relationships", "affinities"].includes(kind) && (
              <Field label="人格">
                <select
                  required
                  disabled={!creating}
                  value={values.persona_id}
                  onChange={(event) => set("persona_id", event.target.value)}
                >
                  {personas.map((persona) => (
                    <option value={persona.persona_id} key={persona.persona_id}>
                      {persona.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {kind === "facts" && (
              <>
                <Field label="分类">
                  <select
                    value={values.category}
                    onChange={(event) => set("category", event.target.value)}
                  >
                    {Object.entries(categories).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="重要性">
                  <input
                    type="number"
                    required
                    min="1"
                    max="5"
                    value={values.importance}
                    onChange={(event) =>
                      set("importance", Number(event.target.value))
                    }
                  />
                </Field>
                <Field label="置信度">
                  <input
                    type="number"
                    required
                    min="0"
                    max="1"
                    step="0.05"
                    value={values.confidence}
                    onChange={(event) =>
                      set("confidence", Number(event.target.value))
                    }
                  />
                </Field>
                <Field label="状态">
                  <select
                    value={values.status}
                    onChange={(event) => set("status", event.target.value)}
                  >
                    <option value="active">已启用</option>
                    <option value="disabled">已停用</option>
                    <option value="deleted">已删除</option>
                  </select>
                </Field>
              </>
            )}
            {kind === "relationships" && (
              <>
                <Field label="消息计数">
                  <input
                    type="number"
                    required
                    min="0"
                    value={values.message_count}
                    onChange={(event) =>
                      set("message_count", Number(event.target.value))
                    }
                  />
                </Field>
                <Field label="直接交互计数">
                  <input
                    type="number"
                    required
                    min="0"
                    value={values.explicit_interaction_count}
                    onChange={(event) =>
                      set(
                        "explicit_interaction_count",
                        Number(event.target.value),
                      )
                    }
                  />
                </Field>
              </>
            )}
            {kind === "episodes" && (
              <Field label="结束消息 ID">
                <input
                  type="number"
                  required
                  min="0"
                  value={values.end_message_id}
                  onChange={(event) =>
                    set("end_message_id", Number(event.target.value))
                  }
                />
              </Field>
            )}
            {kind === "affinities" && (
              <Field label="好感度分值">
                <input
                  type="number"
                  required
                  min="-100"
                  max="100"
                  step="0.1"
                  value={values.affinity_score}
                  onChange={(event) =>
                    set("affinity_score", Number(event.target.value))
                  }
                />
              </Field>
            )}
          </div>
          {["facts", "episodes", "summaries"].includes(kind) && (
            <Field label="内容">
              <textarea
                required
                rows={6}
                maxLength={kind === "facts" ? 500 : 10000}
                value={values[kind === "facts" ? "fact" : "summary"]}
                onChange={(event) =>
                  set(kind === "facts" ? "fact" : "summary", event.target.value)
                }
              />
            </Field>
          )}
          {kind === "relationships" && (
            <Field label="关系备注">
              <textarea
                rows={3}
                maxLength={300}
                value={values.last_reason}
                onChange={(event) => set("last_reason", event.target.value)}
              />
            </Field>
          )}
          <Field label="修改原因">
            <textarea
              required
              rows={2}
              maxLength={300}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
        <div className="modal-footer">
          <button type="button" className="button" onClick={onClose}>
            取消
          </button>
          <button className="button primary" disabled={busy || !reason.trim()}>
            <Save size={15} />
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteMemory({
  kind,
  scope,
  item,
  onClose,
  onSaved,
}: {
  kind: string;
  scope: string;
  item: Row;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function remove(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await write(
        `/memory/${kind}/${item.id}${query({ scope })}`,
        { reason },
        "DELETE",
      );
      onSaved();
      onClose();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`删除${kinds[kind]}`} onClose={onClose}>
      <form onSubmit={remove}>
        <div className="modal-body">
          <Notice>{error}</Notice>
          <p>删除记录 #{item.id}？此操作将保留人工修订保护记录。</p>
          <Field label="删除原因">
            <textarea
              required
              rows={3}
              value={reason}
              maxLength={300}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
        <div className="modal-footer">
          <button className="button" type="button" onClick={onClose}>
            取消
          </button>
          <button
            className="button danger-button"
            disabled={busy || !reason.trim()}
          >
            <Trash2 size={15} />
            {busy ? "删除中…" : "确认删除"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function Memory() {
  const [kind, setKind] = useState("facts");
  const [scope, setScope] = useState("group");
  const [group, setGroup] = useState("");
  const [user, setUser] = useState("");
  const [persona, setPersona] = useState("");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Row>({});
  const [offset, setOffset] = useState(0);
  const [sortBy, setSortBy] = useState("updated_at");
  const [editing, setEditing] = useState<Row | null>(null);
  const [deleting, setDeleting] = useState<Row | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);
  const { data: personaData } = useApi<Row>("/personas");
  useEffect(() => {
    const timeout = setTimeout(() => {
      setFilters({
        group_id: group,
        user_id: user,
        persona_id: persona,
        q: search,
      });
      setOffset(0);
    }, 300);
    return () => clearTimeout(timeout);
  }, [group, user, persona, search]);
  const { data, loading, error, reload } = useApi<ListResult>(
    `/memory/${kind}${query({ scope, ...filters, offset, limit: 25, ...(["relationships", "affinities"].includes(kind) ? { sort_by: sortBy, sort_order: "desc" } : {}) })}`,
  );
  const items = data?.items || [];
  return (
    <>
      <PageHeading
        title="记忆与关系"
        subtitle={`${data ? number(data.total) : "—"} 条${kinds[kind]}`}
      >
        <IconButton label="刷新记忆" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
        <button className="button primary" onClick={() => setEditing({})}>
          <Plus size={16} />
          {kind === "affinities" ? "调整好感度" : "新增记录"}
        </button>
      </PageHeading>
      <div className="tabs">
        {Object.entries(kinds).map(([key, label]) => (
          <button
            key={key}
            className={kind === key ? "active" : ""}
            onClick={() => {
              setKind(key);
              setOffset(0);
              setSortBy("updated_at");
            }}
          >
            {key === "affinities" && <Heart size={15} />}
            {label}
          </button>
        ))}
      </div>
      <section className="data-section">
        <div className="toolbar memory-filters">
          {kind === "facts" && (
            <select
              aria-label="记忆范围"
              value={scope}
              onChange={(event) => {
                setScope(event.target.value);
                setOffset(0);
              }}
            >
              <option value="group">群内记忆</option>
              <option value="global">全局记忆</option>
            </select>
          )}
          <input
            aria-label="按群号筛选"
            inputMode="numeric"
            placeholder="群号"
            value={group}
            onChange={(event) =>
              setGroup(event.target.value.replace(/\D/g, ""))
            }
          />
          <input
            aria-label="按用户筛选"
            inputMode="numeric"
            placeholder="用户 QQ"
            value={user}
            onChange={(event) => setUser(event.target.value.replace(/\D/g, ""))}
          />
          <select
            aria-label="按人格筛选"
            value={persona}
            onChange={(event) => setPersona(event.target.value)}
          >
            <option value="">全部人格</option>
            {(personaData?.items || []).map((row: Row) => (
              <option key={row.persona_id} value={row.persona_id}>
                {row.name}
              </option>
            ))}
          </select>
          {["relationships", "affinities"].includes(kind) && (
            <select
              aria-label="用户列表排序"
              value={sortBy}
              onChange={(event) => {
                setSortBy(event.target.value);
                setOffset(0);
              }}
            >
              <option value="updated_at">最近更新</option>
              {kind === "relationships" ? (
                <option value="message_count">消息条数：从高到低</option>
              ) : (
                <option value="affinity_score">好感度：从高到低</option>
              )}
            </select>
          )}
          {["facts", "episodes", "summaries"].includes(kind) && (
            <div className="search-input">
              <Search size={16} />
              <input
                aria-label="搜索记忆内容"
                placeholder="搜索内容"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
          )}
        </div>
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !items.length ? (
          <Empty title="暂无匹配的记忆记录" />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>
                    {kind === "summaries" || kind === "episodes"
                      ? "群聊"
                      : "用户"}
                  </th>
                  <th>
                    {kind === "affinities"
                      ? "好感度"
                      : kind === "relationships"
                        ? "关系状态"
                        : "内容"}
                  </th>
                  <th>
                    {["relationships", "affinities"].includes(kind)
                      ? "人格"
                      : "分类 / 状态"}
                  </th>
                  <th>更新时间</th>
                  <th className="right">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <div className="identity">
                        <Avatar
                          id={item.user_id || item.group_id}
                          group={!item.user_id}
                        />
                        <div>
                          <strong>{item.user_id || item.group_id}</strong>
                          <small>
                            {item.group_id && item.user_id
                              ? `群 ${item.group_id}`
                              : item.scope === "global"
                                ? "全局"
                                : ""}
                          </small>
                        </div>
                      </div>
                    </td>
                    <td className="memory-content">
                      {kind === "affinities" ? (
                        <span
                          className={`affinity-score ${item.affinity_score >= 0 ? "positive" : "negative"}`}
                        >
                          <Heart size={15} />
                          {number(item.affinity_score)}
                        </span>
                      ) : kind === "relationships" ? (
                        <>
                          <strong>
                            {number(item.message_count)} 条消息 ·{" "}
                            {number(item.explicit_interaction_count)} 次交互
                          </strong>
                          <p>{item.last_reason || "—"}</p>
                        </>
                      ) : (
                        <p>{item.fact || item.summary || "—"}</p>
                      )}
                      {item.protected && (
                        <span className="protected-label">
                          <ShieldCheck size={11} />
                          人工修订
                        </span>
                      )}
                    </td>
                    <td>
                      {item.persona_id ? (
                        <Badge kind="rose">{item.persona_id}</Badge>
                      ) : (
                        <>
                          <Badge>
                            {categories[item.category] ||
                              (kind === "episodes" ? "情节" : "摘要")}
                          </Badge>
                          {item.status && (
                            <small className="cell-note">
                              {item.status === "active"
                                ? "已启用"
                                : item.status === "disabled"
                                  ? "已停用"
                                  : item.status}
                            </small>
                          )}
                        </>
                      )}
                    </td>
                    <td>
                      <Timestamp value={item.updated_at} />
                    </td>
                    <td className="right">
                      <IconButton
                        label={`查看记录 ${item.id}`}
                        onClick={() => setDetail(item)}
                      >
                        <Eye size={15} />
                      </IconButton>
                      <IconButton
                        label={`编辑记录 ${item.id}`}
                        onClick={() => setEditing(item)}
                      >
                        <Edit3 size={15} />
                      </IconButton>
                      {kind !== "affinities" && (
                        <IconButton
                          label={`删除记录 ${item.id}`}
                          onClick={() => setDeleting(item)}
                        >
                          <Trash2 size={15} />
                        </IconButton>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pagination
          offset={offset}
          total={data?.total || 0}
          limit={25}
          setOffset={setOffset}
        />
      </section>
      {editing && (
        <MemoryEditor
          kind={kind}
          scope={scope}
          item={editing}
          personas={personaData?.items || []}
          onClose={() => setEditing(null)}
          onSaved={reload}
        />
      )}
      {deleting && (
        <DeleteMemory
          kind={kind}
          scope={scope}
          item={deleting}
          onClose={() => setDeleting(null)}
          onSaved={reload}
        />
      )}
      {detail && (
        <Modal
          title={`${kinds[kind]} · #${detail.id}`}
          onClose={() => setDetail(null)}
          wide
        >
          <div className="modal-body">
            <JsonDetail value={detail} />
          </div>
        </Modal>
      )}
    </>
  );
}

export function Records({ kind }: { kind: "requests" | "audits" }) {
  const [[from, to], setDates] = useState(defaultDates);
  const [offset, setOffset] = useState(0);
  const [group, setGroup] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [action, setAction] = useState("");
  const [filters, setFilters] = useState<Row>({});
  const [detail, setDetail] = useState<Row | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters({ group_id: group, status, action, source });
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [group, status, action, source]);
  const { data, loading, error, reload } = useApi<ListResult>(
    `/${kind}${query({ date_from: from, date_to: to, ...filters, offset, limit: 25 })}`,
  );
  return (
    <>
      <PageHeading
        title={kind === "requests" ? "请求记录" : "操作审计"}
        subtitle={kind === "requests" ? "模型调用与响应状态" : "管理员变更记录"}
      >
        <IconButton label="刷新记录" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
      </PageHeading>
      <section className="data-section">
        <div className="toolbar">
          <DateRange
            from={from}
            to={to}
            onChange={(a, b) => {
              setDates([a, b]);
              setOffset(0);
            }}
          />
          {kind === "requests" ? (
            <>
              <input
                className="short-input"
                aria-label="请求群号筛选"
                inputMode="numeric"
                placeholder="群号"
                value={group}
                onChange={(event) =>
                  setGroup(event.target.value.replace(/\D/g, ""))
                }
              />
              <select
                aria-label="请求状态筛选"
                value={status}
                onChange={(event) => setStatus(event.target.value)}
              >
                <option value="">全部状态</option>
                <option value="success">成功</option>
                <option value="failed">失败</option>
                <option value="timeout">超时</option>
                <option value="unknown">未知</option>
              </select>
              <select aria-label="请求用途" value={source} onChange={event => setSource(event.target.value)}>
                <option value="">全部用途</option><option value="group_decision">群聊决策</option><option value="summary">摘要</option><option value="dev_decision">开发</option><option value="chat">其他聊天</option>
              </select>
            </>
          ) : (
            <div className="search-input">
              <Search size={16} />
              <input
                aria-label="审计操作筛选"
                placeholder="筛选操作类型"
                value={action}
                onChange={(event) => setAction(event.target.value)}
              />
            </div>
          )}
        </div>
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !data?.items.length ? (
          <Empty title="所选条件下暂无记录" />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                {kind === "requests" ? (
                  <tr>
                    <th>时间</th>
                    <th>模型 / 来源</th>
                    <th>群号</th>
                    <th>状态</th>
                    <th>耗时</th>
                    <th>请求 / 实际输入</th>
                    <th>Tokens</th>
                    <th />
                  </tr>
                ) : (
                  <tr>
                    <th>时间</th>
                    <th>管理员</th>
                    <th>操作</th>
                    <th>目标</th>
                    <th />
                  </tr>
                )}
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Timestamp value={row.created_at} />
                    </td>
                    {kind === "requests" ? (
                      <>
                        <td>
                          <strong>{row.model || "—"}</strong>
                          <small className="cell-note">
                            {[row.provider, row.source]
                              .filter(Boolean)
                              .join(" · ") || "—"}
                          </small>
                        </td>
                        <td>{row.group_id || "—"}</td>
                        <td>
                          <Badge
                            kind={
                              row.status === "ok" || row.status === "success"
                                ? "success"
                                : row.status === "error" ||
                                    row.status === "timeout"
                                  ? "danger"
                                  : "neutral"
                            }
                          >
                            {(
                              {
                                ok: "成功",
                                success: "成功",
                                error: "失败",
                                failed: "失败",
                                cancelled: "已取消",
                                retry_without_search: "联网重试",
                                timeout: "超时",
                                unknown: "未知",
                              } as Row
                            )[row.status] || row.status}
                          </Badge>
                        </td>
                        <td>
                          {row.latency_ms == null
                            ? "—"
                            : `${number(row.latency_ms)} ms`}
                        </td>
                        <td><code>{row.request_id || "未知"}</code><small className="cell-note">第 {row.attempt ?? "未知"} 次 / 排队 {row.queue_wait_ms ?? "未知"} ms</small><small className="cell-note">{row.input_chars ?? "未知"} 字符 / {row.image_count ?? "未知"} 张图</small></td>
                        <td>
                          {row.total_tokens == null ? "未知" : number(row.total_tokens)}
                          <small className="cell-note">
                            {row.input_tokens == null ? "未知" : number(row.input_tokens)} 输入 /{" "}
                            {row.output_tokens == null ? "未知" : number(row.output_tokens)} 输出
                          </small>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>{row.actor}</td>
                        <td>
                          <Badge>{row.action}</Badge>
                        </td>
                        <td>{row.target}</td>
                      </>
                    )}
                    <td>
                      <IconButton
                        label={`查看详情 ${row.id}`}
                        onClick={() => setDetail(row)}
                      >
                        <Eye size={16} />
                      </IconButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pagination
          offset={offset}
          total={data?.total || 0}
          limit={25}
          setOffset={setOffset}
        />
      </section>
      {detail && (
        <Modal
          wide
          title={kind === "requests" ? "请求详情" : "变更详情"}
          onClose={() => setDetail(null)}
        >
          <div className="modal-body">
            <JsonDetail value={detail} />
          </div>
        </Modal>
      )}
    </>
  );
}
