import { useEffect, useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background, BackgroundVariant, BaseEdge, Controls, Edge, EdgeLabelRenderer, EdgeProps, Handle, MarkerType, MiniMap,
  Node, NodeProps, Position, ReactFlow, ReactFlowProvider, useReactFlow,
} from "@xyflow/react";
import flowCss from "@xyflow/react/dist/style.css?raw";
import {
  AlertCircle, Bot, Boxes, Braces, Clock, Cloud, Cog, Database, FileCode2, Flame, FlaskConical, Globe, HardDrive, Inbox,
  LogIn, LucideIcon, Network, Server, Sparkles, Users, Wrench, Zap,
} from "lucide-react";
import { KIND_STYLE } from "./ui";

// React Flow's stylesheet sets `outline: none` on focused nodes, edges and selections, which hides
// keyboard focus. Load it without those declarations so the app's :focus-visible ring applies.
if (typeof document !== "undefined" && !document.getElementById("cw-flow-css")) {
  const el = document.createElement("style");
  el.id = "cw-flow-css";
  el.textContent = flowCss.replace(/outline:\s*none\s*;?/g, "");
  document.head.prepend(el);
}

export interface GNode {
  id: string;
  label: string;
  kind: string;
  detail?: string;
  tech?: string;
  findings?: number;
  hotspot?: number;
  untested?: number;
  arch?: number;
}
export interface GEdge { source: string; target: string; label?: string; weight?: number; manual?: boolean }
export type Overlay = { findings: boolean; hotspots: boolean; untested: boolean; arch?: boolean };
type Dir = "LR" | "TB";
type Pt = { x: number; y: number };

export const KIND_ICON: Record<string, LucideIcon> = {
  component: Boxes, module: FileCode2, function: Braces, entry: LogIn, service: Server, worker: Cog,
  database: Database, vector: Database, cache: Zap, queue: Inbox, scheduler: Clock, http: Globe, llm: Sparkles,
  ingress: Users, storage: HardDrive, cloud: Cloud, agent: Bot, workflow: Network, tool: Wrench,
};

const NODE_W = 236;

function flags(n: GNode, o: Overlay) {
  return (o.findings && !!n.findings) || (o.hotspots && (n.hotspot ?? 0) >= 2) || (o.untested && !!n.untested) || (o.arch !== false && !!n.arch);
}
function nodeHeight(n: GNode, o: Overlay) {
  return 58 + (n.tech ? 18 : 0) + (flags(n, o) ? 26 : 0);
}

// ---------------------------------------------------------------- nodes

type CWData = GNode & { selected: boolean; dim: boolean; overlay: Overlay; dir: Dir } & Record<string, unknown>;

function CWNode({ data }: NodeProps<Node<CWData>>) {
  const s = KIND_STYLE[data.kind] ?? KIND_STYLE.component;
  const Icon = KIND_ICON[data.kind] ?? Boxes;
  return (
    <div className={`cw-node ${data.selected ? "selected" : ""} ${data.dim ? "dim" : ""}`} style={{ width: NODE_W }}>
      <Handle type="target" position={data.dir === "LR" ? Position.Left : Position.Top} />
      <div className="cw-node__row">
        <span className="cw-node__icon" style={{ color: s.color, background: s.soft }}><Icon size={15} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="l truncate" title={data.label}>{data.label}</div>
          <div className="k truncate">{data.detail ? `${s.label} · ${data.detail}` : s.label}</div>
        </div>
      </div>
      {data.tech && <div className="tech" title={data.tech}>{data.tech}</div>}
      {flags(data, data.overlay) && (
        <div className="flags">
          {data.overlay.arch !== false && !!data.arch && <span className="badge cat cat-architecture" title="Architecture suggestions in this code"><Network size={10} />{data.arch} architecture</span>}
          {data.overlay.findings && !!data.findings && (data.findings - (data.overlay.arch !== false ? data.arch ?? 0 : 0)) > 0 && <span className="badge accent"><AlertCircle size={10} />{data.findings - (data.overlay.arch !== false ? data.arch ?? 0 : 0)} finding{data.findings - (data.overlay.arch !== false ? data.arch ?? 0 : 0) > 1 ? "s" : ""}</span>}
          {data.overlay.hotspots && (data.hotspot ?? 0) >= 2 && <span className="badge red"><Flame size={10} />fan-in {data.hotspot}</span>}
          {data.overlay.untested && !!data.untested && <span className="badge amber"><FlaskConical size={10} />{data.untested} untested</span>}
        </div>
      )}
      <Handle type="source" position={data.dir === "LR" ? Position.Right : Position.Bottom} />
    </div>
  );
}

// ---------------------------------------------------------------- edges: ELK's orthogonal route with rounded corners

type CWEdgeData = { points?: Pt[]; showLabel: boolean; active: boolean; dim: boolean; text?: string; manual?: boolean } & Record<string, unknown>;

function roundedPath(pts: Pt[], r = 8): string {
  if (pts.length < 2) return "";
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
    const d1 = Math.hypot(p1.x - p0.x, p1.y - p0.y) || 1, d2 = Math.hypot(p2.x - p1.x, p2.y - p1.y) || 1;
    const rr = Math.min(r, d1 / 2, d2 / 2);
    const a = { x: p1.x - ((p1.x - p0.x) / d1) * rr, y: p1.y - ((p1.y - p0.y) / d1) * rr };
    const b = { x: p1.x + ((p2.x - p1.x) / d2) * rr, y: p1.y + ((p2.y - p1.y) / d2) * rr };
    d += ` L ${a.x} ${a.y} Q ${p1.x} ${p1.y} ${b.x} ${b.y}`;
  }
  const last = pts[pts.length - 1];
  return `${d} L ${last.x} ${last.y}`;
}

function midpoint(pts: Pt[]): Pt {
  let best = -1, at = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const len = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
    if (len > best) { best = len; at = i; }
  }
  return { x: (pts[at].x + pts[at + 1].x) / 2, y: (pts[at].y + pts[at + 1].y) / 2 };
}

function CWEdge({ sourceX, sourceY, targetX, targetY, data, markerEnd }: EdgeProps<Edge<CWEdgeData>>) {
  const pts = data?.points && data.points.length >= 2 ? data.points : [{ x: sourceX, y: sourceY }, { x: targetX, y: targetY }];
  const mid = midpoint(pts);
  return (
    <>
      <BaseEdge path={roundedPath(pts)} markerEnd={markerEnd}
        style={{ stroke: data?.active ? "var(--accent)" : "var(--edge)", strokeWidth: data?.active ? 1.8 : 1.25,
          strokeDasharray: data?.manual ? "5 4" : undefined,  // added by the team in .codewright/agents.yaml
          opacity: data?.dim ? 0.15 : 1, transition: "opacity 150ms, stroke 150ms" }} />
      {data?.showLabel && data.text && (
        <EdgeLabelRenderer>
          <div className={`cw-edge-label ${data.active ? "active" : ""}`}
            style={{ transform: `translate(-50%, -50%) translate(${mid.x}px, ${mid.y}px)` }}>{data.text}</div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { cw: CWNode };
const edgeTypes = { cw: CWEdge };

// ---------------------------------------------------------------- layout

type Layout = { pos: Record<string, Pt>; routes: Record<string, Pt[]> };

const edgeKey = (e: GEdge, i: number) => `e${i}-${e.source}-${e.target}`;

// ELK runs in a Web Worker, created once: laying out a large map no longer blocks scrolling,
// hover or typing while it computes.
let elkReady: Promise<{ layout: (graph: any) => Promise<any> }> | null = null;
function getElk() {
  elkReady ??= Promise.all([import("elkjs/lib/elk-api.js"), import("elkjs/lib/elk-worker.min.js?worker")]).then(
    ([api, worker]) => new api.default({ workerFactory: () => new worker.default() }),
  );
  return elkReady;
}

async function elkLayout(nodes: GNode[], edges: GEdge[], dir: Dir, o: Overlay): Promise<Layout> {
  const elk = await getElk();
  const ids = new Set(nodes.map((n) => n.id));
  const res = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": dir === "LR" ? "RIGHT" : "DOWN",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": dir === "LR" ? "100" : "72",
      "elk.spacing.nodeNode": "28",
      "elk.spacing.edgeNode": "20",
      "elk.spacing.edgeEdge": "14",
      "elk.layered.spacing.edgeNodeBetweenLayers": "22",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
      "elk.separateConnectedComponents": "true",
      "elk.spacing.componentComponent": "56",
    },
    children: nodes.map((n) => ({ id: n.id, width: NODE_W, height: nodeHeight(n, o) })),
    edges: edges.map((e, i) => ({ id: edgeKey(e, i), sources: [e.source], targets: [e.target] }))
      .filter((e) => ids.has(e.sources[0]) && ids.has(e.targets[0])),
  });
  const pos: Record<string, Pt> = {};
  for (const c of res.children ?? []) pos[c.id] = { x: c.x ?? 0, y: c.y ?? 0 };
  const routes: Record<string, Pt[]> = {};
  for (const e of res.edges ?? []) {
    const s = e.sections?.[0];
    if (s) routes[e.id] = [s.startPoint, ...(s.bendPoints ?? []), s.endPoint];
  }
  return { pos, routes };
}

function dagreLayout(nodes: GNode[], edges: GEdge[], dir: Dir, o: Overlay): Layout {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: dir, nodesep: 32, ranksep: 90 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: NODE_W, height: nodeHeight(n, o) });
  for (const e of edges) if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return {
    pos: Object.fromEntries(nodes.map((n) => {
      const p = g.node(n.id);
      return [n.id, { x: p.x - NODE_W / 2, y: p.y - nodeHeight(n, o) / 2 }];
    })),
    routes: {},
  };
}

// ---------------------------------------------------------------- component

interface Props {
  nodes: GNode[];
  edges: GEdge[];
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  onOpen?: (id: string) => void;
  overlay?: Overlay;
  dir?: Dir;
  minimap?: boolean;
  focus?: { id: string; n: number } | null;
}

const DEFAULT_OVERLAY: Overlay = { findings: true, hotspots: true, untested: false };

function Inner({ nodes, edges, selected, onSelect, onOpen, overlay = DEFAULT_OVERLAY, dir = "LR", minimap, focus }: Props) {
  const rf = useReactFlow();
  const [layout, setLayout] = useState<Layout | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    elkLayout(nodes, edges, dir, overlay)
      .catch(() => dagreLayout(nodes, edges, dir, overlay))
      .then((l) => { if (live) setLayout(l); });
    return () => { live = false; };
  }, [nodes, edges, dir, overlay]);

  useEffect(() => {
    if (!layout) return;
    const t = setTimeout(() => rf.fitView({ padding: 0.14, duration: 260, maxZoom: 1.1 }), 30);
    return () => clearTimeout(t);
  }, [layout, rf]);

  useEffect(() => {
    if (!focus || !layout?.pos[focus.id]) return;
    const n = nodes.find((x) => x.id === focus.id);
    const p = layout.pos[focus.id];
    rf.setCenter(p.x + NODE_W / 2, p.y + (n ? nodeHeight(n, overlay) / 2 : 40), { zoom: 1.05, duration: 320 });
  }, [focus, layout, nodes, overlay, rf]);

  const lit = hover ?? selected ?? null;
  const neighbours = useMemo(() => {
    if (!lit) return null;
    const s = new Set([lit]);
    for (const e of edges) {
      if (e.source === lit) s.add(e.target);
      if (e.target === lit) s.add(e.source);
    }
    return s;
  }, [lit, edges]);

  const rfNodes: Node<CWData>[] = useMemo(() => !layout ? [] : nodes.map((n) => ({
    id: n.id, type: "cw", position: layout.pos[n.id] ?? { x: 0, y: 0 },
    data: { ...n, selected: n.id === selected, dim: !!neighbours && !neighbours.has(n.id), overlay, dir },
  })), [layout, nodes, selected, neighbours, overlay, dir]);

  const fewEdges = edges.length <= 14;
  const rfEdges: Edge<CWEdgeData>[] = useMemo(() => !layout ? [] : edges.map((e, i) => {
    const active = !!lit && (e.source === lit || e.target === lit);
    return {
      id: edgeKey(e, i), source: e.source, target: e.target, type: "cw",
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: active ? "var(--accent)" : "var(--edge)" },
      data: { points: layout.routes[edgeKey(e, i)], active, dim: !!lit && !active, showLabel: active || (fewEdges && !lit), text: e.manual ? `${e.label} · added` : e.label, manual: !!e.manual },
    };
  }), [layout, edges, lit, fewEdges]);

  return (
    <ReactFlow
      nodes={rfNodes} edges={rfEdges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
      minZoom={0.15} maxZoom={2} nodesConnectable={false} nodesDraggable={false} proOptions={{ hideAttribution: true }}
      onNodeClick={(_, n) => onSelect?.(n.id)} onNodeDoubleClick={(_, n) => onOpen?.(n.id)}
      onPaneClick={() => onSelect?.(null)}
      onNodeMouseEnter={(_, n) => setHover(n.id)} onNodeMouseLeave={() => setHover(null)}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border-strong)" />
      <Controls showInteractive={false} position="bottom-left" />
      {(minimap ?? nodes.length > 10) && (
        <MiniMap pannable zoomable position="bottom-right"
          nodeColor={(n) => (KIND_STYLE[(n.data as unknown as CWData).kind] ?? KIND_STYLE.component).color}
          maskColor="color-mix(in oklab, var(--bg) 60%, transparent)" />
      )}
    </ReactFlow>
  );
}

export default function Graph(props: Props) {
  return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider>;
}
