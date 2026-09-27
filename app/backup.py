"""SQLite online snapshots and non-destructive restore drills; no HTTP data downloads."""
import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4


def counts(conn):
    names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name:conn.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in names}

def verify(path):
    with sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity check failed')
        return counts(c)

def snapshot(source,folder):
    source=Path(source).resolve();folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    if not source.is_file():raise ValueError('Source database does not exist')
    target=folder/('inventory-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.db')
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:src.backup(dst)
    report={'file':target.name,'created_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'counts':verify(target)}
    manifest=target.with_suffix('.json');manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2));manifest.chmod(0o600)
    return report

def restore(source,target):
    source=Path(source).resolve();target=Path(target).resolve()
    if target.exists():raise ValueError('Restore target must be a new file; existing databases are never overwritten')
    manifest=json.loads(source.with_suffix('.json').read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest()!=manifest['sha256']:raise ValueError('Backup checksum mismatch')
    before=verify(source)
    if before!=manifest['counts']:raise ValueError('Backup row counts mismatch')
    target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        if counts(dst)!=before:raise ValueError('Restore row counts mismatch')
        # A restored copy must not revive old authentication sessions.
        if 'sessions' in before:dst.execute('DELETE FROM sessions');dst.commit()
    after=verify(target)
    if after!={**before,**({'sessions':0} if 'sessions' in before else {})}:raise ValueError('Restore verification failed')
    return {'restored_file':target.name,'counts':after,'sessions_invalidated':True,'verified':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Create a SQLite backup or restore into a NEW database file.')
    sub=parser.add_subparsers(dest='action',required=True)
    make=sub.add_parser('create');make.add_argument('--source',default='.local/inventory.db');make.add_argument('--directory',default='.local/backups')
    recover=sub.add_parser('restore');recover.add_argument('--source',required=True);recover.add_argument('--target',required=True)
    args=parser.parse_args();print(json.dumps(snapshot(args.source,args.directory) if args.action=='create' else restore(args.source,args.target),ensure_ascii=False,indent=2))
