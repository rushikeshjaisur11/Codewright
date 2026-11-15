import { useEffect, useMemo, useRef, useState } from "react";
import {
  Boxes, Calculator, Check, Database, Download, FileText, GitFork, Network, PenTool, RotateCcw, Save, SendHorizontal,
  Sparkles, Star, User,
} from "lucide-react";
import { useApp } from "../App";
import { api, DesignState, Proposal } from "../api";
import Graph from "../Graph";
import { CopyButton, Empty, KIND_STYLE, Modal, Spinner, useToast } from "../ui";

function fmt(v: unknown): string {
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  if (v == null) return "skip";
  if (typeof v === "number") return v.toLocaleString();
  return String(v);
}

export default function Design() {
  const { wid, data } = useApp();
  const toast = useToast();
  const [state, setState] = useState<DesignState | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [text, setText] = useState("");
  const [multi, setMulti] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [cid, setCid] = useState<string>("");
  const [doc, setDoc] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [showInterview, setShowInterview] = useState(false);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let saved: string | null = null;
    try { saved = sessionStorage.getItem("cw-design"); } catch { /* ignore */ }
    const start = () => api.newDesign().then((s) => { setState(s); try { sessionStorage.setItem("cw-design", s.session.id); } catch { /* ignore */ } });
    if (saved) {
      fetch(`/api/design/${saved}`).then((r) => (r.ok ? r.json() : Promise.reject())).then((s: DesignState) => {
        setState(s);
        if (s.proposal) { setProposal(s.proposal); setCid(s.proposal.recommended); }
      }).catch(start);
    } else start();
  }, []);

  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" }); }, [state]);
  useEffect(() => { setMulti([]); setText(""); }, [state?.question?.id]);

  const q = state?.question;
  const cand = useMemo(() => proposal?.candidates.find((c) => c.id === cid) ?? proposal?.candidates[0], [proposal, cid]);

  async function answer(value: unknown) {
    if (!state || !q) return;
    setBusy(true);
    try {
      const s = await api.answer(state.session.id, q.id, value);
      setState(s);
      if (s.done) {
        const p = await api.propose(s.session.id, wid ?? "");
        setProposal(p);
        setCid(p.recommended);
        setShowInterview(false);
        document.querySelector(".content")?.scrollTo({ top: 0 });
      }
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function restart() {
    const s = await api.newDesign();
    try { sessionStorage.setItem("cw-design", s.session.id); } catch { /* ignore */ }
    setState(s); setProposal(null); setCid("");
  }

  async function applyAssumptions(reset?: string) {
    if (!state) return;
    const overrides: Record<string, number | null> = {};
    if (reset) overrides[reset] = null;
    else for (const [k, v] of Object.entries(edits)) if (v.trim() !== "" && !isNaN(Number(v))) overrides[k] = Number(v);
    setBusy(true);
    try {
      const p = await api.assumptions(state.session.id, overrides);
      setProposal(p);
      setEdits({});
      if (!p.candidates.some((c) => c.id === cid)) setCid(p.recommended);
      toast("Capacity recalculated");
    } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  }

  async function save() {
    if (!state || !wid || !cand) return;
    try {
      const r = await api.saveDesign(state.session.id, wid, cand.id);
      toast(`Saved ${r.saved} in ${data?.name}. Map and Review now check this repo for drift.`);
    } catch (e) { toast((e as Error).message, "error"); }
  }

  if (!state) return <Empty icon={<Spinner />} title="Starting Design mode" />;

  const answered = state.questions.filter((x) => x.id in state.session.answers);

  return (
    <div className="grid" style={{ gridTemplateColumns: proposal ? "minmax(0, 1fr)" : "minmax(0, 760px)", justifyContent: "center", alignItems: "start" }}>
      {proposal && !showInterview && (
        <div className="card card-pad row wrap" style={{ gap: 10 }}>
          <PenTool size={16} color="var(--accent)" />
          <span className="grow muted" style={{ fontSize: 13 }}>
            {fmt(state.session.answers.users)} daily users · {fmt(state.session.answers.data_gb)} GB · p95 {fmt(state.session.answers.latency_ms)} ms ·{" "}
            {fmt(state.session.answers.availability)} · {fmt(state.session.answers.cloud)} · team of {fmt(state.session.answers.team)} · {fmt(state.session.answers.workload)}
          </span>
          <button className="btn sm" onClick={() => setShowInterview(true)}>Show interview</button>
          <button className="btn sm ghost" onClick={restart}><RotateCcw size={13} />New design</button>
        </div>
      )}
      {/* interview */}
      {(!proposal || showInterview) && <div className="card" style={{ display: "flex", flexDirection: "column" }}>
        <div className="card-head">
          <h3><PenTool size={16} />Design interview</h3>
          <div className="right">
            {proposal && <button className="btn sm" onClick={() => setShowInterview(false)}>Hide</button>}
            <button className="btn sm ghost" onClick={restart}><RotateCcw size={13} />Restart</button>
          </div>
        </div>
        <div style={{ padding: "10px 16px 0" }}>
          <div className="steps">{state.questions.map((x) => <i key={x.id} className={x.id in state.session.answers ? "on" : ""} />)}</div>
          <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>{state.progress.answered} of {state.progress.total} answered</div>
        </div>
        <div ref={log} style={{ overflow: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 12, flex: 1, maxHeight: proposal ? 420 : undefined }}>
          {!proposal && answered.length === 0 && (
            <div className="callout" style={{ fontSize: 13 }}>
              Describe a new system and Codewright drafts two or three candidate architectures with capacity math in code,
              exact library versions, decision records, a diagram and a starter repo.
            </div>
          )}
          {answered.map((x) => (
            <div key={x.id} className="col" style={{ gap: 6 }}>
              <div className="msg bot"><div className="avatar" style={{ width: 24, height: 24 }}><Boxes size={12} /></div><div className="bubble muted" style={{ fontSize: 13 }}>{x.text}</div></div>
              <div className="msg user" style={{ justifyContent: "flex-end" }}><div className="bubble" style={{ fontSize: 13 }}>{fmt(state.session.answers[x.id])}</div><div className="avatar" style={{ width: 24, height: 24 }}><User size={12} /></div></div>
            </div>
          ))}
          {q && (
            <div className="msg bot"><div className="avatar"><Boxes size={15} /></div>
              <div className="bubble"><div style={{ fontWeight: 560 }}>{q.text}</div>{q.hint && <div className="faint" style={{ fontSize: 12.5, marginTop: 2 }}>{q.hint}</div>}</div>
            </div>
          )}
          {state.done && !proposal && <div className="row muted"><Spinner />Drafting candidate architectures…</div>}
        </div>
        {q && (
          <div style={{ padding: 14, borderTop: "1px solid var(--border)" }}>
            {q.kind === "choice" && (
              <div className="chips">{q.options.map((o) => <button key={o} className="chip" disabled={busy} onClick={() => answer(o)}>{o}</button>)}</div>
            )}
            {q.kind === "multi" && (
              <div className="col">
                <div className="chips">{q.options.map((o) => (
                  <button key={o} className={`chip ${multi.includes(o) ? "on" : ""}`}
                    onClick={() => setMulti(o === "none" ? ["none"] : multi.includes(o) ? multi.filter((x) => x !== o) : [...multi.filter((x) => x !== "none"), o])}>
                    {multi.includes(o) && <Check size={12} />} {o}
                  </button>
                ))}</div>
                <div className="row"><button className="btn primary" disabled={busy} onClick={() => answer(multi)}>{multi.length ? "Continue" : "None of these"}</button></div>
              </div>
            )}
            {(q.kind === "text" || q.kind === "number" || q.kind === "size") && (
              <div className="chat-box" style={{ boxShadow: "none" }}>
                <textarea rows={q.kind === "text" && q.id === "idea" ? 3 : 1} value={text} autoFocus
                  placeholder={q.default != null ? `Default: ${fmt(q.default)}` : "Type your answer"}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); answer(text || (q.default != null ? String(q.default) : "")); } }} />
                <button className="btn primary icon" disabled={busy || (!text.trim() && q.default == null && q.kind !== "number")}
                  onClick={() => answer(text || (q.default != null ? String(q.default) : "skip"))}><SendHorizontal size={16} /></button>
              </div>
            )}
          </div>
        )}
      </div>}

      {/* proposal */}
      {proposal && cand && (
        <div className="col" style={{ gap: 16 }}>
          <div className="page-head" style={{ marginBottom: 0 }}>
            <div>
              <h1>{String(state.session.answers.name ?? "Design")}</h1>
              <p>{String(state.session.answers.idea ?? "")}</p>
            </div>
            <div className="actions">
              <button className="btn" onClick={async () => setDoc(await api.designMd(state.session.id, cand.id))}><FileText size={14} />Design doc</button>
              <a className="btn" href={api.starterUrl(state.session.id, cand.id)}><Download size={14} />Starter repo</a>
              {wid && <button className="btn primary" onClick={save}><Save size={14} />Save as intended architecture</button>}
            </div>
          </div>

          <div className="design-grid">
            {proposal.candidates.map((c) => (
              <div key={c.id} className={`card cand ${c.id === cand.id ? "active" : ""}`} onClick={() => setCid(c.id)}>
                <div className="row">
                  <h3 className="grow">{c.name}</h3>
                  {c.recommended && <span className="badge green"><Star size={11} />Recommended</span>}
                </div>
                <div className="muted" style={{ fontSize: 13, marginTop: 6 }}>{c.summary}</div>
                <div className="fit"><span className="faint" style={{ fontSize: 12 }}>Fit</span>
                  <div className="meter"><span style={{ width: `${c.fit}%`, background: c.fit >= 75 ? "var(--green)" : c.fit >= 50 ? "var(--amber)" : "var(--red)" }} /></div>
                  <span className="mono" style={{ fontSize: 12 }}>{c.fit}</span></div>
                <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>{c.components.length} components</div>
              </div>
            ))}
          </div>

          <div className="card" style={{ height: 500, overflow: "hidden" }}>
            <div className="card-head"><h3><Network size={16} />{cand.name}</h3><div className="right faint" style={{ fontSize: 12.5 }}>click a candidate above to compare</div></div>
            <div style={{ height: 448 }}>
              <Graph nodes={cand.components.map((x) => ({ id: x.id, label: x.label, kind: x.kind, tech: x.tech }))}
                edges={cand.edges} />
            </div>
          </div>

          <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.25fr) minmax(0, 1fr)" }}>
            <div className="card">
              <div className="card-head"><h3><Boxes size={16} />Components</h3></div>
              <table className="table">
                <tbody>
                  {cand.components.map((x) => (
                    <tr key={x.id}>
                      <td style={{ width: 150 }}>
                        <div style={{ fontWeight: 600 }}>{x.label}</div>
                        <span className="badge" style={{ background: KIND_STYLE[x.kind]?.soft, color: KIND_STYLE[x.kind]?.color, marginTop: 4 }}>{KIND_STYLE[x.kind]?.label ?? x.kind}</span>
                      </td>
                      <td><div>{x.tech}</div><div className="faint" style={{ fontSize: 12.5 }}>{x.why}</div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="col" style={{ gap: 16 }}>
              <div className="card card-pad">
                <div className="section-title">Trade-offs</div>
                <ul className="list-plain">
                  {cand.pros.map((p) => <li key={p} className="pro">{p}</li>)}
                  {cand.cons.map((p) => <li key={p} className="con">{p}</li>)}
                </ul>
              </div>
              <div className="card card-pad">
                <div className="section-title"><GitFork size={13} />Revisit when</div>
                <ul className="list-plain">{cand.flips_when.map((p) => <li key={p}>{p}</li>)}</ul>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3><Calculator size={16} />Capacity math</h3>
              <div className="right">
                {Object.keys(edits).length > 0 && <button className="btn sm primary" disabled={busy} onClick={() => applyAssumptions()}>Recalculate</button>}
              </div>
            </div>
            <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 0 }}>
              <table className="table" style={{ borderRight: "1px solid var(--border)" }}>
                <thead><tr><th>Metric</th><th>Value</th><th>How</th></tr></thead>
                <tbody>
                  {proposal.capacity.rows.map((r) => (
                    <tr key={r.metric}><td>{r.metric}</td><td className="mono" style={{ fontWeight: 600, whiteSpace: "nowrap" }}>{r.value}</td><td className="faint" style={{ fontSize: 12.5 }}>{r.how}</td></tr>
                  ))}
                </tbody>
              </table>
              <table className="table">
                <thead><tr><th>Assumption</th><th style={{ width: 110 }}>Value</th></tr></thead>
                <tbody>
                  {proposal.capacity.assumptions.map((a) => (
                    <tr key={a.key}>
                      <td><div className="mono" style={{ fontSize: 12 }}>{a.key}</div><div className="faint" style={{ fontSize: 12 }}>{a.meaning}</div></td>
                      <td>
                        <div className="row" style={{ gap: 4 }}>
                          <input className="input mono" style={{ width: 80, padding: "4px 6px", fontSize: 12, borderColor: a.overridden ? "var(--accent)" : undefined }}
                            value={edits[a.key] ?? String(a.value)} onChange={(e) => setEdits({ ...edits, [a.key]: e.target.value })}
                            onKeyDown={(e) => e.key === "Enter" && applyAssumptions()} />
                          {a.overridden && <button className="btn sm ghost icon" title={`Reset to ${a.default}`} onClick={() => applyAssumptions(a.key)}><RotateCcw size={12} /></button>}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h3><FileText size={16} />Decision records</h3><div className="right faint" style={{ fontSize: 12.5 }}>for the recommended design</div></div>
            <div className="card-pad col" style={{ gap: 8 }}>
              {proposal.decisions.map((d) => (
                <details key={d.id} className="adr">
                  <summary><span className="mono faint">ADR {d.id}</span><b>{d.title}</b></summary>
                  <div className="body">
                    <div><div className="section-title">Context</div>{d.context}</div>
                    <div><div className="section-title">Decision</div>{d.decision}</div>
                    {d.alternatives.length > 0 && <div><div className="section-title">Alternatives</div><ul>{d.alternatives.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                    <div><div className="section-title">Consequences</div><ul>{d.consequences.map((x) => <li key={x}>{x}</li>)}</ul></div>
                    <div><div className="section-title">Revisit when</div><ul>{d.revisit_when.map((x) => <li key={x}>{x}</li>)}</ul></div>
                  </div>
                </details>
              ))}
            </div>
          </div>

          <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)" }}>
            <div className="card">
              <div className="card-head"><h3><Database size={16} />Data model</h3></div>
              <div className="card-pad col" style={{ gap: 10 }}>
                {proposal.data_model.entities.map((e) => (
                  <div key={e.name}><b>{e.name}</b><div className="mono muted" style={{ fontSize: 12 }}>{e.fields.join(" · ")}</div></div>
                ))}
                <div className="faint" style={{ fontSize: 12 }}><Sparkles size={11} /> {proposal.data_model.source}</div>
              </div>
            </div>
            <div className="card">
              <div className="card-head"><h3><Network size={16} />API outline</h3></div>
              <table className="table">
                <tbody>
                  {proposal.data_model.endpoints.map((e) => (
                    <tr key={e.method + e.path}><td><span className={`badge ${e.method === "GET" ? "blue" : "green"}`}>{e.method}</span></td><td className="mono">{e.path}</td><td className="muted">{e.summary}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h3>Pinned versions</h3><div className="right faint" style={{ fontSize: 12.5 }}>latest from PyPI at design time</div></div>
            <div className="card-pad chips">
              {Object.entries(proposal.versions).map(([k, v]) => (
                <span key={k} className="badge outline mono" title={proposal.version_source[k]}>{k}=={v}{proposal.version_source[k] === "fallback" ? " *" : ""}</span>
              ))}
            </div>
          </div>
        </div>
      )}

      {doc != null && (
        <Modal title="Design doc" onClose={() => setDoc(null)} footer={<CopyButton text={doc} label="Copy Markdown" />}>
          <pre className="code" style={{ maxHeight: 460, padding: 12, whiteSpace: "pre-wrap", margin: 0 }}>{doc}</pre>
        </Modal>
      )}
    </div>
  );
}
