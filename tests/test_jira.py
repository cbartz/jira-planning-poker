import json

import pytest
import requests
import responses
from responses import matchers

from jira import JiraClient, JiraError, adf_to_text
from room import Ticket

API = 'https://api.atlassian.com/ex/jira/cloud/rest/api/3'
JQL = 'labels = grooming ORDER BY priority DESC, Rank ASC'


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
             'parent': {'key': 'P-9', 'fields': {'summary': 'Epic'}}}}
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


@pytest.mark.parametrize(('count_status', 'payload', 'expected'), [
    (200, {'count': 231}, 231), (403, {}, None),
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


@responses.activate
def test_save_payload(jira):
    responses.put(f'{API}/issue/P-1', status=204)
    ticket = Ticket('P-1', labels=('S', 'grooming', 'keep'))
    jira.save(ticket, 'M')
    with pytest.raises(ValueError):
        jira.save(ticket, '?')
    assert len(responses.calls) == 1
    assert json.loads(responses.calls[0].request.body) == {
        'fields': {'customfield_10024': 3},
        'update': {'labels': [{'add': 'M'}, {'remove': 'S'}, {'remove': 'grooming'}]},
    }


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
