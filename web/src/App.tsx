import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen, Compass, FolderOpen, GitPullRequest, LayoutDashboard, MessageSquareCode, Moon, Package,
  PenTool, Play, Settings as SettingsIcon, Sun, X,
} from "lucide-react";
import { api, ReviewPayload, WorkspaceInfo } from "./api";
import { CodeView, Modal, Spinner, timeAgo, useToast } from "./ui";
import Overview from "./pages/Overview";
import Review from "./pages/Review";
import MapPage from "./pages/Map";
import Chat from "./pages/Chat";
import Design from "./pages/Design";
import Releases from "./pages/Releases";
import Knowledge from "./pages/Knowledge";
import SettingsPage from "./pages/Settings";

export type Page = "overview" | "review" | "map" | "chat" | "design" | "releases" | "knowledge" | "settings";

interface Ctx {
  wid: string | null;
  data: ReviewPayload | null;
  reload: () => Promise<void>;
  runReview: (proof: boolean, base?: string) => Promise<void>;
  openCode: (file: string, line?: number, span?: [number, number]) => void;
  go: (page: Page, params?: Record<string, string>) => void;
  params: URLSearchParams;
}
const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

function parseHash(): { page: Page; params: URLSearchParams } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [p, qs] = h.split("?");
  const pages: Page[] = ["overview", "review", "map", "chat", "design", "releases", "knowledge", "settings"];
  return { page: (pages.includes(p as Page) ? p : "overview") as Page, params: new URLSearchParams(qs || "") };
}

export default function App() {
  const toast = useToast();
  const [route, setRoute] = useState(parseHash);
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try { return (localStorage.getItem("cw-theme") as "dark" | "light") || "dark"; } catch { return "dark"; }
  });
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [wid, setWid] = useState<string | null>(null);
  const [data, setData] = useState<ReviewPayload | null>(null);
  const [code, setCode] = useState<{ file: string; text: string; line?: number; span?: [number, number] } | null>(null);
  const [openRepo, setOpenRepo] = useState(false);
  const [repoPath, setRepoPath] = useState("");
  const poll = useRef<number | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("cw-theme", theme); } catch { /* private mode */ }
  }, [theme]);

  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  useEffect(() => {
    api.status().then((s) => {
      setWorkspaces(s.workspaces);
      let saved: string | null = null;
      try { saved = localStorage.getItem("cw-ws"); } catch { /* ignore */ }
      const pick = s.workspaces.find((w) => w.id === saved) ?? s.workspaces[0];
      if (pick) setWid(pick.id);
      else setOpenRepo(true);
    }).catch((e) => toast(`Can't reach the Codewright server: ${e.message}`, "error"));
  }, [toast]);

  const reload = useCallback(async () => {
    if (!wid) return;
    const d = await api.review(wid);
    setData(d);
    if (d.running && poll.current == null) {
      poll.current = window.setInterval(async () => {
        const n = await api.review(wid);
        setData(n);
        if (!n.running && poll.current != null) {
          clearInterval(poll.current);
          poll.current = null;
          if (n.error) toast(n.error, "error");
          else toast(`Review finished: ${n.result?.findings.length ?? 0} findings`);
        }
      }, 1200);
    }
  }, [wid, toast]);

  useEffect(() => {
    if (!wid) return;
    try { localStorage.setItem("cw-ws", wid); } catch { /* ignore */ }
    setData(null);
    api.review(wid).then((d) => {
      setData(d);
      if (!d.has_result && !d.running) {
        api.runReview(wid, true, "").then(() => reload());
      } else if (d.running) reload();
    });
    return () => { if (poll.current != null) { clearInterval(poll.current); poll.current = null; } };
  }, [wid, reload]);

  const runReview = useCallback(async (proof: boolean, base = "") => {
    if (!wid) return;
    await api.runReview(wid, proof, base);
    await reload();
  }, [wid, reload]);

  const openCode = useCallback((file: string, line?: number, span?: [number, number]) => {
    if (!wid) return;
    api.source(wid, file).then((s) => setCode({ file, text: s.text, line, span }))
      .catch((e) => toast(e.message, "error"));
  }, [wid, toast]);

  const go = useCallback((page: Page, params?: Record<string, string>) => {
    const qs = params ? `?${new URLSearchParams(params)}` : "";
    window.location.hash = `/${page}${qs}`;
  }, []);

  const ctx = useMemo(() => ({ wid, data, reload, runReview, openCode, go, params: route.params }),
    [wid, data, reload, runReview, openCode, go, route.params]);

  const current = workspaces.find((w) => w.id === wid);
  const r = data?.result;
  const nav: { page: Page; label: string; icon: JSX.Element; count?: number }[] = [
    { page: "overview", label: "Overview", icon: <LayoutDashboard size={17} /> },
    { page: "review", label: "Review", icon: <GitPullRequest size={17} />, count: r?.findings.length },
    { page: "map", label: "Map", icon: <Compass size={17} /> },
    { page: "chat", label: "Code chat", icon: <MessageSquareCode size={17} /> },
    { page: "design", label: "Design", icon: <PenTool size={17} /> },
  ];
  const nav2: { page: Page; label: string; icon: JSX.Element }[] = [
    { page: "releases", label: "Releases", icon: <Package size={17} /> },
    { page: "knowledge", label: "Knowledge base", icon: <BookOpen size={17} /> },
    { page: "settings", label: "Settings", icon: <SettingsIcon size={17} /> },
  ];
  const titles: Record<Page, string> = {
    overview: "Overview", review: "Review", map: "Map", chat: "Code chat", design: "Design",
    releases: "Releases", knowledge: "Knowledge base", settings: "Settings",
  };

  async function doOpen() {
    try {
      const w = await api.open(repoPath.trim());
      setWorkspaces((x) => (x.some((y) => y.id === w.id) ? x : [...x, w]));
      setWid(w.id);
      setOpenRepo(false);
      setRepoPath("");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  const flush = route.page === "map" || route.page === "chat";
  return (
    <AppCtx.Provider value={ctx}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="mark" aria-hidden="true" />
            <div className="brand-name">Codewright</div>
          </div>
          <div className="nav-label">Repository</div>
          <div className="row" style={{ padding: "0 4px 6px" }}>
            <select className="select grow" value={wid ?? ""} onChange={(e) => setWid(e.target.value)}>
              {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              {!workspaces.length && <option value="">No repository</option>}
            </select>
            <button className="btn icon" title="Open another repository" onClick={() => setOpenRepo(true)}><FolderOpen size={15} /></button>
          </div>
          <div className="nav-label">Modes</div>
          {nav.map((n) => (
            <button key={n.page} className={`nav-item ${route.page === n.page ? "active" : ""}`} onClick={() => go(n.page)}>
              {n.icon}{n.label}{n.count != null && <span className="count">{n.count}</span>}
            </button>
          ))}
          <div className="nav-label">Workspace</div>
          {nav2.map((n) => (
            <button key={n.page} className={`nav-item ${route.page === n.page ? "active" : ""}`} onClick={() => go(n.page)}>
              {n.icon}{n.label}
            </button>
          ))}
          <div className="sidebar-foot">
            <button className="btn ghost" style={{ justifyContent: "flex-start" }} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
              {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}{theme === "dark" ? "Light theme" : "Dark theme"}
            </button>
            <div className="note">Codewright suggests. You decide what merges.</div>
          </div>
        </aside>
        <div className="main">
          <header className="topbar">
            <div className="crumbs">
              <span className="truncate" title={current?.root}>{current?.name ?? "Codewright"}</span>
              <span className="faint">/</span><strong>{titles[route.page]}</strong>
            </div>
            <div className="spacer" />
            {data && (
              <span className="status">
                {data.running ? <span className="row"><Spinner size={13} /> Reviewing…</span> : `Last review ${timeAgo(data.last_run)}`}
              </span>
            )}
            {wid && route.page !== "design" && (
              <button className="btn primary" disabled={!!data?.running} onClick={() => runReview(true)}>
                {data?.running ? <Spinner size={14} /> : <Play size={14} />}Run review
              </button>
            )}
          </header>
          <main className={`content ${flush ? "flush" : ""}`}>
            {route.page === "overview" && <Overview />}
            {route.page === "review" && <Review />}
            {route.page === "map" && <MapPage />}
            {route.page === "chat" && <Chat />}
            {route.page === "design" && <Design />}
            {route.page === "releases" && <Releases />}
            {route.page === "knowledge" && <Knowledge />}
            {route.page === "settings" && <SettingsPage />}
          </main>
        </div>
      </div>

      {code && (
        <>
          <div className="drawer-back" onClick={() => setCode(null)} />
          <div className="drawer">
            <div className="drawer-head">
              <span className="mono grow truncate">{code.file}{code.line ? `:${code.line}` : ""}</span>
              <button className="btn ghost icon" onClick={() => setCode(null)}><X size={16} /></button>
            </div>
            <div className="drawer-body">
              <CodeView text={code.text} highlightLines={code.span ?? (code.line ? [code.line, code.line] : undefined)} scrollTo={code.line} />
            </div>
          </div>
        </>
      )}

      {openRepo && (
        <Modal title="Open a repository" onClose={() => setOpenRepo(false)}
          footer={<><button className="btn" onClick={() => setOpenRepo(false)}>Cancel</button>
            <button className="btn primary" disabled={!repoPath.trim()} onClick={doOpen}>Open and review</button></>}>
          <p className="muted" style={{ marginTop: 0 }}>Path to a local repository on the machine running Codewright. Code is analysed locally; nothing is uploaded.</p>
          <input className="input" style={{ width: "100%" }} placeholder="/home/me/projects/my-service" value={repoPath}
            autoFocus onChange={(e) => setRepoPath(e.target.value)} onKeyDown={(e) => e.key === "Enter" && repoPath.trim() && doOpen()} />
        </Modal>
      )}
    </AppCtx.Provider>
  );
}
