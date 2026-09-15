import { useId, useState } from 'react';

import type { CustomerSearchHit } from '../../api/types';
import { readRecentlyViewed, recordRecentlyViewed } from './recentlyViewed';
import { belowSearchMinimum, SEARCH_MIN_CHARS, useCustomerSearch } from './useCustomerSearch';

/**
 * Customer search (task 12.3, requirements 3.1, 3.2, 3.4–3.6).
 *
 * A typeahead that begins searching at three characters, shows each hit's name, id, segment and the
 * city the match came from, offers an empty state with refinement guidance when a query returns
 * nothing, and keeps a recently-viewed list of the last ten customers opened. Selecting a hit
 * records it and hands the id up to the caller (the router navigates to that customer's dashboard).
 *
 * The value tier is intentionally not shown here: the search index returns a deliberately thin hit
 * (id, name, segment, city) so a search on, say, a card's last four digits does not re-display a
 * maskable value. The tier lives on the profile, which the field-masking serializer covers; it is
 * surfaced once a customer is opened, not in the picker.
 */

export function CustomerSearch({
  onSelect,
}: {
  readonly onSelect: (customerId: string) => void;
}): React.JSX.Element {
  const [query, setQuery] = useState('');
  const [recent, setRecent] = useState<readonly CustomerSearchHit[]>(() => readRecentlyViewed());
  const inputId = useId();
  const listId = useId();

  const state = useCustomerSearch(query);
  const belowMinimum = belowSearchMinimum(query);

  const select = (hit: CustomerSearchHit): void => {
    setRecent(recordRecentlyViewed(hit));
    onSelect(hit.customer_id);
  };

  return (
    <div className="search">
      <label htmlFor={inputId} className="search__label">
        Find a customer
      </label>
      <input
        id={inputId}
        type="search"
        className="search__input"
        placeholder="Name, ID, email, phone, account or card last 4…"
        autoComplete="off"
        role="combobox"
        aria-expanded={state.kind === 'results'}
        aria-controls={listId}
        aria-describedby={`${inputId}-hint`}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
        }}
      />

      <p id={`${inputId}-hint`} className="search__hint">
        {belowMinimum
          ? `Type at least ${SEARCH_MIN_CHARS} characters to search.`
          : 'Search matches name, customer ID and identifiers.'}
      </p>

      <div aria-live="polite">
        {state.kind === 'loading' && (
          <p role="status" className="search__status">
            Searching…
          </p>
        )}

        {state.kind === 'error' && (
          <p role="alert" className="search__error">
            {state.message}
          </p>
        )}

        {state.kind === 'results' && state.hits.length === 0 && (
          <div className="search__empty">
            <p>No customers match “{query.trim()}”.</p>
            <p className="search__hint">
              Try a fuller name, the exact customer ID, or a complete account or card number.
            </p>
          </div>
        )}

        {state.kind === 'results' && state.hits.length > 0 && (
          <ul id={listId} className="search__results" role="listbox" aria-label="Search results">
            {state.hits.map((hit) => (
              <ResultRow key={hit.customer_id} hit={hit} onSelect={select} />
            ))}
          </ul>
        )}
      </div>

      {recent.length > 0 && state.kind === 'idle' && (
        <section className="recent" aria-labelledby={`${inputId}-recent`}>
          <h3 id={`${inputId}-recent`} className="recent__title">
            Recently viewed
          </h3>
          <ul className="search__results">
            {recent.map((hit) => (
              <ResultRow key={hit.customer_id} hit={hit} onSelect={select} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function ResultRow({
  hit,
  onSelect,
}: {
  readonly hit: CustomerSearchHit;
  readonly onSelect: (hit: CustomerSearchHit) => void;
}): React.JSX.Element {
  return (
    <li className="result" role="option" aria-selected={false}>
      <button
        type="button"
        className="result__button"
        onClick={() => {
          onSelect(hit);
        }}
      >
        <span className="result__name">{hit.customer_name}</span>
        <span className="result__meta">
          <span className="mono">{hit.customer_id}</span>
          <span className="badge badge--segment">{hit.customer_segment}</span>
          {hit.city != null && hit.city !== '' && <span className="result__city">{hit.city}</span>}
        </span>
      </button>
    </li>
  );
}
