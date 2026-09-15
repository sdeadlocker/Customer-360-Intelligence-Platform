import { useEffect } from 'react';

import { formatDate } from '../dashboard/widgets/format';
import { useCitations } from './CitationContext';

/**
 * The knowledge-citation viewer (task 14.2, requirement 17.6).
 *
 * A knowledge citation opens this panel, which shows the passage's provenance — document title,
 * section path, version and effective-date window — exactly as the backend carries it on the
 * citation (design §8.4). The panel is a dialog: it traps nothing but is dismissible by the close
 * button or the Escape key, and it is labelled for assistive technology. It is mounted once at the
 * dashboard root and driven by the shared citation context, so every AI card and the ask panel open
 * the same viewer.
 *
 * The citation itself carries the metadata needed to attribute the passage, which is what the design
 * requires the viewer to show; the panel offers a link to the full document for the reader who wants
 * the source text, rather than duplicating the passage body into the streamed citation.
 */
export function PassageViewer(): React.JSX.Element | null {
  const { openPassage, closePassage } = useCitations();

  useEffect(() => {
    if (openPassage === null) {
      return;
    }
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        closePassage();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [openPassage, closePassage]);

  if (openPassage === null) {
    return null;
  }

  const effective =
    openPassage.effective_to != null && openPassage.effective_to !== ''
      ? `${formatDate(openPassage.effective_from)} – ${formatDate(openPassage.effective_to)}`
      : `From ${formatDate(openPassage.effective_from)}`;

  return (
    <div className="passage-overlay" role="presentation" onClick={closePassage}>
      <div
        className="passage-viewer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="passage-viewer-title"
        onClick={(event) => {
          event.stopPropagation();
        }}
      >
        <div className="passage-viewer__head">
          <h2 id="passage-viewer-title" className="passage-viewer__title">
            {openPassage.title}
          </h2>
          <button
            type="button"
            className="button button--subtle passage-viewer__close"
            onClick={closePassage}
            aria-label="Close passage viewer"
          >
            Close
          </button>
        </div>

        <dl className="passage-viewer__meta kv">
          <dt className="kv__key">Section</dt>
          <dd className="kv__val">{openPassage.section_path}</dd>
          <dt className="kv__key">Document</dt>
          <dd className="kv__val mono">{openPassage.doc_id}</dd>
          <dt className="kv__key">Version</dt>
          <dd className="kv__val">{openPassage.version}</dd>
          <dt className="kv__key">Effective</dt>
          <dd className="kv__val">{effective}</dd>
        </dl>

        <p className="passage-viewer__note">
          This passage is cited from the knowledge base. Open the full document for the complete
          source text.
        </p>
        <a
          className="button button--subtle"
          href={`/knowledge/documents/${encodeURIComponent(openPassage.doc_id)}?version=${encodeURIComponent(
            openPassage.version,
          )}`}
          target="_blank"
          rel="noreferrer"
        >
          Open document
        </a>
      </div>
    </div>
  );
}
