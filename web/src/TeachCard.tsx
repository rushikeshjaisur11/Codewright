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
      const saved = await api.teachCard(wid, { ...card, sources: (card.sources ?? "").split("\n").map((s) => s.trim()).filter(Boolean) });
      toast(`Saved ${saved.id} to .codewright/cards/ — commit it to share with your team`);
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
