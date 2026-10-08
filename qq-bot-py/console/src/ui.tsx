import { useEffect, useId, useRef, type ReactNode } from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Inbox,
  LoaderCircle,
  RefreshCw,
  X,
} from "lucide-react";
import { dateTime } from "./api";

export function useUnsavedChanges(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const unload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const navigate = (event: MouseEvent) => {
      const anchor = (event.target as Element).closest('a[href^="#"]');
      if (anchor && anchor.getAttribute('href') !== location.hash && !window.confirm('有未保存的更改，确定离开？')) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener('beforeunload', unload);
    document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, [dirty]);
}

export function IconButton({
  label,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  );
}

export function Loading() {
  return (
    <div className="state" role="status">
      <LoaderCircle className="spin" size={24} />
      <span>正在加载</span>
    </div>
  );
}

export function Empty({
  title = "暂无数据",
  detail,
}: {
  title?: string;
  detail?: string;
}) {
  return (
    <div className="state">
      <Inbox size={30} />
      <strong>{title}</strong>
      {detail && <span>{detail}</span>}
    </div>
  );
}

export function ErrorState({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <div className="state error" role="alert">
      <AlertCircle size={26} />
      <strong>无法加载数据</strong>
      <span>{message}</span>
      {retry && (
        <button className="button" onClick={retry}>
          <RefreshCw size={15} />
          重试
        </button>
      )}
    </div>
  );
}

export function Notice({
  children,
  kind = "error",
}: {
  children: ReactNode;
  kind?: "error" | "success";
}) {
  if (!children) return null;
  return (
    <div
      role={kind === "error" ? "alert" : "status"}
      className={`notice ${kind}`}
    >
      {children}
    </div>
  );
}

export function Badge({
  children,
  kind = "neutral",
}: {
  children: ReactNode;
  kind?: "neutral" | "success" | "warning" | "danger" | "rose";
}) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function Avatar({
  id,
  group = false,
  size = 36,
}: {
  id: string | number;
  group?: boolean;
  size?: number;
}) {
  const source = group
    ? `https://p.qlogo.cn/gh/${encodeURIComponent(id)}/${encodeURIComponent(id)}/100`
    : `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(id)}&s=100`;
  return (
    <span className="avatar" style={{ width: size, height: size }}>
      <img
        alt=""
        src={source}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={(event) => {
          event.currentTarget.style.display = "none";
        }}
      />
      <span>{String(id).slice(-2)}</span>
    </span>
  );
}

export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = () =>
      Array.from(
        dialog.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]',
        ) || [],
      );
    focusable()[0]?.focus();
    function keydown(event: KeyboardEvent) {
      const openDialogs = document.querySelectorAll('[role="dialog"]');
      if (openDialogs[openDialogs.length - 1] !== dialog.current) return;
      if (event.key === "Escape") close.current();
      if (event.key === "Tab") {
        const items = focusable();
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`modal ${wide ? "wide" : ""}`}
      >
        <div className="modal-heading">
          <h2 id={titleId}>{title}</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X size={20} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Confirm({
  title,
  children,
  onClose,
  onConfirm,
  busy,
  danger = true,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
  danger?: boolean;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <div className="modal-body">{children}</div>
      <div className="modal-footer">
        <button className="button" onClick={onClose} disabled={busy}>
          取消
        </button>
        <button
          className={`button ${danger ? "danger-button" : "primary"}`}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy && <LoaderCircle size={15} className="spin" />}确认
        </button>
      </div>
    </Modal>
  );
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function Pagination({
  offset,
  total,
  limit,
  setOffset,
}: {
  offset: number;
  total: number;
  limit: number;
  setOffset: (value: number) => void;
}) {
  return (
    <div className="pagination">
      <span>
        {total
          ? `${offset + 1}–${Math.min(offset + limit, total)} / ${total}`
          : "0 条记录"}
      </span>
      <IconButton
        label="上一页"
        disabled={!offset}
        onClick={() => setOffset(Math.max(0, offset - limit))}
      >
        <ChevronLeft size={17} />
      </IconButton>
      <IconButton
        label="下一页"
        disabled={offset + limit >= total}
        onClick={() => setOffset(offset + limit)}
      >
        <ChevronRight size={17} />
      </IconButton>
    </div>
  );
}

export function DateRange({
  from,
  to,
  onChange,
}: {
  from: string;
  to: string;
  onChange: (from: string, to: string) => void;
}) {
  return (
    <div className="date-range">
      <select aria-label="日期范围" value="" onChange={(event) => {
        const preset = event.target.value;
        if (preset) {
          const dates = presetDates(preset);
          onChange(dates[0], dates[1]);
        }
      }}>
        <option value="">选择范围</option><option value="today">今天</option><option value="week">本周</option><option value="last-week">上周</option>
      </select>
      <input
        aria-label="开始日期"
        type="date"
        value={from}
        max={to}
        onChange={(event) => onChange(event.target.value, to)}
      />
      <span>至</span>
      <input
        aria-label="结束日期"
        type="date"
        value={to}
        min={from}
        onChange={(event) => onChange(from, event.target.value)}
      />
    </div>
  );
}

export function defaultDates() {
  return presetDates("week");
}

function presetDates(preset: string): [string, string] {
  const start = new Date(Date.now() + 8 * 3600 * 1000);
  if (preset !== "today") start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7 - (preset === "last-week" ? 7 : 0));
  const end = new Date(start);
  if (preset !== "today") end.setUTCDate(end.getUTCDate() + 6);
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}

export function JsonDetail({ value }: { value: unknown }) {
  return (
    <pre className="json-detail">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Timestamp({ value }: { value: unknown }) {
  return <span className="muted nowrap">{dateTime(value)}</span>;
}
