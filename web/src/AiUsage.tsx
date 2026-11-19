import { useEffect, useState } from "react";
import { Gauge, Save } from "lucide-react";
import { api, AiUsage as Usage } from "./api";
import { useToast } from "./ui";

/** Today's model calls and tokens on this machine, and the daily limit every AI button respects. */
export default function AiUsage() {
  const toast = useToast();
  const [u, setU] = useState<Usage | null>(null);
  const [limit, setLimit] = useState("");
  useEffect(() => { api.aiUsage().then((x) => { setU(x); setLimit(String(x.daily_calls)); }); }, []);
  if (!u) return null;
  const pct = u.daily_calls ? Math.min(100, Math.round((u.calls / u.daily_calls) * 100)) : 0;
  return (
    <div className="card">
      <div className="card-head"><h2><Gauge size={16} />AI usage today</h2>
        <div className="right faint" style={{ fontSize: 12.5 }}>this machine · every model call is also listed in Activity</div>
      </div>
      <div className="card-pad col" style={{ gap: 10 }}>
        <div className="row wrap" style={{ gap: 18 }}>
          <div><div className="faint" style={{ fontSize: 12 }}>Model calls</div><b style={{ fontSize: 20 }}>{u.calls}</b>{u.daily_calls ? <span className="faint"> / {u.daily_calls}</span> : null}</div>
          <div><div className="faint" style={{ fontSize: 12 }}>Tokens in</div><b style={{ fontSize: 20 }}>{u.input_tokens.toLocaleString()}</b></div>
          <div><div className="faint" style={{ fontSize: 12 }}>Tokens out</div><b style={{ fontSize: 20 }}>{u.output_tokens.toLocaleString()}</b></div>
        </div>
        {u.daily_calls > 0 && <div style={{ height: 6, background: "var(--panel-2)", borderRadius: 3 }}><div style={{ width: `${pct}%`, height: 6, borderRadius: 3, background: pct > 85 ? "var(--red, #d33)" : "var(--accent)" }} /></div>}
        <div className="row wrap" style={{ gap: 8 }}>
          <span className="faint" style={{ fontSize: 13 }}>Daily limit (0 for none)</span>
          <input className="input mono" style={{ width: 90 }} value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} aria-label="Daily model call limit" />
          <button className="btn sm" onClick={async () => { const x = await api.setAiBudget(Number(limit || 0)); setU(x); toast(x.daily_calls ? `At most ${x.daily_calls} model calls a day` : "No daily limit"); }}>
            <Save size={13} />Save
          </button>
          <span className="faint" style={{ fontSize: 12 }}>When it is reached, AI buttons say so instead of calling the model.</span>
        </div>
      </div>
    </div>
  );
}
