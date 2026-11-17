from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
        related=store.revisions(DB_PATH,run_id)
        lineage=store.lineage(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
        related=[{'id':state['manifest']['run_id'],'status':state['status'],'formal':state.get('formal',True),
                  'closed':True,'base_run_id':None,'diff':None,'blockers':[]}]
        lineage=[]
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':state['views'][key],'related':related,'lineage':lineage})
