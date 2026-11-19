# Grooming Poker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a team estimate Jira backlog tickets together and write each final estimate to Jira in one click.

**Architecture:** A Flask factory composes a thread-safe in-memory room, a small server-only Jira client, and Jinja templates. htmx polls local state only; Juju runs one Gunicorn worker in one unit so every user shares the same room. A database or SPA would add deployment and synchronization costs without making Jira a better source of truth for saved estimates.

**Tech Stack:** Python 3.12, Flask, Flask-WTF, requests, htmx, Gunicorn, pytest, responses, Jubilant, Rockcraft, Charmcraft, MicroK8s.

**Spec:** [`SPEC.md`](../../../SPEC.md), including the approved single-process design.

## Global Constraints

- No JS build step, no SPA, no JSON API, no CDN, and no WebSockets; poll `every 1s`.
- One unit, `webserver-workers=1`, `webserver-threads=8`, `flask-session-cookie-secure=true` in deployment.
- Default JQL: `labels = grooming ORDER BY priority DESC, Rank ASC`; default field: `customfield_10024`.
- Only Load, Refresh, and Save contact Jira; no browser-sourced JQL; credentials never reach the browser or logs.
- Failing tests first; work on `feat/grooming-poker`; conventional commits without secrets or attribution trailers.

## Review Focus

1. A delayed or double-clicked Save cannot write an estimate to a different round or issue.
2. Jira rejecting a save preserves the current ticket, revealed votes, and ability to retry.
3. An expired facilitator releases ownership; a new session reusing the name does not inherit ownership or votes.
4. An all-`?` round reveals votes without preselecting a writable size.
5. Enhanced search can report more results without a `total`; a failed count must not hide a successful ticket load.

---

### Task 1: Room state

**Files:** `room.py`, `tests/test_room.py`, `requirements-dev.txt`, `.gitignore`.

**Produces:** `SIZE_POINTS`, `CARDS`, `suggest_size(votes: Iterable[str]) -> str | None`, `Ticket`, and `Room(clock=time.monotonic)`. `Ticket` carries key, URL, summary, plain description, issue type, status, priority name/icon, epic key/summary, labels, and points. `Room` owns membership by opaque session ID, facilitator ID, current ticket key, round revision, votes, filtering, and ticket statuses. Methods: `join(participant_id, name)`, `touch(participant_id)`, `claim(participant_id)`, `handoff(participant_id, recipient_id)`, `merge(tickets, total, truncated)`, `set_filter(epic)`, `select(key)`, `vote(participant_id, size, revision)`, `reveal(revision)`, `restart(revision)`, `finish(revision, status, size=None)`, and `visible_tickets()`.

- [ ] Write `test_size_mapping_and_ties`: assert `SIZE_POINTS == {'XS': 1, 'S': 2, 'M': 3, 'L': 5}`, `suggest_size(['M', 'L', '?']) == 'L'`, `suggest_size(['?', '?']) is None`.
- [ ] Write `test_expiration_and_handoff`: at 30 seconds the participant remains; after 30 seconds an idle facilitator expires, its vote disappears, and the same name can rejoin without inheriting the role. Assert only the current holder can transfer to a live member.
- [ ] Write `test_round_transitions`: assert a new round clears votes, stale revisions are rejected, and finishing marks the ticket `estimated` or `skipped` and selects the next pending ticket in the filtered list.
- [ ] Write `test_filter_and_refresh`: assert All/No epic/epic subsets preserve Jira order; after refresh assert the current missing ticket is retained, a different missing pending ticket is removed, an estimated ticket stays estimated, and a newly returned ticket is visible in the refreshed Jira order.
- [ ] Run `python -m pytest -q tests/test_room.py`; expect failure because `room` is missing.
- [ ] Implement the interfaces above. Preserve Jira's response order for matched tickets instead of inventing a priority list. Keep the active ticket visible if it falls out of the search and remember terminal statuses by key even if those issues fall out and later return. Do not automatically advance outside the shared filter.
- [ ] Rerun the room tests; expect all pass. Commit `feat(room): model grooming rounds and ticket state`.

### Task 2: Jira Cloud client

**Files:** `jira.py`, `tests/test_jira.py`, `requirements.txt`.

**Consumes:** `Ticket` and `SIZE_POINTS` from Task 1.

**Produces:** `adf_to_text(document) -> str`, `JiraError`, and `JiraClient(api_url, site_url, email, token, story_points_field, jql)` with `search() -> tuple[list[Ticket], int | None, bool]` and `save(ticket: Ticket, size: str) -> None`.

- [ ] Write `test_adf_text`: adjacent marked text stays adjacent, paragraph and hard-break boundaries become newlines, and missing descriptions produce `''`.
- [ ] Write `test_enhanced_search_paginates`: assert exact Basic auth, two request query dictionaries including `fields=summary,description,labels,issuetype,status,priority,parent,customfield_10024` and the second-page token; assert site browse URLs, parent summary, priority icon, labels, and points are parsed.
- [ ] Write `test_over_limit`: with an outgoing token after 200 tickets and no search `total`, assert search returns exactly 200, tries `POST /rest/api/3/search/approximate-count` with `{'jql': configured_jql}` once, and yields `total=None, truncated=True` if counting fails. Label an approximate total as such in the UI.
- [ ] Write `test_save_payload`: for `['S', 'grooming', 'keep']`, assert exactly one `PUT` with `{'fields': {'customfield_10024': 3}, 'update': {'labels': [{'add': 'M'}, {'remove': 'S'}, {'remove': 'grooming'}]}}`. Assert `?` triggers no request.
- [ ] Write `test_jira_errors`: parameterize 400/401/403/timeouts; assert status and actionable Jira text when available and no credentials in error strings, including if Jira echoes a credential.
- [ ] Run `python -m pytest -q tests/test_jira.py`; expect failure because `jira` is missing.
- [ ] Implement the client with bounded request timeouts and no retries for writes. Follow tokens until 200 tickets; never retain more. Use only a best-effort count when over limit; retain the valid search result if the count is unavailable.
- [ ] Rerun client tests; expect all pass. Commit `feat(jira): load and update grooming tickets`.

### Task 3: Flask and htmx room

**Files:** `app.py`, `templates/{base,login,name,room,_state}.html`, `static/{htmx.min.js,htmx.LICENSE,style.css}`, `tests/test_app.py`, `requirements.txt`.

**Consumes:** `Room`, `JiraClient`, and `JiraError`. **Produces:** `create_app(config=None, *, jira=None) -> Flask`, `GET /healthz`, `GET|POST /login`, `GET|POST /name`, `GET /`, `GET /room/state`, `POST /room/<action>`.

- [ ] Write `test_login_and_rate_limit` and `test_csrf_and_headers`: assert unauthenticated room requests redirect to login; wrong password fails and the sixth attempt within a minute returns 429; a missing CSRF token rejects POST; a valid form or htmx header works; assert cookie and strict CSP, frame, and referrer headers.
- [ ] Write `test_room_flow_and_authorization`: use two Flask clients; assert first claim loads tickets exactly once; polls never call Jira, names and Jira descriptions are escaped, votes are hidden until reveal, attendee control POSTs return 403, ties select the larger size, and a successful save advances. Assert the facilitator's epic filter appears in both clients.
- [ ] Write `test_failed_save_can_retry`, `test_failed_refresh_keeps_snapshot`, and `test_stale_save`: assert the failed operation's Jira message is visible only to the facilitator, the current result/queue survives, and a repeated stale Save causes no second Jira request.
- [ ] Run `python -m pytest -q tests/test_app.py`; expect failure because `app` is missing.
- [ ] Implement the routes, lock room access, and guard Jira mutations with the current round revision. Keep user form inputs outside the regularly replaced participant state or preserve them while a poll is in flight; prevent out-of-order poll responses from overwriting newer action results. Use the same forms for normal POSTs and htmx, with `CSRFProtect` and literal JSON `hx-headers`. Disable htmx eval and inline styles/scripts; vendor a pinned release and its license.
- [ ] Run `python -m pytest -q`; expect all local tests pass. Commit `feat(web): serve the shared grooming room`.

### Task 4: Sandbox and deployment verification

**Files:** `rockcraft.yaml`, `charm/charmcraft.yaml`, `tests/integration/{conftest,test_sandbox,test_charm}.py`, `pytest.ini`, `README.md`, `requirements-dev.txt`.

**Consumes:** Flask profile, `create_app`, Jira client, and the six sandbox variables from the spec; `test_charm.py` also needs an explicit Juju opt-in.

- [ ] Write `test_sandbox_search_save`: use a unique throwaway Jira issue with `grooming` and `S`; wait for search indexing, save `M`, then independently fetch the issue and assert points `3`, label `M`, no `S` or `grooming`. Delete in fixture cleanup. Skip when any sandbox variable is missing.
- [ ] Write `test_charm_login_and_rotation` and `test_deployed_grooming_round`: pack and deploy in a Jubilant temporary model on `ai-dev-controller`, use granted secrets, wait for `active`, assert login rejection and acceptance before and after password rotation; drive login/name/facilitate/vote/reveal/save through the deployed Flask app and assert actual Jira values. Limit deployed test JQL to the throwaway issue. Require explicit Juju opt-in for both tests and sandbox credentials only for the deployed grooming round.
- [ ] With credentials present, run `python -m pytest -q tests/integration/test_sandbox.py`; confirm test failures reflect missing integration wiring or any real Jira mismatch, not exposure of credentials. Run Juju tests with opt-in before deployment files exist; expect missing package configuration.
- [ ] Generate the Rockcraft and Charmcraft Flask profiles and set spec config types/defaults. Install Rockcraft if permitted; use Skopeo to push the packed OCI image to the MicroK8s registry, then deploy the locally packed charm. Keep test secrets, artifacts, and `.env` out of Git.
- [ ] Document local Flask/Gunicorn commands, both API URL modes, cloud ID and editmeta lookups, sandbox and Juju test commands, single-unit/worker constraints, and granting/viewing/rotating both secrets in `README.md`. Explain approximate/missing counts and Jira indexing.
- [ ] Run the local, sandbox, and opted-in Juju suites; verify `active`, password rotation, and the actual Jira update. Report any environment blocker and unverified acceptance criteria separately. Commit `build(juju): package and verify the flask deployment`.
