import { useId, useState } from 'react';

/**
 * A chart paired with a toggleable, accessible data-table equivalent (requirement 16.2, task 13.3).
 *
 * Every visualization in the dashboard must have a table equivalent a screen-reader user can read,
 * and the two must present the same data. Rather than each widget re-implementing the toggle, this
 * wrapper owns it: it renders the chart (an SVG, drawn by the caller and marked `aria-hidden` since
 * the table is the accessible representation) and a "Show data table" control that swaps in the
 * caller-provided `<table>`. The chart itself carries a text summary via `aria-label` so the region
 * is never silent.
 *
 * The full Phase 15 accessibility pass formalizes this across every chart and gauge; this is the
 * shared primitive it builds on, introduced here so 13.3's charts ship with their table from day one.
 */
export function ChartWithTable({
  label,
  chart,
  table,
}: {
  readonly label: string;
  readonly chart: React.ReactNode;
  readonly table: React.ReactNode;
}): React.JSX.Element {
  const [showTable, setShowTable] = useState(false);
  const tableId = useId();

  return (
    <figure className="chart" aria-label={label}>
      {!showTable && (
        <div className="chart__canvas" role="img" aria-label={label}>
          {chart}
        </div>
      )}
      {showTable && (
        <div className="chart__table" id={tableId}>
          {table}
        </div>
      )}
      <button
        type="button"
        className="button button--subtle chart__toggle focus-ring"
        aria-expanded={showTable}
        aria-controls={showTable ? tableId : undefined}
        onClick={() => {
          setShowTable((prev) => !prev);
        }}
      >
        {showTable ? 'Show chart' : 'Show data table'}
      </button>
    </figure>
  );
}
