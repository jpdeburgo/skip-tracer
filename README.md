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
  database.py                    # Render Postgres: property catalog + lead CRUD
  lead_scoring.py                 # motivation/profit/contactability call-priority model
  cli.py                          # gather -> filter -> enrich -> digest orchestration
scripts/
  test_skip_trace.py               # manual BatchData verification (needs a funded wallet)
  test_valuation.py                 # manual BatchData verification (needs a funded wallet)
  verify_zillow_slug.py              # manual Zillow slug verification (no API key needed)
  pull_maryland_leads.py              # BILLABLE monthly property/search pull -> Postgres
  import_search_results.py             # backfill saved JSON into Postgres (free)
  score_leads.py                        # (re)score the catalog into `leads` (free)
  manage_leads.py                        # lead CRUD: record call outcomes (free)
  email_top_leads.py                      # email the top N Maryland leads (free)
tests/                                     # pytest unit tests (no network/paid calls)
main.py                                     # `python main.py` == `python -m skip_tracer.cli`
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

`BATCHDATA_MAX_LEADS_PER_RUN` (default `25`) is a **target number of leads
worth pursuing to find**, not a cap on how many candidates get enriched —
most candidates get dropped by `cli.filter_worth_pursuing()` (see "Pipeline
logic" below), so `main()` keeps enriching candidates one at a time until
either that many pass, or `BATCHDATA_MAX_ENRICHMENT_ATTEMPTS` (default: 5x
the target) candidates have been tried, whichever comes first — that second
env var is the actual hard ceiling on spend for a run where the match rate
is low. Enrichment stops as soon as the target is hit, so a gathered-but-
never-enriched candidate isn't billed and isn't marked seen either — it's
simply tried again on a future run. Filtering (Modules 1-2) is free and
runs over every gathered candidate regardless of either cap. `enrich_lead()`
makes 2 billable BatchData calls per lead (skip-trace + valuation), not 5 —
see "Pipeline logic" below for why permits/DNC/TCPA don't need their own
calls. Raise either cap only once you've confirmed per-lead cost and your
actual match rate against your budget.

### The `property/search` discovery path is separately billable

`scripts/pull_maryland_leads.py` uses a *different* endpoint from the
per-lead enrichment above, and it has its own guard rails:

- Each call returns a **hard maximum of 25 properties** regardless of the
  requested `take`. Full statewide MD coverage of the ~3,986 current
  preforeclosure matches would need roughly 159 more calls.
- A **30-day cooldown** is enforced in `batchdata_search_runs`, keyed on
  `(query, quicklist)`. A second run inside the window is a no-op, not a
  charge. The cooldown checks only *successful* runs, so a 403 doesn't lock
  out a legitimate retry for a month.
- Every response is written to disk **before** it is parsed, so a parsing
  bug can never lose data you already paid for.
- Failed calls are still recorded in `batchdata_search_runs`. A 403
  "Insufficient balance" consumed a request cycle; logging it is what stops
  a retry loop from quietly draining the wallet.

One non-obvious trap: in the search response, `results.meta.results.resultsFound`
is a **server-side count of matches**, while `resultCount` is how many
properties were actually transmitted. `resultsFound: 279621` does not mean
279,621 properties were downloaded — only 25 were. Don't confuse the two.

Also verified the hard way: the `county` and `state` fields inside
`searchCriteria` are **silently ignored**. A request for
`{county: "Montgomery", state: "MD"}` returned properties in OK, FL, CA, MI
and AK. The free-text `query` field (`"Maryland"`, `"Montgomery County, MD"`)
is the only geofence that actually works.

## Test-First Checklist

- [x] **Skip-trace and valuation response shapes** — verified live against
      a real lead (11132 Willowbrook Dr, Potomac, MD 20854) with
      `scripts/test_skip_trace.py` / `scripts/test_valuation.py`.
      `batchdata_client.py`'s parsing (in `cli.enrich_lead()`) matches the
      real shape; `estimate_arv_from_comps()` was removed — valuation
      returns a single AVM figure
      (`results.properties[0].valuation.estimatedValue`), not comps.
- [x] **Zpid-free Zillow slug** — verified: the search URL redirects
      straight to the correct listing (title/address/Zestimate all match).
- [x] Fund BatchData's $50 wallet minimum.
- [x] Confirm Gmail OAuth credentials/refresh token still valid.
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
4. Optionally set `BATCHDATA_MAX_LEADS_PER_RUN` and/or
   `BATCHDATA_MAX_ENRICHMENT_ATTEMPTS` to override their defaults (see
   "Cost control" above).
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
4. **`cli.enrich_lead(record, batchdata)`** — 2 BatchData calls per lead:
   `skip_trace()` for phone + its embedded per-phone DNC flag + person-level
   TCPA flag (the owner name comes from `property.owner.name`, not the
   top-level person, who's just whoever's reachable at the owner's mailing
   address), and `lookup_valuation()` for the **potential ARV**
   (`valuation.estimatedValue`), a fuller owner name, an embedded permit
   summary that feeds `condition.condition_tier()`, and a `quickLists`
   object of real distress/motivation booleans (`cli._distress_flags()`
   surfaces vacant/pre-foreclosure/tax-default/inherited/tired-landlord/
   equity-position ones in the digest when true) — no separate
   permits/DNC/TCPA calls needed (`batchdata_client.py`'s module docstring
   has the full reasoning). Note: `condition_tier()`/MD iMap's own
   `STRUGRAD` field is the only actual property-*condition* signal in this
   pipeline — BatchData's response has no condition/quality-grade field of
   its own for this property (checked directly against the raw response).
   MD iMap's own county-assessed value (`NFMTTLVL`) is shown as the lead's
   **current value**. From there:
   `condition.estimate_repair_cost()` turns the condition tier into a flat
   $/sqft rule-of-thumb **repair-cost estimate** (not a contractor quote —
   only produced for `likely-updated`/`likely-dated`, since `unassessed`
   carries no signal to even guess from), `flag_low_margin()` flags when
   current value already meets/exceeds ARV, and — once both ARV and a
   repair estimate exist — `max_allowable_offer()` computes the 70%-rule
   MAO. When the same response's `openLien.totalOpenLienBalance` is also
   present, `payoff_profit_estimate()` computes profit potential if the
   offer were just enough to cover the owner's existing debt (a distressed
   seller's real floor to avoid a deficiency at foreclosure) — a positive
   result marks the lead `high_priority`, which the digest calls out and
   sorts to the top, since it means there's profit even without asking the
   owner to accept a below-market discount. Also builds a Zillow search
   link (from `PREMCITY`, not `CITY` — MD iMap's two city fields can
   disagree; see `zillow.py`'s docstring for the confirmed real example).
   Degrades per-lead on any BatchData failure rather than failing the run.
   Also captures the owner's email (preferring one BatchData has actually
   tested/verified over an untested one) and a `corporate_or_trust_owned`
   flag from `quickLists.corporateOwned`/`trustOwned`. `enrich_lead()`
   takes an optional `cache` dict (`batchdata_cache.py`) — when given, a
   skip-trace/valuation call already cached for that `ACCTID` is reused
   instead of re-billing BatchData.
5. **`cli.filter_worth_pursuing(leads)`** — drops leads `cli._exclusion_reasons()`
   flags as not worth pursuing: low/negative margin (`low_margin` true, or
   a computed MAO `<= $0`), no phone *and* no email, or corporate/trust
   ownership. A dropped lead is still marked seen (see point 7) — it was
   already paid for, so it isn't re-enriched next run just because this
   run didn't send it. This is a digest filter, not a pre-enrichment one:
   margin and ownership are only knowable after the BatchData calls that
   already cost money.
6. **`cli.build_digest_body()` / `send_weekly_digest()`** — one digest
   email per run listing every lead that passed the filter above.
7. **`state.load_seen_parcels()` / `save_seen_parcels()`** — dedupe key is
   MD iMap's `ACCTID`.
8. **`archive.record_leads()` / `save_lead_archive()`** — writes every
   enriched lead's full data (Zillow link, owner name/phone/email,
   valuation, distress flags, condition) to a local-only `leads_archive.json`,
   keyed by `ACCTID`, regardless of whether it passed the filter in step 5.
9. **`archive.record_leads()` / `save_qualified_leads()`** — writes only
   the leads that passed the filter (i.e. were actually sent) to a second
   local-only file, `qualified_leads.json` — a standing record of "houses
   deemed profitable" independent of the digest email itself. Both files
   are covered by "Local lead archives" below.
10. **`batchdata_cache.py`** — every skip-trace/valuation response gets
    cached locally, keyed by `ACCTID`, alongside the exact MD iMap record
    it came from. `scripts/reprocess_from_cache.py` replays every cached
    lead through the *current* `enrich_lead()`/`filter_worth_pursuing()`
    logic with zero new BatchData calls — run it after changing what gets
    extracted or how a lead is judged, to backfill `leads_archive.json`/
    `qualified_leads.json` without re-paying for leads already fetched.

## Property catalog (Render Postgres)

Everything BatchData `property/search` returns is persisted to a Render
Postgres database so a paid call is made **once** and queried forever.

Set `DATABASE_URL` in `.env` (gitignored) to the Render *external*
connection string. On Render itself the two cron services pull it from the
`property_catalog` database automatically via `fromDatabase`.

### Schema

| Object | Purpose |
| --- | --- |
| `batchdata_search_runs` | One row per paid API call, including failures, with the full raw response. This is both the audit trail and the backing store for the 30-day cooldown. |
| `properties` | One row per distinct property we have paid for, plus the raw JSONB. |
| `leads` | Mutable human workflow state: score, status, phone, notes, call history. |
| `maryland_leads` | View: qualified MD leads joined to their property, best score first. |

Three deliberate design choices:

- **`properties` and `leads` are separate tables.** `properties` is what we
  bought; `leads` is what we did about it. Re-running the scorer refreshes
  every score column but never touches `status`, `notes`, `phone`, `email`,
  or `last_contacted_at`, so a fresh monthly pull cannot erase the record of
  a call that already happened.
- **`maryland_leads` is a VIEW, not a table.** "MD leads that fit our
  criteria" is a derived question, and a view can't drift out of sync with
  its source data the way a copied table would.
- **`upsert_property` refreshes `last_seen_at` but preserves
  `first_seen_at`.** How long a property has been sitting in the catalog is
  itself a signal: a preforeclosure that reappears month after month is an
  owner who still hasn't solved their problem.

### Monthly workflow

```bash
# 1. BILLABLE. Enforces a 30-day cooldown in the database; a second run
#    inside the window is a no-op, not a charge.
PYTHONPATH=src pipenv run python scripts/pull_maryland_leads.py

# 2. Free. Re-score the whole catalog. Safe to run any time you change
#    the weights in lead_scoring.py.
PYTHONPATH=src pipenv run python scripts/score_leads.py --state MD

# 3. Free. Email yourself the top 25 with per-lead call prep.
PYTHONPATH=src pipenv run python scripts/email_top_leads.py --limit 25
PYTHONPATH=src pipenv run python scripts/email_top_leads.py --dry-run   # preview
```

### Recording call outcomes

```bash
PYTHONPATH=src pipenv run python scripts/manage_leads.py list --limit 25
PYTHONPATH=src pipenv run python scripts/manage_leads.py show 12
PYTHONPATH=src pipenv run python scripts/manage_leads.py contact 12 --phone 240-555-0134
PYTHONPATH=src pipenv run python scripts/manage_leads.py status 12 called --contacted \
    --notes "Left voicemail, callback Tuesday"
PYTHONPATH=src pipenv run python scripts/manage_leads.py stats
```

This is not busywork. The scoring weights below are a *reasoned hypothesis*,
not a model fitted to conversion data, and `manage_leads.py stats` is the
only thing that will ever turn them into something empirical.

## Lead scoring — which 50 people to call

Ranking purely by profit is wrong, and it's the mistake this model exists to
avoid. **Profit is what a deal is worth *if* it closes; motivation is what
decides whether it closes at all.** An owner with $1.2M of equity and no
urgency will not sell at a wholesale price — they'll list it retail. So the
blended score is scaled by a motivation factor, which keeps a high-equity,
low-urgency lead from ever topping the call list.

Each property scores 0-100 on three axes, and the blend is then scaled by
two gating factors:

| Axis | Weight | What it measures |
| --- | --- | --- |
| Motivation | 0.50 | Distress stage blended with independent signals |
| Profit | 0.35 | Value minus full payoff, log-scaled |
| Contactability | 0.15 | How likely we are to reach the decision-maker |

| Gate | Effect | Why it multiplies instead of adding |
| --- | --- | --- |
| Motivation factor | 0.5x - 1.0x | A zero-urgency owner should sink, not average out |
| Recency factor | 0.5x - 1.0x | A 10-month-old filing has usually already resolved |
| Timing factor | 0.35x / 0.5x / 1.0x | Running out of clock invalidates the deal entirely |

### Counter-intuitive calls, and why

**Early foreclosure beats late.** The obvious model puts `activeAuction`
and `noticeOfSale` at the top. That is wrong twice over. Maryland gives only
10-30 days' notice of sale, which is not enough runway to skip-trace, reach
an owner, negotiate, and close an assignment, so by that stage the deal is
usually mechanically impossible rather than merely urgent. Arrears, trustee
and legal fees also compound into the payoff as the case advances, so late
stages are precisely where the equity we are scoring has already been eaten.
In a judicial state like Maryland, the lis pendens / Order to Docket is the
real entry point: the owner knows it is real, but there are still months of
runway. `noticeOfLisPendens` and `noticeOfDefault` now score highest.

**Tax default is treated as a stage, not a signal.** Tax debt is small
relative to value (thousands) while mortgage debt is large relative to value
(hundreds of thousands), so a tax-delinquent owner almost by construction
still has a constructible spread. It is a motivation signal that does not
simultaneously destroy the margin, which is exactly the failure mode of
late-stage foreclosure. It is noisier though, since some owners simply
forgot or are disputing the bill.

**`expiredListing` outranks `failedListing`.** These are not synonyms. A
failed listing was withdrawn *before* the contract expired; an expired one
ran its full term. Verified against our own catalog: the single
`expiredListing` property also carries `failedListing`, confirming expired
is a strict subset. A withdrawn-but-unexpired listing may still owe a broker
commission, and the owner may have decided not to sell at all. Neither flag
co-occurs with `activeListing`/`onMarket` here, so neither is currently on
the MLS -- `failedListing` just carries contract risk that `expiredListing`
does not.

**Bare `absenteeOwner` scores zero.** A content landlord is not a motivated
seller. Absentee ownership only earns its keep stacked with real distress,
which the other terms already capture.

### Timing is a feasibility gate, not a preference

BatchData ships real dates in `foreclosure`: `filingDate` is populated on
100% of our Maryland catalog and `auctionDate` on 87%. This matters more
than any weight, because **39 of our 48 Maryland properties have an auction
date that has already passed.** Those owners have most likely already lost
the house or resolved the case. Without this dimension the call list is
mostly dead leads sorted by equity.

One data-quality trap worth knowing: `auctionDate` is sometimes a stale
record carried from an older foreclosure. Our top-ranked lead was filed in
2026 but carried a 2014 `auctionDate`. Any `auctionDate` earlier than its
`filingDate` is discarded rather than trusted.

### Profit uses payoff, not loan balance

- **Profit is log-scaled.** Maryland spreads run from about $40k to $1.2M. A
  linear scale pinned every Bethesda and Annapolis lead at 100 and produced
  a nine-way tie. Log scaling also encodes a real effect: $50k to $150k of
  spread matters far more than $900k to $1M, because expensive houses have a
  much smaller cash-buyer pool and are harder to assign.
- **`totalOpenLienBalance` is the loan balance, not the payoff.** Missed
  payments, late fees, trustee costs and advanced taxes accrue on top and
  grow with the stage of the case, so we apply an arrears haircut that
  scales with stage. Involuntary liens (tax liens, judgments, mechanic's
  liens) live in a *separate* BatchData field and are added on top.
- **Qualification runs the 70% rule directly** (`payoff <= 0.70 x ARV -
  repairs - fee`) rather than thresholding equity percent, which is only a
  proxy for it. Equity percent is used solely as a fallback when lien data
  is missing.

Two honest caveats: `estimatedValue` is an *as-is* AVM but the 70% rule
wants ARV, so we approximate `ARV = value + repairs`; and repair cost is not
knowable from a search result, so a flat 15% is assumed. A property needing
a gut renovation will look better here than it is.

### What this produces

Of 48 Maryland preforeclosures, **7 qualify**. The other 41 break down as 15
failing the 70% rule, 8 with no spread at all, 9 corporate-owned, and 9
already listed. That roughly 15% qualify rate is the useful number for
planning: reaching ~50 callable leads needs about 340 catalogued properties,
i.e. roughly 14 more billable pages, not 159.

### On "1 in 50 calls"

Published funnels put 1 deal per 50 *dials* far outside even the optimistic
bound; a realistic range is 1 per 200-600 dials. **1 deal per 50 actual
conversations is achievable**, and that is the number worth targeting. The
distinction matters for expectations: dials-to-contact runs 10-15%, and
reported experience is that deals typically come from the third through
fifth touch, not the first. Scoring decides *who* to call and in what order;
it does not change the arithmetic of how many dials that takes.

**These weights are still not fitted to conversion data.** They are now
informed by external research and validated against this catalog's actual
data, but no deal has closed through this pipeline yet. Record outcomes with
`manage_leads.py` and revisit them once there is a real funnel to fit
against.

## Local lead archives

Three gitignored, local-only JSON files, all keyed by `ACCTID`:

- `leads_archive.json` (path overridable via `LEADS_ARCHIVE_PATH`) — every
  enriched lead, pursued or not, so losing the digest email doesn't mean
  re-paying BatchData to recover the data.
- `qualified_leads.json` (path overridable via `QUALIFIED_LEADS_PATH`) —
  only the leads that passed `filter_worth_pursuing()` and were sent,
  accumulated across every run.
- `batchdata_cache.json` (path overridable via `BATCHDATA_CACHE_PATH`) —
  the *raw* skip-trace/valuation responses plus the MD iMap record for
  every enriched lead, so `scripts/reprocess_from_cache.py` can re-derive
  the two files above after a logic change without new BatchData spend.
  Only covers leads enriched after this cache was introduced.

**None of these files is ever synced to GitHub**, unlike `state.json`'s
optional `ST_GITHUB_TOKEN` backing. All three contain real property owners'
names, phone numbers, and email addresses (`batchdata_cache.json` more so —
it's the complete raw response, including fields this pipeline doesn't even
use, like income/net-worth demographics) — and this repo is **public**.
Committing any of them would put third parties' contact info into
permanent, public git history.
If you need any of them to survive a host with an ephemeral disk (e.g.
Render Cron without an attached persistent disk) between runs, point the
relevant env var at a mounted persistent disk there — don't route any of
them through GitHub unless you first make this repo private.

## Releases

[`.github/workflows/release.yml`](.github/workflows/release.yml) runs
[python-semantic-release](https://python-semantic-release.readthedocs.io/)
only when a PR targeting `main` is actually merged — not on a direct push
to `main`. This is deliberate: `state.json`'s "Update seen parcels" commits
(and `leads_archive.json`-adjacent bookkeeping) are pushed straight to
`main` by the pipeline itself via the GitHub Contents API, and shouldn't
trigger a version bump. **A direct commit to `main` — including a manual
one — never releases; only a merged PR does.** It reads [Conventional Commits](https://www.conventionalcommits.org/)
— `feat:` -> minor bump, `fix:` -> patch bump, `BREAKING CHANGE:` (or
`feat!:`/`fix!:`) -> major bump, `chore:`/`docs:`/`refactor:`/etc. -> no
release — bumps `__version__` in
[`src/skip_tracer/__init__.py`](src/skip_tracer/__init__.py), updates
`CHANGELOG.md`, and creates the git tag + GitHub Release. Config lives in
[`pyproject.toml`](pyproject.toml); versions stay in the `0.x` range
(`allow_zero_version = true`) until a breaking-change commit or a manually
forced major release says otherwise.

**If you squash-merge PRs** (GitHub's default), the parser also reads each
individual commit line GitHub lists in the squash commit's body
(`commit_parser_options.parse_squash_commits`), not just the PR-title
summary line — but the PR title itself should still be a valid Conventional
Commit type/description, since that's what becomes the squash commit's
subject line.

## Known limitations (v1 POC)

- **Repair cost is a rule-of-thumb, not a real quote.**
  `condition.estimate_repair_cost()` applies a flat $/sqft figure by
  condition tier (`likely-updated` -> $12/sqft, `likely-dated` -> $40/sqft
  — see `condition.py`'s `REPAIR_COST_PER_SQFT`) — no inspection or
  contractor input exists anywhere in this pipeline. Treat the digest's
  "Est. repair cost" and the MAO derived from it as a rough starting point
  for triage, not a number to make an offer on directly. Tune the two
  dollar amounts once you have real rehab cost data from actual deals.
- **DC and two other markets (Tarrant County TX, Baltimore/Anne Arundel) are
  out of scope for v1** — tracked as GitHub issues (see below), not built
  here.
- **Street View + vision-model condition assessment is deferred to v2** —
  also tracked as a GitHub issue.

## Call playbook (domain knowledge from an experienced skip tracer)

This section captures field practice from an experienced Maryland
wholesaler/skip tracer, distilled into how the digest's `cli.call_script()`
builds a per-lead "Call prep" block and why a few pipeline decisions above
are made the way they are. It's a reference for the human making the call,
not something the pipeline acts on automatically (see "No automated
outreach" below).

- **PCMT**: every seller call boils down to four answers — **P**rice,
  **C**ondition, **M**otivation, **T**ime. Get those four and the call is
  done; you don't need the wording of a script beyond that. Each digest
  entry's "Call prep" block is organized around exactly these four.
- **Never anchor with your own number first.** Ask the seller what they
  need to walk away with, or what number they have in mind. Making an
  offer before hearing theirs gives away negotiating room for nothing.
- **The opener should match the distress signal, not be generic.**
  `call_script()` picks an opening angle from the lead's `distress_flags`/
  `motivation_signal`:
  - Foreclosure signals (`Notice of Sale`, `Notice of Default`,
    `Pre-Foreclosure`) → lead with timeline/certainty, not price.
  - `Tax Default` → ask whether back taxes are actually current now; a
    resolved-but-recent default often means a payment plan, a power of
    attorney, or an elderly owner with a caretaker — and repeat
    delinquency is common, so don't assume it's fixed for good.
  - `Inherited` (or a non-sale-transfer `motivation_signal`, e.g. probate/
    divorce transfers with `CONVEY1 == 4` and no `CONSIDR1`) → do **not**
    open with "we pay cash." Sellers of inherited property (often older
    heirs) tend to care more about avoiding capital gains and the hassle
    of probate than about cash speed.
  - `Tired Landlord` → lead with relief from tenant/maintenance hassle.
  - `Vacant` → ask what it's costing them to maintain/insure an empty
    house.
- **A seller with nowhere to go after closing isn't a workable deal**,
  regardless of margin — that's why `call_script()` always includes an
  explicit "confirm they have an exit plan" line. This is a
  disqualifier the human needs to catch live; it isn't derivable from
  any field in the digest.
- **Time-on-market is leverage.** A property that's sat 5+ months
  typically can't pass a lender's inspection as-is, which is exactly why
  it's still unsold — worth raising with the seller as a reason a fast,
  as-is close is actually in their interest, not just ours.
- **Don't trust free/USPS vacant-property lists.** They're only refreshed
  a few times a year and are frequently wrong (confirmed by driving past
  "vacant" addresses that clearly weren't). This is why this pipeline uses
  BatchData's `vacant` quickList flag — a paid, more current signal —
  instead of a free USPS-derived list.
- **Texting requires prior consent, always** (TCPA) — a lead's
  `do_not_call`/`tcpa_risk` fields (from BatchData's skip-trace response)
  drive the "call only, do not text" line in `call_script()`'s compliance
  reminder. Getting this wrong is a real, litigated liability, not just a
  compliance nicety.

## No automated outreach to owners (by design)

This pipeline emails a digest to *you* for manual review — it does not,
and should not without real legal review, text, call, or email property
owners automatically. `batchdata_client.py`'s `check_dnc()`/`check_tcpa()`
exist for that future workflow but aren't called by anything today.
Automated marketing texts/calls are high-risk under the **TCPA**
(statutory damages of $500-$1,500 per message, trebled if willful, and a
real history of class actions against skip-traced cold-texting), and
automated marketing email is lower-risk but still regulated under
**CAN-SPAM**. Some states also restrict soliciting distressed/pre-foreclosure
homeowners specifically — relevant here given the distress signals this
pipeline surfaces. None of this is legal advice; get a real TCPA/real-estate
attorney's sign-off before building outreach automation on top of this.

See the repo's GitHub issues for the full v2 backlog with context for
picking each one up cold.
