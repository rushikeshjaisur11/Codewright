import { useEffect, useState } from "react";
import {
  ArrowRight, Bot, CheckCircle2, CircleDot, ClipboardCheck, FileDiff, GitBranch, Route, TriangleAlert, Wand2, XCircle,
} from "lucide-react";
import { useApp } from "../App";
import { api, MigrationPlan, MigrationRow, PlanReview, StepDetail, UpgradeRewrite } from "../api";
import { CopyButton, DiffView, Empty, Inline, Modal, Spinner, Tabs, useToast } from "../ui";

type Tab = "review" | "migrations";

export default function Plans() {
  const { params, go } = useApp();
  const tab = (params.get("tab") as Tab) || "review";
  return (
    <div className="page grid">
      <div className="page-head">
        <div>
          <h1>Plans &amp; migrations</h1>
          <p>Judge a plan before any code is written, and carry out upgrades as small, ordered steps.</p>
        </div>
        <div className="actions">
          <Tabs value={tab} onChange={(t) => go("plans", { tab: t })}
            options={[{ value: "review", label: "Review a plan" }, { value: "migrations", label: "Migrations" }]} />
        </div>
      </div>
      {tab === "review" ? <PlanReviewTab /> : <MigrationsTab />}
    </div>
  );
}

// ------------------------------------------------------------------ proposal review

const PLACEHOLDER = `Paste an RFC, a design doc or an agent's plan. For example:

1. Add requests to call the partner API from app/api/routes.py.
2. Write our own retry helper with backoff around the partner call.
3. Add a new column exported_at to the orders table.
4. Run 3 replicas with an in-memory cache of partner tokens.`;

const LEVEL = {
  blocker: { badge: "red", icon: <XCircle size={15} color="var(--red)" /> },
  concern: { badge: "amber", icon: <TriangleAlert size={15} color="var(--amber)" /> },
  ok: { badge: "green", icon: <CheckCircle2 size={15} color="var(--green)" /> },
} as const;

function PlanReviewTab() {
  const { wid } = useApp();
  const toast = useToast();
  const [text, setText] = useState(() => {
    try { return sessionStorage.getItem("cw-plan") ?? ""; } catch { return ""; }
  });
  const [res, setRes] = useState<PlanReview | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { try { sessionStorage.setItem("cw-plan", text); } catch { /* private mode */ } }, [text]);

  const run = async () => {
    if (!wid || !text.trim()) return;
    setBusy(true);
    try { setRes(await api.critique(wid, text)); } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };

  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1.2fr)", alignItems: "start" }}>
      <div className="card">
        <div className="card-head"><h2><ClipboardCheck size={16} />The plan</h2></div>
        <div className="card-pad col" style={{ gap: 10 }}>
          <textarea className="textarea" rows={16} value={text} placeholder={PLACEHOLDER}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(); }} />
          <div className="row">
            <span className="faint grow" style={{ fontSize: 12.5 }}>Checked against this repo's stack, code, rules and saved design. Nothing is sent anywhere.</span>
            <button className="btn primary" disabled={busy || !text.trim()} onClick={run}>
              {busy ? <Spinner size={14} /> : <ClipboardCheck size={14} />}Review plan
            </button>
          </div>
        </div>
      </div>
      <div className="card">
        {!res ? (
          <Empty icon={<ClipboardCheck size={22} />} title="No review yet">
            Paste a plan and press Review plan (⌘/Ctrl + Enter). You get a verdict and each point with its evidence: a library the repo already has, a rule it would break, a function with many callers and no test.
          </Empty>
        ) : (
          <>
            <div className="card-head">
              <h2>{res.verdict === "go" ? <CheckCircle2 size={16} color="var(--green)" /> : <TriangleAlert size={16} color="var(--amber)" />}
                {res.verdict === "go" ? "Go" : "Revise"}: {res.headline}</h2>
              <div className="right"><span className="badge outline">Score {res.score}</span></div>
            </div>
            {res.points.length === 0 ? <div className="card-pad muted">Nothing to flag.</div> : (
              <div className="card-pad col" style={{ gap: 14 }}>
                {res.points.map((p, i) => (
                  <div key={i} className="col" style={{ gap: 4 }}>
                    <div className="row" style={{ gap: 8 }}>
                      {LEVEL[p.level].icon}
                      <b className="grow"><Inline text={p.title} /></b>
                      <span className="badge outline">{p.topic}</span>
                    </div>
                    <div style={{ paddingLeft: 23 }}><Inline text={p.detail} /></div>
                    {p.quote && <div className="faint" style={{ paddingLeft: 23, fontSize: 12.5, borderLeft: "2px solid var(--border)", marginLeft: 23, paddingInlineStart: 8 }}>{p.quote}</div>}
                    {p.evidence.length > 0 && (
                      <div className="row wrap" style={{ paddingLeft: 23, gap: 4 }}>
                        {p.evidence.map((e) => e.startsWith("http")
                          ? <a key={e} className="badge outline" href={e} target="_blank" rel="noreferrer">source</a>
                          : <span key={e} className="badge outline mono" style={{ fontSize: 11 }}>{e}</span>)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ migrations

function MigrationsTab() {
  const { wid, data, reload, openCode, go } = useApp();
  const toast = useToast();
  const [rows, setRows] = useState<MigrationRow[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [plan, setPlan] = useState<MigrationPlan | null>(null);
  const [batch, setBatch] = useState(5);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<StepDetail | null>(null);

  const loadRows = () => { if (wid) api.migrations(wid).then((m) => setRows(m.applicable)).catch((e) => toast(e.message, "error")); };
  useEffect(loadRows, [wid, data?.last_run]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!wid || !open) { setPlan(null); return; }
    const row = rows?.find((r) => r.name === open);
    if (row?.planned) api.migration(wid, open).then(setPlan).catch(() => setPlan(null));
    else setPlan(null);
  }, [wid, open, rows]);

  const makePlan = async (name: string) => {
    if (!wid) return;
    setBusy(true);
    try {
      setPlan(await api.planMigration(wid, name, batch));
      toast(`Saved .codewright/migrations/${name}.json`);
      loadRows();
    } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };
  const [ai, setAi] = useState<UpgradeRewrite | null>(null);
  const [aiChosen, setAiChosen] = useState<Set<string>>(new Set());
  const [writing, setWriting] = useState(false);
  const writeWithAi = async () => {
    if (!wid || !open || !step) return;
    setWriting(true);
    try {
      const r = await api.stepAi(wid, open, step.step.n);
      setAi(r);
      setAiChosen(new Set(r.patches.map((p) => p.id)));
      if (!r.patches.length) toast("The AI could not write these changes; see why below", "error");
    } catch (e) { toast((e as Error).message, "error"); } finally { setWriting(false); }
  };
  const handToAgent = async () => {
    if (!wid || !open || !step) return;
    try {
      await api.migrationAgent(wid, open, step.step.n);
      toast("The AI agent is working on this step in a sandbox; follow it on the AI agent page");
      go("agent");
    } catch (e) { toast((e as Error).message, "error"); }
  };
  const showStep = async (n: number) => {
    setAi(null);
    if (!wid || !open) return;
    try { setStep(await api.migrationStep(wid, open, n)); } catch (e) { toast((e as Error).message, "error"); }
  };
  const apply = async () => {
    if (!wid || !open || !step) return;
    setBusy(true);
    try {
      const extra = (ai?.patches ?? []).filter((p) => aiChosen.has(p.id)).map((p) => p.patch);
      const r = await api.applyStep(wid, open, step.step.n, extra);
      setPlan(r.status);
      setStep(null);
      setAi(null);
      toast(`Applied step ${step.step.n} to ${r.files.length} file(s)${r.failed.length ? `; ${r.failed.length} AI change(s) did not apply (${r.failed.join(", ")}${r.errors?.[0] ? `: ${r.errors[0].why.split("\n")[0].slice(0, 160)}` : ""})` : ""}. Nothing is committed: run its tests, then commit from Review.`);
      reload();
    } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };

  if (rows == null) return <div className="card card-pad"><Spinner /></div>;
  if (!rows.length) return (
    <div className="card">
      <Empty icon={<Route size={22} />} title="Nothing to migrate">
        No catalog migration (Pydantic v2, SQLAlchemy 2, FastAPI lifespan and Annotated, blocking calls in async code, aware datetimes, Google ADK) touches this repo's code.
      </Empty>
    </div>
  );

  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(0, 0.9fr) minmax(0, 1.6fr)", alignItems: "start" }}>
      <div className="card">
        <div className="card-head"><h2><Route size={16} />Migrations for this repo</h2></div>
        <div className="table-scroll"><table className="table">
          <tbody>
            {rows.map((r) => (
              <tr key={r.name} className={`click ${open === r.name ? "selected" : ""}`} onClick={() => setOpen(r.name)}>
                <td style={{ width: "100%" }}>
                  <div style={{ fontWeight: 600 }}>{r.title}</div>
                  <div className="faint" style={{ fontSize: 12.5 }}>
                    {r.library ? <span className="mono">{r.library} {r.installed ?? "?"} → {r.target ?? "current"} · </span> : null}
                    {r.places} place(s) in {r.files} file(s){r.autofix ? ` · ${r.autofix} auto-patch` : ""}
                  </div>
                </td>
                <td>{r.planned && <span className="badge green">planned</span>}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>

      <div className="card">
        {!open ? (
          <Empty icon={<GitBranch size={22} />} title="Pick a migration">
            A plan orders the files leaf modules first and cuts them into steps of a few files, each with its own patch, tests and PR text. Progress follows the code: a step is done when none of its changes remain.
            Changes with no automatic patch are written by AI: open a step and click <b>Write the changes with AI</b> (set a model up in Settings).
            To move a library to a new major version, use <b>Releases → Upgrade</b>, which checks what breaks and has AI rewrite it.
          </Empty>
        ) : !plan ? (
          <div className="card-pad col" style={{ gap: 12 }}>
            <h2 style={{ margin: 0 }}>{rows.find((r) => r.name === open)?.title}</h2>
            <div className="row" style={{ gap: 8 }}>
              <label className="muted" htmlFor="batch">Files per step</label>
              <input id="batch" className="input" type="number" min={1} max={50} value={batch} style={{ width: 80 }}
                onChange={(e) => setBatch(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
              <button className="btn primary" disabled={busy} onClick={() => makePlan(open)}>
                {busy ? <Spinner size={14} /> : <Route size={14} />}Plan it
              </button>
            </div>
            <span className="faint" style={{ fontSize: 12.5 }}>The plan is saved to <code>.codewright/migrations/{open}.json</code>; commit it so the team shares progress.</span>
          </div>
        ) : (
          <>
            <div className="card-head">
              <h2>{plan.title}</h2>
              <div className="right row" style={{ gap: 6 }}>
                {plan.library && <span className="badge outline mono">{plan.library} {plan.installed ?? "?"} → {plan.target ?? "current"}</span>}
                <button className="btn sm ghost" disabled={busy} onClick={() => makePlan(plan.name)} title="Re-plan with the current code">Re-plan</button>
              </div>
            </div>
            <div className="card-pad col" style={{ gap: 12 }}>
              {plan.progress != null && (
                <div>
                  <div className="row" style={{ fontSize: 13 }}>
                    <span className="grow">{plan.done_items} of {plan.total} change(s) done</span>
                    <span className="mono muted">{Math.round(plan.progress * 100)}%</span>
                  </div>
                  <div className="meter" style={{ marginTop: 4 }}><span style={{ width: `${plan.progress * 100}%` }} /></div>
                </div>
              )}
              {plan.notes && <div className="muted" style={{ fontSize: 13 }}>{plan.notes}</div>}
              {plan.tools.map((t) => <div key={t} className="callout" style={{ fontSize: 13, padding: "8px 12px" }}>Tool: <Inline text={t} /></div>)}
              {(plan.new_since_plan?.length ?? 0) > 0 && (
                <div className="callout amber">{plan.new_since_plan!.length} new place(s) since the plan was made. Re-plan to include them.</div>
              )}
            </div>
            <div className="table-scroll"><table className="table">
              <thead><tr><th>Step</th><th>Files</th><th>Changes</th><th>Tests</th><th /></tr></thead>
              <tbody>
                {plan.steps.map((s) => (
                  <tr key={s.n} className="click" onClick={() => showStep(s.n)}>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {s.done ? <CheckCircle2 size={14} color="var(--green)" /> : s.n === plan.next_step ? <CircleDot size={14} color="var(--accent)" /> : <CircleDot size={14} color="var(--text-3)" />}
                      {" "}<b>{s.n}</b>{s.depends_on.length > 0 && <span className="faint" style={{ fontSize: 12 }}> after {s.depends_on.join(", ")}</span>}
                    </td>
                    <td className="mono" style={{ fontSize: 12.5 }}>
                      {s.files.map((f) => <div key={f}><a onClick={(e) => { e.stopPropagation(); openCode(f); }}>{f}</a></div>)}
                    </td>
                    <td className="muted" style={{ fontSize: 13 }}>
                      {s.done ? "done" : `${s.remaining || s.items.length} left`}{s.autofix ? ` · ${s.autofix} auto-patch` : ""}
                      {!s.done && s.items.some((i) => !i.autofix) && (
                        <div className="row" style={{ gap: 4, color: "var(--accent)" }} title="Open the step and click “Write the changes with AI”">
                          <Bot size={12} />{s.items.filter((i) => !i.autofix).length} for AI
                        </div>
                      )}
                    </td>
                    <td className="muted" style={{ fontSize: 13 }}>{s.tests.length || <span className="faint">none</span>}</td>
                    <td><ArrowRight size={14} className="faint" /></td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </>
        )}
      </div>

      {step && (
        <Modal title={`Step ${step.step.n}: ${step.step.files.join(", ")}`} onClose={() => setStep(null)} footer={
          <>
            <CopyButton text={step.pr_body} label="Copy PR text" />
            {step.patch && <CopyButton text={step.patch} label="Copy patch" />}
            {!step.step.done && step.step.items.some((i) => !i.autofix) && (
              <button className="btn" disabled={writing || busy} onClick={handToAgent} title="Searches the code and the web, edits a sandbox copy and runs the tests">
                <Wand2 size={14} />Hand to the AI agent
              </button>
            )}
            {!step.step.done && step.step.items.some((i) => !i.autofix) && (
              <button className="btn" disabled={writing || busy} onClick={writeWithAi}>
                {writing ? <Spinner size={14} /> : <Bot size={14} />}{ai ? "Write again with AI" : "Write the changes with AI"}
              </button>
            )}
            {(step.patch || aiChosen.size > 0 && ai) && !step.step.done && (
              <button className="btn primary" disabled={busy} onClick={apply}>
                {busy ? <Spinner size={14} /> : <Wand2 size={14} />}Apply to working tree
              </button>
            )}
          </>
        }>
          <div className="col" style={{ gap: 12 }}>
            <div className="col" style={{ gap: 4 }}>
              {step.step.items.map((i) => (
                <div key={i.key} className="row" style={{ fontSize: 13.5 }}>
                  {i.autofix ? <Wand2 size={13} color="var(--accent)" /> : <CircleDot size={13} className="faint" />}
                  <span className="mono faint">{i.file}:{i.line}</span><span>{i.title}</span>
                </div>
              ))}
            </div>
            {step.skipped.length > 0 && <div className="callout amber">{step.skipped.length} patch(es) didn't apply on top of the others; make those changes by hand.</div>}
            {step.patch ? (
              <div><div className="section-title"><FileDiff size={14} />Combined patch</div><DiffView patch={step.patch} /></div>
            ) : !ai && <div className="callout">No change in this step can be made mechanically. <b>Write the changes with AI</b> rewrites each function for you to review, or make them by hand (the PR text lists them).</div>}
            {ai && (
              <div className="col" style={{ gap: 8 }}>
                <div className="section-title"><Bot size={14} />Written by {ai.model}: tick the ones to apply</div>
                {ai.patches.map((p) => (
                  <div key={p.id} className="col" style={{ gap: 4 }}>
                    <label className="row" style={{ gap: 8, alignItems: "flex-start", fontSize: 13 }}>
                      <input type="checkbox" checked={aiChosen.has(p.id)} style={{ marginTop: 3 }} aria-label={`Apply the change to ${p.function}`}
                        onChange={() => { const n = new Set(aiChosen); n.has(p.id) ? n.delete(p.id) : n.add(p.id); setAiChosen(n); }} />
                      <span><span className="mono">{p.file}</span> <b>{p.function}</b> <span className="muted"><Inline text={p.explanation} /></span></span>
                    </label>
                    <DiffView patch={p.patch} />
                  </div>
                ))}
                {ai.skipped.map((k, i) => <div key={i} style={{ fontSize: 13 }}>✋ <span className="mono">{k.file}</span> {k.function}: <span className="muted">{k.why}</span></div>)}
              </div>
            )}
            {step.step.tests.length > 0 && <div className="mono" style={{ fontSize: 12.5 }}>pytest -q {step.step.tests.join(" ")}</div>}
          </div>
        </Modal>
      )}
    </div>
  );
}
