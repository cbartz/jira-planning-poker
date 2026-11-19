from base64 import b64encode
from urllib.parse import quote

import requests

from room import SIZE_POINTS, Ticket

MAX_SEARCH_PAGES = 250


class JiraError(RuntimeError):
    """An upstream error safe to display to the facilitator."""


class JiraClient:
    def __init__(self, api_url, site_url, email, token, story_points_field, jql):
        self.api = f'{api_url.rstrip("/")}/rest/api/3'
        self.site = site_url.rstrip('/')
        self.auth = (email, token)
        self.field = story_points_field
        self.jql = jql

    def search(self) -> tuple[list[Ticket], int | None, bool]:
        issues = []
        params = {'jql': self.jql, 'fields':
                  f'summary,description,labels,issuetype,status,priority,parent,{self.field}'}
        seen_tokens = set()
        for _ in range(MAX_SEARCH_PAGES):
            remaining = 200 - len(issues)
            data = self._request('GET', '/search/jql', params=dict(params, maxResults=remaining))
            if not isinstance(data.get('issues'), list):
                raise JiraError('Jira returned an invalid ticket list.')
            page = data['issues']
            issues.extend(page[:remaining])
            token = data.get('nextPageToken')
            more = len(page) > remaining or bool(token and not data.get('isLast'))
            if not more:
                return self._tickets(issues), len(issues), False
            if len(issues) == 200:
                try:
                    count = self._request('POST', '/search/approximate-count', json={'jql': self.jql}).get('count')
                except JiraError:
                    count = None
                return self._tickets(issues), count if isinstance(count, int) and count > 200 else None, True
            if not isinstance(token, str) or token in seen_tokens:
                raise JiraError('Jira did not provide a valid next page token.')
            seen_tokens.add(token)
            params['nextPageToken'] = token
        raise JiraError(f'Jira search exceeded {MAX_SEARCH_PAGES} pages before it finished.')

    def save(self, ticket: Ticket, size: str) -> None:
        if size not in SIZE_POINTS:
            raise ValueError('Choose a size that can be saved.')
        # The loaded ticket can be stale by the time the facilitator saves it.
        labels = [{'add': size}, *({'remove': label} for label in SIZE_POINTS if label != size)]
        labels.append({'remove': 'grooming'})
        self._request('PUT', f'/issue/{quote(ticket.key, safe="")}', json={
            'fields': {self.field: SIZE_POINTS[size]}, 'update': {'labels': labels},
        })

    def _tickets(self, issues):
        try:
            fields_by_key = {issue['key']: issue['fields'] for issue in issues}
            parents = {key: fields.get('parent') or {} for key, fields in fields_by_key.items()}
            subtasks = [key for key, fields in fields_by_key.items()
                        if (fields.get('issuetype') or {}).get('subtask') or _hierarchy_level(fields) == -1]

            # A subtask's immediate parent is usually a story, not its epic.
            self._load_parent_fields((parents[key].get('key') for key in subtasks), fields_by_key)
            for key in subtasks:
                story = fields_by_key.get(parents[key].get('key'), {})
                parents[key] = story.get('parent') or {}

            self._load_parent_fields((parent.get('key') for parent in parents.values()
                                      if _hierarchy_level(parent.get('fields') or {}) is None), fields_by_key)
            return [self._ticket(issue, parents[issue['key']], fields_by_key) for issue in issues]
        except (TypeError, KeyError, AttributeError):
            raise JiraError('Jira returned an invalid ticket.') from None

    def _load_parent_fields(self, keys, fields_by_key):
        missing = sorted({key for key in keys if key and key not in fields_by_key})
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            data = self._request('POST', '/issue/bulkfetch', json={
                'issueIdsOrKeys': batch, 'fields': ['parent', 'issuetype', 'summary'],
            })
            if not isinstance(data.get('issues'), list):
                raise JiraError('Jira returned an invalid parent issue list.')
            fields_by_key.update({issue['key']: issue['fields'] for issue in data['issues']})
            for key in batch:
                # Jira can omit issues the token cannot browse.
                fields_by_key.setdefault(key, {})

    def _ticket(self, issue, parent, fields_by_key):
        key, fields = issue['key'], issue['fields']
        priority = fields.get('priority') or {}
        parent_key = parent.get('key')
        embedded = parent.get('fields') or {}
        loaded = fields_by_key.get(parent_key, {})
        level = _hierarchy_level(embedded)
        if level is None:
            level = _hierarchy_level(loaded)
        epic_key = parent_key if level == 1 else None
        return Ticket(
            key=key, url=f'{self.site}/browse/{quote(key, safe="")}',
            summary=fields.get('summary') or '', description=adf_to_text(fields.get('description')),
            issue_type=(fields.get('issuetype') or {}).get('name', ''),
            status=(fields.get('status') or {}).get('name', ''),
            priority=priority.get('name', ''), priority_icon=priority.get('iconUrl', ''),
            epic_key=epic_key, epic_summary=(loaded.get('summary') or embedded.get('summary') or '') if epic_key else '',
            labels=tuple(fields.get('labels') or ()), points=fields.get(self.field),
        )

    def _request(self, method, path, **kwargs):
        if not all(self.auth) or not self.api.startswith(('https://', 'http://')):
            raise JiraError('Jira connection settings are incomplete.')
        try:
            response = requests.request(method, self.api + path, auth=self.auth, timeout=(3, 10),
                                        allow_redirects=False, headers={'Accept': 'application/json'},
                                        **kwargs)
        except requests.RequestException:
            raise JiraError('Could not reach Jira. Try again.') from None
        if 200 <= response.status_code < 300 and method == 'PUT':
            return {}
        try:
            data = response.json()
        except ValueError:
            data = None
        if not 200 <= response.status_code < 300:
            data = data if isinstance(data, dict) else {}
            messages = data.get('errorMessages')
            if isinstance(messages, str):
                messages = [messages]
            elif not isinstance(messages, list):
                messages = []
            fields = data.get('errors') or {}
            if isinstance(fields, dict):
                messages = [*messages, *(f'{name}: {detail}' for name, detail in fields.items())]
            message = ' '.join(map(str, messages)) or str(data.get('message') or '')
            email, token = self.auth
            encoded = b64encode(f'{email}:{token}'.encode('latin1')).decode('ascii')
            for value in (encoded, quote(token, safe=''), token):
                message = message.replace(value, '[redacted]')
            raise JiraError(f'Jira HTTP {response.status_code}: {message[:1000] or "Request failed."}')
        if not isinstance(data, dict):
            raise JiraError('Jira returned an invalid response.')
        return data


def _hierarchy_level(fields):
    return (fields.get('issuetype') or {}).get('hierarchyLevel')


def adf_to_text(document) -> str:
    if isinstance(document, str):
        return document
    if not isinstance(document, dict):
        return ''
    kind = document.get('type')
    if kind == 'text':
        return document.get('text') or ''
    if kind == 'hardBreak':
        return '\n'
    text = ''.join(adf_to_text(node) for node in document.get('content') or [])
    if kind in {'paragraph', 'heading', 'codeBlock', 'tableRow'}:
        return text + '\n'
    return text.rstrip('\n') if kind == 'doc' else text
