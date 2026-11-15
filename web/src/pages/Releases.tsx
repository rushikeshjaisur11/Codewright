import { useEffect, useState } from "react";
import { CircleCheck, Package, RefreshCw, Sparkles, TriangleAlert, Wand2 } from "lucide-react";
import { useApp } from "../App";
import { api, Release } from "../api";
import { Empty, Spinner, useToast } from "../ui";

const BUMP: Record<string, string> = { major: "red", minor: "amber", patch: "blue", current: "green", unknown: "outline" };

function topicCounts(unlocks: Release["unlocks"]): string {
  const counts = new Map<string, number>();
  unlocks.forEach((u) => counts.set(u.topic || "Other", (counts.get(u.topic || "Other") ?? 0) + 1));
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([t, n]) => `${t} ${n}`).join(", ");
}

export default function Releases() {
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
        <div className="actions"><button className="btn" onClick={load} disabled={refreshing}>{refreshing ? <Spinner size={14} /> : <RefreshCw size={14} />}Check PyPI</button></div>
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
