# Accessibility

This is the Phase 15 accessibility record for the Customer 360 web app: what the platform does to be
usable without a mouse or without sight, how that is enforced automatically, and what a manual
screen-reader pass found. It targets WCAG 2.1 AA. Full conformance still requires expert review with
assistive technology on real hardware; this document is the engineering baseline, not a certificate.

_Requirements: 16.1 (contrast, focus, no colour-only), 16.2 (chart table equivalents), 16.3
(keyboard operability, landmarks), 16.4 (live-region announcements)._

## How accessibility is enforced

Two gates, both wired into `npm run check` (the frontend CI entry point):

- **Contrast** — every colour pairing that carries meaning is a design token in
  `src/theme/tokens.css`, and `src/theme/tokens.contrast.test.ts` asserts each pairing clears the AA
  ratio (4.5:1 body text, 3:1 large text and non-text UI). A colour change that dips below the
  threshold fails the build.
- **axe-core** — `src/test/accessibility.test.tsx` renders each interactive surface and runs
  [axe-core](https://github.com/dequelabs/axe-core) (via `jest-axe`), asserting zero violations. The
  matcher is registered globally in `src/test/setup.ts` and typed in `src/test/jest-axe.d.ts`, so
  any test can call `expect(container).toHaveNoViolations()`.

axe is a machine gate: it catches missing labels, broken ARIA structure, nested interactive
controls, and contrast on rendered DOM. It cannot judge focus order, whether an announcement is
_meaningful_, or whether meaning survives without colour. Those are the manual findings below.

Run just the accessibility suite:

```sh
cd frontend && npx vitest run src/test/accessibility.test.tsx
```

## Keyboard operability

- **Single, always-visible focus ring.** One `--focus-ring-*` token, applied via `:focus-visible` in
  `tokens.css`, is used everywhere a control can take focus. It is never removed.
- **Skip links.** Each page renders a `.skip-link` as its first focusable element, so a keyboard user
  can jump past the header straight to `#dashboard-main` / `#search-main`.
- **The relationship graph is fully keyboard-operable.** The SVG canvas is a pointer-and-sight
  artefact; its accessible peer is a keyboard-navigable tree (`NetworkTree.tsx`) presenting the
  _identical_ nodes and edges. A Diagram / Tree switch toggles between them, and selection is shared,
  so nothing in the graph is reachable only with a mouse. The tree follows the WAI-ARIA tree pattern:
  a single tab stop (roving `tabIndex`), Up/Down to move, Home/End to jump, Enter/Space to select.
- **The journey timeline** is a horizontally scrollable region with a focusable container and
  per-milestone buttons carrying full text labels.

## Landmarks and structure

- Each route is a single `<main>` with an `id` targeted by the skip link.
- The dashboard header (identity, filters) is a `<header>`; the AI insights layer is a labelled
  `<section aria-label="AI insights and recommendations">`, so streamed AI content lives inside a
  landmark rather than orphaned after `</main>`.
- Every widget is a `<section>` with an `aria-labelledby` heading (`ModuleState`), so a screen reader
  can enumerate the modules.
- Filters use `<fieldset>`/`<legend>`; the search box is a labelled `role="combobox"` over a
  `role="listbox"` of results.

## Chart and gauge table equivalents (16.2)

Every visualization has a toggleable, accessible `<table>` presenting the same data, via the shared
`ChartWithTable` wrapper. The chart SVG is marked `aria-hidden` (the table is the accessible
representation) and a "Show data table" control swaps it in:

| Visualization                     | Table equivalent            |
| --------------------------------- | --------------------------- |
| Expense — spend by category       | Category / total / count    |
| Expense — monthly trend           | Month / total / count / deviation |
| Risk gauge                        | Band scale with current band marked |
| Offer propensity bars             | Rank / offer / type / EV / confidence / status |
| Relationship graph                | The keyboard tree (`NetworkTree`) |

The AI narrative cards render prose and lists only — no charts — so they need no table equivalent.

## No colour-only encoding (16.1)

Risk and delinquency, and every other status, always carry a label or shape in addition to colour:

- Risk band shows the band **word**; the gauge segment is also outlined, not only tinted.
- A flagged expense month gets a **badge** with the signed sigma ("High +2.6σ"), plus a dashed
  outline on the bar.
- Inferred graph edges are **dashed** as well as tinted; a restricted node is drawn **hollow**
  (structure-only) and the tree item is dashed and labelled "Restricted customer".
- Module states ("no data" / "restricted" / "partial" / "error") each get a distinct left-border
  **and** a text badge, so they are never distinguished by colour alone.

## Live-region announcements (16.4)

- **Async modules** (`ModuleState`) and **streaming AI cards** (`AiCard`) wrap their body in
  `aria-live="polite"` with `aria-busy` during load, so a screen reader is told when a module
  finishes, fails, or comes back restricted.
- **Search results** announce via a polite live region on the results container.
- **The ask-anything panel** does _not_ make its streaming transcript a live region (that would read
  out every token). Instead a dedicated `sr-only`, `aria-atomic` status region announces the
  meaningful transition once per turn: "Finding an answer…", "Answer ready.", "Request refused.",
  "No guidance found." or the failure message.

## Motion

`prefers-reduced-motion` is honoured globally in `tokens.css`; the streaming spinner and answer
cursor drop their animation under that preference.

## Manual screen-reader pass

Performed with VoiceOver (Safari, macOS) and NVDA (Firefox, Windows) against the seeded dataset,
logged in as an RM and re-checked as a restricted (Contact Center) role.

**Verified working**

- Skip link is the first stop on Tab and moves focus into the customer grid.
- Every module is reachable and announced by its heading; masked modules announce "Restricted".
- The relationship network is fully explorable via the tree view; the diagram's nodes are also
  reachable with Tab for sighted keyboard users. Restricted neighbours announce as structure-only
  with no identity.
- Chart "Show data table" toggles are announced with their expanded state; the tables read cleanly.
- Ask-anything: the panel announces "Finding an answer…" then the outcome once, without reading the
  token stream. Refusals and "no guidance" are announced distinctly.
- The non-dismissible AML/PEP compliance banner is announced as an alert and cannot be tabbed away
  into nonexistence.

**Known limitations / follow-ups**

- The timeline is operable but its zoom is a coarse experience under magnification; a future pass
  should add keyboard pan between milestones rather than relying on the scroll container.
- Cytoscape is named in the design for the graph canvas; the current SVG canvas is dependency-free.
  If Cytoscape is adopted later, its canvas must keep the tree peer as the accessible path — the tree
  is the source of truth for graph accessibility, not the visual.
- axe-core and this manual pass do not substitute for a formal WCAG audit with users of assistive
  technology. Treat AA conformance as engineering-verified, not independently certified.
