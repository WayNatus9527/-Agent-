"""Ordered, additive migrations. Existing phase-one databases are adopted as v1."""
from sqlalchemy import Table, Column, Integer, String, select, insert
from . import db

versions = Table('schema_migrations', db.metadata,
                 Column('version', Integer, primary_key=True), Column('name', String, nullable=False))
BASE = ('tenants','users','sessions','warehouses','skus','balances','documents','ledger','outbox','idempotency')
PHASE4 = ('agent_proposals','agent_action_audit')
PHASE3 = ('transfers','transfer_lines','transfer_events','transfer_claims','return_orders','quality_events','user_settings','admin_audit')
PHASE2 = ('import_batches','purchase_orders','purchase_lines','purchase_receipts','approval_requests','reversal_links')

def migrate(engine):
    from . import workflows, operations, accounts, agent_actions  # Register all additive schemas.
    with db.write(engine, '__schema_migration__') as c:
        versions.create(c, checkfirst=True)
        applied = set(c.execute(select(versions.c.version)).scalars())
        if applied - {1, 2, 3, 4}:
            raise RuntimeError('Database schema is newer than this application')
        for version, name, tables in [(1,'phase_one_baseline',BASE),(2,'imports_purchases_approvals',PHASE2),(3,'transfers_returns_trial',PHASE3),(4,'agent_confirmed_actions',PHASE4)]:
            if version in applied:
                continue
            for table in tables:
                db.metadata.tables[table].create(c, checkfirst=True)
            c.execute(insert(versions).values(version=version,name=name))
