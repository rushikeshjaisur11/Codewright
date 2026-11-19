import { useEffect, useState } from "react";
import { Bot, Check, ShieldCheck, Sparkles, Flame, FileCode2 } from "lucide-react";
import { useApp } from "./App";
import { AiBatch, api, PlanFile, ReviewPlan, ReviewScope } from "./api";
import { Spinner, useToast } from "./ui";

/**
 * Tests are the slow part of a review, so they run only on files you confirm. The model (when one is set up)
 * suggests which files are worth it; otherwise they are ranked by impact. Nothing runs until you press the button.
 */
export default function VerifyPanel({ scope, reused }: { scope?: ReviewScope; reused: number }) {
  const { wid, data, reload } = useApp();
  const toast = useToast();
  const [plan, setPlan] = useState<ReviewPlan | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [batch, setBatch] = useState<AiBatch | null>(null);
  const [batchSize, setBatchSize] = useState(10);

  async function checkWithAi() {
    if (!wid) return;
    try {
      let p = await api.aiCheckBatch(wid, batchSize);
      setBatch(p);
      while (p.running) {
        await new Promise((r) => setTimeout(r, 1200));
        p = await api.aiCheckBatchProgress(wid);
        setBatch(p);
      }
      if (p.error) toast(p.error, "error");
      else toast(`AI checked ${p.done}: ${Object.entries(p.verdicts).map(([v, n]) => `${n} ${v}`).join(", ") || "nothing to check"}`);
      await reload();
    } catch (e) { toast((e as Error).message, "error"); }
  }
  const running = !!data?.running;
  const unproven = scope?.unproven ?? 0;
  // Show the suggested files straight away. This ranking is free; the model is only asked on request.
  useEffect(() => {
    if (!wid || !unproven || running) return;
    let live = true;
    api.reviewPlan(wid, false).then((p) => {
      if (!live) return;
      setPlan(p);
      setChosen(new Set(p.files.filter((f) => f.selected).map((f) => f.file)));
    }).catch(() => {});
    return () => { live = false; };
  }, [wid, unproven, running, data?.revision]);
  if (!scope || !wid) return null;
  if (!scope.unproven && !plan) {
    return reused ? <div className="prbar"><ShieldCheck size={15} className="faint" /><span className="faint" style={{ fontSize: 13 }}>
      {reused} finding{reused === 1 ? "" : "s"} kept their earlier proof: nothing they depend on changed.</span></div> : null;
  }

  async function suggest(ai: boolean) {
    setLoading(true);
    try {
      const p = await api.reviewPlan(wid!, ai);
      setPlan(p);
      setChosen(new Set(p.files.filter((f) => f.selected).map((f) => f.file)));
    } catch (e) { toast((e as Error).message, "error"); }
    setLoading(false);
  }

  async function verify() {
    if (!chosen.size) return;
    await api.runReview(wid!, true, "", false, [...chosen]);
    await reload();
  }

  const toggle = (f: PlanFile) => setChosen((c) => { const n = new Set(c); n.has(f.file) ? n.delete(f.file) : n.add(f.file); return n; });

  return (
    <div className="prbar" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <div className="row wrap" style={{ gap: 10 }}>
        <ShieldCheck size={15} className="faint" />
        <span>
          Tests have not run on <strong>{scope.unproven}</strong> patch{scope.unproven === 1 ? "" : "es"} in {scope.unproven_files} file{scope.unproven_files === 1 ? "" : "s"}
          {scope.changed_count ? <> · {scope.changed_count} file{scope.changed_count === 1 ? "" : "s"} edited since they last ran</> : null}.
        </span>
        <span className="grow" />
        <span className="row" style={{ gap: 4 }} title="Have the model read the top unverified findings against the code; their verdicts reorder the list">
          <select className="select" style={{ height: 30, fontSize: 13 }} value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))} aria-label="How many to check">
            {[5, 10, 25, 50].map((n) => <option key={n} value={n}>top {n}</option>)}
          </select>
          <button className="btn sm" disabled={!!batch?.running} onClick={checkWithAi}>
            {batch?.running ? <Spinner size={13} /> : <Bot size={13} />}{batch?.running ? `AI checked ${batch.done}/${batch.total}` : "Check with AI"}
          </button>
        </span>
        <button className="btn sm" disabled={running || loading} onClick={() => suggest(true)}
          title="Ask your configured model which of these files are worth a test run">
          {loading ? <Spinner size={13} /> : <Sparkles size={13} />}{plan?.source === "ai" ? "Ask again" : "Ask AI to refine"}
        </button>
      </div>
      {plan && (
        <>
          <div className="faint" style={{ fontSize: 13 }}>
            {plan.source === "ai"
              ? <>Suggested by <code>{plan.note}</code>. Untick anything you don't want to run.</>
              : <>Files that need a test run, ranked by impact, edits and test coverage. Untick anything you don't want to run.</>}
          </div>
          <div style={{ maxHeight: 280, overflow: "auto", display: "grid", gap: 2 }}>
            {plan.files.map((f) => (
              <label key={f.file} className="row" style={{ gap: 8, padding: "4px 2px", cursor: "pointer", alignItems: "flex-start" }}>
                <input type="checkbox" checked={chosen.has(f.file)} onChange={() => toggle(f)} style={{ marginTop: 3 }} />
                <span style={{ minWidth: 0 }}>
                  <span className="mono truncate" style={{ display: "block" }}><FileCode2 size={12} /> {f.file}
                    {f.changed && <span className="badge amber" style={{ marginLeft: 6 }}>edited</span>}
                    {f.hot > 0 && <Flame size={12} style={{ marginLeft: 6 }} />}</span>
                  <span className="faint" style={{ fontSize: 12.5 }}>{f.reason}</span>
                </span>
              </label>
            ))}
          </div>
          <div className="row wrap" style={{ gap: 8 }}>
            <button className="btn primary sm" disabled={running || !chosen.size} onClick={verify}>
              <Check size={13} />Verify {chosen.size} file{chosen.size === 1 ? "" : "s"}
            </button>
            <button className="btn sm" onClick={() => setChosen(new Set(plan.files.map((f) => f.file)))}>All {plan.files.length}</button>
            <button className="btn sm" onClick={() => setChosen(new Set())}>None</button>
            {plan.source === "ranking" && plan.note && <span className="faint" style={{ fontSize: 12.5 }}>AI refine unavailable: {plan.note}.</span>}
          </div>
        </>
      )}
    </div>
  );
}
