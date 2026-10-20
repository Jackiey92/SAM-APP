import json, sqlite3, time, random, statistics, resource, os
from pathlib import Path
import sqlite_vec
ROOT=Path(__file__).resolve().parent

def connect(path):
    c=sqlite3.connect(path); c.enable_load_extension(True); sqlite_vec.load(c); c.enable_load_extension(False)
    c.execute('create virtual table v using vec0(id integer primary key, embedding float[512] distance_metric=cosine)')
    c.execute('create table metadata(id integer primary key, company_id text, model_version text, dim integer)')
    return c

def blob(v): return sqlite_vec.serialize_float32(v)
def knn(c,v,k): return c.execute('select id,distance from v where embedding match ? and k=? order by distance',(blob(v),k)).fetchall()
def basic():
    path=ROOT/'benchmark.db'; path.unlink(missing_ok=True); c=connect(path)
    rng=random.Random(42); q=[1.0]+[0.0]*511
    vectors=[[1.0, x]+[0.0]*510 for x in [0.0,0.1,0.2]]
    vectors += [[0.0]+[rng.uniform(-1,1) for _ in range(511)] for _ in range(9997)]
    c.executemany('insert into v values (?,?)',[(i+1,blob(v)) for i,v in enumerate(vectors[:1000])]); c.commit()
    first=knn(c,q,3); assert [x[0] for x in first]==[1,2,3]
    # sqlite-vec vec0 uses transactional delete+insert for same-key upsert.
    with c:
        c.execute('delete from v where id=1'); c.execute('insert into v values (1,?)',(blob(vectors[2]),))
    assert c.execute('select embedding from v where id=1').fetchone()[0]==blob(vectors[2])
    c.execute('delete from v where id=1'); c.commit(); assert 1 not in [x[0] for x in knn(c,q,20)]
    try: c.execute('insert into v values (11000,?)',(blob([1.0]*3),))
    except sqlite3.Error as e: dimension_error=str(e); c.rollback()
    else: raise AssertionError('dimension mismatch accepted')
    with c:
        c.execute('insert into metadata values (12000,?,?,?)',('acc-m0','synthetic',512)); c.execute('insert into v values (12000,?)',(blob(q),))
    assert c.execute('select count(*) from metadata where id=12000').fetchone()[0]==1
    assert c.execute('select count(*) from v where id=12000').fetchone()[0]==1
    c.execute('begin'); c.execute('insert into metadata values (13000,?,?,?)',('acc-m0','synthetic',512)); c.execute('insert into v values (13000,?)',(blob(q),)); c.rollback()
    assert c.execute('select count(*) from metadata where id=13000').fetchone()[0]==0
    assert c.execute('select count(*) from v where id=13000').fetchone()[0]==0
    c.execute('delete from v where id=12000'); c.execute('delete from metadata'); c.execute('insert into v values (1,?)',(blob(q),))
    c.executemany('insert into v values (?,?)',[(i+1,blob(vectors[i])) for i in range(1000,10000)]); c.commit()
    timings=[]
    for _ in range(30):
        t=time.perf_counter(); knn(c,q,20); timings.append((time.perf_counter()-t)*1000)
    result={'rows':c.execute('select count(*) from v').fetchone()[0],'first3':first,'dimension_error':dimension_error,'checks_passed':7,'top20_ms_median':statistics.median(timings),'top20_ms_min':min(timings),'top20_ms_max':max(timings),'db_bytes':path.stat().st_size}
    c.close(); path.unlink(); return result

def main():
    result={'A':basic()}; print(json.dumps(result),flush=True)
    from providers import LocalBgeProvider, RemoteOpenAICompatibleProvider
    try: RemoteOpenAICompatibleProvider('https://invalid.example','fake','mock')
    except ValueError: pass
    else: raise AssertionError('default remote allowed')
    start=time.perf_counter(); p=LocalBgeProvider(os.environ.get('M0_MODEL','BAAI/bge-small-zh-v1.5')); load=time.perf_counter()-start
    fixture=json.loads((ROOT/'recall_fixtures.json').read_text()); facts=fixture['facts']; queries=fixture['queries']
    t=time.perf_counter(); p.embed_texts([facts[0]['text']]); single=(time.perf_counter()-t)*1000
    t=time.perf_counter(); p.embed_texts([f['text'] for f in facts[:32]]); batch=time.perf_counter()-t
    result['B']={'model_version':p.model_version,'dim':p.dim,'load_download_seconds':load,'single_ms':single,'batch32_ms':batch*1000,'texts_per_second':32/batch,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'remote_default_blocked':True}
    assert p.dim==512
    c=connect(':memory:'); embeddings=p.embed_texts([f['text'] for f in facts])
    with c:
        for f,v in zip(facts,embeddings):
            c.execute('insert into v values (?,?)',(f['id'],blob(v))); c.execute('insert into metadata values (?,?,?,?)',(f['id'],fixture['company_id'],p.model_version,p.dim))
    details=[]
    for query in queries:
        # single-company PoC: retain explicit trusted company scope interface.
        assert c.execute('select count(*) from metadata where company_id != ?',(fixture['company_id'],)).fetchone()[0]==0
        hits=knn(c,p.embed_query(query['query']),10); ids=[h[0] for h in hits]; gold=set(query['golden'])
        details.append({**query,'recall5':len(gold.intersection(ids[:5]))/len(gold),'recall10':len(gold.intersection(ids))/len(gold),'rr10':next((1/(i+1) for i,x in enumerate(ids) if x in gold),0),'top10':[{'id':i,'score':1-d,'golden':i in gold,'text':facts[i-1]['text']} for i,d in hits]})
    def summary(ds): return {key:statistics.mean(d[key] for d in ds) for key in ['recall5','recall10','rr10']}
    result['C']={'overall':summary(details),'tricky':summary([d for d in details if d['tricky']]),'queries':details}
    result['B']['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    c.close(); out=ROOT/(os.environ.get('M0_OUTPUT','online')+'.json'); out.write_text(json.dumps(result,ensure_ascii=False,indent=2)); print(json.dumps({k:v for k,v in result.items() if k!='C'},ensure_ascii=False)); print(result['C']['overall'])
if __name__=='__main__': main()
