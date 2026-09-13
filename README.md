# skip-tracer

Weekly cron job that finds off-market/distressed properties in Maryland,
filters for genuine absentee/motivated owners, skip-traces them for contact
info, checks flip valuation, estimates a rough condition tier, and emails a
digest with Zillow links. State (which parcels have already been processed)
persists so repeat runs don't reprocess or re-pay BatchData for the same
leads.

## Project layout

```
src/skip_tracer/
  imap_client.py       # MD iMap ArcGIS property search (free, no auth)
  filtering.py           # absentee-address filter + owner entity classification (NER)
  batchdata_client.py     # skip-trace, valuation, permits, DNC/TCPA compliance
  condition.py              # rough condition tier from STRUGRAD + permit recency
  zillow.py                  # zpid-free Zillow search link builder
  gmail_client.py              # Gmail OAuth + send (same pattern as job-finder/nw-deal-screener)
  state.py                      # seen-parcel persistence (local file or GitHub-backed)
  cli.py                          # gather -> filter -> enrich -> digest orchestration
scripts/
  test_skip_trace.py               # manual BatchData verification (needs a funded wallet)
  test_valuation.py                 # manual BatchData verification (needs a funded wallet)
  verify_zillow_slug.py              # manual Zillow slug verification (no API key needed)
tests/                                # pytest unit tests (no network/paid calls)
main.py                                # `python main.py` == `python -m skip_tracer.cli`
```

`cli.py`'s `main()` is built from independently testable pieces:

- `gather_new_leads(seen, jurisdictions)` — fetch MD iMap records, drop
  already-seen parcels, owner-occupied properties, and diplomatic/embassy
  ownership
- `enrich_lead(record, batchdata)` — skip-trace, valuation, permits,
  condition tier, Zillow link for one filtered record
- `build_digest_body(leads)` — plain-text digest body
- `send_weekly_digest(leads, to_email)` — send via Gmail

## Setup

```bash
pipenv install --dev
cp .env.example .env
```

Fill in `.env`:

- `GMAIL_CREDENTIALS_PATH` / `GMAIL_TOKEN_PATH` — see "Gmail API setup" below.
  **If you already have a valid `token.json` from job-finder or
  nw-deal-screener, copy it here instead of re-authorizing** — same OAuth
  app works as long as it has the `gmail.send` scope.
- `DIGEST_EMAIL_TO` — where the weekly digest goes.
- `BATCHDATA_API_KEY` — from BatchData. Requires funding their $50 minimum
  wallet balance first; there's no free trial (confirmed via a live 403
  "Insufficient balance" response with $0 in the wallet).
- `BATCHDATA_MAX_LEADS_PER_RUN` (optional, default `25`) — see "Cost control"
  below before raising this.

**Never commit `.env`, `credentials.json`, or `token.json`** — all three are
gitignored.

## Cost control — read this before your first real run

MD iMap's free absentee-owner filter is broad, not narrow: a live test run
against Montgomery County alone returned **47,888 total parcels** and
**37,917 genuinely absentee** after address-normalization filtering
(12,271 of those also carry a non-sale-transfer motivation signal). This is
every non-owner-occupied property in the county, not just distressed ones —
BatchData's skip-trace/valuation/permits calls are what's expensive
(no free trial), not MD iMap.

`BATCHDATA_MAX_LEADS_PER_RUN` (default `25`) caps how many *new* leads get
BatchData enrichment per run; the rest are left unseen and picked up on a
future run rather than being enriched or dropped. Filtering (Modules 1-2) is
free and runs over every candidate regardless of this cap. Raise it only
once you've confirmed BatchData's actual cost per skip-trace/valuation/permit
call against your budget.

## Test-First Checklist

Everything above marked UNVERIFIED in code comments (BatchData skip-trace
and valuation response shapes, the zpid-free Zillow slug) was inferred from
documentation patterns, not confirmed with a live call. Do these before
trusting the corresponding module in a real run:

- [ ] `PYTHONPATH=src pipenv run python scripts/test_skip_trace.py "<street>" <city> MD <zip>`
      against a real, already-known lead. Confirm the actual response field
      names and adjust `batchdata_client.py`'s `skip_trace()`/`cli.py`'s
      `enrich_lead()` parsing if they differ from the current best guess.
- [ ] `PYTHONPATH=src pipenv run python scripts/test_valuation.py "<street>" <city> MD <zip>`
      — confirm whether the `valuation` dataset returns a single AVM figure
      or raw comps. If comps, `estimate_arv_from_comps()` is needed and
      already wired into `enrich_lead()`; if a single figure, simplify that
      code path to use it directly.
- [ ] `PYTHONPATH=src pipenv run python scripts/verify_zillow_slug.py` — open
      the printed zpid-free search URL in a browser and confirm it resolves
      to the same listing as the known-good canonical URL it prints
      alongside it.
- [ ] Fund BatchData's $50 wallet minimum.
- [ ] Confirm Gmail OAuth credentials/refresh token still valid (see above),
      or run the consent flow fresh.
- [ ] Pull ~25 records from a couple of counties beyond Montgomery/PG and
      manually audit for false positives — the absentee filter was tuned
      against Montgomery + Prince George's specifically and hasn't been
      validated to generalize to all 24 MD jurisdictions.

## Gmail API setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create (or
   pick) a project, then enable the **Gmail API**
   (APIs & Services -> Library -> Gmail API -> Enable).
2. Configure the OAuth consent screen as **External** + **Testing**, and add
   your own Gmail address as a test user.
3. Under **Credentials**, create an **OAuth client ID** of type
   **Desktop app**. Download the resulting JSON and save it as
   `credentials.json` in the project root (or wherever
   `GMAIL_CREDENTIALS_PATH` points).
4. First run opens a browser for you to log in and consent; it then caches a
   refresh token at `GMAIL_TOKEN_PATH` (`token.json` by default).
5. Scope used: `gmail.send` only.

## Usage

```bash
PYTHONPATH=src pipenv run python -m skip_tracer.cli
```

Or `pipenv run python main.py`, equivalent. Pass `--jurisdictions MONT PRIN`
to limit a run to specific counties, or `--no-email` to skip sending.

## Deploying to Render

The root [`render.yaml`](render.yaml) deploys the CLI as a weekly Python
cron job (`0 13 * * 1` — Monday 13:00 UTC). Complete the following in the
Render dashboard before the first run:

1. Create the Blueprint from this repository's `render.yaml`. When
   prompted, provide `DIGEST_EMAIL_TO` and `BATCHDATA_API_KEY`.
2. Add a secret file named `credentials.json` (Google OAuth desktop-app
   client secret) under the cron job's **Environment** page — mounted at
   `/etc/secrets/credentials.json`.
3. Add a secret file named `token.json` (the refresh token from completing
   the Gmail flow locally) — mounted at `/etc/secrets/token.json`. Never
   generate either file on Render or commit their contents.
4. Optionally set `BATCHDATA_MAX_LEADS_PER_RUN` to override the default.
5. For persistent state across Render's ephemeral cron disk, set
   `ST_GITHUB_TOKEN` (a fine-grained PAT with Contents read/write on this
   repo) and `ST_GITHUB_REPO` — `state.json` is then read/written via the
   GitHub Contents API instead of the local disk. Leave both unset to use
   the local file, which won't persist between Render runs.

## Pipeline logic

1. **`imap_client.fetch_jurisdiction_leads(jurs_code)`** — paginated MD
   iMap query, no auth. Runs `MONT`/`PRIN` first
   (`imap_client.PROCESSING_ORDER`), then the remaining 22 jurisdictions.
2. **`filtering.is_genuinely_absentee(record)`** — normalized owner-vs-
   property address comparison. `OOI<>'H'` alone false-positives 28-36% of
   the time (same-address records still flagged non-owner-occupied).
3. **`filtering.classify_owner_entity(record)`** — Hugging Face NER
   (`dslim/bert-base-NER`, lazily loaded on first non-diplomatic-keyword
   call) flags corporate/institutional ownership; a small diplomatic
   keyword list (`EMBASSY`, `EMBSY`, `CONSULATE`) catches what `EXCLASS`
   misses, and diplomatic-owned records are dropped entirely.
4. **`cli.enrich_lead(record, batchdata)`** — skip-trace for
   owner name/phone, DNC check on any phone found, valuation lookup for an
   ARV estimate, `flag_low_margin()` against the county's own assessed
   value, permits for `condition.condition_tier()`, and a Zillow search
   link (built from `PREMCITY`, not `CITY` — MD iMap's two city fields can
   disagree; see `zillow.py`'s docstring for the confirmed real example).
   Degrades per-lead on any BatchData failure rather than failing the run.
5. **`cli.build_digest_body()` / `send_weekly_digest()`** — one digest
   email per run listing every newly-enriched lead.
6. **`state.load_seen_parcels()` / `save_seen_parcels()`** — dedupe key is
   MD iMap's `ACCTID`.

## Known limitations (v1 POC)

- **No repair-cost source.** `batchdata_client.max_allowable_offer()` exists
  but isn't wired into the pipeline — nothing here produces an actual
  dollar repair estimate, so `condition.condition_tier()` is a triage tier,
  not a number a 70%-rule MAO calculation could use yet.
- **DC and two other markets (Tarrant County TX, Baltimore/Anne Arundel) are
  out of scope for v1** — tracked as GitHub issues (see below), not built
  here.
- **Street View + vision-model condition assessment is deferred to v2** —
  also tracked as a GitHub issue.

See the repo's GitHub issues for the full v2 backlog with context for
picking each one up cold.
