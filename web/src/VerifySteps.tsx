import { useEffect, useState } from "react";
import { FilePlus2, FlaskConical, ListChecks } from "lucide-react";
import { useApp } from "./App";
import { AiTest, api, Finding, VerifyStep } from "./api";
import { CodeView, CopyButton, Inline, Spinner, timeAgo, useToast } from "./ui";

const TEST_VERDICT: Record<AiTest["verdict"], { cls: string; label: string }> = {
  verified: { cls: "green", label: "AI test passes before and after" },
  broke: { cls: "red", label: "AI test breaks with the patch" },
  "test-fails-today": { cls: "amber", label: "AI test fails today (not a valid check)" },
  "no-patch": { cls: "amber", label: "AI test written (no patch to try)" },
};

export function AiTestBadge({ test }: { test?: AiTest }) {
  if (!test) return null;
  const v = TEST_VERDICT[test.verdict];
  return <span className={`badge ${v.cls}`} title={test.detail}><FlaskConical size={11} />{v.label}</span>;
}

/** The steps that would prove a finding here, and an AI-written test run before and after the patch. */
export default function VerifySteps({ f }: { f: Finding }) {
  const { wid, reload } = useApp();
  const toast = useToast();
  const [steps, setSteps] = useState<VerifyStep[] | null>(null);
  const [open, setOpen] = useState(false);
  const [writing, setWriting] = useState(false);
  const [test, setTest] = useState<AiTest | undefined>(f.ai_test);
  const [showCode, setShowCode] = useState(false);
  useEffect(() => { if (open && wid && !steps) api.verifySteps(wid, f.id).then((r) => setSteps(r.steps)); }, [open, wid, f.id, steps]);

  async function write() {
    if (!wid) return;
    setWriting(true);
    try {
      const t = await api.aiTest(wid, f.id);
      setTest(t);
      toast(TEST_VERDICT[t.verdict].label);
      reload();
    } catch (e) { toast((e as Error).message, "error"); }
    setWriting(false);
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <button className={`btn sm ${open ? "on" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open}><ListChecks size={13} />How to verify</button>
        <button className="btn sm" disabled={writing} onClick={write} title="The model writes a test for this function; it runs on today's code, then with the patch, in a copy of the repo">
          {writing ? <Spinner size={13} /> : <FlaskConical size={13} />}{writing ? "Writing and running the test…" : test ? "Write the AI test again" : "Let AI write a test and run it"}
        </button>
      </div>
      {open && (
        !steps ? <Spinner size={14} /> : (
          <ol style={{ margin: 0, paddingLeft: 20 }} className="col">
            {steps.map((s, i) => (
              <li key={i} style={{ marginBottom: 6 }}>
                <b><Inline text={s.title} /></b>
                <div className="muted" style={{ fontSize: 13 }}><Inline text={s.detail} /></div>
                {s.command && <div className="row" style={{ gap: 6, marginTop: 4 }}><code style={{ fontSize: 12 }}>{s.command}</code><CopyButton text={s.command} label="Copy" /></div>}
              </li>
            ))}
          </ol>
        )
      )}
      {test && (
        <div className={`callout ${TEST_VERDICT[test.verdict].cls}`}>
          <div className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
            <AiTestBadge test={test} />
            <span className="faint" style={{ fontSize: 12 }}>{test.model} · {timeAgo(test.at)} · <code>{test.path}</code></span>
          </div>
          <div>{test.detail}</div>
          <div className="faint" style={{ fontSize: 12.5, marginTop: 4 }}>
            Today: {test.before.ok ? "passes" : "fails"} ({test.before.seconds}s){test.after ? ` · with the patch: ${test.after.ok ? "passes" : "fails"} (${test.after.seconds}s)` : ""}
          </div>
          <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
            <button className="btn sm" onClick={() => setShowCode(!showCode)}>{showCode ? "Hide" : "Show"} the test and its output</button>
            {test.before.ok && <button className="btn sm" onClick={async () => {
              if (!wid) return;
              try { const r = await api.saveAiTest(wid, f.id); toast(`Added ${r.path}: commit it from the branch bar`); } catch (e) { toast((e as Error).message, "error"); }
            }}><FilePlus2 size={13} />Add this test to the repo</button>}
          </div>
          {showCode && (
            <div className="col" style={{ gap: 6, marginTop: 8 }}>
              <CodeView text={test.test} />
              <CodeView text={(test.after ?? test.before).output || "(no output)"} lang="text" />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
