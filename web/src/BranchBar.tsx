import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, GitBranch, GitBranchPlus, GitCommitHorizontal, GitFork, RefreshCw, RefreshCcwDot } from "lucide-react";
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
  const [creating, setCreating] = useState<{ name: string; from: string | null } | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [committing, setCommitting] = useState<{ message: string; files: Set<string> } | null>(null);
  const refresh = () => wid && api.git(wid).then(setGit).catch(() => setGit(null));
  useEffect(() => { refresh(); }, [wid, data?.revision, data?.last_run]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!git?.git || !wid) return null;
  const dirty = git.changed.length > 0;

  async function act(fn: () => Promise<GitState>, done: string | ((g: GitState) => string), rescan: boolean | ((g: GitState) => boolean)) {
    setBusy(true);
    try {
      const g = await fn();
      setGit(g);
      toast(typeof done === "function" ? done(g) : done.replace("{branch}", g.branch ?? "?"));
      if (typeof rescan === "function" ? rescan(g) : rescan) await runReview(false);
    } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  }
  const ahead = git.ahead ?? 0, behind = git.behind ?? 0;
  const hasRemote = (git.remotes ?? []).length > 0;

  return (
    <div className="prbar" style={{ gap: 8 }}>
      <GitBranch size={15} className="faint" />
      <select className="select mono" style={{ height: 30, fontSize: 13, maxWidth: 280 }} value={git.branch ?? ""} disabled={busy || dirty}
        title={dirty ? "Commit or stash your changes to switch branch" : "Switch branch (the repo is re-scanned)"}
        onChange={(e) => act(() => api.gitSwitch(wid, e.target.value), "Switched to {branch}; re-scanning", true)} aria-label="Branch">
        {git.detached && <option value="">detached at {git.head}</option>}
        {git.branches.map((b) => <option key={b} value={b}>{b}{b === git.default ? " (default)" : ""}</option>)}
      </select>
      <button className="btn sm" disabled={busy} onClick={() => setCreating({ name: git.suggested_branch ?? "codewright/fixes", from: null })}
        title="Start a branch from where you are; patches you already applied come along">
        <GitBranchPlus size={13} />New branch
      </button>
      <button className="btn sm" disabled={busy} onClick={() => setCreating({ name: git.suggested_branch ?? "codewright/fixes", from: git.default ? ((git.remote_branches ?? []).includes(`origin/${git.default}`) ? `origin/${git.default}` : git.default) : (git.branch ?? "") })}
        title="Start a branch from another branch, local or remote">
        <GitFork size={13} />New branch from…
      </button>
      {hasRemote && (
        <span className="row" style={{ gap: 4 }}>
          <span className="mono faint" style={{ fontSize: 12.5 }} title={git.upstream ? `compared with ${git.upstream}` : "no upstream yet: Sync pushes this branch and sets one"}>
            {git.upstream ? <><ArrowUp size={12} />{ahead} <ArrowDown size={12} />{behind}</> : "not pushed"}
          </span>
          <button className="btn ghost sm icon" disabled={busy} aria-label="Fetch" title="Fetch: see what changed on the remote (nothing local changes)"
            onClick={() => act(() => api.gitFetch(wid), (g) => g.upstream ? `${g.branch}: ${g.ahead} to push, ${g.behind} to pull` : "Fetched", false)}>
            <RefreshCw size={13} />
          </button>
          <button className="btn sm" disabled={busy || !git.upstream || behind === 0} onClick={() => act(() => api.gitPull(wid), (g) => `Pulled ${g.pulled ?? 0} commit(s); re-scanning`, (g) => (g.pulled ?? 0) > 0)}
            title={git.upstream ? "Fast-forward to the remote branch (never merges or rewrites)" : "No upstream yet"}>
            <ArrowDown size={13} />Pull{behind ? ` ${behind}` : ""}
          </button>
          <button className="btn sm" disabled={busy || !git.branch} onClick={() => setSyncing(true)} title="Pull, then push this branch">
            <RefreshCcwDot size={13} />Sync
          </button>
        </span>
      )}
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
        <Modal title={creating.from === null ? "New branch" : "New branch from…"} onClose={() => setCreating(null)} footer={<>
          <button className="btn" onClick={() => setCreating(null)}>Cancel</button>
          <button className="btn primary" disabled={busy || !creating.name.trim() || creating.from === ""} onClick={async () => {
            const from = creating.from ?? "";
            await act(() => api.gitBranch(wid, creating.name.trim(), from), "Now on {branch}", !!from);
            setCreating(null);
          }}>{busy ? <Spinner size={14} /> : <GitBranchPlus size={14} />}Create and switch</button>
        </>}>
          <div className="col" style={{ gap: 10 }}>
            <label className="col" style={{ gap: 4 }}><span className="faint" style={{ fontSize: 12.5 }}>Branch name</span>
              <input className="input mono" value={creating.name} onChange={(e) => setCreating({ ...creating, name: e.target.value })} autoFocus aria-label="Branch name" />
            </label>
            {creating.from === null ? (
              <div className="faint" style={{ fontSize: 13 }}>From <code>{git.branch ?? git.head}</code>. {dirty ? `Your ${git.changed.length} uncommitted change(s) come along.` : ""}</div>
            ) : (
              <label className="col" style={{ gap: 4 }}><span className="faint" style={{ fontSize: 12.5 }}>Start from</span>
                <select className="select mono" value={creating.from} onChange={(e) => setCreating({ ...creating, from: e.target.value })} aria-label="Start from">
                  <optgroup label="Local">{git.branches.map((b) => <option key={b} value={b}>{b}</option>)}</optgroup>
                  {(git.remote_branches ?? []).length > 0 && <optgroup label="Remote">{(git.remote_branches ?? []).map((b) => <option key={b} value={b}>{b}</option>)}</optgroup>}
                </select>
                <span className="faint" style={{ fontSize: 12.5 }}>
                  {dirty ? `Your ${git.changed.length} uncommitted change(s) come along if they don't clash. ` : ""}
                  Pick a remote branch (and press the refresh button first) to start from the latest. The new branch doesn't track it, so pushing never lands on it.
                </span>
              </label>
            )}
          </div>
        </Modal>
      )}

      {syncing && (
        <Modal title={`Sync ${git.branch}`} onClose={() => setSyncing(false)} footer={<>
          <button className="btn" onClick={() => setSyncing(false)}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={async () => {
            await act(() => api.gitSync(wid), (g) => `Synced ${g.branch}: pulled ${g.pulled ?? 0}, pushed ${g.pushed ?? 0}`, (g) => (g.pulled ?? 0) > 0);
            setSyncing(false);
          }}>{busy ? <Spinner size={14} /> : <RefreshCcwDot size={14} />}Pull and push</button>
        </>}>
          <div className="col" style={{ gap: 6, fontSize: 13.5 }}>
            {git.upstream ? (<>
              <div>1. Pull {behind ? <b>{behind} commit{behind === 1 ? "" : "s"}</b> : "anything new"} from <code>{git.upstream}</code> (fast-forward only; refused if the branches have diverged).</div>
              <div>2. Push {ahead ? <b>{ahead} commit{ahead === 1 ? "" : "s"}</b> : "any new commits"} to <code>{git.upstream}</code>.</div>
            </>) : (
              <div>Push <code>{git.branch}</code> to <code>{(git.remotes ?? []).includes("origin") ? "origin" : (git.remotes ?? [])[0]}</code> for the first time and track it from now on.</div>
            )}
            {dirty && <div className="faint">Uncommitted changes stay where they are: only commits are pushed.</div>}
            {git.branch === git.default && <div className="badge amber" style={{ alignSelf: "flex-start" }}>This pushes straight to the default branch</div>}
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
