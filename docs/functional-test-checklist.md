# Functional test checklist — pre-deployment gate

Run this before any AWS deploy (Phase 20.8/20.9 are blocked on a clean pass). It walks **every flow,
button and feature**, with dedicated coverage of the AI features. Each item maps to one of the
**eleven success criteria** (SC1–SC11) or a supporting capability (RAG, observability, security).

Mark each row **PASS / FAIL / N/A** and note anything odd. Nothing here deploys — it exercises the
running app locally.

---

## 0. Setup

Two runs give the fullest picture:

- **Run A — mock LLM (offline, deterministic).** Exercises every UI path with no AWS. This is the
  default and validates all non-generative behaviour plus the AI *plumbing* (streaming, citations,
  tool routing) against deterministic stand-ins.
- **Run B — real Bedrock (agents + RAG).** Same walkthrough with `LLM_PROVIDER=bedrock` to confirm
  real answers stream, RAG retrieves real passages, and refusals hold. Needs AWS credentials in the
  default chain and model access enabled for the configured models.

```bash
# Run A (mock):
docker compose -f docker/docker-compose.app.yml up --build
#   web http://localhost:8080   api http://localhost:8000

# Run B (real): set these in a .env beside the compose file, then `up` again.
#   LLM_PROVIDER=bedrock
#   AWS_REGION=us-east-1
#   BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
#   BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
#   (credentials via mounted ~/.aws or the env trio; embeddings ideally baked with EMBED_PROVIDER=bedrock)
```

> **RAG note for Run B:** if the image was built with `EMBED_PROVIDER=mock`, retrieval *works* but
> relevance is weak (pseudo-embeddings). To truly test RAG quality, rebuild the api image with
> `EMBED_PROVIDER=bedrock` (see `deploy/scripts/build-and-push` or the Dockerfile note).

Personas to keep handy (seeded cohorts): **HNW household**, **delinquent borrower**, **thin-file**,
**fraud-flagged**. Note the actual customer IDs/names from the search screen for reuse below.

---

## 1. Auth, roles and shell — SC gate: "six role logins work"

| # | Step | Expected | Result |
|---|---|---|---|
| 1.1 | Open `/` while signed out | Redirected to `/login` (auth guard) | |
| 1.2 | `/login` renders | Six one-click role cards (RM, Wealth Advisor, Contact Center, Branch, Risk, Marketing) + manual username/password form + hero panel | |
| 1.3 | Click each of the six role cards in turn | Each signs in and lands on `/` with that role's label + scope in the header | |
| 1.4 | Manual login form with a seeded username | Signs in successfully | |
| 1.5 | Manual login with a bad credential | Rejected with a clear error, no crash | |
| 1.6 | Theme toggle (login, search header, dashboard header) | Flips light/dark; choice persists across navigation | |
| 1.7 | Sign out | Returns to `/login`; protected routes redirect again | |
| 1.8 | Hit an API route with no token (e.g. `curl localhost:8000/customers/x/360`) | `401` | |

---

## 2. Search landing — SC1 (search) + primary AI entry point

| # | Step | Expected | Result |
|---|---|---|---|
| 2.1 | Search by name / partial | Ranked matches within scope; no out-of-scope customers | |
| 2.2 | Search with no matches | Empty state with refinement guidance, no crash | |
| 2.3 | Recently-viewed list | Populates as you open profiles (up to 10) | |
| 2.4 | Select a result | Navigates to `/customers/:id` | |
| 2.5 | Marketing role search | Pseudonymized / aggregate view; no raw PII | |

---

## 3. Customer 360 dashboard — SC2, SC3, SC4

| # | Step | Expected | Result |
|---|---|---|---|
| 3.1 | Open HNW household customer | Full 360 profile renders; left-nav rail present | |
| 3.2 | Products & holdings (SC3) | Balances, rates, maturities, EMIs shown and internally consistent | |
| 3.3 | Financial health indicators (SC4) | Net worth, utilization, aggregates — computed values, not narrated guesses | |
| 3.4 | Every left-nav section | Each loads; the shared five-state wrapper shows loading/empty/error correctly | |
| 3.5 | Cockpit vs Spotlight views | Both render and switch | |
| 3.6 | Widget deep-links / drilldowns | Expand to evidence; no dead buttons | |

---

## 4. Role-based masking — security gate (every role × the same customer)

Open the **same** customer as each role and confirm field legibility matches the matrix.

| # | Role | Expected on financial detail | Result |
|---|---|---|---|
| 4.1 | RM | Full access to assigned book | |
| 4.2 | Wealth Advisor | Full (investments, net worth, household) | |
| 4.3 | Contact Center | Banded balances (not exact) | |
| 4.4 | Branch | Masked financial detail | |
| 4.5 | Risk | Full risk/fraud/delinquency; marketing restricted | |
| 4.6 | Marketing | Pseudonymized; aggregate only; no raw PII | |
| 4.7 | Any masked field | `meta.masked_fields` reflects what was hidden; nothing leaks in the raw API response | |

---

## 5. Risk, relationships, timeline — SC5, SC6, SC7

| # | Step | Expected | Result |
|---|---|---|---|
| 5.1 | Risk profile (SC5) on fraud-flagged customer | Alerts, exposure, delinquency surface; heuristic scores labelled as such (not bureau) | |
| 5.2 | Relationship network / household (SC6) | Graph renders; household derived from address + relationships | |
| 5.3 | Restricted graph node | Renders structure-only (no leaked detail) | |
| 5.4 | Life-event timeline (SC7) | Events with corroborating transactions; ordering correct | |

---

## 6. AI: customer summary — SC8

| # | Step | Expected | Result |
|---|---|---|---|
| 6.1 | Open AI summary on a rich customer | Summary streams in; renders progressively | |
| 6.2 | Figures in the summary | Every number is copied from data/tool facts, cited — never invented | |
| 6.3 | AI labelling | Clearly marked AI-generated; confidence shown where applicable | |
| 6.4 | Run B (real Bedrock) | A genuine model summary streams end to end | |
| 6.5 | Provider fault (Run B, force an error) | Degrades gracefully to a fallback, never a 500/crash | |

---

## 7. AI: offers & next best actions — SC9, SC10

| # | Step | Expected | Result |
|---|---|---|---|
| 7.1 | Personalized offers (SC9) | Offers with propensity/expected value; suppressed offers shown greyed with reason | |
| 7.2 | Next best actions (SC10) | Ranked recommendations tied to evidence | |
| 7.3 | Offer action buttons | Each does what it says; no dead controls | |
| 7.4 | Cooling-off suppression | A recently-offered item is correctly suppressed | |

---

## 8. AI: natural-language Q&A — SC11 (the headline AI feature)

Cross-customer **Ask-Anything** panel on the landing (`POST /ask`) and per-customer ask
(`POST /customers/{id}/ask`). Test both, in Run A and Run B.

| # | Step | Expected | Result |
|---|---|---|---|
| 8.1 | Ask a factual question ("What's her mortgage balance?") | Exact figure, cited to a record, masked by role; answer streams | |
| 8.2 | Ask a relationship question ("Who else is on that mortgage?") | Deterministic graph traversal with a path | |
| 8.3 | Ask an eligibility question ("Is she eligible for the HELOC promo?") | Facts from data **+** rules from retrieved policy | |
| 8.4 | Ask a pure policy question ("What's our procedure when AML flags fire?") | Retrieval-only answer from the corpus, cited to document/section/version | |
| 8.5 | Ask a product-terms question ("Terms on that cash-back card offer?") | Answer grounded in the offer_terms doc | |
| 8.6 | Cross-customer ask from the landing (no profile open) | Agent resolves the customer, then answers | |
| 8.7 | Ask something unanswerable / out of scope | Correct **refusal**, no fabrication | |
| 8.8 | Cross-customer isolation | An answer never leaks another customer's data | |
| 8.9 | Citations are clickable | Each citation opens the cited passage/record (PassageViewer / citable field) | |
| 8.10 | Numeric guardrail | A retrieved passage never supplies a customer *figure* (figures are tool-called only) | |
| 8.11 | Run B streaming | Real Bedrock tokens stream token-by-token; no buffering stalls on SSE | |

---

## 9. RAG retrieval quality — supporting capability (§17)

| # | Step | Expected | Result |
|---|---|---|---|
| 9.1 | Policy/eligibility question | Correctly ranked passages returned with document, section, version, effective date | |
| 9.2 | Superseded document | Old version excluded by effective-date filtering | |
| 9.3 | Marketing asks a `RISK_ONLY`/`COMPLIANCE_ONLY` question | No restricted content returned; result counts don't reveal existence | |
| 9.4 | No PII in the query path | Customer figures never enter retrieval | |

---

## 10. Signals worklist (Phase 17) — push side

| # | Step | Expected | Result |
|---|---|---|---|
| 10.1 | Worklist on the landing | Ranked, entitlement-scoped signals | |
| 10.2 | Delinquent / HNW-large-deposit / fraud cohorts | Each surfaces its expected signal | |
| 10.3 | Drill into a signal | Opens its evidence | |
| 10.4 | Dismiss / acknowledge | Persists across reload; **no** customer-DB write | |

---

## 11. Reports / prepare-for-meeting (Phase 18)

| # | Step | Expected | Result |
|---|---|---|---|
| 11.1 | Generate a branded report / briefing | Produces the artifact for the selected customer | |
| 11.2 | Export menu formats | Each format works | |
| 11.3 | Content respects role masking | No field the role can't see appears in the export | |

---

## 12. Observability & privacy — supporting capability (§18)

| # | Step | Expected | Result |
|---|---|---|---|
| 12.1 | With telemetry on, perform an ask | A trace is produced for the request | |
| 12.2 | Inspect the trace/span attributes | **No** PII, monetary values, prompts or completions (`OTEL_CAPTURE_PROMPT_CONTENT=false`) | |
| 12.3 | Audit log | Access is recorded (actor, role, action) and is immutable | |

---

## 13. Accessibility & UX polish

| # | Step | Expected | Result |
|---|---|---|---|
| 13.1 | Keyboard-only navigation | All controls reachable and operable; visible focus | |
| 13.2 | The rebuilt footer | Renders full multi-column layout on dashboard + search pages; links/social operable; responsive at narrow widths | |
| 13.3 | Both themes | Light and dark both legible (contrast) | |
| 13.4 | Error boundaries | A forced component error shows a boundary, not a white screen | |

---

## Sign-off

- [ ] All SC1–SC11 rows PASS (Run A)
- [ ] AI features (sections 6–9) PASS in Run B (real Bedrock)
- [ ] Security/masking (section 4) and privacy (section 12) PASS
- [ ] No dead buttons, no unhandled errors, no console errors during the walkthrough

Only after this sign-off do Phase 20.8 (deploy) and 20.9 (post-deploy verification) unblock.
