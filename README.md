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
        dashboards.yml          # dashboard provider (no dashboards yet)
    dashboards/                 # dashboard JSON goes here, later stories
  src/
    garmin_sync/                # Python sync service, built in later stories
  data/
    raw/                        # raw Garmin JSON landing zone, gitignored
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

## Secrets

- `.env` is gitignored. Never commit it.
- `.env.example` contains placeholder values only.
- The Garmin Personal API key goes in `.env` only, when the sync
  service is built. It is never printed in logs.
- Access tokens are short-lived (24 hours) and are minted by the sync
  service at run time. They are not stored in the repo.
- Before any commit, check: `git status` should never show `.env`,
  a token file, or anything under `data/raw/`.

## Roadmap

This repo is built story by story, in this order:

1. **Repo skeleton + local stack** - this README and structure. Done
   when InfluxDB and Grafana are up, Grafana connects to InfluxDB,
   and data survives a restart. No Garmin code yet.
2. **Garmin Personal API auth** - key to 24-hour token, token caching,
   secrets hygiene.
3. **First fetch** - daily summaries and activities, with raw landing
   in `data/raw/`.
4. **Data model + write to InfluxDB** - measurements, tags vs fields,
   UTC timestamps.
5. **Scheduled sync** - watermark, idempotency, retry and backoff.
6. **Grafana dashboards** - daily activity and trends.
7. **Heart rate, sleep, HRV views.**
8. **Error handling, rate limits, logging.**
9. **Runbook and deployment notes.**

No dashboard is expected to show data until stories 2-4 are done.
An empty Grafana at this stage is correct, not a bug.

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
- **Nothing in Grafana yet:** expected at this stage. The sync
  service does not exist yet.

## Licence / use

Personal use only. This project uses your own Garmin Personal API
key to read your own data. It is not a service for other users.
