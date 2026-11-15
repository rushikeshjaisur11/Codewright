import { useEffect, useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpRight, CheckCircle2, Code2, Compass, FileCode2, GitCommit, History, Workflow, XCircle } from "lucide-react";
import { useApp } from "../App";
import { api, MapMeta, MapView, ModulePage } from "../api";
import Graph, { Overlay } from "../Graph";
import { CopyButton, Empty, KIND_STYLE, Spinner, Tabs, useToast } from "../ui";

type Level = "system" | "module" | "flow";

export default function MapPage() {
  const { wid, data, openCode, go } = useApp();
  const toast = useToast();
  const [level, setLevel] = useState<Level>("system");
  const [target, setTarget] = useState("");
  const [meta, setMeta] = useState<MapMeta | null>(null);
  const [view, setView] = useState<MapView | null>(null);
  const [loading, setLoading] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [page, setPage] = useState<ModulePage | null>(null);
  const [overlay, setOverlay] = useState<Overlay>({ findings: true, hotspots: true, untested: false });
  const [showMermaid, setShowMermaid] = useState(false);
  const has = !!data?.result;

  useEffect(() => {
    if (!wid || !has) return;
    api.mapMeta(wid).then(setMeta).catch((e) => toast(e.message, "error"));
  }, [wid, has, toast]);

  useEffect(() => {
    if (!wid || !has) return;
    setLoading(true);
    setSel(null);
    api.map(wid, level, target).then(setView).catch((e) => { setView(null); toast(e.message, "error"); }).finally(() => setLoading(false));
  }, [wid, has, level, target, toast]);

  const node = useMemo(() => view?.nodes.find((n) => n.id === sel) ?? null, [view, sel]);

  useEffect(() => {
    setPage(null);
    if (!wid || !node || node.kind !== "module") return;
    const f = node.files[0]?.file;
    if (f) api.module(wid, f).then(setPage).catch(() => setPage(null));
  }, [wid, node]);

  if (!data) return <Empty icon={<Spinner />} title="Loading" />;
  if (!has) return <div style={{ margin: "auto" }}><Empty icon={<Compass size={22} />} title="Map appears after the first review">{data.running ? "Building the code graph…" : "Run a review to build the code graph."}</Empty></div>;

  function drill(id: string) {
    if (!view) return;
    const n = view.nodes.find((x) => x.id === id);
    if (!n) return;
    if (level === "system" && n.kind === "component") { setLevel("module"); setTarget(n.label); }
    else if (level === "module" && n.kind === "module" && meta) {
      const ep = meta.entry_points.find((e) => n.files.some((f) => f.file === e.file));
      if (ep) { setLevel("flow"); setTarget(ep.id); }
    }
  }

  const kinds = [...new Set((view?.nodes ?? []).map((n) => n.kind))];
  return (
    <div className="map-wrap">
      <div className="map-canvas">
        <div className="map-toolbar">
          <div className="toolbar-card">
            <Tabs<Level> value={level} onChange={(l) => { setLevel(l); setTarget(""); }} options={[
              { value: "system", label: <><Compass size={13} />System</> },
              { value: "module", label: <><FileCode2 size={13} />Modules</> },
              { value: "flow", label: <><Workflow size={13} />Request flow</> },
            ]} />
            {level === "module" && meta && (
              <select className="select" value={target || meta.components[0]} onChange={(e) => setTarget(e.target.value)}>
                {meta.components.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            )}
            {level === "flow" && meta && (
              meta.entry_points.length ? (
                <select className="select" style={{ maxWidth: 320 }} value={target || meta.entry_points[0].id} onChange={(e) => setTarget(e.target.value)}>
                  {meta.entry_points.map((e) => <option key={e.id} value={e.id}>{e.trigger} → {e.name}</option>)}
                </select>
              ) : <span className="faint" style={{ padding: "0 8px" }}>No entry points found</span>
            )}
          </div>
          <div className="toolbar-card text">
            <span className="faint">Overlay</span>
            {(["findings", "hotspots", "untested"] as const).map((k) => (
              <label key={k} className="check">
                <input type="checkbox" checked={overlay[k]} onChange={(e) => setOverlay({ ...overlay, [k]: e.target.checked })} />{k}
              </label>
            ))}
          </div>
          <div style={{ marginLeft: "auto" }} className="row">
            <button className="btn" onClick={() => setShowMermaid(!showMermaid)}>Mermaid</button>
          </div>
        </div>
        {loading && <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", zIndex: 4 }}><Spinner size={22} /></div>}
        {view && view.nodes.length > 0 && (
          <Graph nodes={view.nodes} edges={view.edges} selected={sel} overlay={overlay}
            onSelect={(id) => { if (id && id === sel) drill(id); else setSel(id); }} dir={level === "flow" ? "TB" : "LR"} />
        )}
        {view && view.nodes.length === 0 && <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center" }}><Empty icon={<Compass size={22} />} title="Nothing to draw">This level has no nodes.</Empty></div>}
        {showMermaid && view && (
          <div className="card" style={{ position: "absolute", right: 14, top: 64, width: 380, zIndex: 6, boxShadow: "var(--shadow)" }}>
            <div className="card-head"><h3>Mermaid export</h3><div className="right"><CopyButton small text={view.mermaid} /></div></div>
            <pre className="code" style={{ margin: 0, border: 0, padding: 12, maxHeight: 360 }}>{view.mermaid}</pre>
          </div>
        )}
      </div>

      <aside className="map-side">
        {node ? (
          <>
            <div>
              <div className="row" style={{ marginBottom: 6 }}>
                <span className="badge" style={{ background: KIND_STYLE[node.kind]?.soft, color: KIND_STYLE[node.kind]?.color }}>{KIND_STYLE[node.kind]?.label ?? node.kind}</span>
                {node.findings > 0 && <button className="badge accent" style={{ border: 0, cursor: "pointer" }} onClick={() => go("review", {})}>{node.findings} finding{node.findings > 1 ? "s" : ""}</button>}
              </div>
              <h2 style={{ wordBreak: "break-word" }}>{node.label}</h2>
              {node.detail && <div className="muted" style={{ marginTop: 4 }}>{node.detail}</div>}
              {(level !== "flow" && (node.kind === "component" || node.kind === "module")) && (
                <button className="btn sm" style={{ marginTop: 10 }} onClick={() => drill(node.id)}>
                  {level === "system" ? "Open modules" : "Show request flow"}<ArrowDownRight size={13} />
                </button>
              )}
            </div>
            {page?.doc && <div className="muted" style={{ fontStyle: "italic" }}>{page.doc}</div>}
            {node.files.length > 0 && (
              <div>
                <div className="section-title"><Code2 size={13} />Implemented in</div>
                <div className="col" style={{ gap: 4 }}>
                  {node.files.slice(0, 30).map((f, i) => (
                    <button key={i} className="btn sm ghost mono" style={{ justifyContent: "flex-start" }} onClick={() => openCode(f.file, f.line)}>
                      <span className="truncate">{f.name ? `${f.name}  ` : ""}<span className="faint">{f.file}:{f.line}</span></span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {page && (
              <>
                <div>
                  <div className="section-title">Functions</div>
                  <table className="table" style={{ fontSize: 12.5 }}>
                    <tbody>
                      {page.functions.map((fn) => (
                        <tr key={fn.name} className="click" onClick={() => openCode(page.file, fn.line, [fn.line, fn.end])}>
                          <td className="mono">{fn.name}</td>
                          <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                            {fn.findings > 0 && <span className="badge accent">{fn.findings}</span>}{" "}
                            {fn.tested ? <span className="badge green">tested</span> : <span className="badge amber">untested</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {page.imported_by.length > 0 && <div><div className="section-title"><ArrowUpRight size={13} />Imported by</div><div className="mono muted" style={{ fontSize: 12 }}>{page.imported_by.join(", ")}</div></div>}
                {page.external.length > 0 && <div><div className="section-title"><ArrowDownRight size={13} />External imports</div><div className="mono muted" style={{ fontSize: 12 }}>{page.external.join(", ")}</div></div>}
              </>
            )}
          </>
        ) : (
          <>
            <div>
              <h2>{view?.title ?? "Architecture"}</h2>
              <p className="muted" style={{ margin: "6px 0 0" }}>Generated from the code graph on every review, never drawn by hand. Click a box for its files; click it again to drill down.</p>
            </div>
            <div className="legend">
              {kinds.map((k) => <span key={k}><i style={{ background: KIND_STYLE[k]?.color ?? "var(--accent)" }} />{KIND_STYLE[k]?.label ?? k}</span>)}
            </div>
            {meta?.drift !== undefined && (
              <div className="card card-pad">
                <div className="section-title">Design drift</div>
                {meta.drift === null ? <span className="muted">No intended architecture saved. Make one in <a href="#/design">Design</a>.</span> :
                  meta.drift.ok ? <div className="row"><CheckCircle2 size={15} color="var(--green)" />Matches {meta.drift.design}</div> : (
                    <div className="col" style={{ gap: 4 }}>
                      <div className="row"><XCircle size={15} color="var(--red)" /><b>Drift from {meta.drift.design}</b></div>
                      {meta.drift.unplanned.map((u) => <div key={u.kind} className="muted" style={{ fontSize: 12.5 }}>Unplanned {u.kind}: {u.systems.join(", ")}</div>)}
                      {meta.drift.not_built.map((u) => <div key={u.kind} className="faint" style={{ fontSize: 12.5 }}>Not built yet: {u.components.join(", ")}</div>)}
                      {meta.drift.violations.map((v, i) => <div key={i} className="mono" style={{ fontSize: 11.5, color: "var(--red)" }}>{v.file}: {v.rule}</div>)}
                    </div>
                  )}
              </div>
            )}
            {meta && (
              <div className="card card-pad">
                <div className="section-title"><History size={13} />Architecture changes this week</div>
                {!meta.changes.since ? <span className="muted">First snapshot taken today. Changes show up after later reviews.</span> : (
                  [...meta.changes.added_nodes.map((x) => ["+", x]), ...meta.changes.removed_nodes.map((x) => ["−", x]),
                    ...meta.changes.added_edges.map((x) => ["+", x]), ...meta.changes.removed_edges.map((x) => ["−", x])].length === 0 ?
                    <span className="muted">No structural changes in the last 7 days.</span> :
                    <div className="col mono" style={{ gap: 2, fontSize: 12 }}>
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
                <div className="col" style={{ gap: 4 }}>
                  {meta.entry_points.map((e) => (
                    <button key={e.id} className="btn sm ghost" style={{ justifyContent: "flex-start" }} onClick={() => { setLevel("flow"); setTarget(e.id); }}>
                      <Workflow size={13} /><span className="truncate mono" style={{ fontSize: 11.5 }}>{e.trigger}</span>
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
