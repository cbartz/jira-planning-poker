import hmac
import os
import secrets
import time
from collections import deque
from ipaddress import ip_address, ip_network
from threading import Lock

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from jira import JiraClient, JiraError
from room import CARDS, SIZE_POINTS, Room


def create_app(config=None, *, jira=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        MAX_CONTENT_LENGTH=16_384, APP_PASSWORD_VALUE='', JIRA_API_URL='', JIRA_SITE_URL='',
        JIRA_EMAIL='', JIRA_API_TOKEN_VALUE='', JIRA_STORY_POINTS_FIELD='customfield_10024',
        JIRA_JQL='labels = grooming ORDER BY priority DESC, Rank ASC',
    )
    app.config.from_prefixed_env()
    # Opaque keys and passwords can look like JSON and must remain exact strings.
    for key in ('APP_PASSWORD_VALUE', 'JIRA_API_TOKEN_VALUE', 'SECRET_KEY'):
        if f'FLASK_{key}' in os.environ:
            app.config[key] = os.environ[f'FLASK_{key}']
    if config:
        app.config.update(config)
    if trusted_proxy := app.config.get('TRUSTED_PROXY_NETWORK'):
        app.wsgi_app = _trust_proxy(app.wsgi_app, trusted_proxy)
    if jira is None:
        jira = JiraClient(*(str(app.config[key]) for key in (
            'JIRA_API_URL', 'JIRA_SITE_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN_VALUE',
            'JIRA_STORY_POINTS_FIELD', 'JIRA_JQL',
        )))
    room, lock = Room(), Lock()
    failures: dict[str, deque[float]] = {}
    sweep_at = 0.0
    busy = False
    jira_error = None

    @app.before_request
    def require_login():
        if request.endpoint not in {None, 'health', 'static', 'login'} and 'participant_id' not in session:
            return _redirect('login')

    CSRFProtect(app)

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def health():
        return 'ok\n'

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        nonlocal sweep_at
        if request.method == 'GET':
            return render_template('login.html')
        now = time.monotonic()
        ip = request.remote_addr or 'unknown'
        with lock:
            if now >= sweep_at:
                for address in list(failures):
                    if failures[address][-1] <= now - 60:
                        del failures[address]
                sweep_at = now + 60
            attempts = failures.setdefault(ip, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return render_template('login.html', error='Too many attempts. Try again in a minute.'), 429
            password = str(app.config['APP_PASSWORD_VALUE'] or '').encode()
            if not password or not hmac.compare_digest(password, request.form.get('password', '').encode()):
                attempts.append(now)
                return render_template('login.html', error='Incorrect password.'), 401
            if not attempts:
                del failures[ip]
        session.clear()
        session['participant_id'] = secrets.token_urlsafe(18)
        return _redirect('name')

    @app.route('/name', methods=['GET', 'POST'])
    def name():
        if request.method == 'GET':
            return render_template('name.html')
        with lock:
            try:
                session['name'] = room.join(session['participant_id'], request.form.get('name', ''))
            except ValueError as error:
                return render_template('name.html', error=str(error)), 400
        return _redirect('index')

    @app.get('/')
    @app.get('/room/state', endpoint='state')
    def index():
        with lock:
            if not room.touch(session['participant_id']):
                return _redirect('name')
            return render_room(partial=request.endpoint == 'state')

    @app.post('/room/<action>')
    def change_room(action):
        nonlocal busy, jira_error
        participant = session['participant_id']
        form = request.form
        with lock:
            if not room.touch(participant):
                return _redirect('name')
            if action not in {'claim', 'vote'} and room.facilitator != participant:
                abort(403)
            if busy and action != 'vote':
                return action_result('A Jira request is already in progress.')
            try:
                revision = form.get('revision', type=int)
                if action == 'claim':
                    room.claim(participant)
                    if room.loaded:
                        return action_result()
                elif action == 'handoff':
                    room.handoff(participant, form.get('participant'))
                elif action == 'filter':
                    room.set_filter(form.get('epic'))
                elif action == 'select':
                    room.select(form.get('key'))
                elif action == 'vote':
                    room.vote(participant, form.get('size'), revision)
                elif action == 'reveal':
                    room.reveal(revision)
                elif action == 'restart':
                    room.restart(revision)
                elif action == 'skip':
                    room.finish(revision, 'skipped')
                elif action == 'save':
                    room.check_round(revision)
                    size = form.get('size')
                    if not room.revealed or size not in SIZE_POINTS:
                        raise ValueError('Reveal votes and choose a final size first.')
                    ticket = room.tickets[room.current_key]
                elif action not in {'load', 'refresh'}:
                    abort(404)
            except PermissionError:
                abort(403)
            except ValueError as error:
                return action_result(str(error))
            if action not in {'claim', 'load', 'refresh', 'save'}:
                return action_result()
            # Polls can continue while one network operation reserves room mutations.
            busy = True
            jira_error = None
        try:
            if action == 'save':
                jira.save(ticket, size)
            else:
                snapshot = jira.search()
        except JiraError as error:
            with lock:
                jira_error = str(error)
                busy = False
                return action_result()
        except BaseException:
            with lock:
                busy = False
            raise
        with lock:
            busy = False
            if action == 'save':
                room.finish(revision, 'estimated', size)
            else:
                room.merge(*snapshot)
            return action_result()

    def action_result(error=None):
        if request.headers.get('HX-Request') == 'true':
            return render_room(partial=True, error=error)
        if error:
            flash(error)
        return _redirect('index')

    def render_room(*, partial, error=None):
        participant = session['participant_id']
        facilitator = room.facilitator == participant
        return render_template(
            '_state.html' if partial else 'room.html', room=room, participant=participant,
            facilitator=facilitator, ticket=room.tickets.get(room.current_key), cards=CARDS,
            sizes=SIZE_POINTS, suggestion=room.suggestion,
            error=error or (jira_error if facilitator else None), busy=busy,
        )

    return app


def _redirect(endpoint):
    location = url_for(endpoint)
    if request.headers.get('HX-Request') == 'true':
        return '', 200, {'HX-Redirect': location}
    return redirect(location, code=303 if request.method == 'POST' else 302)


def _trust_proxy(app, network):
    trusted = ip_network(network)
    forwarded = ProxyFix(app, x_for=1, x_proto=1, x_prefix=1)

    def dispatch(environ, start_response):
        try:
            from_proxy = ip_address(environ.get('REMOTE_ADDR') or '') in trusted
        except ValueError:
            from_proxy = False
        return (forwarded if from_proxy else app)(environ, start_response)

    return dispatch


app = create_app()
