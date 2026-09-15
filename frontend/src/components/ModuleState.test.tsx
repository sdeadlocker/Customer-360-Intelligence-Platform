import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { deriveModuleStatus, ModuleState } from './ModuleState';

describe('deriveModuleStatus', () => {
  it('is error when the module failed, regardless of masking', () => {
    expect(deriveModuleStatus({ failed: true, maskedFields: ['profile.dob'] })).toBe('error');
  });

  it('is partial when the module reported a non-fatal error', () => {
    expect(
      deriveModuleStatus({
        errors: [{ module: 'risk', code: 'AGENT_TIMEOUT', message: 'slow' }],
      }),
    ).toBe('partial');
  });

  it('is restricted when fields are masked but nothing failed', () => {
    expect(deriveModuleStatus({ maskedFields: ['contact.email'] })).toBe('restricted');
  });

  it('is ready when nothing failed and nothing is masked', () => {
    expect(deriveModuleStatus({})).toBe('ready');
  });
});

describe('ModuleState rendering', () => {
  it('shows a spinner label while loading', () => {
    render(
      <ModuleState title="Profile" state={{ status: 'loading' }}>
        <p>content</p>
      </ModuleState>,
    );
    expect(screen.getByRole('status')).toHaveTextContent(/loading profile/i);
  });

  it('renders children and a restricted badge when restricted', () => {
    render(
      <ModuleState
        title="Contact"
        state={{ status: 'restricted', maskedFields: ['contact.email'] }}
      >
        <p>visible fields</p>
      </ModuleState>,
    );
    expect(screen.getByText('visible fields')).toBeInTheDocument();
    expect(screen.getByText('Restricted')).toBeInTheDocument();
  });

  it('renders an alert with a reference id on error', () => {
    render(
      <ModuleState
        title="Risk"
        state={{ status: 'error', errorMessage: 'boom', correlationId: 'req-9' }}
      >
        <p>should not show</p>
      </ModuleState>,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('boom');
    expect(alert).toHaveTextContent('req-9');
    expect(screen.queryByText('should not show')).not.toBeInTheDocument();
  });

  it('shows a distinct empty label for present-but-empty (no data)', () => {
    render(
      <ModuleState title="Offers" state={{ status: 'ready' }} isEmpty emptyLabel="No offers.">
        <p>should not show</p>
      </ModuleState>,
    );
    expect(screen.getByText('No offers.')).toBeInTheDocument();
    expect(screen.queryByText('should not show')).not.toBeInTheDocument();
  });

  it('shows partial notices alongside children', () => {
    render(
      <ModuleState
        title="Journey"
        state={{
          status: 'partial',
          errors: [{ module: 'timeline', code: 'UPSTREAM_UNAVAILABLE', message: 'partial data' }],
        }}
      >
        <p>partial content</p>
      </ModuleState>,
    );
    expect(screen.getByText('partial content')).toBeInTheDocument();
    expect(screen.getByText('partial data')).toBeInTheDocument();
    expect(screen.getByText('Partial')).toBeInTheDocument();
  });
});
