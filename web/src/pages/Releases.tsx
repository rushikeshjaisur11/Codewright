import { useEffect, useState } from "react";
import { CircleCheck, Package, PackageMinus, RefreshCw, Sparkles, TriangleAlert, Wand2 } from "lucide-react";
import { useApp } from "../App";
import { api, DepAudit, Release } from "../api";
import { Empty, Inline, Spinner, Tabs, useToast } from "../ui";

const BUMP: Record<string, string> = { major: "red", minor: "amber", patch: "blue", current: "green", unknown: "outline" };

function topicCounts(unlocks: Release["unlocks"]): string {
  const counts = new Map<string, number>();
  unlocks.forEach((u) => counts.set(u.topic || "Other", (counts.get(u.topic || "Other") ?? 0) + 1));
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([t, n]) => `${t} ${n}`).join(", ");
}

export default function Releases() {
  const { params, go } = useApp();
  const tab = params.get("tab") === "audit" ? "audit" : "releases";
  const tabs = (
    <Tabs value={tab} onChange={(t) => go("releases", { tab: t })}
      options={[{ value: "releases", label: "Releases" }, { value: "audit", label: "Audit" }]} />
  );
  return tab === "audit" ? <Audit tabs={tabs} /> : <ReleaseList tabs={tabs} />;
}

const KIND: Record<string, { label: string; badge: string }> = {
  overlap: { label: "Two for one job", badge: "blue" },
  stdlib: { label: "Standard library", badge: "teal" },
  unused: { label: "Unused", badge: "amber" },
  heavy: { label: "Heavy", badge: "outline" },
  license: { label: "Licence", badge: "red" },
  abandoned: { label: "Abandoned", badge: "red" },
};

function Audit({ tabs }: { tabs: JSX.Element }) {
  const { wid, data } = useApp();
  const toast = useToast();
  const [audit, setAudit] = useState<DepAudit | null>(null);
  const [loading, setLoading] = useState(false);
  const load = (online: boolean) => {
    if (!wid) return;
    setLoading(true);
    api.deps(wid, online).then(setAudit).catch((e) => toast(e.message, "error")).finally(() => setLoading(false));
  };
  useEffect(() => { setAudit(null); load(false); }, [wid, data?.last_run]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="page grid">
      <div className="page-head">
        <div><h1>Dependency audit</h1><p>What the project declares against what the code imports: libraries to merge, drop or replace with the standard library, and what each change takes out of the install.</p></div>
        <div className="actions">
          {tabs}
          <button className="btn" onClick={() => load(true)} disabled={loading} title="Also ask the package index when each dependency last released">
            {loading ? <Spinner size={14} /> : <RefreshCw size={14} />}Check release dates
          </button>
        </div>
      </div>
      {!audit ? <div className="card card-pad"><Spinner /></div> : (
        <>
          <div className="card">
            <div className="card-head">
              <h2><PackageMinus size={16} />{audit.findings.length ? `${audit.findings.length} thing(s) to look at` : "Nothing to change"}</h2>
              <div className="right faint" style={{ fontSize: 12.5 }}>
                {audit.declared} declared · {audit.locked} locked{audit.lockfile ? ` (${audit.lockfile})` : ""} · Python {audit.python}
                {audit.could_remove ? ` · ${audit.could_remove} package(s) could leave the install` : ""}
              </div>
            </div>
            {audit.findings.length > 0 && (
              <div className="table-scroll"><table className="table">
                <tbody>
                  {audit.findings.map((f, i) => (
                    <tr key={i}>
                      <td style={{ whiteSpace: "nowrap" }}><span className={`badge ${KIND[f.kind].badge}`}>{KIND[f.kind].label}</span></td>
                      <td style={{ width: "100%" }}>
                        <div style={{ fontWeight: 600 }}><Inline text={f.title} /></div>
                        <div className="muted" style={{ fontSize: 13, marginTop: 2 }}><Inline text={f.detail} /></div>
                        <div style={{ fontSize: 13, marginTop: 4 }}><Inline text={f.suggestion} /></div>
                      </td>
                      <td className="mono muted" style={{ whiteSpace: "nowrap" }}>{f.saves ? `−${f.saves} pkg` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>
          <div className="card">
            <div className="card-head"><h2><Package size={16} />Declared dependencies</h2></div>
            <div className="table-scroll"><table className="table">
              <thead><tr><th>Package</th><th>Version</th><th>Group</th><th>Imported by</th><th>Pulls in</th><th>Licence</th><th>Last release</th></tr></thead>
              <tbody>
                {audit.dependencies.map((d) => (
                  <tr key={d.name}>
                    <td className="mono">{d.name}{d.flags.map((f) => <span key={f} className={`badge ${KIND[f]?.badge ?? "outline"}`} style={{ marginLeft: 6 }}>{KIND[f]?.label ?? f}</span>)}</td>
                    <td className="mono">{d.version ?? <span className="faint">unpinned</span>}</td>
                    <td className="faint">{d.group}</td>
                    <td className="mono">{d.files}{d.test_files ? <span className="faint"> +{d.test_files} test</span> : null}</td>
                    <td className="mono">{d.requires ? `${d.requires}${d.unique ? ` (${d.unique} only via this)` : ""}` : <span className="faint">—</span>}</td>
                    <td className="faint" style={{ fontSize: 12.5 }}>{d.license || "—"}</td>
                    <td className="faint">{d.released || "—"}</td>
                  </tr>
                ))}
                {!audit.dependencies.length && <tr><td colSpan={7} className="muted">No declared dependencies (pyproject.toml or requirements files).</td></tr>}
              </tbody>
            </table></div>
          </div>
        </>
      )}
    </div>
  );
}

function ReleaseList({ tabs }: { tabs: JSX.Element }) {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [rows, setRows] = useState<Release[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // Refreshes keep the current rows on screen; only the first load shows a spinner.
  const load = () => {
    if (!wid) return;
    setRefreshing(true);
    api.releases(wid).then(setRows).catch((e) => { toast(e.message, "error"); setRows((r) => r ?? []); })
      .finally(() => setRefreshing(false));
  };
  useEffect(() => setRows(null), [wid]);
  useEffect(load, [wid, data?.has_result]); // eslint-disable-line react-hooks/exhaustive-deps

  const fresh = (rows ?? []).filter((r) => r.new_since_last_check);
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Release watcher</h1><p>New releases of your dependencies: what each upgrade unlocks, and a preview of what it would change in this repo's code.</p></div>
        <div className="actions">{tabs}<button className="btn" onClick={load} disabled={refreshing}>{refreshing ? <Spinner size={14} /> : <RefreshCw size={14} />}Check PyPI</button></div>
      </div>
      {fresh.length > 0 && (
        <div className="callout amber row" style={{ marginBottom: 14 }}>
          <Sparkles size={16} />
          <span className="grow">{fresh.length} new release{fresh.length > 1 ? "s" : ""} since the last check: {fresh.map((r) => `${r.name} ${r.latest}`).join(", ")}. A re-scan can catch new deprecations.</span>
          <button className="btn sm" onClick={() => runReview(true)}>Re-scan</button>
        </div>
      )}
      <div className="card">
        {rows == null ? <div className="card-pad"><Spinner /></div> : rows.length === 0 ? (
          <Empty icon={<Package size={22} />} title="No dependencies">Add a lockfile, requirements file or pyproject dependencies to this repo.</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>Package</th><th>Installed</th><th>Latest</th><th>Released</th><th>Change</th><th>Upgrade preview for this repo</th><th>Upgrade unlocks</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name}>
                  <td className="mono">{r.name}</td>
                  <td className="mono">{r.installed ?? <span className="faint">unpinned</span>}</td>
                  <td className="mono">{r.latest ?? <span className="faint">offline</span>}</td>
                  <td className="faint">{r.released ?? ""}</td>
                  <td><span className={`badge ${BUMP[r.bump]}`}>{r.bump}</span>{r.new_since_last_check && <span className="badge amber" style={{ marginLeft: 4 }}>new</span>}</td>
                  <td style={{ minWidth: 260 }}>
                    {!r.preview ? <span className="faint">—</span> : (r.preview.new.length + r.preview.resolved.length === 0
                      ? <span className="faint">No change to findings</span>
                      : <div className="col" style={{ gap: 3, fontSize: 13 }}>
                          {r.preview.new.map((n) => (
                            <div key={n.card + n.file + n.line} className="row" title={`${n.file}:${n.line}`}>
                              <TriangleAlert size={12} color="var(--amber)" />To fix: {n.title}
                              {n.autofix && <span className="badge blue" title="Comes with an auto-patch"><Wand2 size={10} /></span>}
                            </div>
                          ))}
                          {r.preview.resolved.map((n) => (
                            <div key={n.card + n.file + n.line} className="row faint"><CircleCheck size={12} color="var(--green)" />Resolved: {n.title}</div>
                          ))}
                        </div>)}
                  </td>
                  <td>{r.unlocks.length === 0 ? <span className="faint">—</span> : r.unlocks.length <= 4 ? r.unlocks.map((u) => (
                    <div key={u.card} className="row" style={{ fontSize: 13 }}>
                      <Sparkles size={12} color="var(--accent)" />{u.title}
                      {u.reference && <span className="badge outline" style={{ height: 18, fontSize: 11 }}>reference</span>}
                    </div>
                  )) : (
                    <details style={{ fontSize: 13 }}>
                      <summary style={{ cursor: "pointer" }}>
                        <Sparkles size={12} color="var(--accent)" style={{ verticalAlign: -1, marginRight: 4 }} />
                        {r.unlocks.length} capabilities: {topicCounts(r.unlocks)}
                      </summary>
                      <div className="col" style={{ gap: 2, marginTop: 6 }}>
                        {r.unlocks.map((u) => <div key={u.card} className="faint">{u.title}</div>)}
                      </div>
                    </details>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
