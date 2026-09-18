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
