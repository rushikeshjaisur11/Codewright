import { useEffect, useState } from "react";
import { BellOff, Bot, CheckCircle2, Plug, Save, ShieldOff, Trash2, XCircle } from "lucide-react";
import { useApp } from "../App";
import { api, Settings } from "../api";
import { CopyButton, Spinner, intentLabel, useToast } from "../ui";

export default function SettingsPage() {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [s, setS] = useState<Settings | null>(null);
  const [model, setModel] = useState("");
  const [constraints, setConstraints] = useState("");

  useEffect(() => {
    if (!wid) return;
    api.settings(wid).then((x) => { setS(x); setModel(x.model); setConstraints(x.constraints.join("\n")); });
  }, [wid]);

  if (!s || !wid) return <Spinner />;

  async function save(patch: Partial<{ model: string; offline: boolean; constraints: string[] }>) {
    try {
      const x = await api.saveSettings(wid!, patch);
      setS(x);
      toast("Settings saved to .codewright/config.json");
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function unmute(intent: string, path: string) {
    await api.mute(wid!, intent, path, true);
    setS(await api.settings(wid!));
    toast(`Unmuted ${intentLabel(intent)}`);
    runReview(false);
  }

  const mcp = JSON.stringify({ mcpServers: { codewright: { command: "codewright", args: ["mcp"] } } }, null, 2);
  return (
    <div className="col" style={{ gap: 16, maxWidth: 900 }}>
      <div className="page-head"><div><h1>Settings</h1><p>Stored in <code>{data?.root}/.codewright/</code>, so the team shares them through git.</p></div></div>

      <div className="card">
        <div className="card-head"><h2><Bot size={16} />Model</h2>
          <div className="right">{s.llm.available ? <span className="badge green"><CheckCircle2 size={11} />available</span> : <span className="badge amber"><XCircle size={11} />not available</span>}</div>
        </div>
        <div className="card-pad col" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 13 }}>
            Review, the critic, proof and Design's capacity math never need a model. A model only writes code-chat answers and drafts Design's data model.
            {!s.llm.available && <> Right now: {s.llm.reason}.</>}
          </div>
          <div className="row wrap">
            <input className="input mono grow" value={model} onChange={(e) => setModel(e.target.value)} placeholder="vertex_ai/gemini-2.5-flash" />
            <button className="btn primary" onClick={() => save({ model })}><Save size={14} />Save</button>
          </div>
          <div className="faint" style={{ fontSize: 12.5 }}>Any LiteLLM model id: <code>vertex_ai/…</code>, <code>gemini/…</code>, <code>anthropic/…</code>, <code>openai/…</code>, <code>ollama/…</code>. Credentials come from the environment.</div>
          <label className="row" style={{ cursor: "pointer" }}>
            <input type="checkbox" checked={s.offline} onChange={(e) => save({ offline: e.target.checked })} />
            <ShieldOff size={14} /><span><b>Offline mode</b> <span className="muted">— never call a model; no code leaves this machine.</span></span>
          </label>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Team constraints</h2></div>
        <div className="card-pad col">
          <div className="muted" style={{ fontSize: 13 }}>One per line, e.g. “no new dependencies” or “must support Python 3.10”. Shared with coding agents through the MCP server.</div>
          <textarea className="textarea mono" rows={4} value={constraints} onChange={(e) => setConstraints(e.target.value)} />
          <div><button className="btn" onClick={() => save({ constraints: constraints.split("\n") })}><Save size={14} />Save constraints</button></div>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2><BellOff size={16} />Mute rules</h2></div>
        {s.mutes.length === 0 ? <div className="card-pad muted">Nothing muted. Use “Mute intent” on a finding to hide that kind of suggestion in this repo.</div> : (
          <table className="table">
            <tbody>
              {s.mutes.map((m) => (
                <tr key={m.intent + m.path}>
                  <td>{m.intent === "*" ? "Every intent" : intentLabel(m.intent)}</td>
                  <td className="mono muted">{m.path || "everywhere"}</td>
                  <td style={{ textAlign: "right" }}><button className="btn sm ghost" onClick={() => unmute(m.intent, m.path)}><Trash2 size={13} />Unmute</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <div className="card-head"><h2><Plug size={16} />Coding agents and CI</h2></div>
        <div className="card-pad col" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 13 }}>Add Codewright as an MCP server so Claude Code, Copilot or Devin can call <code>review</code>, <code>architecture_map</code>, <code>ask_code</code> and <code>check_drift</code>.</div>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <pre className="code grow" style={{ margin: 0, padding: 12 }}>{mcp}</pre>
            <CopyButton text={mcp} small />
          </div>
          <div className="muted" style={{ fontSize: 13 }}>In CI, comment on pull requests with changed-code findings and fail on architecture drift:</div>
          <pre className="code" style={{ margin: 0, padding: 12 }}>{`codewright review . --base origin/main -f pr -o codewright.md\ncodewright check .   # exits 1 on drift from .codewright/design.json`}</pre>
        </div>
      </div>
    </div>
  );
}
