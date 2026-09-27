"""Phase-one relational schema; SQLite local, PostgreSQL configurable."""
import os
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import (create_engine, event, MetaData, Table, Column as C, String as S,
                        Integer as I, JSON, UniqueConstraint as U, CheckConstraint as CK)

metadata = MetaData()
tenants = Table('tenants', metadata, C('id', S, primary_key=True), C('name', S, nullable=False))
users = Table('users', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
              C('username', S, unique=True, nullable=False), C('name', S), C('password_hash', S),
              C('role', S), C('warehouse_ids', JSON))
sessions = Table('sessions', metadata, C('token_hash', S, primary_key=True), C('user_id', S),
                 C('csrf', S), C('expires', I))
warehouses = Table('warehouses', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
                   C('code', S), C('name', S), C('country', S), C('timezone', S), C('authority', S),
                   U('tenant_id', 'code'))
skus = Table('skus', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
             C('code', S), C('name', S), C('spec', S), C('unit', S), C('barcode', S),
             U('tenant_id', 'code'))
balances = Table('balances', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
                 C('owner_id', S, nullable=False), C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False),
                 *[C(k, I, nullable=False, default=0) for k in ('g','q','d','r','t','h','b','offline','online','pending','withdrawing')],
                 C('version', I, nullable=False, default=0), C('updated_at', S),
                 U('tenant_id','owner_id','warehouse_id','sku_id'),
                 *[CK(f'{k} >= 0') for k in ('g','q','d','r','t','h','b','offline','online','pending','withdrawing')])
documents = Table('documents', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
                  C('kind', S), C('warehouse_id', S), C('sku_id', S), C('quantity', I),
                  C('source', S), C('note', S), C('actor_id', S), C('created_at', S), C('status', S))
ledger = Table('ledger', metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
               C('owner_id', S), C('warehouse_id', S), C('sku_id', S), C('document_id', S, unique=True),
               C('kind', S), C('delta', JSON), C('before', JSON), C('after', JSON),
               C('actor_id', S), C('created_at', S))
outbox = Table('outbox', metadata, C('id', S, primary_key=True), C('tenant_id', S),
               C('document_id', S, unique=True), C('event_type', S), C('payload', JSON),
               C('status', S), C('created_at', S))
requests = Table('idempotency', metadata, C('id', S, primary_key=True), C('tenant_id', S), C('actor_id', S),
                 C('key', S), C('fingerprint', S), C('response', JSON),
                 U('tenant_id','actor_id','key'))

def make_engine(url=None, connect_options=None):
    if url is None:
        Path('.local').mkdir(exist_ok=True)
        url = os.environ.get('DATABASE_URL', 'sqlite:///.local/inventory.db')
    engine = create_engine(url, connect_args=({'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {}) | (connect_options or {}), pool_pre_ping=True)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def setup(conn, _):
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
    return engine

@contextmanager
def write(engine, tenant_id=None):
    """Serialize a tenant's writes before reading idempotency or balance state.

    SQLite uses database write lock; PostgreSQL tenant advisory transaction lock.
    Coarse by design for phase one; not a performance claim.
    """
    with engine.connect() as conn:
        try:
            if engine.dialect.name == 'sqlite':
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                conn.begin()
                if tenant_id:
                    from sqlalchemy import text
                    conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:tenant))'), {'tenant': tenant_id})
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
