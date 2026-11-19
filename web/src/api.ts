// Typed client for the Codewright REST API.

export type Proof = "verified" | "unverified" | "broke-tests";

export interface AiCheck {
  verdict: "confirmed" | "needs-changes" | "wrong"; confidence: number; reason: string; risks: string[]; test: string;
  model: string; at: number;
}
export interface ArchInsight {
  id: string; severity: "high" | "medium" | "low"; title: string; why: string; fix: string; file: string; line: number;
  evidence: string; nodes: string[]; source: "rule" | "ai";
}
export interface AiTest {
  verdict: "verified" | "broke" | "test-fails-today" | "no-patch"; detail: string; path: string; test: string;
  before: { ok: boolean; seconds: number; output: string }; after: { ok: boolean; seconds: number; output: string } | null;
  python: string; model: string; at: number;
}
export interface VerifyStep { kind: string; title: string; detail: string; command?: string }
export interface Finding {
  ai_check?: AiCheck;
  ai_test?: AiTest;
  id: string;
  intent: string;
  file: string;
  line: number;
  function: string;
  title: string;
  code_today: string;
  suggestion: string;
  symbol: string;
  api: string;
  gain: string;
  effort: number;
  impact: number;
  risk: string;
  when_not: string;
  threshold: string;
  sources: string[];
  evidence: string;
  snippet: string;
  snippet_lang: string;
  patch: string | null;
  confidence: number;
  hot_path: boolean;
  symbol_check: "ok" | "missing" | "unchecked";
  proof: Proof;
  proof_detail: string;
  score: number;
  rejected_reason: string | null;
  span: [number, number];
  status: "accepted" | "rejected" | "muted" | null;
  card_id: string;
  category: Category; category_label: string;
  perf?: Perf | null;
}

export interface Perf {
  verdict: "faster" | "slower" | "no-change" | "too-small" | "failed";
  before_ms: number; after_ms: number; calls_before: number; calls_after: number;
  tests_before_s: number; tests_after_s: number; rounds: number; speedup: number; note: string;
}

export interface Insight {
  kind: "mute" | "trusted" | "reasons"; intent: string; title: string; path: string;
  accepted: number; rejected: number; rate: number | null; reasons: { reason: string; count: number }[]; message: string;
}

export interface Rule {
  id: string; kind: "forbid-import" | "only-in" | "layers" | "no-blocking-in-async"; reason: string;
  severity: "error" | "warn"; paths: string[]; imports: string[]; exclude: string[];
  layers: { name: string; paths: string[] }[]; calls: string[]; source: string;
}
export interface Violation {
  rule: string; kind: string; severity: "error" | "warn"; file: string; line: number;
  message: string; reason: string; fix: string; target: string; patch?: string | null;
}
export interface ApplyResult { patch: string; applied: string[]; skipped: string[]; files: string[]; rules?: RulesSummary }
export interface RulesSummary {
  rules: Rule[]; violations: Violation[]; errors: number; warnings: number; ok: boolean;
  error: string | null; file: string | null;
}

export interface DepRow {
  name: string; version: string | null; group: string; source: string; imports: string[];
  files: number; test_files: number; requires: number; unique: number; license: string; released: string; flags: string[];
}
export interface DepFinding {
  kind: "overlap" | "stdlib" | "unused" | "license" | "abandoned" | "heavy";
  title: string; deps: string[]; detail: string; suggestion: string; saves: number; effort: number; severity: "info" | "warn";
}
export interface DepAudit {
  python: string; declared: number; locked: number; lockfile: string | null;
  dependencies: DepRow[]; findings: DepFinding[]; could_remove: number;
}

export interface MigrationRow {
  name: string; title: string; library: string | null; installed: string | null; target: string | null;
  places: number; files: number; autofix: number; planned: boolean;
}
export interface MigrationItem { key: string; finding_id: string; title: string; file: string; line: number; autofix: boolean; intent: string }
export interface MigrationStep {
  n: number; files: string[]; items: MigrationItem[]; tests: string[]; depends_on: number[];
  done: boolean; remaining: number; autofix: number;
}
export interface MigrationPlan {
  name: string; title: string; library: string | null; installed: string | null; target: string | null;
  tools: string[]; notes: string; created: number; batch: number; steps: MigrationStep[]; total: number;
  done_items?: number; progress?: number; next_step?: number | null; new_since_plan?: MigrationItem[];
}
export interface StepDetail { step: MigrationStep; patch: string; skipped: string[]; pr_body: string; findings: Finding[] }

export interface PlanPoint {
  level: "blocker" | "concern" | "ok";
  topic: "dependencies" | "reuse" | "code" | "architecture" | "scale" | "completeness";
  title: string; detail: string; evidence: string[]; quote: string;
}
export interface PlanReview {
  verdict: "go" | "revise"; score: number; headline: string; points: PlanPoint[];
  counts: { blocker: number; concern: number; ok: number };
}
export type Category = "architecture" | "agents" | "performance" | "reliability" | "upgrade" | "library";

export interface Dependency { name: string; version: string | null; specifier: string | null; source: string }

export interface Metrics {
  shown: number; verified: number; verified_share: number | null; with_patch: number; hot_path: number;
  dropped: number; accepted: number; rejected: number; acceptance_rate: number | null;
  per_intent: Record<string, { accepted: number; rejected: number; muted: number }>;
  mutes: { intent: string; path: string; reason: string }[];
  constraints: string[];
  insights?: Insight[];
}

export interface ReviewData {
  stack: { python: string | null; python_unsupported?: string | null; sources: string[]; docker_images: string[]; dependencies: Dependency[] };
  files: number; functions: number; entry_points: number; intent_matches: number; seconds: number;
  base: string | null; findings: Finding[]; rejected: Finding[]; muted: Finding[]; metrics: Metrics;
  rules?: RulesSummary;
  scope?: ReviewScope; proved_files?: string[]; reused_proofs?: number;
}
/** What the last review knows about proof: files edited since the tests last ran on them, and what is unproven. */
export interface ReviewScope {
  changed: string[]; changed_count: number; never_reviewed: boolean; unproven: number; unproven_files: number; last_proof: number | null;
}
export interface PlanFile {
  file: string; reason: string; selected: boolean; findings: number; impact: number; hot: number; covered: number; intents: string[]; changed: boolean;
}
export interface ReviewPlan { source: "ai" | "ranking"; note: string; files: PlanFile[]; total: number; changed: number }

export interface Progress {
  stage: string; label: string; step: number; steps: number; stages: { key: string; label: string }[];
  done: number; total: number; percent: number; started: number; proof: boolean;
}

export interface WorkspaceInfo {
  id: string; root: string; name: string; running: boolean; progress?: Progress | null; last_run: number | null; error: string | null;
  has_result: boolean; branch: string | null; findings: number | null; verified: number | null; files: number | null;
  watching?: boolean; revision?: number; graph_ready?: boolean;
}
export interface ChangeItem { id: string; title: string; file: string; line: number }
export interface Revision {
  revision: number; running: boolean; watching: boolean;
  last_change: { at: number; files: string[]; new: ChangeItem[]; resolved: ChangeItem[] } | null;
}

export interface RecentRepo { path: string; name: string; opened: number }
export interface FolderListing {
  path: string; parent: string | null; home: string; git: boolean; python: boolean;
  dirs: { name: string; path: string; git: boolean; python: boolean }[];
}

export interface ReviewPayload extends WorkspaceInfo { result: ReviewData | null; from_cache?: boolean; saved_at?: number }
export interface ActivityEvent { seq: number; at: number; repo: string; message: string; level: string }

export interface MapNode {
  id: string; label: string; kind: string; files: { file: string; line: number; name?: string }[];
  findings: number; hotspot: number; untested: number; detail: string; arch?: number;
  instruction?: string; model?: string;
}
export interface MapEdge { source: string; target: string; label: string; weight: number; manual?: boolean }
export interface MapView { level: string; title: string; nodes: MapNode[]; edges: MapEdge[]; mermaid: string }
export interface EntryPoint {
  id: string; name: string; file: string; line: number; trigger: string;
  app?: boolean; framework?: string; reasons?: string[];
}

export interface Drift {
  design: string; ok: boolean;
  unplanned: { kind: string; systems: string[] }[];
  not_built: { kind: string; components: string[] }[];
  violations: { rule: string; reason: string; file: string; import: string }[];
}

export interface UnusedFile { file: string; reason: string; functions: number }
export interface MapMeta {
  components: string[]; entry_points: EntryPoint[]; unused: UnusedFile[];
  changes: { since: number | null; added_nodes: string[]; removed_nodes: string[]; added_edges: string[]; removed_edges: string[] };
  drift: Drift | null;
}

export interface ModulePage {
  file: string; module: string; doc: string;
  functions: { name: string; line: number; end: number; async: boolean; decorators: string[]; calls: string[];
    fan_in: number; tested: boolean; findings: number }[];
  imports: string[]; external: string[]; imported_by: string[]; findings: string[];
}

export interface Citation { n: number; id: string; file: string; line: number; end: number; title: string; score: number }
export interface KnowledgeCite {
  n: string; id: string; title: string; symbol: string; library: string; versions: string;
  suggestion: string; when_not: string; sources: string[]; fit: { status: FitStatus; detail: string };
}
export interface ChatAnswer {
  answer: string; citations: Citation[]; knowledge?: KnowledgeCite[]; mode: "llm" | "extractive"; llm_error?: string;
}

export interface Release {
  name: string; installed: string | null; latest: string | null; released?: string;
  bump: "major" | "minor" | "patch" | "current" | "unknown"; new_since_last_check: boolean;
  unlocks: { card: string; title: string; topic?: string; reference?: boolean }[];
  preview?: { new: PreviewItem[]; resolved: PreviewItem[] } | null;
}
export interface KnowledgeRow {
  library: string; cards: number; checked: string | null; checked_on: string | null; checked_by: string | null;
  installed: string | null; latest: string | null; released: string | null; status: "behind" | "current" | "unrecorded";
  bump: string; uses: boolean;
}
export interface ReleaseNotes {
  library: string; since: string | null; until: string | null; versions: string[];
  notes: { version: string; date: string; body: string }[]; links: { label: string; url: string }[]; error: string | null;
}
export interface KnowledgeDraft {
  library: string; model: string; notes: ReleaseNotes;
  new_cards: { title: string; symbol: string; versions: string; suggestion: string; gain: string; risk: string; when_not: string; topic: string; sources: string[]; library: string }[];
  outdated: { card: string; why: string; change: string }[];
}
export interface LibraryInfo {
  name: string; found: boolean; version?: string | null; summary: string; requires_python?: string | null;
  links: { label: string; url: string }[]; error: string | null; cards: number; installed: string | null;
}
export interface LibraryDraftCard {
  title: string; symbol: string; versions: string; topic: string; suggestion: string; gain: string; risk: string;
  when_not: string; threshold: string; snippet: string; sources: string[]; library: string;
}
export interface UpgradeItem { id: string; title: string; file: string; line: number; patch: string | null; suggestion: string; intent: string }
export interface UpgradePlan {
  name: string; installed: string | null; version: string;
  requirements: { file: string; changes: { old: string; new: string }[]; diff: string }[];
  lock: { tool: string | null; command: string[]; available: boolean; note: string };
  patches: UpgradeItem[]; manual: UpgradeItem[]; resolved: UpgradeItem[];
  migrations: { name: string; title: string; findings: number }[]; uses: { file: string; names: string[] }[];
}
export interface UpgradeProblem {
  file: string; line: number; function: string; symbol: string; kind: string; issue: string; detail: string;
  old_signature: string | null; new_signature: string | null;
}
export interface UpgradeCheck {
  name: string; version: string; sites: number; symbols: number; files: number; problems: UpgradeProblem[];
  breaking: number; summary: string;
}
export interface UpgradeRewrite {
  patches: { id: string; file: string; function: string; patch: string; explanation: string; fixes: string[] }[];
  skipped: { file: string; function: string; why: string }[]; model: string;
}
export interface UpgradeTrialRun { ok: boolean; passed: number; failed: string[]; counts: Record<string, number>; seconds: number; output: string }
export interface UpgradeTrial {
  before: UpgradeTrialRun | null; after: UpgradeTrialRun | null; new_failures?: string[]; fixed?: string[]; safe?: boolean;
  patches_applied: number; patches_failed: number; lock: { ok: boolean; output: string } | null; error?: string | null;
  tests_ran: boolean; reasons?: string[]; evidence?: string[];
  imports: { modules: number; broken: { module: string; error: string }[]; already_failing: string[]; error: string | null } | null;
  behaviour: { functions?: number; scenarios?: number; changed?: { scenario: string; before: unknown; after: unknown }[]; probe_errors?: string[]; error?: string } | null;
}
export interface UpgradeSafety {
  key: string | null; running: boolean; step: string | null; log: string[]; error: string | null;
  check: UpgradeCheck | null; rewrite: UpgradeRewrite | null; trial: UpgradeTrial | null;
  probes: { probes: { file: string; function: string; code: string }[]; skipped: { file: string; function: string; why: string }[]; model: string | null; why?: string } | null;
}
export interface GitState {
  git: boolean; branch?: string | null; detached?: boolean; head?: string; default?: string; branches: string[];
  changed: { file: string; status: string }[]; suggested_branch?: string; committed?: string;
  remote_branches?: string[]; remotes?: string[]; upstream?: string | null; ahead?: number; behind?: number;
  pulled?: number; pushed?: number;
}
export interface AiUsage { date: string; calls: number; input_tokens: number; output_tokens: number; daily_calls: number }
export interface AiBatch { running: boolean; total: number; done: number; verdicts: Record<string, number>; error: string | null }
export interface AgentFixes {
  agents: { name: string; model?: string; file?: string; line?: number; kind?: string }[];
  links: { from: string; to: string; label: string }[];
  hide: string[]; labels: string[];
}
export interface ScanScope {
  include: string[]; exclude: string[]; total: number; kept: number;
  folders: { path: string; files: number; kept: number; depth: number }[];
}
export interface ReleaseProgress { running: boolean; total: number; done: number; rows: Release[]; error: string | null }
export interface PreviewItem { card: string; title: string; file: string; line: number; autofix: boolean }

export type FitStatus = "fits" | "upgrade" | "older" | "unused" | "unknown";
export interface Card {
  id: string; intent: string; title: string; symbol: string; kind: string; library: string | null;
  versions: string; suggestion: string; gain: string; effort: number; impact: number; risk: string;
  when_not: string; threshold: string; sources: string[]; snippet: string; snippet_lang: string; autofix: boolean;
  topic: string; reference: boolean; extra: string; fit: { status: FitStatus; detail: string }; origin?: "built-in" | "repo";
  rule?: { calls?: string[]; missing_kwarg?: string; in_async?: boolean; paths?: string[] };
}

export type ProviderId = "anthropic" | "openai" | "gemini" | "litellm";
export interface AiSettings {
  provider: ProviderId | ""; model: string; base_url: string; has_key: boolean; key_hint: string;
  configured: boolean; reason: string; from_environment: string[]; from_repo: string[]; litellm_installed: boolean;
  providers: { id: ProviderId; label: string; model: string; needs_key: boolean }[];
}
export interface AiTest { ok: boolean; model: string; reply?: string; ms?: number; error?: string }

export interface Settings {
  model: string; offline: boolean; offline_from_environment: boolean;
  llm: { available: boolean; model: string; provider: string; reason: string }; ai: AiSettings;
  constraints: string[]; mutes: { intent: string; path: string; reason: string }[];
}

export interface IndexState {
  url: string; source: string; is_pypi: boolean; repo: string | null; machine: string | null; env_override: boolean;
}

// ---- node inspector (both maps)
export interface NodeDetails {
  id: string; label: string; kind: string; summary: string;
  facts: { label: string; value: string; mono?: boolean }[];
  sections: { title: string; items: { label: string; sub?: string; file?: string; line?: number; node?: string; flow?: string }[] }[];
  findings: { id: string; title: string; category: Category; category_label: string; intent: string; file: string; line: number; evidence: string; proof: string }[];
  code: { file: string; start: number; end: number; text: string; truncated: boolean } | null;
  text?: { title: string; body: string } | null;
  drill?: { level: string; target: string } | null;
  ask: string;
}

// ---- AI page
export type AiRole = "agents" | "model calls" | "tools" | "runs agents" | "calls AI code" | "imports";
export interface AiFile {
  file: string; sdks: string[]; roles: AiRole[]; agents: string[]; model_calls: number; tools: string[];
  via: string[]; findings: number; unused: string;
}
export interface AiAgent {
  id: string; name: string; var: string; file: string; line: number; framework: string; kind: string;
  model: string; instruction: string; tools: { label: string; file: string; line: number; agent?: string; builtin?: boolean }[];
  children: string[]; child_names: string[]; steps: { name: string; file: string; line: number }[];
}
export interface AiModelCall { file: string; function: string; line: number; api: string; provider: string; model: string }
export interface AiReport {
  files: AiFile[]; agents: AiAgent[]; model_calls: AiModelCall[];
  runs: { file: string; function: string; line: number; agent: string; how: string }[];
  architecture: { nodes: MapNode[]; edges: MapEdge[] }; mermaid: string;
  errors?: { file: string; step: string; error: string; at: string }[];
  insights?: ArchInsight[];
  costs?: { entry: string; node: string; file: string; line: number; calls: number; by_model: Record<string, number>; at_least: boolean; notes: string[] }[];
  summary: { files: number; need_model: number; agents: number; workflows: number; tools: number; model_calls: number;
    models: string[]; frameworks: string[] };
}

// ---- design
export interface Question {
  id: string; text: string; kind: "text" | "number" | "size" | "choice" | "multi"; options: string[];
  default: unknown; hint: string; when: string;
}
export interface DesignState {
  session: { id: string; answers: Record<string, unknown>; overrides: Record<string, number> };
  done: boolean; question: Question | null; progress: { answered: number; total: number };
  questions: Question[]; proposal?: Proposal | null;
}
export interface Component { id: string; label: string; kind: string; tech: string; why: string }
export interface Candidate {
  id: string; name: string; summary: string; components: Component[];
  edges: { source: string; target: string; label: string }[];
  pros: string[]; cons: string[]; flips_when: string[]; fit: number; recommended: boolean;
}
export interface Decision {
  id: string; title: string; context: string; decision: string; alternatives: string[];
  consequences: string[]; revisit_when: string[];
}
export interface Proposal {
  capacity: {
    rows: { metric: string; value: string; how: string }[];
    assumptions: { key: string; value: number; default: number; meaning: string; overridden: boolean }[];
    peak_rps: number; instances: number; data_gb_y3: number;
  };
  versions: Record<string, string>; version_source: Record<string, string>; index?: string | null;
  candidates: Candidate[]; recommended: string; decisions: Decision[];
  data_model: { entities: { name: string; fields: string[] }[]; endpoints: { method: string; path: string; summary: string }[]; source: string };
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* not json */ }
    throw new Error(msg);
  }
  const type = r.headers.get("content-type") || "";
  return (type.includes("json") ? r.json() : r.text()) as Promise<T>;
}

const w = (id: string) => `/api/w/${id}`;
const q = (s: string) => encodeURIComponent(s);

export const api = {
  activity: (since: number, workspace = "") =>
    req<{ events: ActivityEvent[]; last: number }>("GET", `/api/activity?since=${since}&workspace=${q(workspace)}`),
  status: () => req<{ version: string; intents: string[]; workspaces: WorkspaceInfo[] }>("GET", "/api/status"),
  open: (path: string) => req<WorkspaceInfo>("POST", "/api/workspaces", { path }),
  workspaces: () => req<WorkspaceInfo[]>("GET", "/api/workspaces"),
  closeWorkspace: (id: string) => req<{ closed: string }>("DELETE", `/api/workspaces/${id}`),
  recent: () => req<RecentRepo[]>("GET", "/api/recent"),
  forgetRecent: (path: string) => req<{ forgot: string }>("DELETE", `/api/recent?path=${q(path)}`),
  folders: (path = "") => req<FolderListing>("GET", `/api/fs?path=${q(path)}`),
  review: (id: string) => req<ReviewPayload>("GET", `${w(id)}/review`),
  runReview: (id: string, proof: boolean, base: string, bench = false, files?: string[], fresh = false) =>
    req<WorkspaceInfo>("POST", `${w(id)}/review`, { proof, base, bench, files, fresh }),
  reviewPlan: (id: string, ai = true) => req<ReviewPlan>("GET", `${w(id)}/review/plan?ai=${ai}`),
  watch: (id: string, on: boolean) => req<WorkspaceInfo>("POST", `${w(id)}/watch`, { on }),
  revision: (id: string) => req<Revision>("GET", `${w(id)}/revision`),
  feedback: (id: string, fid: string, status: string, reason = "") =>
    req<Metrics>("POST", `${w(id)}/findings/${fid}/feedback`, { status, reason }),
  mute: (id: string, intent: string, path = "", undo = false) => req<Metrics>("POST", `${w(id)}/mute`, { intent, path, undo }),
  apply: (id: string, fid: string) => req<{ applied: boolean; file: string }>("POST", `${w(id)}/findings/${fid}/apply`),
  reportMd: (id: string) => req<string>("GET", `${w(id)}/report.md`),
  prComment: (id: string) => req<string>("GET", `${w(id)}/pr-comment.md`),
  mapMeta: (id: string) => req<MapMeta>("GET", `${w(id)}/map/meta`),
  map: (id: string, level: string, target = "", unused = false) =>
    req<MapView>("GET", `${w(id)}/map/${level}?target=${q(target)}${unused ? "&unused=true" : ""}`),
  ai: (id: string) => req<AiReport>("GET", `${w(id)}/ai`),
  verifySteps: (id: string, fid: string) => req<{ steps: VerifyStep[] }>("GET", `${w(id)}/findings/${fid}/verify`),
  aiTest: (id: string, fid: string) => req<AiTest>("POST", `${w(id)}/findings/${fid}/ai-test`),
  saveAiTest: (id: string, fid: string) => req<{ path: string }>("POST", `${w(id)}/findings/${fid}/ai-test/save`),
  aiCheck: (id: string, fid: string) => req<AiCheck>("POST", `${w(id)}/findings/${fid}/ai-check`),
  knowledgeFreshness: (id: string) => req<KnowledgeRow[]>("GET", `${w(id)}/knowledge/freshness`),
  knowledgeNotes: (id: string, library: string, since: string, until: string) =>
    req<ReleaseNotes>("GET", `${w(id)}/knowledge/notes?library=${q(library)}&since=${q(since)}&until=${q(until)}`),
  knowledgeDraft: (id: string, body: { library: string; since: string; until: string; pasted: string }) =>
    req<KnowledgeDraft>("POST", `${w(id)}/knowledge/draft`, body),
  knowledgeChecked: (id: string, library: string, version: string) =>
    req<KnowledgeRow[]>("POST", `${w(id)}/knowledge/checked`, { library, version }),
  libraryInfo: (id: string, name: string) => req<LibraryInfo>("GET", `${w(id)}/knowledge/library?name=${q(name)}`),
  libraryDraft: (id: string, body: { name: string; focus: string; pasted: string; use_repo: boolean; count: number }) =>
    req<{ library: string; existing: number; cards: LibraryDraftCard[]; model: string }>("POST", `${w(id)}/knowledge/library`, body),
  upgradePlan: (id: string, name: string, version: string) =>
    req<UpgradePlan>("GET", `${w(id)}/upgrade/plan?name=${q(name)}&version=${q(version)}`),
  upgradeBreaking: (id: string, name: string, version: string) =>
    req<{ items: { file: string; change: string; why: string; version: string }[]; model: string; links: { label: string; url: string }[]; versions: string[] }>(
      "POST", `${w(id)}/upgrade/breaking`, { name, version }),
  upgradeSafety: (id: string, step: "check" | "rewrite" | "trial", name: string, version: string, patches: string[] = []) =>
    req<UpgradeSafety>("POST", `${w(id)}/upgrade/safety`, { step, name, version, patches }),
  upgradeSafetyProgress: (id: string) => req<UpgradeSafety>("GET", `${w(id)}/upgrade/safety`),
  upgradeApply: (id: string, body: { name: string; version: string; patch_ids: string[]; branch: string; run_lock: boolean; extra_patches: string[] }) =>
    req<{ branch: string | null; files: string[]; lock: { ok: boolean; command: string; output: string } | null; applied: string[]; skipped: string[]; rewrites_applied?: number; rewrites_failed?: string[] }>(
      "POST", `${w(id)}/upgrade/apply`, body),
  git: (id: string) => req<GitState>("GET", `${w(id)}/git`),
  gitSwitch: (id: string, branch: string) => req<GitState>("POST", `${w(id)}/git/switch`, { branch }),
  gitBranch: (id: string, name: string, start = "") => req<GitState>("POST", `${w(id)}/git/branch`, { name, start }),
  gitFetch: (id: string) => req<GitState>("POST", `${w(id)}/git/fetch`),
  gitPull: (id: string) => req<GitState>("POST", `${w(id)}/git/pull`),
  gitSync: (id: string) => req<GitState>("POST", `${w(id)}/git/sync`),
  gitCommit: (id: string, message: string, files: string[]) => req<GitState>("POST", `${w(id)}/git/commit`, { message, files }),
  aiUsage: () => req<AiUsage>("GET", "/api/settings/ai/usage"),
  setAiBudget: (daily_calls: number) => req<AiUsage>("PUT", "/api/settings/ai/budget", { daily_calls }),
  aiCheckBatch: (id: string, limit: number) => req<AiBatch>("POST", `${w(id)}/ai-check/batch`, { limit }),
  aiCheckBatchProgress: (id: string) => req<AiBatch>("GET", `${w(id)}/ai-check/batch`),
  aiReview: (id: string) => req<{ insights: ArchInsight[]; model?: string; error?: string }>("POST", `${w(id)}/ai/review`),
  aiNode: (id: string, node: string) => req<NodeDetails>("GET", `${w(id)}/ai/node?id=${q(node)}`),
  mapNode: (id: string, node: string, level: string, target = "", unused = false) =>
    req<NodeDetails>("GET", `${w(id)}/inspect?id=${q(node)}&level=${level}&target=${q(target)}${unused ? "&unused=true" : ""}`),
  module: (id: string, path: string) => req<ModulePage>("GET", `${w(id)}/module?path=${q(path)}`),
  source: (id: string, path: string) => req<{ file: string; text: string }>("GET", `${w(id)}/source?path=${q(path)}`),
  chat: (id: string, question: string, history: { role: string; content: string }[]) =>
    req<ChatAnswer>("POST", `${w(id)}/chat`, { question, history }),
  releases: (id: string) => req<Release[]>("GET", `${w(id)}/releases`),
  releasesStart: (id: string) => req<ReleaseProgress>("POST", `${w(id)}/releases/start`),
  releasesProgress: (id: string) => req<ReleaseProgress>("GET", `${w(id)}/releases/progress`),
  rules: (id: string) => req<RulesSummary>("GET", `${w(id)}/rules`),
  applyMany: (id: string, ids: string[] | null, verified_only: boolean, dry_run: boolean) =>
    req<ApplyResult>("POST", `${w(id)}/apply`, { ids, verified_only, dry_run }),
  fixRules: (id: string, keys: string[] | null, dry_run: boolean) =>
    req<ApplyResult>("POST", `${w(id)}/rules/fix`, { keys, dry_run }),
  initRules: (id: string, overwrite = false) => req<RulesSummary>("POST", `${w(id)}/rules/init`, { overwrite }),
  deps: (id: string, online: boolean) => req<DepAudit>("GET", `${w(id)}/deps?online=${online}`),
  migrations: (id: string) =>
    req<{ applicable: MigrationRow[]; catalog: { name: string; title: string; library: string | null }[] }>("GET", `${w(id)}/migrations`),
  planMigration: (id: string, name: string, batch: number) =>
    req<MigrationPlan>("POST", `${w(id)}/migrations/${q(name)}/plan`, { batch }),
  migration: (id: string, name: string) => req<MigrationPlan>("GET", `${w(id)}/migrations/${q(name)}`),
  migrationStep: (id: string, name: string, n: number) => req<StepDetail>("GET", `${w(id)}/migrations/${q(name)}/steps/${n}`),
  applyStep: (id: string, name: string, n: number, extra_patches: string[] = []) =>
    req<{ applied: boolean; files: string[]; failed: string[]; status: MigrationPlan }>("POST", `${w(id)}/migrations/${q(name)}/steps/${n}/apply`, { extra_patches }),
  stepAi: (id: string, name: string, n: number) =>
    req<UpgradeRewrite>("POST", `${w(id)}/migrations/${q(name)}/steps/${n}/ai`),
  critique: (id: string, text: string) => req<PlanReview>("POST", `${w(id)}/critique`, { text }),
  insights: (id: string) => req<Insight[]>("GET", `${w(id)}/insights`),
  agentFixes: (id: string) => req<AgentFixes>("GET", `${w(id)}/ai/fixes`),
  saveAgentFixes: (id: string, fixes: Omit<AgentFixes, "labels">) => req<AgentFixes>("PUT", `${w(id)}/ai/fixes`, { fixes }),
  scanScope: (id: string) => req<ScanScope>("GET", `${w(id)}/scan`),
  saveScanScope: (id: string, include: string[], exclude: string[]) => req<ScanScope>("PUT", `${w(id)}/scan`, { include, exclude }),
  teachCard: (id: string, card: Record<string, unknown>) => req<Card>("POST", `${w(id)}/cards`, { card }),
  forgetCard: (id: string, cardId: string) => req<{ deleted: string }>("DELETE", `${w(id)}/cards/${encodeURIComponent(cardId)}`),
  draftCard: (id: string, body: { topic: string; library: string; notes: string; deepen: string }) =>
    req<Record<string, unknown> & { model: string }>("POST", `${w(id)}/cards/draft`, body),
  cards: (wid?: string | null) => req<Card[]>("GET", `/api/cards${wid ? `?workspace=${encodeURIComponent(wid)}` : ""}`),
  settings: (id: string) => req<Settings>("GET", `${w(id)}/settings`),
  saveSettings: (id: string, s: Partial<{ offline: boolean; constraints: string[] }>) =>
    req<Settings>("POST", `${w(id)}/settings`, s),
  saveAi: (workspace: string, s: Partial<{ provider: string; api_key: string; model: string; base_url: string; clear_key: boolean }>) =>
    req<AiSettings>("PUT", "/api/settings/ai", { ...s, workspace }),
  testAi: (workspace: string) => req<AiTest>("POST", "/api/settings/ai/test", { workspace }),
  index: (id: string) => req<IndexState>("GET", `${w(id)}/index`),
  saveIndex: (id: string, url: string, scope: "repo" | "machine") => req<IndexState>("POST", `${w(id)}/index`, { url, scope }),
  testIndex: (id: string, url = "") => req<{ ok: boolean; detail: string }>("POST", `${w(id)}/index/test`, { url }),
  newDesign: () => req<DesignState>("POST", "/api/design"),
  answer: (sid: string, qid: string, value: unknown) => req<DesignState>("POST", `/api/design/${sid}/answer`, { qid, value }),
  propose: (sid: string, workspace = "") => req<Proposal>("POST", `/api/design/${sid}/propose`, { workspace }),
  assumptions: (sid: string, overrides: Record<string, number | null>) =>
    req<Proposal>("POST", `/api/design/${sid}/assumptions`, { overrides }),
  decisions: (sid: string, cid: string) => req<Decision[]>("GET", `/api/design/${sid}/decisions/${cid}`),
  designMd: (sid: string, cid: string) => req<string>("GET", `/api/design/${sid}/design.md?candidate=${q(cid)}`),
  starterUrl: (sid: string, cid: string) => `/api/design/${sid}/starter.zip?candidate=${q(cid)}`,
  saveDesign: (sid: string, workspace: string, candidate: string) =>
    req<{ saved: string; drift: Drift | null }>("POST", `/api/design/${sid}/save`, { workspace, candidate }),
};
