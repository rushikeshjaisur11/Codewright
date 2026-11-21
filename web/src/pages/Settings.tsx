import { useEffect, useState } from "react";
import { BellOff, Bot, CheckCircle2, Globe, Package, Plug, PlugZap, RotateCcw, Save, ShieldOff, Trash2, Wifi, XCircle } from "lucide-react";
import { useApp } from "../App";
import { AiSettings, AiTest, api, IndexState, ProviderId, SearchSettings, Settings } from "../api";
import { CopyButton, Spinner, intentLabel, useToast } from "../ui";
import ScanScope from "../ScanScope";
import AiUsage from "../AiUsage";

export default function SettingsPage() {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [s, setS] = useState<Settings | null>(null);
  const [constraints, setConstraints] = useState("");
  const [idx, setIdx] = useState<IndexState | null>(null);
  const [indexUrl, setIndexUrl] = useState("");
  const [scope, setScope] = useState<"machine" | "repo">("machine");
  const [probe, setProbe] = useState<{ ok: boolean; detail: string } | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    if (!wid) return;
    api.settings(wid).then((x) => { setS(x); setConstraints(x.constraints.join("\n")); });
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

  async function save(patch: Partial<{ offline: boolean; constraints: string[] }>) {
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
      <div className="page-head"><div><h1>Settings</h1><p>Repo settings are stored in <code>{data?.root}/.codewright/</code>, so the team shares them through git. The model and its key are stored on this machine only.</p></div></div>

      <ModelCard wid={wid} settings={s} onChange={setS} onOffline={(offline) => save({ offline })} />

      <WebSearchCard />

      <AiUsage />

      {wid && <ScanScope />}

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
          <div className="muted" style={{ fontSize: 13 }}>For a whole team, run one shared server and give each person a token; <code>codewright mcp config --url https://your-host</code> prints the setup for Claude Code, Cursor and VS Code:</div>
          <pre className="code" style={{ margin: 0, padding: 12 }}>{`CODEWRIGHT_MCP_TOKENS=$(codewright mcp token) codewright mcp --http --host 0.0.0.0 --root /srv/repos/app`}</pre>
          <div className="muted" style={{ fontSize: 13 }}>In CI, comment on pull requests with changed-code findings and fail on architecture drift:</div>
          <pre className="code" style={{ margin: 0, padding: 12 }}>{`codewright review . --base origin/main -f pr -o codewright.md\ncodewright check .   # exits 1 on drift from .codewright/design.json`}</pre>
        </div>
      </div>
    </div>
  );
}

const PROVIDER_HELP: Record<ProviderId, { short: string; blurb: string; key: string; where: string }> = {
  anthropic: { short: "Claude", blurb: "Anthropic API key", key: "Anthropic API key", where: "Create one in the Anthropic Console under API keys." },
  openai: {
    short: "OpenAI-compatible", blurb: "OpenAI, Ollama, LM Studio, OpenRouter, Azure",
    key: "API key (leave empty for a local server)",
    where: "OpenAI: create a key under API keys. Ollama or LM Studio: no key, only the server address.",
  },
  gemini: { short: "Gemini", blurb: "Google AI Studio API key", key: "Gemini API key", where: "Create one in Google AI Studio under Get API key." },
  litellm: {
    short: "Vertex AI and others", blurb: "Through LiteLLM, with credentials from the environment",
    key: "API key (optional)",
    where: "Most LiteLLM providers read their credentials from the environment, for example gcloud application-default login for Vertex AI.",
  },
};

function ModelCard({ wid, settings, onChange, onOffline }: {
  wid: string; settings: Settings; onChange: (s: Settings) => void; onOffline: (offline: boolean) => void;
}) {
  const ai = settings.ai;
  const [f, setF] = useState({ provider: ai.provider, model: ai.model, base_url: ai.base_url });
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "test" | "off">("");
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");
  const [test, setTest] = useState<AiTest | null>(null);

  const prov = ai.providers.find((p) => p.id === f.provider);
  const fromEnv = (k: string) => ai.from_environment.includes(k);
  const locked = fromEnv("provider");
  const set = (k: "model" | "base_url", v: string) => { setF({ ...f, [k]: v }); setSaved(""); setTest(null); };
  const pick = (id: ProviderId) => {
    const same = id === ai.provider;
    setF({ provider: id, model: same ? ai.model : ai.providers.find((p) => p.id === id)?.model ?? "", base_url: same ? ai.base_url : "" });
    setKey(""); setSaved(""); setTest(null); setErr("");
  };
  const apply = (next: AiSettings) => onChange({ ...settings, ai: next, model: next.model });

  async function save(andTest: boolean) {
    setBusy(andTest ? "test" : "save"); setErr(""); setSaved(""); setTest(null);
    try {
      // Switching provider without typing a key must not send the old provider's key to the new one.
      const switching = f.provider !== ai.provider;
      apply(await api.saveAi(wid, { ...f, api_key: key || undefined, clear_key: switching && !key }));
      setKey(""); setSaved("Saved.");
      if (andTest) setTest(await api.testAi(wid));
      onChange(await api.settings(wid));
    } catch (e) { setErr((e as Error).message); }
    setBusy("");
  }
  async function turnOff() {
    setBusy("off"); setErr(""); setTest(null);
    try {
      await api.saveAi(wid, { provider: "", model: "", base_url: "", clear_key: true });
      const next = await api.settings(wid);
      onChange(next);
      setF({ provider: next.ai.provider, model: next.ai.model, base_url: next.ai.base_url });
      setKey(""); setSaved("The model is off and the key is forgotten. Everything else keeps working.");
    } catch (e) { setErr((e as Error).message); }
    setBusy("");
  }

  const help = f.provider ? PROVIDER_HELP[f.provider] : null;
  const keyPlaceholder = fromEnv("api_key") ? "set by an environment variable"
    : ai.has_key && ai.provider === f.provider ? `saved ${ai.key_hint} (type to replace)` : "paste the key";
  return (
    <div className="card">
      <div className="card-head"><h2><Bot size={16} />Model</h2>
        <div className="right">
          {test && !test.ok ? <span className="badge amber"><XCircle size={11} />not answering</span>
            : test?.ok ? <span className="badge green"><CheckCircle2 size={11} />connected: {settings.llm.model}</span>
            : settings.llm.available ? <span className="badge blue">set up: {settings.llm.model}</span>
            : settings.offline ? <span className="badge amber"><ShieldOff size={11} />offline mode</span>
            : ai.provider ? <span className="badge amber"><XCircle size={11} />not ready</span>
            : <span className="badge outline">off: not needed</span>}
        </div>
      </div>
      <div className="card-pad col" style={{ gap: 18 }}>
        <div className="muted" style={{ fontSize: 13 }}>
          Review, the critic, proof and Design's capacity math never use a model. A model adds two things: written answers in Code chat and a first draft of Design's data model. Set it up here once; no environment variables or restart needed.
          {ai.provider && !settings.llm.available && <> Right now: {settings.llm.reason}.</>}
        </div>

        <div>
          <div className="step-label">1 · Choose a provider</div>
          <div className="choices">
            {ai.providers.map((p) => (
              <button key={p.id} className={`choice ${f.provider === p.id ? "on" : ""}`} aria-pressed={f.provider === p.id} disabled={locked} onClick={() => pick(p.id)}>
                <b>{PROVIDER_HELP[p.id].short}</b>
                <span>{PROVIDER_HELP[p.id].blurb}</span>
              </button>
            ))}
          </div>
          {locked && <div className="faint" style={{ fontSize: 12.5, marginTop: 8 }}>The provider is set by an environment variable on this machine, so it can't be changed here.</div>}
          {ai.from_repo.length > 0 && <div className="faint" style={{ fontSize: 12.5, marginTop: 8 }}>This comes from <code>codewright_model</code> in the repo's <code>.codewright/config.json</code>. Saving here replaces it on this machine.</div>}
        </div>

        {prov && help && (
          <div>
            <div className="step-label">2 · Connect it</div>
            <div className="form-grid">
              <label className="field">{help.key}
                <input className="input" type="password" autoComplete="off" value={key} disabled={fromEnv("api_key")}
                  onChange={(e) => { setKey(e.target.value); setSaved(""); setTest(null); }} placeholder={keyPlaceholder} />
                <span className="hint">{help.where} The key is stored on this machine, outside the repo, and is never shown again.</span>
              </label>
              <label className="field">Model
                <input className="input mono" value={f.model} onChange={(e) => set("model", e.target.value)} placeholder={prov.model} disabled={fromEnv("model")} />
                <span className="hint">{f.provider === "litellm"
                  ? <>A LiteLLM model id such as <code>vertex_ai/gemini-2.5-flash</code> or <code>bedrock/…</code>.</>
                  : "A small, fast model is enough: it writes short answers that cite the code."}</span>
              </label>
              {f.provider === "openai" && (
                <label className="field wide">Server address
                  <input className="input mono" value={f.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder="https://api.openai.com/v1" disabled={fromEnv("base_url")} />
                  <span className="hint">Leave empty for OpenAI; <code>http://localhost:11434/v1</code> for Ollama. Any server that speaks the OpenAI chat format works.</span>
                </label>
              )}
            </div>
            {f.provider === "litellm" && !ai.litellm_installed && (
              <div className="row" style={{ fontSize: 13, color: "var(--amber)", marginTop: 12 }}>
                <XCircle size={14} /><span>LiteLLM is not installed here. Run <code>pip install "codewright[llm]"</code>, then restart Codewright.</span>
              </div>
            )}
            <div className="row wrap" style={{ marginTop: 16 }}>
              <button className="btn primary" disabled={!!busy} onClick={() => save(true)}>{busy === "test" ? <Spinner size={14} /> : <PlugZap size={14} />}{busy === "test" ? "Testing…" : "Save and test"}</button>
              <button className="btn" disabled={!!busy} onClick={() => save(false)}><Save size={14} />Save</button>
              {(ai.provider || ai.has_key) && !locked && <button className="btn ghost" disabled={!!busy} onClick={turnOff}><Trash2 size={14} />Turn off and forget the key</button>}
              {saved && !test && <span className="muted" style={{ fontSize: 13 }}>{saved}</span>}
            </div>
            {test && (
              <div className="row" role={test.ok ? "status" : "alert"} style={{ fontSize: 13, marginTop: 12, color: test.ok ? "var(--green)" : "var(--red)", alignItems: "flex-start" }}>
                {test.ok ? <CheckCircle2 size={14} style={{ marginTop: 3, flex: "none" }} /> : <XCircle size={14} style={{ marginTop: 3, flex: "none" }} />}
                <span>{test.ok ? `Connected. ${test.model} answered in ${test.ms} ms.` : `The model did not answer: ${test.error}`}</span>
              </div>
            )}
          </div>
        )}
        {!prov && saved && <div className="muted" style={{ fontSize: 13 }}>{saved}</div>}
        {err && <div className="row" role="alert" style={{ fontSize: 13, color: "var(--red)" }}><XCircle size={14} />{err}</div>}

        <label className="row" style={{ cursor: settings.offline_from_environment ? "not-allowed" : "pointer" }}>
          <input type="checkbox" checked={settings.offline} disabled={settings.offline_from_environment} onChange={(e) => onOffline(e.target.checked)} />
          <ShieldOff size={14} /><span><b>Offline mode for this repo</b> <span className="muted">— never call a model, even when one is set up. {settings.offline_from_environment ? "Set by CODEWRIGHT_OFFLINE." : "Shared with the team through git."}</span></span>
        </label>

        <div>
          <div className="step-label">What is sent to the model</div>
          <ul className="plain-list">
            <li><b>Nothing</b> while the model is off or offline mode is on, and nothing during a review either way.</li>
            <li><b>Code chat:</b> your question, the last few messages and the handful of code passages and knowledge cards that matched it.</li>
            <li><b>Design:</b> the project idea and workload you typed in the interview, to draft the data model. No code.</li>
            <li><b>AI agent:</b> your task, and the files, search results, web pages and test output it reads while it works. It edits a sandbox copy; your files change only when you apply its patch.</li>
          </ul>
        </div>
      </div>
    </div>
  );
}

const SEARCH_HELP: Record<string, { label: string; blurb: string; key?: string; url?: boolean }> = {
  duckduckgo: { label: "DuckDuckGo", blurb: "No key needed. Fine for occasional use; it sometimes blocks automated searches." },
  brave: { label: "Brave Search", blurb: "Dependable results with an API key (free tier available).", key: "Brave Search API key" },
  tavily: { label: "Tavily", blurb: "Search made for AI agents; needs an API key.", key: "Tavily API key" },
  searxng: { label: "SearXNG", blurb: "Your own metasearch server: private, no key.", url: true },
  off: { label: "Off", blurb: "The agent works only from the repository and installed packages." },
};

function WebSearchCard() {
  const toast = useToast();
  const [s, setS] = useState<SearchSettings | null>(null);
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => { api.searchSettings().then((x) => { setS(x); setProvider(x.provider); setUrl(x.url); }).catch(() => undefined); }, []);
  if (!s) return null;
  const help = SEARCH_HELP[provider] ?? SEARCH_HELP.duckduckgo;

  async function save() {
    setBusy(true); setTest(null);
    try {
      const x = await api.saveSearch({ provider, url, ...(key ? { api_key: key } : {}) });
      setS(x); setKey("");
      const t = provider === "off" ? null : await api.testSearch();
      setTest(t ? { ok: t.ok, text: t.ok ? `Working: ${t.results?.[0]?.title ?? ""}` : `Not working: ${t.error}` } : null);
      toast("Web search settings saved on this machine");
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  }

  return (
    <div className="card">
      <div className="card-head"><h2><Globe size={16} />Web search for the AI agent</h2>
        <div className="right">{s.available ? <span className="badge blue">{SEARCH_HELP[s.provider]?.label ?? s.provider}</span> : <span className="badge amber">{s.reason}</span>}</div>
      </div>
      <div className="card-pad col" style={{ gap: 14 }}>
        <div className="muted" style={{ fontSize: 13 }}>The agent looks up release notes, migration guides and API docs while it changes code. It only reads public pages: addresses on your network are refused.</div>
        <div className="choices">
          {s.providers.map((p) => (
            <button key={p} className={`choice ${provider === p ? "on" : ""}`} aria-pressed={provider === p} onClick={() => { setProvider(p); setTest(null); }}>
              <b>{SEARCH_HELP[p]?.label ?? p}</b><span>{SEARCH_HELP[p]?.blurb}</span>
            </button>
          ))}
        </div>
        {(help.key || help.url) && (
          <div className="form-grid">
            {help.key && <label className="field">{help.key}
              <input className="input" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
                placeholder={s.has_key && s.provider === provider ? `saved ${s.key_hint} (type to replace)` : "paste the key"} />
              <span className="hint">Stored on this machine, outside the repo; never shown again.</span>
            </label>}
            {help.url && <label className="field">SearXNG address
              <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://search.example.org" />
              <span className="hint">The instance must have the JSON format enabled.</span>
            </label>}
          </div>
        )}
        <div className="row wrap" style={{ gap: 10 }}>
          <button className="btn primary" disabled={busy} onClick={save}>{busy ? <Spinner size={14} /> : <Save size={14} />}Save and test</button>
          {test && <span className="row" role={test.ok ? "status" : "alert"} style={{ fontSize: 13, color: test.ok ? "var(--green)" : "var(--red)" }}>
            {test.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{test.text}</span>}
        </div>
      </div>
    </div>
  );
}

