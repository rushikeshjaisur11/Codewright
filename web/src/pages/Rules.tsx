import { useEffect, useState } from "react";
import { CheckCircle2, Code2, FileCode2, ShieldCheck, Wand2, XCircle } from "lucide-react";
import { useApp } from "../App";
import { api, Rule, RulesSummary } from "../api";
import { Empty, Inline, Spinner, useToast } from "../ui";

const KIND_LABEL: Record<Rule["kind"], string> = {
  "forbid-import": "Forbidden import",
  "only-in": "Only in",
  layers: "Layers",
  "no-blocking-in-async": "No blocking in async",
};

function describe(r: Rule): string {
  if (r.kind === "layers") return r.layers.map((l) => l.name).join(" › ");
  if (r.kind === "only-in") return `${r.imports.join(", ")} only from ${r.paths.join(", ")}`;
  if (r.kind === "no-blocking-in-async") return `in ${r.paths.join(", ")}`;
  return `${r.paths.join(", ")} may not import ${r.imports.join(", ")}`;
}

const EXAMPLE = `rules:
  - id: api-no-db
    kind: forbid-import
    in: app/api
    import: app/db/**
    reason: routes go through app/services
  - id: stripe-in-billing
    kind: only-in
    import: stripe
    in: [app/billing]
  - id: layering
    kind: layers
    layers:
      - {name: api, paths: [app/api]}
      - {name: services, paths: [app/services]}
      - {name: db, paths: [app/db]}
  - id: async-io
    kind: no-blocking-in-async
    in: app
    severity: warn`;

export default function Rules() {
  const { wid, data, openCode, reload } = useApp();
  const toast = useToast();
  const [s, setS] = useState<RulesSummary | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!wid) return;
    setS(null);
    api.rules(wid).then(setS).catch((e) => toast(e.message, "error"));
  }, [wid, data?.revision, data?.last_run, toast]);

  const init = async (overwrite: boolean) => {
    if (!wid) return;
    setBusy(true);
    try {
      const r = await api.initRules(wid, overwrite);
      setS(r);
      toast(`Wrote ${r.file} with ${r.rules.length} rule(s)`);
      reload();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  if (!s) return <div className="page"><Spinner /></div>;
  const byRule = new Map<string, number>();
  s.violations.forEach((v) => byRule.set(v.rule, (byRule.get(v.rule) ?? 0) + 1));

  return (
    <div className="page grid">
      <div className="page-head">
        <div>
          <h1>Architecture rules</h1>
          <p>Decisions the code must keep, checked on every review and by <code>codewright check</code> in CI. Each violation comes with a way round it that keeps behaviour.</p>
        </div>
        <div className="actions">
          <button className="btn" disabled={busy} onClick={() => init(!!s.file)}
            title="Write the one-way dependencies between packages that today's code already keeps">
            {busy ? <Spinner size={14} /> : <Wand2 size={14} />}{s.file ? "Re-infer from today's code" : "Write rules from today's code"}
          </button>
        </div>
      </div>

      {s.error && <div className="callout red">Couldn't read the rules: {s.error}</div>}

      {!s.rules.length && !s.error ? (
        <div className="card">
          <Empty icon={<ShieldCheck size={22} />} title="No rules yet">
            <div className="col" style={{ gap: 10, alignItems: "stretch", textAlign: "left", maxWidth: 620 }}>
              <span>Start from what the code already does (the button above freezes today's dependency directions), or write <code>.codewright/rules.yaml</code> yourself. The saved Design adds its own decisions too.</span>
              <pre className="code" style={{ padding: 12, margin: 0, fontSize: 12.5 }}>{EXAMPLE}</pre>
            </div>
          </Empty>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="card-head">
              <h2>{s.ok ? <CheckCircle2 size={16} color="var(--green)" /> : <XCircle size={16} color="var(--red)" />}
                {s.violations.length ? `${s.errors} error(s), ${s.warnings} warning(s)` : `All ${s.rules.length} rule(s) hold`}</h2>
              <div className="right faint mono" style={{ fontSize: 12 }}>{s.file ?? "design.json"}</div>
            </div>
            {s.violations.length > 0 && (
              <div className="table-scroll"><table className="table">
                <thead><tr><th>Where</th><th>Rule</th><th>What, and the way round it</th></tr></thead>
                <tbody>
                  {s.violations.map((v, i) => (
                    <tr key={i}>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <button className="btn ghost sm mono" style={{ paddingLeft: 0 }} onClick={() => openCode(v.file, v.line)}>
                          <Code2 size={13} />{v.file}:{v.line}
                        </button>
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <span className={`badge ${v.severity === "error" ? "red" : "amber"}`}>{v.severity}</span>{" "}
                        <span className="mono" style={{ fontSize: 12.5 }}>{v.rule}</span>
                      </td>
                      <td>
                        <div><Inline text={v.message} /></div>
                        <div className="muted" style={{ fontSize: 13, marginTop: 3 }}><Inline text={v.fix} /></div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>

          <div className="card">
            <div className="card-head"><h2><FileCode2 size={16} />Rules</h2></div>
            <div className="table-scroll"><table className="table">
              <thead><tr><th>Rule</th><th>Kind</th><th>Says</th><th>Why</th><th>Broken</th></tr></thead>
              <tbody>
                {s.rules.map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.id}{r.severity === "warn" && <span className="badge amber" style={{ marginLeft: 6 }}>warn</span>}</td>
                    <td><span className="badge outline">{KIND_LABEL[r.kind]}</span></td>
                    <td className="mono" style={{ fontSize: 12.5 }}>{describe(r)}</td>
                    <td className="muted" style={{ fontSize: 13 }}>{r.reason || <span className="faint">—</span>}</td>
                    <td className="mono">{byRule.get(r.id) ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </div>
        </>
      )}
    </div>
  );
}
