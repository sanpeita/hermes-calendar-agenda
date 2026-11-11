# Calendar Agenda

**Agent tools and dashboard API routes for today's Google Calendar agenda, plus per-event outcome tracking, for Hermes Agent.**

A calendar shows what was *planned*. It never records what actually *happened*.
Calendar Agenda closes that gap: it gives Hermes a read-only view of today's
agenda and a safe, explicit way to record each event's outcome — without ever
repurposing the event's title or its Google Calendar status.

Built and used daily in a real multi-agent setup where several Hermes profiles
share one calendar and a human reviews, every morning and evening, which
planned items were done, interrupted, cancelled, skipped — or deliberately
kept ("frozen") for later.

## What it does

- **Dashboard API routes** (`dashboard/plugin_api.py`) — `GET /today` returns
  today's events from the profile's primary Google Calendar, bounded by local
  midnight, each annotated with its recorded Hermes state. The routes are the
  backend; this plugin ships no Desktop pane or dashboard frontend of its own.
- **Two agent tools**:
  - `calendar_agenda_list_today` — read today's agenda (IDs, titles, times,
    state). It deliberately does **not** return event descriptions or
    locations.
  - `calendar_agenda_set_state` — record an event's outcome and slot
    retention as two independent axes (see below).
- **Two-axis state, stored in the event itself.** State lives in Google
  Calendar *private extended properties* (`hermesAgendaOutcome`,
  `hermesAgendaKeepSlot`, `hermesAgendaUpdatedAt`, `hermesAgendaOriginalEnd`) —
  no side database, and the visible title and the Google event status are
  never touched.
  - *Outcome*: `done` · `interrupted` · `cancelled` · `skipped`
  - *Keep slot*: `true` = the scheduled time block stays as-is ("frozen");
    `false` = the slot may be released (see safety rules).

## Safety rules for time changes

Releasing a slot can shorten an event's end to when the work actually
finished. This is guarded, in code, by all of the following:

- Only **timed, personal events clearly owned by the user, with no guests**,
  may have their end time changed. Events with guests, shared events,
  all-day events, and events not clearly owned are **never** retimed — only
  their private Hermes state is updated.
- Before shortening, the **exact original end is saved** in a private
  property. Marking the slot as kept later **restores the original end**
  exactly. Releasing a slot twice can never extend it.
- Writes are conditional (`If-Match` on the event etag), send no update
  notifications (`sendUpdates=none`), and are **verified by re-reading the
  event** — a state update that cannot be verified raises an error instead of
  claiming success.
- Failures (including a helper that tries to exit the process) are converted
  into ordinary tool/HTTP errors; the agent and the routes keep running.

## Authentication

This plugin has **no token file** and does not depend on another skill. It
declares its own Google OAuth client under `oauth:` in `plugin.yaml`, Hermes
performs the sign-in, and every Calendar call is made through
`credentialed_provider_request` with Hermes attaching the grant. Plugin code
never sees, refreshes or rewrites a token.

One-time setup, per Hermes profile:

1. In the Google Cloud console, create (or pick) a project and enable the
   **Google Calendar API**.
2. Configure the OAuth consent screen (External) and add your own account as a
   test user.
3. Credentials → Create credentials → **OAuth client ID** → *Desktop app* →
   copy the client ID and client secret.
4. Export them for the profile Hermes runs as:

   ```bash
   CALENDAR_AGENDA_GOOGLE_CLIENT_ID=...
   CALENDAR_AGENDA_GOOGLE_CLIENT_SECRET=...
   ```

5. Sign in from your own terminal (it opens a browser):

   ```bash
   hermes auth add calendar-agenda:google
   hermes auth status calendar-agenda:google
   ```

Requested scope: `https://www.googleapis.com/auth/calendar.events`. Requests go
to `www.googleapis.com` only. The grant is stored and refreshed by Hermes per
profile — two profiles can therefore point at two different Google accounts —
and is removed by `hermes auth logout calendar-agenda:google` or by uninstalling
the plugin.

The two variables are declared under `optional_env`, so the plugin still loads
before you sign in; the tools then answer with a "not signed in" error naming
the command above, instead of failing to load.

## Requirements

- Hermes Agent **>= 0.22.3** — the release in which `oauth:` manifest entries and
  `credentialed_provider_request` first ship together
- A Google Cloud OAuth client of your own (free)
- For the dashboard API routes: the Hermes dashboard runtime (FastAPI /
  Pydantic, provided by Hermes)
- No third-party Python dependencies of its own

## Install

From the plugin catalog (once listed):

```bash
hermes plugins install calendar-agenda
hermes plugins enable calendar-agenda
```

Manually: copy this directory into your profile's `plugins/` folder as
`calendar-agenda/`, then enable it for that profile.

## Configuration

- Calendar: the profile's `primary` Google Calendar.
- Timezone: currently fixed to **Asia/Tokyo** (`TIME_ZONE_NAME` in
  `calendar_agenda_service.py`). Change that one constant to use another
  timezone. Making it a proper setting is on the roadmap.

## Tools reference

### `calendar_agenda_list_today`
No parameters. Returns today's date, timezone, and events with `id`,
`summary`, `start`, `end`, `allDay`, the recorded `state`
(`result` / `keepSlot` / `updatedAt`), `hasAttendees`, and
`timeChangeAllowed`.

### `calendar_agenda_set_state`
| Parameter | Type | Notes |
| --- | --- | --- |
| `event_id` | string | Exact event ID from `calendar_agenda_list_today` (required) |
| `outcome` | enum | `done` / `interrupted` / `cancelled` / `skipped` (required) |
| `keep_slot` | boolean | `true` keeps the scheduled block; `false` releases it where safe (required) |
| `actual_end` | string | Optional ISO 8601 timestamp with timezone offset; defaults to now |

Returns the verified result, including whether the calendar end actually
changed (`calendarEndChanged`), the reason when it did not
(`timeChangeReason`), and whether an original end is saved
(`originalEndSaved`).

## Dashboard API

Mounted by Hermes under `/api/plugins/calendar-agenda/`:

- `GET /today` — today's agenda with state (503 with a generic message if
  Google Calendar is temporarily unavailable; internals are not leaked)
- `PATCH /events/{event_id}/state` — same state update as the tool
  (`{outcome, keep_slot, actual_end?}`)

## Disclosure

- **Network**: `https://www.googleapis.com` (Calendar v3) only, with the grant
  Hermes holds for `calendar-agenda:google`.
- **Reads**: today's events on the primary calendar. The agent-facing list
  tool exposes IDs, titles and times only — not descriptions or locations.
- **Writes**: private extended properties on events, plus guarded end-time
  shorten/restore on personal events as described above. Titles and Google
  event status are never modified; no notifications are sent to attendees.
- **No telemetry, no self-updating, no background processes.**

## Tests

23 offline unit tests (fake Calendar client; no network, no real account):

```bash
PYTHONPATH=. python -m unittest discover -s tests -p "test_*.py"
```

They cover: Tokyo-midnight windowing and pagination, shortening with exact
original-end save/restore, guest events never being retimed, the REST adapter
(URL and query building, `If-Match`, `sendUpdates=none`, HTTP error and timeout
mapping), a guard that the service never re-introduces a borrowed credential
(`google_token` / `google_api` / `build_service`), and error isolation (a
failing helper becomes a 503 / tool error, never a dead agent).

## License

MIT — see [LICENSE](LICENSE) for the full text. Copyright (c) 2026 sanpeita.
