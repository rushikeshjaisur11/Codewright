import { useEffect, useState } from "react";
import { Link2, Plus, Save, Trash2 } from "lucide-react";
import { useApp } from "./App";
import { api, AgentFixes as Fixes, MapNode } from "./api";
import { Spinner, useToast } from "./ui";

/**
 * What the code cannot show: agents built at run time, links made through config or over HTTP, boxes to hide.
 * Saved in .codewright/agents.yaml; added links are drawn dashed.
 */
export default function AgentFixes({ nodes, onSaved }: { nodes: MapNode[]; onSaved: () => void }) {
  const { wid } = useApp();
  const toast = useToast();
  const [fixes, setFixes] = useState<Fixes | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [label, setLabel] = useState("runs");
  const [agent, setAgent] = useState({ name: "", model: "" });
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (wid) api.agentFixes(wid).then(setFixes); }, [wid]);
  if (!fixes || !wid) return null;
  const labels = [...new Set(nodes.map((n) => n.label))].sort();

  async function save(next: Fixes) {
    setSaving(true);
    try {
      const out = await api.saveAgentFixes(wid!, { agents: next.agents, links: next.links, hide: next.hide });
      setFixes(out);
      onSaved();
      toast("Saved to .codewright/agents.yaml — commit it to share");
    } catch (e) { toast((e as Error).message, "error"); }
    setSaving(false);
  }

  return (
    <div className="card-pad col" style={{ gap: 10, borderTop: "1px solid var(--border)" }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <b><Link2 size={14} /> Corrections</b>
        <span className="faint" style={{ fontSize: 12.5 }}>for wiring the code can't show: agents loaded from config, calls to other services. Saved in <code>.codewright/agents.yaml</code>.</span>
      </div>
      <datalist id="cw-boxes">{labels.map((l) => <option key={l} value={l} />)}</datalist>
      <div className="row wrap" style={{ gap: 6 }}>
        <input className="input" list="cw-boxes" placeholder="From (a box, or a new name)" value={from} onChange={(e) => setFrom(e.target.value)} style={{ width: 220 }} aria-label="From" />
        <select className="select" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Link">
          {fixes.labels.map((l) => <option key={l}>{l}</option>)}
        </select>
        <input className="input" list="cw-boxes" placeholder="To" value={to} onChange={(e) => setTo(e.target.value)} style={{ width: 220 }} aria-label="To" />
        <button className="btn sm" disabled={saving || !from || !to} onClick={() => { save({ ...fixes, links: [...fixes.links, { from, to, label }] }); setFrom(""); setTo(""); }}>
          {saving ? <Spinner size={13} /> : <Plus size={13} />}Add missing link
        </button>
      </div>
      <div className="row wrap" style={{ gap: 6 }}>
        <input className="input" placeholder="Agent built at run time" value={agent.name} onChange={(e) => setAgent({ ...agent, name: e.target.value })} style={{ width: 220 }} aria-label="Agent name" />
        <input className="input" placeholder="Its model (optional)" value={agent.model} onChange={(e) => setAgent({ ...agent, model: e.target.value })} style={{ width: 180 }} aria-label="Agent model" />
        <button className="btn sm" disabled={saving || !agent.name} onClick={() => { save({ ...fixes, agents: [...fixes.agents, { name: agent.name, model: agent.model || undefined }] }); setAgent({ name: "", model: "" }); }}>
          <Plus size={13} />Add agent
        </button>
      </div>
      {(fixes.links.length > 0 || fixes.agents.length > 0 || fixes.hide.length > 0) && (
        <div className="col" style={{ gap: 4, fontSize: 13 }}>
          {fixes.agents.map((a, i) => (
            <div key={`a${i}`} className="row" style={{ gap: 8 }}><span className="badge outline">agent</span><b>{a.name}</b>{a.model && <span className="mono faint">{a.model}</span>}
              <button className="btn ghost sm icon" aria-label="Remove" onClick={() => save({ ...fixes, agents: fixes.agents.filter((_, j) => j !== i) })}><Trash2 size={13} /></button></div>
          ))}
          {fixes.links.map((l, i) => (
            <div key={`l${i}`} className="row" style={{ gap: 8 }}><span className="badge outline">link</span><span><b>{l.from}</b> <span className="faint">{l.label}</span> <b>{l.to}</b></span>
              <button className="btn ghost sm icon" aria-label="Remove" onClick={() => save({ ...fixes, links: fixes.links.filter((_, j) => j !== i) })}><Trash2 size={13} /></button></div>
          ))}
          {fixes.hide.map((h, i) => (
            <div key={`h${i}`} className="row" style={{ gap: 8 }}><span className="badge outline">hidden</span><b>{h}</b>
              <button className="btn ghost sm icon" aria-label="Show again" onClick={() => save({ ...fixes, hide: fixes.hide.filter((_, j) => j !== i) })}><Trash2 size={13} /></button></div>
          ))}
        </div>
      )}
      <div className="row wrap" style={{ gap: 6 }}>
        <select className="select" defaultValue="" onChange={(e) => { if (e.target.value) save({ ...fixes, hide: [...fixes.hide, e.target.value] }); e.target.value = ""; }} aria-label="Hide a box">
          <option value="">Hide a box that isn't really part of it…</option>
          {labels.filter((l) => !fixes.hide.includes(l)).map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <span className="faint" style={{ fontSize: 12 }}><Save size={11} /> each change is saved straight away</span>
      </div>
    </div>
  );
}
