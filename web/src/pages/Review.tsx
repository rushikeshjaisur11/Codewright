import { useEffect, useMemo, useState } from "react";
import {
  BellOff, BookOpen, Check, ClipboardCopy, Code2, Download, FileDiff, Flame, Gauge, GitBranch, Lightbulb, Play, Ruler,
  Search, ShieldAlert, ShieldCheck, ThumbsDown, ThumbsUp, TriangleAlert, Wand2,
} from "lucide-react";
import { useApp } from "../App";
import { api, Finding } from "../api";
import { CodeView, CopyButton, DiffView, Empty, Inline, Modal, ProofBadge, Source, Spinner, Tabs, intentLabel, useToast } from "../ui";

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
  if (!r) return <Empty icon={data.running ? <Spinner /> : <Play size={20} />} title={data.running ? "Reviewing the repository" : "No review yet"}>
    {data.running ? "Findings appear here when the run finishes." : <button className="btn primary" onClick={() => runReview(true)}><Play size={14} />Run review</button>}
  </Empty>;

  async function verdict(f: Finding, s: string, why = "") {
    if (!wid) return;
    try {
      await api.feedback(wid, f.id, f.status === s ? "clear" : s, why);
      await reload();
      toast(f.status === s ? "Feedback cleared" : s === "accepted" ? "Accepted. Similar findings now rank higher here." : s === "rejected" ? "Rejected. Similar findings now rank lower here." : "Muted");
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function muteIntent(f: Finding) {
    if (!wid) return;
    await api.mute(wid, f.intent);
    toast(`${intentLabel(f.intent)} muted for this repo. Undo it in Settings.`);
    await runReview(false);
  }

  async function applyPatch(f: Finding) {
    if (!wid) return;
    try {
      const res = await api.apply(wid, f.id);
      toast(`Patch applied to ${res.file}. Check the change, then commit it yourself.`);
      setApplying(null);
      await runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function showReport(kind: "md" | "pr") {
    if (!wid) return;
    setReport(kind === "md" ? await api.reportMd(wid) : await api.prComment(wid));
  }

  const filtered = !!(query || intent || proof || status || hotOnly);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Review</h1>
          <p>
            {r.findings.length} suggestions, ranked and checked against the versions this repo installs
            {r.base ? <>, for code changed since <code>{r.base}</code></> : null}.
            <span className="faint"> {r.rejected.length} dropped by the critic.</span>
          </p>
        </div>
        <div className="actions">
          <button className="btn" disabled={!!data?.running} onClick={() => runReview(true, "", true)}
            title="Run the review with proof, then time each verified patch: the tests that run it, before and after (best of 3)">
            <Gauge size={14} />Measure speed
          </button>
          <button className={`btn ${prMode ? "on" : ""}`} onClick={() => setPrMode(!prMode)}><GitBranch size={14} />PR mode</button>
          <button className="btn" onClick={() => showReport("pr")}><ClipboardCopy size={14} />PR comment</button>
          <button className="btn" onClick={() => showReport("md")}><Download size={14} />Report</button>
        </div>
      </div>

      {prMode && (
        <div className="prbar">
          <GitBranch size={15} className="faint" />
          <span>Review only code changed since</span>
          <input className="input mono" style={{ width: 180 }} value={base} onChange={(e) => setBase(e.target.value)} aria-label="Base ref" />
          <button className="btn primary sm" disabled={data.running} onClick={() => runReview(true, base)}><Play size={13} />Review changes</button>
          <button className="btn sm" disabled={data.running} onClick={() => runReview(true, "")}>Whole repo</button>
          <span className="faint" style={{ fontSize: 13 }}>In CI: <code>codewright review --base {base} -f pr</code></span>
        </div>
      )}

      <div className="appframe">
        <div className="appframe-list">
          <div className="appframe-bar">
            <Tabs<ListTab> value={tab} onChange={setTab} options={[
              { value: "shown", label: <>Shown <span className="badge">{r.findings.length}</span></> },
              { value: "dropped", label: <>Dropped <span className="badge">{r.rejected.length}</span></> },
              { value: "muted", label: <>Muted <span className="badge">{r.muted.length}</span></> },
            ]} />
          </div>
          <div className="appframe-bar">
            <div className="search grow">
              <Search size={14} />
              <input className="input" style={{ width: "100%" }} placeholder="Search findings" aria-label="Search findings" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
            <button className={`btn sm icon ${hotOnly ? "on" : ""}`} title="Hot path only" aria-label="Hot path only" aria-pressed={hotOnly} onClick={() => setHotOnly(!hotOnly)}><Flame size={14} /></button>
            <div className="row wrap" style={{ width: "100%", gap: 6 }}>
              <select className="select grow" style={{ height: 30, fontSize: 13 }} value={intent} onChange={(e) => setIntent(e.target.value)} aria-label="Intent">
                <option value="">All intents</option>
                {intents.map((i) => <option key={i} value={i}>{intentLabel(i)}</option>)}
              </select>
              <select className="select" style={{ height: 30, fontSize: 13 }} value={proof} onChange={(e) => setProof(e.target.value)} aria-label="Proof">
                <option value="">Any proof</option><option value="verified">Verified</option><option value="unverified">Unverified</option>
              </select>
              <select className="select" style={{ height: 30, fontSize: 13 }} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
                <option value="">Any status</option><option value="open">Open</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option>
              </select>
            </div>
          </div>
          <div className="rows">
            {list.length === 0 && (
              <div className="card-pad faint" style={{ fontSize: 14 }}>
                {filtered ? "No findings match these filters." : tab === "dropped" ? "The critic dropped nothing: every candidate named a file, an API version, a gain, a risk and a source." : tab === "muted" ? "Nothing is muted." : "No findings."}
              </div>
            )}
            {list.map((f) => (
              <button key={f.id} className={`fitem ${selected?.id === f.id ? "active" : ""} ${f.status === "rejected" ? "dim" : ""}`}
                onClick={() => go("review", { f: f.id })}>
                <div className="t">{f.title}</div>
                <div className="loc truncate">{f.file}:{f.line}</div>
                <div className="meta">
                  {tab === "dropped" ? <span className="badge red">{f.rejected_reason}</span> : <ProofBadge proof={f.proof} />}
                  {f.patch && <span className="badge"><Wand2 size={11} />Patch</span>}
                  {f.hot_path && <span className="badge"><Flame size={11} />Hot</span>}
                  {f.status === "accepted" && <span className="badge green"><Check size={11} />Accepted</span>}
                  {tab === "shown" && <span className="score">{f.score.toFixed(2)}</span>}
                </div>
              </button>
            ))}
          </div>
        </div>

        <div className="appframe-open">
          {selected ? (
            <Detail f={selected} dropped={tab === "dropped"}
              onAccept={() => verdict(selected, "accepted")}
              onReject={() => (selected.status === "rejected" ? verdict(selected, "rejected") : setRejecting(selected))}
              onMute={() => muteIntent(selected)} onApply={() => setApplying(selected)}
              onCode={() => openCode(selected.file, selected.line, selected.span)} />
          ) : (
            <Empty icon={<ShieldCheck size={20} />} title="Nothing selected">Pick a finding on the left.</Empty>
          )}
        </div>
      </div>

      {rejecting && (
        <Modal title="Reject this suggestion" onClose={() => setRejecting(null)}
          footer={<><button className="btn" onClick={() => setRejecting(null)}>Cancel</button>
            <button className="btn primary" onClick={() => { verdict(rejecting, "rejected", reason); setRejecting(null); }}><ThumbsDown size={14} />Reject suggestion</button></>}>
          <p className="muted" style={{ marginTop: 0 }}>Why? Findings of the same intent will rank lower in this repo.</p>
          <div className="chips" style={{ marginBottom: 12 }}>
            {["Not worth the churn", "Wrong for our scale", "We can't add dependencies", "Behaviour must stay the same", "Wrong API"].map((x) => (
              <button key={x} className={`chip ${reason === x ? "on" : ""}`} onClick={() => setReason(x)}>{x}</button>
            ))}
          </div>
          <textarea className="textarea" rows={3} placeholder="Or write your own reason" aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Modal>
      )}

      {applying && (
        <Modal title="Apply this patch to your working tree?" onClose={() => setApplying(null)}
          footer={<><button className="btn" onClick={() => setApplying(null)}>Cancel</button>
            <button className="btn primary" onClick={() => applyPatch(applying)}><Wand2 size={14} />Apply patch</button></>}>
          <p className="muted" style={{ marginTop: 0 }}>This edits <code>{applying.file}</code>. Nothing is committed or pushed; you review the diff and commit it.</p>
          {applying.proof !== "verified" && <div className="callout amber">Not proven by tests yet. {applying.proof_detail}</div>}
        </Modal>
      )}

      {report != null && (
        <Modal title="Report" onClose={() => setReport(null)} footer={<CopyButton text={report} label="Copy Markdown" />}>
          <pre className="code" style={{ maxHeight: 440, padding: 14, whiteSpace: "pre-wrap", margin: 0 }}>{report}</pre>
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
    <>
      <div className="detail-head">
        <div className="row wrap" style={{ marginBottom: 10, gap: 6 }}>
          <span className="badge outline">{intentLabel(f.intent)}</span>
          {!dropped && <ProofBadge proof={f.proof} />}
          {f.hot_path && <span className="badge"><Flame size={11} />Hot path</span>}
          <span className={`badge ${f.symbol_check === "missing" ? "red" : ""}`}>
            {f.symbol_check === "ok" ? <Check size={11} /> : null}API {f.symbol_check === "ok" ? "exists" : f.symbol_check}
          </span>
          <span className="badge">Confidence {Math.round(f.confidence * 100)}%</span>
          {f.perf?.verdict === "faster" && <span className="badge green" title={`${f.perf.before_ms} ms → ${f.perf.after_ms} ms, best of ${f.perf.rounds}`}><Gauge size={11} />Measured {f.perf.speedup >= 10 ? Math.round(f.perf.speedup) : f.perf.speedup.toFixed(1)}× faster</span>}
          {f.perf?.verdict === "slower" && <span className="badge red"><Gauge size={11} />Measured {(1 / f.perf.speedup).toFixed(1)}× slower</span>}
          {f.perf?.verdict === "no-change" && <span className="badge outline"><Gauge size={11} />No speed change</span>}
        </div>
        <h2>{f.title}</h2>
        <div className="row wrap" style={{ marginTop: 6, gap: 4 }}>
          <button className="btn ghost sm mono" style={{ paddingLeft: 0 }} onClick={onCode}><Code2 size={13} />{f.file}:{f.line}</button>
          <span className="faint mono" style={{ fontSize: 12 }}>in {f.function}</span>
        </div>
        {!dropped && (
          <div className="row wrap" style={{ marginTop: 14 }}>
            <button className={`btn ${f.status === "accepted" ? "on" : "primary"}`} onClick={onAccept}>
              <ThumbsUp size={14} />{f.status === "accepted" ? "Accepted" : "Accept"}
            </button>
            <button className={`btn ${f.status === "rejected" ? "danger on" : ""}`} onClick={onReject}>
              <ThumbsDown size={14} />{f.status === "rejected" ? "Rejected" : "Reject"}
            </button>
            {f.patch && <button className="btn" onClick={onApply}><Wand2 size={14} />Apply patch</button>}
            {f.patch && <CopyButton text={f.patch} label="Copy patch" />}
            <button className="btn ghost" onClick={onMute} title="Hide this intent in this repo"><BellOff size={14} />Mute intent</button>
          </div>
        )}
      </div>

      <div className="detail-body">
        {dropped && <div className="callout red"><b>Dropped by the critic:</b> {f.rejected_reason}. Shown only so you can see why.</div>}

        <div className="lead">Found: <Inline text={f.evidence} />.</div>

        <div className="callout accent">
          <div className="section-title" style={{ color: "var(--text)" }}><Lightbulb size={14} />Instead, use</div>
          <div><Inline text={f.suggestion} /></div>
          <div className="mono" style={{ marginTop: 8, color: "var(--text-2)" }}>{f.api}</div>
        </div>

        {f.patch ? (
          <div>
            <div className="row" style={{ marginBottom: 10 }}>
              <div className="section-title" style={{ margin: 0 }}><FileDiff size={14} />Patch</div>
              <div style={{ marginLeft: "auto" }}>
                <Tabs value={view} onChange={setView} options={[{ value: "after", label: "Diff" }, { value: "before", label: "Code today" }]} />
              </div>
            </div>
            {view === "after" ? <DiffView patch={f.patch} /> : <CodeView text={f.code_today} start={f.span[0]} highlightLines={[f.line, f.line]} />}
          </div>
        ) : (
          <div className="grid" style={{ gridTemplateColumns: f.snippet ? "repeat(auto-fit, minmax(280px, 1fr))" : "1fr" }}>
            <div>
              <div className="section-title"><Code2 size={14} />Code today</div>
              <CodeView text={f.code_today} start={f.span[0]} highlightLines={[f.line, f.line]} />
            </div>
            {f.snippet && (
              <div>
                <div className="section-title"><Lightbulb size={14} />Suggested shape</div>
                <CodeView text={f.snippet} lang={f.snippet_lang} />
              </div>
            )}
          </div>
        )}

        <div className="kv">
          <div className="kv-item good"><div className="k"><ThumbsUp size={13} />Gain</div><Inline text={f.gain} /></div>
          <div className="kv-item warn"><div className="k"><TriangleAlert size={13} />Risk · effort {f.effort} of 5</div><Inline text={f.risk} /></div>
          <div className="kv-item bad"><div className="k"><ShieldAlert size={13} />When not to</div><Inline text={f.when_not} /></div>
          <div className="kv-item"><div className="k"><Ruler size={13} />Scale threshold</div><Inline text={f.threshold} /></div>
        </div>

        <div>
          <div className="section-title"><ShieldCheck size={14} />Proof</div>
          <div className={`callout ${f.proof === "verified" ? "green" : f.proof === "broke-tests" ? "red" : "amber"}`} style={{ whiteSpace: "pre-wrap" }}>
            <Inline text={f.proof_detail || "Not run."} />
          </div>
        </div>

        <div>
          <div className="section-title"><BookOpen size={14} />Sources</div>
          <div className="col sources" style={{ gap: 4 }}>{f.sources.map((s) => <Source key={s} url={s} />)}</div>
        </div>
      </div>
    </>
  );
}
