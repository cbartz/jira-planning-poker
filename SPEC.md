# Jira Planning Poker — spec

A small internal web app for backlog grooming: the team estimates Jira tickets
labelled `grooming` with T-shirt sizes, and the facilitator writes the result
back to Jira in one click. Replaces planningpokeronline.com plus the manual
Jira update.

## Stack

- **Backend:** Flask, served by gunicorn (as provided by the 12-factor Flask profile).
- **Frontend:** server-rendered Jinja templates + **htmx** for partial updates.
  No JS build step, no SPA, no JSON API. Vendor `htmx.min.js` into `static/`
  (no CDN — internal deployment).
- **Real-time:** htmx polling (`hx-trigger="every 1s"`) of a room-state partial.
  No WebSockets.
- **State:** in memory, single process. Sessions are ephemeral; results live in Jira.
- **Deployment:** Juju 12-factor PaaS charm — `rockcraft init --profile flask-framework`
  for the rock, `charmcraft init --profile flask-framework` in `charm/`.

## Size mapping

| Size | Story points |
|------|--------------|
| XS   | 1            |
| S    | 2            |
| M    | 3            |
| L    | 5            |

Cards: `XS`, `S`, `M`, `L`, `?` (`?` = "can't estimate", never written to Jira).

## User flow

1. **Login:** single shared password → session cookie.
2. **Pick a name:** free text (trimmed, 1–32 chars, unique within the room). Stored in the session.
3. **Room:** one global room.
   - The first person to click "Facilitate" becomes facilitator; the facilitator can hand the role to another participant.
   - The ticket list is loaded from Jira using the configured JQL (see *Loading tickets* below).
   - The facilitator selects the current ticket. Everyone sees its key (linked to Jira), summary, and description as plain text.
   - Participants vote. Before the reveal, the room shows *who* has voted but not *what* they voted.
   - The facilitator reveals the votes. Everyone sees all votes plus the most common size.
   - The facilitator picks the final size (preselected: most common; on a tie, the larger size), then clicks **Save to Jira**. Alternatively they can start a **New round** (clears the votes) or **Skip** the ticket.
4. **Save to Jira** (facilitator only) on the issue, in one request:
   - set the story points field to the mapped value
   - add the size label (`XS`/`S`/`M`/`L`) and remove any *other* size label already present
   - remove the `grooming` label
   - then mark the ticket done in the room and advance to the next ticket
5. Participants who stop polling for more than 30s drop out of the participant list.

## Loading tickets

- **When:** the facilitator clicks **Load tickets**, which happens automatically the
  first time someone becomes facilitator. The server runs the configured JQL once and
  keeps a snapshot of the results in the room's memory.
- **Who calls Jira:** only the server, and only on **Load**, **Refresh**, and **Save**.
  Participant polling reads the in-memory room and never calls Jira, so ten people
  polling every second still means zero Jira calls.
- **What is stored per ticket:**
  - key and Jira link
  - summary, issue type, status, priority (name and icon)
  - parent epic (key and summary), if any
  - description as plain text
  - current labels and current story points, so re-grooming shows the existing estimate
- **Order:** highest priority first, then backlog rank within the same priority. The
  default JQL ends in `ORDER BY priority DESC, Rank ASC`. Priority order comes from the
  Jira site's priority scheme, so do not hardcode priority names. The facilitator can
  still pick any ticket out of order.
- **Size limit:** at most 200 tickets per load. Page through results with
  `nextPageToken` until the limit; if more match, show "showing 200 of N".
- **Epic filter (UI):**
  - A dropdown above the ticket list offers **All**, **No epic**, and each epic that
    has loaded tickets, with its key and summary and the count of pending tickets.
  - The options are built from the `parent` field of the loaded snapshot. This uses
    the hierarchy that is current in Jira Cloud, not the legacy "Epic Link" field.
    For subtasks, the load resolves the story or task's epic using the snapshot
    or a bulk parent lookup. Only Jira hierarchy-level 1 parents appear as epics.
  - Filtering happens **in memory on the server**. It never adds to the JQL, so there
    is no user input in JQL and no injection surface.
  - The selected epic is part of the room state, so everyone sees the same filtered
    list. Only the facilitator can change it.
  - Sorting by priority, then rank, still applies within the filter.
- **Refresh:** re-runs the JQL and merges the results:
  - new tickets are appended
  - tickets that no longer match are removed, except the one currently being voted on
  - tickets already saved in this session stay marked as done
- **States in the room:** each ticket is `pending`, `estimated`, or `skipped`. Estimated
  tickets drop out of the JQL on the next refresh anyway, because `grooming` was removed.
- **Errors:** if loading fails (bad JQL, 401, network), show Jira's message to the
  facilitator and keep the previous snapshot.

## Jira Cloud integration

- **Auth:** HTTP Basic with `email:api_token`, from the server only. The token never
  reaches the browser.
- **Token type:** use a **scoped** API token with the classic scopes `read:jira-work` and
  `write:jira-work`. Scoped tokens only work through the API gateway,
  `https://api.atlassian.com/ex/jira/<cloudId>`, not through `https://<site>.atlassian.net`
  (calls there return 401). That's why the API URL and the site URL are configured
  separately:
  - **API URL:** used for every REST call.
  - **Site URL:** used only to build the `/browse/<KEY>` links shown in the UI.
- **Classic (unscoped) tokens** also work: set the API URL to the site URL. The app treats
  the API URL as an opaque base and appends `/rest/api/3/...`, with no branching on token
  type. Scoped tokens are recommended because they are limited to read/write work.
- **Setup lookups.** The README must include these commands:
  ```bash
  # API URL for scoped tokens (cloudId lookup, no auth needed)
  echo "https://api.atlassian.com/ex/jira/$(curl -s https://<site>.atlassian.net/_edge/tenant_info | jq -r .cloudId)"

  # story points field ID ("Story Points" in company-managed, "Story point estimate" in team-managed projects)
  curl -s -u "$EMAIL:$TOKEN" "$API_URL/rest/api/3/field" \
    | jq -r '.[] | select(.name | test("story point"; "i")) | "\(.id)\t\(.name)"'

  # several matches are common (duplicates from team-managed projects or plugins):
  # pick the one that is editable on a real grooming ticket
  curl -s -u "$EMAIL:$TOKEN" "$API_URL/rest/api/3/issue/<PROJ-123>/editmeta" \
    | jq -r '.fields | to_entries[] | select(.value.name | test("story point"; "i")) | "\(.key)\t\(.value.name)"'
  ```
  If `editmeta` lists no story points field, the field is not on the project's edit
  screen, and saves will fail with 400 until a Jira admin adds it.
- **Avoid endpoints that need more scopes,** e.g. `/myself`, which needs `read:jira-user`.
- **Search:** `GET /rest/api/3/search/jql` with
  `fields=summary,description,labels,issuetype,status,priority,parent,<story-points-field>`. The old
  `/rest/api/3/search` endpoint is deprecated. Paginate with `nextPageToken`.
- **Description:** comes back as Atlassian Document Format (ADF). Convert it to
  plain text (walk the text nodes) and do **not** render Jira HTML, which avoids XSS.
- **Update:** `PUT /rest/api/3/issue/{key}`:
  ```json
  {
    "fields": {"customfield_10024": 3},
    "update": {"labels": [{"add": "M"}, {"remove": "XS"}, {"remove": "S"}, {"remove": "L"}, {"remove": "grooming"}]}
  }
  ```
- **Gotchas:**
  - The story points field must be on the issue's edit screen, or Jira returns 400. Show Jira's error message in the UI.
  - The field ID differs per site: production is `customfield_10024`; the sandbox will differ.
- **Errors:** surface Jira failures (401, 403, 400 with message) to the facilitator. Never log the token.

## Configuration (charm config → env)

Verified against paas-charm `main` (2026-11-16). Flask prefixes options with
`FLASK_`. A `type: secret` option produces one env var per key in the secret:
`FLASK_<OPTION>_<KEY>`.

| Option | Type | Default | Env var in the app |
|---|---|---|---|
| `jira-api-url` | string | — (scoped token: `https://api.atlassian.com/ex/jira/<cloudId>`; classic token: `https://<site>.atlassian.net`) | `FLASK_JIRA_API_URL` |
| `jira-site-url` | string | — (e.g. `https://example.atlassian.net`) | `FLASK_JIRA_SITE_URL` |
| `jira-email` | string | — | `FLASK_JIRA_EMAIL` |
| `jira-api-token` | **secret** (key `value`) | — | `FLASK_JIRA_API_TOKEN_VALUE` |
| `jira-story-points-field` | string | `customfield_10024` | `FLASK_JIRA_STORY_POINTS_FIELD` |
| `jira-jql` | string | `labels = grooming ORDER BY priority DESC, Rank ASC` | `FLASK_JIRA_JQL` |
| `app-password` | **secret** (key `value`) | — | `FLASK_APP_PASSWORD_VALUE` |

- **Reading config:** load everything with `app.config.from_prefixed_env()`. Without
  the prefix, the keys become `JIRA_API_URL`, `APP_PASSWORD_VALUE`, and so on.
- **Optional `paas-config.yaml` mapping:** this can rename env vars, e.g.
  `secret-env-vars: {value: JIRA_API_TOKEN}`. It isn't needed, so keep the defaults.
- **Flask secret key:** the charm generates `FLASK_SECRET_KEY` and stores it in a Juju
  secret, and Flask picks it up through `from_prefixed_env()`. The built-in
  `rotate-secret-key` action rotates it, which logs everyone out.
- **Built-in options to use:** `flask-session-cookie-secure=true`,
  `webserver-workers=1`, `webserver-threads=8`.
- **Secret changes:** the charm reconciles on `secret-changed`. Updating a Juju user
  secret restarts the app with the new value, without any custom Juju action. The
  charm explicitly maps `app-password` because PaaS reserves `app-` option names.

### Credential operations (document in README, no custom charm actions)

The app password and Jira token are Juju **user** secrets that the operator owns.
Viewing and rotating them uses plain `juju` commands:

```bash
# one-time setup
juju add-secret jira-planning-poker-password value="$(openssl rand -base64 18)"
juju grant-secret jira-planning-poker-password jira-planning-poker
juju config jira-planning-poker app-password=secret:<id>

# print the current password
juju show-secret jira-planning-poker-password --reveal

# rotate the password (the app restarts and picks it up)
juju update-secret jira-planning-poker-password value="$(openssl rand -base64 18)"

# rotate the Jira token (create the new token in Atlassian first)
juju update-secret jira-token value=<new token>
```

- **Local dev:** read the same `FLASK_*` env vars. A `.env` file is fine, but it must
  be gitignored.

## Security requirements

- **Login:**
  - Required for every route except `/healthz` and the static assets.
  - Compare passwords with `hmac.compare_digest`.
  - Rate-limit failed logins per client IP (e.g. 5 per minute; in memory is fine).
- **Cookies:** session cookie `HttpOnly`, `Secure` (configurable off for local HTTP dev), `SameSite=Lax`.
- **CSRF:** protect all POSTs, e.g. Flask-WTF `CSRFProtect`, with the token sent via htmx `hx-headers`.
- **Escaping:** keep Jinja autoescape on. Treat names and Jira text as untrusted.
- **Headers:** a strict CSP (`default-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.
- **Authorization:** only the facilitator can select a ticket, reveal, start a new round, skip, or save. Check this on the server, not only by hiding buttons.

## Deployment constraint (important)

All state is in memory, so the app must run as **one process**:

- one unit
- gunicorn with **1 worker** (use threads for concurrency)

Set this with `webserver-workers=1` (give it a default in `charmcraft.yaml`) and
`webserver-threads=8` for concurrency. Document both in the README.

## Testing

- **Unit tests** (pytest): size mapping, vote aggregation and tie-break, room state transitions, facilitator authorization, epic filter (All, No epic, a specific epic; priority order kept), refresh merge, ADF→text, login and rate limiting, and CSRF.
- **Jira client:** test against mocked HTTP (`responses`). Assert the exact request payloads.
- **Sandbox integration tests:** run against a real Jira Cloud sandbox and are skipped
  unless all of these env vars are set:

  | Env var | Example |
  |---|---|
  | `JIRA_SANDBOX_API_URL` | scoped token: `https://api.atlassian.com/ex/jira/<cloudId>`; classic token: `https://<sandbox>.atlassian.net` |
  | `JIRA_SANDBOX_SITE_URL` | `https://<sandbox>.atlassian.net` |
  | `JIRA_SANDBOX_EMAIL` | the token owner's account email |
  | `JIRA_SANDBOX_API_TOKEN` | the scoped token |
  | `JIRA_SANDBOX_STORY_POINTS_FIELD` | the sandbox's field ID, e.g. `customfield_10016` |
  | `JIRA_SANDBOX_PROJECT_KEY` | the project where throwaway issues are created |

  Each test creates a throwaway issue labelled `grooming`, runs search → save, asserts
  the fields and labels, and then deletes the issue. Never print or log the token.
- **Charm integration tests** (pytest + `jubilant`) on the existing Juju controller
  `ai-dev-controller` (cloud `microk8s/localhost`), in a temporary model:
  - Pack the rock with `rockcraft pack` and push it to the MicroK8s registry. Pack
    the charm with `charmcraft pack`.
  - Deploy, create and grant the secrets, and wait for `active`.
  - Assert that login works and that a wrong password is rejected.
  - Rotate the `app-password` secret, then assert that the new password works and the
    old one fails.
- **End-to-end:** run one full grooming round through the deployed app against the
  Jira sandbox (opt-in through the `JIRA_SANDBOX_*` env vars).

## Working agreement

- **Tests first:** write failing tests first, then implement.
- **Git:**
  - Work on a `feat/...` branch, never `main`.
  - Use conventional commits.
  - No AI attribution trailers in commits.
  - Never commit secrets.
- **Code:** keep it small. Flask app factory, a `jira.py` client, a `room.py` state model, templates, and no extra layers.

## Done when

- A full round works end to end on the sandbox: login → pick a name → vote → reveal → save.
- Story points, the size label, and the removed `grooming` label are visible in Jira.
- The charm deploys on MicroK8s with secrets set through Juju, and `juju status` is active.
- The README covers local dev, the sandbox tests, and deploy and secret setup (including rotating the Jira token), plus the setup lookups for the cloudId/API URL and the story points field ID.
