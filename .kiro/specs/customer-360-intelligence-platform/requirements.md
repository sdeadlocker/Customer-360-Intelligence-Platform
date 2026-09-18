# Requirements — Customer 360 Intelligence Platform

**Status:** Phase 1 draft — awaiting review
**Source:** Production Specification Document (provided by stakeholder)
**Next phase:** Design (blocked on open decisions in §12)

---

## 1. Introduction

The Customer 360 Intelligence Platform provides a unified, AI-augmented view of a banking customer by
consolidating profile, contact, financial, credit, risk, behavioral, engagement, relationship, asset and
life-event data into a single experience.

The platform serves six personas:

| Persona | Primary need | Data sensitivity |
|---|---|---|
| Relationship Manager (RM) | Full 360 view, next best actions | High — full access to assigned book |
| Wealth Advisor | Investments, net worth, household | High |
| Contact Center Agent | Identity verification, recent activity, service history | Medium — masked financial detail |
| Branch Employee | Profile, holdings, basic servicing | Medium — masked |
| Risk Analyst | Risk, fraud, delinquency, exposure | High on risk, restricted on marketing |
| Marketing Analyst | Segments, offers, propensity | Low on PII — aggregate/pseudonymized |

**Definitions**

- **Customer 360** — the aggregated read model combining all domains for one customer.
- **Agent** — a bounded AI capability that consumes 360 data and produces a narrative or ranked output.
- **Knowledge Graph (KG)** — node/edge representation of customers, products, assets and events used for
  relationship and multi-hop queries.
- **NBO / NBA** — Next Best Offer / Next Best Action.
- **Propensity** — modeled probability that a customer accepts a given offer.

---

## 2. Scope

### 2.1 In scope

- Customer search, selection and the Customer 360 dashboard with all widget modules in §6 of the source spec.
- Financial, credit, risk, relationship, journey and offer intelligence read models.
- Knowledge graph for relationship and multi-hop traversal.
- Seven AI agents (Summary, Financial Health, Risk, Life Event, Offer, Relationship, Journey).
- Natural-language Q&A over customer data with grounded citations.
- Retrieval layer over institutional knowledge: product catalog, policy, procedure, offer terms, playbooks.
- Observability across traces, metrics and logs, including agent, model and retrieval telemetry.
- Agent evaluation framework with ground-truth datasets and CI quality gates.
- REST APIs per §7 of the source spec.
- OAuth 2.0 / SSO, RBAC, field-level masking, audit logging, encryption.
- Synthetic seed dataset of 100 customers with realistic distributions, plus a synthetic knowledge corpus.

### 2.2 Out of scope (this release)

- Write-back to core banking / CRM systems of record. The platform is **read-only** over source data.
- Real-time streaming ingestion (batch + on-demand refresh only).
- Model training pipelines for propensity/risk scores. Scores are consumed as data or computed by
  deterministic heuristics that are clearly labelled as such.
- Mobile-native applications. Responsive web only.
- Regulatory report generation.

---

## 3. Customer Discovery and Selection

**User story:** As a relationship manager, I want to find a customer quickly by any identifier I have on
hand, so that I can start serving them without knowing their customer ID.

### Acceptance criteria

1. WHEN a user types 3 or more characters into customer search THEN the system SHALL return matching
   customers within 500 ms, matching on customer name, customer ID, email, phone, mobile, account number,
   card last-4 and loan number.
2. WHEN search results are displayed THEN each result SHALL show customer name, customer ID, segment,
   customer value tier and city.
3. WHEN a user has a restricted book of business THEN the system SHALL return only customers the user is
   entitled to view, and SHALL NOT reveal the existence of non-entitled customers.
4. WHEN a user selects a search result THEN the system SHALL load the Customer 360 dashboard for that
   customer.
5. WHEN a search returns no results THEN the system SHALL display an empty state with search-refinement guidance.
6. WHEN a user has viewed customers previously THEN the system SHALL offer a recently-viewed list of the
   last 10 customers.
7. WHEN a user applies a date range, segment or product filter in the dashboard header THEN all
   time-scoped and product-scoped widgets SHALL re-render against that filter, and the active filter set
   SHALL remain visible.

---

## 4. Customer 360 Unified View

**User story:** As any authorized user, I want a single pane of glass for the selected customer, so that I
do not have to open multiple systems.

### Acceptance criteria

1. WHEN a customer is selected THEN the system SHALL render the 360 dashboard containing the profile,
   financial overview, AI summary, expense analytics, risk analysis, offer intelligence, customer journey,
   relationship network and next-best-actions modules.
2. WHEN the dashboard loads THEN the system SHALL display profile identity and financial headline values
   within 3 seconds at p95.
3. WHEN an AI-generated module has not yet completed THEN the system SHALL render a skeleton state for
   that module only, and SHALL NOT block the rest of the dashboard.
4. WHEN any single data domain fails to load THEN the system SHALL render the remaining modules and SHALL
   display a per-module error state with a retry affordance.
5. WHEN the customer profile is rendered THEN the system SHALL display customer ID, name, type, segment,
   customer-since date, date of birth, citizenship, occupation, employer, employment status, marital
   status, customer value, preferred language and preferred channel.
6. WHEN contact information is rendered THEN the system SHALL display email, phone, mobile, address lines
   1–2, city, state, country and postal code, subject to the masking rules in §11.
7. WHEN a field has no value in the source data THEN the system SHALL render an explicit "not available"
   indicator rather than a blank or zero.
8. WHEN any figure is displayed THEN the system SHALL show its as-of timestamp on hover or in the module
   footer.
9. WHEN a user chooses to export the loaded customer's data THEN the system SHALL offer Excel, CSV, JSON and
   PDF, SHALL export only the data already loaded and the role is entitled to see (masked fields stay masked,
   failed modules stay absent), and SHALL do so without issuing new requests for data the role cannot access.
10. WHEN a user exports customer data THEN the system SHALL record the export as an audited action per §12.
11. WHEN a user selects the compact ("at-a-glance") view THEN the dashboard SHALL present every section's key
    information shrunk to fit approximately one screen, without removing information from the full views
    (Spotlight and 360° Cockpit), which SHALL remain complete and unchanged.

---

## 5. Financial Health Intelligence

**User story:** As a wealth advisor, I want the customer's complete financial picture with trends, so that
I can advise on net worth and cash flow.

### Acceptance criteria

1. WHEN the financial overview renders THEN the system SHALL display total deposits, total loans, total
   investments, total liabilities, net worth, household net worth, monthly income, monthly expense and
   FICO score.
2. WHEN net worth is displayed THEN the system SHALL compute it as total assets minus total liabilities and
   SHALL expose the contributing components on drill-down.
3. WHEN product holdings render THEN the system SHALL list all accounts, deposits, loans, credit cards and
   investment accounts with the fields defined in the domain data model.
4. WHEN a deposit account is displayed THEN the system SHALL show account number, product type, balance,
   interest rate and maturity date.
5. WHEN a loan is displayed THEN the system SHALL show loan number, type, original amount, current balance,
   interest rate, monthly EMI, status and start/end dates.
6. WHEN a credit card is displayed THEN the system SHALL show card type, credit limit, current balance,
   utilization rate, rewards balance, monthly spend, overlimit events and fraud alerts.
7. WHEN an investment account is displayed THEN the system SHALL show portfolio value and asset allocation
   across mutual funds, stocks, bonds and retirement accounts, plus the investment risk profile.
8. WHEN expense analytics render THEN the system SHALL aggregate transactions into spending categories and
   SHALL display category distribution and a monthly trend series over the selected date range.
9. WHEN transaction history is available THEN the system SHALL derive a month-over-month spend trend and
   SHALL flag categories whose spend deviates more than a configurable threshold from the customer's
   trailing 6-month average.
10. WHEN credit profile renders THEN the system SHALL display FICO score, behavior score, propensity score,
    credit utilization, years on bureau, number of inquiries, number of trades, credit accounts and credit
    exposure.
11. WHEN a financial health score is presented THEN the system SHALL display the score, its band, and the
    named factors that drove it.

---

## 6. Relationship Intelligence

**User story:** As a relationship manager, I want to see the customer's household and connected entities,
so that I can understand total relationship value and identify referral opportunities.

### Acceptance criteria

1. WHEN the relationship module renders THEN the system SHALL display household members, joint account
   holders, beneficiaries, family network, related customers, linked assets and connected products.
2. WHEN a relationship network is displayed THEN the system SHALL render it as an interactive graph whose
   nodes are the primary node types and whose edges are the relationship types defined in the KG model.
3. WHEN a user selects a graph node THEN the system SHALL display that node's attributes and SHALL allow
   navigation to that entity's own 360 view if it is a customer the user is entitled to view.
4. WHEN a related customer is outside the user's entitlement THEN the system SHALL render the node as
   present-but-restricted, showing relationship type only and no identifying detail.
5. WHEN a household is identified THEN the system SHALL display household net worth, household deposits and
   the count of household products.
6. WHEN a relationship is inferred rather than system-of-record THEN the system SHALL label it as inferred
   and SHALL display its confidence score.
7. WHEN a graph traversal query is executed THEN the system SHALL return results within 2 seconds at p95
   for traversals up to 3 hops.

---

## 7. Customer Journey Intelligence

**User story:** As a relationship manager, I want a chronological narrative of the customer's milestones,
so that I can reference their history in conversation.

### Acceptance criteria

1. WHEN the journey module renders THEN the system SHALL display a chronological timeline combining life
   events, product acquisitions, major transactions, applications, relationship changes and investment
   milestones.
2. WHEN a life event is displayed THEN the system SHALL show life event type, date, confidence score and
   source.
3. WHEN a life event is inferred by an agent THEN the system SHALL label it as AI-inferred and SHALL cite
   the underlying signals used.
4. WHEN a transaction qualifies as a major transaction THEN the system SHALL include it in the timeline,
   where "major" is defined by a configurable absolute threshold and a multiple of the customer's median
   transaction value.
5. WHEN an application appears in the timeline THEN the system SHALL show product applied, application date,
   channel, status, fraud result and decision date.
6. WHEN a customer engagement event occurs THEN the system SHALL record it with event ID, type, date,
   channel, session ID, device type and outcome, and SHALL make the engagement history filterable by
   channel and event type.
7. WHEN the timeline exceeds the visible range THEN the system SHALL support zoom and pan across the
   customer's full tenure.

---

## 8. Risk Intelligence

**User story:** As a risk analyst, I want the customer's risk posture with explainable drivers, so that I
can act on genuine exposure rather than raw scores.

### Acceptance criteria

1. WHEN the risk module renders THEN the system SHALL display risk score, fraud score, PID score, SID
   score, delinquency status, current days past due, default indicator, charge-off indicator, AML flag and
   PEP flag.
2. WHEN a risk score is displayed THEN the system SHALL display its band and the ranked drivers that
   contributed to it.
3. WHEN any risk threshold is breached THEN the system SHALL raise a visible alert on the dashboard,
   ordered by severity.
4. WHEN AML or PEP flags are set THEN the system SHALL display a persistent compliance indicator that
   cannot be dismissed by the user.
5. WHEN exposure analysis renders THEN the system SHALL display total credit exposure across loans, cards
   and contingent facilities.
6. WHEN fraud indicators exist on cards or applications THEN the system SHALL surface them in the risk
   module with their source and date.
7. WHEN a user lacks the risk-view entitlement THEN the system SHALL hide detailed risk scores and SHALL
   display only a coarse risk band.

---

## 9. Offer Intelligence

**User story:** As a relationship manager, I want ranked offers with acceptance likelihood, so that I lead
with the conversation most likely to land.

### Acceptance criteria

1. WHEN the offer module renders THEN the system SHALL display each offer's ID, name, business group, type,
   start date, end date, status, customer reaction and acceptance probability.
2. WHEN offers are recommended THEN the system SHALL rank them by expected value and SHALL display the
   ranking rationale for each.
3. WHEN a next best offer is presented THEN the system SHALL display its acceptance probability as a
   calibrated percentage with a confidence indicator.
4. WHEN cross-sell and upsell opportunities are identified THEN the system SHALL distinguish between the
   two and SHALL show the product-affinity basis for each.
5. WHEN a customer holds a product that an offer would duplicate THEN the system SHALL exclude that offer
   from the ranked recommendations, SHALL record the suppression reason, and SHALL display the offer in a
   visually de-emphasized state with the suppression reason visible to entitled users.
6. WHEN a customer has declined an offer THEN the system SHALL reflect that reaction and SHALL not re-rank
   it into the top position within a configurable cooling-off window.
7. WHEN a campaign is active THEN the system SHALL display campaign membership and current status for the
   customer.

---

## 10. Agentic AI Layer

**User story:** As any authorized user, I want AI-generated narratives and recommendations grounded in the
customer's actual data, so that I can act on them with confidence.

### Acceptance criteria

1. WHEN a customer is loaded THEN the Customer Summary Agent SHALL produce an executive summary, a customer
   snapshot and advisor notes from profile, accounts, products, risk and relationship inputs.
2. WHEN the Financial Health Agent runs THEN it SHALL produce a net worth analysis, spending insights and a
   financial health score.
3. WHEN the Risk Agent runs THEN it SHALL produce a risk assessment, named risk drivers and alerts.
4. WHEN the Life Event Detection Agent runs THEN it SHALL produce a life event timeline and life-event-based
   recommendations, each with a confidence score.
5. WHEN the Offer Recommendation Agent runs THEN it SHALL produce a next best offer, propensity scores and a
   ranked offer list.
6. WHEN the Relationship Intelligence Agent runs THEN it SHALL produce a household view, relationship
   summary and network insights including influence indicators.
7. WHEN the Customer Journey Agent runs THEN it SHALL produce a customer journey, a timeline narrative and a
   growth story.
8. WHEN any agent produces output THEN the system SHALL attach citations to the specific source records
   used, and the UI SHALL allow the user to inspect those records.
9. WHEN an agent cannot ground a claim in available data THEN it SHALL omit the claim rather than
   speculate, and SHALL state which data was unavailable.
10. WHEN an agent is invoked THEN the system SHALL return its output within 5 seconds at p95 and SHALL
    stream partial output where the transport supports it.
11. WHEN an agent invocation fails or times out THEN the system SHALL display a non-blocking failure state
    with retry, and SHALL never display a partial narrative as if it were complete.
12. WHEN agent output is displayed THEN the system SHALL label it as AI-generated and SHALL show the
    generation timestamp.
13. WHEN identical inputs are submitted for the same customer within a configurable TTL THEN the system
    SHALL serve cached agent output rather than regenerating it.
14. WHEN any agent runs THEN the system SHALL log the prompt inputs, tool calls, model identity and output
    to the audit trail.

---

## 11. Natural Language Q&A

**User story:** As a relationship manager, I want to ask questions in plain language directly from the search
landing page — immediately after login, before opening any profile — and get answers with the supporting
detail, so that I do not have to first find a customer or hunt across widgets.

### Acceptance criteria

1. WHEN a user submits a natural-language question from the search landing page THEN the system SHALL search
   across all customers the user is entitled to, resolve which customer the question is about, and return a
   grounded answer together with the underlying records that support it — without requiring the user to open a
   customer profile first. WHEN a question is asked from within a specific customer's view THEN the answer
   SHALL be scoped to that customer. The customer's dashboard SHALL expose a persistent, customer-scoped Ask
   panel (pinned at the top, under the filter bar) as this single-customer entry point.
2. WHEN a question requires relationship traversal THEN the system SHALL query the knowledge graph and
   SHALL present the traversal path used.
3. WHEN a question requires aggregation across accounts, transactions or products THEN the system SHALL
   compute the aggregate deterministically from source records rather than inferring it from narrative.
4. WHEN a question is ambiguous THEN the system SHALL ask a clarifying question rather than guessing.
5. WHEN a question falls outside the platform's data THEN the system SHALL state that it cannot answer and
   SHALL NOT fabricate a response.
6. WHEN a question targets data the user is not entitled to see THEN the system SHALL refuse and SHALL log
   the attempt, without disclosing the withheld values.
7. WHEN an answer is returned THEN the system SHALL return it within 5 seconds at p95.
8. WHEN a user asks a follow-up question THEN the system SHALL retain conversation context within the
   current session — the cross-customer search conversation, or the current customer's session when scoped to
   one customer.
9. WHEN a user switches to a different customer's view THEN the system SHALL clear that customer's prior
   context to prevent cross-customer leakage, and the entitlement gate SHALL be enforced per result on the
   cross-customer path so an answer never includes a customer the user is not entitled to see.

---

## 12. Security, Privacy and Compliance

**User story:** As a security officer, I want enforced authentication, authorization, masking and audit, so
that customer data access is controlled and provable.

### Acceptance criteria

1. WHEN a user accesses any API or UI route THEN the system SHALL require a valid OAuth 2.0 access token
   and SHALL reject unauthenticated requests with 401.
2. WHEN the enterprise identity provider is configured THEN the system SHALL support SSO login and SHALL
   derive user roles from IdP claims.
3. WHEN a user requests a resource THEN the system SHALL authorize the request against their role and their
   customer entitlement scope, and SHALL return 403 on failure.
4. WHEN a role lacks entitlement to a sensitive field THEN the system SHALL mask that field at the API
   boundary, and the unmasked value SHALL NOT be present in the response payload.
5. WHEN masking is applied THEN the system SHALL mask at minimum full account numbers, card numbers, VIN,
   national identifiers, date of birth and full street address, per the role matrix.
6. WHEN a user performs any read of customer data, agent invocation, unmask action or export THEN the
   system SHALL write an immutable audit record containing actor, role, customer ID, action, fields
   accessed, timestamp, source IP and correlation ID.
7. WHEN data is stored THEN the system SHALL encrypt it at rest.
8. WHEN data is transmitted THEN the system SHALL enforce TLS 1.2 or higher.
9. WHEN customer data is sent to an AI model THEN the system SHALL apply the configured data-minimization
   policy and SHALL record which fields were included.
10. WHEN a session is idle beyond the configured timeout THEN the system SHALL terminate the session.
11. WHEN audit records are written THEN they SHALL be append-only and retained per the configured retention
    period.

---

## 13. Performance, Scalability and Reliability

### Acceptance criteria

1. WHEN the Customer 360 dashboard is requested THEN the system SHALL complete initial meaningful render
   within 3 seconds at p95.
2. WHEN an agent is invoked THEN the system SHALL respond within 5 seconds at p95.
3. WHEN a graph query up to 3 hops is executed THEN the system SHALL respond within 2 seconds at p95.
4. WHEN the platform is loaded THEN it SHALL support at least 100 customers in the dataset and 100
   concurrent users without breaching the above targets.
5. WHEN API load increases THEN the API tier SHALL scale horizontally behind a load balancer with no
   sticky-session dependency.
6. WHEN multiple agents are required for one dashboard THEN the system SHALL execute independent agents
   concurrently.
7. WHEN a downstream dependency is unavailable THEN the system SHALL fail that module gracefully, apply
   circuit-breaking, and continue serving unaffected modules.
8. WHEN any request is served THEN the system SHALL propagate a correlation ID across API, graph and agent
   calls for traceability.

---

## 14. Data Foundation

### Acceptance criteria

1. WHEN the system is provisioned THEN it SHALL contain a synthetic dataset of at least 100 customers
   covering every entity in the domain data model, and the generated volume SHALL be configurable so the
   same generator can produce 1,000 or more customers without code changes.
2. WHEN the dataset is generated THEN it SHALL produce realistic distributions across segments, value
   tiers, credit bands, delinquency states, product mixes and life stages, including deliberate edge cases:
   thin-file customers, high-net-worth customers, delinquent customers, fraud-flagged customers, and
   customers with no relationships.
3. WHEN customers are related THEN the dataset SHALL contain multi-member households with joint accounts,
   beneficiaries and linked assets sufficient to exercise 3-hop graph traversal.
4. WHEN the knowledge graph is built THEN it SHALL contain every primary node type and every relationship
   type defined in the KG model, and SHALL be reconstructible from the relational source data.
5. WHEN a data record is loaded THEN the system SHALL validate it against its schema and SHALL reject or
   quarantine records that fail validation.
6. WHEN derived values such as net worth, utilization or household aggregates are stored THEN the system
   SHALL be able to recompute them from source records.
7. WHEN the dataset is regenerated with the same seed THEN it SHALL produce identical output.

---

## 15. API Surface

### Acceptance criteria

1. WHEN the API is deployed THEN it SHALL expose `GET /customers/{id}`, `GET /customers/{id}/360`,
   `GET /customers/{id}/accounts`, `GET /customers/{id}/loans`, `GET /customers/{id}/deposits`,
   `GET /customers/{id}/investments`, `GET /customers/{id}/relationships`,
   `GET /customers/{id}/household`, `GET /customers/{id}/risk`, `GET /customers/{id}/insights` and
   `GET /customers/{id}/recommendations`.
2. WHEN any endpoint returns an error THEN it SHALL use a consistent error envelope containing code,
   message, correlation ID and no sensitive data.
3. WHEN a collection endpoint is called THEN it SHALL support pagination and SHALL return total counts.
4. WHEN the API is deployed THEN it SHALL publish an OpenAPI specification that matches the implementation.
5. WHEN a requested customer does not exist or is not entitled THEN the API SHALL return 404 for
   non-existent and 403 for non-entitled, without leaking existence beyond that distinction where policy
   requires uniform 404.

---

## 16. Accessibility and Usability

### Acceptance criteria

1. WHEN any UI is rendered THEN it SHALL meet WCAG 2.1 AA for contrast, focus visibility, keyboard
   operability and programmatic labelling.
1a. WHEN the UI offers a light and a dark theme THEN it SHALL persist the user's explicit choice, SHALL
    otherwise follow the operating system `prefers-color-scheme`, and SHALL meet the WCAG 2.1 AA contrast bar
    in **both** themes.
1b. WHEN an icon or glyph conveys meaning THEN that meaning SHALL also be carried by an adjacent text label,
    and purely decorative glyphs SHALL be hidden from assistive technology.
1c. WHEN a value originates as a machine token — an enum, a masked score band or a dotted field path — THEN
    the UI SHALL present it in human-readable form (e.g. `VERY_LOW` → "Very low", `net_worth_cents` → "Net
    worth") while leaving server-formatted strings (bands, partials, ranges) and numbers unchanged.
1d. WHEN a chart or visual is rendered as decorative reinforcement THEN it SHALL sit alongside the numbers or
    a data-table equivalent that remains the accessible source of truth, and SHALL NOT be the only way to
    obtain the information.
2. WHEN a chart or graph is rendered THEN the system SHALL provide an equivalent accessible text or table
   representation.
3. WHEN a user navigates by keyboard alone THEN all interactive elements including the relationship graph
   SHALL be reachable and operable.
4. WHEN content updates asynchronously THEN the system SHALL announce the update to assistive technology.

> Full WCAG conformance requires manual testing with assistive technologies and expert accessibility
> review; automated checks alone are not sufficient evidence.

---

## 17. Knowledge Retrieval (RAG Layer)

**User story:** As a relationship manager, I want the AI to explain product eligibility, policy and process
alongside customer facts, so that I get an answer I can act on rather than one I have to go look up.

### Scope boundary

The retrieval layer covers **institutional knowledge only** — product catalogs, policies, procedures, offer
terms, disclosures and advisor playbooks. Customer facts continue to come exclusively from the typed tool
layer. This boundary is a requirement, not an implementation preference: retrieval is approximate, and a
customer's balance or delinquency status must never be approximate.

### Acceptance criteria

1. WHEN the knowledge base is provisioned THEN it SHALL contain documents across product catalog, lending
   and account policy, KYC/AML and fraud procedure, offer terms and conditions, advisor playbooks, and
   compliance disclosure language.
2. WHEN a document is ingested THEN the system SHALL record its document ID, title, knowledge domain,
   version, effective-from and effective-to dates, jurisdiction, access level and source, and SHALL split it
   into retrievable chunks with stable deterministic chunk identifiers.
3. WHEN a document is re-ingested with changes THEN the system SHALL create a new version and SHALL retain
   the prior version for audit, and retrieval SHALL return only chunks effective as of the query date.
4. WHEN a retrieval query is executed THEN the system SHALL combine lexical and semantic retrieval and
   SHALL fuse the two result sets into a single ranked list.
5. WHEN retrieval results are assembled THEN the system SHALL return the passage text together with document
   ID, section, version and effective date sufficient to cite the source.
6. WHEN an agent or answer uses retrieved knowledge THEN the system SHALL cite the specific document and
   section, and the UI SHALL allow the user to inspect the cited passage.
7. WHEN a numeric or customer-specific claim is made THEN the system SHALL ground it in the customer fact
   layer and SHALL NOT ground it in retrieved documents.
8. WHEN a document carries an access level above the user's role entitlement THEN the system SHALL exclude
   it from retrieval before ranking, and SHALL NOT reveal its existence.
9. WHEN a retrieval query is constructed from customer context THEN the system SHALL include only
   non-identifying qualifiers such as held product types, segment and life stage, and SHALL NOT include
   personally identifying data in the query.
10. WHEN retrieval returns no sufficiently relevant passage THEN the system SHALL state that no supporting
    guidance was found and SHALL NOT substitute unsupported content.
11. WHEN retrieved content contains text resembling instructions to the model THEN the system SHALL treat it
    as reference data only and SHALL NOT allow it to alter agent behavior.
12. WHEN retrieval executes as part of a dashboard agent THEN it SHALL complete within 500 ms at p95, and as
    part of a Q&A answer within 1.5 seconds at p95.
13. WHEN reranking is enabled THEN the system SHALL apply it to fused candidates before context assembly,
    and WHEN reranking is unavailable THEN the system SHALL fall back to fusion order without failing the
    request.

---

## 18. Observability

**User story:** As an operator, I want to see how the platform and its agents behave in production, so that
I can prove the latency and quality targets are being met and diagnose failures quickly.

### Acceptance criteria

1. WHEN any request is processed THEN the system SHALL emit a distributed trace spanning the API, the
   authorization gate, application services, database queries, graph traversals, retrieval calls, agent
   nodes and model invocations.
2. WHEN telemetry is emitted THEN traces, metrics and logs SHALL share a common trace identifier so that a
   log line, a metric spike and a trace can be correlated.
3. WHEN a model or agent operation is instrumented THEN the system SHALL follow standard generative-AI
   telemetry conventions, recording model identity, operation, token counts and duration.
4. WHEN telemetry is emitted THEN the system SHALL record request rate, error rate and duration per
   endpoint, and SHALL record duration, outcome, token usage and estimated cost per agent.
5. WHEN telemetry is emitted THEN the system SHALL record retrieval latency, candidate counts, zero-result
   rate and rerank usage.
6. WHEN telemetry is emitted THEN the system SHALL record database connection pool wait time, busy-retry
   counts, graph hop and node counts, graph truncation events, agent cache hit ratio, claim-validator
   rejection rate and agent degradation rate.
7. WHEN the audit queue depth grows or a request fails closed for audit reasons THEN the system SHALL emit a
   metric and SHALL raise an alert.
8. WHEN telemetry is emitted THEN it SHALL NOT contain personally identifying data, monetary values, prompt
   content or completion content, and customer identifiers SHALL NOT be used as metric labels.
9. WHEN a customer reference is required for trace-level diagnosis THEN the system SHALL use a salted
   pseudonymous identifier rather than the customer ID.
10. WHEN the platform is deployed THEN it SHALL expose dashboards covering platform health and error budget,
    agent performance and cost, retrieval quality, and data and security signals.
11. WHEN a service level objective is defined THEN it SHALL correspond to a stated performance requirement,
    and the system SHALL alert on error-budget burn rate rather than on single-sample threshold breaches.
12. WHEN the dashboard is used by a real user THEN the system SHALL capture client-side timing for
    meaningful render and first AI card so that the 3-second and 5-second targets are measured as
    experienced, not only server-side.
13. WHEN a telemetry backend is changed THEN it SHALL require only collector configuration changes and no
    application code changes.
14. WHEN readiness is evaluated THEN the system SHALL report the health of the customer database, the
    knowledge base, the model provider and the audit writer.

---

## 19. Agent Evaluation Framework

**User story:** As an AI owner, I want agent quality measured against known ground truth and gated in CI, so
that a prompt or model change cannot silently degrade what a relationship manager is shown.

### Acceptance criteria

1. WHEN the evaluation framework is provisioned THEN it SHALL include a fixed evaluation panel of customers
   spanning every generated cohort, a question bank with expected answers and expected behaviors, per-customer
   material-fact expectations, and an adversarial suite.
2. WHEN an evaluation run executes THEN it SHALL record the prompt version, model identity, provider, data
   seed, code revision and configuration for every result.
3. WHEN agent output is evaluated THEN the system SHALL measure schema conformance, groundedness of numeric
   claims, citation validity, coverage of material facts, and latency, tokens and cost per agent.
4. WHEN a customer has a material fact such as severe delinquency, an AML or PEP flag, a maturing deposit or
   a fraud alert THEN evaluation SHALL assert that the relevant agent output surfaces it.
5. WHEN agent output is evaluated for safety THEN the system SHALL assert that no value the role is not
   entitled to see appears in any narrative, and any occurrence SHALL fail the run.
6. WHEN the adversarial suite executes THEN it SHALL include instruction-like text embedded in customer data
   fields, attempts to extract masked fields, attempts to retrieve another customer's data, and attempts to
   override agent instructions, and all SHALL be resisted.
7. WHEN life-event detection is evaluated THEN the system SHALL report precision, recall and F1 against the
   seeded ground-truth life events.
8. WHEN offer recommendation is evaluated THEN the system SHALL report ranking quality against the seeded
   acceptance propensities.
9. WHEN risk output is evaluated THEN the system SHALL assert that the named risk drivers correspond to the
   actual drivers of the underlying score.
10. WHEN Q&A is evaluated THEN the system SHALL report answer correctness against the answer key and a
    confusion matrix over answer, clarify and refuse behaviors.
11. WHEN retrieval is evaluated THEN the system SHALL report recall at k, mean reciprocal rank and
    attribution correctness against labelled query-to-passage pairs.
12. WHEN qualitative dimensions such as clarity and usefulness are evaluated THEN the system MAY use a model
    as judge against a published rubric, and such scores SHALL be advisory and SHALL NOT be the sole basis
    for a pass or fail gate.
13. WHEN an evaluation run completes THEN the system SHALL produce a machine-readable and a human-readable
    report and SHALL diff the results against the stored baseline.
14. WHEN evaluation runs in continuous integration THEN it SHALL execute deterministically against the mock
    provider on every change, and SHALL fail the build on any entitlement leakage, any adversarial failure,
    any schema non-conformance, or a groundedness or coverage regression beyond the configured tolerance.
15. WHEN evaluation runs against the live model provider THEN it SHALL be schedulable and on-demand, and
    SHALL report cost for the run.
16. WHEN a prompt or model change is proposed THEN the system SHALL support comparing challenger against
    champion on the same panel and SHALL block promotion on regression.
17. WHEN evaluation results are stored THEN they SHALL be retained per run so that quality trend over time
    is reportable.

---

## 19a. Revenue Intelligence

**User story:** As a relationship manager or a regional head, I want every opportunity the platform
can see turned into a *priced* number I can act on — money the bank is leaving on the table — so that
my day is spent on what moves revenue rather than on reading dashboards.

These capabilities were added after the original success criteria (Phase 22). They extend the
deterministic layer: each figure is computed in integer cents from the customer database, and each
finding cites the record or the policy document it rests on. Four plays are defined; play 6 (fee
recovery) is delivered end to end, and plays 4, 5 and 8 exist as UI presenting a defined contract,
falling back to a clearly-labelled illustrative preview until their detection backends are built.

### Acceptance criteria

1. WHEN the landing page loads THEN the system SHALL present a revenue "opportunity pipeline" summary
   across the caller's entitled book — total identified, realistically capturable, realized, and the
   realization rate — entitlement-scoped so a restricted book sees only its own opportunities.
2. WHEN fee recovery is computed for a customer THEN the system SHALL identify deposit charges the
   bank was entitled to but did not collect — a fee never posted despite the cycle not qualifying for
   a waiver, a fee reversed beyond the courtesy allowance, a fee billed below the current schedule,
   and a billable service left unbilled — and SHALL price each in integer cents.
3. WHEN a fee-recovery finding is presented THEN the system SHALL cite the product sheet or policy
   document its amount is measured against, and SHALL state the evidence that established it,
   including which balance basis was used where the source data cannot supply a historical balance.
4. WHEN the evidence for a recoverable charge is ambiguous THEN the system SHALL decline to raise a
   finding rather than assert one, because presenting a bank with fees it was not entitled to is a
   worse error than a missed recovery.
5. WHEN a role sees banded rather than exact balances THEN the system SHALL band recoverable amounts
   in the same way, and SHALL never place an account's full number in a finding.
6. WHEN a fee-recovery scan is run THEN the system SHALL write an immutable audit record of the read,
   recording the kinds of finding surfaced but no monetary value (per §18 telemetry rules).
7. WHEN a revenue play's detection backend is not yet available THEN the system SHALL present its
   panel with clearly-labelled illustrative figures behind a visible "preview" marker, and SHALL
   NOT present an ungrounded figure as if it were grounded customer data.
8. WHEN money-in-motion, held-away assets, or economic profit are surfaced THEN the system SHALL do so
   as priced opportunities with a dollar target and, where inferred, a confidence and the observed
   basis — never as an unqualified assertion. *(Backends for these three are scoped, not yet built.)*

---

## 19b. Conversational Voice Assistant

**User story:** As a relationship manager, I want to speak my question and hear the answer, with a
friendly assistant presence, so that using the platform feels like talking to a colleague rather than
filling in a form.

These capabilities were added in Phase 23. They are a presentation layer over the existing grounded
Q&A: voice input only ever produces text that flows through the same claim-validated, entitlement-
scoped pipeline a typed question does, so no grounding, masking or entitlement guarantee is affected.

### Acceptance criteria

1. WHEN the browser supports speech recognition THEN the system SHALL offer a microphone control that
   transcribes a spoken question into the Ask composer, and SHALL show the transcript as it is spoken.
2. WHEN speech recognition is unavailable in the browser THEN the system SHALL hide the microphone
   affordance rather than present a broken control, and the typed experience SHALL be unaffected.
3. WHEN voice input is offered THEN the system SHALL make it opt-in (nothing listens until the user
   activates the microphone) and SHALL disclose that browser transcription may send audio to the
   browser's provider, because a spoken banking question can contain personally identifying data.
4. WHEN spoken replies are enabled THEN the system SHALL read a completed answer aloud, SHALL default
   this off, SHALL persist the user's choice, and SHALL never speak a refused answer or mid-stream
   partial.
5. WHEN the assistant is present THEN the system SHALL show an assistant avatar whose state reflects
   what it is actually doing — idle, listening, thinking, or speaking — and whose speaking motion is
   driven by the same synthesized speech the user hears, so the avatar never implies a live capability
   the platform does not have.
6. WHEN an assistant avatar image is supplied THEN the system SHALL use it, and SHALL otherwise fall
   back to an illustrated portrait, with no code change required to swap between them.
7. WHEN the assistant receives a greeting, a thanks, or a "what can you do" question THEN it SHALL
   respond conversationally and describe its capabilities, rather than treating the message as a
   customer lookup.
8. WHEN any voice or avatar animation is shown THEN it SHALL be suppressed under the user's
   reduced-motion preference, and the meaning SHALL remain carried by the answer text and status.

---

## 19c. Cohort and Pitch Questions in Ask AI

**User story:** As a relationship manager, I want to ask about a *group* of customers ("who are my
high-risk customers", "which clients are past due") and to ask the assistant to "prepare a pitch" or
tell me "what to say" to a customer, so that Ask AI answers the questions I actually have, not only
single-fact lookups.

These capabilities were added in Phase 24. They are two new tools on the same claim-validated,
entitlement-scoped Q&A pipeline the typed and spoken questions already use: nothing bypasses masking,
grounding or entitlement. Book-level questions were previously answered "no matching customer was
found" because the only cross-customer tool was a name resolver; these criteria close that gap.

### Acceptance criteria

1. WHEN a question is about a set of customers by a criterion — a risk band, a segment, a value tier,
   or whether they are past due — THEN the system SHALL list the matching customers from the caller's
   entitled book, ranked, rather than attempting to resolve a single named customer.
2. WHEN a cohort question is answered THEN the result SHALL be entitlement-scoped inside the query, so
   a restricted book never surfaces a customer outside it and the count never reveals an out-of-book
   customer (requirement 3.3).
3. WHEN a cohort is listed THEN each member SHALL carry only display-only, non-maskable attributes
   (identifier, name, segment, value tier, the derived risk band, delinquency status) — never a raw
   balance — because a cohort list is a picker into the reading tools, not citable financial data.
4. WHEN "high risk" or "the riskiest" customers are asked for THEN the system SHALL interpret that as
   the top of the risk distribution (the band and above), not one exact band, so a legitimately empty
   top band does not read as a broken feature.
5. WHEN the user asks to prepare a pitch, for talking points, or what to tell a customer THEN the
   system SHALL compose one grounded briefing from that customer's recommended offers, headline
   financial position, and risk and compliance flags.
6. WHEN a pitch is composed THEN every figure in it SHALL be masked per the caller's role exactly as
   the underlying offer, financial and risk reads are, and SHALL be cited to the record — a role that
   cannot see a balance or an offer's expected value never receives it in a talking point.
7. WHEN either capability runs on the universal Ask surface or within a single customer's dashboard
   THEN it SHALL behave identically, because both surfaces share one Q&A graph and tool registry.

---

## 19d. Conversational Refinement, Live Generation and Ask AI Presentation

**User story:** As a relationship manager, I want Ask AI to feel like a real conversation — I refine
my last question, I ask "who can give me the most profit", I read a natural-language answer in a
proper chat window, and when the model is live it narrates rather than dumping fields.

These capabilities were added in Phase 25, refining Phase 24. They are conversational, presentation
and provider changes over the same claim-validated, entitlement-scoped pipeline: no grounding,
masking or entitlement guarantee is affected, and no REST surface is added.

### Acceptance criteria

1. WHEN a follow-up refines a cohort already established in the conversation ("only high risk", "just
   the platinum ones", "who of those is past due") THEN the system SHALL continue that cohort rather
   than treat the refinement as a customer-name lookup.
2. WHEN a question asks for the most valuable / most profitable customers, or who to pitch, without a
   named filter THEN the system SHALL return the entitled book ranked by customer value, rather than
   answering "no supporting guidance".
3. WHEN a risk question names no explicit band ("rising risk", "customers at risk") THEN the system
   SHALL interpret it as the riskiest cohort (the elevated band and above), rather than listing the
   whole book unfiltered.
4. WHEN a grounded answer must fall back to the deterministic template (live generation unavailable,
   or the model's prose failed claim validation) THEN the fallback SHALL be human-readable — money
   as dollars with a labelled field — while still citing every figure by its source id, rather than a
   raw ``field_cents: 12345`` dump.
5. WHEN live generation is configured THEN the system SHALL use Amazon Bedrock (a Claude model, via a
   cross-region inference profile where required) for narration, and SHALL fall back to the
   deterministic provider without failing the request when Bedrock is unavailable.
6. WHEN a conversation has one or more turns THEN the Ask panel SHALL present a scrolling chat thread
   that keeps the latest message in view, SHALL let the user send further messages in the same
   thread, and SHALL offer a collapse control that closes the panel back to its launcher.
7. WHEN an answer contains light markdown emphasis (``**bold**``) THEN the UI SHALL render it as
   emphasis rather than showing the literal characters, and SHALL preserve the answer's line breaks.

---

## 20. Assumptions

These were not stated in the source spec. They are the working assumptions for design unless corrected.

| # | Assumption |
|---|---|
| A1 | The platform is read-only over source data; no write-back to core systems in this release. |
| A2 | Source data is supplied as batch extracts or generated synthetically; no live core-banking connectivity. |
| A3 | Propensity, FICO, behavior, PID, SID and fraud scores arrive as data. Where absent, the platform computes clearly-labelled heuristic substitutes. |
| A4 | A single tenant / single bank; no multi-tenant isolation required. |
| A5 | English-only UI in this release, with preferred-language captured as data for future localization. |
| A6 | Currency is USD, single currency, with formatting localized to the user. |
| A7 | Entitlement scope is expressed as an assignable book of business per user, plus role-level field policy. |
| A8 | Deployment target is containerized on a single cloud provider. |
| A9 | The knowledge corpus is institutional and non-customer-specific. No customer documents, statements or correspondence are indexed. |
| A10 | The knowledge corpus is synthetic for this build, authored to be structurally realistic; real policy documents can be ingested through the same pipeline. |
| A11 | Ground truth for evaluation is derived from the seeded generator, which is possible because generation is deterministic. Evaluation against production data would require a separate labelling effort. |
| A12 | Telemetry is exported to a collector; the concrete backend (self-hosted or vendor) is a deployment choice, not an application concern. |

---

## 21. Decisions — LOCKED

All decisions are confirmed by the stakeholder. Design proceeds on these; see `design.md` §1.

| # | Decision | Locked choice | Notes |
|---|---|---|---|
| D1 | Technology stack | React 18 + TypeScript (Vite) / FastAPI + Python 3.12 | Python keeps agents, scoring and data generation in one ecosystem |
| D2 | Graph storage | **SQLite** property-graph tables with recursive CTE traversal | `GraphRepository` port keeps a Neo4j adapter available for production |
| D3 | Primary datastore | **SQLite 3** (WAL, JSON1, FTS5) | Money stored as integer cents; derived tables replace materialized views; see `design.md` §1.1 |
| D4 | LLM provider | **Amazon Bedrock** (Converse API) | `MockLLMProvider` retained for CI and as the circuit-breaker fallback |
| D5 | Agent orchestration | **LangGraph** `StateGraph` + `SqliteSaver` | Three-wave dashboard graph; ReAct loop for Q&A |
| D6 | Auth for the build | Local OAuth 2.0 provider behind an `IdentityProvider` port | OIDC adapter documented for enterprise IdP swap |
| D7 | Real vs synthetic data | Seeded synthetic generator in-repo | `SourceLoader` port allows an extract loader later |
| D8 | NLP Q&A approach | Tool-calling over the typed service layer | Text-to-SQL rejected: a generated query bypasses masking and entitlement |
| D9 | Deliverable shape | Full-stack application | All eleven success criteria are RM-facing workflows |
| D10 | Scoring transparency | Heuristic scores labelled; bureau-style scores consumed as data | A heuristic is never presented as a bureau score |
| D11 | Retrieval layer | Hybrid RAG over institutional knowledge in SQLite (`sqlite-vec` + FTS5), Bedrock Titan Text Embeddings V2, optional Bedrock Rerank | Customer facts stay tool-called; retrieval never supplies a customer figure |
| D12 | Observability | OpenTelemetry SDK with GenAI semantic conventions, OTLP to a collector; Prometheus + Grafana + Jaeger locally | Backend swap is a collector config change (CloudWatch / X-Ray / vendor) |
| D13 | Agent evaluation | In-repo framework with ground truth derived from the seeded generator; deterministic gates in CI on the mock provider, scheduled runs on Bedrock | Panel and question bank extend to real labelled data later |

### 21.1 Policy decisions confirmed

| Topic | Confirmed position |
|---|---|
| Contact Center financial detail | Banded balances, not exact figures |
| Marketing access to PII | Fully pseudonymized; no names or contact data |
| Household definition | Derived from address + relationships (no system-of-record household ID) |
| Suppressed offers | Removed from ranking but shown greyed with the suppression reason visible |

---

## 22. Traceability to success criteria

| Source success criterion | Covered by |
|---|---|
| 1. Search for a customer | §3 |
| 2. View complete Customer 360 profile | §4 |
| 3. View products and holdings | §5.3–5.7 |
| 4. View financial health indicators | §5.1, §5.8–5.11 |
| 5. View customer risk profile | §8 |
| 6. View relationship network and household | §6 |
| 7. View life-event timeline | §7 |
| 8. View AI-generated customer summary | §10.1 |
| 9. View personalized offers and propensity | §9 |
| 10. View next best actions and recommendations | §9.2–9.4, §10.5 |
| 11. Find customer information via NLP | §11, §17 (policy and product questions) |

### 22.1 Supporting capability coverage

| Capability | Requirement section |
|---|---|
| Knowledge retrieval (RAG) | §17 |
| Observability | §18 |
| Agent evaluation | §19 |
| Revenue intelligence (post-criteria, Phase 22) | §19a |
| Conversational voice assistant (Phase 23) | §19b |
| Cohort and pitch questions in Ask AI (Phase 24) | §19c |
| Conversational refinement, live generation, Ask AI presentation (Phase 25) | §19d |
