import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Copy, ExternalLink, Loader2, X } from "lucide-react";
import type { Proof } from "./api";

// ---------------------------------------------------------------- toasts
type Toast = { id: number; text: string; kind: "ok" | "error" };
const ToastCtx = createContext<(text: string, kind?: "ok" | "error") => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, text, kind }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === "error" ? 7000 : 3200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-wrap">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind === "error" ? "error" : ""}`}>
            {t.kind === "error" ? <AlertTriangle size={16} color="var(--red)" /> : <CheckCircle2 size={16} color="var(--green)" />}
            <div className="grow">{t.text}</div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---------------------------------------------------------------- small pieces
export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} className="spin" />;
}

export function Empty({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div>
        <div className="icon">{icon}</div>
        <h3>{title}</h3>
        <div style={{ maxWidth: 460 }}>{children}</div>
      </div>
    </div>
  );
}

export function Stat({ label, value, sub, icon, meter, color }: {
  label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode; meter?: number | null; color?: string;
}) {
  return (
    <div className="card stat">
      <div className="label">{icon}{label}</div>
      <div className="value" style={color ? { color } : undefined}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
      {meter != null && <div className="meter"><span style={{ width: `${Math.round(meter * 100)}%`, background: color }} /></div>}
    </div>
  );
}

export function ProofBadge({ proof }: { proof: Proof }) {
  const map = { verified: ["green", "Verified"], unverified: ["amber", "Unverified"], "broke-tests": ["red", "Broke tests"] } as const;
  const [c, t] = map[proof];
  return <span className={`badge ${c}`}><span className="dot" />{t}</span>;
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="tabs">
      {options.map((o) => (
        <button key={o.value} className={`tab ${o.value === value ? "active" : ""}`} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function CopyButton({ text, label = "Copy", small }: { text: string; label?: string; small?: boolean }) {
  const toast = useToast();
  return (
    <button className={`btn ${small ? "sm" : ""}`} onClick={() => {
      navigator.clipboard.writeText(text).then(() => toast("Copied to clipboard"), () => toast("Couldn't copy", "error"));
    }}>
      <Copy size={14} />{label}
    </button>
  );
}

export function Source({ url }: { url: string }) {
  let host = url;
  try { host = new URL(url).host.replace(/^www\./, ""); } catch { /* keep */ }
  return <a href={url} target="_blank" rel="noreferrer"><ExternalLink size={12} />{host}<span className="faint">{url.slice(url.indexOf(host) + host.length, 80)}</span></a>;
}

export function Modal({ title, children, onClose, footer }: { title: string; children: ReactNode; onClose: () => void; footer?: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="card-head"><h3>{title}</h3><div className="right"><button className="btn ghost icon" onClick={onClose}><X size={16} /></button></div></div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- code + diff

const KW = new Set("def class return if elif else for while try except finally with as import from async await yield lambda pass raise in not and or is None True False global nonlocal break continue del assert match case".split(" "));

function highlight(line: string, lang: string): ReactNode {
  if (lang !== "python") return line;
  const out: ReactNode[] = [];
  const re = /(#.*$)|("""[^]*?"""|'''[^]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(@[\w.]+)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|([A-Za-z_][A-Za-z0-9_]*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(line))) {
    if (m.index > last) out.push(line.slice(last, m.index));
    const [tok, com, str, dec, num, fn, word] = m;
    const key = i++;
    if (com) out.push(<span key={key} className="tok-com">{tok}</span>);
    else if (str) out.push(<span key={key} className="tok-str">{tok}</span>);
    else if (dec) out.push(<span key={key} className="tok-dec">{tok}</span>);
    else if (num) out.push(<span key={key} className="tok-num">{tok}</span>);
    else if (fn) out.push(<span key={key} className={KW.has(fn) ? "tok-kw" : "tok-fn"}>{tok}</span>);
    else if (word && KW.has(word)) out.push(<span key={key} className="tok-kw">{tok}</span>);
    else out.push(tok);
    last = m.index + tok.length;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

export function CodeView({ text, start = 1, lang = "python", highlightLines, scrollTo }: {
  text: string; start?: number; lang?: string; highlightLines?: [number, number]; scrollTo?: number;
}) {
  const lines = useMemo(() => text.replace(/\n$/, "").split("\n"), [text]);
  useEffect(() => {
    if (scrollTo) document.getElementById(`ln-${scrollTo}`)?.scrollIntoView({ block: "center" });
  }, [scrollTo, text]);
  return (
    <div className="code">
      <div style={{ padding: "8px 0" }}>
        {lines.map((l, i) => {
          const n = i + start;
          const hl = highlightLines && n >= highlightLines[0] && n <= highlightLines[1];
          return (
            <div key={i} id={`ln-${n}`} className={`code-line ${hl ? "hl" : ""}`}>
              <span className="ln">{n}</span><span className="tx">{highlight(l, lang) || " "}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function DiffView({ patch }: { patch: string }) {
  const rows = useMemo(() => {
    const out: { cls: string; ln: string; tx: string }[] = [];
    let newLn = 0;
    for (const l of patch.replace(/\n$/, "").split("\n")) {
      if (l.startsWith("---") || l.startsWith("+++")) continue;
      const m = /^@@ -\d+(?:,\d+)? \+(\d+)/.exec(l);
      if (m) { newLn = Number(m[1]); out.push({ cls: "hunk", ln: "", tx: l }); continue; }
      if (l.startsWith("+")) { out.push({ cls: "add", ln: String(newLn++), tx: l }); }
      else if (l.startsWith("-")) { out.push({ cls: "del", ln: "", tx: l }); }
      else { out.push({ cls: "", ln: String(newLn++), tx: l }); }
    }
    return out;
  }, [patch]);
  const file = /^\+\+\+ b\/(.*)$/m.exec(patch)?.[1];
  return (
    <div className="code">
      {file && <div style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)", color: "var(--text-2)" }}>{file}</div>}
      <div style={{ padding: "6px 0" }}>
        {rows.map((r, i) => (
          <div key={i} className={`code-line ${r.cls}`}><span className="ln">{r.ln}</span><span className="tx">{r.tx || " "}</span></div>
        ))}
      </div>
    </div>
  );
}

export const KIND_STYLE: Record<string, { color: string; soft: string; label: string }> = {
  component: { color: "var(--accent)", soft: "var(--accent-soft)", label: "Component" },
  module: { color: "var(--accent)", soft: "var(--accent-soft)", label: "Module" },
  service: { color: "var(--accent)", soft: "var(--accent-soft)", label: "Service" },
  worker: { color: "var(--violet)", soft: "var(--violet-soft)", label: "Worker" },
  function: { color: "var(--teal)", soft: "var(--teal-soft)", label: "Function" },
  entry: { color: "var(--green)", soft: "var(--green-soft)", label: "Entry point" },
  database: { color: "var(--blue)", soft: "var(--blue-soft)", label: "Database" },
  vector: { color: "var(--blue)", soft: "var(--blue-soft)", label: "Vector index" },
  cache: { color: "var(--red)", soft: "var(--red-soft)", label: "Cache" },
  queue: { color: "var(--amber)", soft: "var(--amber-soft)", label: "Queue" },
  scheduler: { color: "var(--amber)", soft: "var(--amber-soft)", label: "Scheduler" },
  http: { color: "var(--text-2)", soft: "var(--panel-2)", label: "External API" },
  llm: { color: "var(--violet)", soft: "var(--violet-soft)", label: "LLM" },
  ingress: { color: "var(--green)", soft: "var(--green-soft)", label: "Clients" },
  storage: { color: "var(--teal)", soft: "var(--teal-soft)", label: "Storage" },
  cloud: { color: "var(--amber)", soft: "var(--amber-soft)", label: "Cloud" },
};

export function intentLabel(intent: string) {
  return intent.replace(/-/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function timeAgo(t: number | null) {
  if (!t) return "never";
  const s = Math.round(Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
