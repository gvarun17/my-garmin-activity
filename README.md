# my-garmin-activity

Personal fitness dashboard: pull your own data from the official Garmin
Connect Personal API and visualise it in Grafana, backed by InfluxDB.

This is a personal, single-user project. It is deliberately separate from
the Fitness Tracking chat, which reads Garmin data via Apple Health.
Here the data comes direct from Garmin, not via Apple Health.

## Architecture

```text
Garmin watch
  -> Garmin Connect cloud
  -> Python sync service (scheduled, pull-only)
  -> InfluxDB (time series)
  -> Grafana (dashboards)
```

Why this shape, and what is deliberately not here:

- **Pull-only, not streaming.** The Personal API does not push to you
  the way the business Health API does. A scheduled poller is the
  correct pattern. There is no Kafka, MQTT, or webhook in this project.
- **Raw landing before curated data.** The sync service saves the raw
  Garmin JSON responses under `data/raw/` first, then transforms them
  into InfluxDB points. If the measurement design changes later, you
  can reprocess without re-pulling from Garmin.
- **Stateless, idempotent sync.** The sync job keeps a watermark of
  the last successful date. Re-running it must not duplicate points.
- **Store in UTC, display in IST.** Timestamps are stored in UTC in
  InfluxDB. Grafana handles local display, so daily boundaries do not
  drift.
- **Grafana is a read-only consumer.** Dashboards live in this repo
  under `grafana/dashboards/` (as code), so a dashboard is rebuildable
  and not just clicks in the UI.

## Data source: Garmin Personal API

- Official Garmin Connect Personal API, documented in the Personal API
  Swagger docs at
  https://developer.garmin.com/gc-developer-program/personal/swagger-docs/
- Your long-lived Personal API key is **not** used directly on data
  endpoints. It is exchanged for a short-lived access token.
- Access tokens are valid for 24 hours. There are no refresh tokens:
  the sync service simply mints a new token when it needs one.
- The Personal API is read-only and pull-only for a personal key.

**Never paste your Personal API key or any access token in this chat,
in an issue, or in this repo.** Keys live only in a local `.env` file
that is gitignored. See [Secrets](#secrets).

## Stack

| Layer | Choice | Version pinned in `docker-compose.yml` |
| --- | --- | --- |
| Time-series store | InfluxDB | `influxdb:2.7` |
| Dashboard | Grafana | `grafana/grafana:11.4.3` |
| Sync service | Python (to be built) | Python 3.12, stdlib + `requests` |
| Orchestration | Docker Compose | local only to start |

Versions are pinned on purpose, not `:latest`. A moving tag is not
reproducible, and InfluxDB `latest` can silently become v3, whose
setup differs from the v2 init variables used here.

Why InfluxDB 2.7 for v1, when the separate InfluxDB learning project
uses InfluxDB 3 Core: 2.7 has mature Grafana provisioning for
Flux/InfluxQL and avoids Grafana 12 + Flight SQL setup friction on
day one. The data model here (small daily/activity points) is portable,
so a move to InfluxDB 3 later is a migration story, not a rewrite.
Flux is deprecated in favour of SQL for InfluxDB 3.x, which is another
reason not to build new Flux dashboards if that migration happens.

## Project structure

```text
my-garmin-activity/
  README.md
  .env.example                  # placeholder names only, no real values
  .gitignore
  docker-compose.yml            # InfluxDB + Grafana, pinned versions
  grafana/
    provisioning/
      datasources/
        influxdb.yml            # InfluxDB datasource, UID: influxdb
      dashboards/
        dashboards.yml          # dashboard provider
    dashboards/
      fitness-overview.json     # steps, distance, resting HR, activities
      sleep-hrv.json            # sleep duration/score, HRV vs baseline
  src/
    garmin_sync/
      auth.py                   # Story 2: Personal API token mint/cache
      endpoints.py              # Story 3: endpoint paths, limits, chunking
      client.py                 # Story 3: fetch, chunking, 500-split, retry
      models.py                 # Story 4: Garmin rows -> InfluxDB points
      influx.py                 # Story 4: InfluxDB line-protocol writer
      state.py                  # Story 5: per-kind watermark state
      sync.py                   # Story 5: fetch -> raw -> write -> watermark
      __main__.py               # CLI: python -m garmin_sync
  tests/
    test_auth.py                # Story 2 tests, all Garmin calls mocked
    test_client_models.py       # Story 3-4 tests, all Garmin calls mocked
    test_influx_sync.py         # Story 4-5 tests, watermark behaviour
  requirements.txt              # requests
  Dockerfile                    # one-shot sync image, Python 3.12-slim
  data/
    raw/                        # raw Garmin JSON landing zone, gitignored
    sync-state.json             # per-kind watermark, gitignored
```

## Quick start

### 1. Create your local environment file

```bash
cp .env.example .env
```

Then edit `.env` and fill in your own values. Do not commit `.env`.
See `.env.example` for the exact variable names.

For InfluxDB, the first `docker compose up` uses the init variables
in `.env` to create the initial user, organisation, bucket, and admin
token. If you change those values later, they do not retroactively
change an already-initialised volume. To re-initialise, stop the
stack and remove the named volumes (see Troubleshooting).

### 2. Start the stack

```bash
docker compose up -d
docker compose ps
docker compose logs -f influxdb grafana
```

### 3. Check both services

- InfluxDB: http://localhost:8086
- Grafana: http://localhost:3000

Log in to Grafana with the admin user and password from `.env`.
The InfluxDB datasource is provisioned with UID `influxdb`. Open
Connections > Data sources, select it, and use Save & test. It should
connect even though there is no fitness data yet, because no sync
service has run yet.

### 4. Stop the stack

```bash
docker compose down
```

Data survives `down` because it lives in named Docker volumes
(`influxdb-data`, `grafana-data`), not in the containers. Use
`docker compose down -v` only if you intentionally want to wipe all
local data and re-initialise InfluxDB from scratch.

## Story 2: authentication

Implemented in `src/garmin_sync/auth.py`. No data is fetched yet.

- Reads `GARMIN_API_KEY` from the environment, after loading `.env`
  if present. A placeholder value is rejected, without echoing it.
- Exchanges the key at the Personal API token endpoint, using an
  `Authorization: ApiKey` header. The response is the raw JWT body,
  not JSON, so it is read as text and its `exp` claim is decoded.
- Caches the token in `.garmin-token.json` (gitignored, mode 0600).
  A cached token is reused while it has more than 1 hour left
  (`GARMIN_TOKEN_REFRESH_THRESHOLD_SECONDS`, default 3600).
  Otherwise a new token is minted. There are no refresh tokens.
- Data endpoints in later stories will use the token as a `Bearer`
  token, via `auth_headers()`. Do not use the `ApiKey` header there.

### Test auth on your machine

From the repo root, after filling in `.env`:

```bash
pip install -r requirements.txt
PYTHONPATH=src python3 -m garmin_sync.auth
```

Expected output is only a source (cache or minted) and an expiry
time, in UTC and local time. It never prints token characters.
Use `--force` to mint a new token even if a usable one is cached.

### Test auth without a real key

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

All tests use mocked HTTP calls. No network call and no real key
are involved. Request failures are wrapped into a readable
`AuthError` instead of a raw requests traceback, with a hint to
switch off macOS system Python 3.9 / LibreSSL if that is the cause.

## Stories 3-9: fetch, store, sync, dashboards, runbook

### Fetch (Story 3)

`src/garmin_sync/client.py` fetches, in 28-day chunks by default:
- daily summaries: `/usersummary-service/v2/dailysummaries`
- sleep: `/sleep-service/sleep/dailySleepSummariesByDate`
- HRV: `/hrv-service/hrv/daily/{start}/{end}`
- activities: `/activitylist-service/activities/search/activities`,
  paged, in 30-day windows

Defensive rules, from community reports of the Personal API:
- Empty ranges answer 204 with no body: treated as empty.
- Activity search fails with HTTP 500 once a window holds roughly
  50+ activities: the window is split in half, down to a single day.
- All response fields are optional. Rows without a usable date are
  skipped, never allowed to fail a whole sync.
- Requests are spaced 0.8s apart, one at a time, with retries for
  429 and 5xx honouring `Retry-After`.

Endpoint paths and parameter names are centralised in
`src/garmin_sync/endpoints.py`. Garmin's Swagger page does not
expose a downloadable spec, so if a range call returns 400, check
that endpoint in Swagger and adjust only that file.

### Data model (Story 4)

Measurements in InfluxDB bucket `fitness`:
- `daily_summary`: steps, distance_m, calories, resting/min/max HR,
  stress_avg, body battery charged/drained/latest/end-of-day
- `activity`: distance_m, duration_s, calories, avg/max HR, with
  activity_id and activity_name as fields, activity_type as a tag
- `sleep`: duration_s, score, deep/light/REM seconds and percentages,
  awake_count, sleep_qualifier as a tag
- `hrv`: last_night_avg, weekly_avg, baseline_low/upper, hrv_status
  as a tag (status is Garmin's reading of weeklyAvg vs baseline,
  not last night alone)

Times are UTC in InfluxDB. Daily rows use midnight UTC on their
calendar date. Activities use `startTimeLocal` interpreted in
`GARMIN_TIMEZONE` (default Asia/Kolkata), then converted to UTC.
Missing fields are omitted, never written as zero.

### Scheduled sync (Story 5)

```bash
# Local, with a modern Python venv (not system Python 3.9):
PYTHONPATH=src python -m garmin_sync --days 7
PYTHONPATH=src python -m garmin_sync --days 30 --dry-run
PYTHONPATH=src python -m garmin_sync --start 2026-10-01 --end 2026-10-28 --kinds daily,sleep

# Docker, one-shot (stack must already be up):
docker compose --profile sync run --rm sync --days 7
```

State is one watermark date per kind in `data/sync-state.json`.
It advances only after that kind's points are written, so a failed
kind retries from its old watermark. A dry run writes nothing and
advances nothing. By default the window ends yesterday, because
today is incomplete in Garmin; use `--include-today` to override.
Re-running a window is safe: raw files for the same window are
overwritten, and InfluxDB points for the same time/measurement/tags
overwrite rather than duplicate in practice for daily rows.

Schedule it, for example every 6 hours, from cron/launchd on your
machine, or from a scheduled job that runs the Docker one-shot
command. The container itself does not loop.

### Dashboards (Stories 6-7)

Provisioned automatically from `grafana/dashboards/`:
- **Fitness Overview**: steps by day, resting HR trend, distance
  by day, activities table, Body Battery end of day, stress average
- **Sleep and HRV**: sleep duration in hours, sleep score, HRV
  last night vs weekly average, HRV baseline band

Open Grafana, pick a time range that covers your synced window,
and check the dashboards. If a panel is empty, first check that
its measurement has points in InfluxDB for that range; empty
Grafana before the first sync is expected.

### Error handling, rate limits, logging (Story 8)

- Auth failures never print the key or token, and token-endpoint
  failures are wrapped into a short `AuthError`.
- Garmin 401 means the token is invalid/expired; the next run mints
  a new one. Garmin 404 for an endpoint means check Swagger and
  `endpoints.py`. Garmin 429/5xx are retried up to 3 times with
  backoff and `Retry-After`.
- Sync logs go to stdout in a standard format, with per-kind counts
  and failed kinds. The CLI exits non-zero if any kind failed, so a
  scheduler can detect it, while successful kinds keep their progress.
- No `verify=False`, no insecure TLS workarounds anywhere.

### Runbook (Story 9)

Daily operation is: stack up, sync runs, dashboards read. In detail:
1. `docker compose up -d` (InfluxDB + Grafana).
2. Run a sync (local venv or Docker one-shot, above).
3. Check the sync log for fetched/written/skipped/failed counts.
4. Check Grafana dashboards for the synced range.
5. If a kind failed, fix the cause and re-run: its watermark has
   not advanced, so it will retry the same window.

Backups: InfluxDB data lives in the `influxdb-data` Docker volume.
Raw Garmin JSON under `data/raw/` is a second, re-creatable copy
of what was fetched, but it is gitignored and lives only on the
machine that ran the sync, so back up `data/` if you care about it.

## Out of scope, by Personal API design

Not in this project, because the Personal API does not provide them
or does not allow them:
- VO2 max, training readiness, training status, training load,
  race predictions (Garmin removes or does not expose these via
  the Personal API; they remain in Connect/manual FIT downloads)
- Per-lap or per-second splits from JSON endpoints (those need the
  FIT file download, a separate story if ever wanted)
- Writing anything back to Garmin or the watch (Personal API is
  read-only and pull-only)


## Secrets

- `.env` is gitignored. Never commit it.
- `.env.example` contains placeholder values only.
- The Garmin Personal API key goes in `.env` only, when the sync
  service is built. It is never printed in logs.
- Access tokens are short-lived (24 hours) and are minted by the sync
  service at run time. They are not stored in the repo.
- Before any commit, check: `git status` should never show `.env`,
  a token file, or anything under `data/raw/`.

## Roadmap: all stories built

1. **Repo skeleton + local stack** - done, verified by Varun:
   InfluxDB and Grafana are up.
2. **Garmin Personal API auth** - done in code, plus the macOS
   system-Python SSL diagnosis and readable-error patch. Still to
   be proven on a real key in a modern-Python venv.
3. **First fetch** - done: daily, activities, sleep, HRV, raw landing.
4. **Data model + write to InfluxDB** - done.
5. **Scheduled sync** - done: watermark, dry-run, one-shot Docker.
6. **Grafana dashboards** - done: Fitness Overview.
7. **Heart rate, sleep, HRV views** - done: Sleep and HRV dashboard,
   plus resting HR in Fitness Overview.
8. **Error handling, rate limits, logging** - done.
9. **Runbook and deployment notes** - done, in this README.

What is still to be proven on real data, not mocked tests:
auth on a real key in a modern-Python venv, a first real sync,
and dashboards populated for a synced range. Endpoint parameter
names for range calls should be confirmed against Swagger if
Garmin returns 400 (see endpoints.py note).

## Troubleshooting

- **Grafana datasource test fails:** check InfluxDB is healthy
  (`docker compose ps`), that the bucket and token in `.env` match
  the initialised InfluxDB, and that you have not changed `.env`
  after first init without wiping volumes.
- **InfluxDB will not initialise:** a volume from an earlier run
  with different init values will win over new `.env` values. Stop
  and use `docker compose down -v` to re-initialise, knowing this
  wipes local data.
- **Port already in use (8086 or 3000):** another InfluxDB or Grafana
  instance (for example from another project) is already bound to
  that port. Stop it, or change the host port mapping in
  `docker-compose.yml` for this project only.
- **Nothing in Grafana yet:** check first that a sync has actually
  run and written points for that time range. Before the first sync,
  empty Grafana is expected.
- **SSL / TLS error on macOS:** if you see
  `TLSV1_ALERT_PROTOCOL_VERSION` with LibreSSL 2.8.3, you are on
  macOS system Python 3.9. Switch to Python 3.12+ with OpenSSL 3
  in a venv (see Story 2 / auth error hint). Do not use
  `verify=False`.
- **Garmin 400 on a range call:** check that endpoint's parameters
  in the Personal API Swagger docs and adjust only
  `src/garmin_sync/endpoints.py`.
- **Garmin 500 on activities:** this is the known ~50-activity
  window failure. The client already splits the window down to a
  single day; if it still fails on a single day, check that day's
  raw file and the sync log for the exact error.

## Licence / use

Personal use only. This project uses your own Garmin Personal API
key to read your own data. It is not a service for other users.
