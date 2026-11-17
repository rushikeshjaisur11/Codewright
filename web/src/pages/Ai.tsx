import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, Bot, Code2, Cpu, EyeOff, FileCode2, Network, Sparkles, Wrench } from "lucide-react";
import { useApp } from "../App";
import { AiFile, AiReport, AiRole, api, MapNode } from "../api";
import Graph, { KIND_ICON } from "../Graph";
import { Empty, KIND_STYLE, Spinner, Stat, Tabs, useToast } from "../ui";

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
  const { wid, data, openCode, go } = useApp();
  const toast = useToast();
  const [rep, setRep] = useState<AiReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const has = !!data?.result;

  useEffect(() => {
    if (!wid || !has) return;
    setLoading(true);
    api.ai(wid).then(setRep).catch((e) => toast(e.message, "error")).finally(() => setLoading(false));
  }, [wid, has, data?.revision, toast]);

  const byId = useMemo(() => new Map((rep?.architecture.nodes ?? []).map((n) => [n.id, n])), [rep]);
  const node = sel ? byId.get(sel) ?? null : null;
  const links = useMemo(() => {
    const out: { n: MapNode; label: string; dir: "out" | "in" }[] = [];
    if (!rep || !sel) return out;
    for (const e of rep.architecture.edges) {
      if (e.source === sel && byId.get(e.target)) out.push({ n: byId.get(e.target)!, label: e.label, dir: "out" });
      if (e.target === sel && byId.get(e.source)) out.push({ n: byId.get(e.source)!, label: e.label, dir: "in" });
    }
    return out;
  }, [rep, sel, byId]);

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
  const NodeIcon = node ? KIND_ICON[node.kind] ?? Bot : Bot;

  return (
    <div className="grid page">
      <div className="page-head">
        <div>
          <h1>AI &amp; agents</h1>
          <p>The files that need a model to work, and how the agents fit together: who runs each agent, what it delegates to, and which tools and models it uses. Read from the code; no model is called.</p>
        </div>
      </div>

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
            <div className="right faint" style={{ fontSize: 12.5 }}>click a box for details · double-click to open the code</div>
          </div>
          <div className="ai-arch-body">
            <div className="ai-arch-canvas">
              <Graph nodes={rep.architecture.nodes} edges={rep.architecture.edges} selected={sel} onSelect={setSel} dir="LR" minimap={false}
                onOpen={(id) => { const n = byId.get(id); if (n?.files[0]) openCode(n.files[0].file, n.files[0].line); }} />
            </div>
            <aside className="ai-arch-side">
              {node ? (
                <>
                  <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
                    <span className="cw-node__icon" style={{ width: 34, height: 34, color: KIND_STYLE[node.kind]?.color, background: KIND_STYLE[node.kind]?.soft }}>
                      <NodeIcon size={17} />
                    </span>
                    <div className="grow" style={{ minWidth: 0 }}>
                      <div className="faint" style={{ fontSize: 12.5 }}>{node.kind === "entry" ? "Runs an agent" : KIND_STYLE[node.kind]?.label ?? node.kind}</div>
                      <h3 style={{ fontSize: 17, wordBreak: "break-word" }}>{node.label}</h3>
                      {node.detail && <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>{node.detail}</div>}
                    </div>
                  </div>
                  {node.model && <div style={{ fontSize: 13 }}><span className="faint">Model </span><span className="mono">{node.model}</span></div>}
                  {node.instruction && (
                    <div>
                      <div className="section-title">Instruction</div>
                      <div className="muted" style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{node.instruction}</div>
                    </div>
                  )}
                  {node.files[0] && (
                    <div className="row wrap">
                      <button className="btn sm" onClick={() => openCode(node.files[0].file, node.files[0].line)}><Code2 size={13} />Open code</button>
                      {node.findings > 0 && <button className="btn sm" onClick={() => go("review")}>{node.findings} finding{node.findings > 1 ? "s" : ""} in this file</button>}
                    </div>
                  )}
                  {links.length > 0 && (
                    <div>
                      <div className="section-title">Connections</div>
                      <div className="conn">
                        {links.map((l, i) => (
                          <button key={i} className="conn-row" onClick={() => setSel(l.n.id)}>
                            {l.dir === "out" ? <ArrowRight size={13} className="faint" /> : <ArrowLeft size={13} className="faint" />}
                            <span className="grow truncate">{l.n.label}</span><span className="faint" style={{ fontSize: 12 }}>{l.label}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <div className="col" style={{ gap: 12 }}>
                  <div className="muted" style={{ fontSize: 13 }}>
                    Arrows read: an entry point <b>runs</b> an agent, an agent <b>delegates</b> to sub-agents or hands off, uses a <b>tool</b> or another agent <b>as tool</b>, and talks to a <b>model</b>. Functions that call a model directly <b>prompt</b> it.
                  </div>
                  <div className="chips">
                    {(["entry", "agent", "workflow", "tool", "llm", "function"] as const).filter((k) => rep.architecture.nodes.some((n) => n.kind === k)).map((k) => {
                      const I = KIND_ICON[k] ?? Bot;
                      const n = rep.architecture.nodes.filter((x) => x.kind === k).length;
                      return <span key={k} className="chip on" style={{ height: 28, fontSize: 12.5, cursor: "default" }}>
                        <I size={13} style={{ color: KIND_STYLE[k]?.color }} />{k === "entry" ? "Runs agents" : k === "llm" ? "Model" : KIND_STYLE[k]?.label}<span className="faint">{n}</span></span>;
                    })}
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
