import { useEffect, useState, type FormEvent } from "react";
import { Brain, Database, RefreshCw, Save, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { api, dateTime, errorMessage, query, useApi, write } from "../api";
import { Badge, Empty, ErrorState, Field, Loading, Notice } from "../ui";
import { PageHeading } from "./Operations";

type Route = {
  role: string; provider: string; protocol: string; model: string; timeout: number;
  max_output_tokens: number; concurrency: number; daily_limit: number;
  enabled: boolean; source: string; version: number; configured: boolean;
};
const routeLabels: Record<string, string> = {
  group_chat: "主聊天", memory: "记忆整理", emotion: "情绪判断", meme: "表情分类",
  embedding: "语义向量", expert: "GPT 群聊专家", development: "GPT 开发助手",
};
const memoryLabels: Record<string, string> = {
  fact: "人物事实", episode: "事件记忆", style: "交流风格", joke: "群梗", candidate: "待整理候选",
};

function RouteEditor({ route, reload }: { route: Route; reload: () => void }) {
  const [draft, setDraft] = useState(route);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const title = routeLabels[route.role] || route.role;
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const { provider, protocol, model, timeout, max_output_tokens, concurrency, daily_limit, enabled } = draft;
      await write(`/agent/routes/${route.role}`, {
        version: route.version,
        patch: { provider, protocol, model, timeout, max_output_tokens, concurrency, daily_limit, enabled },
      }, "PATCH");
      reload();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  return <form className="card route-card" onSubmit={save}>
    <div className="section-heading">
      <div><h2><Sparkles size={16} />{title}</h2><span>来源 {route.source} · 版本 {route.version}</span></div>
      <Badge kind={route.configured ? "success" : "warning"}>{route.configured ? "凭据已配置" : "待配置凭据"}</Badge>
    </div>
    <div className="route-body">
      <Notice>{error}</Notice>
      {!route.configured && <p className="muted route-hint">请在服务器本地配置接口地址和凭据，再启用此用途。</p>}
      <div className="form-grid route-fields">
        <Field label="供应商"><select aria-label={`${title}供应商`} value={draft.provider} onChange={event => setDraft({ ...draft, provider: event.target.value })}>
          {["antigravity", "google", "openai_responses", "primary", "fallback"].map(value => <option key={value}>{value}</option>)}
        </select></Field>
        <Field label="接口协议"><select aria-label={`${title}协议`} value={draft.protocol} onChange={event => setDraft({ ...draft, protocol: event.target.value })}>
          {["openai", "responses", "google", "embedding"].map(value => <option key={value}>{value}</option>)}
        </select></Field>
        <Field label="模型"><input aria-label={`${title}模型`} value={draft.model} required onChange={event => setDraft({ ...draft, model: event.target.value })} /></Field>
        {([
          ["timeout", "单次超时（秒）"],
          ["max_output_tokens", route.role === "embedding" ? "向量维度" : "输出 token 上限"],
          ["concurrency", "并发上限"],
          ["daily_limit", "专家每日次数"],
        ] as const).filter(([key]) => key !== "daily_limit" || route.role === "expert").map(([key, label]) =>
          <Field label={label} key={key}><input aria-label={`${title}${label}`} type="number" min={1} step={1} required value={draft[key]} onChange={event => setDraft({ ...draft, [key]: Number(event.target.value) })} /></Field>)}
      </div>
    </div>
    <div className="route-footer">
      <label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用{title}</label>
      <button type="submit" className="button primary" disabled={busy}><Save size={15} />{busy ? "保存中…" : `保存${title}`}</button>
    </div>
  </form>;
}

export function AgentRoutes() {
  const state = useApi<{ items: Route[] }>("/agent/routes");
  return <>
    <PageHeading title="用途路由" subtitle="为聊天、记忆和专家分别设置模型；接口地址与密钥由服务器本地配置管理。">
      <button className="button" onClick={state.reload}><RefreshCw size={15} />刷新</button>
    </PageHeading>
    {state.loading ? <Loading /> : state.error ? <ErrorState message={state.error} retry={state.reload} /> :
      !state.data?.items.length ? <Empty title="暂无模型路由" /> :
        <div className="agent-page-stack">{state.data.items.map(route => <RouteEditor key={`${route.role}:${route.version}`} route={route} reload={state.reload} />)}</div>}
  </>;
}

type MemoryRecord = {
  id: number; group_id: number; user_id: number; kind: string; content: string;
  source: string; version: number; protected: boolean; evidence: unknown[];
};
type Privacy = { learning: boolean; style_learning: boolean; evaluation: boolean; version: number };

export function MemoryV2() {
  const [group, setGroup] = useState("");
  const [user, setUser] = useState("");
  const [kind, setKind] = useState("");
  const [error, setError] = useState("");
  const [report, setReport] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const state = useApi<{ items: MemoryRecord[] }>(`/agent/memory${query({ group_id: group, user_id: user, kind })}`);
  const privacy = useApi<Privacy>(user ? `/agent/privacy/${user}${query({ group_id: group || 0 })}` : null);
  useEffect(() => { setReport(null); setError(""); }, [group, user]);

  async function changePrivacy(key: string, value: boolean) {
    if (!privacy.data) return;
    setBusy(true); setError("");
    try {
      await write(`/agent/privacy/${user}`, { group_id: Number(group || 0), version: privacy.data.version, [key]: value }, "PATCH");
      privacy.reload(); state.reload(); setReport(null);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function showReport() {
    const path = `/agent/reports${query({ group_id: group, user_id: user })}`;
    setBusy(true); setError("");
    try { setReport(await api(path)); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }

  return <>
    <PageHeading title="长期记忆" subtitle="按群查看事实、事件、交流风格与群梗；人工修订后受保护。">
      <button className="button" onClick={() => { state.reload(); privacy.reload(); }}><RefreshCw size={15} />刷新</button>
    </PageHeading>
    <Notice>{error}</Notice>
    <div className="agent-page-stack">
      <section className="card memory-filter-card" aria-label="记忆筛选">
        <div className="form-grid memory-filter-grid">
          <Field label="群号" hint="留空查看各群及公开稳定信息"><input aria-label="记忆群号" inputMode="numeric" placeholder="输入群号" value={group} onChange={event => setGroup(event.target.value.replace(/\D/g, ""))} /></Field>
          <Field label="QQ" hint="指定用户后可管理学习偏好"><input aria-label="记忆用户" inputMode="numeric" placeholder="输入 QQ 号" value={user} onChange={event => setUser(event.target.value.replace(/\D/g, ""))} /></Field>
          <Field label="类型"><select aria-label="记忆类型" value={kind} onChange={event => setKind(event.target.value)}>
            <option value="">全部类型</option>{Object.entries(memoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></Field>
        </div>
      </section>
      {user && <section className="card memory-privacy-card" aria-label="用户学习偏好">
        <div className="section-heading"><div><h2><ShieldCheck size={16} />用户偏好</h2><span>QQ {user} · {group ? `仅群 ${group}` : "全局偏好"}</span></div></div>
        {privacy.loading ? <Loading /> : privacy.error ? <ErrorState message={privacy.error} retry={privacy.reload} /> : privacy.data && <div className="memory-privacy-body">
          <div className="memory-privacy-toggles">{([
            ["learning", "记忆学习"], ["style_learning", "风格学习"], ["evaluation", "允许被评价"],
          ] as const).map(([key, label]) => <label className="checkbox-label" key={key}>
            <input type="checkbox" disabled={busy} checked={privacy.data![key]} onChange={event => void changePrivacy(key, event.target.checked)} />{label}
          </label>)}</div>
          <div className="actions">
            {group && <button className="button" disabled={busy} onClick={() => void showReport()}>查看评价依据</button>}
            <button className="button danger-button" disabled={busy} onClick={() => {
              if (window.confirm("删除该范围的记忆、画像、向量与归档，并停止学习？")) void changePrivacy("forget", true);
            }}><Trash2 size={15} />忘记该用户</button>
          </div>
        </div>}
      </section>}
      {report !== null && <section className="card memory-report-card"><div className="section-heading"><h2>评价依据</h2><button className="button" onClick={() => setReport(null)}>收起</button></div><pre className="developer-output">{JSON.stringify(report, null, 2)}</pre></section>}
      {state.loading ? <Loading /> : state.error ? <ErrorState message={state.error} retry={state.reload} /> : !state.data?.items.length ?
        <div className="card"><Empty title="没有符合条件的记忆" detail="可调整群号、QQ 或类型筛选；新消息整理后会出现在这里。" /></div> :
        state.data.items.map(record => <MemoryEditor key={`${record.id}:${record.version}`} record={record} reload={state.reload} />)}
    </div>
  </>;
}

function MemoryEditor({ record, reload }: { record: MemoryRecord; reload: () => void }) {
  const [content, setContent] = useState(record.content);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(remove = false) {
    if (remove && !window.confirm("删除后将立即停止召回，后续整理也不会恢复这条记忆。继续？")) return;
    setBusy(true); setError("");
    try {
      await write(`/agent/memory/${record.id}`, { version: record.version, ...(remove ? { delete: true } : { content }) }, "PATCH");
      reload();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  return <section className="card memory-card">
    <div className="memory-card-heading">
      <div><h2><Brain size={16} />记忆 #{record.id}</h2><p className="memory-meta">{record.group_id ? `群 ${record.group_id}` : "本人公开的稳定信息"} · QQ {record.user_id}</p></div>
      <div className="memory-badges"><Badge>{memoryLabels[record.kind] || record.kind}</Badge>{record.protected && <Badge kind="success">人工保护</Badge>}</div>
    </div>
    <p className="muted memory-meta">来源 {record.source} · 版本 {record.version}</p>
    <Notice>{error}</Notice>
    <Field label="记忆内容"><textarea aria-label={`记忆${record.id}`} value={content} rows={3} maxLength={1500} onChange={event => setContent(event.target.value)} /></Field>
    <details className="details-block"><summary>消息证据 <span className="muted">({record.evidence?.length || 0})</span></summary><pre className="developer-output">{JSON.stringify(record.evidence || [], null, 2)}</pre></details>
    <div className="actions memory-card-actions"><button className="button primary" disabled={busy || !content.trim()} onClick={() => void save()}><Save size={15} />保存修订</button><button className="button danger-button" disabled={busy} onClick={() => void save(true)}><Trash2 size={15} />删除</button><span className="muted">{content.length}/1500 字符</span></div>
  </section>;
}

type JobStatus = {
  jobs?: Array<{ group_id: number; checkpoint: number; status: string; pending: number; attempts: number; last_error: string; next_run: string }>;
  archive_bytes?: number; archive_limit_bytes?: number; archive_warning?: boolean;
  embedding_tokens_reserved?: Record<string, number>; database_error?: string;
  spool?: { pending: number | null; bytes?: number; last_error?: string; warning?: boolean };
};
const jobLabels: Record<string, string> = { pending: "等待", running: "整理中", retry: "待重试" };
const mib = (bytes: number) => (bytes / 1024 / 1024).toFixed(2);

export function MemoryJobs() {
  const state = useApi<JobStatus>("/agent/jobs");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const data = state.data;
  async function retry() {
    setBusy(true); setError("");
    try { await write("/agent/jobs/retry"); state.reload(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  const archiveLimit = data?.archive_limit_bytes || 5 * 1024 ** 3;
  const archivePercentage = Math.min(100, Math.max(0, (data?.archive_bytes || 0) / archiveLimit * 100));
  return <>
    <PageHeading title="整理任务" subtitle="观察消息积压、检查点、重试、归档容量和向量预算。">
      <button className="button" onClick={state.reload}><RefreshCw size={15} />刷新</button>
      <button className="button primary" disabled={busy} onClick={() => void retry()}><RefreshCw size={15} />{busy ? "提交中…" : "重试待整理任务"}</button>
    </PageHeading>
    <Notice>{error}</Notice>
    {state.loading ? <Loading /> : state.error ? <ErrorState message={state.error} retry={state.reload} /> : data && <div className="agent-page-stack">
      {data.database_error && <Notice>数据库暂不可用：{data.database_error}。以下本地暂存状态仍可查看。</Notice>}
      <div className="memory-job-stats">
        <section className="card memory-job-stat">
          <div className="metric-top"><h2>本地待入库消息</h2><Database size={18} /></div>
          <strong className="memory-stat-value">{data.spool ? data.spool.pending === null ? "无法读取" : data.spool.pending.toLocaleString() : "—"}<small>{data.spool?.pending != null ? "条" : ""}</small></strong>
          <p className="muted">{data.spool ? `${mib(data.spool.bytes || 0)} MiB / 128 MiB` : "暂无本地暂存状态"}</p>
          {data.spool?.warning && <Notice>暂存接近容量上限或读取异常，请恢复数据库连接并检查磁盘。</Notice>}
          {data.spool?.last_error && <p className="muted break-word">最近错误：{data.spool.last_error}</p>}
        </section>
        <section className="card memory-job-stat">
          <div className="metric-top"><h2>归档容量</h2><Database size={18} /></div>
          <strong className="memory-stat-value">{mib(data.archive_bytes || 0)}<small>MiB</small></strong>
          <div className="progress-track" role="progressbar" aria-label="归档容量使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(archivePercentage)}><i style={{ width: `${archivePercentage}%` }} /></div>
          <p className="muted">总容量 {(archiveLimit / 1024 ** 3).toFixed(2)} GiB · 已使用 {archivePercentage.toFixed(1)}%</p>
          {data.archive_warning && <Notice>归档已超过 80%，请检查增长速度与存储容量。</Notice>}
        </section>
        <section className="card memory-job-stat">
          <div className="metric-top"><h2>向量预算预留</h2><Sparkles size={18} /></div>
          <div className="memory-token-budget">{Object.entries(data.embedding_tokens_reserved || {}).map(([key, value]) => <div key={key}><span className="muted">{key.includes("bootstrap") ? "首次历史索引" : "当日向量请求"}</span><strong>{value.toLocaleString()} <small>token</small></strong></div>)}</div>
          {!Object.keys(data.embedding_tokens_reserved || {}).length && <p className="muted">暂无向量请求记录</p>}
          <p className="muted">按文本字节数保守预留，包含失败请求；实际账单以供应商为准。</p>
        </section>
      </div>
      <section className="card">
        <div className="section-heading"><div><h2>群整理队列</h2><span>按群查看任务执行状态和最近错误</span></div><Badge>{data.jobs?.length || 0} 个群</Badge></div>
        {!data.jobs?.length ? <Empty title="暂无整理任务" detail="启用测试群并开始聊天后，消息会进入持久队列。" /> : <div className="table-wrap agent-job-table memory-job-table" tabIndex={0} aria-label="群整理队列">
          <table><thead><tr><th>群号</th><th>状态</th><th>待处理消息</th><th>检查点</th><th>累计尝试</th><th>下次整理</th><th>最近错误</th></tr></thead>
            <tbody>{data.jobs.map(job => <tr key={job.group_id}><td className="nowrap">{job.group_id}</td><td><Badge kind={job.status === "retry" ? "warning" : job.status === "running" ? "rose" : "neutral"}>{jobLabels[job.status] || job.status}</Badge></td><td>{job.pending}</td><td>{job.checkpoint}</td><td>{job.attempts}</td><td className="nowrap">{dateTime(job.next_run)}</td><td className="wrap-cell">{job.last_error || "—"}</td></tr>)}</tbody>
          </table>
        </div>}
      </section>
      <details className="card details-block memory-diagnostics"><summary>诊断数据</summary><pre className="developer-output">{JSON.stringify(data, null, 2)}</pre></details>
    </div>}
  </>;
}