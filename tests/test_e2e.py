from fastapi.testclient import TestClient
from app import main,store
from app.engine import evaluate
import pytest

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)
def test_api_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();b=c.post('/api/runs',json={}).json();assert a['manifest']['run_id']==b['manifest']['run_id'];assert len(c.get('/api/runs').json())==1
def test_stale_review(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();url='/api/runs/'+a['manifest']['run_id']+'/review';assert c.post(url,json={'revision':1}).status_code==200;assert c.post(url,json={'revision':1}).status_code==409
def test_seven_entries(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    for k in main.MODULES:
        assert c.post('/api/'+k,json={}).status_code==200
        assert c.get('/'+k).status_code==200
def test_bad_dataset(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.post('/api/runs',json={'dataset':'../bad'}).status_code==422
def test_missing_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/runs/none').status_code==404
def test_restart_store(tmp_path,sample):
    p=str(tmp_path/'durable.sqlite');store.save(p,sample);assert store.get(p,sample['manifest']['run_id'])['manifest']['sha256']==sample['manifest']['sha256']
def test_bad_revision(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);s=c.post('/api/runs',json={}).json();assert c.post('/api/runs/'+s['manifest']['run_id']+'/review',json={'revision':0}).status_code==422

def test_clean_navigation_run_preserved(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    for k in main.MODULES:
        html=c.get('/'+k+'?run_id='+rid).text
        assert '?run_id='+rid in html
        assert rid in html
    assert c.get('/api/export/'+rid).status_code==200

def test_custom_csv_snapshot_and_navigation(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    original=(ROOT/'sample_data/clean.csv').read_text()
    # 改成独立于所有仓内样例的证据编号，追踪其跨页与重跑行为。
    first=original.splitlines()[1].split(',')[0]
    custom=original.replace(first,first+'-CUSTOM')
    result=c.post('/api/runs',json={'csv_text':custom}).json()
    rid=result['manifest']['run_id']
    assert '-CUSTOM' in str(result['records']) or '-CUSTOM' in str(result['quarantine'])
    for key in main.MODULES:
        response=c.get('/'+key+'?run_id='+rid)
        assert response.status_code==200
        assert '?run_id='+rid in response.text
    repeated=c.post('/api/runs/'+rid+'/rerun',json={})
    assert repeated.status_code==200
    assert repeated.json()['manifest']['run_id']==rid
    assert repeated.json()['manifest']['sha256']==result['manifest']['sha256']

def _correction(device='REF-A1', field='capacity_kw', to='200'):
    return {'device': device, 'field': field, 'to': to,
            'basis': '台账更正单 PCS-2026-1015', 'reason': '铭牌容量录入有误', 'actor': '运维张工'}

def test_revision_not_closed_cannot_publish(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    base=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=base['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction('REF-A1','zone','西区')]}).json()
    assert draft['closed'] is False
    assert draft['blockers']
    assert c.post('/api/runs/'+draft['manifest']['run_id']+'/publish').status_code==409
    # 未闭合草案不能替换正式版本，原批仍是正式版且可核验导出。
    assert c.get('/api/runs/'+rid).json()['formal'] is True
    assert c.get('/api/export/'+rid).status_code==200

def test_revision_publish_and_rollback(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    base=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=base['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    new_id=draft['manifest']['run_id']
    assert draft['closed'] is True
    published=c.post('/api/runs/'+new_id+'/publish').json()
    assert published['formal'] is True
    old=c.get('/api/runs/'+rid).json()
    assert old['formal'] is False and old['superseded_by']==new_id
    # 旧归档仍可核验：内容不变、可导出。
    assert old['manifest']['sha256']==base['manifest']['sha256']
    assert c.get('/api/export/'+rid).status_code==200
    # 回滚恢复旧正式版本。
    restored=c.post('/api/runs/'+new_id+'/rollback').json()
    assert restored['manifest']['run_id']==rid and restored['formal'] is True
    assert c.get('/api/runs/'+new_id).json()['formal'] is False

def test_publish_conflicts_on_stale_base(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    base=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=base['manifest']['run_id']
    first=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction('REF-A1','capacity_kw','200')]}).json()
    second=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction('REF-A2','capacity_kw','200')]}).json()
    assert c.post('/api/runs/'+first['manifest']['run_id']+'/publish').status_code==200
    # 原批已被替代，第二份草案基于旧正式版，必须拒绝切换。
    assert c.post('/api/runs/'+second['manifest']['run_id']+'/publish').status_code==409
    assert c.get('/api/runs/'+first['manifest']['run_id']).json()['formal'] is True

def test_revision_replay_is_idempotent(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    a=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    b=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    assert a['manifest']['run_id']==b['manifest']['run_id']
    assert a['diff']==b['diff']

def test_revision_lineage_and_list(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    new_id=draft['manifest']['run_id']
    c.post('/api/runs/'+new_id+'/publish')
    items=c.get('/api/runs/'+rid+'/revisions').json()
    assert {item['id'] for item in items}=={rid,new_id}
    actions=[event['action'] for event in c.get('/api/runs/'+new_id+'/lineage').json()]
    assert 'created' in actions and 'published' in actions

def test_revision_from_non_formal_base_rejected(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    c.post('/api/runs/'+draft['manifest']['run_id']+'/publish')
    stale=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction('REF-A2','capacity_kw','200')]})
    assert stale.status_code==409

def test_chained_revision_then_rollback(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    first=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction('REF-A1','capacity_kw','200')]}).json()
    c.post('/api/runs/'+first['manifest']['run_id']+'/publish')
    second=c.post('/api/runs/'+first['manifest']['run_id']+'/revisions',
                  json={'corrections':[_correction('REF-A2','capacity_kw','200')]}).json()
    assert second['base_run_id']==first['manifest']['run_id']
    assert second['closed'] is True
    c.post('/api/runs/'+second['manifest']['run_id']+'/publish')
    # 回滚只退一层：恢复第一版，原批仍是已替代的旧版。
    restored=c.post('/api/runs/'+second['manifest']['run_id']+'/rollback').json()
    assert restored['manifest']['run_id']==first['manifest']['run_id']
    assert restored['formal'] is True
    assert c.get('/api/runs/'+rid).json()['formal'] is False

def test_revision_bundle_carries_basis_and_diff(tmp_path, monkeypatch):
    import io, json, zipfile
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    new_id=draft['manifest']['run_id']
    bundle=zipfile.ZipFile(io.BytesIO(c.get('/api/export/'+new_id).content))
    assert set(bundle.namelist())=={'manifest.json','evidence.json','revision.json'}
    revision=json.loads(bundle.read('revision.json'))
    assert revision['base_run_id']==rid
    assert revision['revision_info']['corrections'][0]['basis']=='台账更正单 PCS-2026-1015'
    assert revision['diff']['counts']['affected']>0
    assert revision['diff']['counts']['inherited']>0
    assert revision['closed'] is True
    # 原批归档不含修订内容，仍按原样核验。
    old=zipfile.ZipFile(io.BytesIO(c.get('/api/export/'+rid).content))
    assert 'revision.json' not in old.namelist()

def test_draft_review_does_not_publish(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    new_id=draft['manifest']['run_id']
    c.post('/api/runs/'+new_id+'/review',json={'revision':1})
    assert c.get('/api/runs/'+new_id).json()['formal'] is False
    assert c.get('/api/runs/'+rid).json()['formal'] is True

def test_revision_rerun_stays_draft(tmp_path, monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    draft=c.post('/api/runs/'+rid+'/revisions',json={'corrections':[_correction()]}).json()
    new_id=draft['manifest']['run_id']
    repeated=c.post('/api/runs/'+new_id+'/rerun',json={})
    assert repeated.status_code==200
    # 重跑不得绕过发布门禁生成正式批，批号与草案一致。
    assert repeated.json()['manifest']['run_id']==new_id
    assert repeated.json()['formal'] is False
    assert repeated.json()['diff']==draft['diff']
