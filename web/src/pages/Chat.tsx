import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import { MessageSquareCode, SendHorizontal, User } from "lucide-react";
import { useApp } from "../App";
import { api, Citation } from "../api";
import { Empty, Spinner, useToast } from "../ui";

interface Msg { role: "user" | "assistant"; content: string; citations?: Citation[]; mode?: string }

const SUGGESTIONS = [
  "How does a request flow through this repo?",
  "Where do we talk to the database?",
  "Which code calls external HTTP APIs?",
  "Where are agents or models called?",
];

export default function Chat() {
  const { wid, data, openCode } = useApp();
  const toast = useToast();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" }); }, [msgs, busy]);

  async function send(q: string) {
    if (!wid || !q.trim() || busy) return;
    const history = msgs.map((m) => ({ role: m.role, content: m.content }));
    setMsgs((x) => [...x, { role: "user", content: q }]);
    setText("");
    setBusy(true);
    try {
      const a = await api.chat(wid, q, history);
      setMsgs((x) => [...x, { role: "assistant", content: a.answer, citations: a.citations, mode: a.mode }]);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (!data?.result) return <div style={{ margin: "auto" }}><Empty icon={<MessageSquareCode size={22} />} title="Code chat needs a review first">{data?.running ? <Spinner /> : "Run a review to index the code."}</Empty></div>;

  return (
    <div className="chat">
      <div className="chat-log" ref={log}>
        {msgs.length === 0 && (
          <div style={{ margin: "auto", textAlign: "center", maxWidth: 560 }}>
            <div className="empty" style={{ padding: "20px 0" }}><div>
              <div className="icon"><MessageSquareCode size={22} /></div>
              <h3>Ask about {data.name}</h3>
              <div className="muted">Every answer cites the files and lines it comes from. Click a citation to open the code.</div>
            </div></div>
            <div className="chips" style={{ justifyContent: "center" }}>
              {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "user" ? "user" : "bot"}`}>
            <div className="avatar">{m.role === "user" ? <User size={14} /> : <span className="mark" aria-hidden="true" />}</div>
            <div className="bubble">
              <Markdown>{m.content}</Markdown>
              {m.citations && m.citations.length > 0 && (
                <div className="cites">
                  {m.citations.map((c) => (
                    <button key={c.n} className="cite" onClick={() => openCode(c.file, c.line, [c.line, c.end])}>
                      <b>[{c.n}]</b>{c.file}:{c.line}
                    </button>
                  ))}
                </div>
              )}
              {m.mode === "extractive" && <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>Built from the code graph. Configure a model in Settings for written answers.</div>}
            </div>
          </div>
        ))}
        {busy && <div className="msg bot"><div className="avatar"><span className="mark" aria-hidden="true" /></div><div className="bubble muted pulse">Searching the code graph…</div></div>}
      </div>
      <div className="chat-input">
        <div className="chat-box">
          <textarea rows={1} value={text} placeholder="Ask how something works, where it lives, or what calls it"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); } }} />
          <button className="btn primary icon" disabled={!text.trim() || busy} onClick={() => send(text)}><SendHorizontal size={16} /></button>
        </div>
      </div>
    </div>
  );
}
