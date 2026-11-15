import { useEffect, useState } from "react";
import { AlertCircle, ArrowRight, CheckCircle2, Compass, FileCode2, Flame, GitPullRequest, Layers, ShieldCheck, ThumbsUp, XCircle } from "lucide-react";
import { useApp } from "../App";
import { api, Drift, Release } from "../api";
import { Empty, ProofBadge, Spinner, Stat, intentLabel } from "../ui";

export default function Overview() {
  const { data, go, wid } = useApp();
  const [drift, setDrift] = useState<Drift | null | undefined>(undefined);
  const [releases, setReleases] = useState<Release[] | null>(null);
  const r = data?.result;

  useEffect(() => {
    if (!wid || !r) return;
    api.mapMeta(wid).then((m) => setDrift(m.drift)).catch(() => setDrift(null));
    api.releases(wid).then(setReleases).catch(() => setReleases([]));
  }, [wid, r]);

  if (!data) return <Empty icon={<Spinner />} title="Loading" />;
  if (!r) return (
    <Empty icon={data.running ? <Spinner /> : <GitPullRequest size={22} />} title={data.running ? "Reviewing the repository" : "No review yet"}>
      {data.running ? "Discovering the stack, building the code graph, matching intents to capability cards and running proofs." : "Run a review to see findings."}
    </Empty>
  );

  const m = r.metrics;
  const byIntent = Object.entries(r.findings.reduce<Record<string, number>>((a, f) => ({ ...a, [f.intent]: (a[f.intent] ?? 0) + 1 }), {}))
    .sort((a, b) => b[1] - a[1]);
  const maxIntent = Math.max(1, ...byIntent.map(([, n]) => n));
  const upgrades = (releases ?? []).filter((x) => x.bump === "major" || x.bump === "minor");

  return (
    <div className="grid page">
      <div className="page-head">
        <div>
          <h1>{data.name}</h1>
          <p className="mono faint" style={{ fontSize: 12.5 }}>{data.root}</p>
        </div>
      </div>

      <div className="stats">
        <Stat label="Findings shown" icon={<AlertCircle size={14} />} value={m.shown}
          sub={`${r.intent_matches} intent matches · ${m.dropped} dropped by the critic`} />
        <Stat label="Proven by tests" icon={<ShieldCheck size={14} />} value={m.verified}
          sub={m.with_patch ? `${m.with_patch} with an auto-patch` : "no auto-patches this run"}
          meter={m.verified_share} />
        <Stat label="Acceptance rate" icon={<ThumbsUp size={14} />}
          value={m.acceptance_rate == null ? "—" : `${Math.round(m.acceptance_rate * 100)}%`}
          sub={`${m.accepted} accepted · ${m.rejected} rejected · target 60%+`} meter={m.acceptance_rate}
          color={m.acceptance_rate == null ? undefined : m.acceptance_rate >= 0.6 ? "var(--green)" : "var(--amber)"} />
        <Stat label="On the hot path" icon={<Flame size={14} />} value={m.hot_path} sub="reachable from a route, CLI or main" />
        <Stat label="Code graph" icon={<FileCode2 size={14} />} value={r.functions}
          sub={`functions in ${r.files} files · ${r.entry_points} entry points · ${r.seconds}s`} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(0, 1fr)" }}>
        <div className="card">
          <div className="card-head">
            <h2><GitPullRequest size={16} />Top findings</h2>
            <div className="right"><button className="btn sm ghost" onClick={() => go("review")}>All findings<ArrowRight size={13} /></button></div>
          </div>
          {r.findings.length === 0 ? (
            <div className="card-pad muted">No findings. Nothing in this repo matched a capability card with proof-ready advice.</div>
          ) : (
            <table className="table">
              <tbody>
                {r.findings.slice(0, 6).map((f) => (
                  <tr key={f.id} className="click" onClick={() => go("review", { f: f.id })}>
                    <td style={{ width: "100%" }}>
                      <div style={{ fontWeight: 600 }}>{f.title}</div>
                      <div className="mono faint" style={{ fontSize: 11.5 }}>{f.file}:{f.line}</div>
                    </td>
                    <td><ProofBadge proof={f.proof} /></td>
                    <td className="mono muted" style={{ textAlign: "right" }}>{f.score.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="col" style={{ gap: 16 }}>
          <div className="card">
            <div className="card-head"><h2><Layers size={16} />Findings by intent</h2></div>
            <div className="card-pad col" style={{ gap: 10 }}>
              {byIntent.length === 0 && <span className="muted">None</span>}
              {byIntent.map(([intent, n]) => (
                <div key={intent}>
                  <div className="row" style={{ fontSize: 13 }}><span className="grow">{intentLabel(intent)}</span><span className="mono muted">{n}</span></div>
                  <div className="meter" style={{ marginTop: 4 }}><span style={{ width: `${(n / maxIntent) * 100}%` }} /></div>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2><Compass size={16} />Architecture drift</h2>
              <div className="right"><button className="btn sm ghost" onClick={() => go(drift ? "map" : "design")}>{drift ? "Open map" : "Design"}<ArrowRight size={13} /></button></div>
            </div>
            <div className="card-pad">
              {drift === undefined ? <Spinner /> : drift === null ? (
                <span className="muted">No intended architecture yet. Create one in Design mode and save it to this repo to check drift on every review.</span>
              ) : drift.ok ? (
                <div className="row"><CheckCircle2 size={16} color="var(--green)" /><span>Code matches the <b>{drift.design}</b> design.</span></div>
              ) : (
                <div className="col">
                  <div className="row"><XCircle size={16} color="var(--red)" /><b>Drift from {drift.design}</b></div>
                  {drift.unplanned.map((u) => <div key={u.kind} className="muted">Unplanned {u.kind}: {u.systems.join(", ")}</div>)}
                  {drift.violations.map((v, i) => <div key={i} className="muted mono" style={{ fontSize: 12 }}>{v.file}: {v.rule}</div>)}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)" }}>
        <div className="card">
          <div className="card-head">
            <h2><Layers size={16} />Stack</h2>
            <div className="right"><span className="badge outline">Python {r.stack.python ?? "?"}</span></div>
          </div>
          <table className="table">
            <thead><tr><th>Package</th><th>Version</th><th>Read from</th></tr></thead>
            <tbody>
              {r.stack.dependencies.slice(0, 12).map((d) => (
                <tr key={d.name}>
                  <td className="mono">{d.name}</td>
                  <td className="mono">{d.version ?? <span className="faint">{d.specifier ?? "unpinned"}</span>}</td>
                  <td className="faint">{d.source}</td>
                </tr>
              ))}
              {!r.stack.dependencies.length && <tr><td colSpan={3} className="muted">No dependencies found. Add a lockfile or requirements file.</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="card">
          <div className="card-head">
            <h2><Flame size={16} />Upgrades worth a look</h2>
            <div className="right"><button className="btn sm ghost" onClick={() => go("releases")}>All releases<ArrowRight size={13} /></button></div>
          </div>
          {releases == null ? <div className="card-pad"><Spinner /></div> : upgrades.length === 0 ? (
            <div className="card-pad muted">Every pinned dependency is on its latest major and minor release, or PyPI couldn't be reached.</div>
          ) : (
            <table className="table">
              <tbody>
                {upgrades.slice(0, 8).map((u) => (
                  <tr key={u.name}>
                    <td className="mono">{u.name}</td>
                    <td className="mono muted">{u.installed} → {u.latest}</td>
                    <td><span className={`badge ${u.bump === "major" ? "red" : "amber"}`}>{u.bump}</span></td>
                    <td className="muted" style={{ fontSize: 12.5 }}>{u.unlocks.length ? `unlocks: ${u.unlocks.map((x) => x.title).join("; ")}` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
