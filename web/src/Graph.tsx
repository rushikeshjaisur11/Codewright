import { useEffect, useMemo } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background, BackgroundVariant, Controls, Edge, Handle, MarkerType, MiniMap, Node, NodeProps, Position,
  ReactFlow, ReactFlowProvider, useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { AlertCircle, Flame, FlaskConical } from "lucide-react";
import { KIND_STYLE } from "./ui";

export interface GNode {
  id: string;
  label: string;
  kind: string;
  detail?: string;
  tech?: string;
  findings?: number;
  hotspot?: number;
  untested?: number;
}
export interface GEdge { source: string; target: string; label?: string; weight?: number }

type CWData = GNode & { selected: boolean; overlay: Overlay; dir: "LR" | "TB" } & Record<string, unknown>;
export type Overlay = { findings: boolean; hotspots: boolean; untested: boolean };

function CWNode({ data }: NodeProps<Node<CWData>>) {
  const s = KIND_STYLE[data.kind] ?? KIND_STYLE.component;
  const horiz = data.dir === "LR";
  return (
    <div className={`cw-node ${data.selected ? "selected" : ""}`} style={{ borderColor: data.selected ? undefined : `color-mix(in srgb, ${s.color} 45%, var(--border))` }}>
      <Handle type="target" position={horiz ? Position.Left : Position.Top} style={{ opacity: 0 }} />
      <div className="k" style={{ color: s.color }}><span className="dot" />{s.label}</div>
      <div className="l">{data.label}</div>
      {data.tech && <div className="tech">{data.tech}</div>}
      {data.detail && !data.tech && <div className="d">{data.detail}</div>}
      <div className="flags">
        {data.overlay.findings && !!data.findings && <span className="badge accent"><AlertCircle size={10} />{data.findings} finding{data.findings > 1 ? "s" : ""}</span>}
        {data.overlay.hotspots && (data.hotspot ?? 0) >= 2 && <span className="badge red"><Flame size={10} />fan-in {data.hotspot}</span>}
        {data.overlay.untested && !!data.untested && <span className="badge amber"><FlaskConical size={10} />{data.untested} untested</span>}
      </div>
      <Handle type="source" position={horiz ? Position.Right : Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { cw: CWNode };

function layout(nodes: GNode[], edges: GEdge[], dir: "LR" | "TB") {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: dir, nodesep: 36, ranksep: dir === "LR" ? 90 : 70, marginx: 30, marginy: 80 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: 210, height: n.tech ? 92 : 78 });
  for (const e of edges) if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return Object.fromEntries(nodes.map((n) => {
    const p = g.node(n.id);
    return [n.id, { x: p.x - 105, y: p.y - (n.tech ? 46 : 39) }];
  }));
}

function Inner({ nodes, edges, selected, onSelect, overlay, dir, minimap }: Props) {
  const rf = useReactFlow();
  const pos = useMemo(() => layout(nodes, edges, dir ?? "LR"), [nodes, edges, dir]);
  const rfNodes: Node<CWData>[] = useMemo(() => nodes.map((n) => ({
    id: n.id, type: "cw", position: pos[n.id] ?? { x: 0, y: 0 },
    data: { ...n, selected: n.id === selected, overlay: overlay ?? { findings: true, hotspots: true, untested: false }, dir: dir ?? "LR" },
  })), [nodes, pos, selected, overlay, dir]);
  const rfEdges: Edge[] = useMemo(() => edges.map((e, i) => {
    const active = selected && (e.source === selected || e.target === selected);
    return {
      id: `e${i}-${e.source}-${e.target}`, source: e.source, target: e.target, label: e.label, type: "smoothstep",
      animated: !!active,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: active ? "var(--accent)" : "var(--border-strong)" },
      style: { stroke: active ? "var(--accent)" : "var(--border-strong)", strokeWidth: active ? 2 : 1.4 },
      labelBgPadding: [6, 3] as [number, number], labelBgBorderRadius: 4,
    };
  }), [edges, selected]);
  useEffect(() => { const t = setTimeout(() => rf.fitView({ padding: 0.18, duration: 250, maxZoom: 1.05 }), 30); return () => clearTimeout(t); }, [nodes, edges, rf]);
  return (
    <ReactFlow
      nodes={rfNodes} edges={rfEdges} nodeTypes={nodeTypes} fitView fitViewOptions={{ maxZoom: 1.05, padding: 0.18 }} minZoom={0.2} maxZoom={1.8}
      onNodeClick={(_, n) => onSelect?.(n.id)} onPaneClick={() => onSelect?.(null)}
      nodesConnectable={false} proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} color="var(--border-strong)" />
      <Controls showInteractive={false} position="bottom-left" />
      {(minimap ?? nodes.length > 10) && <MiniMap pannable zoomable position="bottom-right" nodeColor={(n) => (KIND_STYLE[(n.data as unknown as CWData).kind] ?? KIND_STYLE.component).color} maskColor="rgba(0,0,0,0.25)" />}
    </ReactFlow>
  );
}

interface Props {
  nodes: GNode[]; edges: GEdge[]; selected?: string | null; onSelect?: (id: string | null) => void;
  overlay?: Overlay; dir?: "LR" | "TB"; minimap?: boolean;
}

export default function Graph(props: Props) {
  return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider>;
}
