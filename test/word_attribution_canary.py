"""Read-only replay of fixed real segments and synthetic speaker boundaries.

The real-item labels are consistency with a fixed diarization, NOT human truth.
Usage: python test/word_attribution_canary.py --baseline DIR --input JSON
       --diarization JSON --out DIR
"""
import argparse
import importlib.util
import json
import statistics
import tempfile
import time
from pathlib import Path


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def main():
    p=argparse.ArgumentParser()
    for name in ('baseline','input','diarization','out'):p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    modules={'upstream':load('baseline',a.baseline/'lib/diarize_sherpa.py'),
             'fork':load('candidate',Path(__file__).resolve().parents[1]/'lib/diarize_sherpa.py')}
    real=json.loads(a.input.read_text())['segments']
    segs=json.loads(a.diarization.read_text())['segments']
    synthetic=[
        {'name':'within-segment-switch','segments':[{'start':0,'end':2,'speaker':'A'},
         {'start':2,'end':3,'speaker':'B'}], 'asr':{'start':0,'end':3,'text':'Yes I agree',
          'words':[{'start':0,'end':1,'word':'Yes'},{'start':1,'end':2,'word':' I'},
                   {'start':2,'end':3,'word':' agree'}]},'expected':['A','B']},
        {'name':'silence-is-not-identity','segments':[{'start':0,'end':1,'speaker':'A'}],
         'asr':{'start':3,'end':4,'text':'No evidence'},'expected':['UNASSIGNED']},
        {'name':'empty-diarization','segments':[],
         'asr':{'start':0,'end':1,'text':'Unknown'},'expected':['UNASSIGNED']},
    ]
    for run in (1,2):
        results={}
        for name,module in modules.items():
            output=[]
            with tempfile.TemporaryDirectory() as td:
                f=Path(td)/'one.json'
                for i,item in enumerate(real):
                    f.write_text(json.dumps({'segments':[item]}));t=time.perf_counter()
                    turns=module.build_turns(segs,str(f));elapsed=time.perf_counter()-t
                    wanted=' '.join(w['word'].strip() for w in item.get('words',[]))
                    got=' '.join(text.strip() for turn in turns for text in turn[2])
                    output.append({'kind':'real','index':i,'seconds':elapsed,'turns':len(turns),
                                   'word_preservation':wanted.split()==got.split(),
                                   'labels':[x[0] for x in turns]})
                for case in synthetic:
                    f.write_text(json.dumps({'segments':[case['asr']]}))
                    try:
                        turns=module.build_turns(case['segments'],str(f))
                        actual=[x[0] for x in turns]
                        output.append({'kind':'synthetic','name':case['name'],'pass':actual==case['expected'],'actual':actual})
                    except Exception as error:
                        output.append({'kind':'synthetic','name':case['name'],'pass':False,'error':type(error).__name__})
            times=[x['seconds'] for x in output if x['kind']=='real']
            results[name]={'items':output,'real_count':len(real),'synthetic_count':len(synthetic),
                           'p50_seconds':statistics.median(times),'max_seconds':max(times),
                           'preserved_real_items':sum(x.get('word_preservation',False) for x in output),
                           'synthetic_pass':sum(x.get('pass',False) for x in output)}
        (a.out/f'run-{run}.json').write_text(json.dumps(results,indent=2))
        print(json.dumps({'run':run,**{k:{x:v for x,v in d.items() if x!='items'} for k,d in results.items()}}))


if __name__=='__main__':main()
