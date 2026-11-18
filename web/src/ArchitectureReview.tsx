import { useState } from "react";
import { Bot, ShieldAlert } from "lucide-react";
import { useApp } from "./App";
import { api, Insight } from "./api";
import { Inline, Spinner, useToast } from "./ui";

const SEV: Record<Insight["severity"], string> = { high: "red", medium: "amber", low: "outline" };

/**
 * Improvements read from how the agents are wired: each one names the file and line and the code that shows
 * it. The model can be asked for a deeper pass on top; its suggestions are marked as such.
 */
export default function ArchitectureReview({ insights, onFocus }: { insights: Insight[]; onFocus: (node: string) => void }) {
  const { wid, openCode } = useApp();
  const toast = useToast();
  const [ai, setAi] = useState<Insight[] | null>(null);
  const [asking, setAsking] = useState(false);
  const all = [...insights, ...(ai ?? [])];

  async function deeper() {
    if (!wid) return;
    setAsking(true);
    try {
      const r = await api.aiReview(wid);
      if (r.error) toast(r.error, "error");
      setAi(r.insights);
      if (!r.error) toast(r.insights.length ? `${r.insights.length} more from ${r.model}` : "The model found nothing to add");
    } catch (e) { toast((e as Error).message, "error"); }
    setAsking(false);
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2><ShieldAlert size={16} />Architecture review</h2>
        <div className="right row" style={{ gap: 8 }}>
          <span className="faint" style={{ fontSize: 12.5 }}>
            {insights.length ? `${insights.length} from how the agents are wired` : "Nothing the rules flag"}
            {ai ? ` · ${ai.length} from the AI` : ""}
          </span>
          <button className="btn sm" disabled={asking} onClick={deeper} title="Send the architecture and its key functions to the model set up in Settings">
            {asking ? <Spinner size={13} /> : <Bot size={13} />}{ai ? "Ask the AI again" : "Ask AI for a deeper review"}
          </button>
        </div>
      </div>
      {all.length === 0 ? (
        <div className="card-pad faint" style={{ fontSize: 13 }}>
          No fan-out, routing, loop, state or endpoint problems found in how these agents are put together.
        </div>
      ) : (
        <div className="col" style={{ gap: 0 }}>
          {all.map((i, n) => (
            <div key={`${i.id}-${n}`} className="card-pad" style={{ borderTop: n ? "1px solid var(--border)" : undefined }}>
              <div className="row wrap" style={{ gap: 8, marginBottom: 4 }}>
                <span className={`badge ${SEV[i.severity]}`}>{i.severity}</span>
                {i.source === "ai" && <span className="badge blue"><Bot size={11} />AI</span>}
                <b><Inline text={i.title} /></b>
              </div>
              <div className="muted" style={{ fontSize: 13 }}><Inline text={i.why} /></div>
              <div style={{ fontSize: 13, marginTop: 4 }}><b>Change:</b> <Inline text={i.fix} /></div>
              <div className="row wrap" style={{ gap: 8, marginTop: 6 }}>
                {i.file && <button className="btn ghost sm mono" style={{ paddingLeft: 0 }} onClick={() => openCode(i.file, i.line)}>{i.file}:{i.line}</button>}
                {i.evidence && <code className="faint" style={{ fontSize: 12 }}>{i.evidence}</code>}
                {i.nodes[0] && <button className="btn ghost sm" onClick={() => onFocus(i.nodes[0])}>Show in diagram</button>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
