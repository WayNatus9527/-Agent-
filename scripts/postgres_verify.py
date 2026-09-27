"""Run a pg_dump/pg_restore drill on isolated, generated test schemas/databases."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, delete
from sqlalchemy.engine import make_url
from app import db
from app.bootstrap import seed
from app.domain import stock_action


def digest(engine):
    result={}
    with engine.connect() as c:
        for table in db.metadata.sorted_tables:
            if table.name=='sessions':continue
            rows=[dict(r) for r in c.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            result[table.name]={'count':len(rows),'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url-file',required=True);parser.add_argument('--bin-dir');parser.add_argument('--output',default='.local/postgres-verification.json');args=parser.parse_args()
    if args.bin_dir:bin_dir=Path(args.bin_dir)
    else:
        from pgserver._commands import POSTGRES_BIN_PATH
        bin_dir=POSTGRES_BIN_PATH
    url=make_url(Path(args.url_file).read_text().strip());assert url.drivername=='postgresql+psycopg'
    root=db.make_engine(url.render_as_string(hide_password=False))
    suffix=uuid4().hex;schema='drill_'+suffix;database='restore_'+suffix
    source=None;target=None;created_database=False
    report={'kind':'isolated PostgreSQL dump/restore drill'}
    try:
        with root.begin() as c:
            report['server_version']=c.exec_driver_sql('SHOW server_version').scalar()
            c.exec_driver_sql('CREATE SCHEMA '+schema)
        source=db.make_engine(url.render_as_string(hide_password=False),{'options':'-csearch_path='+schema})
        seed(source,password=secrets.token_urlsafe(24))
        with source.connect() as c:actor=dict(c.execute(select(db.users).where(db.users.c.id=='admin')).mappings().one())
        stock_action(source,actor,'receipt',{'warehouse_id':'wh-cn','sku_id':'sku-1','g':7,'q':2,'d':1,'source':'restore-drill','expected_version':1},'restore-drill-receipt')
        before=digest(source)
        folder=Path('.local/backups');folder.mkdir(parents=True,exist_ok=True);archive=folder/('postgres-'+suffix+'.dump')
        env=os.environ.copy();env.pop('PGOPTIONS',None)
        env.update(PGUSER=url.username or '',PGPASSWORD=url.password or '',PGDATABASE=url.database or 'postgres',PGHOST=url.query.get('host',url.host or 'localhost'),PGPORT=str(url.query.get('port',url.port or 5432)))
        subprocess.run([str(bin_dir/'pg_dump'),'-Fc','--schema='+schema,'-f',str(archive)],env=env,check=True,umask=0o077,capture_output=True)
        with root.connect().execution_options(isolation_level='AUTOCOMMIT') as c:c.exec_driver_sql('CREATE DATABASE '+database)
        created_database=True
        subprocess.run([str(bin_dir/'pg_restore'),'--exit-on-error','--no-owner','--no-privileges','--dbname='+database,str(archive)],env=env,check=True,capture_output=True)
        target=db.make_engine(url.set(database=database).render_as_string(hide_password=False),{'options':'-csearch_path='+schema})
        with db.write(target) as c:c.execute(delete(db.sessions))
        after=digest(target)
        assert before==after,'Restored row content differs'
        from app.migrations import migrate
        migrate(target)
        with target.connect() as c:
            assert c.exec_driver_sql('SELECT COUNT(*) FROM ledger').scalar()==13
            assert c.exec_driver_sql("SELECT g FROM balances WHERE warehouse_id='wh-cn' AND sku_id='sku-1'").scalar()==335
        report.update(verified=True,sessions_invalidated=True,table_digests=after,archive=archive.name,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2));output.chmod(0o600)
        print(json.dumps({'server_version':report['server_version'],'verified':True,'tables_checked':len(after),'report':str(output)},ensure_ascii=False))
    finally:
        if source:source.dispose()
        if target:target.dispose()
        if created_database:
            with root.connect().execution_options(isolation_level='AUTOCOMMIT') as c:c.exec_driver_sql('DROP DATABASE '+database)
        with root.begin() as c:c.exec_driver_sql('DROP SCHEMA IF EXISTS '+schema+' CASCADE')
        root.dispose()

if __name__=='__main__':main()
