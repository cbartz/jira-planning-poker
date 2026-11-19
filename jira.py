from base64 import b64encode
from urllib.parse import quote

import requests

from room import SIZE_POINTS, Ticket


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
        tickets = []
        params = {'jql': self.jql, 'fields':
                  f'summary,description,labels,issuetype,status,priority,parent,{self.field}'}
        seen_tokens = set()
        while len(tickets) < 200:
            remaining = 200 - len(tickets)
            data = self._request('GET', '/search/jql', params=dict(params, maxResults=remaining))
            if not isinstance(data.get('issues'), list):
                raise JiraError('Jira returned an invalid ticket list.')
            page = data['issues']
            try:
                tickets.extend(self._ticket(issue) for issue in page[:remaining])
            except (TypeError, KeyError, AttributeError):
                raise JiraError('Jira returned an invalid ticket.') from None
            token = data.get('nextPageToken')
            more = len(page) > remaining or bool(token and not data.get('isLast'))
            if not more:
                return tickets, len(tickets), False
            if len(tickets) == 200:
                try:
                    count = self._request('POST', '/search/approximate-count', json={'jql': self.jql}).get('count')
                except JiraError:
                    count = None
                return tickets, count if isinstance(count, int) and count > 200 else None, True
            if not isinstance(token, str) or token in seen_tokens:
                raise JiraError('Jira did not provide a valid next page token.')
            seen_tokens.add(token)
            params['nextPageToken'] = token

    def save(self, ticket: Ticket, size: str) -> None:
        if size not in SIZE_POINTS:
            raise ValueError('Choose a size that can be saved.')
        labels = [{'add': size}] if size not in ticket.labels else []
        labels.extend({'remove': label} for label in SIZE_POINTS
                      if label != size and label in ticket.labels)
        labels.append({'remove': 'grooming'})
        self._request('PUT', f'/issue/{quote(ticket.key, safe="")}', json={
            'fields': {self.field: SIZE_POINTS[size]}, 'update': {'labels': labels},
        })

    def _ticket(self, issue):
        key, fields = issue['key'], issue['fields']
        priority = fields.get('priority') or {}
        parent = fields.get('parent') or {}
        return Ticket(
            key=key, url=f'{self.site}/browse/{quote(key, safe="")}',
            summary=fields.get('summary') or '', description=adf_to_text(fields.get('description')),
            issue_type=(fields.get('issuetype') or {}).get('name', ''),
            status=(fields.get('status') or {}).get('name', ''),
            priority=priority.get('name', ''), priority_icon=priority.get('iconUrl', ''),
            epic_key=parent.get('key'), epic_summary=(parent.get('fields') or {}).get('summary', ''),
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
