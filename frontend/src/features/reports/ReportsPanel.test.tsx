import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { RunView } from './reportsApi';

// The panel drives the report API wrappers; mock them so the component test is deterministic and
// backend-free (mirrors SignalsWorklist.test.tsx).
const { defineReportMock, runReportMock, listRunsMock, scheduleReportMock, downloadMock } =
  vi.hoisted(() => ({
    defineReportMock: vi.fn(),
    runReportMock: vi.fn(),
    listRunsMock: vi.fn(),
    scheduleReportMock: vi.fn(),
    downloadMock: vi.fn(),
  }));

vi.mock('./reportsApi', () => ({
  defineReport: defineReportMock,
  runReport: runReportMock,
  listRuns: listRunsMock,
  scheduleReport: scheduleReportMock,
  disableSchedule: vi.fn(),
  downloadArtifactUrl: downloadMock,
}));

import { ReportsPanel } from './ReportsPanel';

function run(overrides: Partial<RunView> = {}): RunView {
  return {
    run_id: 5,
    definition_id: 3,
    report_type: 'PDF_PACK',
    status: 'SUCCEEDED',
    trigger_kind: 'MANUAL',
    customers_rendered: 1,
    model_ids: ['mock-llm-v1'],
    prompt_versions: ['v1'],
    degraded: false,
    started_at: '2026-09-01T00:00:00Z',
    finished_at: '2026-09-01T00:00:05Z',
    error: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('ReportsPanel', () => {
  it('defines, runs and lists a report, announcing the outcome', async () => {
    defineReportMock.mockResolvedValue(3);
    runReportMock.mockResolvedValue(run());
    listRunsMock.mockResolvedValue([run()]);

    render(<ReportsPanel customerId="C-00001" />);
    await userEvent.click(screen.getByRole('button', { name: /generate report/i }));

    await waitFor(() => {
      expect(defineReportMock).toHaveBeenCalledTimes(1);
    });
    // The definition is scoped to the customer in view.
    expect(defineReportMock.mock.calls[0]![0]).toMatchObject({
      report_type: 'PDF_PACK',
      scope: { kind: 'CUSTOMER', customer_id: 'C-00001' },
    });
    expect(runReportMock).toHaveBeenCalledWith(3);
    // The run history table renders the produced run with a download control.
    expect(await screen.findByRole('button', { name: /download pdf/i })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/report generated/i);
  });

  it('shows the branding preview live as the fields change', async () => {
    render(<ReportsPanel customerId="C-1" />);
    const preview = screen.getByLabelText('Branding preview');
    const brand = screen.getByLabelText('Brand name');
    await userEvent.clear(brand);
    await userEvent.type(brand, 'Acme Trust');
    expect(preview).toHaveTextContent('Acme Trust');
  });

  it('requires a definition before scheduling', () => {
    render(<ReportsPanel customerId="C-1" />);
    // The Schedule button is disabled until a report has been defined and run.
    expect(screen.getByRole('button', { name: 'Schedule' })).toBeDisabled();
  });

  it('schedules after a report is defined', async () => {
    defineReportMock.mockResolvedValue(3);
    runReportMock.mockResolvedValue(run());
    listRunsMock.mockResolvedValue([run()]);
    scheduleReportMock.mockResolvedValue(11);

    render(<ReportsPanel customerId="C-1" />);
    await userEvent.click(screen.getByRole('button', { name: /generate report/i }));
    await screen.findByRole('button', { name: /download pdf/i });

    await userEvent.click(screen.getByRole('button', { name: 'Schedule' }));
    await waitFor(() => {
      expect(scheduleReportMock).toHaveBeenCalledWith(3, 'WEEKLY');
    });
    expect(screen.getByRole('status')).toHaveTextContent(/scheduled weekly/i);
  });
});
