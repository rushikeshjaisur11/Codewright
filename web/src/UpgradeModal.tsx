import { useEffect, useState } from "react";
import { ArrowUpCircle, Bot, GitBranchPlus, Wand2 } from "lucide-react";
import { useApp } from "./App";
import { api, UpgradePlan } from "./api";
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

  useEffect(() => {
    if (!wid) return;
    api.upgradePlan(wid, name, version).then((p) => { setPlan(p); setChosen(new Set(p.patches.map((x) => x.id))); })
      .catch((e) => toast((e as Error).message, "error"));
  }, [wid, name, version]); // eslint-disable-line react-hooks/exhaustive-deps

  async function apply() {
    if (!wid || !plan) return;
    setBusy(true);
    try {
      const r = await api.upgradeApply(wid, { name, version, patch_ids: [...chosen], branch: branch.on ? branch.name : "", run_lock: runLock });
      toast(`Upgraded ${name} to ${version}${r.branch ? ` on ${r.branch}` : ""}: ${r.files.length} file(s) changed${r.lock && !r.lock.ok ? ` (lockfile: ${r.lock.output.slice(0, 120)})` : ""}. Re-scanning; then run the tests and commit from Review.`);
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
          <section className="col" style={{ gap: 6 }}>
            <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={branch.on} onChange={(e) => setBranch({ ...branch, on: e.target.checked })} /><GitBranchPlus size={13} /><b>On a new branch</b></label>
            {branch.on && <input className="input mono" value={branch.name} onChange={(e) => setBranch({ ...branch, name: e.target.value })} aria-label="Upgrade branch name" />}
          </section>
        </div>
      )}
    </Modal>
  );
}
