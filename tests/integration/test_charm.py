import os
import secrets
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import jubilant
import pytest
import requests
from bs4 import BeautifulSoup

from conftest import eventually

pytestmark = pytest.mark.skipif(os.getenv('JIRA_PLANNING_POKER_JUJU_TESTS') != '1',
                                reason='Set JIRA_PLANNING_POKER_JUJU_TESTS=1 to deploy to MicroK8s')
ROOT = Path(__file__).resolve().parents[2]
APP = 'jira-planning-poker'
CONTROLLER = 'ai-dev-controller'
KUBECTL = ['microk8s', 'kubectl']


@pytest.fixture(scope='module')
def packed():
    assert (ROOT / 'rockcraft.yaml').is_file(), 'Flask rock configuration is missing'
    charm_root = ROOT / 'charm'
    assert (charm_root / 'charmcraft.yaml').is_file(), 'Flask charm configuration is missing'
    # Managed craft builds can only write artifacts to paths inside their project mounts.
    with (TemporaryDirectory(prefix=f'.{APP}-pack-', dir=ROOT) as rock_output,
          TemporaryDirectory(prefix=f'.{APP}-pack-', dir=charm_root) as charm_output):
        subprocess.run(['rockcraft', 'pack', '--output', Path(rock_output).name], cwd=ROOT, check=True)
        subprocess.run(['charmcraft', 'pack', '--output', Path(charm_output).name], cwd=charm_root, check=True)
        rock, = Path(rock_output).glob('*.rock')
        charm, = Path(charm_output).glob('*.charm')
        yield rock, charm


@pytest.fixture(scope='module')
def artifacts(packed):
    rock, charm = packed
    image = f'localhost:32000/{APP}:{uuid4().hex}'
    namespace = f'{APP}-registry-{uuid4().hex[:8]}'
    subprocess.run([*KUBECTL, 'create', 'namespace', namespace], check=True)
    try:
        kubectl = [*KUBECTL, '-n', namespace]
        subprocess.run([*kubectl, 'create', 'deployment', 'registry', '--image=registry:2'], check=True)
        subprocess.run([*kubectl, 'create', 'service', 'nodeport', 'registry', '--tcp=5000:5000',
                        '--node-port=32000'], check=True)
        subprocess.run([*kubectl, 'rollout', 'status', 'deployment/registry', '--timeout=120s'], check=True)
        with requests.Session() as client:
            client.trust_env = False
            assert eventually(lambda: registry_ready(client), timeout=30), 'Registry API did not become ready'
        subprocess.run(['skopeo', 'copy', '--insecure-policy', '--dest-tls-verify=false',
                        f'oci-archive:{rock}', f'docker://{image}'], check=True)
        yield charm, image
    finally:
        subprocess.run([*KUBECTL, 'delete', 'namespace', namespace, '--wait=false'], check=True)


def test_charm_login_and_rotation(artifacts):
    password, new_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with deployed(artifacts, password=password) as (juju, base):
        with requests.Session() as client:
            assert _login(client, base, 'wrong').status_code == 401
            login = _login(client, base, password)
            assert login.status_code == 303
            assert login.headers['Location'] == '/name'
        _set_secret(juju, 'app-password', new_password, update=True)

        def new_login_works():
            with requests.Session() as client:
                try:
                    return _login(client, base, new_password).status_code == 303
                except (requests.RequestException, AttributeError, KeyError, TypeError):
                    return False

        assert eventually(new_login_works, interval=10, timeout=180), 'New app password did not take effect'
        with requests.Session() as client:
            assert _login(client, base, password).status_code == 401
        juju.wait(lambda status: jubilant.all_active(status, APP))


def test_deployed_grooming_round(sandbox, issue, artifacts):
    password = secrets.token_urlsafe(24)
    jql = f'key = "{issue}" AND labels = grooming ORDER BY priority DESC, Rank ASC'
    with deployed(artifacts, password=password, sandbox=sandbox, jql=jql) as (_, base):
        with requests.Session() as client:
            assert _login(client, base, password).headers['Location'] == '/name'
            assert _post(client, base, '/name', name='Facilitator').headers['Location'] == '/'
            _action(client, base, 'claim')
            snapshot = eventually(lambda: _find_ticket(client, base, issue), interval=2)
            assert snapshot, 'Throwaway issue did not appear in the deployed room'
            current = _action(client, base, 'select', key=issue)
            round_state = current.select_one('[data-current]')
            assert round_state['data-current'] == issue
            revision = round_state['data-revision']
            _action(client, base, 'vote', size='M', revision=revision)
            revealed = _action(client, base, 'reveal', revision=revision)
            assert 'Most common: M' in revealed.get_text()
            saved = _action(client, base, 'save', size='M', revision=revision)
            assert saved.select_one('[data-current]')['data-current'] == ''
            assert 'Estimated' in saved.get_text()
    fields = sandbox.fields(issue)
    assert fields[sandbox.field] == 3
    assert set(fields['labels']) == {'M', 'test-keep'}


@contextmanager
def deployed(artifacts, *, password, sandbox=None, jql=None):
    charm, image = artifacts
    with jubilant.temp_model(controller=CONTROLLER, cloud='microk8s/localhost') as juju:
        config = {
            'flask-session-cookie-secure': False,  # Local port-forward is HTTP.
            'jira-api-url': sandbox.api_url if sandbox else 'https://example.invalid',
            'jira-site-url': sandbox.site_url if sandbox else 'https://example.invalid',
            'jira-email': sandbox.email if sandbox else 'test@example.invalid',
        }
        if sandbox:
            config.update({'jira-story-points-field': sandbox.field, 'jira-jql': jql})
        juju.deploy(charm, APP, resources={'flask-app-image': image}, config=config)
        for secret_name, value in [('app-password', password),
                                   ('jira-api-token', sandbox.token if sandbox else 'unused')]:
            uri = _set_secret(juju, secret_name, value)
            juju.grant_secret(uri, APP)
            juju.config(APP, {secret_name: uri})
        juju.wait(lambda status: jubilant.all_active(status, APP), timeout=300)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        namespace = juju.model.rsplit(':', 1)[-1]
        with forward(namespace, f'statefulset/{APP}', port, 8000):
            yield juju, f'http://127.0.0.1:{port}'


@contextmanager
def forward(namespace, target, local, remote):
    with subprocess.Popen([*KUBECTL, '-n', namespace, 'port-forward', target, f'{local}:{remote}'],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            def bound():
                if process.poll() is not None:
                    raise RuntimeError(process.stdout.read().decode())
                with socket.socket() as connection:
                    return connection.connect_ex(('127.0.0.1', local)) == 0

            assert eventually(bound, timeout=30), f'Could not forward {target}'
            yield
        finally:
            if process.poll() is None:
                process.terminate()


def _set_secret(juju, name, value, *, update=False):
    return juju.cli('update-secret' if update else 'add-secret', name, 'value#file=/dev/stdin',
                    stdin=value).strip()


def registry_ready(client):
    try:
        return client.get('http://127.0.0.1:32000/v2/', timeout=2).status_code == 200
    except requests.RequestException:
        return False


def _post(client, base, path, *, token=None, **data):
    if token is None:
        page = client.get(base + path, timeout=10)
        token = BeautifulSoup(page.content, 'html.parser').select_one('[name=csrf_token]')['value']
    return client.post(base + path, data=dict(data, csrf_token=token), timeout=30, allow_redirects=False)


def _login(client, base, password):
    return _post(client, base, '/login', password=password)


def _action(client, base, action, **data):
    page = client.get(base, timeout=10)
    token = BeautifulSoup(page.content, 'html.parser').select_one('[name=csrf_token]')['value']
    response = _post(client, base, f'/room/{action}', token=token, **data)
    assert response.status_code == 303
    page = client.get(base, timeout=10)
    return BeautifulSoup(page.content, 'html.parser')


def _find_ticket(client, base, issue):
    page = client.get(base, timeout=10)
    if issue in page.text:
        return True
    _action(client, base, 'refresh')
    return False
