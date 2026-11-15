import { useEffect, useState } from "react";
import { BellOff, Bot, CheckCircle2, Package, Plug, RotateCcw, Save, ShieldOff, Trash2, Wifi, XCircle } from "lucide-react";
import { useApp } from "../App";
import { api, IndexState, Settings } from "../api";
import { CopyButton, Spinner, intentLabel, useToast } from "../ui";

export default function SettingsPage() {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [s, setS] = useState<Settings | null>(null);
  const [model, setModel] = useState("");
  const [constraints, setConstraints] = useState("");
  const [idx, setIdx] = useState<IndexState | null>(null);
  const [indexUrl, setIndexUrl] = useState("");
  const [scope, setScope] = useState<"machine" | "repo">("machine");
  const [probe, setProbe] = useState<{ ok: boolean; detail: string } | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    if (!wid) return;
    api.settings(wid).then((x) => { setS(x); setModel(x.model); setConstraints(x.constraints.join("\n")); });
    api.index(wid).then(setIdx).catch(() => setIdx(null));
  }, [wid]);

  async function testIndex(url: string) {
    setProbing(true); setProbe(null);
    try { setProbe(await api.testIndex(wid!, url)); } catch (e) { setProbe({ ok: false, detail: (e as Error).message }); }
    finally { setProbing(false); }
  }

  async function saveIndex(url: string, where: "machine" | "repo") {
    try {
      setIdx(await api.saveIndex(wid!, url, where));
      setIndexUrl(""); setProbe(null);
      toast(url ? `Index saved for ${where === "repo" ? "this repo" : "this machine"}` : `Removed the ${where === "repo" ? "repo" : "machine"} setting`);
    } catch (e) { toast((e as Error).message, "error"); }
  }

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
        <div className="card-head"><h2><Package size={16} />Package index</h2>
          <div className="right">{idx && (idx.is_pypi ? <span className="badge outline">PyPI</span> : <span className="badge blue">custom</span>)}</div>
        </div>
        <div className="card-pad col" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 13 }}>
            Where Releases, upgrade previews and Design look up versions, and where proof environments install from. PyPI unless you set your own, such as Artifactory or Nexus.
          </div>
          {idx && (
            <div className="row wrap" style={{ gap: 8 }}>
              <span className="mono" style={{ fontSize: 13 }}>{idx.url}</span>
              <span className="faint" style={{ fontSize: 12.5 }}>from {idx.source}</span>
              <button className="btn sm ghost" disabled={probing} onClick={() => testIndex("")}><Wifi size={13} />Test</button>
            </div>
          )}
          {idx?.env_override && <div className="faint" style={{ fontSize: 12.5 }}><code>CODEWRIGHT_INDEX_URL</code> is set in the environment and wins over the settings below.</div>}
          <div className="row wrap">
            <input className="input mono grow" value={indexUrl} onChange={(e) => setIndexUrl(e.target.value)}
              placeholder="https://artifactory.example.com/artifactory/api/pypi/pypi-virtual/simple" />
            <select className="input" style={{ width: "auto" }} value={scope} onChange={(e) => setScope(e.target.value as "machine" | "repo")}>
              <option value="machine">This machine</option>
              <option value="repo">This repo (shared via git)</option>
            </select>
            <button className="btn" disabled={!indexUrl.trim() || probing} onClick={() => testIndex(indexUrl)}><Wifi size={14} />Test</button>
            <button className="btn primary" disabled={!indexUrl.trim()} onClick={() => saveIndex(indexUrl, scope)}><Save size={14} />Save</button>
          </div>
          {probing && <div className="row muted" style={{ fontSize: 13 }}><Spinner />Looking a package up…</div>}
          {probe && (
            <div className="row" style={{ fontSize: 13, color: probe.ok ? "var(--green)" : "var(--red)" }}>
              {probe.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{probe.detail}
            </div>
          )}
          {idx && (idx.machine || idx.repo) && (
            <div className="row wrap" style={{ gap: 8 }}>
              {idx.repo && <button className="btn sm ghost" onClick={() => saveIndex("", "repo")}><RotateCcw size={13} />Remove repo setting ({idx.repo})</button>}
              {idx.machine && <button className="btn sm ghost" onClick={() => saveIndex("", "machine")}><RotateCcw size={13} />Remove machine setting ({idx.machine})</button>}
            </div>
          )}
          <div className="faint" style={{ fontSize: 12.5 }}>
            Use the Simple API URL your pip or uv uses. Without a setting, Codewright also picks up <code>UV_DEFAULT_INDEX</code>, <code>UV_INDEX_URL</code>, <code>PIP_INDEX_URL</code>, the repo's <code>[tool.uv.index]</code>, <code>uv.toml</code> and <code>pip.conf</code>.
            Credentials: <code>~/.netrc</code>, <code>CODEWRIGHT_INDEX_TOKEN</code>, or in the URL for the machine setting only. They are never shown here.
          </div>
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
