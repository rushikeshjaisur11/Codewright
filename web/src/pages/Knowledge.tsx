import { Fragment, useEffect, useMemo, useState } from "react";
import { BookOpen, Radar, Search, Wand2 } from "lucide-react";
import { api, Card, FitStatus } from "../api";
import { CodeView, Source, Spinner, Tabs, intentLabel } from "../ui";

const FIT: Record<FitStatus, { cls: string; label: string }> = {
  fits: { cls: "green", label: "fits this repo" },
  upgrade: { cls: "amber", label: "needs an upgrade" },
  older: { cls: "outline", label: "older versions" },
  unused: { cls: "outline", label: "not a dependency" },
  unknown: { cls: "outline", label: "version unknown" },
};

const lib = (c: Card) => c.library ?? "python";

export default function Knowledge({ wid }: { wid: string | null }) {
  const [cards, setCards] = useState<Card[] | null>(null);
  const [q, setQ] = useState("");
  const [library, setLibrary] = useState("all");
  const [fitsOnly, setFitsOnly] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { setCards(null); api.cards(wid).then(setCards); }, [wid]);

  const libraries = useMemo(() => {
    const counts = new Map<string, number>();
    (cards ?? []).forEach((c) => counts.set(lib(c), (counts.get(lib(c)) ?? 0) + 1));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [cards]);

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (cards ?? []).filter((c) =>
      (library === "all" || lib(c) === library)
      && (!fitsOnly || c.fit.status === "fits")
      && (!needle || `${c.title} ${c.intent} ${c.symbol} ${lib(c)} ${c.topic} ${c.suggestion}`.toLowerCase().includes(needle)));
  }, [cards, q, library, fitsOnly]);

  // Group by library, then topic (cards without one fall back to their intent).
  const groups = useMemo(() => {
    const out = new Map<string, Card[]>();
    for (const c of list) {
      const key = `${lib(c)}\u0000${c.topic || intentLabel(c.intent)}`;
      out.set(key, [...(out.get(key) ?? []), c]);
    }
    return [...out.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [list]);

  const detectors = (cards ?? []).filter((c) => !c.reference).length;
  const fitting = (cards ?? []).filter((c) => c.fit.status === "fits").length;

  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Knowledge base</h1><p>What each library version offers: the API, gain, risk, “when not to” and sources. Cards with a detector become findings; reference entries are for looking things up.</p></div>
        <div className="actions">
          <div className="row" style={{ position: "relative" }}>
            <Search size={14} style={{ position: "absolute", left: 10, color: "var(--text-3)" }} />
            <input className="input" style={{ paddingLeft: 30, width: 260 }} placeholder="Search, e.g. persist sessions" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the knowledge base" />
          </div>
        </div>
      </div>
      {!cards ? <Spinner /> : (
        <div className="col" style={{ gap: 18 }}>
          <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "center" }}>
            <div style={{ overflowX: "auto", maxWidth: "100%", whiteSpace: "nowrap" }}>
              <Tabs<string> value={library} onChange={setLibrary} options={[
                { value: "all", label: <>All <span className="badge">{cards.length}</span></> },
                ...libraries.map(([name, n]) => ({ value: name, label: <>{name} <span className="badge">{n}</span></> })),
              ]} />
            </div>
            {wid && (
              <label className="row" style={{ gap: 6, cursor: "pointer" }}>
                <input type="checkbox" checked={fitsOnly} onChange={(e) => setFitsOnly(e.target.checked)} />
                Only what fits this repo's versions <span className="faint">({fitting})</span>
              </label>
            )}
          </div>
          <div className="faint">{list.length} of {cards.length} entries · {detectors} with a detector, {cards.length - detectors} reference. Add your own with <code>codewright review --cards ./my-cards</code>.</div>
          {groups.map(([key, cs]) => {
            const [library, topic] = key.split("\u0000");
            return (
              <div key={key}>
                <div className="section-title"><BookOpen size={13} />{topic} <span className="faint mono" style={{ fontWeight: 400 }}>{library}</span></div>
                <div className="card" style={{ overflowX: "auto" }}>
                  <table className="table" style={{ minWidth: 560 }}>
                    <tbody>
                      {cs.map((c) => (
                        <Fragment key={c.id}>
                          <tr className="click" onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id}>
                            <td style={{ width: "48%" }}><div style={{ fontWeight: 600 }}>{c.title}</div><div className="mono faint" style={{ fontSize: 11.5 }}>{c.symbol}</div></td>
                            <td>
                              {c.reference
                                ? <span className="badge outline">reference</span>
                                : <span className="badge outline" title={`Found by the ${intentLabel(c.intent)} detector`}><Radar size={11} />detector</span>}
                              {" "}{c.autofix && <span className="badge blue"><Wand2 size={11} />auto-patch</span>}
                            </td>
                            <td className="mono muted">{c.versions}</td>
                            <td style={{ whiteSpace: "nowrap" }}>{wid && <span className={`badge ${FIT[c.fit.status].cls}`} title={c.fit.detail}>{FIT[c.fit.status].label}</span>}</td>
                          </tr>
                          {open === c.id && (
                            <tr><td colSpan={4} style={{ background: "var(--panel-2)" }}>
                              <div className="col" style={{ gap: 10, padding: "4px 0" }}>
                                <div>{c.suggestion}</div>
                                <div className="kv">
                                  <div className="kv-item good"><div className="k">Gain</div>{c.gain}</div>
                                  <div className="kv-item warn"><div className="k">Risk</div>{c.risk}</div>
                                  <div className="kv-item bad"><div className="k">When not to</div>{c.when_not}</div>
                                  <div className="kv-item"><div className="k">Scale threshold</div>{c.threshold}</div>
                                </div>
                                {wid && c.fit.detail && <div className="faint">For this repo: {c.fit.detail}.</div>}
                                {c.snippet && <CodeView text={c.snippet} lang={c.snippet_lang} />}
                                <div className="faint mono" style={{ fontSize: 11.5 }}>impact {c.impact} · effort {c.effort} · {c.kind}{c.reference ? "" : ` · intent ${c.intent}`}</div>
                                <div className="col sources" style={{ gap: 3 }}>{c.sources.map((s) => <Source key={s} url={s} />)}</div>
                              </div>
                            </td></tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
