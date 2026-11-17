from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize
from app import revisions

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    if previous.get('base_run_id'):
        # 修订批重跑必须回到“基于原批的修订”链路，否则会生成一条绕过发布门禁的正式批。
        # 原批此时可能已不是正式版（修订已发布），重跑只要求它仍存在。
        return rebuild_revision(previous['base_run_id'],previous['revision_info']['corrections'],require_formal=False)
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
@app.post('/api/runs/{run_id}/revisions')
def create_revision(run_id:str,payload:dict=Body(...)):
    """设备归属更正后发起修订：整批重算、回植未受影响片段、保存为修订草案。"""
    return rebuild_revision(run_id,payload.get('corrections'))

def rebuild_revision(run_id,corrections,require_formal=True):
    """按原批与其更正依据重新组装修订版，供发起修订与修订批重跑共用。"""
    base=store.get(DB_PATH,run_id)
    prepared=revisions.prepare(base,corrections,require_formal=require_formal)
    evaluated=evaluate({'dataset':base['source'],'overrides':prepared['overrides']},snapshot=base['input_snapshot'])
    result=revisions.assemble(base,evaluated,prepared)
    return store.save_revision(DB_PATH,result,run_id)
@app.get('/api/runs/{run_id}/revisions')
def list_revisions(run_id:str):
    return store.revisions(DB_PATH,run_id)
@app.get('/api/runs/{run_id}/lineage')
def lineage(run_id:str):
    return store.lineage(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/publish')
def publish(run_id:str):
    return store.publish(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/rollback')
def rollback(run_id:str):
    return store.rollback(DB_PATH,run_id)
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result,store.lineage(DB_PATH,run_id)),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
