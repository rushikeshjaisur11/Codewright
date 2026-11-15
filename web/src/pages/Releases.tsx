import { useEffect, useState } from "react";
import { Package, RefreshCw, Sparkles } from "lucide-react";
import { useApp } from "../App";
import { api, Release } from "../api";
import { Empty, Spinner, useToast } from "../ui";

const BUMP: Record<string, string> = { major: "red", minor: "amber", patch: "blue", current: "green", unknown: "outline" };

export default function Releases() {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [rows, setRows] = useState<Release[] | null>(null);
  const load = () => { if (wid) { setRows(null); api.releases(wid).then(setRows).catch((e) => { toast(e.message, "error"); setRows([]); }); } };
  useEffect(load, [wid, data?.has_result]); // eslint-disable-line react-hooks/exhaustive-deps

  const fresh = (rows ?? []).filter((r) => r.new_since_last_check);
  return (
    <div>
      <div className="page-head">
        <div><h1>Release watcher</h1><p>New releases of your dependencies, and the capability cards an upgrade would unlock.</p></div>
        <div className="actions"><button className="btn" onClick={load}><RefreshCw size={14} />Check PyPI</button></div>
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
            <thead><tr><th>Package</th><th>Installed</th><th>Latest</th><th>Released</th><th>Change</th><th>Upgrade unlocks</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name}>
                  <td className="mono">{r.name}</td>
                  <td className="mono">{r.installed ?? <span className="faint">unpinned</span>}</td>
                  <td className="mono">{r.latest ?? <span className="faint">offline</span>}</td>
                  <td className="faint">{r.released ?? ""}</td>
                  <td><span className={`badge ${BUMP[r.bump]}`}>{r.bump}</span>{r.new_since_last_check && <span className="badge amber" style={{ marginLeft: 4 }}>new</span>}</td>
                  <td>{r.unlocks.length ? r.unlocks.map((u) => <div key={u.card} className="row" style={{ fontSize: 13 }}><Sparkles size={12} color="var(--accent)" />{u.title}</div>) : <span className="faint">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
