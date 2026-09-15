import { useCallback, useMemo, useRef } from 'react';

import { humanizeEnum } from './format';
import { isRestrictedNode, type GraphEdgeData, type GraphNodeData } from './types';

/**
 * Keyboard-navigable tree mirror of the relationship graph canvas (task 15.1, requirement 16.3).
 *
 * The SVG canvas in {@link ./RelationshipWidget} is a rich visual, but a mouse-and-sight artefact: a
 * keyboard or screen-reader user cannot explore a radial diagram. This tree is the accessible peer —
 * it presents the *identical* nodes and edges, so nothing in the graph is unreachable without a
 * pointer. The focus customer is the tree root; every other node hangs off it as a child, labelled
 * with the node's identity and the relationship (edge type, and whether it is inferred and with what
 * confidence) that connects it to the focus. Selecting a tree item drives the same node inspector the
 * canvas does, so the two views stay in lockstep.
 *
 * It follows the WAI-ARIA tree pattern: a single tab stop (roving `tabIndex`), Up/Down to move
 * between visible items, Home/End to jump to the ends, and Enter/Space to select. That is the full
 * keyboard traversal the phase gate asks for over the relationship graph.
 */
export function NetworkTree({
  focusId,
  nodes,
  edges,
  selectedId,
  onSelect,
}: {
  readonly focusId: string;
  readonly nodes: readonly GraphNodeData[];
  readonly edges: readonly GraphEdgeData[];
  readonly selectedId: string | null;
  readonly onSelect: (node: GraphNodeData) => void;
}): React.JSX.Element {
  // Flattened, keyboard-traversable order: the focus root first, then its neighbours in graph order.
  const ordered = useMemo(() => orderNodes(nodes, focusId), [nodes, focusId]);
  const focusNode = ordered.find((n) => n.node_id === focusId) ?? ordered[0];
  const children = ordered.filter((n) => n.node_id !== focusNode?.node_id);

  // The roving tabindex target: the selected item if any, else the root.
  const activeId = selectedId ?? focusNode?.node_id ?? '';
  const itemRefs = useRef(new Map<string, HTMLLIElement>());

  const focusItem = useCallback((nodeId: string) => {
    itemRefs.current.get(nodeId)?.focus();
  }, []);

  const move = useCallback(
    (fromId: string, delta: number) => {
      const index = ordered.findIndex((n) => n.node_id === fromId);
      if (index < 0) {
        return;
      }
      const next = Math.min(ordered.length - 1, Math.max(0, index + delta));
      const target = ordered[next];
      if (target !== undefined) {
        focusItem(target.node_id);
      }
    },
    [ordered, focusItem],
  );

  const jump = useCallback(
    (to: 'start' | 'end') => {
      const target = to === 'start' ? ordered[0] : ordered[ordered.length - 1];
      if (target !== undefined) {
        focusItem(target.node_id);
      }
    },
    [ordered, focusItem],
  );

  if (focusNode === undefined) {
    return <p className="module__empty">No relationships to show.</p>;
  }

  const edgeFor = (nodeId: string): GraphEdgeData | undefined =>
    edges.find(
      (e) =>
        (e.src_id === focusId && e.dst_id === nodeId) ||
        (e.dst_id === focusId && e.src_id === nodeId),
    );

  return (
    <ul className="network-tree" role="tree" aria-label="Relationship network, keyboard-navigable">
      <TreeItem
        node={focusNode}
        edge={undefined}
        isRoot
        selected={selectedId === focusNode.node_id}
        tabbable={activeId === focusNode.node_id}
        registerRef={(el) => registerRef(itemRefs, focusNode.node_id, el)}
        onSelect={onSelect}
        onMove={(delta) => {
          move(focusNode.node_id, delta);
        }}
        onJump={jump}
      >
        {children.length > 0 && (
          <ul role="group" className="network-tree__group">
            {children.map((node) => (
              <TreeItem
                key={node.node_id}
                node={node}
                edge={edgeFor(node.node_id)}
                selected={selectedId === node.node_id}
                tabbable={activeId === node.node_id}
                registerRef={(el) => registerRef(itemRefs, node.node_id, el)}
                onSelect={onSelect}
                onMove={(delta) => {
                  move(node.node_id, delta);
                }}
                onJump={jump}
              />
            ))}
          </ul>
        )}
      </TreeItem>
    </ul>
  );
}

function TreeItem({
  node,
  edge,
  isRoot = false,
  selected,
  tabbable,
  registerRef,
  onSelect,
  onMove,
  onJump,
  children,
}: {
  readonly node: GraphNodeData;
  readonly edge: GraphEdgeData | undefined;
  readonly isRoot?: boolean;
  readonly selected: boolean;
  readonly tabbable: boolean;
  readonly registerRef: (el: HTMLLIElement | null) => void;
  readonly onSelect: (node: GraphNodeData) => void;
  readonly onMove: (delta: number) => void;
  readonly onJump: (to: 'start' | 'end') => void;
  readonly children?: React.ReactNode;
}): React.JSX.Element {
  const restricted = isRestrictedNode(node);
  const identity = restricted ? 'Restricted customer' : node.label;
  const relationship = describeEdge(edge);

  return (
    <li
      ref={registerRef}
      role="treeitem"
      aria-selected={selected}
      aria-level={isRoot ? 1 : 2}
      tabIndex={tabbable ? 0 : -1}
      className={`network-tree__item${selected ? ' network-tree__item--selected' : ''}${
        restricted ? ' network-tree__item--restricted' : ''
      }`}
      onClick={(event) => {
        event.stopPropagation();
        onSelect(node);
      }}
      onKeyDown={(event) => {
        switch (event.key) {
          case 'Enter':
          case ' ':
            event.preventDefault();
            onSelect(node);
            break;
          case 'ArrowDown':
            event.preventDefault();
            onMove(1);
            break;
          case 'ArrowUp':
            event.preventDefault();
            onMove(-1);
            break;
          case 'Home':
            event.preventDefault();
            onJump('start');
            break;
          case 'End':
            event.preventDefault();
            onJump('end');
            break;
          default:
            break;
        }
      }}
    >
      <span className="network-tree__row">
        <span className="network-tree__label">
          {isRoot && <span className="network-tree__focus-tag">Focus</span>} {identity}
        </span>
        <span className="network-tree__type">{humanizeEnum(node.node_type)}</span>
        {relationship !== null && <span className="network-tree__edge">{relationship}</span>}
      </span>
      {children}
    </li>
  );
}

/** A human sentence for the edge joining a neighbour to the focus, mirroring the canvas's edges. */
function describeEdge(edge: GraphEdgeData | undefined): string | null {
  if (edge === undefined) {
    return null;
  }
  const kind = humanizeEnum(edge.edge_type);
  if (!edge.is_inferred) {
    return kind;
  }
  const confidence =
    edge.confidence === null || edge.confidence === undefined
      ? ''
      : ` ${Math.round(edge.confidence * 100)}% confidence`;
  return `${kind} · inferred${confidence}`;
}

/** Focus node first (as the root), then neighbours in their given order. */
function orderNodes(nodes: readonly GraphNodeData[], focusId: string): readonly GraphNodeData[] {
  const focus = nodes.filter((n) => n.node_id === focusId);
  const rest = nodes.filter((n) => n.node_id !== focusId);
  return [...focus, ...rest];
}

function registerRef(
  refs: React.RefObject<Map<string, HTMLLIElement>>,
  nodeId: string,
  el: HTMLLIElement | null,
): void {
  const map = refs.current;
  if (map === null) {
    return;
  }
  if (el === null) {
    map.delete(nodeId);
  } else {
    map.set(nodeId, el);
  }
}
