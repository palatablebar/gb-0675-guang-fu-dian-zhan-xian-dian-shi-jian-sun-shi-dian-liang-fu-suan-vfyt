import io, json, zipfile
from app.errors import BusinessError

def serialize(result, lineage=None):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        manifest={**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')}
        manifest.update({key:result.get(key) for key in ['formal','base_run_id','superseded_by','closed']})
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        if result.get('revision_info') or result.get('diff'):
            archive.writestr('revision.json',json.dumps({
                'base_run_id':result.get('base_run_id'),
                'revision_info':result.get('revision_info'),
                'diff':result.get('diff'),
                'blockers':result.get('blockers',[]),
                'closed':result.get('closed'),
                'lineage':lineage or []},ensure_ascii=False,indent=2))
    return buffer.getvalue()
