"""Persistence, migration and hosted configuration contracts."""
from contextlib import closing
import importlib
from pathlib import Path
import re

import psycopg
import pytest

from backend import db
from backend.app import create_app
from backend.migrations import LATEST_SCHEMA_VERSION
from test_workflow import client_for


@pytest.fixture
def clean_config(monkeypatch, tmp_path):
    for key in ('DATABASE_URL', 'RELIEFLINK_DB', 'VERCEL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(db, 'ROOT', tmp_path)
    return tmp_path


def test_target_precedence_and_hosted_configuration(clean_config, monkeypatch):
    assert db.database_target() == clean_config / 'data' / 'relieflink.db'
    monkeypatch.setenv('RELIEFLINK_DB', '/tmp/another-relieflink.db')
    assert db.database_target() == Path('/tmp/another-relieflink.db')
    url = 'postgresql://user:secret@example.invalid/demo'
    monkeypatch.setenv('DATABASE_URL', url)
    assert db.database_target() == url
    assert db.database_target(clean_config / 'explicit.db') == clean_config / 'explicit.db'
    monkeypatch.setenv('VERCEL', '1')
    assert db.database_target() == url
    monkeypatch.delenv('DATABASE_URL')
    with pytest.raises(db.DatabaseConfigurationError, match='DATABASE_URL'):
        db.database_target()
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///tmp/demo.db')
    with pytest.raises(db.DatabaseConfigurationError, match='PostgreSQL|postgres'):
        db.database_target()


def test_local_dotenv_is_loaded_server_side(clean_config, monkeypatch):
    url = 'postgresql://local-file@example.invalid/demo'
    (clean_config / '.env.local').write_text(f'DATABASE_URL={url}\n')
    (clean_config / '.env').write_text('DATABASE_URL=postgresql://fallback@example.invalid/demo\n')
    assert db.database_target() == url
    monkeypatch.setenv('DATABASE_URL', 'postgresql://environment@example.invalid/demo')
    assert 'environment@' in db.database_target()
    monkeypatch.delenv('DATABASE_URL')
    monkeypatch.setenv('VERCEL', '1')
    with pytest.raises(db.DatabaseConfigurationError):
        db.database_target()


def test_hosted_app_startup_neither_connects_nor_seeds(monkeypatch):
    module = importlib.import_module('backend.app')
    def forbidden(*args, **kwargs):
        pytest.fail('Hosted startup must not contact, migrate or seed the database.')
    monkeypatch.setattr(module, 'initialize', forbidden)
    monkeypatch.setattr(db, 'connect', forbidden)
    app = create_app('postgresql://user:secret@example.invalid/demo')
    assert db.database_kind(app.state.db_path) == 'postgresql'


def test_remote_connections_default_to_tls_and_pooler_safe_settings(monkeypatch):
    captured = {}
    def fake_connect(target, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(db.psycopg, 'connect', fake_connect)
    db.connect('postgresql://user:secret@example.invalid/demo')
    assert captured['sslmode'] == 'require'
    assert captured['autocommit'] is True
    assert captured['prepare_threshold'] is None
    captured.clear()
    db.connect('postgresql://user@127.0.0.1/demo')
    assert 'sslmode' not in captured


def test_connection_failures_do_not_expose_credentials(monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError('Connection failed: user:super-secret@example.invalid')
    monkeypatch.setattr(db.psycopg, 'connect', fail)
    with pytest.raises(db.DatabaseUnavailableError) as caught:
        db.connect('postgresql://user:super-secret@example.invalid/demo')
    assert 'super-secret' not in str(caught.value)
    with client_for(create_app('postgresql://user:super-secret@example.invalid/demo')) as client:
        response = client.get('/api/health')
        assert response.status_code == 503
        assert 'super-secret' not in response.text


def test_hosted_frontend_and_assets_work_without_database_connection(monkeypatch):
    root = Path(__file__).resolve().parent.parent
    if not (root / 'dist' / 'index.html').exists():
        pytest.skip('Run npm run build to verify the built frontend.')
    def fail(*args, **kwargs):
        pytest.fail('The public frontend must not require a database connection.')
    monkeypatch.setattr(db, 'connect', fail)
    with client_for(create_app('postgresql://user@example.invalid/demo')) as client:
        response = client.get('/')
        assert response.status_code == 200
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
        assert assets
        for asset in assets:
            assert client.get(asset).status_code == 200
        assert client.get('/api/unknown').status_code == 404


def test_migrations_and_seed_are_idempotent(db_target):
    with db.transaction(db_target) as connection:
        connection.execute("UPDATE locations SET notes=? WHERE id='camp-a'", ('Keep my survey',))
        db.bump(connection)
        before = {row['key']: row['value'] for row in connection.execute('SELECT * FROM meta')}
    db.initialize(db_target)
    db.initialize(db_target)
    db.check_ready(db_target)
    with db.read_transaction(db_target) as connection:
        after = {row['key']: row['value'] for row in connection.execute('SELECT * FROM meta')}
        assert after == before
        assert connection.execute("SELECT notes FROM locations WHERE id='camp-a'").fetchone()[0] == 'Keep my survey'
        assert connection.execute('SELECT COUNT(*) FROM locations').fetchone()[0] == 5
        assert connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_SCHEMA_VERSION


def test_sqlite_upgrade_adopts_existing_records(tmp_path):
    target = tmp_path / 'legacy.db'
    db.initialize(target)
    with db.transaction(target) as connection:
        connection.execute('DROP TABLE schema_migrations')
        connection.execute("UPDATE locations SET population=12345 WHERE id='camp-a'")
        db.put_meta(connection, 'version', '27')
        initialized = connection.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()[0]
    db.initialize(target)
    with db.read_transaction(target) as connection:
        assert connection.execute("SELECT population FROM locations WHERE id='camp-a'").fetchone()[0] == 12345
        assert db.version(connection) == 27
        assert connection.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()[0] == initialized


def test_failed_write_rolls_back_completely(db_target):
    with pytest.raises(RuntimeError, match='abandon'):
        with db.transaction(db_target) as connection:
            connection.execute("UPDATE locations SET notes='Must not persist' WHERE id='camp-a'")
            db.put_meta(connection, 'abandoned', 'yes')
            raise RuntimeError('abandon')
    with db.read_transaction(db_target) as connection:
        assert connection.execute("SELECT notes FROM locations WHERE id='camp-a'").fetchone()[0] != 'Must not persist'
        assert connection.execute("SELECT value FROM meta WHERE key='abandoned'").fetchone() is None


def test_bound_values_and_quoted_literals_round_trip(db_target):
    text = "Where? 75% 'safe'"
    with db.read_transaction(db_target) as connection:
        row = connection.execute('SELECT ? AS "?field", \'50%?\' AS literal', (text,)).fetchone()
        assert row[0] == row['?field'] == text
        assert dict(row)['literal'] == '50%?'


def test_health_checks_the_selected_database(db_target):
    with client_for(create_app(db_target)) as client:
        response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'database': db.database_kind(db_target),
                               'schema_version': LATEST_SCHEMA_VERSION, 'demo': True}


def test_postgres_cold_start_leaves_empty_schema_until_explicit_setup(postgres_target):
    for _ in range(2):
        with client_for(create_app(postgres_target)) as client:
            assert client.get('/api/health').status_code == 503
    with closing(db.connect(postgres_target)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=current_schema()').fetchone()[0] == 0
    db.initialize(postgres_target)
    with client_for(create_app(postgres_target)) as client:
        assert client.get('/api/health').status_code == 200


def test_cli_initialises_selected_database_without_printing_url(postgres_target, monkeypatch, capsys):
    from backend.seed import main
    monkeypatch.setenv('DATABASE_URL', postgres_target)
    monkeypatch.setattr('sys.argv', ['backend.seed'])
    main()
    monkeypatch.setattr('sys.argv', ['backend.seed', '--check'])
    main()
    output = capsys.readouterr().out
    assert 'PostgreSQL database' in output
    assert postgres_target not in output
    db.check_ready(postgres_target)
