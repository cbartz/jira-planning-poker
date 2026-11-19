import re

import pytest

from app import create_app
from jira import JiraError
from room import Ticket


class JiraStub:
    def __init__(self):
        self.searches = 0
        self.saves = []
        self.search_error = self.save_error = None

    def search(self):
        self.searches += 1
        if self.search_error:
            raise JiraError(self.search_error)
        return [Ticket('P-1', summary='First story', description='<b>plain text</b>', epic_key='P-9'),
                Ticket('P-2', summary='Second story', epic_key='P-9'),
                Ticket('P-3', summary='Unrelated story')], 3, False

    def save(self, ticket, size):
        self.saves.append((ticket.key, size))
        if self.save_error:
            raise JiraError(self.save_error)


@pytest.fixture
def web():
    jira = JiraStub()
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test-key', 'APP_PASSWORD_VALUE': 'shared'}, jira=jira)
    return app, jira


def test_login_and_rate_limit(web):
    app, _ = web
    client = app.test_client()
    assert client.get('/room/state').location == '/login'
    assert client.get('/healthz').status_code == 200
    token = _token(client.get('/login'))
    for _ in range(5):
        assert client.post('/login', data={'password': 'wrong', 'csrf_token': token}).status_code == 401
    assert client.post('/login', data={'password': 'shared', 'csrf_token': token}).status_code == 429


def test_csrf_and_headers(web):
    app, _ = web
    client = app.test_client()
    assert client.post('/login', data={'password': 'shared'}).status_code == 400
    page = client.get('/login')
    assert page.headers['Content-Security-Policy'].startswith("default-src 'self'")
    assert 'unsafe-inline' not in page.headers['Content-Security-Policy']
    assert page.headers['X-Frame-Options'] == 'DENY'
    assert page.headers['Referrer-Policy'] == 'same-origin'
    response = client.post('/login', data={'password': 'shared'}, headers={'X-CSRFToken': _token(page)})
    assert response.location == '/name'
    assert all(value in response.headers['Set-Cookie'] for value in ('Secure', 'HttpOnly', 'SameSite=Lax'))


def test_room_flow_and_authorization(web):
    app, jira = web
    host, guest = _join(app, 'Host'), _join(app, '<Guest>')
    _action(host, 'claim')
    assert jira.searches == 1
    assert _action(guest, 'filter', epic='P-9').status_code == 403
    assert _action(host, 'filter', epic='P-9').status_code == 200
    assert b'Unrelated story' not in guest.get('/room/state').data
    page = _action(host, 'select', key='P-1')
    revision = _revision(page)
    assert b'&lt;b&gt;plain text&lt;/b&gt;' in page.data
    assert b'&lt;Guest&gt;' in page.data
    _action(host, 'vote', size='M', revision=revision)
    _action(guest, 'vote', size='L', revision=revision)
    before = host.get('/room/state').data
    assert before.count(b'>Voted<') == 2
    assert b'data-revealed-vote' not in before
    for action in ('select', 'reveal', 'restart', 'skip', 'save', 'load', 'refresh', 'handoff'):
        assert _action(guest, action, revision=revision).status_code == 403
    _action(host, 'reveal', revision=revision)
    after = guest.get('/room/state').data
    assert b'data-revealed-vote>M<' in after and b'data-revealed-vote>L<' in after
    assert b'Most common: L' in after
    assert b'value="L" selected' in host.get('/room/state').data
    assert jira.searches == 1
    saved = _action(host, 'save', size='L', revision=revision)
    assert jira.saves == [('P-1', 'L')]
    assert b'data-current="P-2"' in saved.data
    assert b'Estimated' in saved.data


def test_failed_save_can_retry(web):
    app, jira = web
    host, guest = _join(app, 'Host'), _join(app, 'Guest')
    revision = _open_round(host)
    jira.save_error = 'Story points is not on the edit screen'
    page = _action(host, 'save', revision=revision, size='M')
    assert b'Story points is not on the edit screen' in page.data
    assert b'data-current="P-1"' in page.data and b'Most common: M' in page.data
    assert b'edit screen' not in guest.get('/room/state').data
    jira.save_error = None
    assert b'data-current="P-2"' in _action(host, 'save', revision=revision, size='M').data


def test_failed_refresh_keeps_snapshot(web):
    app, jira = web
    host, guest = _join(app, 'Host'), _join(app, 'Guest')
    _open_round(host)
    jira.search_error = 'Invalid JQL'
    page = _action(host, 'refresh')
    assert b'Invalid JQL' in page.data and b'data-current="P-1"' in page.data
    assert b'Second story' in page.data
    assert b'Invalid JQL' not in guest.get('/room/state').data


def test_stale_save(web):
    app, jira = web
    host = _join(app, 'Host')
    revision = _open_round(host)
    _action(host, 'save', revision=revision, size='M')
    response = _action(host, 'save', revision=revision, size='L')
    assert b'The round has changed' in response.data
    assert jira.saves == [('P-1', 'M')]


def test_epic_picker_stays_open_across_polls(web):
    app, _ = web
    host = _join(app, 'Host')
    _action(host, 'claim')
    picker = rb'<select name="epic" id="([^"]+)" hx-preserve>'
    first = re.search(picker, host.get('/room/state').data)[1]
    assert re.search(picker, host.get('/room/state').data)[1] == first
    assert re.search(picker, _action(host, 'filter', epic='P-9').data)[1] != first


def _token(page):
    return re.search(rb'"X-CSRFToken": "([^"]+)"', page.data)[1].decode()


def _join(app, name):
    client = app.test_client()
    token = _token(client.get('/login'))
    client.post('/login', data={'password': 'shared', 'csrf_token': token})
    token = _token(client.get('/name'))
    assert client.post('/name', data={'name': name, 'csrf_token': token}).location == '/'
    return client


def _action(client, action, **data):
    token = _token(client.get('/'))
    return client.post(f'/room/{action}', data=data,
                       headers={'HX-Request': 'true', 'X-CSRFToken': token})


def _revision(page):
    return re.search(rb'data-revision="(\d+)"', page.data)[1].decode()


def _open_round(client):
    _action(client, 'claim')
    revision = _revision(_action(client, 'select', key='P-1'))
    _action(client, 'vote', revision=revision, size='M')
    _action(client, 'reveal', revision=revision)
    return revision
