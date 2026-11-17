import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

# runs 表中的修订谱系列。旧库在连接时按列补齐，不丢历史数据。
_DERIVED = [
    ('base_run_id', 'TEXT'), ('formal', 'INTEGER NOT NULL DEFAULT 1'),
    ('superseded_by', 'TEXT'), ('closed', 'INTEGER'), ('previous_formal', 'TEXT'),
]

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    columns={row['name'] for row in con.execute('PRAGMA table_info(runs)')}
    for name, ddl in _DERIVED:
        if name not in columns: con.execute(f'ALTER TABLE runs ADD COLUMN {name} {ddl}')
    con.execute("CREATE TABLE IF NOT EXISTS lineage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, action TEXT NOT NULL, detail TEXT, at TEXT NOT NULL)")
    con.commit()
    return con

def _record(con, run_id):
    row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result'])
    result.update(revision=row['revision'],status=row['status'],formal=bool(row['formal']),
                  base_run_id=row['base_run_id'],superseded_by=row['superseded_by'],
                  closed=(None if row['closed'] is None else bool(row['closed'])),
                  previous_formal=row['previous_formal'])
    return result

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result,formal) VALUES(?,?,?,1)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def save_revision(path, result, base_id):
    """保存修订草案。草案 formal=0，只有闭合且显式发布后才成为正式版本。"""
    run_id=result['manifest']['run_id']
    closed=bool(result.get('closed'))
    if run_id==base_id: raise BusinessError('REVISION_IDENTICAL','更正经重算后与原批相同，无需修订',409)
    with connect(path) as con:
        existing=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if existing:
            previous=_record(con,run_id)
            if previous['base_run_id']!=base_id:
                raise BusinessError('REVISION_CONFLICT','同一修订结果已归属其他原批',409)
            return previous
        con.execute('INSERT INTO runs(id,digest,result,revision,status,base_run_id,formal,closed) VALUES(?,?,?,1,?,?,0,?)',
                    (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False),'待发布',base_id,1 if closed else 0))
        con.execute('INSERT INTO lineage_events(run_id,action,detail,at) VALUES(?,?,?,?)',
                    (run_id,'created',f'由 {base_id} 重算，阻断 {len(result.get("blockers",[]))} 项',_now()))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: return _record(con,run_id)

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status,base_run_id,formal,closed,superseded_by FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def publish(path, run_id):
    """把闭合的修订草案原子切换为正式版本；任一步失败整体回滚，旧正式版不受影响。"""
    with connect(path) as con:
        draft=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not draft: raise BusinessError('NOT_FOUND','批次不存在',404)
        if draft['base_run_id'] is None:
            raise BusinessError('NOT_A_REVISION','该批次不是修订草案',409)
        if not draft['closed']:
            raise BusinessError('REVISION_NOT_CLOSED','差异未闭合，不能替换正式版本',409)
        if draft['formal']:
            return _record(con,run_id)
        base_id=draft['base_run_id']
        base=con.execute('SELECT * FROM runs WHERE id=?',(base_id,)).fetchone()
        if not base or not base['formal'] or base['superseded_by']:
            raise BusinessError('BASE_NOT_FORMAL','原批已不是当前正式版本，请基于最新版本修订',409)
        now=_now()
        changed=con.execute('UPDATE runs SET formal=0,superseded_by=? WHERE id=? AND formal=1 AND superseded_by IS NULL',(run_id,base_id))
        if changed.rowcount!=1: raise BusinessError('SWITCH_CONFLICT','正式版本已被其他修订切换',409)
        switched=con.execute("UPDATE runs SET formal=1,previous_formal=?,status=? WHERE id=? AND formal=0",(base_id,'已复核',run_id))
        if switched.rowcount!=1:
            # 显式抛错让上下文管理器回滚，旧正式版本保持可用。
            raise BusinessError('SWITCH_FAILED','正式版本切换失败，已恢复旧版',409)
        con.execute('INSERT INTO lineage_events(run_id,action,detail,at) VALUES(?,?,?,?)',(run_id,'published',f'替代 {base_id}',now))
        con.execute('INSERT INTO lineage_events(run_id,action,detail,at) VALUES(?,?,?,?)',(base_id,'superseded',f'由 {run_id} 替代',now))
    return get(path,run_id)

def rollback(path, run_id):
    """撤销一次正式切换，恢复被替代的旧版；旧版重新可核验、可导出。"""
    with connect(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        if not row['formal'] or not row['previous_formal']:
            raise BusinessError('NOT_ROLLBACKABLE','该批次不是可回滚的正式修订',409)
        previous=row['previous_formal']
        restored=con.execute('UPDATE runs SET formal=1,superseded_by=NULL WHERE id=? AND formal=0 AND superseded_by=?',(previous,run_id))
        if restored.rowcount!=1: raise BusinessError('ROLLBACK_FAILED','旧版状态异常，未恢复',409)
        reverted=con.execute('UPDATE runs SET formal=0,previous_formal=NULL WHERE id=? AND formal=1',(run_id,))
        if reverted.rowcount!=1: raise BusinessError('ROLLBACK_FAILED','修订状态异常，已回滚旧版',409)
        con.execute('INSERT INTO lineage_events(run_id,action,detail,at) VALUES(?,?,?,?)',(run_id,'rolled_back',f'恢复 {previous}',_now()))
    return get(path,previous)

def revisions(path, run_id):
    with connect(path) as con:
        if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
            raise BusinessError('NOT_FOUND','批次不存在',404)
        rows=[dict(r) for r in con.execute('SELECT id,status,formal,closed,previous_formal,superseded_by,result FROM runs WHERE id=? OR base_run_id=? ORDER BY rowid',(run_id,run_id))]
    items=[]
    for row in rows:
        result=json.loads(row.pop('result'))
        row['diff']=result.get('diff');row['blockers']=result.get('blockers',[])
        items.append(row)
    return items

def lineage(path, run_id):
    with connect(path) as con:
        rows=[dict(r) for r in con.execute('SELECT id,action,detail,at FROM lineage_events WHERE run_id=? ORDER BY id',(run_id,))]
    return rows

def _now():
    return datetime.now(timezone.utc).isoformat()
