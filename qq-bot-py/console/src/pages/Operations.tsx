import { useEffect, useState, type FormEvent } from "react";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  Edit3,
  MessageCircle,
  Plus,
  RefreshCw,
  Save,
  Search,
  SlidersHorizontal,
  Users,
} from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
  Loading,
  Modal,
  Notice,
  Pagination,
  useUnsavedChanges,
} from "../ui";

const modes: Record<string, string> = {
  auto: "自动回复",
  at: "仅 @ 回复",
  off: "已关闭",
};
const stages: Record<string, string> = {
  normal: "正常",
  at_only: "仅 @ 回复",
  hard_limit: "已达硬上限",
  unlimited: "不限额",
};

export function PageHeading({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      <div className="heading-actions">{children}</div>
    </div>
  );
}

function Quota({ quota }: { quota?: Row }) {
  if (!quota) return <span className="muted">不可用</span>;
  const percentage = quota.limit
    ? Math.min(100, (quota.count / quota.limit) * 100)
    : 0;
  return (
    <div className="quota">
      <div>
        <span>
          {number(quota.count)}{" "}
          <span className="muted">
            / {quota.limit === 0 ? "∞" : number(quota.limit)}
          </span>
        </span>
        <span
          className={quota.stage === "hard_limit" ? "danger-text" : "muted"}
        >
          {quota.limit
            ? `${Math.round((quota.count / quota.limit) * 100)}%`
            : "不限额"}
        </span>
      </div>
      <div className="progress-track">
        <i
          className={
            percentage >= 100 ? "high" : percentage >= 75 ? "medium" : ""
          }
          style={{ width: `${percentage}%` }}
        />
      </div>
      {quota.uncertain > 0 && <small className="danger-text">另有 {quota.uncertain} 轮结果不明，已保留额度</small>}
    </div>
  );
}

export function Dashboard() {
  const [[from, to], setDates] = useState(defaultDates);
  const { data, loading, error, reload } = useApi<Row>(
    `/overview${query({ date_from: from, date_to: to })}`,
  );
  const stats = data;
  const totals = stats?.totals || {};
  const groups: Row[] = data?.quotas || [];
  const metrics = [
    {
      label: "收到消息",
      value: totals.incoming,
      icon: ArrowDownLeft,
      color: "teal",
      detail: "群聊入站消息",
    },
    {
      label: "交互消息",
      value: totals.interaction,
      icon: MessageCircle,
      color: "rose",
      detail: "有效交互事件",
    },
    {
      label: "Agent 回复",
      value: totals.reply_round,
      icon: ArrowUpRight,
      color: "blue",
      detail: "成功回复轮次",
    },
    {
      label: "模型请求",
      value: totals.model_request,
      icon: Activity,
      color: "teal",
      detail: `超时率 ${stats?.request_metrics?.timeout_rate == null ? "未知" : (stats.request_metrics.timeout_rate * 100).toFixed(1) + "%"}`,
    },
    {
      label: "活跃用户",
      value: stats?.active_users,
      icon: Users,
      color: "amber",
      detail: "去重用户数",
    },
  ];
  return (
    <>
      <PageHeading title="运行概览" subtitle="Agent 运行与群聊活动">
        <DateRange from={from} to={to} onChange={(a, b) => setDates([a, b])} />
        <IconButton label="刷新概览" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
      </PageHeading>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        data && (
          <>
            {data.coverage_incomplete && <Notice>统计起始：{data.statistics_since || "未知"}，起始时间之前的历史不可还原</Notice>}
            {(data.health?.error || data.health?.dropped > 0 || data.health?.gap_detected || data.health?.error_recorded) && <Notice>统计采集存在异常或历史缺口：{data.health?.error || data.health?.error_recorded || "采集曾中断"}，数据可能不完整</Notice>}
            <div className="status-strip">
              <div>
                <span
                  className={`status-dot ${data.status?.agent_enabled ? "online" : ""}`}
                />
                <strong>
                  Agent {data.status?.agent_enabled ? "已启用" : "已停用"}
                </strong>
              </div>
              <span className="strip-divider" />
              <span>QQ 连接 <strong>{data.status?.bot_online == null ? "未知" : data.status.bot_online ? "在线" : "离线"}</strong></span>
              <span>启用群 <strong>{number(data.status?.active_groups)}</strong></span>
              <span>
                当前人格 <strong>{data.status?.active_persona || "—"}</strong>
              </span>
              <span>
                模型 <strong>{data.status?.model || "—"}</strong>
              </span>
              <span className="status-last">
                <Clock3 size={14} /> Asia/Shanghai
              </span>
            </div>
            <div className="metrics">
              {metrics.map((metric) => (
                <div className="metric" key={metric.label}>
                  <div className="metric-top">
                    <span>{metric.label}</span>
                    <span className={`metric-icon ${metric.color}`}>
                      <metric.icon size={19} />
                    </span>
                  </div>
                  <strong>{number(metric.value)}</strong>
                  <small>{metric.detail}</small>
                </div>
              ))}
            </div>
            <section className="data-section">
              <div className="section-heading">
                <div>
                  <h2>消息与回复趋势</h2>
                  <span>
                    {from} 至 {to}
                  </span>
                </div>
                <Badge>按日聚合</Badge>
              </div>
              {stats?.daily?.length ? (
                <div className="chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                      data={stats.daily}
                      margin={{ top: 18, right: 20, bottom: 0, left: -18 }}
                    >
                      <CartesianGrid
                        strokeDasharray="3 4"
                        vertical={false}
                        stroke="var(--line)"
                      />
                      <XAxis
                        dataKey="day"
                        tickFormatter={(value) => String(value).slice(5)}
                        tickLine={false}
                        axisLine={false}
                        fontSize={12}
                        stroke="var(--muted)"
                      />
                      <YAxis
                        tickLine={false}
                        axisLine={false}
                        allowDecimals={false}
                        fontSize={12}
                        stroke="var(--muted)"
                      />
                      <Tooltip
                        contentStyle={{
                          background: "var(--surface)",
                          border: "1px solid var(--line)",
                          borderRadius: 6,
                          fontSize: 12,
                        }}
                      />
                      <Legend
                        iconType="circle"
                        iconSize={7}
                        wrapperStyle={{ paddingTop: 16, fontSize: 12 }}
                      />
                      <Line
                        isAnimationActive={false}
                        type="monotone"
                        dataKey="incoming"
                        name="收到消息"
                        stroke="#1b9a8e"
                        strokeWidth={2.5}
                        dot={false}
                        activeDot={{ r: 4 }}
                      />
                      <Line
                        isAnimationActive={false}
                        type="monotone"
                        dataKey="interaction"
                        name="交互消息"
                        stroke="#cf5473"
                        strokeWidth={2}
                        dot={false}
                      />
                      <Line
                        isAnimationActive={false}
                        type="monotone"
                        dataKey="reply_round"
                        name="Agent 回复"
                        stroke="#6d89c9"
                        strokeWidth={2}
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <Empty title="所选时间内暂无活动记录" />
              )}
            </section>
            <section className="data-section">
              <div className="section-heading"><h2>请求耗时趋势</h2><span>平均 {stats?.request_metrics?.average_ms == null ? "未知" : (stats.request_metrics.average_ms / 1000).toFixed(2) + "s"}</span></div>
              {stats?.request_metrics?.daily?.length ? <div className="chart"><ResponsiveContainer width="100%" height="100%">
                <LineChart data={stats.request_metrics.daily} margin={{top: 12, right: 20, bottom: 0, left: 10}}>
                  <CartesianGrid strokeDasharray="3 4" vertical={false} stroke="var(--line)" />
                  <XAxis dataKey="day" fontSize={12} /><YAxis fontSize={12} unit="ms" /><Tooltip />
                  <Line dataKey="latency_ms" name="平均耗时 ms" stroke="#1b9a8e" dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer></div> : <Empty title="暂无请求耗时记录" />}
            </section>
            <section className="data-section">
              <div className="section-heading">
                <div>
                  <h2>群聊配额</h2>
                  <span>今日已发送回复轮次</span>
                </div>
                <a className="text-link" href="#groups">
                  管理群聊
                  <ChevronRight size={15} />
                </a>
              </div>
              {groups.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>群聊</th>
                        <th>回复模式</th>
                        <th>今日用量</th>
                        <th>每小时软上限</th>
                        <th>配额状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groups.map((group) => (
                        <tr key={group.group_id}>
                          <td>
                            <div className="identity">
                              <Avatar id={group.group_id} group />
                              <div>
                                <strong>
                                  {group.name || `群 ${group.group_id}`}
                                </strong>
                                <small>{group.group_id}</small>
                              </div>
                            </div>
                          </td>
                          <td>
                            <Badge
                              kind={
                                group.mode === "auto"
                                  ? "success"
                                  : group.mode === "at"
                                    ? "rose"
                                    : "neutral"
                              }
                            >
                              {modes[group.mode] || group.mode}
                            </Badge>
                          </td>
                          <td>
                            <Quota quota={group.quota} />
                          </td>
                          <td>{number(group.hourly_reply_soft_limit)}</td>
                          <td>
                            <Badge
                              kind={
                                group.quota?.stage === "hard_limit"
                                  ? "danger"
                                  : group.quota?.stage === "at_only"
                                    ? "warning"
                                    : "neutral"
                              }
                            >
                              {group.quota
                                ? stages[group.quota.stage] || group.quota.stage
                                : "不可用"}
                            </Badge>
                            {group.quota?.enforced === false && (
                              <small className="cell-note">观察模式</small>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="暂无受管理群聊" />
              )}
            </section>
          </>
        )
      )}
    </>
  );
}

function GroupEditor({
  groups,
  onClose,
  onSaved,
}: {
  groups: Row[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const batch = groups.length > 1;
  const group = groups[0] || {};
  const [id, setId] = useState(String(group.group_id || ""));
  const [mode, setMode] = useState(batch ? "" : group.mode || "off");
  const [hour, setHour] = useState(
    batch ? "" : String(group.hourly_reply_soft_limit ?? 30),
  );
  const [day, setDay] = useState(
    batch ? "" : String(group.daily_reply_limit ?? 200),
  );
  const [inheritHour, setInheritHour] = useState(!batch && (!group.group_id || !!group.inherited?.includes("hourly_reply_soft_limit")));
  const [inheritDay, setInheritDay] = useState(!batch && (!group.group_id || !!group.inherited?.includes("daily_reply_limit")));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const changes: Row = {};
    if (mode) changes.mode = mode;
    if (inheritHour || hour !== "") changes.hourly_reply_soft_limit = inheritHour ? null : Number(hour);
    if (inheritDay || day !== "") changes.daily_reply_limit = inheritDay ? null : Number(day);
    try {
      const result = batch
        ? await write<Row>(
            "/groups/bulk",
            {
              group_ids: groups.map((item) => item.group_id),
              patch: changes,
              versions: Object.fromEntries(
                groups.map((item) => [String(item.group_id), item.version]),
              ),
            },
            "PATCH",
          )
        : await write<Row>(
            `/groups/${id}`,
            { version: group.version ?? 0, patch: changes },
            "PATCH",
          );
      if (result.failed?.length)
        throw new Error(`${result.failed.length} 个群聊更新失败，请刷新后重试`);
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
      title={
        batch
          ? `批量设置 · ${groups.length} 个群聊`
          : group.group_id
            ? "编辑群聊设置"
            : "添加群聊"
      }
      onClose={onClose}
    >
      <form onSubmit={save}>
        <div className="modal-body form-grid">
          <Notice>{error}</Notice>
          {!batch && (
            <Field label="群号">
              <input
                inputMode="numeric"
                pattern="[1-9][0-9]*"
                required
                disabled={!!group.group_id}
                value={id}
                onChange={(event) => setId(event.target.value)}
              />
            </Field>
          )}
          <Field label="回复模式">
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value)}
            >
              {batch && <option value="">保持不变</option>}
              {Object.entries(modes).map(([key, name]) => (
                <option key={key} value={key}>
                  {name}
                </option>
              ))}
            </select>
          </Field>
          <div><Field label="每小时软上限">
            <input
              type="number"
              min="0"
              max="10000"
              placeholder={batch ? "保持不变" : undefined}
              required={!batch && !inheritHour}
              disabled={inheritHour}
              value={hour}
              onChange={(event) => setHour(event.target.value)}
            />
          </Field>
          <label className="muted"><input type="checkbox" checked={inheritHour} onChange={event => setInheritHour(event.target.checked)} /> 继承全局小时频率</label></div>
          <div><Field label="每日回复上限">
            <input
              type="number"
              min="0"
              max="100000"
              placeholder={batch ? "保持不变" : undefined}
              required={!batch && !inheritDay}
              disabled={inheritDay}
              value={day}
              onChange={(event) => setDay(event.target.value)}
            />
          </Field>
          <label className="muted"><input type="checkbox" checked={inheritDay} onChange={event => setInheritDay(event.target.checked)} /> 继承全局每日额度</label></div>
        </div>
        <div className="modal-footer">
          <button type="button" className="button" onClick={onClose}>
            取消
          </button>
          <button
            className="button primary"
            disabled={busy || !Object.values({ mode, hour, day, inheritHour, inheritDay }).some(Boolean)}
          >
            <Save size={15} />
            {busy ? "保存中…" : "保存设置"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function GroupDetail({ group, onClose }: { group: Row; onClose: () => void }) {
  const [[from, to], setDates] = useState(defaultDates);
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useApi<ListResult>(
    `/stats/users${query({ group_id: group.group_id, date_from: from, date_to: to, offset, limit: 20 })}`,
  );
  const trend = useApi<Row>(`/stats/groups${query({ group_id: group.group_id, date_from: from, date_to: to })}`);
  return (
    <Modal wide title={`群聊活动 · ${group.group_id}`} onClose={onClose}>
      <div className="modal-body">
        <div className="detail-summary">
          <Avatar id={group.group_id} group size={44} />
          <div>
            <strong>{group.name || `群 ${group.group_id}`}</strong>
            <p className="muted">{modes[group.mode]}</p>
          </div>
          <div className="detail-quota">
            <Quota quota={group.quota} />
          </div>
        </div>
        <div className="toolbar">
          <DateRange
            from={from}
            to={to}
            onChange={(a, b) => {
              setDates([a, b]);
              setOffset(0);
            }}
          />
          <IconButton label="刷新用户活动" onClick={reload}>
            <RefreshCw size={16} />
          </IconButton>
        </div>
        {trend.error ? <ErrorState message={trend.error} retry={trend.reload} /> : trend.data?.daily?.length ? <div className="chart"><ResponsiveContainer width="100%" height="100%">
          <LineChart data={trend.data.daily} margin={{top: 12, right: 10, bottom: 0, left: -20}}>
            <CartesianGrid strokeDasharray="3 4" vertical={false} stroke="var(--line)" /><XAxis dataKey="day" fontSize={11} /><YAxis fontSize={11} /><Tooltip /><Legend />
            <Line dataKey="incoming" name="消息数" stroke="#1b9a8e" dot={false} isAnimationActive={false} /><Line dataKey="reply_round" name="回复轮次" stroke="#cf5473" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer></div> : null}
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !data?.items.length ? (
          <Empty title="所选时间内暂无用户活动" />
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>消息数</th>
                    <th>其他明确互动</th>
                    <th>直接 @</th>
                    <th>回复轮次</th>
                    <th>工具调用</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((row) => (
                    <tr key={row.user_id}>
                      <td>
                        <div className="identity">
                          <Avatar id={row.user_id} />
                          <strong>{row.user_id}</strong>
                        </div>
                      </td>
                      <td>{number(row.incoming)}</td>
                      <td>{number(row.interaction)}</td>
                      <td>{number(row.direct_at)}</td>
                      <td>{number(row.reply_round)}</td>
                      <td>{number(row.tool)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              offset={offset}
              total={data.total}
              limit={20}
              setOffset={setOffset}
            />
          </>
        )}
      </div>
    </Modal>
  );
}

export function Groups() {
  const { data, loading, error, reload } = useApi<ListResult>("/groups");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [editing, setEditing] = useState<Row[] | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);
  const rows = (data?.items || []).filter(
    (row) =>
      (!filter || row.mode === filter) &&
      `${row.group_id} ${row.name || ""}`.includes(search),
  );
  const all =
    rows.length > 0 && rows.every((row) => selected.includes(row.group_id));
  return (
    <>
      <PageHeading
        title="群聊管理"
        subtitle={`${data ? data.total : "—"} 个群聊`}
      >
        <IconButton label="刷新群聊" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
        <button className="button primary" onClick={() => setEditing([])}>
          <Plus size={16} />
          添加群聊
        </button>
      </PageHeading>
      <section className="data-section">
        <div className="toolbar">
          <div className="search-input">
            <Search size={16} />
            <input
              aria-label="搜索群聊"
              placeholder="搜索群号或名称"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <select
            aria-label="回复模式筛选"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="">全部模式</option>
            {Object.entries(modes).map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
          {selected.length > 0 && (
            <>
              <span className="selection-count">已选 {selected.length}</span>
              <button
                className="button"
                onClick={() =>
                  setEditing(
                    (data?.items || []).filter((row) =>
                      selected.includes(row.group_id),
                    ),
                  )
                }
              >
                <SlidersHorizontal size={15} />
                批量设置
              </button>
              <button className="text-button" onClick={() => setSelected([])}>
                取消选择
              </button>
            </>
          )}
        </div>
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : !rows.length ? (
          <Empty
            title={search || filter ? "没有匹配的群聊" : "暂无受管理群聊"}
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="check-cell">
                    <input
                      type="checkbox"
                      aria-label="选择全部群聊"
                      checked={all}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? [
                                ...new Set([
                                  ...selected,
                                  ...rows.map((row) => row.group_id),
                                ]),
                              ]
                            : selected.filter(
                                (id) =>
                                  !rows.some((row) => row.group_id === id),
                              ),
                        )
                      }
                    />
                  </th>
                  <th>群聊</th>
                  <th>回复模式</th>
                  <th>今日用量</th>
                  <th>小时软上限</th>
                  <th>配额状态</th>
                  <th className="right">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.group_id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`选择群 ${row.group_id}`}
                        checked={selected.includes(row.group_id)}
                        onChange={(event) =>
                          setSelected(
                            event.target.checked
                              ? [...selected, row.group_id]
                              : selected.filter((id) => id !== row.group_id),
                          )
                        }
                      />
                    </td>
                    <td>
                      <button
                        className="identity identity-button"
                        onClick={() => setDetail(row)}
                      >
                        <Avatar group id={row.group_id} />
                        <span>
                          <strong>{row.name || `群 ${row.group_id}`}</strong>
                          <small>{row.group_id}</small>
                        </span>
                      </button>
                    </td>
                    <td>
                      <Badge
                        kind={
                          row.mode === "auto"
                            ? "success"
                            : row.mode === "at"
                              ? "rose"
                              : "neutral"
                        }
                      >
                        {modes[row.mode]}
                      </Badge>
                      {row.restrictions?.map((restriction: string) => <small className="cell-note" key={restriction}>{restriction}</small>)}
                    </td>
                    <td>
                      <Quota quota={row.quota} />
                    </td>
                    <td>
                      {number(row.hourly_reply_soft_limit)}
                      {row.inherited?.includes("hourly_reply_soft_limit") && (
                        <small className="cell-note">继承全局</small>
                      )}
                    </td>
                    <td>
                      <Badge
                        kind={
                          row.quota?.stage === "hard_limit"
                            ? "danger"
                            : "neutral"
                        }
                      >
                        {stages[row.quota?.stage] || "不可用"}
                      </Badge>
                      {row.quota?.enforced === false && (
                        <small className="cell-note">观察模式</small>
                      )}
                    </td>
                    <td className="right">
                      <IconButton
                        label={`查看群 ${row.group_id} 活动`}
                        onClick={() => setDetail(row)}
                      >
                        <Activity size={16} />
                      </IconButton>
                      <IconButton
                        label={`编辑群 ${row.group_id}`}
                        onClick={() => setEditing([row])}
                      >
                        <Edit3 size={16} />
                      </IconButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {editing && (
        <GroupEditor
          groups={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setSelected([]);
            reload();
          }}
        />
      )}
      {detail && <GroupDetail group={detail} onClose={() => setDetail(null)} />}
    </>
  );
}

const settingSections = [
  {
    name: "运行设置",
    icon: Bot,
    fields: [
      ["agent_enabled", "启用 Agent", "boolean"],
      ["model", "模型", "text"],
      ["development_mode", "运行范围", "mode"],
      ["test_groups", "测试群组", "ids"],
    ],
  },
  {
    name: "决策与上下文",
    icon: SlidersHorizontal,
    fields: [
      ["decision_timeout_seconds", "决策超时（秒）", "number", 5, 600],
      ["decision_concurrency", "并发决策数", "number", 1, 32],
      ["decision_context_chars", "主动决策上下文字符数", "number", 500, 100000],
      [
        "decision_direct_context_chars",
        "直接交互上下文字符数",
        "number",
        500,
        100000,
      ],
      ["decision_recent_messages", "主动决策最近消息数", "number", 1, 500],
      [
        "decision_direct_recent_messages",
        "直接交互最近消息数",
        "number",
        1,
        500,
      ],
      ["decision_images", "图片上限", "number", 0, 5],
      ["decision_avatars", "头像上限", "number", 0, 10],
      ["summary_timeout_seconds", "摘要超时（秒）", "number", 10, 900],
      ["decision_backend_search", "启用后端搜索", "boolean"],
    ],
  },
  {
    name: "全局回复配额",
    icon: Clock3,
    fields: [
      ["hourly_reply_soft_limit", "每小时软上限", "number", 0, 10000],
      ["daily_reply_limit", "每日回复上限", "number", 0, 100000],
      ["quota_enforcement_enabled", "启用配额限制", "boolean"],
      ["quota_enforcement_groups", "执行配额的群组", "ids"],
    ],
  },
];

export function Settings() {
  const { data, loading, error, reload } = useApi<Row>("/settings");
  const [values, setValues] = useState<Row>({});
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (data) setValues(data);
  }, [data]);
  const dirty = data && JSON.stringify(values) !== JSON.stringify(data);
  useUnsavedChanges(!!dirty);
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setSaveError("");
    setSaved(false);
    const normalized = { ...values };
    for (const key of ["test_groups", "quota_enforcement_groups"]) {
      if (typeof normalized[key] === "string")
        normalized[key] = normalized[key]
          .split(/[,，\s]+/)
          .filter(Boolean)
          .map(Number);
    }
    const changes = Object.fromEntries(
      Object.entries(normalized).filter(
        ([key, value]) =>
          key !== "version" &&
          JSON.stringify(value) !== JSON.stringify(data?.[key]),
      ),
    );
    try {
      await write(
        "/settings",
        { version: data?.version, patch: changes },
        "PATCH",
      );
      setSaved(true);
      reload();
    } catch (cause) {
      setSaveError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeading title="Agent 设置" subtitle="全局运行参数">
        <Badge>版本 {data?.version ?? "—"}</Badge>
        <IconButton label="重新加载设置" onClick={reload}>
          <RefreshCw size={17} />
        </IconButton>
      </PageHeading>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        <form onSubmit={save}>
          <Notice>{saveError}</Notice>
          {saved && <Notice kind="success">设置已保存</Notice>}
          {settingSections.map((section) => (
            <section className="settings-section" key={section.name}>
              <div className="settings-title">
                <section.icon size={19} />
                <h2>{section.name}</h2>
              </div>
              <div className="settings-fields">
                {section.fields.map(([rawKey, rawLabel, rawType, min, max]) => {
                  const key = String(rawKey),
                    label = String(rawLabel),
                    type = String(rawType);
                  return (
                    <Field key={key} label={label}>
                      {data?.defaults && <small className="muted">默认 {JSON.stringify(data.defaults[key])} · {Object.prototype.hasOwnProperty.call(data.overrides || {}, key) ? "控制台覆盖" : "继承 YAML"} · {data.loaded ? "已生效" : "未加载"}</small>}
                      {type === "boolean" ? (
                        <span className="switch-field">
                          <input
                            role="switch"
                            type="checkbox"
                            checked={!!values[key]}
                            onChange={(event) => {
                              setValues({
                                ...values,
                                [key]: event.target.checked,
                              });
                              setSaved(false);
                            }}
                          />
                          <span>{values[key] ? "已启用" : "已关闭"}</span>
                        </span>
                      ) : type === "mode" ? (
                        <select
                          value={values[key] || "all"}
                          onChange={(event) =>
                            setValues({ ...values, [key]: event.target.value })
                          }
                        >
                          <option value="all">全部群聊</option>
                          <option value="test">仅测试群</option>
                          <option value="at">仅 @ 交互</option>
                        </select>
                      ) : type === "ids" ? (
                        <input
                          value={
                            Array.isArray(values[key])
                              ? values[key].join(", ")
                              : values[key] || ""
                          }
                          placeholder="123456, 789012"
                          onChange={(event) => {
                            setValues({ ...values, [key]: event.target.value });
                          }}
                        />
                      ) : (
                        <input
                          type={type}
                          required
                          min={min}
                          max={max}
                          value={values[key] ?? ""}
                          onChange={(event) =>
                            setValues({
                              ...values,
                              [key]:
                                type === "number"
                                  ? Number(event.target.value)
                                  : event.target.value,
                            })
                          }
                        />
                      )}
                    </Field>
                  );
                })}
              </div>
            </section>
          ))}
          <div className="save-bar">
            <span className="muted">
              {dirty ? "有未保存的更改" : "所有更改已保存"}
            </span>
            <button
              type="button"
              className="button"
              disabled={!dirty || busy}
              onClick={() => data && setValues(data)}
            >
              放弃更改
            </button>
            <button className="button primary" disabled={!dirty || busy}>
              {busy ? (
                <RefreshCw size={15} className="spin" />
              ) : saved ? (
                <Check size={15} />
              ) : (
                <Save size={15} />
              )}
              {busy ? "保存中…" : "保存设置"}
            </button>
          </div>
        </form>
      )}
    </>
  );
}
