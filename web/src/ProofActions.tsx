import { useState } from "react";
import { Bot, FlaskConical, RefreshCw, ThumbsDown } from "lucide-react";
import { useApp } from "./App";
import { AiCheck, api, Finding } from "./api";
import { Inline, Spinner, timeAgo, useToast } from "./ui";
import VerifySteps from "./VerifySteps";

const VERDICT: Record<AiCheck["verdict"], { cls: string; label: string }> = {
  confirmed: { cls: "green", label: "AI: looks right" },
  "needs-changes": { cls: "amber", label: "AI: needs changes" },
  wrong: { cls: "red", label: "AI: wrong here" },
};

export function AiVerdictBadge({ check }: { check?: AiCheck }) {
  if (!check) return null;
  const v = VERDICT[check.verdict];
  return <span className={`badge ${v.cls}`} title={check.reason}><Bot size={11} />{v.label}</span>;
}

/** What is known about a finding, what is missing, and the buttons that can fill the gap. */
export default function ProofActions({ f }: { f: Finding }) {
  const { wid, data, reload } = useApp();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const [check, setCheck] = useState<AiCheck | undefined>(f.ai_check);
  const running = !!data?.running;
  const testedThis = running && data?.progress?.proof;

  const gap = f.proof === "verified"
    ? "Tests passed before and after this patch."
    : f.proof === "broke-tests"
      ? "The patch broke tests: it was dropped from the suggestions."
      : !f.patch
        ? "There is no automatic patch for this, so tests cannot prove it. Ask the AI to read it against your code, or apply it by hand and run your tests."
        : /not selected|not run|no-proof|proof not run/i.test(f.proof_detail)
          ? "The tests have not been run against this patch yet."
          : "The tests ran but could not prove this patch (see the detail below). An AI check can still tell you whether it is right for this code.";

  async function runTests() {
    if (!wid) return;
    await api.runReview(wid, true, "", false, [f.file]);
    toast(`Running the tests against the patches in ${f.file}`);
    await reload();
  }

  async function askAi() {
    if (!wid) return;
    setAsking(true);
    try {
      const c = await api.aiCheck(wid, f.id);
      setCheck(c);
      toast(VERDICT[c.verdict].label);
      reload();  // the list shows the verdict too
    } catch (e) { toast((e as Error).message, "error"); }
    setAsking(false);
  }

  return (
    <div className="col" style={{ gap: 10 }}>
      <div className={`callout ${f.proof === "verified" ? "green" : f.proof === "broke-tests" ? "red" : "amber"}`} style={{ whiteSpace: "pre-wrap" }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>{gap}</div>
        <Inline text={f.proof_detail || "Not run."} />
      </div>
      <div className="row wrap" style={{ gap: 8 }}>
        {f.patch && f.proof !== "verified" && f.proof !== "broke-tests" && (
          <button className="btn primary sm" disabled={running} onClick={runTests} title="Run your tests with this file's patches applied (only this file)">
            {testedThis ? <Spinner size={13} /> : <FlaskConical size={13} />}Run the tests on this
          </button>
        )}
        <button className={`btn sm ${!f.patch && !check ? "primary" : ""}`} disabled={asking} onClick={askAi}
          title="Ask the model set up in Settings whether this suggestion is right for this code">
          {asking ? <Spinner size={13} /> : check ? <RefreshCw size={13} /> : <Bot size={13} />}
          {check ? "Ask the AI again" : f.proof === "verified" ? "Ask AI for a second opinion" : "Ask AI to check"}
        </button>
      </div>
      <VerifySteps f={f} />
      {check && (
        <div className={`callout ${VERDICT[check.verdict].cls}`}>
          <div className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
            <AiVerdictBadge check={check} />
            <span className="faint" style={{ fontSize: 12 }}>{Math.round(check.confidence * 100)}% sure · {check.model} · {timeAgo(check.at)}</span>
          </div>
          <div style={{ whiteSpace: "pre-wrap" }}><Inline text={check.reason} /></div>
          {check.risks.length > 0 && <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{check.risks.map((r, i) => <li key={i}><Inline text={r} /></li>)}</ul>}
          {check.test && <div style={{ marginTop: 6 }}><b>A test that would prove it:</b> <Inline text={check.test} /></div>}
          {check.verdict !== "confirmed" && f.status !== "rejected" && (
            <div style={{ marginTop: 8 }}>
              <button className="btn sm" onClick={async () => {
                if (!wid) return;
                await api.feedback(wid, f.id, "rejected", `AI check: ${check.reason}`.slice(0, 300));
                toast("Rejected with the AI's reason; similar findings now rank lower here");
                reload();
              }}><ThumbsDown size={13} />Reject with the AI's reason</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
