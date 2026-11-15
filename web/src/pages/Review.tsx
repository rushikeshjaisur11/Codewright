import { useEffect, useMemo, useState } from "react";
import {
  BellOff, BookOpen, Check, ClipboardCopy, Code2, Download, FileDiff, Flame, GitBranch, Lightbulb, Play, Ruler,
  Search, ShieldAlert, ShieldCheck, ThumbsDown, ThumbsUp, TriangleAlert, Wand2,
} from "lucide-react";
import { useApp } from "../App";
import { api, Finding } from "../api";
import { CodeView, CopyButton, DiffView, Empty, Modal, ProofBadge, Source, Spinner, Tabs, intentLabel, useToast } from "../ui";

type ListTab = "shown" | "dropped" | "muted";

export default function Review() {
  const { data, wid, reload, params, go, openCode, runReview } = useApp();
  const toast = useToast();
  const r = data?.result;
  const [tab, setTab] = useState<ListTab>("shown");
  const [query, setQuery] = useState("");
  const [intent, setIntent] = useState("");
  const [proof, setProof] = useState("");
  const [status, setStatus] = useState("");
  const [hotOnly, setHotOnly] = useState(false);
  const [rejecting, setRejecting] = useState<Finding | null>(null);
  const [reason, setReason] = useState("");
  const [applying, setApplying] = useState<Finding | null>(null);
  const [prMode, setPrMode] = useState(false);
  const [base, setBase] = useState("origin/main");
  const [report, setReport] = useState<string | null>(null);

  const list = useMemo(() => {
    if (!r) return [];
    const src = tab === "shown" ? r.findings : tab === "dropped" ? r.rejected : r.muted;
    const ql = query.toLowerCase();
    return src.filter((f) =>
      (!ql || `${f.title} ${f.file} ${f.function} ${f.evidence}`.toLowerCase().includes(ql)) &&
      (!intent || f.intent === intent) && (!proof || f.proof === proof) &&
      (!status || (status === "open" ? !f.status : f.status === status)) && (!hotOnly || f.hot_path));
  }, [r, tab, query, intent, proof, status, hotOnly]);

  const selectedId = params.get("f");
  const selected = list.find((f) => f.id === selectedId) ?? list[0];
  const intents = useMemo(() => [...new Set((r?.findings ?? []).map((f) => f.intent))].sort(), [r]);

  useEffect(() => { setReason(""); }, [rejecting]);

  if (!data) return <Empty icon={<Spinner />} title="Loading" />;
  if (!r) return <Empty icon={data.running ? <Spinner /> : <Play size={22} />} title={data.running ? "Reviewing…" : "No review yet"}>
    {data.running ? "Findings appear here when the run finishes." : <button className="btn primary" onClick={() => runReview(true)}><Play size={14} />Run review</button>}
  </Empty>;

  async function verdict(f: Finding, s: string, why = "") {
    if (!wid) return;
    try {
      await api.feedback(wid, f.id, f.status === s ? "clear" : s, why);
      await reload();
      toast(f.status === s ? "Feedback cleared" : s === "accepted" ? "Marked accepted: similar findings rank higher" : s === "rejected" ? "Marked rejected: similar findings rank lower" : "Muted");
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function muteIntent(f: Finding) {
    if (!wid) return;
    await api.mute(wid, f.intent);
    toast(`Muted “${intentLabel(f.intent)}” for this repo. Undo in Settings.`);
    await runReview(false);
  }

  async function applyPatch(f: Finding) {
    if (!wid) return;
    try {
      const res = await api.apply(wid, f.id);
      toast(`Patch applied to ${res.file}. Review the change and commit it yourself.`);
      setApplying(null);
      await runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function showReport(kind: "md" | "pr") {
    if (!wid) return;
    setReport(kind === "md" ? await api.reportMd(wid) : await api.prComment(wid));
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Review</h1>
          <p>Ranked “instead of X, use Y” suggestions, checked against your installed versions{r.base ? <> · PR mode: changes since <code>{r.base}</code></> : null}.</p>
        </div>
        <div className="actions">
          <button className={`btn ${prMode ? "on" : ""}`} onClick={() => setPrMode(!prMode)}><GitBranch size={14} />PR mode</button>
          <button className="btn" onClick={() => showReport("pr")}><ClipboardCopy size={14} />PR comment</button>
          <button className="btn" onClick={() => showReport("md")}><Download size={14} />Report</button>
        </div>
      </div>

      {prMode && (
        <div className="card card-pad row wrap" style={{ marginBottom: 14 }}>
          <GitBranch size={16} className="muted" />
          <span>Review only code changed since</span>
          <input className="input mono" style={{ width: 200 }} value={base} onChange={(e) => setBase(e.target.value)} />
          <button className="btn primary" disabled={data.running} onClick={() => runReview(true, base)}><Play size={14} />Review changes</button>
          <button className="btn ghost" disabled={data.running} onClick={() => runReview(true, "")}>Whole repo</button>
          <span className="faint" style={{ fontSize: 12.5 }}>Same as <code>codewright review --base {base} -f pr</code> in CI.</span>
        </div>
      )}

      <div className="filters">
        <Tabs<ListTab> value={tab} onChange={setTab} options={[
          { value: "shown", label: <>Shown <span className="badge">{r.findings.length}</span></> },
          { value: "dropped", label: <>Dropped by critic <span className="badge">{r.rejected.length}</span></> },
          { value: "muted", label: <>Muted <span className="badge">{r.muted.length}</span></> },
        ]} />
        <div className="row" style={{ position: "relative" }}>
          <Search size={14} style={{ position: "absolute", left: 10, color: "var(--text-3)" }} />
          <input className="input" style={{ paddingLeft: 30 }} placeholder="Search findings" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <select className="select" value={intent} onChange={(e) => setIntent(e.target.value)}>
          <option value="">All intents</option>
          {intents.map((i) => <option key={i} value={i}>{intentLabel(i)}</option>)}
        </select>
        <select className="select" value={proof} onChange={(e) => setProof(e.target.value)}>
          <option value="">Any proof</option><option value="verified">Verified</option><option value="unverified">Unverified</option>
        </select>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option><option value="open">Open</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option>
        </select>
        <button className={`btn ${hotOnly ? "on" : ""}`} style={hotOnly ? { borderColor: "var(--red)", color: "var(--red)" } : undefined} onClick={() => setHotOnly(!hotOnly)}><Flame size={14} />Hot path</button>
      </div>

      {list.length === 0 ? (
        <div className="card"><Empty icon={<ShieldCheck size={22} />} title={tab === "shown" ? "Nothing to show" : "Empty"}>
          {tab === "shown" ? "No findings match these filters." : tab === "dropped" ? "The critic didn't drop anything: every candidate named a file, an API version, a gain, a risk and a source." : "No muted findings."}
        </Empty></div>
      ) : (
        <div className="review-layout">
          <div className="flist">
            {list.map((f) => (
              <button key={f.id} className={`fitem ${selected?.id === f.id ? "active" : ""} ${f.status === "rejected" ? "dim" : ""}`}
                onClick={() => go("review", { f: f.id })}>
                <div className="t">{f.title}</div>
                <div className="loc">{f.file}:{f.line} · {f.function}</div>
                <div className="meta">
                  {tab === "dropped" ? <span className="badge red"><ShieldAlert size={11} />{f.rejected_reason}</span> : <ProofBadge proof={f.proof} />}
                  {f.hot_path && <span className="badge red"><Flame size={11} />hot</span>}
                  {f.patch && <span className="badge blue"><Wand2 size={11} />patch</span>}
                  {f.status === "accepted" && <span className="badge green"><Check size={11} />accepted</span>}
                  {f.status === "rejected" && <span className="badge"><ThumbsDown size={11} />rejected</span>}
                  {tab === "shown" && <span className="score">{f.score.toFixed(2)}</span>}
                </div>
              </button>
            ))}
          </div>

          {selected && <Detail f={selected} dropped={tab === "dropped"}
            onAccept={() => verdict(selected, "accepted")} onReject={() => selected.status === "rejected" ? verdict(selected, "rejected") : setRejecting(selected)}
            onMute={() => muteIntent(selected)} onApply={() => setApplying(selected)}
            onCode={() => openCode(selected.file, selected.line, selected.span)} />}
        </div>
      )}

      {rejecting && (
        <Modal title="Reject this suggestion" onClose={() => setRejecting(null)}
          footer={<><button className="btn" onClick={() => setRejecting(null)}>Cancel</button>
            <button className="btn danger on" onClick={() => { verdict(rejecting, "rejected", reason); setRejecting(null); }}><ThumbsDown size={14} />Reject</button></>}>
          <p className="muted" style={{ marginTop: 0 }}>Why? Codewright learns from this: findings of the same intent rank lower in this repo.</p>
          <div className="chips" style={{ marginBottom: 10 }}>
            {["Not worth the churn", "Wrong for our scale", "We can't add dependencies", "Behaviour must stay the same", "Wrong API"].map((x) => (
              <button key={x} className={`chip ${reason === x ? "on" : ""}`} onClick={() => setReason(x)}>{x}</button>
            ))}
          </div>
          <textarea className="textarea" rows={3} placeholder="Or write a reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Modal>
      )}

      {applying && (
        <Modal title="Apply patch to the working tree?" onClose={() => setApplying(null)}
          footer={<><button className="btn" onClick={() => setApplying(null)}>Cancel</button>
            <button className="btn primary" onClick={() => applyPatch(applying)}><Wand2 size={14} />Apply patch</button></>}>
          <p className="muted" style={{ marginTop: 0 }}>This edits <code>{applying.file}</code> in <code>{data.root}</code>. Nothing is committed or pushed; review the diff and commit it yourself.</p>
          {applying.proof !== "verified" && <div className="callout amber" style={{ fontSize: 13 }}>This patch isn't proven by tests yet. {applying.proof_detail}</div>}
        </Modal>
      )}

      {report != null && (
        <Modal title="Report" onClose={() => setReport(null)} footer={<CopyButton text={report} label="Copy Markdown" />}>
          <pre className="code" style={{ maxHeight: 420, padding: 12, whiteSpace: "pre-wrap", margin: 0 }}>{report}</pre>
        </Modal>
      )}
    </div>
  );
}

function Detail({ f, dropped, onAccept, onReject, onMute, onApply, onCode }: {
  f: Finding; dropped: boolean; onAccept: () => void; onReject: () => void; onMute: () => void; onApply: () => void; onCode: () => void;
}) {
  const [view, setView] = useState<"after" | "before">("after");
  useEffect(() => setView("after"), [f.id]);
  return (
    <div className="card">
      <div className="detail-head">
        <div className="row wrap" style={{ marginBottom: 8 }}>
          <span className="badge accent">{intentLabel(f.intent)}</span>
          {!dropped && <ProofBadge proof={f.proof} />}
          {f.hot_path && <span className="badge red"><Flame size={11} />Hot path</span>}
          <span className={`badge ${f.symbol_check === "ok" ? "green" : f.symbol_check === "missing" ? "red" : "outline"}`}>
            API check: {f.symbol_check}
          </span>
          <span className="badge outline">confidence {Math.round(f.confidence * 100)}%</span>
        </div>
        <h2>{f.title}</h2>
        <div className="row wrap" style={{ marginTop: 8 }}>
          <button className="btn sm ghost mono" onClick={onCode}><Code2 size={13} />{f.file}:{f.line}</button>
          <span className="faint mono" style={{ fontSize: 12 }}>in {f.function}</span>
        </div>
        {!dropped && (
          <div className="row wrap" style={{ marginTop: 14 }}>
            <button className={`btn success ${f.status === "accepted" ? "on" : ""}`} onClick={onAccept}><ThumbsUp size={14} />{f.status === "accepted" ? "Accepted" : "Accept"}</button>
            <button className={`btn danger ${f.status === "rejected" ? "on" : ""}`} onClick={onReject}><ThumbsDown size={14} />{f.status === "rejected" ? "Rejected" : "Reject"}</button>
            {f.patch && <button className="btn" onClick={onApply}><Wand2 size={14} />Apply patch</button>}
            {f.patch && <CopyButton text={f.patch} label="Copy patch" />}
            <button className="btn ghost" onClick={onMute} title="Hide this intent in this repo"><BellOff size={14} />Mute intent</button>
          </div>
        )}
      </div>
      <div className="detail-body">
        {dropped && <div className="callout red"><b>Dropped by the critic:</b> {f.rejected_reason}. It is shown here only for transparency.</div>}
        <div>
          <div className="section-title"><Search size={13} />What we found</div>
          <div>{f.evidence}</div>
        </div>
        <div className="callout">
          <div className="section-title" style={{ color: "var(--accent)" }}><Lightbulb size={13} />Instead, use</div>
          <div>{f.suggestion}</div>
          <div className="mono" style={{ marginTop: 8, fontSize: 12.5, color: "var(--text-2)" }}>{f.api}</div>
        </div>
        <div className="kv">
          <div className="kv-item good"><div className="k" style={{ color: "var(--green)" }}><ThumbsUp size={12} />Gain</div>{f.gain}</div>
          <div className="kv-item warn"><div className="k" style={{ color: "var(--amber)" }}><TriangleAlert size={12} />Risk · effort {f.effort}/5</div>{f.risk}</div>
          <div className="kv-item bad"><div className="k" style={{ color: "var(--red)" }}><ShieldAlert size={12} />When not to</div>{f.when_not}</div>
          <div className="kv-item"><div className="k"><Ruler size={12} />Scale threshold</div>{f.threshold}</div>
        </div>
        <div>
          <div className="section-title"><ShieldCheck size={13} />Proof</div>
          <div className={`callout ${f.proof === "verified" ? "green" : f.proof === "broke-tests" ? "red" : "amber"}`} style={{ whiteSpace: "pre-wrap" }}>
            {f.proof_detail || "Not run."}
          </div>
        </div>
        {f.patch ? (
          <div>
            <div className="row" style={{ marginBottom: 8 }}>
              <div className="section-title" style={{ margin: 0 }}><FileDiff size={13} />Patch</div>
              <div style={{ marginLeft: "auto" }}>
                <Tabs value={view} onChange={setView} options={[{ value: "after", label: "Diff" }, { value: "before", label: "Code today" }]} />
              </div>
            </div>
            {view === "after" ? <DiffView patch={f.patch} /> : <CodeView text={f.code_today} start={f.span[0]} highlightLines={[f.line, f.line]} />}
          </div>
        ) : (
          <div className="grid" style={{ gridTemplateColumns: f.snippet ? "1fr 1fr" : "1fr" }}>
            <div>
              <div className="section-title"><Code2 size={13} />Code today</div>
              <CodeView text={f.code_today} start={f.span[0]} highlightLines={[f.line, f.line]} />
            </div>
            {f.snippet && (
              <div>
                <div className="section-title"><Lightbulb size={13} />Suggested shape</div>
                <CodeView text={f.snippet} lang={f.snippet_lang} />
              </div>
            )}
          </div>
        )}
        <div>
          <div className="section-title"><BookOpen size={13} />Sources</div>
          <div className="col sources" style={{ gap: 4 }}>{f.sources.map((s) => <Source key={s} url={s} />)}</div>
        </div>
      </div>
    </div>
  );
}
