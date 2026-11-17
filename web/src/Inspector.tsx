import { ReactNode, useState } from "react";
import { ArrowDownRight, ChevronDown, Code2, Crosshair, Layers, MessageSquareCode, Workflow } from "lucide-react";
import type { NodeDetails } from "./api";
import { KIND_ICON } from "./Graph";
import { CategoryBadge, CodeView, KIND_STYLE, Spinner } from "./ui";

const KIND_NAME: Record<string, string> = { entry: "Entry point", llm: "Model or AI service", unknown: "Box" };

/** The side panel for one box on either map: what it is, facts, findings, links and its code. */
export default function Inspector({ d, loading, onSelect, onOpenCode, onFinding, onAsk, onFocus, focused, onDrill, onFlow, extra }: {
  d: NodeDetails | null;
  loading?: boolean;
  onSelect: (id: string) => void;
  onOpenCode: (file: string, line?: number, span?: [number, number]) => void;
  onFinding: (id: string) => void;
  onAsk?: (q: string) => void;
  onFocus?: () => void;
  focused?: boolean;
  onDrill?: () => void;
  onFlow?: (entry: string) => void;
  extra?: ReactNode;
}) {
  if (!d) return <div className="row muted" style={{ fontSize: 13 }}>{loading ? <><Spinner size={14} />Loading details…</> : "No details."}</div>;
  const Icon = KIND_ICON[d.kind] ?? Layers;
  const style = KIND_STYLE[d.kind];
  const sections = d.sections.filter((s) => s.items.length > 0);
  return (
    <div className="inspector">
      <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
        <span className="cw-node__icon" style={{ width: 36, height: 36, flex: "none", color: style?.color, background: style?.soft }}><Icon size={18} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="faint" style={{ fontSize: 12.5 }}>{KIND_NAME[d.kind] ?? style?.label ?? d.kind}</div>
          <h2 style={{ fontSize: 18, wordBreak: "break-word" }}>{d.label}</h2>
        </div>
        {loading && <Spinner size={14} />}
      </div>
      {d.summary && <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>{d.summary}</p>}

      <div className="row wrap" style={{ gap: 6 }}>
        {onDrill && <button className="btn sm primary" onClick={onDrill}>Drill in<ArrowDownRight size={13} /></button>}
        {d.code && <button className="btn sm" onClick={() => onOpenCode(d.code!.file, d.code!.start, [d.code!.start, d.code!.end])}><Code2 size={13} />Open code</button>}
        {onFocus && <button className={`btn sm ${focused ? "on" : ""}`} aria-pressed={focused} onClick={onFocus} title="Show only this box and what it connects to"><Crosshair size={13} />{focused ? "Show everything" : "Focus"}</button>}
        {onAsk && d.ask && <button className="btn sm" onClick={() => onAsk(d.ask)} title={d.ask}><MessageSquareCode size={13} />Ask about it</button>}
        {extra}
      </div>

      {d.findings.length > 0 && (
        <div>
          <div className="section-title">Suggestions here <span className="faint">{d.findings.length}</span></div>
          <div className="conn">
            {d.findings.map((f) => (
              <button key={f.id} className="conn-row" style={{ alignItems: "flex-start" }} onClick={() => onFinding(f.id)} title={f.evidence}>
                <span className="grow" style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 13 }}>{f.title}</span>
                  <span className="row" style={{ gap: 6, marginTop: 4 }}><CategoryBadge category={f.category} /><span className="faint mono truncate" style={{ fontSize: 11.5 }}>{f.file}:{f.line}</span></span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {d.facts.length > 0 && (
        <dl className="facts">
          {d.facts.filter((f) => f.value).map((f, i) => (
            <div key={i}><dt>{f.label}</dt><dd className={f.mono ? "mono" : ""}>{f.value}</dd></div>
          ))}
        </dl>
      )}

      {d.text && (
        <div>
          <div className="section-title">{d.text.title}</div>
          <div className="text-block">{d.text.body}</div>
        </div>
      )}

      {sections.map((s) => <Section key={s.title} s={s} onSelect={onSelect} onOpenCode={onOpenCode} onFlow={onFlow} />)}

      {d.code && (
        <div>
          <div className="section-title"><Code2 size={13} />{d.code.file}:{d.code.start}{d.code.truncated ? " (first lines)" : ""}</div>
          <div className="code-preview"><CodeView text={d.code.text} start={d.code.start} /></div>
        </div>
      )}
    </div>
  );
}

function Section({ s, onSelect, onOpenCode, onFlow }: {
  s: NodeDetails["sections"][number]; onSelect: (id: string) => void; onOpenCode: (file: string, line?: number) => void; onFlow?: (entry: string) => void;
}) {
  const [all, setAll] = useState(false);
  const items = all ? s.items : s.items.slice(0, 8);
  return (
    <div>
      <div className="section-title">{s.title} <span className="faint">{s.items.length}</span></div>
      <div className="conn">
        {items.map((it, i) => {
          const go = it.node ? () => onSelect(it.node!) : it.flow && onFlow ? () => onFlow(it.flow!) : it.file ? () => onOpenCode(it.file!, it.line) : undefined;
          const hint = it.node ? "Select on the map" : it.flow ? "Show the request flow" : it.file ? "Open the code" : "";
          return (
            <button key={i} className="conn-row" style={{ alignItems: "flex-start" }} onClick={go} disabled={!go} title={hint}>
              <span className="grow" style={{ minWidth: 0 }}>
                <span className="truncate" style={{ display: "block", fontSize: 13 }}>{it.label}</span>
                {it.sub && <span className="faint truncate" style={{ display: "block", fontSize: 11.5 }}>{it.sub}</span>}
              </span>
              {it.node ? <Crosshair size={12} className="faint" style={{ marginTop: 3, flex: "none" }} />
                : it.flow ? <Workflow size={12} className="faint" style={{ marginTop: 3, flex: "none" }} />
                : it.file ? <Code2 size={12} className="faint" style={{ marginTop: 3, flex: "none" }} /> : null}
            </button>
          );
        })}
      </div>
      {s.items.length > 8 && (
        <button className="btn sm ghost" style={{ marginTop: 4 }} onClick={() => setAll(!all)}>
          <ChevronDown size={13} style={{ transform: all ? "rotate(180deg)" : undefined }} />{all ? "Show fewer" : `Show all ${s.items.length}`}
        </button>
      )}
    </div>
  );
}
