import json
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from dashboard_state import dashboard_router


def shop(handle, ready='true', status='active'):
    return {'handle': handle, 'fields': [{'key': 'is_fully_ready', 'value': ready},
            {'key': 'status', 'value': status}, {'key': 'owner_customer_id', 'value': 'PRIVATE'}]}


def campaign(handle, **state):
    return {'handle': handle, 'fields': [{'key': 'data', 'value': json.dumps(state)}]}


@pytest.fixture
def api():
    graphql = Mock(return_value={})
    app = FastAPI()
    app.include_router(dashboard_router(graphql, lambda: 'custom_shop', lambda: 'store_fundraising'))
    return TestClient(app), graphql


def test_ten_stores_use_one_query(api):
    client, graphql = api
    handles = [f'team-{i}' for i in range(10)]
    graphql.return_value = {key: value for i, h in enumerate(handles)
                            for key, value in [(f's{i}', shop(h)), (f'f{i}', None)]}
    response = client.post('/dashboard/state', json={'handles': handles})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert set(response.json()['stores']) == set(handles)
    graphql.assert_called_once()
    query, variables = graphql.call_args.args
    assert len(variables) == 20
    assert variables['s9'] == {'type': 'custom_shop', 'handle': 'team-9'}
    assert 'PRIVATE' not in response.text


@pytest.mark.parametrize('handles', [[], ['a'] * 11, ['../secret'], ['x" }'], ['UPPER'], [None]])
def test_invalid_input_does_not_query(api, handles):
    client, graphql = api
    assert client.post('/dashboard/state', json={'handles': handles}).status_code in (400, 422)
    graphql.assert_not_called()


def test_duplicates_are_collapsed(api):
    client, graphql = api
    graphql.return_value = {'s0': shop('team'), 'f0': None}
    assert list(client.post('/dashboard/state', json={'handles': ['team', 'team']}).json()['stores']) == ['team']
    assert len(graphql.call_args.args[1]) == 2


@pytest.mark.parametrize('show_bar', [True, False])
def test_fundraiser_privacy(api, show_bar):
    client, graphql = api
    graphql.return_value = {'s0': shop('team'), 'f0': campaign('team', enabled=True,
        show_bar=show_bar, goal=1000, total_raised=250, cause_name='Travel', end_date='2026-12-31',
        stripe_account_id='PRIVATE', owner_customer_id='PRIVATE', ledger=['PRIVATE'])}
    response = client.post('/dashboard/state', json={'handles': ['team']})
    fr = response.json()['stores']['team']['fundraising']
    assert fr['enabled'] is True
    assert ('total_raised' in fr) is show_bar
    assert ('cause_name' in fr) is show_bar
    assert 'PRIVATE' not in response.text


def test_partial_missing_malformed_and_sleeping_stores(api):
    client, graphql = api
    graphql.return_value = {'s0': shop('good', 'false', 'building'), 'f0': None,
        's1': None, 'f1': None, 's2': shop('bad'),
        'f2': {'handle': 'bad', 'fields': [{'key': 'data', 'value': 'not json'}]},
        's3': shop('sleep', 'true', 'sleeping'), 'f3': None}
    result = client.post('/dashboard/state', json={'handles': ['good', 'missing', 'bad', 'sleep']}).json()['stores']
    assert result['good']['ready'] is False
    assert result['good']['fundraising']['enabled'] is False
    assert 'error' in result['missing'] and 'error' in result['bad']
    assert result['sleep']['status'] == 'sleeping'


def test_incomplete_fundraiser_response_does_not_disable(api):
    client, graphql = api
    graphql.return_value = {'s0': shop('team')}
    result = client.post('/dashboard/state', json={'handles': ['team']}).json()['stores']['team']
    assert 'fundraising' not in result


def test_mismatched_store_is_not_applied(api):
    client, graphql = api
    graphql.return_value = {'s0': shop('another'), 'f0': None}
    assert 'error' in client.post('/dashboard/state', json={'handles': ['team']}).json()['stores']['team']


def test_upstream_failure_is_not_false_readiness(api):
    client, graphql = api
    graphql.side_effect = RuntimeError('PRIVATE upstream diagnostic')
    response = client.post('/dashboard/state', json={'handles': ['team']})
    assert response.status_code == 502
    assert 'PRIVATE' not in response.text
    assert 'stores' not in response.json()


def test_shopify_wait_runs_outside_event_loop(api):
    client, graphql = api
    threads = []
    graphql.side_effect = lambda *args: threads.append(threading.current_thread().name) or {'s0': shop('team'), 'f0': None}
    assert client.post('/dashboard/state', json={'handles': ['team']}).status_code == 200
    assert threads == ['AnyIO worker thread']
