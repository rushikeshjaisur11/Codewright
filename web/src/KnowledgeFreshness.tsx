import { Fragment, useEffect, useState } from "react";
import { Bot, CheckCircle2, History, Save } from "lucide-react";
import { api, KnowledgeDraft, KnowledgeRow, ReleaseNotes } from "./api";
import { Inline, Spinner, useToast } from "./ui";

const STATUS: Record<KnowledgeRow["status"], { cls: string; label: string }> = {
  behind: { cls: "amber", label: "newer releases" },
  current: { cls: "green", label: "current" },
  unrecorded: { cls: "outline", label: "not recorded" },
};

/**
 * Libraries move fast: each library's cards were checked against a version; this lists where newer releases
 * exist, what changed, and lets the model draft the card updates for the team to review and save.
 */
export default function KnowledgeFreshness({ wid, onSaved, onDeepen }: { wid: string; onSaved: () => void; onDeepen: (library: string) => void }) {
  const toast = useToast();
  const [rows, setRows] = useState<KnowledgeRow[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [notes, setNotes] = useState<ReleaseNotes | null>(null);
  const [draft, setDraft] = useState<KnowledgeDraft | null>(null);
  const [pick, setPick] = useState<Set<number>>(new Set());
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => { setRows(null); api.knowledgeFreshness(wid).then(setRows).catch(() => setRows([])); }, [wid]);
  if (!rows) return <div className="card card-pad"><Spinner /> Checking which libraries have moved on…</div>;
  const behind = rows.filter((r) => r.status === "behind");

  async function show(r: KnowledgeRow) {
    if (open === r.library) { setOpen(null); return; }
    setOpen(r.library); setNotes(null); setDraft(null); setBusy("notes");
    try { setNotes(await api.knowledgeNotes(wid, r.library, r.checked ?? "", r.latest ?? "")); } catch (e) { toast((e as Error).message, "error"); }
    setBusy("");
  }
  async function draftIt(r: KnowledgeRow) {
    setBusy("draft");
    try {
      const d = await api.knowledgeDraft(wid, { library: r.library, since: r.checked ?? "", until: r.latest ?? "", pasted });
      setDraft(d); setPick(new Set(d.new_cards.map((_, i) => i)));
      toast(`${d.model}: ${d.new_cards.length} new card(s), ${d.outdated.length} card(s) to look at`);
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy("");
  }
  async function saveDrafts(r: KnowledgeRow) {
    if (!draft) return;
    setBusy("save");
    let n = 0;
    for (const [i, c] of draft.new_cards.entries()) {
      if (!pick.has(i)) continue;
      try { await api.teachCard(wid, { ...c, library: r.library }); n++; } catch (e) { toast(`${c.title}: ${(e as Error).message}`, "error"); }
    }
    toast(`Saved ${n} card(s) to .codewright/cards/`);
    setBusy("");
    onSaved();
  }
  async function markChecked(r: KnowledgeRow) {
    if (!r.latest) return;
    try { setRows(await api.knowledgeChecked(wid, r.library, r.latest)); toast(`${r.library} marked as checked against ${r.latest}`); } catch (e) { toast((e as Error).message, "error"); }
  }

  return (
    <div className="card">
      <div className="card-head"><h2><History size={16} />Keeping it current</h2>
        <div className="right faint" style={{ fontSize: 12.5 }}>
          {behind.length ? `${behind.length} librar${behind.length === 1 ? "y has" : "ies have"} releases newer than their cards` : "every library's cards are up to date with its newest release"}
        </div>
      </div>
      <table className="table"><tbody>
        {rows.map((r) => (
          <Fragment key={r.library}>
            <tr>
              <td className="mono">{r.library}{r.uses && <span className="badge outline" style={{ marginLeft: 6 }}>used here {r.installed}</span>}</td>
              <td className="faint" style={{ fontSize: 12.5 }}>{r.cards} card{r.cards === 1 ? "" : "s"}</td>
              <td className="mono" style={{ fontSize: 12.5 }}>{r.checked ? <>checked {r.checked}<span className="faint"> ({r.checked_by})</span></> : <span className="faint">no record</span>}</td>
              <td className="mono" style={{ fontSize: 12.5 }}>{r.latest ? `newest ${r.latest}` : <span className="faint">offline</span>}</td>
              <td><span className={`badge ${STATUS[r.status].cls}`}>{STATUS[r.status].label}{r.status === "behind" ? ` (${r.bump})` : ""}</span></td>
              <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                {r.status !== "current" && <button className="btn sm" onClick={() => show(r)}>{open === r.library ? "Close" : "What changed"}</button>}
                <button className="btn ghost sm" style={{ marginLeft: 4 }} onClick={() => onDeepen(r.library)} title={`Draft more cards for ${r.library}`}>Deepen</button>
              </td>
            </tr>
            {open === r.library && (
              <tr><td colSpan={6} style={{ background: "var(--panel-2)" }}>
                <div className="col" style={{ gap: 10, padding: "6px 0" }}>
                  {busy === "notes" || !notes ? <Spinner /> : (<>
                    <div style={{ fontSize: 13 }}>
                      {notes.versions.length ? <>Releases since {r.checked ?? "the cards were written"}: <span className="mono">{notes.versions.join(", ")}</span>.</> : "No release list available."}
                      {notes.error && <span className="faint"> {notes.error}</span>}
                      {" "}{notes.links.map((l) => <a key={l.url} href={l.url} target="_blank" rel="noreferrer" style={{ marginRight: 8 }}>{l.label}</a>)}
                    </div>
                    {notes.notes.length > 0 && (
                      <details><summary style={{ cursor: "pointer", fontSize: 13 }}>{notes.notes.length} release note section(s)</summary>
                        <div className="col" style={{ gap: 8, marginTop: 6, maxHeight: 260, overflow: "auto", fontSize: 12.5, whiteSpace: "pre-wrap" }}>
                          {notes.notes.map((n) => <div key={n.version}><b>{n.version}</b> {n.date}<div className="muted">{n.body}</div></div>)}
                        </div>
                      </details>
                    )}
                    <textarea className="input" rows={2} placeholder="Paste release notes the page couldn't fetch (optional)" value={pasted} onChange={(e) => setPasted(e.target.value)} />
                    <div className="row wrap" style={{ gap: 8 }}>
                      <button className="btn sm primary" disabled={busy === "draft"} onClick={() => draftIt(r)}>{busy === "draft" ? <Spinner size={13} /> : <Bot size={13} />}Draft updates with AI</button>
                      {r.latest && <button className="btn sm" onClick={() => markChecked(r)} title="After reviewing the changes: record that the cards are checked against this version"><CheckCircle2 size={13} />Mark checked against {r.latest}</button>}
                    </div>
                    {draft && (
                      <div className="col" style={{ gap: 6 }}>
                        {draft.new_cards.map((c, i) => (
                          <label key={i} className="row" style={{ gap: 8, fontSize: 13, alignItems: "flex-start" }}>
                            <input type="checkbox" checked={pick.has(i)} onChange={() => { const s = new Set(pick); s.has(i) ? s.delete(i) : s.add(i); setPick(s); }} style={{ marginTop: 3 }} />
                            <span><b>{c.title}</b> <span className="mono faint">{c.versions}</span><div className="muted"><Inline text={c.suggestion} /></div></span>
                          </label>
                        ))}
                        {draft.outdated.map((o) => (
                          <div key={o.card} style={{ fontSize: 13 }}>⚠ <span className="mono">{o.card}</span>: <Inline text={o.why} /> <span className="muted"><Inline text={o.change} /></span></div>
                        ))}
                        {draft.new_cards.length > 0 && <div><button className="btn sm" disabled={busy === "save" || !pick.size} onClick={() => saveDrafts(r)}><Save size={13} />Save {pick.size} card(s)</button></div>}
                      </div>
                    )}
                  </>)}
                </div>
              </td></tr>
            )}
          </Fragment>
        ))}
      </tbody></table>
    </div>
  );
}
