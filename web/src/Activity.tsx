import { useEffect, useRef, useState } from "react";
import { ScrollText } from "lucide-react";
import { api, ActivityEvent } from "./api";

/**
 * What the server is doing, live: parsing, detection, test runs, release lookups, map builds. The same lines
 * print in the terminal running `codewright serve`. Polls fast while something runs, slowly otherwise.
 */
export function useActivity(wid: string | null, running: boolean) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const last = useRef(0);
  useEffect(() => { setEvents([]); last.current = 0; }, [wid]);
  useEffect(() => {
    if (!wid) return;
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const r = await api.activity(last.current, wid);
        if (stop) return;
        last.current = r.last;
        if (r.events.length) setEvents((e) => [...e, ...r.events].slice(-300));
      } catch { /* the server may be restarting */ }
      if (!stop) timer = window.setTimeout(tick, running ? 700 : 4000);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [wid, running]);
  return events;
}

export function ActivityButton({ events, running }: { events: ActivityEvent[]; running: boolean }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open && box.current) box.current.scrollTop = box.current.scrollHeight; }, [open, events]);
  const latest = events[events.length - 1];
  return (
    <div style={{ position: "relative" }}>
      <button className={`btn ${open ? "on" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open}
        title={latest ? latest.message : "What Codewright is doing"}>
        <ScrollText size={14} className={running ? "pulse" : ""} />Activity
      </button>
      {open && (
        <div className="card" style={{ position: "absolute", right: 0, top: "calc(100% + 6px)", width: "min(560px, 90vw)", zIndex: 50, boxShadow: "var(--shadow-pop)" }}>
          <div className="card-head"><h2><ScrollText size={15} />Activity</h2>
            <span className="right faint" style={{ fontSize: 12 }}>also printed by <code>codewright serve</code></span></div>
          <div ref={box} className="mono" style={{ maxHeight: 360, overflow: "auto", padding: "8px 14px", fontSize: 12, lineHeight: 1.6 }}>
            {events.length === 0 && <div className="faint">Nothing yet.</div>}
            {events.map((e) => (
              <div key={e.seq} style={{ color: e.level === "error" || e.level === "warn" ? "var(--red, #d33)" : undefined, whiteSpace: "pre-wrap" }}>
                <span className="faint">{new Date(e.at * 1000).toLocaleTimeString()}</span> {e.message}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
