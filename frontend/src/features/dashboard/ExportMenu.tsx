import { useEffect, useRef, useState } from 'react';

import type { Dashboard360 } from './dashboardData';
import { exportCustomerData, type ExportFormat } from './exportCustomerData';

/**
 * Export-data control (visual + client-side export).
 *
 * A button that opens a small menu offering the loaded customer's 360 data as Excel, CSV, JSON or
 * PDF. It exports only what the dashboard already fetched and the role is entitled to see (masked
 * fields stay masked, failed modules stay absent) — no new network calls and no server changes. The
 * menu closes on outside click, on Escape, and after a choice.
 */

interface FormatOption {
  readonly format: ExportFormat;
  readonly label: string;
  readonly icon: string;
  readonly hint: string;
}

const OPTIONS: readonly FormatOption[] = [
  { format: 'excel', label: 'Excel', icon: '📊', hint: '.xls spreadsheet' },
  { format: 'csv', label: 'CSV', icon: '📄', hint: 'comma-separated' },
  { format: 'json', label: 'JSON', icon: '{ }', hint: 'raw structured data' },
  { format: 'pdf', label: 'PDF', icon: '📕', hint: 'print-ready report' },
];

export function ExportMenu({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointer = (event: MouseEvent): void => {
      if (rootRef.current !== null && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const choose = (format: ExportFormat): void => {
    exportCustomerData(view, format);
    setOpen(false);
  };

  return (
    <div className="export-menu" ref={rootRef}>
      <button
        type="button"
        className="button export-menu__trigger focus-ring"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((prev) => !prev);
        }}
      >
        <span aria-hidden="true">⬇️</span> Export data
      </button>

      {open && (
        <div className="export-menu__panel" role="menu" aria-label="Export customer data">
          <p className="export-menu__heading">Download all customer data</p>
          {OPTIONS.map((option) => (
            <button
              key={option.format}
              type="button"
              role="menuitem"
              className="export-menu__item focus-ring"
              onClick={() => {
                choose(option.format);
              }}
            >
              <span className="export-menu__item-icon" aria-hidden="true">
                {option.icon}
              </span>
              <span className="export-menu__item-text">
                <span className="export-menu__item-label">{option.label}</span>
                <span className="export-menu__item-hint">{option.hint}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
