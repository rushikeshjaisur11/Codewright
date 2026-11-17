import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown, ArrowLeft, ArrowRight, ArrowDownRight, CheckCircle2, ChevronRight, Code2, Compass, CornerDownRight,
  EyeOff, FileCode2, Network, GitCommit, History, Layers, Search, Workflow, XCircle,
} from "lucide-react";
import { useApp } from "../App";
import { api, MapMeta, MapNode, MapView, ModulePage } from "../api";
import Graph, { KIND_ICON, Overlay } from "../Graph";
import { CopyButton, Empty, KIND_STYLE, Spinner, Tabs, useToast } from "../ui";

type Level = "system" | "module" | "flow";
type Crumb = { level: Level; target: string; label: string };

export default function MapPage() {
  const { wid, data, openCode, go } = useApp();
  const toast = useToast();
  const [trail, setTrail] = useState<Crumb[]>([{ level: "system", target: "", label: "System" }]);
  const here = trail[trail.length - 1];
  const [meta, setMeta] = useState<MapMeta | null>(null);
  const [view, setView] = useState<MapView | null>(null);
  const [loading, setLoading] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ id: string; n: number } | null>(null);
  const [page, setPage] = useState<ModulePage | null>(null);
  const [overlay, setOverlay] = useState<Overlay>({ arch: true, findings: true, hotspots: true, untested: false });
  const [dir, setDir] = useState<"LR" | "TB">("LR");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<"" | "overlay" | "mermaid">("");
  const [q, setQ] = useState("");
  const [qOpen, setQOpen] = useState(false);
  const [showUnused, setShowUnused] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const has = !!data?.result;

  useEffect(() => {
    if (!wid || !has) return;
    api.mapMeta(wid).then(setMeta).catch((e) => toast(e.message, "error"));
  }, [wid, has, toast]);

  useEffect(() => {
    if (!wid || !has) return;
    setLoading(true);
    setSel(null);
    setHidden(new Set());
    setDir(here.level === "flow" ? "TB" : "LR");
    api.map(wid, here.level, here.target, showUnused).then(setView)
      .catch((e) => { setView(null); toast(e.message, "error"); })
      .finally(() => setLoading(false));
  }, [wid, has, here.level, here.target, showUnused, toast]);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const tag = document.activeElement?.tagName;
      if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
        e.preventDefault();
        searchRef.current?.focus();
      }
      if (e.key === "Escape") { setMenu(""); setQOpen(false); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  const visible = useMemo(() => {
    if (!view) return { nodes: [] as MapNode[], edges: [] as MapView["edges"] };
    const nodes = view.nodes.filter((n) => !hidden.has(n.kind));
    const ids = new Set(nodes.map((n) => n.id));
    return { nodes, edges: view.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
  }, [view, hidden]);

  const byId = useMemo(() => new Map((view?.nodes ?? []).map((n) => [n.id, n])), [view]);
  const node = sel ? byId.get(sel) ?? null : null;
  const links = useMemo(() => {
    const none = { out: [] as { n: MapNode; label: string }[], inc: [] as { n: MapNode; label: string }[] };
    if (!view || !sel) return none;
    for (const e of view.edges) {
      if (e.source === sel && byId.get(e.target)) none.out.push({ n: byId.get(e.target)!, label: e.label });
      if (e.target === sel && byId.get(e.source)) none.inc.push({ n: byId.get(e.source)!, label: e.label });
    }
    return none;
  }, [view, sel, byId]);

  useEffect(() => {
    setPage(null);
    if (!wid || !node || node.kind !== "module") return;
    const f = node.files[0]?.file;
    if (f) api.module(wid, f).then(setPage).catch(() => setPage(null));
  }, [wid, node]);

  const matches = useMemo(() => {
    const t = q.trim().toLowerCase();
    if (!t || !view) return [];
    return view.nodes.filter((n) => `${n.label} ${n.detail}`.toLowerCase().includes(t)).slice(0, 8);
  }, [q, view]);

  if (!data) return <Empty icon={<Spinner />} title="Loading" />;
  if (!has) {
    return <div style={{ margin: "auto" }}><Empty icon={<Compass size={20} />} title="The map appears after the first review">
      {data.running ? "Building the code graph…" : "Run a review to build the code graph."}</Empty></div>;
  }

  function goTo(level: Level, target: string, label: string, replace = false) {
    setTrail((t) => {
      if (level === "system") return [{ level, target: "", label: "System" }];
      return [...(replace ? t.slice(0, -1) : t), { level, target, label }];
    });
  }
  function pick(id: string) {
    setSel(id);
    setFocus({ id, n: Date.now() });
    setQ("");
    setQOpen(false);
  }
  function drillTarget(n: MapNode): Crumb | null {
    if (here.level === "system" && n.kind === "component") return { level: "module", target: n.label, label: n.label };
    if (here.level === "module" && n.kind === "module" && meta) {
      const ep = meta.entry_points.find((e) => n.files.some((f) => f.file === e.file));
      if (ep) return { level: "flow", target: ep.id, label: ep.name };
    }
    return null;
  }
  function drill(id: string) {
    const n = byId.get(id);
    const t = n && drillTarget(n);
    if (t) goTo(t.level, t.target, t.label);
    else if (n?.files[0]) openCode(n.files[0].file, n.files[0].line);
  }
  function switchLevel(l: Level) {
    if (l === "system") goTo("system", "", "System");
    else if (l === "module" && meta?.components[0]) goTo("module", meta.components[0], meta.components[0], here.level !== "system");
    else if (l === "flow" && meta?.entry_points[0]) goTo("flow", meta.entry_points[0].id, meta.entry_points[0].name, here.level !== "system");
  }

  const kinds = Object.entries((view?.nodes ?? []).reduce<Record<string, number>>((a, n) => ({ ...a, [n.kind]: (a[n.kind] ?? 0) + 1 }), {}));
  const drillTo = node ? drillTarget(node) : null;
  const NodeIcon = node ? KIND_ICON[node.kind] ?? Layers : Layers;

  return (
    <div className="map-wrap">
      <div className="map-canvas">
        <div className="map-toolbar">
          <div className="toolbar-card">
            <Tabs<Level> value={here.level} onChange={switchLevel} options={[
              { value: "system", label: <><Compass size={13} />System</> },
              { value: "module", label: <><FileCode2 size={13} />Modules</> },
              { value: "flow", label: <><Workflow size={13} />Request flow</> },
            ]} />
            {here.level === "module" && meta && (
              <select className="select" aria-label="Component" value={here.target} onChange={(e) => goTo("module", e.target.value, e.target.value, true)}>
                {meta.components.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            )}
            {here.level === "flow" && meta && meta.entry_points.length > 0 && (
              <select className="select" aria-label="Entry point" style={{ maxWidth: 280 }} value={here.target}
                onChange={(e) => { const ep = meta.entry_points.find((x) => x.id === e.target.value)!; goTo("flow", ep.id, ep.name, true); }}>
                {meta.entry_points.map((e) => <option key={e.id} value={e.id}>{e.trigger} → {e.name}</option>)}
              </select>
            )}
          </div>

          <div className="toolbar-card search-card">
            <Search size={14} className="faint" style={{ marginLeft: 10 }} />
            <input ref={searchRef} className="bare" placeholder="Find a node" aria-label="Find a node" value={q}
              onChange={(e) => { setQ(e.target.value); setQOpen(true); }} onFocus={() => setQOpen(true)} onBlur={() => setQOpen(false)}
              onKeyDown={(e) => { if (e.key === "Enter" && matches[0]) pick(matches[0].id); }} />
            <kbd>/</kbd>
            {qOpen && matches.length > 0 && (
              <div className="popover" style={{ top: 44, left: 0, width: 300, padding: 4 }}>
                {matches.map((n) => {
                  const I = KIND_ICON[n.kind] ?? Layers;
                  return (
                    <button key={n.id} className="popover-item" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(n.id)}>
                      <I size={14} style={{ color: KIND_STYLE[n.kind]?.color }} /><span className="grow truncate">{n.label}</span>
                      <span className="faint" style={{ fontSize: 12 }}>{KIND_STYLE[n.kind]?.label}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div style={{ marginLeft: "auto" }} className="toolbar-card">
            <button className="btn sm ghost icon" title={dir === "LR" ? "Lay out top to bottom" : "Lay out left to right"}
              aria-label="Change layout direction" onClick={() => setDir(dir === "LR" ? "TB" : "LR")}>
              {dir === "LR" ? <ArrowRight size={15} /> : <ArrowDown size={15} />}
            </button>
            <div style={{ position: "relative" }}>
              <button className={`btn sm ghost ${menu === "overlay" ? "on" : ""}`} aria-expanded={menu === "overlay"}
                onClick={() => setMenu(menu === "overlay" ? "" : "overlay")}><Layers size={14} />Overlays</button>
              {menu === "overlay" && (
                <div className="popover" style={{ top: 40, right: 0, width: 220, padding: 4 }}>
                  {([["arch", "Architecture suggestions"], ["findings", "Other findings"], ["hotspots", "Hotspots (fan-in)"], ["untested", "Untested code"]] as const).map(([k, label]) => (
                    <label key={k} className="check popover-item">
                      <input type="checkbox" checked={overlay[k] !== false} onChange={(e) => setOverlay({ ...overlay, [k]: e.target.checked })} />{label}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <div style={{ position: "relative" }}>
              <button className={`btn sm ghost ${menu === "mermaid" ? "on" : ""}`} aria-expanded={menu === "mermaid"}
                onClick={() => setMenu(menu === "mermaid" ? "" : "mermaid")}>Mermaid</button>
              {menu === "mermaid" && view && (
                <div className="popover" style={{ top: 40, right: 0, width: 400 }}>
                  <div className="row" style={{ padding: "10px 12px", borderBottom: "1px solid var(--border)" }}>
                    <span className="grow" style={{ fontWeight: 500 }}>Mermaid export</span><CopyButton small text={view.mermaid} />
                  </div>
                  <pre className="code" style={{ margin: 0, border: 0, borderRadius: 0, padding: 12, maxHeight: 340 }}>{view.mermaid}</pre>
                </div>
              )}
            </div>
          </div>
          {trail.length > 1 && (
            <div style={{ flexBasis: "100%" }}><nav className="crumbs-bar" aria-label="Map path">
            <button className="btn sm ghost icon" aria-label="Back" onClick={() => setTrail((t) => t.slice(0, -1))}><ArrowLeft size={14} /></button>
            {trail.map((c, i) => (
              <span key={i} className="row" style={{ gap: 4 }}>
                {i > 0 && <ChevronRight size={13} className="faint" />}
                {i === trail.length - 1 ? <strong>{c.label}</strong>
                  : <button className="link-btn" onClick={() => setTrail((t) => t.slice(0, i + 1))}>{c.label}</button>}
              </span>
            ))}
          </nav></div>
          )}
        </div>

        {loading && <div className="map-loading"><Spinner size={20} /></div>}
        {view && visible.nodes.length > 0 && (
          <Graph nodes={visible.nodes} edges={visible.edges} selected={sel} overlay={overlay} dir={dir} focus={focus}
            onSelect={setSel} onOpen={drill} />
        )}
        {view && visible.nodes.length === 0 && (
          <div className="map-loading"><Empty icon={<Compass size={20} />} title="Nothing to draw">Every kind on this level is hidden, or it has no nodes.</Empty></div>
        )}
      </div>

      <aside className="map-side">
        {node ? (
          <>
            <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
              <span className="cw-node__icon" style={{ width: 36, height: 36, color: KIND_STYLE[node.kind]?.color, background: KIND_STYLE[node.kind]?.soft }}>
                <NodeIcon size={18} />
              </span>
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="faint" style={{ fontSize: 13 }}>{KIND_STYLE[node.kind]?.label ?? node.kind}</div>
                <h2 style={{ wordBreak: "break-word" }}>{node.label}</h2>
                {node.detail && <div className="muted mono" style={{ fontSize: 12, marginTop: 2 }}>{node.detail}</div>}
              </div>
            </div>
            <div className="row wrap">
              {drillTo && (
                <button className="btn sm primary" onClick={() => goTo(drillTo.level, drillTo.target, drillTo.label)}>
                  {drillTo.level === "module" ? "Open modules" : "Show request flow"}<ArrowDownRight size={13} />
                </button>
              )}
              {node.files[0] && <button className="btn sm" onClick={() => openCode(node.files[0].file, node.files[0].line)}><Code2 size={13} />Open code</button>}
              {!!node.arch && <button className="btn sm" onClick={() => go("review", { cat: "architecture" })}><Network size={13} />{node.arch} architecture suggestion{node.arch > 1 ? "s" : ""}</button>}
              {node.findings - (node.arch ?? 0) > 0 && <button className="btn sm" onClick={() => go("review", {})}>{node.findings - (node.arch ?? 0)} other finding{node.findings - (node.arch ?? 0) > 1 ? "s" : ""}</button>}
            </div>
            {(node.hotspot >= 2 || node.untested > 0) && (
              <div className="row wrap" style={{ gap: 6 }}>
                {node.hotspot >= 2 && <span className="badge red">Called from {node.hotspot} places</span>}
                {node.untested > 0 && <span className="badge amber">{node.untested} untested</span>}
              </div>
            )}
            {page?.doc && <div className="muted" style={{ fontStyle: "italic" }}>{page.doc}</div>}

            {(links.out.length > 0 || links.inc.length > 0) && (
              <div>
                <div className="section-title">Connections</div>
                <div className="conn">
                  {links.out.map((l, i) => (
                    <button key={`o${i}`} className="conn-row" onClick={() => pick(l.n.id)}>
                      <ArrowRight size={13} className="faint" /><span className="grow truncate">{l.n.label}</span><span className="faint">{l.label}</span>
                    </button>
                  ))}
                  {links.inc.map((l, i) => (
                    <button key={`i${i}`} className="conn-row" onClick={() => pick(l.n.id)}>
                      <ArrowLeft size={13} className="faint" /><span className="grow truncate">{l.n.label}</span><span className="faint">{l.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {page ? (
              <div>
                <div className="section-title">Functions</div>
                <div className="conn">
                  {page.functions.map((fn) => (
                    <button key={fn.name} className="conn-row" onClick={() => openCode(page.file, fn.line, [fn.line, fn.end])}>
                      <CornerDownRight size={13} className="faint" /><span className="grow truncate mono" style={{ fontSize: 12 }}>{fn.name}</span>
                      {fn.findings > 0 && <span className="badge accent">{fn.findings}</span>}
                      {fn.tested ? <span className="badge green">tested</span> : <span className="badge amber">untested</span>}
                    </button>
                  ))}
                </div>
              </div>
            ) : node.files.length > 0 && (
              <div>
                <div className="section-title"><Code2 size={13} />Implemented in</div>
                <div className="conn">
                  {node.files.slice(0, 30).map((f, i) => (
                    <button key={i} className="conn-row" onClick={() => openCode(f.file, f.line)}>
                      <span className="grow truncate mono" style={{ fontSize: 12 }}>{f.name ? `${f.name} · ` : ""}{f.file}:{f.line}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        ) : (
          <>
            <div>
              <h2>{view?.title ?? "Architecture"}</h2>
              <p className="muted" style={{ margin: "6px 0 0", fontSize: 14 }}>
                Generated from the code graph on every review. Hover a box to trace its connections, click it for details,
                double-click to drill in. Press <kbd>/</kbd> to find a node.
              </p>
            </div>
            <div>
              <div className="section-title">Show</div>
              <div className="chips">
                {kinds.map(([k, n]) => {
                  const I = KIND_ICON[k] ?? Layers;
                  const off = hidden.has(k);
                  return (
                    <button key={k} className={`chip ${off ? "" : "on"}`} aria-pressed={!off} style={{ height: 30, fontSize: 13 }}
                      onClick={() => setHidden((h) => { const s = new Set(h); if (off) s.delete(k); else s.add(k); return s; })}>
                      <I size={13} style={{ color: off ? undefined : KIND_STYLE[k]?.color }} />{KIND_STYLE[k]?.label ?? k}
                      <span className="faint">{n}</span>
                    </button>
                  );
                })}
              </div>
            </div>
            {meta && meta.unused.length > 0 && here.level !== "flow" && (
              <div className="card card-pad">
                <div className="section-title"><EyeOff size={13} />Not part of the running system</div>
                <p className="muted" style={{ margin: "0 0 10px", fontSize: 13 }}>
                  {meta.unused.length} file{meta.unused.length > 1 ? "s" : ""} that no entry point, launch config or agent loads. {showUnused ? "Shown on the map." : "Left off the map."}
                </p>
                <label className="row" style={{ cursor: "pointer", fontSize: 13, marginBottom: 10 }}>
                  <input type="checkbox" checked={showUnused} onChange={(e) => setShowUnused(e.target.checked)} />Show them on the map
                </label>
                <div className="conn">
                  {meta.unused.slice(0, 40).map((u) => (
                    <button key={u.file} className="conn-row" onClick={() => openCode(u.file, 1)} title={u.file} style={{ alignItems: "flex-start" }}>
                      <FileCode2 size={13} className="faint" style={{ marginTop: 3, flex: "none" }} />
                      <span className="grow" style={{ minWidth: 0 }}>
                        <span className="mono" style={{ fontSize: 12, display: "block", wordBreak: "break-all" }}>{u.file}</span>
                        <span className="faint" style={{ fontSize: 11.5 }}>{u.reason}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {meta?.drift !== undefined && (
              <div className="card card-pad">
                <div className="section-title">Design drift</div>
                {meta.drift === null ? <span className="muted" style={{ fontSize: 14 }}>No intended architecture saved. Make one in <a href="#/design">Design</a>.</span>
                  : meta.drift.ok ? <div className="row"><CheckCircle2 size={15} color="var(--green)" />Matches {meta.drift.design}</div> : (
                    <div className="col" style={{ gap: 4 }}>
                      <div className="row"><XCircle size={15} color="var(--red)" /><span style={{ fontWeight: 500 }}>Drift from {meta.drift.design}</span></div>
                      {meta.drift.unplanned.map((u) => <div key={u.kind} className="muted" style={{ fontSize: 13 }}>Unplanned {u.kind}: {u.systems.join(", ")}</div>)}
                      {meta.drift.not_built.map((u) => <div key={u.kind} className="faint" style={{ fontSize: 13 }}>Not built yet: {u.components.join(", ")}</div>)}
                      {meta.drift.violations.map((v, i) => <div key={i} className="mono" style={{ fontSize: 11.5, color: "var(--red)" }}>{v.file}: {v.rule}</div>)}
                    </div>
                  )}
              </div>
            )}
            {meta && (
              <div className="card card-pad">
                <div className="section-title"><History size={13} />Changes this week</div>
                {!meta.changes.since ? <span className="muted" style={{ fontSize: 14 }}>First snapshot taken. Changes show up after later reviews.</span> : (
                  [...meta.changes.added_nodes, ...meta.changes.removed_nodes, ...meta.changes.added_edges, ...meta.changes.removed_edges].length === 0
                    ? <span className="muted" style={{ fontSize: 14 }}>No structural changes in the last 7 days.</span>
                    : <div className="col mono" style={{ gap: 2, fontSize: 12 }}>
                      {meta.changes.added_nodes.map((x) => <span key={`an${x}`} style={{ color: "var(--green)" }}>+ {x}</span>)}
                      {meta.changes.removed_nodes.map((x) => <span key={`rn${x}`} style={{ color: "var(--red)" }}>− {x}</span>)}
                      {meta.changes.added_edges.map((x) => <span key={`ae${x}`} style={{ color: "var(--green)" }}>+ {x.replace(/[cxm]:/g, "")}</span>)}
                      {meta.changes.removed_edges.map((x) => <span key={`re${x}`} style={{ color: "var(--red)" }}>− {x.replace(/[cxm]:/g, "")}</span>)}
                    </div>
                )}
              </div>
            )}
            {meta && meta.entry_points.length > 0 && (
              <div>
                <div className="section-title"><GitCommit size={13} />Entry points</div>
                <div className="conn">
                  {meta.entry_points.map((e) => (
                    <button key={e.id} className="conn-row" onClick={() => goTo("flow", e.id, e.name, here.level === "flow")}>
                      <Workflow size={13} className="faint" /><span className="grow truncate mono" style={{ fontSize: 12 }}>{e.trigger}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
