/**
 * Client-side export of the loaded customer 360 data (JSON / CSV / Excel / PDF).
 *
 * This exports exactly the data the dashboard already fetched and is entitled to see — the composed
 * `Dashboard360` payload — so it never reaches for anything the role cannot access: masked fields
 * are already masked in `raw`, and failed modules are simply absent. No new network calls, no server
 * changes, no external libraries. Each generator turns the in-memory payload into a Blob (or, for
 * PDF, a print-ready document) and triggers a browser download.
 */

import type { Dashboard360 } from './dashboardData';
import { withAllSections } from './exportCapture';

export type ExportFormat = 'json' | 'csv' | 'excel' | 'pdf';

/** A flattened key/value pair, e.g. `profile.customer_segment` → `HNW`. */
interface FlatRow {
  readonly path: string;
  readonly value: string;
}

/** Trigger a browser download of a Blob under the given filename. */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Release the object URL on the next tick so the download has started.
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

/** A safe, timestamped file stem like `customer-C-00077-2026-09-14`. */
function fileStem(customerId: string): string {
  const safeId = customerId.replace(/[^a-z0-9_-]+/gi, '_');
  const date = new Date().toISOString().slice(0, 10);
  return `customer-${safeId}-${date}`;
}

/** Recursively flatten the payload into dotted-path rows for tabular formats. */
function flatten(value: unknown, prefix = ''): FlatRow[] {
  if (value === null || value === undefined) {
    return [{ path: prefix, value: '' }];
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return [{ path: prefix, value: '[]' }];
    }
    return value.flatMap((item, i) => flatten(item, `${prefix}[${String(i)}]`));
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return [{ path: prefix, value: '{}' }];
    }
    return entries.flatMap(([key, v]) => flatten(v, prefix === '' ? key : `${prefix}.${key}`));
  }
  // Only primitives reach here (null/array/object handled above); stringify each explicitly.
  if (typeof value === 'string') {
    return [{ path: prefix, value }];
  }
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return [{ path: prefix, value: value.toString() }];
  }
  return [{ path: prefix, value: '' }];
}

/** Escape a value for CSV (RFC 4180: wrap in quotes, double interior quotes). */
function csvCell(text: string): string {
  if (/[",\n\r]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

/** Escape text for safe inclusion in HTML/XML (Excel SpreadsheetML + PDF document). */
function escapeXml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function buildJson(view: Dashboard360): Blob {
  const doc = {
    customer_id: view.customerId,
    exported_at: new Date().toISOString(),
    masked_fields: view.maskedFields,
    module_errors: view.errors,
    data: view.raw,
  };
  return new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
}

function buildCsv(view: Dashboard360): Blob {
  const rows = flatten(view.raw);
  const lines = ['Field,Value', ...rows.map((r) => `${csvCell(r.path)},${csvCell(r.value)}`)];
  // Prepend a BOM so Excel opens UTF-8 CSVs with the correct encoding.
  return new Blob(['\uFEFF' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
}

/**
 * Excel via SpreadsheetML 2003 (`.xls`). This is plain XML that Excel, LibreOffice and Google
 * Sheets open natively — a dependency-free way to produce a real spreadsheet with typed cells.
 */
function buildExcel(view: Dashboard360): Blob {
  const rows = flatten(view.raw);
  const bodyRows = rows
    .map(
      (r) =>
        `<Row><Cell><Data ss:Type="String">${escapeXml(r.path)}</Data></Cell>` +
        `<Cell><Data ss:Type="String">${escapeXml(r.value)}</Data></Cell></Row>`,
    )
    .join('');
  const xml =
    `<?xml version="1.0"?>` +
    `<?mso-application progid="Excel.Sheet"?>` +
    `<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" ` +
    `xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">` +
    `<Worksheet ss:Name="Customer ${escapeXml(view.customerId)}"><Table>` +
    `<Row><Cell><Data ss:Type="String">Field</Data></Cell>` +
    `<Cell><Data ss:Type="String">Value</Data></Cell></Row>` +
    bodyRows +
    `</Table></Worksheet></Workbook>`;
  return new Blob([xml], { type: 'application/vnd.ms-excel' });
}

/**
 * Collect the running app's styles so the cloned dashboard markup renders identically in the print
 * iframe (an iframe does not inherit the parent page's stylesheets). We copy every `<style>` block
 * inline and re-link every stylesheet `<link>` by absolute href (Vite injects one bundled CSS link
 * in prod and inline styles in dev, so this covers both).
 */
function collectDocumentStyles(): string {
  const parts: string[] = [];
  for (const node of Array.from(document.querySelectorAll('style, link[rel="stylesheet"]'))) {
    if (node instanceof HTMLStyleElement) {
      parts.push(`<style>${node.textContent ?? ''}</style>`);
    } else if (node instanceof HTMLLinkElement && node.href !== '') {
      parts.push(`<link rel="stylesheet" href="${escapeXml(node.href)}" />`);
    }
  }
  return parts.join('');
}

/**
 * Capture the live dashboard grid (cards + charts) as HTML so the PDF mirrors the on-screen view,
 * charts included. The dashboard renders its content into `#dashboard-main`; the charts there are
 * inline SVG (donut, relationship graph) and styled DOM (expense bars, monthly trend, risk gauge,
 * offer bars), so cloning the node — together with the app CSS from {@link collectDocumentStyles} —
 * reproduces them faithfully. Returns null when the grid is not on the page.
 *
 * Note: whatever is currently mounted is captured. In "Spotlight" (one-section) mode only the
 * visible section is present; switch to "360° Cockpit" before exporting to include every chart.
 */
function captureDashboardVisual(): string | null {
  const grid = document.getElementById('dashboard-main');
  if (grid === null) {
    return null;
  }
  const clone = grid.cloneNode(true) as HTMLElement;
  // Strip interactive-only attributes that add nothing to a static print.
  for (const el of Array.from(clone.querySelectorAll('[aria-busy]'))) {
    el.removeAttribute('aria-busy');
  }
  return clone.outerHTML;
}

/** Build the print-ready HTML document string for the PDF report. */
function buildPrintHtml(view: Dashboard360, visual: string | null): string {
  const rows = flatten(view.raw);
  const tableRows = rows
    .map((r) => `<tr><th scope="row">${escapeXml(r.path)}</th><td>${escapeXml(r.value)}</td></tr>`)
    .join('');
  const generated = new Date().toLocaleString();
  const maskedNote =
    view.maskedFields.length > 0
      ? ` · ${String(view.maskedFields.length)} field(s) masked for your role`
      : '';

  const appStyles = collectDocumentStyles();

  // The captured dashboard grid (with charts) as the visual section, then the flat data appendix.
  const visualSection =
    visual !== null
      ? `<h2 class="pdf-section">Dashboard overview</h2>` +
        `<div class="pdf-visual dashboard">${visual}</div>`
      : '';

  return (
    `<!doctype html><html lang="en"><head><meta charset="utf-8" />` +
    `<title>Customer ${escapeXml(view.customerId)} — Customer 360 export</title>` +
    appStyles +
    `<style>` +
    `*{box-sizing:border-box}` +
    `body{font-family:'Inter',system-ui,Arial,sans-serif;color:#161c2d;margin:1.25rem;background:#fff;}` +
    `h1{font-size:1.5rem;margin:0 0 .25rem;letter-spacing:-.02em}` +
    `.brand{display:inline-flex;align-items:center;gap:.5rem;font-weight:700;color:#4f46e5}` +
    `.brand .mark{width:1.6rem;height:1.6rem;border-radius:8px;background:linear-gradient(135deg,#4f46e5,#9333ea);color:#fff;display:inline-flex;align-items:center;justify-content:center}` +
    `.meta{color:#586074;font-size:.85rem;margin:0 0 1.25rem}` +
    `.pdf-section{font-size:1.05rem;margin:1.5rem 0 .75rem;padding-bottom:.35rem;border-bottom:2px solid #e5e7eb;}` +
    // Neutralise the app's fixed/decorative bits and force a light, print-friendly layout for the
    // captured grid: no blur backdrops, no hover transforms, cards laid out in a simple column.
    `.pdf-visual{background:transparent !important;}` +
    `.pdf-visual .dashboard__grid,.pdf-visual.dashboard{display:block !important;max-width:none !important;padding:0 !important;}` +
    `.pdf-visual .dash-cell{display:block !important;width:100% !important;grid-column:auto !important;margin:0 0 1rem !important;break-inside:avoid;}` +
    `.pdf-visual .module{box-shadow:none !important;transform:none !important;break-inside:avoid;}` +
    `.pdf-visual .ai-experience{display:block !important;}` +
    `.pdf-visual .ai-experience>*{margin-bottom:1rem;}` +
    // Data appendix table.
    `.pdf-appendix{width:100%;border-collapse:collapse;font-size:.78rem;margin-top:.5rem}` +
    `.pdf-appendix th,.pdf-appendix td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #e5e7eb;vertical-align:top;word-break:break-word}` +
    `.pdf-appendix thead th{background:#f1f2f8;text-transform:uppercase;letter-spacing:.04em;font-size:.7rem;color:#586074}` +
    `.pdf-appendix tbody th{font-weight:600;color:#4338ca;width:40%}` +
    `@media print{body{margin:.6rem}.pdf-section{break-before:auto}}` +
    `</style></head><body>` +
    `<div class="brand"><span class="mark">◐</span> Customer 360 Intelligence Platform</div>` +
    `<h1>Customer ${escapeXml(view.customerId)} — full export</h1>` +
    `<p class="meta">Generated ${escapeXml(generated)} · scoped to your entitlements${maskedNote}</p>` +
    visualSection +
    `<h2 class="pdf-section">Full data</h2>` +
    `<table class="pdf-appendix"><thead><tr><th scope="col">Field</th><th scope="col">Value</th></tr></thead>` +
    `<tbody>${tableRows}</tbody></table>` +
    `</body></html>`
  );
}

/**
 * PDF via a hidden same-page iframe + the browser's print pipeline (user picks "Save as PDF").
 *
 * This deliberately avoids `window.open`: a popup trips the browser's pop-up blocker and, opened
 * with noopener, cannot be scripted (leaving a blank tab). Instead we mount an offscreen iframe,
 * write the report into it, print just that iframe, then clean it up — no popup, no blank page, no
 * blocker prompt.
 */
async function exportPdf(view: Dashboard360): Promise<void> {
  // Capture the dashboard grid with EVERY section mounted (the dashboard's capture provider mounts
  // all cards for the duration, so Spotlight mode does not truncate the report to one section).
  const visual = await withAllSections(() => captureDashboardVisual());
  const html = buildPrintHtml(view, visual);

  const iframe = document.createElement('iframe');
  iframe.setAttribute('aria-hidden', 'true');
  iframe.style.position = 'fixed';
  iframe.style.right = '0';
  iframe.style.bottom = '0';
  iframe.style.width = '0';
  iframe.style.height = '0';
  iframe.style.border = '0';
  iframe.style.visibility = 'hidden';
  document.body.appendChild(iframe);

  const cleanup = (): void => {
    // Remove the iframe after the print dialog has had time to capture it.
    setTimeout(() => {
      if (iframe.parentNode !== null) {
        iframe.parentNode.removeChild(iframe);
      }
    }, 3000);
  };

  const doc = iframe.contentWindow?.document;
  if (doc === undefined) {
    cleanup();
    return;
  }

  doc.open();
  doc.write(html);
  doc.close();

  // Print exactly once, whichever of onload / the fallback timer fires first.
  let printed = false;
  const runPrint = (): void => {
    if (printed) {
      return;
    }
    printed = true;
    const frameWin = iframe.contentWindow;
    if (frameWin === null) {
      cleanup();
      return;
    }
    frameWin.focus();
    frameWin.print();
    cleanup();
  };

  iframe.onload = () => {
    // Give the re-linked app stylesheet (and web fonts) time to load and lay out the cloned charts
    // before opening the print dialog, so the first paint is fully styled.
    setTimeout(runPrint, 450);
  };
  // Fallback in case onload does not fire for the written document.
  setTimeout(runPrint, 1200);
}

/**
 * Export the customer's loaded 360 data in the requested format. File formats trigger a download;
 * PDF opens the browser's print dialog (via a hidden iframe) so the user can "Save as PDF".
 */
export function exportCustomerData(view: Dashboard360, format: ExportFormat): void {
  const stem = fileStem(view.customerId);
  switch (format) {
    case 'json':
      downloadBlob(buildJson(view), `${stem}.json`);
      return;
    case 'csv':
      downloadBlob(buildCsv(view), `${stem}.csv`);
      return;
    case 'excel':
      downloadBlob(buildExcel(view), `${stem}.xls`);
      return;
    case 'pdf':
      void exportPdf(view);
      return;
  }
}
