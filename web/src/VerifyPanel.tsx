import { useState } from "react";
import { Check, ShieldCheck, Sparkles, Flame, FileCode2 } from "lucide-react";
import { useApp } from "./App";
import { api, PlanFile, ReviewPlan, ReviewScope } from "./api";
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
  const running = !!data?.running;
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
    setPlan(null);
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
        {!plan && <button className="btn primary sm" disabled={running || loading} onClick={() => suggest(true)}>
          {loading ? <Spinner size={13} /> : <Sparkles size={13} />}Choose files to verify
        </button>}
      </div>
      {plan && (
        <>
          <div className="faint" style={{ fontSize: 13 }}>
            {plan.source === "ai"
              ? <>Suggested by <code>{plan.note}</code>. Untick anything you don't want to run.</>
              : <>Ranked by impact, edits and test coverage{plan.note ? <> (no model: {plan.note})</> : null}. Untick anything you don't want to run.</>}
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
            <button className="btn sm" onClick={() => setPlan(null)}>Cancel</button>
            {plan.source === "ranking" && <span className="faint" style={{ fontSize: 12.5 }}>Set up a model in Settings to have AI choose.</span>}
          </div>
        </>
      )}
    </div>
  );
}
