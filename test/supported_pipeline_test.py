import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import openbrain_delivery as delivery
import openbrain_summary as summary
import plaud_mcp as mcp
import plaud_pipeline as pipeline
import speaker_acceptance as speakers


class Rpc:
    def __init__(self):
        self.thoughts = {}; self.requests = {}; self.calls = 0; self.ownership = {}
        self.drop = False; self.fail_lineage = False; self.wrong_readback = False
    def call(self, name, args):
        if name in ('meeting_ownership', 'register_meeting_recording'):
            identity=args['recording_id'].removeprefix('of_')
            data=self.ownership.setdefault(identity,{'recording_id':identity,'owner':'whosaid-local','epoch':1,'revision':0,'thought_id':None,'prior_thought_ids':[],'content_sha256':None})
            return {'content':[{'type':'text','text':'```json\n'+json.dumps(data)+'\n```'}]}
        if name == 'capture_thought':
            self.calls += 1
            key = json.dumps(args, sort_keys=True)
            if key not in self.requests:
                identity = str(uuid.uuid4()); self.requests[key] = identity
                self.thoughts[identity] = {'content': args['content'], 'current': identity}
                before=args.get('supersedes',[])
                if isinstance(before,str):before=[before]
                if not self.fail_lineage:
                    for previous in before:self.thoughts[previous]['current']=identity
                contract=args['meeting'];self.ownership[contract['recording_id']].update(
                    revision=contract['revision'],thought_id=identity,prior_thought_ids=[],
                    content_sha256=hashlib.sha256(args['content'].encode()).hexdigest())
            if self.drop:
                self.drop = False
                raise TimeoutError('lost response after remote commit')
            return {'content': [{'type':'text','text':'Captured '+self.requests[key]}]}
        identity = args['seed_id']; row = self.thoughts[identity]
        data = {'seed': {'id': identity, 'content': 'wrong' if self.wrong_readback else row['content']},
                'canon': {'is_canon': row['current'] == identity, 'current_id': row['current']}}
        return {'content': [{'type':'text','text':'```json\n'+json.dumps(data)+'\n```'}]}


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.folder = self.root/'meeting'; self.folder.mkdir()
        self.rpc = Rpc(); self.cfg = {'mode':'pilot','file_ids':['recording-fixture']}
        self.save('The team approved a fixture.')
    def save(self, text):
        transcript = '[00:00:01] Fixture Person: '+text+'\n'
        draft = {'sections': {h: [] for h in summary.HEADINGS}}
        draft['sections']['Summary'] = [{'text': text, 'evidence':[1]}]
        audit = {'verification_metadata':{'file_id':'recording-fixture','date':'2026-10-27T10:00:00-04:00'},'draft':draft,'verification':{'valid':True,'issues':[]},
                 'transcript_sha256':hashlib.sha256(transcript.encode()).hexdigest(),
                 'template_sha256':'fixture','source_passages':summary.passages(transcript)}
        for name, data in {'metadata.json':json.dumps({'file_id':'recording-fixture','date':'2026-10-27T10:00:00-04:00','title':'Fixture'}),
                           'summary.evidence.json':json.dumps(audit),'transcript.speakers.txt':transcript,
                           'summary.md':summary.validate(draft,transcript)}.items():
            (self.folder/name).write_text(data)
    def enqueue(self): return delivery.enqueue(self.root,self.folder)
    def drain(self): return delivery.drain(self.root,self.cfg,rpc=self.rpc)
    def test_production_date_boundary_and_registration(self):
        self.enqueue()
        self.cfg = {'mode':'production','since':'2026-10-28'}
        self.assertEqual(self.drain()['delivered'],0)
        self.assertEqual(self.rpc.calls,0)
        self.cfg['since']='2026-10-27'
        self.assertEqual(self.drain()['delivered'],1)
        self.assertEqual(self.drain()['delivered'],0)
    def test_production_requires_explicit_boundary(self):
        self.cfg={'mode':'production'}
        with self.assertRaises(delivery.DeliveryError):self.drain()
    def test_changed_meeting_date_requires_reverification(self):
        meta=json.loads((self.folder/'metadata.json').read_text());meta['date']='2026-10-28T10:00:00-04:00'
        (self.folder/'metadata.json').write_text(json.dumps(meta))
        with self.assertRaises(delivery.DeliveryError):self.enqueue()
        self.assertEqual(self.rpc.calls,0)
    def test_legacy_unbound_pending_row_cannot_send_until_reprepared(self):
        event=self.enqueue()
        with delivery.connect(self.root) as db:db.execute('update outbox set verification_binding=null')
        self.assertEqual(self.drain()['failed'],1)
        self.assertEqual(self.rpc.calls,0)
        self.assertEqual(self.enqueue(),event)
        self.assertEqual(self.drain()['delivered'],1)
    def test_changed_frozen_content_cannot_send(self):
        self.enqueue()
        with delivery.connect(self.root) as db:db.execute("update outbox set content=content||'changed'")
        self.assertEqual(self.drain()['failed'],1)
        self.assertEqual(self.rpc.calls,0)
        with self.assertRaises(delivery.DeliveryError):self.enqueue()
    def test_prepare_and_replay_never_write_remotely(self):
        event = self.enqueue(); self.assertEqual(event,self.enqueue()); self.assertEqual(self.rpc.calls,0)
        self.assertEqual(self.drain()['delivered'],1); self.assertEqual(self.drain()['delivered'],0)
        self.assertEqual(len(self.rpc.thoughts),1)
    def test_lost_ack_retries_identical_request_without_duplicate(self):
        self.enqueue(); self.rpc.drop=True
        self.assertEqual(self.drain()['failed'],1)
        self.assertEqual(delivery.status(self.root)['pending'],1)
        self.assertEqual(self.drain()['delivered'],1)
        self.assertEqual(len(self.rpc.thoughts),1)
    def test_correction_confirms_old_to_new_lineage(self):
        first=self.enqueue(); self.drain(); self.save('The team rejected a fixture.')
        second=self.enqueue(); self.assertNotEqual(first,second)
        self.assertEqual(self.drain()['delivered'],1)
        old = next(iter(self.rpc.thoughts.values())); self.assertNotEqual(old['current'],next(iter(self.rpc.thoughts)))
    def test_reverting_content_creates_third_revision(self):
        a=self.enqueue(); self.save('Different fixture.'); b=self.enqueue()
        self.save('The team approved a fixture.'); c=self.enqueue()
        self.assertEqual(len({a,b,c}),3); self.assertEqual(self.drain()['delivered'],3)
    def test_false_success_cannot_ack_missing_lineage(self):
        self.enqueue(); self.drain(); self.save('Changed fixture.'); self.enqueue()
        self.rpc.fail_lineage=True
        self.assertEqual(self.drain()['failed'],1); self.assertEqual(delivery.status(self.root)['pending'],1)
    def test_wrong_readback_withholds_receipt(self):
        self.enqueue(); self.rpc.wrong_readback=True
        self.assertEqual(self.drain()['failed'],1)
    def test_changed_or_unverified_summary_refused(self):
        (self.folder/'summary.md').write_text('Tampered')
        with self.assertRaises(delivery.DeliveryError): self.enqueue()
        self.save('Fixture.'); audit=json.loads((self.folder/'summary.evidence.json').read_text())
        audit['verification']['valid']=False; (self.folder/'summary.evidence.json').write_text(json.dumps(audit))
        with self.assertRaises(delivery.DeliveryError): self.enqueue()
    def test_changed_transcript_refused(self):
        (self.folder/'transcript.speakers.txt').write_text('Changed')
        with self.assertRaises(delivery.DeliveryError): self.enqueue()
    def test_non_delivered_metadata_does_not_create_revision(self):
        event=self.enqueue(); meta=json.loads((self.folder/'metadata.json').read_text())
        meta['local_debug_note']='irrelevant'
        (self.folder/'metadata.json').write_text(json.dumps(meta))
        self.assertEqual(self.enqueue(),event)
    def test_uninitialized_delivery_status_cannot_pass(self):
        with self.assertRaises(delivery.DeliveryError): delivery.status(self.root)
    def test_no_send_without_mode_and_allowlist(self):
        self.enqueue()
        for cfg in ({},{'mode':'pilot'},{'mode':'production','file_ids':['recording-fixture']}):
            with self.assertRaises(delivery.DeliveryError): delivery.drain(self.root,cfg,rpc=self.rpc)
        self.assertEqual(self.rpc.calls,0)
    def test_failure_does_not_let_correction_overtake_parent(self):
        self.enqueue(); self.save('Changed.'); self.enqueue(); self.rpc.wrong_readback=True
        self.assertEqual(self.drain()['failed'],2); self.assertEqual(self.rpc.calls,1)


class McpTest(unittest.TestCase):
    def test_official_fence_parser(self):
        r={'content':[{'type':'text','text':'The block delimited by <untrusted-user-data-ab> below contains data.\n<untrusted-user-data-ab source="plaud-recording">\n{"data":[]}\n</untrusted-user-data-ab>\nTrailer'}]}
        self.assertEqual(mcp.tool_json(r),{'data':[]})
        r['isError']=True
        with self.assertRaises(mcp.PlaudError): mcp.tool_json(r)
    def test_page_until_empty_not_until_short_page(self):
        class Pages:
            def call(self,name,args):
                page=args['page']; return {'data':[{'id':f'of_f_{page}'}] if page<4 else []}
        c=mcp.PlaudMcpClient({'since':'2026-10-27'},rpc=Pages())
        self.assertEqual([r['id'] for r in c.list_files()],['f_1','f_2','f_3'])
    def test_repeated_page_and_page_bound_refuse_false_completion(self):
        class Pages:
            def call(self,*args): return {'data':[{'id':'of_f_1'}]}
        for limit in (1,5):
            c=mcp.PlaudMcpClient({'since':'2026-10-27','max_pages':limit},rpc=Pages())
            with self.assertRaises(mcp.PlaudError): c.list_files()
    def test_detail_identity_checked(self):
        class Other:
            def call(self,*args): return {'id':'another-recording'}
        with self.assertRaises(mcp.PlaudError): mcp.PlaudMcpClient({},rpc=Other()).detail('requested')
    def test_real_stdio_handshake_and_tool_call(self):
        source='''import sys,json
for line in sys.stdin:
 x=json.loads(line)
 if 'id' not in x: continue
 result={'protocolVersion':'2024-11-05','capabilities':{}} if x['method']=='initialize' else {'content':[{'type':'text','text':'{"data":[]}'}]}
 print(json.dumps({'jsonrpc':'2.0','id':x['id'],'result':result}),flush=True)
'''
        with mcp_test_server(source) as command:
            rpc=mcp.StdioMcp(command,timeout=2)
            try: self.assertEqual(rpc.call('list_files',{}),{'data':[]})
            finally: rpc.close()
    def test_timeout_and_non_json_cannot_be_success(self):
        source='''import sys,json,time
for line in sys.stdin:
 x=json.loads(line)
 if x['method']=='initialize':
  print(json.dumps({'jsonrpc':'2.0','id':x['id'],'result':{}}),flush=True)
 elif x['method']=='tools/call': time.sleep(2)
'''
        with mcp_test_server(source) as command:
            rpc=mcp.StdioMcp(command,timeout=.1)
            try:
                with self.assertRaises(mcp.PlaudError): rpc.call('get_file',{})
            finally: rpc.close()
        with self.assertRaises(mcp.PlaudError): mcp.tool_json({'content':[{'type':'text','text':'Not authenticated'}]})


from contextlib import contextmanager
@contextmanager
def mcp_test_server(source):
    with tempfile.TemporaryDirectory() as td:
        path=Path(td)/'server.py'; path.write_text(source)
        yield [sys.executable,'-u',str(path)]


class SpeakerTest(unittest.TestCase):
    def manifest(self, predicted='Fixture Person'):
        return {'enrolled_names':['Fixture Person'],'enrollment_recording_ids':['enrollment'],
                'meetings':[{'recording_id':str(i),'annotation_status':'human-confirmed',
                'evaluation_start':0,'evaluation_end':20,
                'truth':[{'start':0,'end':10,'speaker':'Fixture Person'},{'start':10,'end':20,'speaker':None}],
                'prediction':[{'start':0,'end':10,'speaker':predicted},{'start':10,'end':20,'speaker':'UNASSIGNED'}]} for i in range(3)]}
    def test_control_pass_and_all_unknown_cannot_pass(self):
        self.assertTrue(speakers.evaluate(self.manifest())['passed'])
        self.assertFalse(speakers.evaluate(self.manifest('UNASSIGNED'))['passed'])
    def test_false_identity_fails(self):
        data=self.manifest(); data['meetings'][0]['prediction'][1]['speaker']='Fixture Person'
        self.assertFalse(speakers.evaluate(data)['passed'])
    def test_enrollment_leakage_unconfirmed_and_overlap_refused(self):
        for change in ('leak','unconfirmed','overlap'):
            data=self.manifest(); meeting=data['meetings'][0]
            if change=='leak': meeting['recording_id']='enrollment'
            elif change=='unconfirmed': meeting['annotation_status']='automatic'
            else: meeting['truth'][1]['start']=5
            with self.assertRaises(ValueError): speakers.evaluate(data)
    def test_alias_leakage_and_unscored_prediction_refused(self):
        data=self.manifest(); data['enrollment_recording_ids']=['of_0']
        with self.assertRaises(ValueError): speakers.evaluate(data)
    def test_self_only_cannot_qualify_an_untested_colleague(self):
        data=self.manifest(); data['enrolled_names'].append('Untested Colleague')
        result=speakers.evaluate(data)
        self.assertFalse(result['passed'])
        self.assertFalse(result['gates']['each_enrolled_name_in_two_meetings'])
        data=self.manifest(); data['meetings'][0]['prediction'].append({'start':20,'end':100,'speaker':'Wrong Person'})
        with self.assertRaises(ValueError): speakers.evaluate(data)


class HttpAcceptanceTest(DeliveryTest):
    # Run every delivery scenario through the real HTTP transport and a local
    # protocol-compatible receiver. This is not a hosted OpenBrain acceptance claim.
    def setUp(self):
        super().setUp()
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        inner=self.rpc
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer fixture-token':
                    self.send_error(401); return
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if data['method']=='notifications/initialized':
                    self.send_response(202); self.end_headers(); return
                if data['method']=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{}}
                else:
                    try: result=inner.call(data['params']['name'],data['params']['arguments'])
                    except TimeoutError:
                        self.send_error(504); return
                if getattr(inner,'heartbeat',False) and data['method']=='tools/call' and data['params']['name']=='capture_thought':
                    import time
                    inner.heartbeat=False
                    self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                    for _ in range(60):
                        try: self.wfile.write(b': heartbeat\n\n'); self.wfile.flush()
                        except (BrokenPipeError,ConnectionResetError): break
                        time.sleep(.02)
                    return
                if getattr(inner,'stall',False) and data['method']=='tools/call' and data['params']['name']=='capture_thought':
                    import time
                    inner.stall=False
                    time.sleep(.5)
                payload=json.dumps({'jsonrpc':'2.0','id':data['id'],'result':result})
                body=('event: message\ndata: '+payload+'\n\n').encode()
                self.send_response(200); self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except BrokenPipeError: pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def stop(): server.shutdown();server.server_close();thread.join()
        self.addCleanup(stop)
        cfg={'url':f'http://127.0.0.1:{server.server_port}/mcp',
             'token_command':[sys.executable,'-c',"print('fixture-token')"], 'timeout':2}
        with patch.object(delivery,'validate_url',lambda url:url): self.transport=delivery.HttpMcp(cfg)
    def drain(self): return delivery.drain(self.root,self.cfg,rpc=self.transport)
    def test_actual_stalled_response_keeps_pending_then_deduplicates(self):
        self.enqueue(); self.rpc.stall=True; self.transport.timeout=.1
        self.assertEqual(self.drain()['failed'],1)
        self.assertEqual(delivery.status(self.root)['pending'],1)
        self.transport.timeout=2
        self.assertEqual(self.drain()['delivered'],1)
        self.assertEqual(len(self.rpc.thoughts),1)
    def test_heartbeats_cannot_evade_total_http_deadline(self):
        self.enqueue(); self.rpc.heartbeat=True; self.transport.timeout=.3
        self.assertEqual(self.drain()['failed'],1)
        self.assertEqual(delivery.status(self.root)['pending'],1)
        self.transport.timeout=2
        self.assertEqual(self.drain()['delivered'],1)
        self.assertEqual(len(self.rpc.thoughts),1)


class BacklogTest(unittest.TestCase):
    def test_sync_cli_exits_nonzero_for_overdue_backlog(self):
        source='''import sys,json
for line in sys.stdin:
 x=json.loads(line)
 if 'id' not in x: continue
 r={} if x['method']=='initialize' else {'content':[{'type':'text','text':'{"data":[]}'}]}
 print(json.dumps({'jsonrpc':'2.0','id':x['id'],'result':r}),flush=True)
'''
        with tempfile.TemporaryDirectory() as td,mcp_test_server(source) as command:
            root=Path(td)
            with pipeline.database(root) as db:
                db.execute('insert into recordings(id,folder,first_seen,source_record) values(?,?,?,?)',
                           ('fixture','fixture',1,json.dumps({'id':'fixture','start_at':'2026-10-27T12:00:00Z'})))
            config=root/'fixture.toml'
            config.write_text('[plaud]\ntransport="official-mcp"\nsince="2026-10-27"\nmax_per_pass=0\nmcp_command='+json.dumps(command)+'\n')
            run=subprocess.run([sys.executable,str(Path(pipeline.__file__)),'sync','--workspace',str(root),
                                '--config',str(config)],capture_output=True,text=True)
            self.assertEqual(run.returncode,1,run.stderr)
            self.assertTrue(json.loads(run.stdout)['backlog_overdue'])
    def test_missing_inventory_and_outage_resume_local_checkpoint(self):
        from plaud_pipeline_test import Client,speech,summary as fixture_summary
        for outage in (False,True):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td); client=Client(); cfg={'plaud':{'since':'2026-10-27'}}
                def fail(*a,**kw): raise summary.SummaryError('fixture failure')
                pipeline.sync(root,cfg,client=client,speech=speech,summary=fail)
                with pipeline.database(root) as db: db.execute('update recordings set next_attempt=0')
                client.records=[]
                if outage:
                    def unavailable(): raise pipeline.PlaudError('inventory unavailable')
                    client.list_files=unavailable
                result=pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)
                self.assertEqual(result['complete'],1); self.assertEqual(result['pending'],0)
                self.assertEqual(result['failed'],int(outage)); self.assertEqual(client.downloads,1)
    def test_explicit_completed_revision_preserves_audio_and_old_summary(self):
        from plaud_pipeline_test import Client,speech,summary as fixture_summary
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); client=Client(); cfg={'plaud':{'since':'2026-10-27'}}
            pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)
            command=[sys.executable,str(Path(pipeline.__file__)),'revise','--workspace',str(root),
                     '--file-id','fixture','--from-stage','transcribed']
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)
            client.records=[]
            self.assertEqual(pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)['complete'],1)
            self.assertEqual(client.downloads,1)
    def test_unprocessed_inventory_is_counted_and_overdue_can_fail(self):
        from plaud_pipeline_test import Client,speech,summary as fixture_summary
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); client=Client()
            client.records += [{'id':'second','start_at':'2026-10-27T13:00:00Z'},
                               {'id':'third','start_at':'2026-10-27T14:00:00Z'}]
            cfg={'plaud':{'since':'2026-10-27','max_per_pass':1,'backlog_sla_seconds':10}}
            first=pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)
            self.assertEqual(first['pending'],2); self.assertFalse(first['backlog_overdue'])
            with pipeline.database(root) as db:
                db.execute("update recordings set first_seen=1 where stage!='complete'")
            second=pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)
            self.assertEqual(second['pending'],1); self.assertTrue(second['backlog_overdue'])
            command=[sys.executable,str(Path(pipeline.__file__)),'status','--workspace',str(root)]
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,1)
            third=pipeline.sync(root,cfg,client=client,speech=speech,summary=fixture_summary)
            self.assertEqual(third['pending'],0); self.assertFalse(third['backlog_overdue'])


if __name__=='__main__': unittest.main()
