# CHANGELOG

<!-- version list -->

## v0.7.0 (2026-09-18)

### Features

- Payoff-profit prioritization and a BatchData response cache
  ([#9](https://github.com/jpdeburgo/skip-tracer/pull/9),
  [`64caf6e`](https://github.com/jpdeburgo/skip-tracer/commit/64caf6e574ebe76d05513c3b05e40dd0d4fd8f8a))


## v0.6.0 (2026-09-18)

### Features

- Release only on PR merge; qualified-leads archive; clearer valuation labels
  ([#8](https://github.com/jpdeburgo/skip-tracer/pull/8),
  [`79baae5`](https://github.com/jpdeburgo/skip-tracer/commit/79baae580b7974e4d1a0b82613e3b4e28317bb4a))


## v0.5.0 (2026-09-17)

### Features

- BATCHDATA_MAX_LEADS_PER_RUN is now a target match count, not a scan cap
  ([`90bd1e0`](https://github.com/jpdeburgo/skip-tracer/commit/90bd1e01666e54f60dc5b45c8d308a66a2f72617))


## v0.4.0 (2026-09-14)

### Chores

- Clear seen-parcel state
  ([`a3950c9`](https://github.com/jpdeburgo/skip-tracer/commit/a3950c9950d4413ae8a237772fbde6a7fa3c8a97))

### Features

- Add local-only full-lead archive
  ([`31414ff`](https://github.com/jpdeburgo/skip-tracer/commit/31414ff72292d9065d0c56f63dbc57b7619cd195))


## v0.3.0 (2026-09-14)

### Features

- Filter out leads not worth pursuing; capture owner email
  ([`77a3833`](https://github.com/jpdeburgo/skip-tracer/commit/77a3833a68146ae560a3d9c33589c5275620a2a3))


## v0.2.0 (2026-09-14)

### Features

- Surface BatchData's distress flags; fix valuation request shape
  ([`bcf3d03`](https://github.com/jpdeburgo/skip-tracer/commit/bcf3d03124f1fbe8bf7d9eef38f6e221740b01dc))


## v0.1.0 (2026-09-14)

### Features

- Add current value, repair cost estimate, and ARV to the digest
  ([`66d654a`](https://github.com/jpdeburgo/skip-tracer/commit/66d654ab1c51269841b10a06913c0a4df7934db5))


## v0.0.2 (2026-09-14)

### Bug Fixes

- NER pipeline kwarg and unbounded entity classification scan
  ([`6a9757e`](https://github.com/jpdeburgo/skip-tracer/commit/6a9757ebf63d09ffc2ba8174387f4eaa6bd08976))


## v0.0.1 (2026-09-14)

### Bug Fixes

- Correct BatchData response parsing against a real verified lead
  ([`232a2ee`](https://github.com/jpdeburgo/skip-tracer/commit/232a2ee364bac847741470aee05363359b1ffd0a))

### Continuous Integration

- Run the pytest suite on push and pull request
  ([`ece44a2`](https://github.com/jpdeburgo/skip-tracer/commit/ece44a20f3f39188908ff1aa5a44e3d068aba2fc))


## v0.0.0 (2026-09-13)

- Initial Release
