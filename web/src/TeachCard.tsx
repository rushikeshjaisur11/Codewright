import { useState } from "react";
import { Bot, Save } from "lucide-react";
import { api, Card } from "./api";
import { Modal, Spinner, useToast } from "./ui";

type Draft = Partial<Record<"title" | "topic" | "library" | "versions" | "symbol" | "suggestion" | "gain" | "risk" | "when_not" | "threshold" | "snippet", string>> & { sources?: string };

const FIELDS: [keyof Draft, string, boolean][] = [
  ["title", "Title: the practice, as advice", false],
  ["library", "Library (pip name; empty for Python itself)", false],
  ["versions", "Versions it applies to, e.g. >=2.0 or *", false],
  ["topic", "Topic it belongs under", false],
  ["symbol", "API it is about, e.g. langgraph.types.Send", false],
  ["suggestion", "What to do", true],
  ["gain", "Why: what it gains", true],
  ["risk", "Risk", true],
  ["when_not", "When not to", true],
  ["threshold", "When it starts to matter", true],
  ["sources", "Sources (one URL per line)", true],
  ["snippet", "Example code", true],
];

/**
 * Teach Codewright something it does not know: a practice, a team convention, an API. Fill the card by hand,
 * or have the model draft it from a topic and pasted docs, then edit. Saved to .codewright/cards/ in the repo.
 */
export default function TeachCard({ wid, deepen, onClose, onSaved }: {
  wid: string; deepen?: Card | null; onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const [card, setCard] = useState<Draft>(() => (deepen ? { library: deepen.library ?? "", topic: deepen.topic, versions: deepen.versions } : { versions: "*" }));
  const [topic, setTopic] = useState(deepen ? `Deeper than: ${deepen.title}` : "");
  const [notes, setNotes] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [rule, setRule] = useState({ on: false, calls: "", missing_kwarg: "", in_async: false, paths: "" });

  async function draft() {
    setDrafting(true);
    try {
      const d = await api.draftCard(wid, { topic, library: card.library ?? "", notes, deepen: deepen?.id ?? "" });
      const sources = Array.isArray(d.sources) ? (d.sources as string[]).join("\n") : String(d.sources ?? "");
      setCard((c) => ({ ...c, ...Object.fromEntries(Object.entries(d).filter(([k, v]) => k !== "model" && k !== "sources" && v)), sources: sources || c.sources }));
      toast(`Drafted by ${d.model}: check every field before saving`);
    } catch (e) { toast((e as Error).message, "error"); }
    setDrafting(false);
  }

  async function save() {
    setSaving(true);
    try {
      const saved = await api.teachCard(wid, {
        ...card,
        sources: (card.sources ?? "").split("\n").map((s) => s.trim()).filter(Boolean),
        ...(rule.on && rule.calls.trim() ? { rule: { calls: rule.calls, missing_kwarg: rule.missing_kwarg, in_async: rule.in_async, paths: rule.paths } } : {}),
      });
      toast(saved.rule?.calls
        ? `Saved ${saved.id}: the next scan flags ${saved.rule.calls.join(", ")} as a finding`
        : `Saved ${saved.id} to .codewright/cards/ — commit it to share with your team`);
      onSaved();
    } catch (e) { toast((e as Error).message, "error"); }
    setSaving(false);
  }

  return (
    <Modal title={deepen ? "Deepen this card" : "Teach Codewright"} onClose={onClose} footer={<>
      <span className="faint" style={{ fontSize: 12.5, marginRight: "auto" }}>Saved as a reference card in <code>.codewright/cards/</code>: searchable here, in code chat and over MCP.</span>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={saving || !card.title || !card.suggestion} onClick={save}>{saving ? <Spinner size={14} /> : <Save size={14} />}Save card</button>
    </>}>
      <div className="col" style={{ gap: 10 }}>
        <div className="callout accent col" style={{ gap: 8 }}>
          <div style={{ fontWeight: 600 }}><Bot size={14} /> Draft with AI (optional)</div>
          <input className="input" placeholder="Topic, e.g. fan out with LangGraph Send" value={topic} onChange={(e) => setTopic(e.target.value)} aria-label="Topic" />
          <textarea className="input" rows={4} placeholder="Paste docs, release notes or your team's notes: the draft is grounded in these" value={notes} onChange={(e) => setNotes(e.target.value)} aria-label="Notes" />
          <div><button className="btn sm" disabled={drafting || (!topic && !deepen)} onClick={draft}>{drafting ? <Spinner size={13} /> : <Bot size={13} />}Draft the card</button></div>
        </div>
        <div className="callout col" style={{ gap: 8 }}>
          <label className="row" style={{ gap: 6, fontWeight: 600, cursor: "pointer" }}>
            <input type="checkbox" checked={rule.on} onChange={(e) => setRule({ ...rule, on: e.target.checked })} />
            Turn it into a check: reviews flag code that breaks it
          </label>
          {rule.on && (<>
            <input className="input mono" placeholder="Calls to flag, e.g. requests.get, requests.post" value={rule.calls} onChange={(e) => setRule({ ...rule, calls: e.target.value })} aria-label="Calls to flag" />
            <div className="row wrap" style={{ gap: 8 }}>
              <input className="input mono" style={{ width: 220 }} placeholder="…only without this keyword, e.g. timeout" value={rule.missing_kwarg} onChange={(e) => setRule({ ...rule, missing_kwarg: e.target.value })} aria-label="Missing keyword" />
              <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={rule.in_async} onChange={(e) => setRule({ ...rule, in_async: e.target.checked })} />only inside async def</label>
            </div>
            <input className="input mono" placeholder="Only in these paths (optional), e.g. services/**" value={rule.paths} onChange={(e) => setRule({ ...rule, paths: e.target.value })} aria-label="Paths" />
          </>)}
        </div>
        {FIELDS.map(([k, label, long]) => (
          <label key={k} className="col" style={{ gap: 4 }}>
            <span className="faint" style={{ fontSize: 12.5 }}>{label}</span>
            {long
              ? <textarea className={`input ${k === "snippet" ? "mono" : ""}`} rows={k === "snippet" ? 5 : 2} value={card[k] ?? ""} onChange={(e) => setCard({ ...card, [k]: e.target.value })} />
              : <input className="input" value={card[k] ?? ""} onChange={(e) => setCard({ ...card, [k]: e.target.value })} />}
          </label>
        ))}
      </div>
    </Modal>
  );
}
