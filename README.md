# CMN-C2-258 — Enterprise Competitive Analysis & Win Rate Estimation Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

This agent drafts a competitive briefing for a B2B deal. Given one or more named competitors, it
collects facts from their public pages with source attribution, aggregates the operator's own
historical win/loss records, and returns a single Markdown report covering feature comparison,
differentiation angles, and a win-rate estimate that always carries its sample size.

It suits the moment before a competitive bid: a salesperson working out what to emphasise against
a particular rival, a sales-operations team keeping a periodic view of competitor movement, or a
manager who wants an allocation decision backed by "M wins out of N" rather than by impression.

The split between what is computed and what is written by a language model is deliberate, because
the two carry very different warranties:

- **Win rates are counted, never generated.** The aggregation is deterministic record counting over
  the operator's `win_loss` table. The model never sees the arithmetic and never produces a rate.
- **Every rate carries its sample size, and the output gate enforces it.** A win-rate line that
  reaches the output without an adjacent sample-count annotation is replaced with an explicit
  notice rather than published. Below `min_sample` matches, the figure is labelled a reference
  value; no statistical test is claimed.
- **When matches are too few, the filters relax in a stated order** (`fallback_order`) rather than
  silently widening. The report records which filters were relaxed.
- **Collected facts are short attributed snippets, not page copies.** Each snippet is clipped to
  `fact_max_chars`, and the output gate re-clips any quotation that grew past `quote_max_chars`.
- **Records that do not satisfy the data contract are quarantined, not dropped silently.** Rows
  missing a required column, carrying an outcome outside the won/lost enum, or an unparseable
  close date are set aside with a reason and surfaced as a data-quality note, so a column-name
  mismatch shows up as a fixable schema problem rather than as a wrong number.

The agent's value is the join between public information and the operator's own history. With no
win/loss data behind it, the run stops with an error rather than degrading into a public-web
summary — a public-information-only comparison is something a general web-search assistant already
does. It is not a forecasting, pricing, legal or purchasing-decision system.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `win_loss` table reachable through a `BaseDatabase` implementation passed as `config["db"]`
  (or pre-materialised rows as `config["win_loss_rows"]`, which is how the tests run offline).
  The normalised columns are `record_id`, `competitor`, `outcome`, `close_date`, plus the optional
  `deal_name`, `industry`, `product_category`, `amount` and `loss_reason`. Physical column names
  that differ are absorbed by `column_map`; outcome spellings are normalised in both English and
  Japanese;
- a `BaseLLM` implementation as `config["llm"]` for the narrative synthesis. `src/services/azure_llm.py`
  ships an Azure OpenAI adapter that resolves `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` and
  `AZURE_OPENAI_DEPLOYMENT` from the bound secret provider on every call rather than caching a
  client; the Marketplace entry adapter (`cli.py`) wires it and refuses to start if any of the
  three is missing or the endpoint has an unusable shape;
- caller authentication in front of the entry adapter;
- outbound network access to the competitor pages you intend to cite.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`), `timeout_seconds` (`30`) | Framework retry budget and timeout |
| `win_loss_table` (`"win_loss"`) | Table name to read |
| `column_map` (`{}`) | `{normalised name: physical column}` when the table's columns differ |
| `recent_years` (`3`), `max_records` (`500`) | How far back and how many rows to read |
| `amount_bands` (`[10M, 50M, 100M]`) | Boundaries for the "similar deal size" filter |
| `fallback_order` (`industry, product_category, amount_band`) | Order in which filters are relaxed when matches fall below `min_sample` |
| `min_sample` (`3`) | Below this many matches, the rate is labelled a reference value |
| `web_max_pages_per_competitor` (`3`) | Page budget per competitor |
| `web_rate_limit_sec` (`1.0`), `web_timeout_sec` (`10`) | Per-origin request interval and fetch timeout |
| `fact_max_chars` (`90`), `quote_max_chars` (`90`) | Snippet clip length, and the output gate's quotation ceiling |

Note that the standalone server constructs the graph with no configuration — see *Behaviour
without the platform*.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is the agent's import-time dependency: `src/` imports `framework.*` and `shared.*`
directly, so with no SDK installed the process does not start at all. That is what "does not run
standalone" means here — there is no degraded mode that runs without the framework.

With the SDK installed but no platform around it, the standalone server in `src/api/server.py`
does come up, and it is worth being exact about what it can and cannot do:

- `/health` answers `{"status": "ok", "agent": ...}`.
- It constructs the graph as `Graph()` — **with no configuration**. `config/config.yaml` is read
  by the Marketplace entry adapter, not by the standalone server, so every tuning key above falls
  back to its in-code default and neither a database nor a language model is injected.
- Consequently an authenticated `/invoke` reaches the win/loss fetch with an empty repository and
  ends as `status=error`, because contract-conforming history is this agent's operating premise.
  Injecting `config["db"]` (or `config["win_loss_rows"]`) and `config["llm"]` is what makes the
  standalone path usable.

The test suite runs without a platform connection, using injected fakes for the database, the web
fetcher and the language model.

## Input

`POST /invoke` takes a JSON body with `input` and an optional `session_id`. `input` is either a
JSON payload (preferred) or a plain competitor name treated as a single competitor with no deal
context:

| Field | Meaning |
|---|---|
| `competitors` | List of competitor names. Required — an empty list ends the run as an error |
| `industry`, `product_category` | Similarity filters for the win-rate estimate |
| `amount` | Deal size, matched against `amount_bands` |
| `competitor_urls` | `{competitor: [url, ...]}` — the public pages to collect from |

Callers must be authenticated. A verified trust level stamped by upstream middleware is used as
is; otherwise, when `INVOKE_AUTH_TOKEN` is set on the server environment, a matching
`Authorization: Bearer` header promotes the caller to `VERIFIED_EXTERNAL`. A caller that is
neither middleware-verified nor token-authenticated is rejected with **401**, and a caller below
verified trust with **403** — there is no anonymous path into the graph.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"competitors\": [\"CompX\"], \"industry\": \"manufacturing\", \"product_category\": \"ERP\", \"amount\": 45000000, \"competitor_urls\": {\"CompX\": [\"https://competitor.example.com/product\"]}}",
    "session_id": "demo-001"
  }'
```

An empty `input`, an oversized `input`, or a payload naming no competitor ends as `status=error`
before any fetch is attempted.

## Output

`output` is the finished Markdown report (also surfaced as `formatted_output` and `result`),
inside the standard AgentCore envelope:

```markdown
# Competitive briefing

## Win rate from historical records (deterministic count)
- **CompX**: 33% (1 win of 3)  ※reference value (small sample: 3)

## Competitor public information (collected, with sources)
- **CompX** — source: https://competitor.example.com/product (fetched 2026-07-03T…)
  - CompX ERP Suite
  - CompX prices its ERP with maintenance included

## Synthesis
### Differentiation
- Emphasise total maintenance cost
```

Data-quality notes — quarantined rows, relaxed filters, a truncated fetch, a suspected column
mismatch — are carried in the same report rather than being dropped, so the reader can tell a
thin answer from a confident one.

Values above are illustrative; real output depends on the competitors named, the pages reachable,
and the operator's own records.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node body
runs. Web collection is bounded on several axes at once: HTTP and HTTPS only, hosts resolved with
private, loopback, link-local and cloud-metadata addresses refused, redirects re-validated on every
hop instead of followed automatically, a per-origin request interval, a per-competitor page budget,
a response-size cap, and `robots.txt` respected — an explicit `Disallow` is never fetched.

Free text taken from the operator's records is PII-masked **before** it enters state, so it is
masked before it can reach the language model, and masked again on the way out. Individual deal
names and customer names are not carried into the report.

Known limits, recorded rather than hidden:

- **A win rate is a count, not a prediction.** Sample size is always shown, small samples are
  labelled as reference values, and no statistical test is performed.
- **Coverage of public pages is partial by design.** Login-gated and paid content is out of scope,
  and the page budget bounds what is read. The absence of a fact is not evidence of its absence in
  the world.
- **`robots.txt` handling fails open when it cannot be retrieved.** Only an explicit `Disallow`
  blocks a fetch; an unreachable `robots.txt` is treated as permitting collection.
- **The terms of service of the sites you collect from are your responsibility**, not the
  template's.
- **Out-of-enum outcomes are quarantined, not counted.** Deals recorded as cancelled or postponed
  are permitted in the table but excluded from the rate, which is why the quarantine count belongs
  in the report next to the rate.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be made against the primary sources by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), with the domain workflow nested in the
  `main` slot as a subgraph
- Generation mode: `llm` (synthesis only; win-rate aggregation is deterministic)
- HITL: not enabled
- Persistence: none — no checkpointer is configured
- External API clients: the web fetcher is built in; the database and language-model clients are
  deployment-injected

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not — see *Behaviour
without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the win/loss data contract and the
security design. `docs/03_test_spec.md` records the test specification and the verification
results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
