"""Run the workflow on SQLite and optionally an isolated PostgreSQL schema.

RELIEFLINK_TEST_DATABASE_URL must name a disposable test database. Each test
gets a unique schema; DATABASE_URL (the application database) is never used.
"""
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from backend.db import initialize


@pytest.fixture
def postgres_target():
    url = os.environ.get('RELIEFLINK_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set RELIEFLINK_TEST_DATABASE_URL to run PostgreSQL integration tests.')
    schema = 'relieflink_test_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query['options'] = query.get('options', '') + f' -csearch_path={schema}'
        target = urlunsplit(parts._replace(query=urlencode(query)))
        try:
            yield target
        finally:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture(params=['sqlite', 'postgresql'])
def db_target(request, tmp_path):
    if request.param == 'postgresql':
        target = request.getfixturevalue('postgres_target')
    else:
        target = tmp_path / 'workflow.db'
    initialize(target)
    return target
