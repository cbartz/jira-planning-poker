# Jira Planning Poker

Run a backlog grooming session and save T-shirt estimates directly to Jira. One
participant clicks **Facilitate**, selects a ticket, and reveals the votes. Save
updates story points and size labels and removes `grooming` in one Jira request.
`?` means “can’t estimate” and is not saved. Room state is ephemeral; saved
estimates live in Jira.

## Jira setup

Use a scoped API token with `read:jira-work` and `write:jira-work`. Set the API
URL to the Atlassian API gateway and the site URL to your `atlassian.net` site
(for issue links only). A classic unscoped token uses the site URL for both.

Replace the site and issue placeholders, and set `EMAIL`, `TOKEN`, and `API_URL`
in your shell without adding them to shell history or enabling shell tracing:

```bash
# API URL for scoped tokens (cloudId lookup, no auth needed)
echo "https://api.atlassian.com/ex/jira/$(curl -s https://<site>.atlassian.net/_edge/tenant_info | jq -r .cloudId)"

# Story Points (company-managed) or Story point estimate (team-managed)
curl -s -u "$EMAIL:$TOKEN" "$API_URL/rest/api/3/field" \
  | jq -r '.[] | select(.name | test("story point"; "i")) | "\(.id)\t\(.name)"'

# When there are duplicates, find the field editable on an actual ticket
curl -s -u "$EMAIL:$TOKEN" "$API_URL/rest/api/3/issue/<PROJ-123>/editmeta" \
  | jq -r '.fields | to_entries[] | select(.value.name | test("story point"; "i")) | "\(.key)\t\(.value.name)"'
```

If `editmeta` lists no story-points field, a Jira admin needs to put it on the
project’s edit screen; saves will otherwise return HTTP 400. The production
default is `customfield_10024`. Set the field explicitly when a site differs.

## Local development

Use Python 3.12. If you keep settings in a local `.env`, source it yourself and
never commit it. The app reads the same `FLASK_*` settings as the charm:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export FLASK_SECRET_KEY="$(openssl rand -hex 32)"
export FLASK_SESSION_COOKIE_SECURE=false  # Local HTTP only.
export FLASK_JIRA_API_URL="$API_URL"
export FLASK_JIRA_SITE_URL="$SITE_URL"
export FLASK_JIRA_EMAIL="$EMAIL"
export FLASK_JIRA_API_TOKEN_VALUE="$TOKEN"
export FLASK_JIRA_STORY_POINTS_FIELD=customfield_10024
read -rs -p 'Shared password: ' FLASK_APP_PASSWORD_VALUE
export FLASK_APP_PASSWORD_VALUE

gunicorn --workers 1 --threads 8 --bind 127.0.0.1:8000 app:app
```

Open `http://127.0.0.1:8000`. The optional `FLASK_JIRA_JQL` defaults to
`labels = grooming ORDER BY priority DESC, Rank ASC`. Loading keeps up to 200
tickets; above that limit, Jira can provide only an approximate count. Refresh
reruns the configured JQL. Changing the epic filter or polling never calls Jira.

## Deploy on Juju

Build on amd64 with Rockcraft and Charmcraft. The files are based on their
`flask-framework` profiles. Push the rock to a registry reachable from your
Kubernetes nodes before deploying the charm:

```bash
rockcraft pack
skopeo copy oci-archive:jira-planning-poker_0.1.0_amd64.rock docker://REGISTRY/jira-planning-poker:0.1.0
charmcraft pack --project-dir charm
juju deploy ./jira-planning-poker_amd64.charm jira-planning-poker \
  --resource flask-app-image=REGISTRY/jira-planning-poker:0.1.0 \
  --config jira-api-url="$API_URL" --config jira-site-url="$SITE_URL" \
  --config jira-email="$EMAIL" --config jira-story-points-field=customfield_10024
```

Keep **one unit** and **one Gunicorn worker** (`webserver-workers=1`); extra
processes or units would create separate rooms. `webserver-threads=8` allows
concurrent polling. Put the app behind HTTPS ingress and leave
`flask-session-cookie-secure=true`. For a path-stripping ingress or to rate-limit
by original client IP, set `trusted-proxy-network` (locally `FLASK_TRUSTED_PROXY_NETWORK`)
to the immediate ingress proxy’s address or dedicated subnet in CIDR notation.
Use only a range reserved for trusted ingress proxies, not the general pod
network. The ingress must set the rightmost `X-Forwarded-For`,
`X-Forwarded-Proto`, and (for a path mount) `X-Forwarded-Prefix`. The app ignores
these headers from outside the configured range. The charm creates its own Flask signing key;
`juju run jira-planning-poker/0 rotate-secret-key` invalidates existing cookies.

### Set and rotate credentials

The operator owns both Juju user secrets. Save the IDs returned by `add-secret`
and substitute them below:

```bash
juju add-secret jira-planning-poker-password value="$(openssl rand -base64 18)"
juju grant-secret jira-planning-poker-password jira-planning-poker
juju config jira-planning-poker app-password=secret:<password-id>

juju add-secret jira-token value="$TOKEN"
juju grant-secret jira-token jira-planning-poker
juju config jira-planning-poker jira-api-token=secret:<token-id>
juju status jira-planning-poker

# Reveal only in a private terminal.
juju show-secret jira-planning-poker-password --reveal
juju update-secret jira-planning-poker-password value="$(openssl rand -base64 18)"
# Create a replacement token in Atlassian first, then set NEW_TOKEN securely.
juju update-secret jira-token value="$NEW_TOKEN"
```

Updates restart the workload and clear the in-memory room. Revoke the old Jira
token once the replacement works.

## Tests

```bash
python -m pytest -q tests/test_room.py tests/test_jira.py tests/test_app.py
python -m pytest -q tests/integration/test_sandbox.py
JIRA_PLANNING_POKER_JUJU_TESTS=1 python -m pytest -q -s tests/integration/test_charm.py
```

Live Jira tests require `JIRA_SANDBOX_API_URL`, `JIRA_SANDBOX_SITE_URL`,
`JIRA_SANDBOX_EMAIL`, `JIRA_SANDBOX_API_TOKEN`,
`JIRA_SANDBOX_STORY_POINTS_FIELD`, and `JIRA_SANDBOX_PROJECT_KEY`. The account
must be able to create, edit, and delete Stories in that project. Tests wait
for Jira search indexing and delete the test issue in fixture cleanup.

The opt-in Juju tests require `ai-dev-controller` on `microk8s/localhost`,
working Rockcraft/Charmcraft build providers, `microk8s kubectl`, Skopeo, and
NodePort 32000 available on a non-production cluster. They pack both artifacts,
push the image to a temporary registry, and deploy to disposable models. The password rotation
test does not require Jira credentials. The deployed grooming round requires
the same sandbox variables and permissions as the standalone Jira test.
