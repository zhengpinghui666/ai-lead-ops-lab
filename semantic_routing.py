"""Persistent 1:1 assignment with original-provider retries and one local GPU slot."""
import json,sqlite3,threading
import analysis_store as store
import clubops as app

SCHEMA='''CREATE TABLE IF NOT EXISTS semantic_routes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, policy_engine TEXT NOT NULL, task_key TEXT NOT NULL,
 task_kind TEXT NOT NULL, lane TEXT NOT NULL CHECK(lane IN ('api','local')),
 model TEXT NOT NULL, engine TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(policy_engine,task_key));'''
LOCAL_SLOT=threading.Lock()

def local_settings(settings):
    return dict(settings,split_enabled=False,backend='ollama',model=settings['local_model'],api_base_url='',max_concurrency=1)

class Reservation:
    def __init__(self,row,settings,locked=False):
        self.row=row;self.settings=settings;self.locked=locked
    def release(self):
        if self.locked:self.locked=False;LOCAL_SLOT.release()
    def metadata(self):
        return dict(route_id=self.row['id'],lane=self.row['lane'],model=self.row['model'],engine=self.row['engine'])

def reserve(c,settings,task_key,task_kind='analysis'):
    import semantic
    policy=semantic.engine_for(settings)
    if not settings['split_enabled']:
        lane='local' if settings['backend']=='ollama' else 'api'
        row=dict(id=None,lane=lane,model=settings['model'],engine=policy)
    else:
        row=c.execute('SELECT * FROM semantic_routes WHERE policy_engine=? AND task_key=?',(policy,task_key)).fetchone()
        if row:row=dict(row)
        else:
            previous=c.execute('SELECT lane FROM semantic_routes WHERE policy_engine=? ORDER BY id DESC LIMIT 1',(policy,)).fetchone()
            lane='api' if previous and previous['lane']=='local' else 'local'
            selected=local_settings(settings) if lane=='local' else dict(settings,split_enabled=False)
            row=dict(id=None,lane=lane,model=selected['model'],engine=semantic.engine_for(selected))
    locked=row['lane']=='local'
    if locked and not LOCAL_SLOT.acquire(blocking=False):
        raise semantic.ModelBusy('本地模型正在分析，任务保留原分配并等待；没有改派 API')
    try:
        if settings['split_enabled'] and row['id'] is None:
            row['id']=c.execute('INSERT INTO semantic_routes(policy_engine,task_key,task_kind,lane,model,engine,created_at) VALUES(?,?,?,?,?,?,?)',
                (policy,task_key,task_kind,row['lane'],row['model'],row['engine'],app.now())).lastrowid
        selected=local_settings(settings) if settings['split_enabled'] and row['lane']=='local' else dict(settings,split_enabled=False)
        return Reservation(row,selected,locked)
    except BaseException:
        if locked:LOCAL_SLOT.release()
        raise

def counts(settings):
    if not settings['split_enabled']:return None
    import semantic
    try:
        with app.db() as c:
            rows=c.execute('SELECT lane,count(*) AS count FROM semantic_routes WHERE policy_engine=? GROUP BY lane',(semantic.engine_for(settings),)).fetchall()
        count={r['lane']:r['count'] for r in rows}
    except sqlite3.OperationalError:count={}
    return dict(local=count.get('local',0),api=count.get('api',0),local_model=settings['local_model'],api_model=settings['model'],ratio='1:1',local_busy=LOCAL_SLOT.locked())
