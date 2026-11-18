// Typed client for the Codewright REST API.

export type Proof = "verified" | "unverified" | "broke-tests";

export interface Finding {
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
  stack: { python: string | null; sources: string[]; docker_images: string[]; dependencies: Dependency[] };
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
  watching?: boolean; revision?: number;
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

export interface ReviewPayload extends WorkspaceInfo { result: ReviewData | null }

export interface MapNode {
  id: string; label: string; kind: string; files: { file: string; line: number; name?: string }[];
  findings: number; hotspot: number; untested: number; detail: string; arch?: number;
  instruction?: string; model?: string;
}
export interface MapEdge { source: string; target: string; label: string; weight: number }
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
export interface PreviewItem { card: string; title: string; file: string; line: number; autofix: boolean }

export type FitStatus = "fits" | "upgrade" | "older" | "unused" | "unknown";
export interface Card {
  id: string; intent: string; title: string; symbol: string; kind: string; library: string | null;
  versions: string; suggestion: string; gain: string; effort: number; impact: number; risk: string;
  when_not: string; threshold: string; sources: string[]; snippet: string; snippet_lang: string; autofix: boolean;
  topic: string; reference: boolean; extra: string; fit: { status: FitStatus; detail: string };
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
  aiNode: (id: string, node: string) => req<NodeDetails>("GET", `${w(id)}/ai/node?id=${q(node)}`),
  mapNode: (id: string, node: string, level: string, target = "", unused = false) =>
    req<NodeDetails>("GET", `${w(id)}/inspect?id=${q(node)}&level=${level}&target=${q(target)}${unused ? "&unused=true" : ""}`),
  module: (id: string, path: string) => req<ModulePage>("GET", `${w(id)}/module?path=${q(path)}`),
  source: (id: string, path: string) => req<{ file: string; text: string }>("GET", `${w(id)}/source?path=${q(path)}`),
  chat: (id: string, question: string, history: { role: string; content: string }[]) =>
    req<ChatAnswer>("POST", `${w(id)}/chat`, { question, history }),
  releases: (id: string) => req<Release[]>("GET", `${w(id)}/releases`),
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
  applyStep: (id: string, name: string, n: number) =>
    req<{ applied: boolean; files: string[]; status: MigrationPlan }>("POST", `${w(id)}/migrations/${q(name)}/steps/${n}/apply`),
  critique: (id: string, text: string) => req<PlanReview>("POST", `${w(id)}/critique`, { text }),
  insights: (id: string) => req<Insight[]>("GET", `${w(id)}/insights`),
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
