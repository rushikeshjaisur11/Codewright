import { useEffect, useState } from "react";
import { Bot, Library, Save } from "lucide-react";
import { api, LibraryDraftCard, LibraryInfo } from "./api";
import { Inline, Modal, Spinner, useToast } from "./ui";

/**
 * Add a library to the knowledge base, or deepen one it covers: the model drafts cards from the library's README,
 * docs the team pastes and how this repo uses it; the team edits and picks what to save (.codewright/cards/).
 */
export default function LibraryKnowledge({ wid, library, onClose, onSaved }: {
  wid: string; library?: string; onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const deepen = !!library;
  const [name, setName] = useState(library ?? "");
  const [info, setInfo] = useState<LibraryInfo | null>(null);
  const [looking, setLooking] = useState(false);
  const [focus, setFocus] = useState("");
  const [pasted, setPasted] = useState("");
  const [useRepo, setUseRepo] = useState(true);
  const [count, setCount] = useState(8);
  const [drafting, setDrafting] = useState(false);
  const [cards, setCards] = useState<LibraryDraftCard[] | null>(null);
  const [pick, setPick] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);

  async function lookUp(n = name) {
    if (!n.trim()) return;
    setLooking(true);
    try { setInfo(await api.libraryInfo(wid, n.trim())); } catch (e) { toast((e as Error).message, "error"); }
    setLooking(false);
  }
  useEffect(() => { if (library) lookUp(library); }, [library]); // eslint-disable-line react-hooks/exhaustive-deps

  async function draft() {
    setDrafting(true);
    try {
      const d = await api.libraryDraft(wid, { name: name.trim(), focus, pasted, use_repo: useRepo, count });
      setCards(d.cards);
      setPick(new Set(d.cards.map((_, i) => i)));
      toast(d.cards.length ? `${d.model} drafted ${d.cards.length} card(s): check each before saving` : "The model found nothing it could ground in the material");
    } catch (e) { toast((e as Error).message, "error"); }
    setDrafting(false);
  }

  async function save() {
    if (!cards) return;
    setSaving(true);
    let n = 0;
    for (const [i, c] of cards.entries()) {
      if (!pick.has(i)) continue;
      try { await api.teachCard(wid, { ...c }); n++; } catch (e) { toast(`${c.title}: ${(e as Error).message}`, "error"); }
    }
    if (n && info?.version && !info.cards) {  // a new library: its cards were written for this version
      try { await api.knowledgeChecked(wid, name.trim(), info.version); } catch { /* the cards are saved either way */ }
    }
    toast(`Saved ${n} card(s) for ${name} to .codewright/cards/`);
    setSaving(false);
    onSaved();
  }

  const edit = (i: number, k: keyof LibraryDraftCard, v: string) => setCards((cs) => cs!.map((c, j) => (j === i ? { ...c, [k]: v } : c)));

  return (
    <Modal title={deepen ? `Deepen ${library}` : "Add a library"} onClose={onClose} footer={<>
      <span className="faint" style={{ fontSize: 12.5, marginRight: "auto" }}>Saved as your team's cards in <code>.codewright/cards/</code>.</span>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!cards || !pick.size || saving} onClick={save}>{saving ? <Spinner size={14} /> : <Save size={14} />}Save {pick.size || ""} card{pick.size === 1 ? "" : "s"}</button>
    </>}>
      <div className="col" style={{ gap: 10 }}>
        {!deepen && (
          <div className="row" style={{ gap: 6 }}>
            <input className="input mono" style={{ flex: 1 }} placeholder="Package name, e.g. langgraph" value={name} onChange={(e) => { setName(e.target.value); setInfo(null); }}
              onKeyDown={(e) => e.key === "Enter" && lookUp()} aria-label="Library" autoFocus />
            <button className="btn" disabled={looking || !name.trim()} onClick={() => lookUp()}>{looking ? <Spinner size={14} /> : <Library size={14} />}Look up</button>
          </div>
        )}
        {info && (
          <div className="callout" style={{ fontSize: 13 }}>
            {info.found ? (<>
              <b>{info.name}</b> {info.version && <span className="mono">{info.version}</span>} — {info.summary}
              <div className="faint" style={{ marginTop: 4 }}>
                {info.cards ? `${info.cards} card(s) already` : "no cards yet"}{info.installed ? ` · this repo uses ${info.installed}` : " · not a dependency of this repo"}
                {info.links.slice(0, 4).map((l) => <a key={l.url} href={l.url} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>{l.label}</a>)}
              </div>
            </>) : <span className="faint">{info.error} — paste its docs below and the draft will use those.</span>}
          </div>
        )}
        <input className="input" placeholder={deepen ? "Go deeper on, e.g. streaming, checkpointing, error handling" : "Focus on (optional), e.g. what production code gets wrong"} value={focus} onChange={(e) => setFocus(e.target.value)} aria-label="Focus" />
        <textarea className="input" rows={4} placeholder="Paste docs, a guide or your team's notes (optional): the cards are grounded in these and the README" value={pasted} onChange={(e) => setPasted(e.target.value)} aria-label="Docs" />
        <div className="row wrap" style={{ gap: 12, fontSize: 13 }}>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={useRepo} onChange={(e) => setUseRepo(e.target.checked)} />Start from how this repo uses it</label>
          <label className="row" style={{ gap: 6 }}>Up to
            <select className="select" style={{ height: 28 }} value={count} onChange={(e) => setCount(Number(e.target.value))} aria-label="How many cards">
              {[5, 8, 12, 15].map((n) => <option key={n}>{n}</option>)}
            </select> cards</label>
          <button className="btn sm primary" disabled={drafting || !name.trim()} onClick={draft}>{drafting ? <Spinner size={13} /> : <Bot size={13} />}{cards ? "Draft again" : "Draft cards with AI"}</button>
        </div>
        {cards && cards.map((c, i) => (
          <div key={i} className="callout col" style={{ gap: 6, opacity: pick.has(i) ? 1 : 0.55 }}>
            <label className="row" style={{ gap: 8 }}>
              <input type="checkbox" checked={pick.has(i)} onChange={() => { const s = new Set(pick); s.has(i) ? s.delete(i) : s.add(i); setPick(s); }} />
              <input className="input" style={{ flex: 1, fontWeight: 600 }} value={c.title} onChange={(e) => edit(i, "title", e.target.value)} aria-label="Card title" />
              <input className="input mono" style={{ width: 110 }} value={c.versions} onChange={(e) => edit(i, "versions", e.target.value)} aria-label="Versions" title="Versions it applies to" />
            </label>
            <textarea className="input" rows={2} value={c.suggestion} onChange={(e) => edit(i, "suggestion", e.target.value)} aria-label="What to do" />
            <div className="faint" style={{ fontSize: 12.5 }}>
              {c.symbol && <code>{c.symbol}</code>} {c.gain && <>· <Inline text={c.gain} /></>}
              {c.sources.length > 0 && <> · {c.sources.map((s) => <a key={s} href={s} target="_blank" rel="noreferrer" style={{ marginLeft: 4 }}>source</a>)}</>}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}
