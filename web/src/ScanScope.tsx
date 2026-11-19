import { useEffect, useMemo, useState } from "react";
import { FolderTree, Play, Save } from "lucide-react";
import { useApp } from "./App";
import { api, ScanScope as Scope } from "./api";
import { Spinner, useToast } from "./ui";

/**
 * Which folders a review reads. Ticking a folder out adds it to `exclude`; free-form patterns cover the rest
 * (`**\/migrations`, `*_pb2.py`). Saved in .codewright/config.json so the team shares the same scope.
 */
export default function ScanScope() {
  const { wid, runReview, data } = useApp();
  const toast = useToast();
  const [scope, setScope] = useState<Scope | null>(null);
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [saving, setSaving] = useState(false);
  const [deep, setDeep] = useState(false);

  const load = () => wid && api.scanScope(wid).then((s) => { setScope(s); setInclude(s.include.join("\n")); setExclude(s.exclude.join("\n")); });
  useEffect(() => { setScope(null); load(); }, [wid]); // eslint-disable-line react-hooks/exhaustive-deps

  const lines = (t: string) => t.split("\n").map((x) => x.trim()).filter(Boolean);
  const excluded = useMemo(() => new Set(lines(exclude)), [exclude]);
  const folders = (scope?.folders ?? []).filter((f) => deep || f.depth === 1).sort((a, b) => b.files - a.files);

  function toggle(path: string) {
    const ex = lines(exclude);
    setExclude((excluded.has(path) ? ex.filter((x) => x !== path) : [...ex, path]).join("\n"));
  }

  async function save(rescan: boolean) {
    if (!wid) return;
    setSaving(true);
    try {
      const s = await api.saveScanScope(wid, lines(include), lines(exclude));
      setScope(s);
      toast(`${s.kept} of ${s.total} Python files will be reviewed`);
      if (rescan) await runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
    setSaving(false);
  }

  return (
    <div className="card">
      <div className="card-head"><h2><FolderTree size={16} />What gets reviewed</h2>
        <div className="right faint" style={{ fontSize: 12.5 }}>
          {scope ? `${scope.kept} of ${scope.total} Python files` : ""} · saved in <code>.codewright/config.json</code>
        </div>
      </div>
      {!scope ? <div className="card-pad"><Spinner /></div> : (
        <div className="card-pad col" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 13 }}>
            Leave out vendored packages, generated clients, migrations and anything else that isn't your code: every scan gets faster and
            the findings, maps and agent architecture are about what matters. Untick a folder, or write patterns below.
          </div>
          <div style={{ maxHeight: 260, overflow: "auto" }}>
            <table className="table">
              <tbody>
                {folders.map((f) => (
                  <tr key={f.path}>
                    <td style={{ width: 28 }}><input type="checkbox" checked={!excluded.has(f.path)} onChange={() => toggle(f.path)} aria-label={`Review ${f.path}`} /></td>
                    <td className="mono" style={{ paddingLeft: (f.depth - 1) * 16 }}>{f.path}</td>
                    <td className="mono muted" style={{ textAlign: "right" }}>{f.files} files</td>
                    <td className="faint" style={{ textAlign: "right", fontSize: 12 }}>{f.kept === f.files ? "" : f.kept ? `${f.kept} kept` : "left out"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <label className="row" style={{ gap: 6, fontSize: 13 }}><input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />Show sub-folders</label>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <label className="col" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 12.5 }}>Only these (one per line; empty means everything)</span>
              <textarea className="input mono" rows={4} value={include} onChange={(e) => setInclude(e.target.value)} placeholder={"services/api\nlibs/core"} />
            </label>
            <label className="col" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 12.5 }}>Leave out (one per line; <code>**/name</code> matches at any depth)</span>
              <textarea className="input mono" rows={4} value={exclude} onChange={(e) => setExclude(e.target.value)} placeholder={"vendor\n**/migrations\n*_pb2.py"} />
            </label>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn" disabled={saving} onClick={() => save(false)}>{saving ? <Spinner size={14} /> : <Save size={14} />}Save</button>
            <button className="btn primary" disabled={saving || !!data?.running} onClick={() => save(true)}><Play size={14} />Save and re-scan</button>
          </div>
        </div>
      )}
    </div>
  );
}
