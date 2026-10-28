"""Garmin Personal API endpoint definitions.

Base host is personal.connectapi.garmin.com, per Garmin's Personal API
documentation and community reports. Paths below are the service
paths reported for the Personal API.

One honest caveat, kept in one place on purpose: Garmin's Swagger
page renders in a browser and does not expose a downloadable spec,
so query-parameter names for the range endpoints are the ones
commonly reported for these services. If Garmin returns 400 for a
range call, check that endpoint in the Swagger docs for your account
and adjust ONLY the params in this file. All fetch code uses these
constants, so a param rename is a one-file fix.

Limits applied here, from community reports of the spec:
- sleep and body-battery: 31 days per request (we chunk at 28)
- stress: 28 days per request
- daily summaries: up to a year per request (we still chunk at 28,
  which is safe and keeps every range call uniform)
- activity search: fails with HTTP 500 once a window holds roughly
  50+ activities, so we walk 30-day windows and split on 500 down
  to a single day (see client.py)
- empty ranges answer 204 with no body, not an empty array
"""

BASE_URL = "https://personal.connectapi.garmin.com"

# Path templates. {start} and {end} are YYYY-MM-DD dates.
DAILY_SUMMARIES_PATH = "/usersummary-service/v2/dailysummaries"
SLEEP_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"
HRV_PATH = "/hrv-service/hrv/daily/{start}/{end}"
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"

CHUNK_DAYS = 28
ACTIVITY_WINDOW_DAYS = 30
ACTIVITY_PAGE_SIZE = 100

# Polite spacing between Garmin requests. Community reports show
# 0.8s spacing, one request at a time, never produced a 429 over
# several hundred requests. We default to 0.8s and still honour
# 429 / Retry-After when Garmin does rate-limit.
REQUEST_DELAY_SECONDS = 0.8
