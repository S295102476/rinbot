import { useEffect, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import { RefreshCw, Play, Check, X, RotateCcw, Download, ClipboardList, Clock3, GitCompareArrows } from "lucide-react";
import { api, dateTime, errorMessage, useApi, write } from "../api";
import { Badge, Empty, ErrorState, Field, Loading, Notice } from "../ui";
import { PageHeading } from "./Operations";

type CheckResult = { check: string; ok: boolean; output: string };
type Task = {
  id: string; actor: string; status: string; version: number; request: string;
  created_at: number; updated_at: number; expires_at?: number; iterations: number;
  elapsed_seconds: number; summary: string; diff: string; error: string;
  checks: CheckResult[]; calls: { iteration: number; seconds: number; status: string }[];
};
const labels: Record<string, string> = {
  queued: "等待执行", running: "分析中", checking: "检查中", checks_failed: "检查未通过",
  pending_approval: "等待审批", applied: "已应用", rejected: "已拒绝", cancelled: "已取消",
  failed: "失败", completed: "分析完成", expired: "审批过期", rolled_back: "已回滚",
  applying: "应用中", rolling_back: "回滚中",
};
const activeStatuses = ["queued", "running", "checking", "applying", "rolling_back"];
function TaskBadge({ status }: { status: string }) {
  return <Badge kind={status === "applied" ? "success" : ["failed", "checks_failed"].includes(status) ? "danger" : status === "pending_approval" ? "warning" : activeStatuses.includes(status) ? "rose" : "neutral"}>{labels[status] || status}</Badge>;
}

export function DeveloperTasks() {
  const list = useApi<{ items: Task[]; total: number }>("/developer/tasks");
  const [selected, setSelected] = useState("");
  const detail = useApi<Task>(selected ? `/developer/tasks/${selected}` : null);
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const task = detail.data;
  const active = task && activeStatuses.includes(task.status);
  useEffect(() => { setReviewed(false); }, [selected, task?.version]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => { detail.reload(); list.reload(); }, 4000);
    return () => window.clearInterval(timer);
  }, [active, detail.reload, list.reload]);

  async function create(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const item = await write<Task>("/developer/tasks", { request });
      setSelected(item.id); setRequest(""); list.reload();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function decide(action: string) {
    if (!task) return;
    if (action === "approve" && !window.confirm("将已检查的差异应用到源文件，并保留回滚副本。确认应用？")) return;
    if (action === "rollback" && !window.confirm("将此任务修改的源文件恢复到应用前。确认回滚？")) return;
    setBusy(true); setError("");
    try {
      await write(`/developer/tasks/${task.id}/${action}`, { version: task.version });
      detail.reload(); list.reload();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function downloadDiff() {
    if (!task) return;
    try {
      const latest = await api<Task>(`/developer/tasks/${task.id}`);
      const url = URL.createObjectURL(new Blob([latest.diff], { type: "text/plain;charset=utf-8" }));
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `rinbot-${task.id}.patch`; anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) { setError(errorMessage(cause)); }
  }

  return <>
    <PageHeading title="开发任务" subtitle="在隔离的源码副本中分析和检查，审核完整差异后应用。">
      <button className="button" onClick={() => { list.reload(); detail.reload(); }}><RefreshCw size={15} />刷新</button>
    </PageHeading>
    <Notice kind="success">检查在受限容器内执行。应用补丁会保留回滚副本；服务重启与发布由管理员单独操作。</Notice>
    <div className="agent-page-stack">
      <form className="card developer-create" onSubmit={create}>
        <Field label="开发需求" hint="写清要修改的文件、预期行为和验收方式。任务创建后可在下方查看进度。">
          <textarea id="developer-request" aria-label="开发需求" value={request} onChange={event => setRequest(event.target.value)} maxLength={8000} rows={4} required placeholder="例如：修复群聊管理的筛选问题，并运行相关检查" />
        </Field>
        <div className="actions"><span className="muted">{request.length}/8000 字符</span><button type="submit" className="button primary" disabled={busy || !request.trim()}><Play size={15} />{busy ? "提交中…" : "创建任务"}</button></div>
      </form>
      <Notice>{error}</Notice>
      <div className="developer-layout developer-task-layout">
        <section className="card developer-list" aria-label="开发任务列表">
          <div className="section-heading"><h2><ClipboardList size={16} />任务列表</h2><Badge>{list.data?.total ?? 0}</Badge></div>
          {list.loading ? <Loading /> : list.error ? <ErrorState message={list.error} retry={list.reload} /> : !list.data?.items.length ? <Empty title="暂无开发任务" detail="提交需求后，任务会出现在这里。" /> :
            <div className="developer-list-items">{list.data.items.map(item => <button type="button" className={`developer-list-item ${selected === item.id ? "selected" : ""}`} aria-pressed={selected === item.id} key={item.id} onClick={() => { setSelected(item.id); setError(""); }}>
              <span className="developer-item-title">{item.request}</span><span className="developer-item-meta"><TaskBadge status={item.status} /><small>{dateTime(item.created_at)}</small></span>
            </button>)}</div>}
        </section>
        <section className="card developer-detail" aria-label="开发任务详情">
          {detail.loading ? <Loading /> : detail.error ? <ErrorState message={detail.error} retry={detail.reload} /> : !task ? <Empty title="选择任务查看详情" detail="查看模型说明、检查结果和待审核补丁。" /> : <>
            <div className="section-heading"><h2><GitCompareArrows size={16} />任务详情</h2><TaskBadge status={task.status} /></div>
            <div className="developer-detail-body">
              <div className="developer-meta-grid">
                <div><span>发起人</span><strong>{task.actor}</strong></div>
                <div><span>模型轮次</span><strong>{task.iterations} 轮</strong></div>
                <div><span>运行耗时</span><strong><Clock3 size={13} />{Math.round(task.elapsed_seconds)} 秒</strong></div>
                <div><span>更新时间</span><strong>{dateTime(task.updated_at)}</strong></div>
              </div>
              <p className="muted developer-task-id">任务 {task.id} · 版本 {task.version}</p>
              {task.expires_at && <p className="muted">审批有效期至 {dateTime(task.expires_at)}</p>}
              {task.error && <Notice>{task.error}</Notice>}
              <section className="developer-section"><h3>原始需求</h3><p className="developer-request-text">{task.request}</p></section>
              {task.summary && <section className="developer-section"><h3>模型说明</h3><div className="developer-summary"><ReactMarkdown skipHtml components={{ img: () => null }}>{task.summary}</ReactMarkdown></div></section>}
              {task.status === "completed" && !task.diff && <div className="developer-status-note" role="status">分析已结束，但没有可应用的补丁。模型说明里的代码或差异文本不会自动成为源文件修改。</div>}
              <details className="details-block"><summary>模型调用记录 <span className="muted">({task.calls?.length || 0})</span></summary>
                {task.calls?.length ? <div className="table-wrap" tabIndex={0}><table><thead><tr><th>轮次</th><th>耗时</th><th>状态</th></tr></thead><tbody>{task.calls.map((call, index) => <tr key={`${call.iteration}-${index}`}><td>第 {call.iteration} 轮</td><td>{call.seconds} 秒</td><td className="wrap-cell">{call.status}</td></tr>)}</tbody></table></div> : <p className="muted">暂无模型调用记录。</p>}
              </details>
              <section className="developer-section"><h3>检查结果</h3>
                {!task.checks?.length ? <p className="muted">尚未运行检查。</p> : <div className="developer-checks">{task.checks.map((check, index) => <details className="details-block" key={`${check.check}-${index}`} open={!check.ok}>
                  <summary><Badge kind={check.ok ? "success" : "danger"}>{check.ok ? "通过" : "失败"}</Badge><span>{check.check}</span></summary><pre className="developer-output" tabIndex={0}>{check.output || "无输出"}</pre>
                </details>)}</div>}
              </section>
              <section className="developer-section"><div className="developer-section-heading"><h3>完整差异</h3>{task.diff && <button className="button" onClick={() => void downloadDiff()}><Download size={15} />下载补丁</button>}</div>
                {task.diff ? <pre className="developer-diff" tabIndex={0}>{task.diff}</pre> : <p className="muted">此任务尚未提出源文件修改。</p>}
              </section>
              {task.status === "pending_approval" && <div className="developer-approval">
                <label className="checkbox-label"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />我已检查完整差异与检查结果</label>
                <div className="actions"><button className="button primary" disabled={busy || !reviewed || !task.diff || !task.checks?.length || task.checks.some(check => !check.ok)} onClick={() => void decide("approve")}><Check size={15} />批准应用</button>
                  <button className="button danger-button" disabled={busy} onClick={() => void decide("reject")}><X size={15} />拒绝</button></div>
              </div>}
              {["queued", "running", "checking", "checks_failed"].includes(task.status) && <div className="actions"><button className="button" disabled={busy} onClick={() => void decide("cancel")}><X size={15} />取消任务</button></div>}
              {task.status === "applied" && <div className="actions"><button className="button" disabled={busy} onClick={() => void decide("rollback")}><RotateCcw size={15} />回滚源文件</button></div>}
            </div>
          </>}
        </section>
      </div>
    </div>
  </>;
}