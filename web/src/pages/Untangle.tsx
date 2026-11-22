import { useEffect, useMemo, useState } from "react";
import { Code2, Eraser, Scissors, Sparkles, Wand2 } from "lucide-react";
import { useApp } from "../App";
import { api, RemovalResult, UntangleInventory, UntangleItem, UntangleKind } from "../api";
import { CopyButton, DiffView, Empty, Inline, Modal, Spinner, Stat, Tabs, useToast } from "../ui";

const ORDER: UntangleKind[] = ["unused", "unused-module", "test-only", "wrapper", "unused-param", "swallowed", "commented-code"];
// What a patch removes by itself; the rest needs judgement, so it goes to the AI agent.
const REMOVABLE = new Set<UntangleKind>(["unused", "unused-module", "commented-code"]);
const HELP: Record<UntangleKind, string> = {
  unused: "Nothing live names it: not in code, strings, templates or config. Code that only dead code uses is listed too, and goes with it.",
  "unused-module": "Nothing in use imports it, and nothing names it in a string or launch file.",
  "test-only": "Only tests use it. Removing it means removing those tests too, so this goes to the AI agent.",
  wrapper: "A function that only passes its arguments to another function of the repo: one more name to read on every change.",
  "unused-param": "Every caller works out a value the function never reads.",
  swallowed: "`except Exception: pass` and friends: a bug here looks like success.",
  "commented-code": "Git keeps the history; commented-out code goes stale and reads like it matters.",
};
const BADGE: Record<UntangleKind, string> = {
  unused: "red", "unused-module": "red", "test-only": "amber", wrapper: "teal", "unused-param": "teal", swallowed: "rose", "commented-code": "outline",
};

export default function Untangle() {
  const { wid, data, openCode, go, reload } = useApp();
  const toast = useToast();
  const [inv, setInv] = useState<UntangleInventory | null>(null);
  const [scope, setScope] = useState<"sure" | "all">("sure");
  const [kind, setKind] = useState<UntangleKind | "">("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<RemovalResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!wid) return;
    setInv(null);
    setPicked(new Set());
    api.untangle(wid).then(setInv).catch((e) => toast(e.message, "error"));
  }, [wid, data?.revision, data?.last_run, toast]);

  const shown = useMemo(
    () => (inv?.items ?? []).filter((i) => scope === "all" || i.confidence === "high"),
    [inv, scope],
  );
  const counts = useMemo(() => {
    const c = new Map<UntangleKind, number>();
    shown.forEach((i) => c.set(i.kind, (c.get(i.kind) ?? 0) + 1));
    return c;
  }, [shown]);
  const kinds = ORDER.filter((k) => counts.get(k) && (!kind || kind === k));
  const chosen = shown.filter((i) => picked.has(i.id));
  const removable = chosen.filter((i) => REMOVABLE.has(i.kind));

  const toggle = (ids: string[], on: boolean) => setPicked((p) => {
    const n = new Set(p);
    ids.forEach((x) => (on ? n.add(x) : n.delete(x)));
    return n;
  });

  const showRemoval = async (ids: string[]) => {
    if (!wid || !ids.length) return;
    setBusy(true);
    try { setPreview(await api.untangleRemove(wid, ids, true)); } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };
  const applyRemoval = async () => {
    if (!wid || !preview) return;
    setBusy(true);
    try {
      const r = await api.untangleRemove(wid, preview.removed, false);
      toast(`Removed ${r.removed.length} item(s), ${r.lines} line(s), in ${r.files.length} file(s). Nothing is committed: run the tests.`);
      setPreview(null);
      setPicked(new Set());
      reload();
    } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };
  const handToAgent = async () => {
    if (!wid || !chosen.length) return;
    setBusy(true);
    try {
      await api.untangleAgent(wid, chosen.map((i) => i.id));
      toast("The AI agent is working on it in a sandbox; follow it and apply its change on the AI agent page");
      go("agent");
    } catch (e) { toast((e as Error).message, "error"); } finally { setBusy(false); }
  };

  if (!inv) return <div className="page"><Spinner /></div>;
  const unusedLines = shown.filter((i) => i.kind === "unused" || i.kind === "unused-module").reduce((a, i) => a + i.lines, 0);
  const sureRemovable = (inv.items ?? []).filter((i) => i.confidence === "high" && REMOVABLE.has(i.kind)).map((i) => i.id);

  return (
    <div className="page grid">
      <div className="page-head">
        <div>
          <h1>Untangle</h1>
          <p>What the code carries that nothing needs, so every change reads less and breaks less. A name counts as used anywhere it appears: code, strings, templates, config. Framework hooks and decorated handlers are never listed{inv.mode === "library" ? ", and neither is this library's public API" : ""}.</p>
        </div>
        <div className="actions">
          <Tabs value={scope} onChange={(v) => { setScope(v); setPicked(new Set()); }}
            options={[{ value: "sure", label: "Sure" }, { value: "all", label: "Everything" }]} />
          {sureRemovable.length > 0 && (
            <button className="btn primary" disabled={busy} onClick={() => showRemoval(sureRemovable)}
              title="One patch removing every sure unused function, class, module and block of commented-out code, with the imports only they used">
              {busy ? <Spinner size={14} /> : <Scissors size={14} />}Remove all sure ({sureRemovable.length})
            </button>
          )}
        </div>
      </div>

      <div className="card">
        <div className="stats">
          <Stat label="Sure findings" value={inv.high} sub={`${inv.items.length - inv.high} more to check by hand`} />
          <Stat label="Unused lines" value={unusedLines} sub="functions, classes and modules nothing uses" />
          <Stat label="Silenced errors" value={inv.counts.swallowed ?? 0} sub="broad excepts that carry on quietly" />
          <Stat label="Read as" value={inv.mode === "library" ? "Library" : "App"} sub={inv.mode === "library" ? "public names are its product" : "every name must earn its place"} />
        </div>
      </div>

      {shown.length === 0 ? (
        <div className="card">
          <Empty icon={<Sparkles size={22} />} title="Nothing to untangle">
            {scope === "sure" && inv.items.length ? "Nothing sure. Switch to Everything for the findings to check by hand." : "No unused code, wrappers, silenced errors or commented-out code found."}
          </Empty>
        </div>
      ) : (
        <>
          <div className="chips" role="group" aria-label="Kind">
            <button className={`chip ${!kind ? "on" : ""}`} aria-pressed={!kind} onClick={() => setKind("")}>All {shown.length}</button>
            {ORDER.filter((k) => counts.get(k)).map((k) => (
              <button key={k} className={`chip ${kind === k ? "on" : ""}`} aria-pressed={kind === k} title={HELP[k]} onClick={() => setKind(kind === k ? "" : k)}>
                {inv.kinds[k]} {counts.get(k)}
              </button>
            ))}
          </div>

          {chosen.length > 0 && (
            <div className="callout accent row" style={{ gap: 10, flexWrap: "wrap", position: "sticky", top: 0, zIndex: 2 }}>
              <strong>{chosen.length} selected</strong>
              <span className="grow" />
              {removable.length > 0 && (
                <button className="btn sm primary" disabled={busy} onClick={() => showRemoval(removable.map((i) => i.id))}
                  title="Preview one patch that removes them, the dead code only they use, and the imports that go with them">
                  <Eraser size={13} />Remove {removable.length}
                </button>
              )}
              <button className="btn sm" disabled={busy} onClick={handToAgent}
                title="The AI agent makes the changes in a sandbox copy, at every call site, and runs the tests; you apply its change">
                <Wand2 size={13} />Hand {chosen.length} to the AI agent
              </button>
              <button className="btn sm ghost" onClick={() => setPicked(new Set())}>Clear</button>
            </div>
          )}

          {kinds.map((k) => {
            const rows = shown.filter((i) => i.kind === k);
            const all = rows.every((i) => picked.has(i.id));
            return (
              <div className="card" key={k}>
                <div className="card-head">
                  <h2><span className={`badge ${BADGE[k]}`}>{rows.length}</span>{inv.kinds[k]}</h2>
                  <div className="right row" style={{ gap: 8 }}>
                    <span className="faint" style={{ fontSize: 12.5, maxWidth: 520 }}><Inline text={HELP[k]} /></span>
                    <button className="btn sm ghost" onClick={() => toggle(rows.map((i) => i.id), !all)}>{all ? "Unselect all" : "Select all"}</button>
                  </div>
                </div>
                <div className="table-scroll"><table className="table">
                  <tbody>
                    {rows.map((i) => <Row key={i.id} item={i} on={picked.has(i.id)} toggle={(on) => toggle([i.id], on)}
                      open={() => openCode(i.file, i.line, [i.line, i.end_line])} />)}
                  </tbody>
                </table></div>
              </div>
            );
          })}
        </>
      )}

      {preview && (
        <Modal title={preview.removed.length ? `Remove ${preview.removed.length} item(s), ${preview.lines} line(s)?` : "Nothing can be removed automatically"}
          onClose={() => setPreview(null)} footer={<>
            <button className="btn" onClick={() => setPreview(null)}>Cancel</button>
            {preview.patch && <CopyButton text={preview.patch} label="Copy patch" />}
            {preview.patch && (
              <button className="btn primary" disabled={busy} onClick={applyRemoval}>
                {busy ? <Spinner size={14} /> : <Scissors size={14} />}Apply to working tree
              </button>
            )}
          </>}>
          <p className="muted" style={{ marginTop: 0 }}>
            Every changed file still parses, and the imports of what goes are dropped everywhere, so nothing breaks at import.
            Nothing is committed: run the tests afterwards.{preview.deleted.length ? ` Deletes ${preview.deleted.length} file(s).` : ""}
          </p>
          {preview.skipped.length > 0 && (
            <div className="callout amber" style={{ marginBottom: 12, fontSize: 13 }}>
              {preview.skipped.length} left out: {preview.skipped.slice(0, 5).map((s) => `${s.id.split(":").slice(1).join(":")} (${s.why})`).join("; ")}
              {preview.skipped.length > 5 ? "…" : ""}
            </div>
          )}
          {preview.patch && <DiffView patch={preview.patch} />}
        </Modal>
      )}
    </div>
  );
}

function Row({ item: i, on, toggle, open }: { item: UntangleItem; on: boolean; toggle: (on: boolean) => void; open: () => void }) {
  return (
    <tr>
      <td style={{ width: 28, verticalAlign: "top", paddingTop: 14 }}>
        <input type="checkbox" checked={on} onChange={(e) => toggle(e.target.checked)} aria-label={`Select ${i.title}`} />
      </td>
      <td style={{ width: "100%" }}>
        <div style={{ fontWeight: 600 }}>
          <Inline text={i.title} />
          {i.confidence !== "high" && <span className="badge amber" style={{ marginLeft: 8 }}>check</span>}
        </div>
        <div className="muted" style={{ fontSize: 13, marginTop: 2 }}><Inline text={i.why} /></div>
        <button className="btn ghost sm mono" style={{ paddingLeft: 0, marginTop: 2, fontSize: 12 }} onClick={open} title="Show the code">
          <Code2 size={12} />{i.file}:{i.line}
        </button>
      </td>
      <td className="mono muted" style={{ whiteSpace: "nowrap", verticalAlign: "top", paddingTop: 14 }}>{i.lines ? `−${i.lines}` : ""}</td>
    </tr>
  );
}
