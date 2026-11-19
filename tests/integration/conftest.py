import os
import time
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
import requests


@dataclass(frozen=True)
class Sandbox:
    api_url: str
    site_url: str
    email: str
    token: str = field(repr=False)
    field: str
    project: str

    def request(self, method, path, **kwargs):
        response = requests.request(method, f'{self.api_url.rstrip("/")}/rest/api/3{path}',
                                    auth=(self.email, self.token), timeout=15,
                                    headers={'Accept': 'application/json'}, allow_redirects=False, **kwargs)
        if not response.ok:
            pytest.fail(f'Jira sandbox {method} {path}: HTTP {response.status_code}')
        return response.json() if response.content else None

    def fields(self, key):
        return self.request('GET', f'/issue/{key}', params={'fields': f'labels,{self.field}'})['fields']


@pytest.fixture(scope='session')
def sandbox():
    names = tuple(f'JIRA_SANDBOX_{name}' for name in (
        'API_URL', 'SITE_URL', 'EMAIL', 'API_TOKEN', 'STORY_POINTS_FIELD', 'PROJECT_KEY',
    ))
    if not all(os.getenv(name) for name in names):
        pytest.skip('Set all six JIRA_SANDBOX_* variables to run live Jira tests')
    connection = Sandbox(*(os.environ[name] for name in names))
    permissions = connection.request('GET', '/mypermissions', params={
        'projectKey': connection.project, 'permissions': 'CREATE_ISSUES,DELETE_ISSUES',
    })['permissions']
    if not all(permissions.get(key, {}).get('havePermission') for key in ('CREATE_ISSUES', 'DELETE_ISSUES')):
        pytest.skip('Sandbox account must be able to create and delete throwaway issues in the test project')
    return connection


@pytest.fixture
def issue(sandbox):
    result = sandbox.request('POST', '/issue', json={'fields': {
        'project': {'key': sandbox.project}, 'issuetype': {'name': 'Story'},
        'summary': f'Grooming Poker throwaway {uuid4().hex}', 'labels': ['grooming', 'S', 'test-keep'],
    }})
    key = result['key']
    try:
        yield key
    finally:
        sandbox.request('DELETE', f'/issue/{key}')


def eventually(probe, *, timeout=60, interval=1):
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)
