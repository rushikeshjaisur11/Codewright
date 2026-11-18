import { useEffect, useMemo, useState } from "react";
import { Bot, Cpu, EyeOff, FileCode2, Network, Search, Sparkles, Wrench } from "lucide-react";
import { useApp } from "../App";
import { AiFile, AiReport, AiRole, api, NodeDetails } from "../api";
import Graph, { KIND_ICON } from "../Graph";
import { CopyButton, Empty, KIND_STYLE, Spinner, Stat, Tabs, useToast } from "../ui";
import Inspector from "../Inspector";

const kindName = (k: string) => (k === "entry" ? "Runs agents" : k === "llm" ? "Model" : KIND_STYLE[k]?.label ?? k);

const ROLE: Record<AiRole, { label: string; badge: string; help: string }> = {
  agents: { label: "Defines agents", badge: "blue", help: "Builds agents or agent workflows" },
  "model calls": { label: "Calls a model", badge: "rose", help: "Sends requests to an LLM directly" },
  "runs agents": { label: "Runs agents", badge: "green", help: "Starts an agent: a route, CLI command or job" },
  tools: { label: "Agent tools", badge: "amber", help: "Functions an agent can call" },
  "calls AI code": { label: "Calls AI code", badge: "outline", help: "Calls a function that needs a model" },
  imports: { label: "Imports an AI SDK", badge: "outline", help: "Imports an SDK but makes no call the scan can see" },
};
type Filter = "all" | "model" | AiRole;

export default function AiPage() {
  const { wid, data, openCode, go, params } = useApp();
  const toast = useToast();
  const [rep, setRep] = useState<AiReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [details, setDetails] = useState<NodeDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [focusOn, setFocusOn] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [find, setFind] = useState("");
  const [focus, setFocusPt] = useState<{ id: string; n: number } | null>(null);
  const [showMermaid, setShowMermaid] = useState(false);
  const [root, setRoot] = useState("");
  // The map needs only the parsed code: draw it as soon as that is ready, and refresh when findings arrive.
  const has = !!data?.result || !!data?.graph_ready;
  const done = !!data?.has_result; // true once this session's review (not the saved one) has landed

  useEffect(() => {
    if (!wid || !has) return;
    setLoading(true);
    api.ai(wid).then(setRep).catch((e) => toast(e.message, "error")).finally(() => setLoading(false));
  }, [wid, has, done, data?.revision, toast]);

  const byId = useMemo(() => new Map((rep?.architecture.nodes ?? []).map((n) => [n.id, n])), [rep]);
  const node = sel ? byId.get(sel) ?? null : null;
  useEffect(() => { const n = params.get("node"); if (n) setSel(n); }, [params]);
  useEffect(() => {
    if (!wid || !sel) { setDetails(null); return; }
    let live = true;
    setDetailsLoading(true);
    api.aiNode(wid, sel).then((d) => { if (live) setDetails(d); }).catch(() => { if (live) setDetails(null); })
      .finally(() => { if (live) setDetailsLoading(false); });
    return () => { live = false; };
  }, [wid, sel]);
  const visible = useMemo(() => {
    const all = rep?.architecture ?? { nodes: [], edges: [] };
    let nodes = all.nodes.filter((n) => !hidden.has(n.kind));
    if (root) {  // only what this entry point or agent reaches, following the arrows
      const out = new Map<string, string[]>();
      for (const e of all.edges) out.set(e.source, [...(out.get(e.source) ?? []), e.target]);
      const seen = new Set([root]);
      const stack = [root];
      while (stack.length) for (const t of out.get(stack.pop()!) ?? []) if (!seen.has(t)) { seen.add(t); stack.push(t); }
      nodes = nodes.filter((n) => seen.has(n.id));
    }
    if (focusOn) {
      const near = new Set([focusOn]);
      for (const e of all.edges) { if (e.source === focusOn) near.add(e.target); if (e.target === focusOn) near.add(e.source); }
      nodes = nodes.filter((n) => near.has(n.id));
    }
    const ids = new Set(nodes.map((n) => n.id));
    return { nodes, edges: all.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
  }, [rep, hidden, focusOn, root]);
  // Where an architecture starts: entry points (apps, routes, `adk web`), then agents nothing else drives.
  const roots = useMemo(() => {
    const all = rep?.architecture ?? { nodes: [], edges: [] };
    const driven = new Set(all.edges.map((e) => e.target));
    const tops = all.nodes.filter((n) => !driven.has(n.id) && (n.kind === "entry" || n.kind === "agent" || n.kind === "workflow"));
    return tops.sort((a, b) => (a.kind === "entry" ? 0 : 1) - (b.kind === "entry" ? 0 : 1) || a.label.localeCompare(b.label));
  }, [rep]);
  const kinds = useMemo(() => Object.entries((rep?.architecture.nodes ?? []).reduce<Record<string, number>>((a, n) => ({ ...a, [n.kind]: (a[n.kind] ?? 0) + 1 }), {})), [rep]);
  const matches = useMemo(() => {
    const t = find.trim().toLowerCase();
    return !t || !rep ? [] : rep.architecture.nodes.filter((n) => `${n.label} ${n.detail}`.toLowerCase().includes(t)).slice(0, 8);
  }, [find, rep]);
  function pick(id: string) {
    const k = rep?.architecture.nodes.find((n) => n.id === id)?.kind;
    if (k && hidden.has(k)) setHidden((h) => { const x = new Set(h); x.delete(k); return x; });
    if (focusOn) setFocusOn(id); // walking the graph in focus mode: re-centre on the box picked
    setSel(id); setFocusPt({ id, n: Date.now() }); setFind("");
  }

  if (!data) return <Empty icon={<Spinner />} title="Loading" />;
  if (!has) return <Empty icon={<Bot size={20} />} title="AI & agents appear after the first review">Run a review to build the code graph.</Empty>;
  if (!rep) return <Empty icon={loading ? <Spinner /> : <Bot size={20} />} title={loading ? "Finding AI code" : "Nothing loaded"} />;

  const s = rep.summary;
  if (rep.files.length === 0) {
    return (
      <div className="page">
        <div className="page-head"><div><h1>AI &amp; agents</h1></div></div>
        <Empty icon={<Sparkles size={22} />} title="No AI in this repo">
          No file imports an LLM SDK or agent framework (Anthropic, OpenAI, Gemini, Google ADK, LangChain, LangGraph, CrewAI, Pydantic AI, LiteLLM and others).
        </Empty>
      </div>
    );
  }

  const needsModel = (f: AiFile) => f.roles.includes("agents") || f.roles.includes("model calls");
  const shown = rep.files.filter((f) => filter === "all" || (filter === "model" ? needsModel(f) : f.roles.includes(filter)));
  const counts = (r: AiRole) => rep.files.filter((f) => f.roles.includes(r)).length;
  const hasGraph = rep.architecture.nodes.length > 0;

  return (
    <div className="grid page">
      <div className="page-head">
        <div>
          <h1>AI &amp; agents</h1>
          <p>The files that need a model to work, and how the agents fit together: who runs each agent, what it delegates to, and which tools and models it uses. Read from the code; no model is called.</p>
        </div>
      </div>

      {!!rep.errors?.length && (
        <div className="callout amber" style={{ marginBottom: 14, fontSize: 13 }}>
          {rep.errors.length} file{rep.errors.length === 1 ? "" : "s"} could not be read for agents and were skipped; the rest of the page is complete.
          {rep.errors.slice(0, 5).map((e, i) => <div key={i} className="mono" style={{ fontSize: 12 }}>{e.file}: {e.error} ({e.step}, {e.at})</div>)}
        </div>
      )}
      <div className="stats">
        <Stat label="Files that need a model" icon={<Sparkles size={14} />} value={s.need_model}
          sub={`${s.files} files touch AI in some way`} />
        <Stat label="Agents" icon={<Bot size={14} />} value={s.agents}
          sub={s.workflows ? `${s.workflows} workflow graph${s.workflows > 1 ? "s" : ""}` : s.frameworks.filter((f) => /ADK|Agents|CrewAI|Pydantic|LangChain|smol|AutoGen|LlamaIndex/.test(f)).join(", ") || "none defined"} />
        <Stat label="Tools" icon={<Wrench size={14} />} value={s.tools} sub="functions and toolsets agents can call" />
        <Stat label="Direct model calls" icon={<Cpu size={14} />} value={s.model_calls} sub={s.models.length ? `${s.models.length} model${s.models.length > 1 ? "s" : ""}: ${s.models.slice(0, 2).join(", ")}${s.models.length > 2 ? "…" : ""}` : "no literal model names"} />
      </div>

      {hasGraph && (
        <div className="card ai-arch">
          <div className="card-head"><h2><Network size={16} />Agent architecture</h2>
            <div className="right faint" style={{ fontSize: 12.5 }}>click a box for details · double-click to open its code</div>
          </div>
          <div className="ai-toolbar">
            <div className="search" style={{ width: 240 }}>
              <Search size={14} />
              <input className="input" style={{ width: "100%", height: 30 }} placeholder="Find an agent, tool or model" aria-label="Find a box"
                value={find} onChange={(e) => setFind(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && matches[0]) pick(matches[0].id); }} />
              {find && matches.length > 0 && (
                <div className="popover" style={{ top: 36, left: 0, width: 280, padding: 4 }}>
                  {matches.map((m) => {
                    const I = KIND_ICON[m.kind] ?? Bot;
                    return <button key={m.id} className="popover-item" onClick={() => pick(m.id)}>
                      <I size={14} style={{ color: KIND_STYLE[m.kind]?.color }} /><span className="grow truncate">{m.label}</span>
                      <span className="faint" style={{ fontSize: 12 }}>{kindName(m.kind)}</span></button>;
                  })}
                </div>
              )}
            </div>
            {roots.length > 0 && (
              <label className="row" style={{ gap: 6, fontSize: 13 }}>
                <span className="faint">Start from</span>
                <select className="select" style={{ height: 30, fontSize: 13, maxWidth: 260 }} value={root}
                  onChange={(e) => { setRoot(e.target.value); setFocusOn(null); setSel(null); }} aria-label="Start from">
                  <option value="">Everything</option>
                  {roots.map((r) => <option key={r.id} value={r.id}>{r.kind === "entry" ? "▶ " : ""}{r.label}</option>)}
                </select>
              </label>
            )}
            <div className="chips">
              {kinds.map(([k, n]) => {
                const I = KIND_ICON[k] ?? Bot;
                const off = hidden.has(k);
                return (
                  <button key={k} className={`chip ${off ? "" : "on"}`} aria-pressed={!off} style={{ height: 28, fontSize: 12.5 }} title={off ? "Show" : "Hide"}
                    onClick={() => setHidden((h) => { const x = new Set(h); if (off) x.delete(k); else x.add(k); return x; })}>
                    <I size={13} style={{ color: off ? undefined : KIND_STYLE[k]?.color }} />{kindName(k)}<span className="faint">{n}</span>
                  </button>
                );
              })}
            </div>
            <div style={{ marginLeft: "auto", position: "relative" }}>
              <button className={`btn sm ghost ${showMermaid ? "on" : ""}`} onClick={() => setShowMermaid(!showMermaid)} aria-expanded={showMermaid}>Mermaid</button>
              {showMermaid && (
                <div className="popover" style={{ top: 38, right: 0, width: 420 }}>
                  <div className="row" style={{ padding: "10px 12px", borderBottom: "1px solid var(--border)" }}>
                    <span className="grow" style={{ fontWeight: 500 }}>Mermaid export</span><CopyButton small text={rep.mermaid} />
                  </div>
                  <pre className="code" style={{ margin: 0, border: 0, borderRadius: 0, padding: 12, maxHeight: 340 }}>{rep.mermaid}</pre>
                </div>
              )}
            </div>
          </div>
          <div className="ai-arch-body">
            <div className="ai-arch-canvas">
              {focusOn && byId.get(focusOn) && (
                <div className="focus-banner">
                  <span>Showing <b>{byId.get(focusOn)!.label}</b> and what it connects to</span>
                  <button className="btn sm" onClick={() => setFocusOn(null)}>Show everything</button>
                </div>
              )}
              {visible.nodes.length > 0 ? (
                <Graph nodes={visible.nodes} edges={visible.edges} selected={sel} onSelect={setSel} dir="LR" minimap={false} focus={focus}
                  onOpen={(id) => { const n = byId.get(id); if (n?.files[0]) openCode(n.files[0].file, n.files[0].line); }} />
              ) : <div className="map-loading"><Empty icon={<Network size={20} />} title="Nothing to draw">Every kind is hidden.</Empty></div>}
            </div>
            <aside className="ai-arch-side">
              {node ? (
                <Inspector d={details} loading={detailsLoading} onSelect={pick} onOpenCode={openCode}
                  onFinding={(f) => go("review", { f })} onAsk={(q) => go("chat", { q })}
                  onFocus={() => setFocusOn(focusOn === node.id ? null : node.id)} focused={focusOn === node.id}
                  extra={roots.some((r) => r.id === node.id) || node.kind === "entry" || node.kind === "agent" || node.kind === "workflow" ? (
                    <button className={`btn sm ${root === node.id ? "on" : ""}`} aria-pressed={root === node.id}
                      title="Show everything this box starts or delegates to, down to tools and models"
                      onClick={() => { setRoot(root === node.id ? "" : node.id); setFocusOn(null); }}>
                      <Network size={13} />{root === node.id ? "Show everything" : "Show its tree"}
                    </button>) : undefined} />
              ) : (
                <div className="col" style={{ gap: 12 }}>
                  <div className="muted" style={{ fontSize: 13 }}>
                    Click any box to see what it is, where it is defined, what it connects to, the suggestions in its code and the code itself.
                  </div>
                  <div className="muted" style={{ fontSize: 13 }}>
                    Arrows read: an entry point <b>runs</b> an agent, an agent <b>delegates</b> to sub-agents or hands off, uses a <b>tool</b> or another agent <b>as tool</b>, and talks to a <b>model</b>. Functions that call a model directly <b>prompt</b> it.
                  </div>
                  {rep.agents.length === 0 && <div className="faint" style={{ fontSize: 12.5 }}>No agents found; the graph shows the functions that call a model directly.</div>}
                </div>
              )}
            </aside>
          </div>
        </div>
      )}
      <div className="card">
        <div className="card-head"><h2><FileCode2 size={16} />Files that use AI</h2>
          <div className="right">
            <Tabs<Filter> value={filter} onChange={setFilter} options={[
              { value: "all", label: <>All <span className="faint">{rep.files.length}</span></> },
              { value: "model", label: <>Need a model <span className="faint">{s.need_model}</span></> },
              ...(["runs agents", "tools", "calls AI code"] as AiRole[]).filter((r) => counts(r)).map((r) => ({ value: r as Filter, label: <>{ROLE[r].label} <span className="faint">{counts(r)}</span></> })),
            ]} />
          </div>
        </div>
        <table className="table">
          <thead><tr><th>File</th><th>What it does</th><th>Uses</th><th style={{ width: 90 }}></th></tr></thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.file} className="click" onClick={() => openCode(f.file, 1)}>
                <td>
                  <div className="mono" style={{ fontSize: 12.5, wordBreak: "break-all" }}>{f.file}</div>
                  {f.unused && <div className="row faint" style={{ fontSize: 12, marginTop: 4, gap: 5 }} title="Hidden on the Map"><EyeOff size={12} />not loaded by the running system: {f.unused}</div>}
                </td>
                <td>
                  <div className="row wrap" style={{ gap: 5 }}>
                    {f.roles.map((r) => <span key={r} className={`badge ${ROLE[r].badge}`} title={ROLE[r].help}>{ROLE[r].label}</span>)}
                  </div>
                  <div className="faint" style={{ fontSize: 12.5, marginTop: 6 }}>
                    {[
                      f.agents.length ? `agents: ${f.agents.join(", ")}` : "",
                      f.model_calls ? `${f.model_calls} model call${f.model_calls > 1 ? "s" : ""}` : "",
                      f.tools.length ? `tools: ${f.tools.join(", ")}` : "",
                      f.via.length ? `calls ${f.via.slice(0, 3).join(", ")}${f.via.length > 3 ? "…" : ""}` : "",
                    ].filter(Boolean).join(" · ")}
                  </div>
                </td>
                <td><div className="row wrap" style={{ gap: 5 }}>{f.sdks.map((x) => <span key={x} className="badge outline">{x}</span>)}</div></td>
                <td style={{ textAlign: "right" }}>{f.findings > 0 && <span className="badge accent">{f.findings} finding{f.findings > 1 ? "s" : ""}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rep.model_calls.length > 0 && (
        <div className="card">
          <div className="card-head"><h2><Cpu size={16} />Direct model calls</h2></div>
          <table className="table">
            <thead><tr><th>Where</th><th>Call</th><th>Provider</th><th>Model</th></tr></thead>
            <tbody>
              {rep.model_calls.map((c, i) => (
                <tr key={i} className="click" onClick={() => openCode(c.file, c.line)}>
                  <td className="mono" style={{ fontSize: 12.5 }}>{c.function} <span className="faint">{c.file}:{c.line}</span></td>
                  <td className="mono" style={{ fontSize: 12.5 }}>{c.api}</td>
                  <td>{c.provider}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>{c.model || <span className="faint">set at runtime</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
