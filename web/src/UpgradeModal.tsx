import { useEffect, useRef, useState } from "react";
import { ArrowUpCircle, Bot, FlaskConical, GitBranchPlus, ShieldCheck, Wand2 } from "lucide-react";
import { useApp } from "./App";
import { api, UpgradePlan, UpgradeSafety } from "./api";
import { DiffView, Inline, Modal, Spinner, useToast } from "./ui";

/**
 * Upgrade one dependency with its code changes: the requirement edit, the lockfile, the patches the new version
 * calls for (ticked), what to do by hand, and what the release notes mean for this repo. Nothing is committed.
 */
export default function UpgradeModal({ name, version, onClose }: { name: string; version: string; onClose: () => void }) {
  const { wid, runReview, go } = useApp();
  const toast = useToast();
  const [plan, setPlan] = useState<UpgradePlan | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [branch, setBranch] = useState({ on: true, name: `codewright/upgrade-${name}-${version}` });
  const [runLock, setRunLock] = useState(true);
  const [busy, setBusy] = useState(false);
  const [breaking, setBreaking] = useState<{ items: { file: string; change: string; why: string; version: string }[]; model: string; links: { label: string; url: string }[] } | null>(null);
  const [reading, setReading] = useState(false);
  const [safety, setSafety] = useState<UpgradeSafety | null>(null);
  const [rewrites, setRewrites] = useState<Set<string>>(new Set());
  const poll = useRef<number | null>(null);
  const key = `${name}==${version}`;
  const mine = safety && safety.key === key ? safety : null;

  useEffect(() => {
    if (!wid) return;
    api.upgradeSafetyProgress(wid).then((s) => { setSafety(s); if (s.running) watch(); }).catch(() => {});
    return () => { if (poll.current) window.clearInterval(poll.current); };
  }, [wid]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {  // tick every rewrite as it arrives
    if (mine?.rewrite) setRewrites(new Set(mine.rewrite.patches.map((p) => p.id)));
  }, [mine?.rewrite]); // eslint-disable-line react-hooks/exhaustive-deps

  function watch() {
    if (!wid || poll.current) return;
    poll.current = window.setInterval(async () => {
      try {
        const s = await api.upgradeSafetyProgress(wid);
        setSafety(s);
        if (!s.running && poll.current) { window.clearInterval(poll.current); poll.current = null; if (s.error) toast(s.error, "error"); }
      } catch { /* the next tick tries again */ }
    }, 1200);
  }

  const chosenRewrites = () => (mine?.rewrite?.patches ?? []).filter((p) => rewrites.has(p.id)).map((p) => p.patch);

  async function safetyStep(step: "check" | "rewrite" | "agent" | "trial") {
    if (!wid) return;
    try { setSafety(await api.upgradeSafety(wid, step, name, version, step === "trial" ? chosenRewrites() : [])); watch(); }
    catch (e) { toast((e as Error).message, "error"); }
  }

  useEffect(() => {
    if (!wid) return;
    api.upgradePlan(wid, name, version).then((p) => { setPlan(p); setChosen(new Set(p.patches.map((x) => x.id))); })
      .catch((e) => toast((e as Error).message, "error"));
  }, [wid, name, version]); // eslint-disable-line react-hooks/exhaustive-deps

  async function apply() {
    if (!wid || !plan) return;
    setBusy(true);
    try {
      const r = await api.upgradeApply(wid, { name, version, patch_ids: [...chosen], branch: branch.on ? branch.name : "", run_lock: runLock, extra_patches: chosenRewrites() });
      toast(`Upgraded ${name} to ${version}${r.branch ? ` on ${r.branch}` : ""}: ${r.files.length} file(s) changed${r.rewrites_failed?.length ? `; ${r.rewrites_failed.length} rewrite(s) did not apply${r.rewrite_errors?.[0] ? ` (${r.rewrite_errors[0].why.split("\n")[0].slice(0, 160)})` : ""}` : ""}${r.lock && !r.lock.ok ? ` (lockfile: ${r.lock.output.slice(0, 120)})` : ""}. Re-scanning; then run the tests and commit from Review.`);
      onClose();
      await runReview(false);
      go("review");
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  }

  async function readNotes() {
    if (!wid) return;
    setReading(true);
    try { setBreaking(await api.upgradeBreaking(wid, name, version)); } catch (e) { toast((e as Error).message, "error"); }
    setReading(false);
  }

  return (
    <Modal title={`Upgrade ${name} ${plan?.installed ?? ""} → ${version}`} onClose={onClose} footer={<>
      <span className="faint" style={{ fontSize: 12.5, marginRight: "auto" }}>Nothing is committed: review the change, run the tests, then commit from the Review page.</span>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!plan || busy} onClick={apply}>{busy ? <Spinner size={14} /> : <ArrowUpCircle size={14} />}Upgrade</button>
    </>}>
      {!plan ? <Spinner /> : (
        <div className="col" style={{ gap: 14 }}>
          <section className="col" style={{ gap: 6 }}>
            <b>1. The requirement</b>
            {plan.requirements.length === 0
              ? <div className="faint" style={{ fontSize: 13 }}>{name} has no version written down: the lockfile decides.</div>
              : plan.requirements.map((r) => <DiffView key={r.file} patch={r.diff} />)}
          </section>
          <section className="col" style={{ gap: 6 }}>
            <b>2. The lockfile</b>
            <div style={{ fontSize: 13 }}>{plan.lock.note}{plan.lock.command.length ? <>: <code>{plan.lock.command.join(" ")}</code></> : null}</div>
            {plan.lock.command.length > 0 && (
              <label className="row" style={{ gap: 6, fontSize: 13 }}>
                <input type="checkbox" checked={runLock && plan.lock.available} disabled={!plan.lock.available} onChange={(e) => setRunLock(e.target.checked)} />
                {plan.lock.available ? "Run it now" : `${plan.lock.tool} isn't installed here: run it yourself afterwards`}
              </label>
            )}
          </section>
          <section className="col" style={{ gap: 6 }}>
            <b>3. Code changes the new version calls for</b>
            {plan.patches.length === 0 && plan.manual.length === 0 && <div className="faint" style={{ fontSize: 13 }}>None found by the detectors at {version}. Read the release notes below for anything else.</div>}
            {plan.patches.map((p) => (
              <label key={p.id} className="row" style={{ gap: 8, fontSize: 13, alignItems: "flex-start" }}>
                <input type="checkbox" checked={chosen.has(p.id)} onChange={() => { const n = new Set(chosen); n.has(p.id) ? n.delete(p.id) : n.add(p.id); setChosen(n); }} style={{ marginTop: 3 }} />
                <span><Wand2 size={12} /> <b>{p.title}</b> <span className="mono faint">{p.file}:{p.line}</span></span>
              </label>
            ))}
            {plan.manual.map((p) => (
              <div key={p.id} style={{ fontSize: 13 }}>✋ <b>{p.title}</b> <span className="mono faint">{p.file}:{p.line}</span> <span className="muted">— by hand: <Inline text={p.suggestion} /></span></div>
            ))}
            {plan.resolved.length > 0 && <div className="faint" style={{ fontSize: 13 }}>Goes away at {version}: {plan.resolved.map((r) => r.title).join("; ")}</div>}
            {plan.migrations.length > 0 && <div style={{ fontSize: 13 }}>A planned migration exists for this: <button className="btn ghost sm" onClick={() => { onClose(); go("plans"); }}>{plan.migrations[0].title}</button></div>}
          </section>
          <section className="col" style={{ gap: 6 }}>
            <b>4. What else the release notes say</b>
            <div className="row wrap" style={{ gap: 8 }}>
              <button className="btn sm" disabled={reading} onClick={readNotes}>{reading ? <Spinner size={13} /> : <Bot size={13} />}Ask AI what this breaks here</button>
              <span className="faint" style={{ fontSize: 12.5 }}>reads the notes between your version and {version} against the {plan.uses.length} file(s) that import {name}</span>
            </div>
            {breaking && (breaking.items.length === 0
              ? <div className="faint" style={{ fontSize: 13 }}>{breaking.model}: nothing in the notes touches what this repo uses.</div>
              : <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>{breaking.items.map((b, i) => (
                  <li key={i}><b><Inline text={b.change} /></b>{b.file && <span className="mono faint"> {b.file}</span>} <span className="muted"><Inline text={b.why} /></span>{b.version && <span className="faint"> ({b.version})</span>}</li>
                ))}</ul>)}
            {breaking?.links.map((l) => <a key={l.url} href={l.url} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>{l.label}</a>)}
          </section>
          <section className="col" style={{ gap: 8 }} aria-label="Make sure nothing breaks">
            <b><ShieldCheck size={14} /> 5. Make sure nothing breaks</b>
            <div className="faint" style={{ fontSize: 12.5 }}>
              Installs {name} {version} on its own and checks every use in this repo against its real signatures; writes the code changes for
              what breaks; then compares today&apos;s code with a copy that has the upgrade and those changes: every module that imports {name}
              is imported, the functions that use it are called with the same inputs (AI writes the inputs, today&apos;s code gives the
              expected answers, so no tests are needed) and your tests run if you have any. Your repo is not touched.
            </div>
            <div className="row wrap" style={{ gap: 8 }}>
              <button className="btn sm" disabled={!!mine?.running || !!safety?.running} onClick={() => safetyStep("check")}>
                {mine?.running && mine.step === "check" ? <Spinner size={13} /> : <ShieldCheck size={13} />}1. Check every use
              </button>
              <button className="btn sm" disabled={!(mine?.check?.problems.some((p) => p.issue !== "changed") || mine?.trial?.behaviour?.changed?.length) || !!safety?.running} onClick={() => safetyStep("rewrite")}>
                {mine?.running && mine.step === "rewrite" ? <Spinner size={13} /> : <Bot size={13} />}2. Write the code changes with AI
              </button>
              <button className="btn sm" title="The agent reads the code, looks up the new version's docs and changelog on the web, edits a sandbox copy and runs your tests"
                disabled={!(mine?.check?.problems.some((p) => p.issue !== "changed") || mine?.trial?.behaviour?.changed?.length) || !!safety?.running} onClick={() => safetyStep("agent")}>
                {mine?.running && mine.step === "agent" ? <Spinner size={13} /> : <Wand2 size={13} />}…or hand them to the AI agent
              </button>
              <button className="btn sm" disabled={!!safety?.running} onClick={() => safetyStep("trial")}>
                {mine?.running && mine.step === "trial" ? <Spinner size={13} /> : <FlaskConical size={13} />}3. Compare before and after
              </button>
            </div>
            {safety?.running && !mine && <div className="faint" style={{ fontSize: 12.5 }}>Busy with {safety.key}: wait for it to finish.</div>}
            {mine && mine.log.length > 0 && (
              <div className="mono faint" style={{ fontSize: 12 }}>{mine.log.slice(-3).map((l, i) => <div key={i}>{l}</div>)}</div>
            )}
            {mine?.check && (
              <div className="col" style={{ gap: 4, fontSize: 13 }}>
                <div><b>{mine.check.breaking ? "⚠" : "✓"} {mine.check.summary}</b></div>
                {mine.check.problems.length > 0 && (
                  <table className="table" style={{ fontSize: 12.5 }}>
                    <thead><tr><th>Where</th><th>Uses</th><th>At {version}</th></tr></thead>
                    <tbody>{mine.check.problems.slice(0, 60).map((p, i) => (
                      <tr key={i}>
                        <td className="mono">{p.file}:{p.line}{p.function ? <span className="faint"> {p.function}</span> : null}</td>
                        <td className="mono">{p.symbol}</td>
                        <td><b>{p.issue}</b> <span className="muted">{p.detail}</span>
                          {p.new_signature && <div className="mono faint" style={{ fontSize: 11.5 }}>now: {p.new_signature}</div>}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                )}
              </div>
            )}
            {mine?.rewrite && (
              <div className="col" style={{ gap: 6, fontSize: 13 }}>
                <div className="faint">{mine.rewrite.model}: {mine.rewrite.patches.length} change(s){mine.rewrite.agent ? ` by the AI agent in ${mine.rewrite.agent.steps_used} step(s)` : ", each checked to compile"}. Ticked ones go into the test run and the upgrade.</div>
                {mine.rewrite.agent && (
                  <details><summary className="faint" style={{ cursor: "pointer" }}>What the agent did</summary>
                    <div className="mono faint" style={{ fontSize: 12 }}>{mine.rewrite.agent.steps.filter((e) => e.kind === "tool").map((e, i) => <div key={i}>{e.ok ? "✓" : "✗"} {e.tool} {e.detail}</div>)}</div>
                  </details>
                )}
                {mine.rewrite.patches.map((p) => (
                  <div key={p.id} className="col" style={{ gap: 4 }}>
                    <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                      <input type="checkbox" checked={rewrites.has(p.id)} style={{ marginTop: 3 }}
                        onChange={() => { const n = new Set(rewrites); n.has(p.id) ? n.delete(p.id) : n.add(p.id); setRewrites(n); }} />
                      <span><Wand2 size={12} /> <span className="mono">{p.file}</span> <b>{p.function}</b> <span className="muted"><Inline text={p.explanation} /></span></span>
                    </label>
                    <DiffView patch={p.patch} />
                  </div>
                ))}
                {mine.rewrite.skipped.map((k, i) => <div key={i}>✋ <span className="mono">{k.file}</span> {k.function}: <span className="muted">{k.why}</span></div>)}
              </div>
            )}
            {mine?.trial && <TrialResult t={mine.trial} probes={mine.probes} version={version} />}
          </section>
          <section className="col" style={{ gap: 6 }}>
            <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={branch.on} onChange={(e) => setBranch({ ...branch, on: e.target.checked })} /><GitBranchPlus size={13} /><b>On a new branch</b></label>
            {branch.on && <input className="input mono" value={branch.name} onChange={(e) => setBranch({ ...branch, name: e.target.value })} aria-label="Upgrade branch name" />}
          </section>
        </div>
      )}
    </Modal>
  );
}

function show(v: unknown): string {
  const s = JSON.stringify(v);
  return s === undefined ? "nothing" : s.length > 220 ? s.slice(0, 220) + "…" : s;
}

/** What the before/after comparison found: imports, behaviour of the functions that use the library, tests. */
function TrialResult({ t, probes, version }: { t: NonNullable<UpgradeSafety["trial"]>; probes: UpgradeSafety["probes"]; version: string }) {
  if (t.error && !t.evidence?.length) return <div className="muted" style={{ fontSize: 13 }}>{t.error}</div>;
  const changed = t.behaviour?.changed ?? [];
  return (
    <div className="col" style={{ gap: 6, fontSize: 13 }}>
      <div><b>{t.safe ? "✓ Safe to upgrade" : "⚠ Not safe yet"}</b>
        {t.reasons?.length ? <span>: {t.reasons.join("; ")}</span> : t.evidence?.length ? <span className="muted">: {t.evidence.join(", ")}</span> : null}
        {t.patches_applied + t.patches_failed > 0 && <span className="faint"> (with {t.patches_applied} change(s){t.patches_failed ? `, ${t.patches_failed} did not apply` : ""})</span>}
      </div>
      {t.imports && (
        <div>Imports: {t.imports.broken.length === 0
          ? <span className="muted">all {t.imports.modules} module(s) that use it still import at {version}</span>
          : t.imports.broken.map((b) => <div key={b.module}>✗ <code>{b.module}</code> <span className="muted">{b.error}</span></div>)}
        </div>
      )}
      {t.behaviour && (t.behaviour.error
        ? <div className="muted">Behaviour: {t.behaviour.error}</div>
        : <div className="col" style={{ gap: 3 }}>
            <div>Behaviour: {changed.length === 0
              ? <span className="muted">{t.behaviour.scenarios} call(s) in {t.behaviour.functions} function(s) gave the same result before and after</span>
              : <b>{changed.length} of {t.behaviour.scenarios} call(s) changed</b>}</div>
            {changed.slice(0, 20).map((c) => (
              <div key={c.scenario} className="mono" style={{ fontSize: 12 }}>
                <b>{c.scenario}</b><div className="faint">today: {show(c.before)}</div><div>after: {show(c.after)}</div>
              </div>
            ))}
            {changed.length > 0 && <div className="faint">Write the code changes with AI uses these to keep today&apos;s behaviour.</div>}
            {(t.behaviour.probe_errors ?? []).length > 0 && <div className="faint">Probes that could not start: {(t.behaviour.probe_errors ?? []).join(", ")}</div>}
          </div>)}
      {!t.behaviour && probes?.why && <div className="faint">Behaviour not compared: {probes.why}</div>}
      {probes && probes.probes.length > 0 && (
        <details><summary className="faint">The calls compared ({probes.model})</summary>
          {probes.probes.map((p) => <pre key={p.function} className="mono" style={{ fontSize: 11.5, whiteSpace: "pre-wrap" }}>{`# ${p.file}: ${p.function}\n${p.code}`}</pre>)}
        </details>
      )}
      {t.tests_ran && t.before && t.after ? (<>
        <div>Tests: today {t.before.passed} passed, {t.before.failed.length} failing → after {t.after.passed} passed, {t.after.failed.length} failing</div>
        {(t.new_failures ?? []).length > 0 && <div>Newly failing: {(t.new_failures ?? []).map((x) => <code key={x} style={{ marginRight: 6 }}>{x}</code>)}</div>}
        {!t.safe && <details><summary className="faint">Test output after the upgrade</summary><pre className="mono" style={{ fontSize: 11.5, whiteSpace: "pre-wrap" }}>{t.after.output}</pre></details>}
      </>) : <div className="faint">Tests: none in this repo; the imports and behaviour above are the evidence.</div>}
    </div>
  );
}
