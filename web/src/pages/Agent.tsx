import { useEffect, useRef, useState } from "react";
import { Bot, CheckCircle2, CircleStop, FileSearch, FlaskConical, Globe, Pencil, Play, Search, Terminal, Wand2, XCircle } from "lucide-react";
import { useApp } from "../App";
import { AgentEvent, AgentJob, api } from "../api";
import { DiffView, Empty, Spinner, useToast } from "../ui";

const EXAMPLES = [
  "Add retries with exponential backoff to the HTTP calls in the client",
  "Replace the deprecated datetime.utcnow() calls and keep behaviour the same",
  "Add type hints to the public functions in the service layer",
  "Write tests for the parsing module and fix any bug they find",
];

const TOOL: Record<string, { icon: JSX.Element; label: string }> = {
  list_files: { icon: <FileSearch size={13} />, label: "Listed" },
  search_code: { icon: <Search size={13} />, label: "Searched the code" },
  read_file: { icon: <FileSearch size={13} />, label: "Read" },
  edit_file: { icon: <Pencil size={13} />, label: "Edited" },
  write_file: { icon: <Pencil size={13} />, label: "Wrote" },
  run_tests: { icon: <FlaskConical size={13} />, label: "Ran tests" },
  run_python: { icon: <Terminal size={13} />, label: "Ran Python" },
  web_search: { icon: <Globe size={13} />, label: "Searched the web" },
  fetch_url: { icon: <Globe size={13} />, label: "Read" },
};

function Step({ e }: { e: AgentEvent }) {
  const [open, setOpen] = useState(false);
  if (e.kind === "text") return <div className="muted" style={{ fontSize: 13, whiteSpace: "pre-wrap", padding: "4px 0" }}>{e.text}</div>;
  const t = TOOL[e.tool ?? ""] ?? { icon: <Wand2 size={13} />, label: e.tool };
  return (
    <div className="col" style={{ gap: 4 }}>
      <button className="row" style={{ gap: 8, background: "none", border: 0, padding: "3px 0", cursor: "pointer", color: "inherit", textAlign: "left", fontSize: 13 }}
        onClick={() => setOpen(!open)} aria-expanded={open}>
        <span style={{ color: !e.ok ? "var(--red)" : /^(FAILED|exit code [1-9])/.test(e.result ?? "") ? "var(--amber)" : "var(--green)", display: "flex" }}>{e.ok ? t.icon : <XCircle size={13} />}</span>
        <b style={{ fontWeight: 600 }}>{t.label}</b>
        <span className="mono faint" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>{e.detail}</span>
        {e.seconds != null && e.seconds >= 1 && <span className="faint" style={{ fontSize: 11.5, marginLeft: "auto", flex: "none" }}>{e.seconds}s</span>}
      </button>
      {open && <pre className="mono" style={{ fontSize: 11.5, margin: 0, padding: 10, background: "var(--panel-2)", borderRadius: 6, whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{e.result}</pre>}
    </div>
  );
}

export default function AgentPage() {
  const { wid, runReview } = useApp();
  const toast = useToast();
  const [task, setTask] = useState("");
  const [web, setWeb] = useState(true);
  const [tests, setTests] = useState(true);
  const [job, setJob] = useState<AgentJob | null>(null);
  const [busy, setBusy] = useState("");
  const events = useRef<AgentEvent[]>([]);
  const log = useRef<HTMLDivElement>(null);

  async function poll(since: number) {
    if (!wid) return;
    const p = await api.agentProgress(wid, since);
    events.current = since === 0 ? p.events : [...events.current, ...p.events];
    setJob({ ...p, events: events.current });
    return p;
  }

  useEffect(() => { events.current = []; setJob(null); if (wid) poll(0).catch(() => undefined); }, [wid]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!job?.running) return;
    const t = window.setInterval(() => { poll(events.current.length).catch(() => undefined); }, 1000);
    return () => window.clearInterval(t);
  }, [job?.running]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [job?.events.length]);

  async function start() {
    if (!wid || !task.trim()) return;
    setBusy("start");
    try {
      events.current = [];
      setJob(await api.agentStart(wid, task, { web, tests }));
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy("");
  }

  async function stop() {
    if (!wid) return;
    try { setJob({ ...(await api.agentStop(wid)), events: events.current }); } catch (e) { toast((e as Error).message, "error"); }
  }

  async function apply() {
    if (!wid) return;
    setBusy("apply");
    try {
      const r = await api.agentApply(wid);
      toast(`Applied to ${r.files.join(", ")}. Nothing is committed: review the diff, then commit from Review.`);
      await poll(0);
      runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy("");
  }

  if (!wid) return <Empty icon={<Bot size={22} />} title="Open a repository first" />;
  const res = job?.result;
  const v = res?.verification;
  return (
    <div className="col" style={{ gap: 16, maxWidth: 1000 }}>
      <div className="page-head"><div>
        <h1>AI agent</h1>
        <p>Describe a change. The agent searches and reads your code, looks up docs and release notes on the web, edits a sandbox copy of the repo and runs your tests there. You review the result and apply it; your files change only then.</p>
      </div></div>

      <div className="card"><div className="card-pad col" style={{ gap: 12 }}>
        <textarea className="input" rows={4} value={task} placeholder="What should change? Be as specific as you would be with a colleague: which behaviour, which files if you know, how to check it."
          aria-label="Task for the agent" onChange={(e) => setTask(e.target.value)} disabled={job?.running}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start(); }} style={{ resize: "vertical", fontFamily: "inherit" }} />
        {!task && !job?.running && (
          <div className="chips">{EXAMPLES.map((x) => <button key={x} className="chip" onClick={() => setTask(x)}>{x}</button>)}</div>
        )}
        <div className="row wrap" style={{ gap: 16 }}>
          <label className="row" style={{ gap: 6, fontSize: 13 }}><input type="checkbox" checked={web} onChange={(e) => setWeb(e.target.checked)} disabled={job?.running} /><Globe size={13} />Search the web and read docs</label>
          <label className="row" style={{ gap: 6, fontSize: 13 }}><input type="checkbox" checked={tests} onChange={(e) => setTests(e.target.checked)} disabled={job?.running} /><FlaskConical size={13} />Run tests and scripts in the sandbox</label>
          <div className="row" style={{ gap: 8, marginLeft: "auto" }}>
            {job?.running
              ? <button className="btn" onClick={stop}><CircleStop size={14} />Stop</button>
              : <button className="btn primary" disabled={!task.trim() || !!busy} onClick={start}>{busy === "start" ? <Spinner size={14} /> : <Play size={14} />}Start</button>}
          </div>
        </div>
      </div></div>

      {job?.task && (
        <div className="card">
          <div className="card-head"><h2><Bot size={16} />{job.origin ? `From ${job.origin}` : "Task"}</h2>
            <div className="right">
              {job.running ? <span className="badge blue"><Spinner size={11} />{job.status}</span>
                : job.error ? <span className="badge amber"><XCircle size={11} />error</span>
                : res ? <span className={`badge ${res.ended === "finished" || res.ended === "answered" ? "green" : "amber"}`}>{res.ended} · {res.steps_used} step(s)</span> : null}
            </div>
          </div>
          <div className="card-pad col" style={{ gap: 10 }}>
            <div style={{ fontSize: 13.5, whiteSpace: "pre-wrap" }}>{job.task}</div>
            <div ref={log} className="col" style={{ gap: 2, maxHeight: 420, overflow: "auto", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              {job.events.map((e, i) => <Step key={i} e={e} />)}
              {job.running && <div className="faint pulse" style={{ fontSize: 12.5, padding: "4px 0" }}>{job.status}</div>}
              {!job.running && job.events.length === 0 && !res && <div className="faint" style={{ fontSize: 12.5 }}>No steps yet.</div>}
            </div>
            {job.error && <div className="row" role="alert" style={{ color: "var(--red)", fontSize: 13 }}><XCircle size={14} />{job.error}</div>}
          </div>
        </div>
      )}

      {res && (
        <div className="card">
          <div className="card-head"><h2><Wand2 size={16} />Result</h2>
            <div className="right">
              {v ? (v.ok ? <span className="badge green"><CheckCircle2 size={11} />test suite passes</span>
                : v.new_failures.length ? <span className="badge red"><XCircle size={11} />{v.new_failures.length} test(s) broken by the change</span>
                : <span className="badge amber">{v.failed.length} test(s) were already failing</span>)
                : res.tests ? <span className={`badge ${res.tests.ok ? "green" : "amber"}`}>{res.tests.ok ? "agent's last test run passed" : "agent's last test run failed"}{res.tests.after_last_edit ? "" : " (before its last edit)"}</span>
                : res.patch ? <span className="badge outline">not tested</span> : null}
            </div>
          </div>
          <div className="card-pad col" style={{ gap: 12 }}>
            <div style={{ fontSize: 13.5, whiteSpace: "pre-wrap" }}>{res.summary}</div>
            {res.error && <div className="faint" style={{ fontSize: 12.5 }}>The model stopped: {res.error}</div>}
            {v && !v.ok && (
              <details><summary className="faint" style={{ fontSize: 12.5, cursor: "pointer" }}>Test output</summary>
                <pre className="mono" style={{ fontSize: 11.5, whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto" }}>{v.output}</pre>
                {v.new_failures.length > 0 && <div style={{ fontSize: 12.5 }}>Fail only with the change: <span className="mono">{v.new_failures.join(", ")}</span></div>}
              </details>
            )}
            {res.patch ? <>
              <div className="faint" style={{ fontSize: 12.5 }}>{res.files.length} file(s): <span className="mono">{res.files.join(", ")}</span>{!res.applies && res.apply_error && <> · <span style={{ color: "var(--red)" }}>does not apply to your files now: {res.apply_error.split("\n")[0]}</span></>}</div>
              <DiffView patch={res.patch} />
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary" disabled={!!busy || job?.applied || !res.applies} onClick={apply}>
                  {busy === "apply" ? <Spinner size={14} /> : <Wand2 size={14} />}{job?.applied ? "Applied" : "Apply to working tree"}
                </button>
                <span className="faint" style={{ fontSize: 12.5 }}>Nothing is committed. Start a new task to discard this one.</span>
              </div>
            </> : <div className="muted" style={{ fontSize: 13 }}>The agent made no change.</div>}
          </div>
        </div>
      )}
    </div>
  );
}
