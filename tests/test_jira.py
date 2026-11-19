import json

import pytest
import requests
import responses
from responses import matchers

from jira import JiraClient, JiraError, adf_to_text
from room import Ticket

API = 'https://api.atlassian.com/ex/jira/cloud/rest/api/3'
JQL = 'labels = grooming ORDER BY priority DESC, Rank ASC'
M_UPDATE = {
    'fields': {'customfield_10024': 3},
    'update': {'labels': [
        {'add': 'M'}, {'remove': 'XS'}, {'remove': 'S'}, {'remove': 'L'}, {'remove': 'grooming'},
    ]},
}


@pytest.fixture
def jira():
    return JiraClient(API.removesuffix('/rest/api/3'), 'https://site.atlassian.net',
                      'user', 'secret', 'customfield_10024', JQL)


def test_adf_text():
    def text(value):
        return {'type': 'text', 'text': value}

    document = {'type': 'doc', 'content': [
        {'type': 'paragraph', 'content': [text('Hello '), text('<b>world</b>')]},
        {'type': 'paragraph', 'content': [text('Second'), {'type': 'hardBreak'}, text('line')]},
    ]}
    assert adf_to_text(document) == 'Hello <b>world</b>\nSecond\nline'
    assert adf_to_text(None) == ''


@responses.activate
def test_enhanced_search_paginates(jira):
    params = {'jql': JQL, 'maxResults': '200', 'fields':
              'summary,description,labels,issuetype,status,priority,parent,customfield_10024'}
    issue = {'key': 'P-1', 'fields': {'summary': 'Story', 'labels': ['S', 'grooming'],
             'customfield_10024': 2, 'issuetype': {'name': 'Story'}, 'status': {'name': 'Open'},
             'priority': {'name': 'Urgent', 'iconUrl': 'https://site.atlassian.net/icon'},
             'parent': {'key': 'P-9', 'fields': {'summary': 'Epic', 'issuetype': {'hierarchyLevel': 1}}}}}
    for query, page in [(params, {'issues': [issue], 'nextPageToken': 'next'}),
                        (dict(params, maxResults='199', nextPageToken='next'),
                         {'issues': [{'key': 'P-2', 'fields': {}}], 'isLast': True})]:
        responses.get(f'{API}/search/jql', json=page, match=[
            matchers.query_param_matcher(query),
            matchers.header_matcher({'Authorization': 'Basic dXNlcjpzZWNyZXQ='}),
        ])
    tickets, total, truncated = jira.search()
    assert (total, truncated) == (2, False)
    assert tickets[0] == Ticket('P-1', 'https://site.atlassian.net/browse/P-1', 'Story', '',
                                'Story', 'Open', 'Urgent', 'https://site.atlassian.net/icon',
                                'P-9', 'Epic', ('S', 'grooming'), 2)
    assert tickets[1].key == 'P-2'


@responses.activate(assert_all_requests_are_fired=False)
def test_search_bounds_empty_continuation_pages(jira, monkeypatch):
    monkeypatch.setattr('jira.MAX_SEARCH_PAGES', 3, raising=False)
    for token in ('one', 'two', 'three'):
        responses.get(f'{API}/search/jql', json={'issues': [], 'nextPageToken': token})
    fallback = responses.get(f'{API}/search/jql', json={'issues': [], 'isLast': True})
    with pytest.raises(JiraError, match='exceeded 3 pages'):
        jira.search()
    assert len(responses.calls) == 3
    assert fallback.call_count == 0


@responses.activate
def test_search_uses_loaded_parent_to_find_subtask_epic(jira):
    epic = {'key': 'P-9', 'fields': {'summary': 'Rollout', 'issuetype': {'hierarchyLevel': 1}}}
    parent = {'key': 'P-8', 'fields': {'summary': 'Story', 'issuetype': {'hierarchyLevel': 0}}}
    subtask = {'key': 'P-1', 'fields': {'issuetype': {'subtask': True}, 'parent': parent}}
    story = {'key': 'P-8', 'fields': {**parent['fields'], 'parent': epic}}
    responses.get(f'{API}/search/jql', json={'issues': [subtask, story], 'isLast': True})
    tickets, _, _ = jira.search()
    assert [(ticket.key, ticket.epic_key, ticket.epic_summary) for ticket in tickets] == [
        ('P-1', 'P-9', 'Rollout'), ('P-8', 'P-9', 'Rollout'),
    ]
    assert len(responses.calls) == 1


@responses.activate
def test_search_bulk_fetches_missing_parent_once_for_subtasks(jira):
    story_type = {'hierarchyLevel': 0, 'subtask': False}
    story = {'key': 'P-8', 'fields': {'issuetype': story_type, 'parent': {
        'key': 'P-9', 'fields': {'summary': 'Rollout', 'issuetype': {'hierarchyLevel': 1}},
    }}}
    issues = [
        {'key': 'P-1', 'fields': {'issuetype': {'hierarchyLevel': -1},
                                  'parent': {'key': 'P-8', 'fields': {'issuetype': story_type}}}},
        {'key': 'P-2', 'fields': {'issuetype': {'subtask': True}, 'parent': {'key': 'P-8'}}},
        {'key': 'P-3', 'fields': {'issuetype': {'subtask': True}, 'parent': {'key': 'P-7'}}},
        {'key': 'P-4', 'fields': {'issuetype': {'hierarchyLevel': 1}, 'parent': {
            'key': 'P-10', 'fields': {'summary': 'Initiative', 'issuetype': {'hierarchyLevel': 2}},
        }}},
    ]
    responses.get(f'{API}/search/jql', json={'issues': issues, 'isLast': True})
    responses.post(f'{API}/issue/bulkfetch', json={'issues': [story]}, match=[
        matchers.json_params_matcher({'issueIdsOrKeys': ['P-7', 'P-8'],
                                      'fields': ['parent', 'issuetype', 'summary']}),
    ])
    tickets, _, _ = jira.search()
    assert [(ticket.key, ticket.epic_key) for ticket in tickets] == [
        ('P-1', 'P-9'), ('P-2', 'P-9'), ('P-3', None), ('P-4', None),
    ]


@pytest.mark.parametrize(('count_status', 'payload', 'expected'), [
    (200, {'count': 231}, 231), (403, {}, None), (502, {'errorMessages': 502}, None),
])
@responses.activate
def test_over_limit(jira, count_status, payload, expected):
    responses.get(f'{API}/search/jql', json={
        'issues': [{'key': f'P-{n}', 'fields': {}} for n in range(200)], 'nextPageToken': 'more',
    })
    responses.post(f'{API}/search/approximate-count', status=count_status, json=payload,
                   match=[matchers.json_params_matcher({'jql': JQL})])
    tickets, total, truncated = jira.search()
    assert (len(tickets), total, truncated) == (200, expected, True)
    assert len(responses.calls) == 2


@pytest.mark.parametrize('snapshot_labels', [('S', 'grooming', 'keep'), ('M', 'keep')])
@responses.activate
def test_save_payload(jira, snapshot_labels):
    responses.put(f'{API}/issue/P-1', status=204)
    jira.save(Ticket('P-1', labels=snapshot_labels), 'M')
    assert len(responses.calls) == 1
    assert json.loads(responses.calls[0].request.body) == M_UPDATE


@responses.activate
def test_explicit_save_retry_reapplies_the_full_label_update(jira):
    responses.put(f'{API}/issue/P-1', body=requests.Timeout())
    responses.put(f'{API}/issue/P-1', status=204)
    ticket = Ticket('P-1', labels=('M', 'keep'))
    with pytest.raises(JiraError):
        jira.save(ticket, 'M')
    assert len(responses.calls) == 1
    jira.save(ticket, 'M')
    assert [json.loads(call.request.body) for call in responses.calls] == [M_UPDATE, M_UPDATE]


@responses.activate
def test_save_rejects_question_mark_without_request(jira):
    with pytest.raises(ValueError):
        jira.save(Ticket('P-1'), '?')
    assert not responses.calls


@pytest.mark.parametrize('status', [400, 401, 403, None])
@responses.activate
def test_jira_errors(jira, status):
    if status is None:
        responses.get(f'{API}/search/jql', body=requests.Timeout('secret'))
    else:
        responses.get(f'{API}/search/jql', status=status,
                      json={'errors': {'field': 'Cannot edit field: secret'}})
    with pytest.raises(JiraError) as error:
        jira.search()
    message = str(error.value)
    assert 'secret' not in message
    assert (f'{status}' in message and 'Cannot edit field' in message) if status else 'reach Jira' in message
