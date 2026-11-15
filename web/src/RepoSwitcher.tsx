import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowUp, Check, ChevronsUpDown, Clock, Folder, FolderGit2, FolderOpen, GitBranch, Home, Loader2, Search, X,
} from "lucide-react";
import { api, FolderListing, RecentRepo, WorkspaceInfo } from "./api";
import { timeAgo, useToast } from "./ui";

interface Props {
  workspaces: WorkspaceInfo[];
  current: WorkspaceInfo | undefined;
  onSwitch: (id: string) => void;
  onChanged: () => Promise<WorkspaceInfo[]>;
  open: boolean;
  setOpen: (v: boolean) => void;
}

type Row = { key: string; kind: "open" | "recent" | "action"; ws?: WorkspaceInfo; recent?: RecentRepo };

function shortPath(p: string, home?: string) {
  const s = home && p.startsWith(home) ? `~${p.slice(home.length)}` : p;
  return s.length > 52 ? `…${s.slice(-50)}` : s;
}

export default function RepoSwitcher({ workspaces, current, onSwitch, onChanged, open, setOpen }: Props) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [recent, setRecent] = useState<RecentRepo[]>([]);
  const [idx, setIdx] = useState(0);
  const [mode, setMode] = useState<"list" | "browse">("list");
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const home = listing?.home;

  // ⌘K / Ctrl+K anywhere toggles the switcher
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpen(!open); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, setOpen]);

  useEffect(() => {
    if (!open) return;
    setQ(""); setIdx(0); setMode(workspaces.length ? "list" : "browse");
    api.recent().then(setRecent).catch(() => setRecent([]));
    api.folders("").then((l) => { setListing(l); setPathInput(l.path); }).catch(() => {});
    const t = setTimeout(() => inputRef.current?.focus(), 30);
    return () => clearTimeout(t);
  }, [open, workspaces.length]);

  const rows: Row[] = useMemo(() => {
    const t = q.trim().toLowerCase();
    const m = (name: string, path: string) => !t || name.toLowerCase().includes(t) || path.toLowerCase().includes(t);
    return [
      ...workspaces.filter((w) => m(w.name, w.root)).map((w) => ({ key: `w${w.id}`, kind: "open" as const, ws: w })),
      ...recent.filter((r) => m(r.name, r.path)).map((r) => ({ key: `r${r.path}`, kind: "recent" as const, recent: r })),
      { key: "browse", kind: "action" as const },
    ];
  }, [q, workspaces, recent]);

  useEffect(() => { setIdx((i) => Math.min(i, rows.length - 1)); }, [rows.length]);

  const openPath = useCallback(async (path: string) => {
    setBusy(true);
    try {
      const w = await api.open(path);
      await onChanged();
      onSwitch(w.id);
      setOpen(false);
      toast(`Opened ${w.name}`);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }, [onChanged, onSwitch, setOpen, toast]);

  function choose(r: Row) {
    if (r.kind === "open" && r.ws) { onSwitch(r.ws.id); setOpen(false); }
    else if (r.kind === "recent" && r.recent) openPath(r.recent.path);
    else { setMode("browse"); }
  }

  async function closeWs(w: WorkspaceInfo) {
    await api.closeWorkspace(w.id);
    const list = await onChanged();
    if (current?.id === w.id && list[0]) onSwitch(list[0].id);
    api.recent().then(setRecent);
  }

  async function forget(r: RecentRepo) {
    await api.forgetRecent(r.path);
    setRecent((x) => x.filter((y) => y.path !== r.path));
  }

  async function browse(path: string) {
    try {
      const l = await api.folders(path);
      setListing(l);
      setPathInput(l.path);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  function onKey(e: React.KeyboardEvent) {
    if (mode !== "list") return;
    if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(rows.length - 1, i + 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(0, i - 1)); }
    if (e.key === "Enter" && rows[idx]) { e.preventDefault(); choose(rows[idx]); }
  }

  return (
    <>
      <button className="repo-trigger" onClick={() => setOpen(true)} aria-haspopup="dialog" aria-expanded={open}>
        <span className="repo-trigger__icon"><FolderGit2 size={16} /></span>
        <span className="grow" style={{ minWidth: 0, textAlign: "left" }}>
          <span className="repo-trigger__name truncate">{current?.name ?? "Open a repository"}</span>
          <span className="repo-trigger__sub truncate">
            {current ? <>{current.branch && <><GitBranch size={11} />{current.branch} · </>}{current.findings ?? "–"} findings</> : "⌘K"}
          </span>
        </span>
        <ChevronsUpDown size={15} className="faint" />
      </button>

      {open && (
        <div className="palette-back" onMouseDown={() => setOpen(false)}>
          <div className="palette" role="dialog" aria-label="Switch repository" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}>
            {mode === "list" ? (
              <>
                <div className="palette-search">
                  <Search size={16} className="faint" />
                  <input ref={inputRef} placeholder="Switch repository…" aria-label="Search repositories" value={q}
                    onChange={(e) => { setQ(e.target.value); setIdx(0); }}
                    onKeyDown={(e) => e.key === "Escape" && setOpen(false)} />
                  <kbd>esc</kbd>
                </div>
                <div className="palette-list">
                  {rows.some((r) => r.kind === "open") && <div className="palette-group">Open</div>}
                  {rows.map((r, i) => {
                    if (r.kind === "open" && r.ws) {
                      const w = r.ws;
                      return (
                        <div key={r.key} className={`palette-row ${i === idx ? "active" : ""}`} onMouseEnter={() => setIdx(i)} onClick={() => choose(r)}>
                          <span className="palette-row__icon"><FolderGit2 size={16} /></span>
                          <span className="grow" style={{ minWidth: 0 }}>
                            <span className="row" style={{ gap: 8 }}>
                              <span className="palette-row__name truncate">{w.name}</span>
                              {w.branch && <span className="faint row" style={{ gap: 3, fontSize: 12 }}><GitBranch size={11} />{w.branch}</span>}
                              {w.running && <Loader2 size={12} className="spin faint" />}
                            </span>
                            <span className="palette-row__path truncate">{shortPath(w.root, home)}</span>
                          </span>
                          <span className="palette-row__meta">
                            {w.findings != null && <span>{w.findings} findings{w.verified ? ` · ${w.verified} proven` : ""}</span>}
                            <span className="faint">{w.running ? "reviewing…" : timeAgo(w.last_run)}</span>
                          </span>
                          {current?.id === w.id ? <Check size={15} style={{ color: "var(--accent)" }} />
                            : <button className="btn sm ghost icon" title="Close" aria-label={`Close ${w.name}`} onClick={(e) => { e.stopPropagation(); closeWs(w); }}><X size={13} /></button>}
                        </div>
                      );
                    }
                    if (r.kind === "recent" && r.recent) {
                      const rr = r.recent;
                      const first = i === rows.findIndex((x) => x.kind === "recent");
                      return (
                        <div key={r.key}>
                          {first && <div className="palette-group">Recent</div>}
                          <div className={`palette-row ${i === idx ? "active" : ""}`} onMouseEnter={() => setIdx(i)} onClick={() => choose(r)}>
                            <span className="palette-row__icon"><Clock size={15} /></span>
                            <span className="grow" style={{ minWidth: 0 }}>
                              <span className="palette-row__name truncate">{rr.name}</span>
                              <span className="palette-row__path truncate">{shortPath(rr.path, home)}</span>
                            </span>
                            <span className="palette-row__meta"><span className="faint">opened {timeAgo(rr.opened)}</span></span>
                            <button className="btn sm ghost icon" title="Remove from recent" aria-label={`Forget ${rr.name}`} onClick={(e) => { e.stopPropagation(); forget(rr); }}><X size={13} /></button>
                          </div>
                        </div>
                      );
                    }
                    return (
                      <div key={r.key}>
                        <div className="palette-sep" />
                        <div className={`palette-row ${i === idx ? "active" : ""}`} onMouseEnter={() => setIdx(i)} onClick={() => choose(r)}>
                          <span className="palette-row__icon"><FolderOpen size={15} /></span>
                          <span className="grow palette-row__name">Open a folder…</span>
                          <span className="faint" style={{ fontSize: 12 }}>browse this machine</span>
                        </div>
                      </div>
                    );
                  })}
                  {rows.length === 1 && q && <div className="faint" style={{ padding: "10px 14px", fontSize: 13.5 }}>No repository matches “{q}”.</div>}
                </div>
                <div className="palette-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> to move</span><span><kbd>↵</kbd> to open</span><span><kbd>⌘K</kbd> to toggle</span></div>
              </>
            ) : (
              <>
                <div className="palette-search">
                  {workspaces.length > 0 && <button className="btn sm ghost icon" aria-label="Back" onClick={() => setMode("list")}><ArrowLeft size={15} /></button>}
                  <input className="mono" style={{ fontSize: 13 }} aria-label="Folder path" value={pathInput}
                    onChange={(e) => setPathInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") browse(pathInput); if (e.key === "Escape") setOpen(false); }} />
                  <button className="btn sm ghost icon" title="Home" aria-label="Home folder" onClick={() => browse("")}><Home size={14} /></button>
                  <button className="btn sm ghost icon" title="Up one folder" aria-label="Up one folder" disabled={!listing?.parent} onClick={() => listing?.parent && browse(listing.parent)}><ArrowUp size={14} /></button>
                </div>
                <div className="palette-list">
                  {!listing ? <div className="faint" style={{ padding: 14 }}><Loader2 size={14} className="spin" /></div> : (
                    <>
                      {listing.dirs.length === 0 && <div className="faint" style={{ padding: "10px 14px", fontSize: 13.5 }}>No subfolders here.</div>}
                      {listing.dirs.map((d) => (
                        <div key={d.path} className="palette-row" onClick={() => browse(d.path)} onDoubleClick={() => openPath(d.path)}>
                          <span className="palette-row__icon">{d.git ? <FolderGit2 size={15} /> : <Folder size={15} />}</span>
                          <span className="grow palette-row__name truncate">{d.name}</span>
                          {d.python && <span className="badge">Python</span>}
                          {d.git && <span className="badge">git</span>}
                          {(d.git || d.python) && (
                            <button className="btn sm" onClick={(e) => { e.stopPropagation(); openPath(d.path); }}>Open</button>
                          )}
                        </div>
                      ))}
                    </>
                  )}
                </div>
                <div className="palette-foot" style={{ justifyContent: "space-between" }}>
                  <span className="truncate">
                    {listing && (listing.python || listing.git)
                      ? <>This folder {listing.git ? "is a git repo" : "has Python project files"}.</>
                      : "Pick a folder with a Python project. Code is analysed locally; nothing is uploaded."}
                  </span>
                  <button className="btn sm primary" disabled={busy || !listing} onClick={() => listing && openPath(listing.path)}>
                    {busy ? <Loader2 size={13} className="spin" /> : <FolderOpen size={13} />}Open this folder
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
