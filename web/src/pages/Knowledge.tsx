import { useEffect, useMemo, useState } from "react";
import { BookOpen, Search, Wand2 } from "lucide-react";
import { api, Card } from "../api";
import { CodeView, Source, Spinner, intentLabel } from "../ui";

export default function Knowledge() {
  const [cards, setCards] = useState<Card[] | null>(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { api.cards().then(setCards); }, []);
  const list = useMemo(() => (cards ?? []).filter((c) => !q || `${c.title} ${c.intent} ${c.symbol} ${c.library ?? ""}`.toLowerCase().includes(q.toLowerCase())), [cards, q]);
  const groups = useMemo(() => list.reduce<Record<string, Card[]>>((a, c) => ({ ...a, [c.intent]: [...(a[c.intent] ?? []), c] }), {}), [list]);

  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Knowledge base</h1><p>Capability cards: what each library version offers for each intent, with gain, risk, “when not to” and sources.</p></div>
        <div className="actions">
          <div className="row" style={{ position: "relative" }}>
            <Search size={14} style={{ position: "absolute", left: 10, color: "var(--text-3)" }} />
            <input className="input" style={{ paddingLeft: 30, width: 260 }} placeholder="Search cards" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        </div>
      </div>
      {!cards ? <Spinner /> : (
        <div className="col" style={{ gap: 18 }}>
          <div className="faint">{cards.length} cards across {new Set(cards.map((c) => c.intent)).size} intents. Add your own with <code>codewright review --cards ./my-cards</code>.</div>
          {Object.entries(groups).sort().map(([intent, cs]) => (
            <div key={intent}>
              <div className="section-title"><BookOpen size={13} />{intentLabel(intent)}</div>
              <div className="card">
                <table className="table">
                  <tbody>
                    {cs.map((c) => (
                      <>
                        <tr key={c.id} className="click" onClick={() => setOpen(open === c.id ? null : c.id)}>
                          <td style={{ width: "50%" }}><div style={{ fontWeight: 600 }}>{c.title}</div><div className="mono faint" style={{ fontSize: 11.5 }}>{c.symbol}</div></td>
                          <td><span className="badge outline">{c.kind}</span> {c.autofix && <span className="badge blue"><Wand2 size={11} />auto-patch</span>}</td>
                          <td className="mono muted">{c.library ?? "python"} {c.versions}</td>
                          <td className="mono faint" style={{ whiteSpace: "nowrap" }}>impact {c.impact} · effort {c.effort}</td>
                        </tr>
                        {open === c.id && (
                          <tr key={`${c.id}-d`}><td colSpan={4} style={{ background: "var(--panel-2)" }}>
                            <div className="col" style={{ gap: 10, padding: "4px 0" }}>
                              <div>{c.suggestion}</div>
                              <div className="kv">
                                <div className="kv-item good"><div className="k">Gain</div>{c.gain}</div>
                                <div className="kv-item warn"><div className="k">Risk</div>{c.risk}</div>
                                <div className="kv-item bad"><div className="k">When not to</div>{c.when_not}</div>
                                <div className="kv-item"><div className="k">Scale threshold</div>{c.threshold}</div>
                              </div>
                              {c.snippet && <CodeView text={c.snippet} lang={c.snippet_lang} />}
                              <div className="col sources" style={{ gap: 3 }}>{c.sources.map((s) => <Source key={s} url={s} />)}</div>
                            </div>
                          </td></tr>
                        )}
                      </>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
