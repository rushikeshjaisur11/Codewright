import { useEffect, useState } from "react";
import { GitBranch, GitCommitHorizontal, GitBranchPlus } from "lucide-react";
import { useApp } from "./App";
import { api, GitState } from "./api";
import { Modal, Spinner, useToast } from "./ui";

/**
 * Where the fixes go: the current branch, switching to another (only with a clean tree, so nothing is lost),
 * a new branch for fixes (applied patches come along), and committing the fixes. Nothing is pushed.
 */
export default function BranchBar() {
  const { wid, data, runReview } = useApp();
  const toast = useToast();
  const [git, setGit] = useState<GitState | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState<string | null>(null);
  const [committing, setCommitting] = useState<{ message: string; files: Set<string> } | null>(null);
  const refresh = () => wid && api.git(wid).then(setGit).catch(() => setGit(null));
  useEffect(() => { refresh(); }, [wid, data?.revision, data?.last_run]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!git?.git || !wid) return null;
  const dirty = git.changed.length > 0;

  async function act(fn: () => Promise<GitState>, done: string, rescan: boolean) {
    setBusy(true);
    try {
      const g = await fn();
      setGit(g);
      toast(done.replace("{branch}", g.branch ?? "?"));
      if (rescan) await runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  }

  return (
    <div className="prbar" style={{ gap: 8 }}>
      <GitBranch size={15} className="faint" />
      <select className="select mono" style={{ height: 30, fontSize: 13, maxWidth: 280 }} value={git.branch ?? ""} disabled={busy || dirty}
        title={dirty ? "Commit or stash your changes to switch branch" : "Switch branch (the repo is re-scanned)"}
        onChange={(e) => act(() => api.gitSwitch(wid, e.target.value), "Switched to {branch}; re-scanning", true)} aria-label="Branch">
        {git.detached && <option value="">detached at {git.head}</option>}
        {git.branches.map((b) => <option key={b} value={b}>{b}{b === git.default ? " (default)" : ""}</option>)}
      </select>
      <button className="btn sm" disabled={busy} onClick={() => setCreating(git.suggested_branch ?? "codewright/fixes")}
        title="Start a branch for fixes from here; patches you already applied come along">
        <GitBranchPlus size={13} />New fix branch
      </button>
      {dirty ? (
        <>
          <span className="faint" style={{ fontSize: 13 }}>{git.changed.length} uncommitted change{git.changed.length === 1 ? "" : "s"}</span>
          <button className="btn sm primary" disabled={busy} onClick={() => setCommitting({
            message: `Apply Codewright fixes${data?.result?.findings[0] ? `: ${data.result.findings[0].title}` : ""}`,
            files: new Set(git.changed.filter((c) => c.file.endsWith(".py")).map((c) => c.file)),
          })}><GitCommitHorizontal size={13} />Commit fixes</button>
        </>
      ) : <span className="faint" style={{ fontSize: 13 }}>clean · apply patches below, then commit them here</span>}
      {git.branch && git.branch === git.default && dirty && (
        <span className="badge amber" title="Fixes usually go on their own branch">on the default branch: start a fix branch first?</span>
      )}

      {creating !== null && (
        <Modal title="New fix branch" onClose={() => setCreating(null)} footer={<>
          <button className="btn" onClick={() => setCreating(null)}>Cancel</button>
          <button className="btn primary" disabled={busy || !creating.trim()} onClick={async () => {
            await act(() => api.gitBranch(wid, creating.trim()), "Now on {branch}", false);
            setCreating(null);
          }}>{busy ? <Spinner size={14} /> : <GitBranchPlus size={14} />}Create and switch</button>
        </>}>
          <div className="col" style={{ gap: 8 }}>
            <label className="col" style={{ gap: 4 }}><span className="faint" style={{ fontSize: 12.5 }}>Branch name</span>
              <input className="input mono" value={creating} onChange={(e) => setCreating(e.target.value)} autoFocus />
            </label>
            <div className="faint" style={{ fontSize: 13 }}>From <code>{git.branch ?? git.head}</code>. {dirty ? `Your ${git.changed.length} uncommitted change(s) come along.` : ""}</div>
          </div>
        </Modal>
      )}

      {committing && (
        <Modal title={`Commit on ${git.branch ?? git.head}`} onClose={() => setCommitting(null)} footer={<>
          <span className="faint" style={{ fontSize: 12.5, marginRight: "auto" }}>Nothing is pushed.</span>
          <button className="btn" onClick={() => setCommitting(null)}>Cancel</button>
          <button className="btn primary" disabled={busy || !committing.files.size || !committing.message.trim()} onClick={async () => {
            await act(() => api.gitCommit(wid, committing.message, [...committing.files]), "Committed on {branch}", false);
            setCommitting(null);
          }}>{busy ? <Spinner size={14} /> : <GitCommitHorizontal size={14} />}Commit {committing.files.size} file{committing.files.size === 1 ? "" : "s"}</button>
        </>}>
          <div className="col" style={{ gap: 10 }}>
            <textarea className="input" rows={3} value={committing.message} onChange={(e) => setCommitting({ ...committing, message: e.target.value })} aria-label="Commit message" />
            <div className="col" style={{ gap: 4 }}>
              {git.changed.map((c) => (
                <label key={c.file} className="row mono" style={{ gap: 8, fontSize: 13 }}>
                  <input type="checkbox" checked={committing.files.has(c.file)} onChange={() => {
                    const n = new Set(committing.files); n.has(c.file) ? n.delete(c.file) : n.add(c.file); setCommitting({ ...committing, files: n });
                  }} />
                  <span className="badge outline">{c.status}</span>{c.file}
                </label>
              ))}
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
