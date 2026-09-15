import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { ModuleState, type ModuleStateModel } from '../../../components/ModuleState';
import { formatCents, humanizeEnum } from './format';
import { NetworkTree } from './NetworkTree';
import { isRestrictedNode, type GraphNodeData, type RelationshipsData } from './types';
import { useSubResource } from './useSubResource';

/**
 * Relationship network widget (task 13.7, requirements 6.1–6.6).
 *
 * A canvas over the customer's entitlement-redacted neighbourhood subgraph, drawn as a radial SVG
 * layout centred on the focus customer (the design names Cytoscape; this ships a dependency-free
 * SVG canvas with the same behaviour — a full Cytoscape swap is a later enhancement, and Phase 15
 * adds the keyboard-navigable tree mirror). A node inspector shows the selected node's detail;
 * clicking an entitled `Customer` node navigates to that customer's 360 view. A restricted,
 * out-of-book customer is rendered as structure-only — the node and its edge are kept, but it
 * carries no identity (requirement 6.4). Inferred edges are visually distinct and show their
 * confidence (requirement 6.6). A household panel shows the household rollups.
 *
 * The relationship graph is not part of the composed 360 payload, so it is fetched from
 * `/relationships`; the household rollup from `/household`.
 */
export function RelationshipWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const relationships = useSubResource<RelationshipsData>(
    `/customers/${encodeURIComponent(customerId)}/relationships`,
  );

  const model: ModuleStateModel = useMemo(() => {
    if (relationships.kind === 'loading') {
      return { status: 'loading' };
    }
    if (relationships.kind === 'error') {
      return {
        status: 'error',
        errorMessage: relationships.message,
        correlationId: relationships.correlationId,
      };
    }
    const masked = relationships.meta.masked_fields ?? [];
    return {
      status: masked.length > 0 ? 'restricted' : 'ready',
      maskedFields: masked,
    };
  }, [relationships]);

  const data = relationships.kind === 'ready' ? relationships.data : undefined;
  const nodes = data?.nodes ?? [];
  const edges = data?.edges ?? [];
  const isEmpty = relationships.kind === 'ready' && nodes.length <= 1;

  return (
    <ModuleState title="Relationship network" state={model} isEmpty={isEmpty}>
      {data !== undefined && nodes.length > 0 && (
        <Network customerId={customerId} nodes={nodes} edges={edges} />
      )}
    </ModuleState>
  );
}

interface HouseholdResponse {
  readonly household: { readonly household_name?: string; readonly member_count?: number } | null;
  readonly net_worth_cents?: number | null;
  readonly total_deposits_cents?: number | null;
  readonly product_count?: number | null;
  readonly member_count?: number | null;
}

/**
 * Household card — the household rollups, split out of the relationship network so each is its own
 * dashboard card (network diagram vs household summary). Fetches `/household`.
 */
export function HouseholdWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const household = useSubResource<HouseholdResponse>(
    `/customers/${encodeURIComponent(customerId)}/household`,
  );
  const model: ModuleStateModel =
    household.kind === 'loading'
      ? { status: 'loading' }
      : household.kind === 'error'
        ? {
            status: 'error',
            errorMessage: household.message,
            correlationId: household.correlationId,
          }
        : { status: 'ready' };
  const isEmpty = household.kind === 'ready' && household.data.household === null;

  return (
    <ModuleState
      title="Household"
      state={model}
      isEmpty={isEmpty}
      emptyLabel="No household on file."
    >
      {household.kind === 'ready' && household.data.household !== null && (
        <div className="widget">
          <HouseholdPanel household={household} />
        </div>
      )}
    </ModuleState>
  );
}

function Network({
  customerId,
  nodes,
  edges,
}: {
  readonly customerId: string;
  readonly nodes: readonly GraphNodeData[];
  readonly edges: RelationshipsData['edges'];
}): React.JSX.Element {
  const navigate = useNavigate();
  const [selected, setSelected] = useState<GraphNodeData | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [view, setView] = useState<'diagram' | 'tree'>('diagram');

  const focusId = `CUSTOMER:${customerId}`;
  const layout = useMemo(() => radialLayout(nodes, focusId), [nodes, focusId]);
  const positions = layout.positions;

  // Degree (connection count) per node, used to size nodes by importance.
  const degree = useMemo(() => {
    const counts = new Map<string, number>();
    for (const edge of edges ?? []) {
      counts.set(edge.src_id, (counts.get(edge.src_id) ?? 0) + 1);
      counts.set(edge.dst_id, (counts.get(edge.dst_id) ?? 0) + 1);
    }
    return counts;
  }, [edges]);

  const allEdges = edges ?? [];
  const inferredCount = allEdges.filter((e) => e.is_inferred).length;
  // The node whose incident edges should be emphasised: the hovered one, else the selected one.
  const active = hovered ?? selected?.node_id ?? null;

  return (
    <div className="widget">
      {/* A compact stat strip so the network's shape is legible before reading the diagram. */}
      <div className="graph-stats" aria-hidden="true">
        <span className="graph-stat">
          <span className="graph-stat__icon">🕸️</span>
          <span className="graph-stat__value">{Math.max(0, nodes.length - 1)}</span>
          <span className="graph-stat__label">connections</span>
        </span>
        <span className="graph-stat">
          <span className="graph-stat__icon">✨</span>
          <span className="graph-stat__value">{inferredCount}</span>
          <span className="graph-stat__label">inferred</span>
        </span>
      </div>

      <div className="viewswitch" role="group" aria-label="Relationship network view">
        <button
          type="button"
          className={`viewswitch__btn focus-ring${view === 'diagram' ? ' viewswitch__btn--active' : ''}`}
          aria-pressed={view === 'diagram'}
          onClick={() => {
            setView('diagram');
          }}
        >
          Diagram
        </button>
        <button
          type="button"
          className={`viewswitch__btn focus-ring${view === 'tree' ? ' viewswitch__btn--active' : ''}`}
          aria-pressed={view === 'tree'}
          onClick={() => {
            setView('tree');
          }}
        >
          Tree
        </button>
      </div>

      {view === 'tree' ? (
        <NetworkTree
          focusId={focusId}
          nodes={nodes}
          edges={edges ?? []}
          selectedId={selected?.node_id ?? null}
          onSelect={setSelected}
        />
      ) : (
        <svg
          className="graph"
          viewBox="0 0 200 200"
          role="group"
          aria-label="Relationship network diagram. Switch to the tree view for keyboard navigation."
        >
          <defs>
            <radialGradient id="graph-focus-halo" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="var(--color-accent-strong)" stopOpacity="0.35" />
              <stop offset="100%" stopColor="var(--color-accent-strong)" stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* Soft concentric guide rings so the canvas reads as a relationship map. */}
          <circle cx="100" cy="100" r="72" className="graph__ring" />
          <circle cx="100" cy="100" r="40" className="graph__ring" />

          {allEdges.map((edge, i) => {
            const from = positions.get(edge.src_id);
            const to = positions.get(edge.dst_id);
            if (from === undefined || to === undefined) {
              return null;
            }
            const emphasised =
              active !== null && (edge.src_id === active || edge.dst_id === active);
            const dimmed = active !== null && !emphasised;
            const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
            // A gentle curve: pull the control point slightly off the straight line.
            const cx = mid.x + (to.y - from.y) * 0.12;
            const cy = mid.y - (to.x - from.x) * 0.12;
            const strength = edge.confidence ?? 1;
            return (
              <g key={`${edge.src_id}-${edge.dst_id}-${i}`}>
                <path
                  d={`M ${String(from.x)} ${String(from.y)} Q ${String(cx)} ${String(cy)} ${String(to.x)} ${String(to.y)}`}
                  fill="none"
                  className={`graph__edge${edge.is_inferred ? ' graph__edge--inferred' : ''}${
                    emphasised ? ' graph__edge--active' : ''
                  }${dimmed ? ' graph__edge--dim' : ''}`}
                  style={{ strokeWidth: 0.6 + strength * 1.1 }}
                />
                {edge.is_inferred && edge.confidence != null && emphasised && (
                  <text x={cx} y={cy} className="graph__edge-label" textAnchor="middle">
                    {Math.round(edge.confidence * 100)}%
                  </text>
                )}
              </g>
            );
          })}
          {nodes.map((node) => {
            const pos = positions.get(node.node_id);
            if (pos === undefined) {
              return null;
            }
            const restricted = isRestrictedNode(node);
            const isFocus = node.node_id === focusId;
            const deg = degree.get(node.node_id) ?? 0;
            const radius = isFocus ? 11 : Math.min(10, 6 + deg * 0.9);
            const dimmed = active !== null && active !== node.node_id;
            return (
              <g
                key={node.node_id}
                transform={`translate(${String(pos.x)}, ${String(pos.y)})`}
                className={`graph__node-group${dimmed ? ' graph__node-group--dim' : ''}`}
                onMouseEnter={() => {
                  setHovered(node.node_id);
                }}
                onMouseLeave={() => {
                  setHovered(null);
                }}
              >
                {isFocus && <circle r={22} fill="url(#graph-focus-halo)" />}
                <circle
                  r={radius}
                  className={nodeClass(
                    node,
                    restricted,
                    isFocus,
                    selected?.node_id === node.node_id,
                  )}
                  tabIndex={0}
                  role="button"
                  aria-label={
                    restricted
                      ? 'Restricted customer (structure only)'
                      : `${node.node_type}: ${node.label}`
                  }
                  onClick={() => {
                    setSelected(node);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelected(node);
                    }
                  }}
                />
                {/* A type glyph inside the node (decorative — the accessible name is on the circle). */}
                <text
                  className="graph__node-glyph"
                  textAnchor="middle"
                  dominantBaseline="central"
                  aria-hidden="true"
                  style={{ fontSize: `${String(radius * 1.1)}px` }}
                >
                  {nodeGlyph(node, restricted, isFocus)}
                </text>
              </g>
            );
          })}
        </svg>
      )}

      {view === 'diagram' && (
        <p className="graph__legend">
          <span className="graph__legend-item">
            <span className="graph__legend-glyph">👤</span> Customer
          </span>
          <span className="graph__legend-item">
            <span className="graph__legend-glyph">🏢</span> Entity
          </span>
          <span className="graph__legend-item">
            <span className="graph__legend-glyph">🔒</span> Restricted
          </span>
          <span className="graph__legend-item">
            <span className="graph__swatch graph__swatch--inferred-edge" /> Inferred link
          </span>
        </p>
      )}

      {selected !== null && (
        <NodeInspector
          node={selected}
          focusCustomerId={customerId}
          edges={edges ?? []}
          onNavigate={(targetCustomerId) => {
            void navigate(`/customers/${encodeURIComponent(targetCustomerId)}`);
          }}
        />
      )}
    </div>
  );
}

function NodeInspector({
  node,
  focusCustomerId,
  edges,
  onNavigate,
}: {
  readonly node: GraphNodeData;
  readonly focusCustomerId: string;
  readonly edges: NonNullable<RelationshipsData['edges']>;
  readonly onNavigate: (customerId: string) => void;
}): React.JSX.Element {
  const restricted = isRestrictedNode(node);
  const isCustomer = node.node_type === 'Customer';
  const incident = edges.filter((e) => e.src_id === node.node_id || e.dst_id === node.node_id);
  // Offer navigation only for an entitled customer node that is not the one already in view.
  const canNavigate =
    isCustomer && !restricted && node.entity_id !== '' && node.entity_id !== focusCustomerId;

  return (
    <div className="inspector" role="group" aria-label="Node detail">
      <div className="inspector__head">
        <span className="inspector__avatar" aria-hidden="true">
          {nodeGlyph(node, restricted, false)}
        </span>
        <div>
          <h3 className="inspector__title">{restricted ? 'Restricted customer' : node.label}</h3>
          <span className="badge inspector__type-badge">{humanizeEnum(node.node_type)}</span>
        </div>
      </div>
      <dl className="kv">
        <dt className="kv__key">Type</dt>
        <dd className="kv__val">{humanizeEnum(node.node_type)}</dd>
        <dt className="kv__key">Connections</dt>
        <dd className="kv__val">{incident.length}</dd>
      </dl>
      {restricted ? (
        <p className="inspector__restricted">
          This customer is outside your book. Only the connection is shown; their identity is
          withheld.
        </p>
      ) : (
        canNavigate && (
          <button
            type="button"
            className="button focus-ring"
            onClick={() => {
              onNavigate(node.entity_id);
            }}
          >
            Open 360 view
          </button>
        )
      )}
    </div>
  );
}

function HouseholdPanel({
  household,
}: {
  readonly household: ReturnType<typeof useSubResource<HouseholdResponse>>;
}): React.JSX.Element {
  if (household.kind !== 'ready' || household.data.household === null) {
    return <></>;
  }
  const data = household.data;
  const members = data.member_count ?? data.household?.member_count ?? null;
  return (
    <div className="household">
      <h3 className="widget__subhead">Household</h3>
      {data.household?.household_name != null && data.household.household_name !== '' && (
        <p className="household__name">{data.household.household_name}</p>
      )}
      <div className="household__stats">
        <HouseholdStat icon="👥" label="Members" value={members != null ? String(members) : '—'} />
        <HouseholdStat
          icon="💰"
          label="Net worth"
          value={formatCents(data.net_worth_cents ?? undefined)}
        />
        <HouseholdStat
          icon="🏦"
          label="Deposits"
          value={formatCents(data.total_deposits_cents ?? undefined)}
        />
        <HouseholdStat
          icon="📦"
          label="Products"
          value={data.product_count != null ? String(data.product_count) : '—'}
        />
      </div>
    </div>
  );
}

function HouseholdStat({
  icon,
  label,
  value,
}: {
  readonly icon: string;
  readonly label: string;
  readonly value: string;
}): React.JSX.Element {
  return (
    <div className="household-stat">
      <span className="household-stat__icon" aria-hidden="true">
        {icon}
      </span>
      <span className="household-stat__body">
        <span className="household-stat__value">{value}</span>
        <span className="household-stat__label">{label}</span>
      </span>
    </div>
  );
}

interface Point {
  readonly x: number;
  readonly y: number;
}

/** A radial layout: the focus node at centre, its neighbours evenly around it (rest on an outer ring). */
function radialLayout(
  nodes: readonly GraphNodeData[],
  focusId: string,
): { positions: Map<string, Point> } {
  const positions = new Map<string, Point>();
  const centre: Point = { x: 100, y: 100 };
  positions.set(focusId, centre);

  const others = nodes.filter((n) => n.node_id !== focusId);
  const radius = 72;
  others.forEach((node, index) => {
    const angle = (2 * Math.PI * index) / Math.max(1, others.length);
    positions.set(node.node_id, {
      x: centre.x + radius * Math.cos(angle),
      y: centre.y + radius * Math.sin(angle),
    });
  });
  return { positions };
}

/** A decorative type glyph drawn inside a node. Purely visual — the accessible name is on the circle. */
function nodeGlyph(node: GraphNodeData, restricted: boolean, isFocus: boolean): string {
  if (restricted) {
    return '🔒';
  }
  if (isFocus) {
    return '⭐';
  }
  switch (node.node_type) {
    case 'Customer':
      return '👤';
    case 'Household':
      return '🏠';
    case 'Employer':
    case 'Organization':
      return '🏢';
    case 'Account':
      return '💼';
    default:
      return '•';
  }
}

function nodeClass(
  node: GraphNodeData,
  restricted: boolean,
  isFocus: boolean,
  isSelected: boolean,
): string {
  const classes = ['graph__node'];
  if (restricted) {
    classes.push('graph__node--restricted');
  } else if (node.node_type === 'Customer') {
    classes.push('graph__node--customer');
  } else {
    classes.push('graph__node--entity');
  }
  if (isFocus) {
    classes.push('graph__node--focus');
  }
  if (isSelected) {
    classes.push('graph__node--selected');
  }
  return classes.join(' ');
}
