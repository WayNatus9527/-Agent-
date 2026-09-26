"""Ordered, additive migrations. Existing phase-one databases are adopted as v1."""
from sqlalchemy import Table, Column, Integer, String, select, insert
from . import db

versions = Table('schema_migrations', db.metadata,
                 Column('version', Integer, primary_key=True), Column('name', String, nullable=False))
BASE = ('tenants','users','sessions','warehouses','skus','balances','documents','ledger','outbox','idempotency')
PHASE2 = ('import_batches','purchase_orders','purchase_lines','purchase_receipts','approval_requests','reversal_links')

def migrate(engine):
    from . import workflows  # Register the new tables before migration.
    with db.write(engine, '__schema_migration__') as c:
        versions.create(c, checkfirst=True)
        applied = set(c.execute(select(versions.c.version)).scalars())
        if applied - {1, 2}:
            raise RuntimeError('Database schema is newer than this application')
        for version, name, tables in [(1,'phase_one_baseline',BASE),(2,'imports_purchases_approvals',PHASE2)]:
            if version in applied:
                continue
            for table in tables:
                db.metadata.tables[table].create(c, checkfirst=True)
            c.execute(insert(versions).values(version=version,name=name))
