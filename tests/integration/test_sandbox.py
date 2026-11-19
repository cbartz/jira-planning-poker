from conftest import eventually
from jira import JiraClient


def test_sandbox_search_save(sandbox, issue):
    jql = f'key = "{issue}" AND labels = grooming ORDER BY priority DESC, Rank ASC'
    jira = JiraClient(sandbox.api_url, sandbox.site_url, sandbox.email, sandbox.token, sandbox.field, jql)
    tickets = eventually(lambda: jira.search()[0])
    assert tickets and tickets[0].key == issue
    jira.save(tickets[0], 'M')
    fields = sandbox.fields(issue)
    assert fields[sandbox.field] == 3
    assert set(fields['labels']) == {'M', 'test-keep'}
