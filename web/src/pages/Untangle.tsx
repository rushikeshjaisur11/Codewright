import { useEffect, useMemo, useState } from "react";
import { Code2, Eraser, GitCommitHorizontal, Scissors, Sparkles, Wand2 } from "lucide-react";
import { useApp } from "../App";
import { api, RemovalResult, UntangleInventory, UntangleItem, UntangleKind } from "../api";
import { CopyButton, DiffView, Empty, Inline, Modal, Spinner, Stat, Tabs, useToast } from "../ui";

type Group = "surplus" | "links";
const ORDER: Record<Group, UntangleKind[]> = {
  surplus: ["unused", "unused-module", "test-only", "wrapper", "unused-param", "swallowed", "commented-code"],
  links: ["stale-global", "split-setting", "co-change", "copied-logic", "copied-constant", "call-order", "outside-state"],
};
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
  "co-change": "Files that keep changing in the same commits although neither imports or names the other: whoever changes one has to remember the other.",
  "stale-global": "`from m import X` copies X once; when m reassigns it later (`global X`), this module keeps the old value.",
  "split-setting": "One environment variable read in several modules: its default and parsing are decided in several places, and can disagree.",
  "copied-constant": "The same constant defined in several modules: changing it takes several edits, and the copies can drift apart.",
  "call-order": "A global that starts as None and is set by another function: this works only if that ran first, and nothing makes sure it did.",
  "outside-state": "Another module's state set or mutated from here: what that module does depends on whether this ran.",
  "copied-logic": "The same function body in several places: a fix in one copy misses the others.",
};
const BADGE: Record<UntangleKind, string> = {
  unused: "red", "unused-module": "red", "test-only": "amber", wrapper: "teal", "unused-param": "teal", swallowed: "rose", "commented-code": "outline",
  "co-change": "accent", "stale-global": "red", "split-setting": "amber", "copied-constant": "amber", "call-order": "rose", "outside-state": "rose", "copied-logic": "teal",
};

export default function Untangle() {
  const { wid, data, openCode, go, reload, params } = useApp();
  const toast = useToast();
  const [inv, setInv] = useState<UntangleInventory | null>(null);
  const [scope, setScope] = useState<"sure" | "all">("sure");
  const [group, setGroup] = useState<Group>(params.get("group") === "links" ? "links" : "surplus");
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

  const sure = useMemo(
    () => (inv?.items ?? []).filter((i) => scope === "all" || i.confidence === "high"),
    [inv, scope],
  );
  const shown = useMemo(() => sure.filter((i) => ORDER[group].includes(i.kind)), [sure, group]);
  const counts = useMemo(() => {
    const c = new Map<UntangleKind, number>();
    shown.forEach((i) => c.set(i.kind, (c.get(i.kind) ?? 0) + 1));
    return c;
  }, [shown]);
  const kinds = ORDER[group].filter((k) => counts.get(k) && (!kind || kind === k));
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
  const inGroup = (g: Group) => sure.filter((i) => ORDER[g].includes(i.kind)).length;
  const unusedLines = sure.filter((i) => i.kind === "unused" || i.kind === "unused-module").reduce((a, i) => a + i.lines, 0);
  const sureRemovable = (inv.items ?? []).filter((i) => i.confidence === "high" && REMOVABLE.has(i.kind)).map((i) => i.id);

  return (
    <div className="page grid">
      <div className="page-head">
        <div>
          <h1>Untangle</h1>
          <p>What the code carries that nothing needs, and the links it doesn't show: places that must change together or agree, without the code saying so. A name counts as used anywhere it appears (code, strings, templates, config); framework hooks and decorated handlers are never listed{inv.mode === "library" ? ", and, read as a library, neither is its public API" : ""}.</p>
        </div>
        <div className="actions">
          <Tabs value={group} onChange={(v) => { setGroup(v); setKind(""); setPicked(new Set()); }}
            options={[{ value: "surplus", label: `Surplus ${inGroup("surplus")}` }, { value: "links", label: `Hidden links ${inGroup("links")}` }]} />
          <Tabs value={scope} onChange={(v) => { setScope(v); setPicked(new Set()); }}
            options={[{ value: "sure", label: "Sure" }, { value: "all", label: "Everything" }]} />
          {group === "surplus" && sureRemovable.length > 0 && (
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
          <Stat label="Silenced errors" value={inv.silenced ?? 0} sub="broad excepts that carry on quietly" />
          <Stat label="Hidden links" value={inv.links} sub={`${inv.counts["co-change"] ?? 0} pairs of files that change together`} />
        </div>
      </div>

      {group === "links" && inv.history && (
        <div className="callout amber" style={{ fontSize: 13 }}>Git history: {inv.history}. The findings from the code itself are below.</div>
      )}

      {shown.length === 0 ? (
        <div className="card">
          <Empty icon={<Sparkles size={22} />} title="Nothing to untangle">
            {scope === "sure" && sure.length < inv.items.length ? "Nothing sure here. Switch to Everything for the findings to check by hand."
              : group === "links" ? "No hidden links found: no files that change together without a reason in the code, no settings read in several places, no shared state."
              : "No unused code, wrappers, silenced errors or commented-out code found."}
          </Empty>
        </div>
      ) : (
        <>
          <div className="chips" role="group" aria-label="Kind">
            <button className={`chip ${!kind ? "on" : ""}`} aria-pressed={!kind} onClick={() => setKind("")}>All {shown.length}</button>
            {ORDER[group].filter((k) => counts.get(k)).map((k) => (
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
                      open={(file, line, end) => openCode(file ?? i.file, line ?? i.line, [line ?? i.line, end ?? (line ?? i.end_line)])} />)}
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

function Row({ item: i, on, toggle, open }: {
  item: UntangleItem; on: boolean; toggle: (on: boolean) => void; open: (file?: string, line?: number, end?: number) => void;
}) {
  const other = typeof i.data.other === "string" ? i.data.other : null;
  const subjects = Array.isArray(i.data.subjects) ? (i.data.subjects as string[]) : [];
  const list = [i.data.places, i.data.reads, i.data.defs].find(Array.isArray);
  const places = (list ?? []) as { file: string; line: number }[];
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
        {subjects.length > 0 && (
          <div className="faint" style={{ fontSize: 12.5, marginTop: 4 }}>
            <GitCommitHorizontal size={12} style={{ verticalAlign: "-2px" }} /> {subjects.slice(0, 3).map((x) => `“${x}”`).join(" · ")}
          </div>
        )}
        <div className="row" style={{ gap: 4, flexWrap: "wrap", marginTop: 2 }}>
          {(places.length ? places : [{ file: i.file, line: i.line }]).slice(0, 8).map((p, n) => (
            <button key={n} className="btn ghost sm mono" style={{ paddingLeft: 0, fontSize: 12 }} onClick={() => open(p.file, p.line, p.line)} title="Show the code">
              <Code2 size={12} />{p.file}:{p.line}
            </button>
          ))}
          {other && (
            <button className="btn ghost sm mono" style={{ paddingLeft: 0, fontSize: 12 }} onClick={() => open(other, 1, 1)} title="Show the other file">
              <Code2 size={12} />{other}
            </button>
          )}
        </div>
      </td>
      <td className="mono muted" style={{ whiteSpace: "nowrap", verticalAlign: "top", paddingTop: 14 }}>{i.lines ? `−${i.lines}` : ""}</td>
    </tr>
  );
}
