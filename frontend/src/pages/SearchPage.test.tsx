import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

/**
 * The landing page's left navigation rail (Phase 22).
 *
 * The page carries four substantial surfaces, so it adopts the dashboard's rail chrome and its
 * scroll-to-section behaviour. It has **no view modes**: the landing page is a daily briefing, so
 * every section is always mounted and the rail is purely a jump-to. These tests pin the three things
 * that matter about that:
 *
 *  - the rail lists every section, grouped, and marks the selected one as current;
 *  - every section stays mounted — nothing is hidden behind a mode toggle;
 *  - the **universal Ask panel is not a section** — it stays pinned outside the rail and the content
 *    column, because requirement 11.1 wants a plain-language question answerable immediately after
 *    login with no navigation at all. A refactor that turned Ask into a nav section would break that,
 *    so it is asserted explicitly.
 */

const { getEnvelopeMock, postEnvelopeMock } = vi.hoisted(() => ({
  getEnvelopeMock: vi.fn(),
  postEnvelopeMock: vi.fn(),
}));

vi.mock('../api/client', () => ({
  getEnvelope: getEnvelopeMock,
  postEnvelope: postEnvelopeMock,
  ApiError: class ApiError extends Error {
    code = 'X';
    correlationId = null;
    status = 500;
  },
}));

const logoutMock = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    status: 'authenticated',
    principal: { role: 'RM', entitlement: { kind: 'BOOK' } },
    login: vi.fn(),
    logout: logoutMock,
  }),
}));

import { SearchPage } from './SearchPage';

function renderLanding(): { readonly container: HTMLElement } {
  const { container } = render(
    <MemoryRouter>
      <SearchPage />
    </MemoryRouter>,
  );
  return { container };
}

/** The section ids currently mounted in the content column, in DOM order. */
function mountedSections(container: HTMLElement): readonly string[] {
  return [...container.querySelectorAll('[data-section]')].map(
    (node) => node.getAttribute('data-section') ?? '',
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('SearchPage — left navigation rail', () => {
  it('lists every section in the rail, grouped', async () => {
    // The revenue endpoints are not deployed; every feed degrades rather than throwing.
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    renderLanding();

    const nav = screen.getByRole('navigation', { name: /workspace sections/i });
    expect(within(nav).getByText('Revenue')).toBeInTheDocument();
    expect(within(nav).getByText('Customers')).toBeInTheDocument();
    for (const label of ['Opportunity pipeline', 'Money in motion', 'Find a customer']) {
      expect(await within(nav).findByRole('button', { name: label })).toBeInTheDocument();
    }
  });

  it('mounts every section, in order, with no view-mode toggle', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    const { container } = renderLanding();
    await screen.findByRole('navigation', { name: /workspace sections/i });

    // Finding a customer leads — the most-used action — then the two revenue sections. The signals
    // worklist was removed from this page.
    expect(mountedSections(container)).toEqual(['search', 'pipeline', 'money-in-motion']);
    // The dashboard's Spotlight / Overview switch is deliberately absent here.
    expect(screen.queryByRole('group', { name: /view mode/i })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Spotlight' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Overview' })).toBeNull();
  });

  it('keeps every section mounted after navigating to one', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    const { container } = renderLanding();
    const nav = screen.getByRole('navigation', { name: /workspace sections/i });

    await userEvent.click(within(nav).getByRole('button', { name: 'Money in motion' }));
    // Navigating scrolls; it never unmounts the other sections.
    expect(mountedSections(container)).toEqual(['search', 'pipeline', 'money-in-motion']);
  });

  it('marks the selected nav entry as current', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    renderLanding();
    const nav = screen.getByRole('navigation', { name: /workspace sections/i });

    const searchLink = within(nav).getByRole('button', { name: 'Find a customer' });
    await userEvent.click(searchLink);
    expect(searchLink).toHaveAttribute('aria-current', 'true');
    expect(within(nav).getByRole('button', { name: 'Money in motion' })).not.toHaveAttribute(
      'aria-current',
    );
  });

  it('pins the universal Ask panel outside the rail, collapsed, and expands it on click', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    const { container } = renderLanding();
    const nav = screen.getByRole('navigation', { name: /workspace sections/i });

    // It starts as the slim collapsible launcher (like the dashboard's), not the open panel: a
    // launcher button, no composer yet.
    const launcher = await screen.findByRole('button', { name: /ask anything across your book/i });
    expect(nav.contains(launcher)).toBe(false);
    expect(launcher.closest('[data-section]')).toBeNull();
    expect(container.querySelector('#landing-ask')).not.toBeNull();
    expect(screen.queryByLabelText('Your question')).toBeNull();

    // Clicking it opens the full panel — the level-2 title and the composer appear.
    await userEvent.click(launcher);
    expect(screen.getByRole('heading', { name: 'Ask anything', level: 2 })).toBeInTheDocument();
    expect(screen.getByLabelText('Your question')).toBeInTheDocument();

    // And it can be collapsed again, back to the launcher.
    await userEvent.click(screen.getByRole('button', { name: /^collapse$/i }));
    expect(
      screen.getByRole('button', { name: /ask anything across your book/i }),
    ).toBeInTheDocument();
  });

  it('offers an Ask AI jump affordance in the rail', () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    renderLanding();
    const nav = screen.getByRole('navigation', { name: /workspace sections/i });
    // Discoverability of Ask must not depend on the panel happening to be scrolled into view.
    expect(within(nav).getByRole('button', { name: 'Ask AI' })).toBeInTheDocument();
  });

  it('exposes one main landmark and a labelled nav, and has no axe violations', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not deployed'));

    const { container } = renderLanding();
    await screen.findByRole('navigation', { name: /workspace sections/i });

    // Restructuring the page around a rail is exactly the change that could produce two `main`
    // landmarks or an unlabelled nav, so the landmark shape is asserted alongside the axe pass.
    expect(screen.getAllByRole('main')).toHaveLength(1);
    for (const toggle of screen.queryAllByRole('button', { name: /show data table/i })) {
      await userEvent.click(toggle);
    }
    expect(await axe(container)).toHaveNoViolations();
  });
});
