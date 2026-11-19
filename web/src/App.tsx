import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen, Bot, ClipboardCheck, Compass, GitPullRequest, LayoutDashboard, MessageSquareCode, Moon, Package,
  PenTool, Play, Radio, Settings as SettingsIcon, ShieldCheck, Sun, Wand2, X,
} from "lucide-react";
import { api, ReviewPayload, WorkspaceInfo } from "./api";
import { CodeView, ProgressBar, ReviewProgress, Spinner, timeAgo, useToast } from "./ui";
import RepoSwitcher from "./RepoSwitcher";
import { ActivityButton, useActivity } from "./Activity";
// Pages load on demand: the first screen doesn't wait for the map, design or chat code.
const Overview = lazy(() => import("./pages/Overview"));
const Review = lazy(() => import("./pages/Review"));
const MapPage = lazy(() => import("./pages/Map"));
const Chat = lazy(() => import("./pages/Chat"));
const AiPage = lazy(() => import("./pages/Ai"));
const Design = lazy(() => import("./pages/Design"));
const Releases = lazy(() => import("./pages/Releases"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const Plans = lazy(() => import("./pages/Plans"));
const Rules = lazy(() => import("./pages/Rules"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const AgentPage = lazy(() => import("./pages/Agent"));

export type Page =
  | "overview" | "review" | "map" | "ai" | "chat" | "agent" | "design" | "plans" | "rules" | "releases" | "knowledge" | "settings";

interface Ctx {
  wid: string | null;
  data: ReviewPayload | null;
  reload: () => Promise<void>;
  runReview: (proof: boolean, base?: string, bench?: boolean) => Promise<void>;
  openCode: (file: string, line?: number, span?: [number, number]) => void;
  go: (page: Page, params?: Record<string, string>) => void;
  params: URLSearchParams;
}
const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

function parseHash(): { page: Page; params: URLSearchParams } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [p, qs] = h.split("?");
  const pages: Page[] = ["overview", "review", "map", "ai", "chat", "agent", "design", "plans", "rules", "releases", "knowledge", "settings"];
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

  const refreshWorkspaces = useCallback(async () => {
    const list = await api.workspaces();
    setWorkspaces(list);
    return list;
  }, []);

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
          refreshWorkspaces();
          if (n.error) toast(n.error, "error");
          else toast(`Review finished: ${n.result?.findings.length ?? 0} findings`);
        }
      }, 700);
    }
  }, [wid, toast, refreshWorkspaces]);

  useEffect(() => {
    if (!wid) return;
    try { localStorage.setItem("cw-ws", wid); } catch { /* ignore */ }
    setData(null);
    api.review(wid).then((d) => {
      setData(d);
      if (!d.has_result && !d.running) {
        api.runReview(wid, false, "").then(() => reload()); // a quick scan: tests only run on files you choose
      } else if (d.running) reload();
    });
    return () => { if (poll.current != null) { clearInterval(poll.current); poll.current = null; } };
  }, [wid, reload]);

  // Live: the server re-reviews on every save; poll a tiny revision counter and reload only on change.
  const [watching, setWatching] = useState(false);
  const seenRevision = useRef<number | null>(null);
  useEffect(() => { setWatching(!!workspaces.find((x) => x.id === wid)?.watching); }, [wid, workspaces]);
  useEffect(() => {
    if (!wid || !watching) return;
    seenRevision.current = null;
    const timer = window.setInterval(async () => {
      try {
        const r = await api.revision(wid);
        if (seenRevision.current !== null && r.revision !== seenRevision.current && !r.running) {
          setData(await api.review(wid));
          refreshWorkspaces();
          const c = r.last_change;
          if (c && (c.new.length || c.resolved.length)) {
            toast([c.new.length && `${c.new.length} new`, c.resolved.length && `${c.resolved.length} resolved`].filter(Boolean).join(", ")
              + ` after saving ${c.files.slice(0, 2).join(", ")}${c.files.length > 2 ? "…" : ""}`);
          }
        }
        if (!r.running) seenRevision.current = r.revision;
      } catch { /* server restarting: try again next tick */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [wid, watching, toast, refreshWorkspaces]);
  const toggleWatch = useCallback(async () => {
    if (!wid) return;
    const info = await api.watch(wid, !watching);
    setWatching(!!info.watching);
    toast(info.watching ? "Live: re-reviewing on every save" : "Live review off");
  }, [wid, watching, toast]);

  const runReview = useCallback(async (proof: boolean, base = "", bench = false) => {
    if (!wid) return;
    await api.runReview(wid, proof, base, bench);
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
    { page: "ai", label: "AI & agents", icon: <Bot size={17} /> },
    { page: "chat", label: "Code chat", icon: <MessageSquareCode size={17} /> },
    { page: "agent", label: "AI agent", icon: <Wand2 size={17} /> },
    { page: "design", label: "Design", icon: <PenTool size={17} /> },
    { page: "plans", label: "Plans & migrations", icon: <ClipboardCheck size={17} /> },
  ];
  const nav2: { page: Page; label: string; icon: JSX.Element; count?: number }[] = [
    { page: "rules", label: "Architecture rules", icon: <ShieldCheck size={17} />, count: r?.rules?.errors || undefined },
    { page: "releases", label: "Dependencies", icon: <Package size={17} /> },
    { page: "knowledge", label: "Knowledge base", icon: <BookOpen size={17} /> },
    { page: "settings", label: "Settings", icon: <SettingsIcon size={17} /> },
  ];
  const titles: Record<Page, string> = {
    overview: "Overview", review: "Review", map: "Map", ai: "AI & agents", chat: "Code chat", agent: "AI agent", design: "Design",
    plans: "Plans & migrations", rules: "Architecture rules",
    releases: "Dependencies", knowledge: "Knowledge base", settings: "Settings",
  };

  const activity = useActivity(wid, !!data?.running);
  const latest = activity[activity.length - 1];
  // A repo's first review: pages that need a result show the progress instead of an empty state.
  const needsResult = !["design", "knowledge", "settings", "agent"].includes(route.page);
  // These pages need only the parsed code, so they show as soon as it is read, while findings are still computed.
  const graphOnly = ["map", "ai", "releases", "rules"].includes(route.page);
  const firstRun = needsResult && !!data && !data.result && data.running && !(graphOnly && data.graph_ready);
  const finishing = graphOnly && !!data && !data.result && data.running && !!data.graph_ready;
  const flush = !firstRun && (route.page === "map" || route.page === "chat");
  return (
    <AppCtx.Provider value={ctx}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="mark" aria-hidden="true" />
            <div className="brand-name">Codewright</div>
          </div>
          <RepoSwitcher workspaces={workspaces} current={current} onSwitch={setWid} onChanged={refreshWorkspaces}
            open={openRepo} setOpen={setOpenRepo} />
          <div className="nav-label">Modes</div>
          {nav.map((n) => (
            <button key={n.page} className={`nav-item ${route.page === n.page ? "active" : ""}`} onClick={() => go(n.page)}>
              {n.icon}{n.label}{n.count != null && <span className="count">{n.count}</span>}
            </button>
          ))}
          <div className="nav-label">Workspace</div>
          {nav2.map((n) => (
            <button key={n.page} className={`nav-item ${route.page === n.page ? "active" : ""}`} onClick={() => go(n.page)}>
              {n.icon}{n.label}{n.count != null && <span className="count">{n.count}</span>}
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
              <span className="faint sep-slash">/</span><strong>{titles[route.page]}</strong>
            </div>
            <div className="spacer" />
            {data && (
              <span className="status">
                {data.running
                  ? <span className="row truncate" style={{ maxWidth: 420 }} title={latest?.message ?? data.progress?.label}><Spinner size={13} /> {latest?.message ?? (data.progress ? `${data.progress.label} · ${data.progress.percent}%` : "Starting review…")}{data.progress ? ` · ${data.progress.percent}%` : ""}</span>
                  : `Last review ${timeAgo(data.last_run)}`}
              </span>
            )}
            {wid && <ActivityButton events={activity} running={!!data?.running} />}
            {wid && route.page !== "design" && (
              <button className={`btn ${watching ? "accent" : ""}`} onClick={toggleWatch} aria-pressed={watching}
                title="Re-review automatically whenever a Python file is saved (tests not run)">
                <Radio size={14} className={watching ? "pulse" : ""} />{watching ? "Live" : "Go live"}
              </button>
            )}
            {wid && route.page !== "design" && (
              <button className="btn primary" disabled={!!data?.running} onClick={() => runReview(false)}
                title="Re-scan the code for suggestions. Fast: tests are not run, and proof already gathered for unchanged files is kept">
                {data?.running ? <Spinner size={14} /> : <Play size={14} />}Re-scan
              </button>
            )}
            {data?.running && <ProgressBar percent={data.progress?.percent} label="Review progress" />}
          </header>
          <main className={`content ${flush ? "flush" : ""}`}>
            <Suspense fallback={<div className="page"><Spinner /></div>}>
            {firstRun && <ReviewProgress name={data!.name} progress={data!.progress} />}
            {data?.result?.stack.python_unsupported && (
              <div className="callout amber" style={{ margin: "10px 16px 0", fontSize: 13 }}>
                This repo targets Python {data.result.stack.python_unsupported}. Codewright reviews code for Python 3.11 and newer, so its
                suggestions may use features {data.result.stack.python_unsupported} doesn't have. Upgrade the project's Python (and
                <code> requires-python</code>) to get advice that fits.
              </div>
            )}
            {data?.from_cache && <div className="callout row" style={{ margin: "10px 16px 0", fontSize: 13 }}>{data.running ? <Spinner size={13} /> : null}Showing the review from {timeAgo(data.saved_at ?? null)}{data.running ? "; re-checking only the files that changed since" : ""}.</div>}
            {finishing && <div className="callout row" style={{ margin: "10px 16px 0", fontSize: 13 }}><Spinner size={13} />Findings are still being worked out{data?.progress ? ` (${data.progress.label.toLowerCase()}, ${data.progress.percent}%)` : ""}. This page is already live and fills in when they land.</div>}
            {!firstRun && <>
            {route.page === "overview" && <Overview />}
            {route.page === "review" && <Review />}
            {route.page === "map" && <MapPage />}
            {route.page === "ai" && <AiPage />}
            {route.page === "chat" && <Chat />}
            {route.page === "agent" && <AgentPage />}
            {route.page === "design" && <Design />}
            {route.page === "plans" && <Plans />}
            {route.page === "rules" && <Rules />}
            {route.page === "releases" && <Releases />}
            {route.page === "knowledge" && <Knowledge wid={wid} />}
            {route.page === "settings" && <SettingsPage />}
            </>}
            </Suspense>
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

    </AppCtx.Provider>
  );
}
