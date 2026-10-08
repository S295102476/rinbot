import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  Activity,
  Bot,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Database,
  Fingerprint,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Menu,
  MessageSquare,
  Moon,
  Settings2,
  ShieldCheck,
  Smile,
  Sun,
  Users,
  X,
} from "lucide-react";
import { api, errorMessage, setCsrfToken, write } from "./api";
import { IconButton, Loading, Notice } from "./ui";
import { Dashboard, Groups, Settings } from "./pages/Operations";
import { Memes } from "./pages/Memes";
import { Personas } from "./pages/Personas";
import { Memory, Records } from "./pages/MemoryRecords";

type Session = { username: string; csrf_token: string };
const navigation = [
  {
    id: "overview",
    label: "运行概览",
    icon: LayoutDashboard,
    section: "工作台",
  },
  { id: "groups", label: "群聊管理", icon: Users },
  { id: "settings", label: "Agent 设置", icon: Settings2 },
  { id: "memes", label: "表情库", icon: Smile, section: "内容与记忆" },
  { id: "personas", label: "人格管理", icon: Fingerprint },
  { id: "memory", label: "记忆与关系", icon: Database },
  { id: "requests", label: "请求记录", icon: Activity, section: "观察与审计" },
  { id: "audits", label: "操作审计", icon: ClipboardList },
];

function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await write("/auth/login", { username, password });
      const session = await api<Session>("/auth/me");
      setPassword("");
      onLogin(session);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <div className="login-brand">
        <Bot size={28} />
        <span>管理控制台</span>
      </div>
      <form className="login-form" onSubmit={submit}>
        <div className="login-icon">
          <LockKeyhole size={24} />
        </div>
        <h1>登录管理控制台</h1>
        <p className="muted">管理员身份验证</p>
        <Notice>{error}</Notice>
        <label className="field">
          <span className="field-label">用户名</span>
          <input
            autoComplete="username"
            placeholder="admin"
            autoFocus
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">密码</span>
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button className="button primary login-submit" disabled={busy}>
          {busy ? "正在验证…" : "登录"}
          <ChevronRight size={16} />
        </button>
      </form>
      <div className="login-footer">
        <ShieldCheck size={14} /> 管理员安全会话
      </div>
    </main>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [page, setPage] = useState(() => location.hash.slice(1) || "overview");
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [theme, setTheme] = useState(
    () =>
      localStorage.getItem("qq-admin-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
  const [logoutBusy, setLogoutBusy] = useState(false);
  const onLogin = useCallback((value: Session) => {
    setCsrfToken(value.csrf_token);
    setSession(value);
    setSessionError("");
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("qq-admin-theme", theme);
  }, [theme]);
  useEffect(() => {
    let active = true;
    api<Session>("/auth/me")
      .then((value) => {
        if (active) onLogin(value);
      })
      .catch(() => {})
      .finally(() => {
        if (active) setChecking(false);
      });
    const expired = () => {
      setCsrfToken("");
      setSession(null);
      setSessionError("会话已过期，请重新登录。");
    };
    const hashChange = () => {
      setPage(location.hash.slice(1) || "overview");
      setDrawer(false);
    };
    window.addEventListener("session-expired", expired);
    window.addEventListener("hashchange", hashChange);
    return () => {
      active = false;
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("hashchange", hashChange);
    };
  }, [onLogin]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawer(false);
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, []);
  async function logout() {
    setLogoutBusy(true);
    try {
      await write("/auth/logout");
      setCsrfToken("");
      setSession(null);
    } catch (cause) {
      setSessionError(errorMessage(cause));
    } finally {
      setLogoutBusy(false);
    }
  }
  if (checking)
    return (
      <main className="auth-loading">
        <Loading />
      </main>
    );
  if (!session)
    return (
      <>
        <Login onLogin={onLogin} />
        {sessionError && (
          <div className="session-notice">
            <Notice>{sessionError}</Notice>
          </div>
        )}
      </>
    );
  const current = navigation.find((item) => item.id === page) || navigation[0];
  return (
    <div className={`app ${collapsed ? "sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>
      {drawer && (
        <button
          aria-label="关闭导航"
          className="drawer-backdrop"
          onClick={() => setDrawer(false)}
        />
      )}
      <aside className={`sidebar ${drawer ? "open" : ""}`} aria-label="主导航">
        <a className="brand" href="#overview">
          <span className="brand-icon">
            <Bot size={24} />
          </span>
          <span className="brand-copy">
            <strong>管理控制台</strong>
            <small>管理员工作区</small>
          </span>
        </a>
        <IconButton
          label="关闭导航"
          onClick={() => setDrawer(false)}
          className="icon-button mobile-close"
        >
          <X size={19} />
        </IconButton>
        <nav>
          {navigation.map((item) => (
            <div key={item.id}>
              {item.section && (
                <div className="nav-section">{item.section}</div>
              )}
              <a
                className={`nav-item ${current.id === item.id ? "active" : ""}`}
                href={`#${item.id}`}
                aria-current={current.id === item.id ? "page" : undefined}
                title={collapsed ? item.label : undefined}
                onClick={() => setDrawer(false)}
              >
                <item.icon size={19} />
                <span>{item.label}</span>
                {current.id === item.id && <i />}
              </a>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace">
            <span className="workspace-icon">
              <MessageSquare size={17} />
            </span>
            <span>
              <strong>管理控制台</strong>
              <small>管理工作区</small>
            </span>
          </div>
          <button
            className="collapse-button"
            onClick={() => setCollapsed((value) => !value)}
            title={collapsed ? "展开侧栏" : "收起侧栏"}
          >
            {collapsed ? (
              <ChevronRight size={17} />
            ) : (
              <>
                <ChevronLeft size={17} />
                <span>收起侧栏</span>
              </>
            )}
          </button>
        </div>
      </aside>
      <div className="main-layout">
        <header className="topbar">
          <div className="breadcrumb">
            <IconButton
              label="打开导航"
              className="icon-button mobile-menu"
              onClick={() => setDrawer(true)}
            >
              <Menu size={20} />
            </IconButton>
            <span>控制台</span>
            <ChevronRight size={13} />
            <strong>{current.label}</strong>
          </div>
          <div className="topbar-actions">
            <IconButton
              label={theme === "dark" ? "切换浅色模式" : "切换深色模式"}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
            </IconButton>
            <span className="topbar-divider" />
            <span className="admin-avatar">
              {session.username.charAt(0).toUpperCase()}
            </span>
            <span className="admin-name">{session.username}</span>
            <IconButton label="退出登录" disabled={logoutBusy} onClick={logout}>
              <LogOut size={17} />
            </IconButton>
          </div>
        </header>
        <main id="main" className="main-content" tabIndex={-1}>
          {sessionError && <Notice>{sessionError}</Notice>}
          {current.id === "overview" && <Dashboard />}
          {current.id === "groups" && <Groups />}
          {current.id === "settings" && <Settings />}
          {current.id === "memes" && <Memes />}
          {current.id === "personas" && <Personas />}
          {current.id === "memory" && <Memory />}
          {current.id === "requests" && <Records kind="requests" />}
          {current.id === "audits" && <Records kind="audits" />}
        </main>
        <footer className="app-footer">
          <span>管理控制台</span>
          <span>
            <ShieldCheck size={13} /> 已验证的管理员会话
          </span>
        </footer>
      </div>
    </div>
  );
}
