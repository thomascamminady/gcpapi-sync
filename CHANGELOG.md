# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are `0.MINOR.YYYYMMDD`: `MINOR` is bumped when this package changes, and the
date records when the current state of Garmin's API was picked up (it changes only
when Garmin changes something). `scripts/bump_version.py` computes the next version.

## [0.1.20261022] - 2026-10-22

### Added

- `GarminClient` and `AsyncGarminClient` covering every endpoint of the Garmin
  Connect Personal API, with identical signatures.
- 109 pydantic response models generated from Garmin's OpenAPI document, corrected
  where the live API disagrees with it, with typed time-series accessors.
- API key discovery, token exchange, in-memory and file token caches (compatible
  with Garmin's official client), renewal before expiry and after rejection.
- Retries with exponential backoff, jitter and `Retry-After`; a typed exception
  hierarchy.
- Automatic splitting of long date ranges and paging of activity searches.
- The `gcpapi` command line tool: `setup`, `status`, `logout`, `today`, `activities`,
  `sync`, `export`, and a JSON command for every client method.
- Weekly detection of changes to Garmin's API.
