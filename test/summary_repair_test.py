"""Repair must preserve supported items and never bypass final verification."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import openbrain_summary as s
import plaud_pipeline as p


def draft():
    result={'sections':{h:[] for h in s.HEADINGS}}
    result['sections']['Summary']=[{'text':'A fixture decision was discussed.','evidence':[1]}]
    result['sections']['Decisions']=[{'text':'Supported fixture decision.','evidence':[1]},
                                    {'text':'Unsupported ownership rationale.','evidence':[1]}]
    return result


def rejection(section='Decisions', index=1):
    return {'valid':False,'issues':['Unsupported ownership rationale.'],
            'targets':[{'section':section,'index':index,'reason':'No source establishes this ownership rationale.'}]}


PASS={'valid':True,'issues':[],'targets':[]}


class Scripted:
    def __init__(self,*results): self.results=list(results);self.schemas=[]
    def __call__(self,prompt,schema,cfg):
        self.schemas.append(schema)
        if not self.results: raise AssertionError('Unbounded/unexpected model request')
        result=self.results.pop(0)
        if isinstance(result,Exception): raise result
        return copy.deepcopy(result),{'fixture_usage':True}


class RepairTest(unittest.TestCase):
    text='[00:00:01] Fixture Person: A fixture decision was discussed.\n'
    def summarize(self,provider,cfg=None):
        return s.summarize(self.text,{'date':'2026-10-27'},cfg or {},request=provider)
    def test_repairs_only_flagged_item_then_rechecks_full_summary(self):
        initial=draft()
        replacement={'text':'Ownership rationale was not established.','evidence':[1]}
        provider=Scripted(initial,rejection(),{'edits':[{'section':'Decisions','index':1,'replacement':replacement}]},PASS)
        md,audit=self.summarize(provider)
        self.assertEqual(provider.schemas,[s.SCHEMA,s.VERIFY_SCHEMA,s.REPAIR_SCHEMA,s.VERIFY_SCHEMA])
        self.assertEqual(audit['draft']['sections']['Summary'],initial['sections']['Summary'])
        self.assertEqual(audit['draft']['sections']['Decisions'][0],initial['sections']['Decisions'][0])
        self.assertEqual(audit['draft']['sections']['Decisions'][1],replacement)
        self.assertEqual(audit['verification'],{'valid':True,'issues':[]})
        self.assertNotIn('Unsupported ownership rationale.',md)
        self.assertEqual(audit['evidence_quotes']['Decisions'][1]['quotes'][0]['id'],1)
    def test_model_cannot_edit_an_unflagged_item(self):
        provider=Scripted(draft(),rejection(),{'edits':[{'section':'Summary','index':0,'replacement':None}]})
        with self.assertRaises(s.SummaryError) as caught:self.summarize(provider)
        self.assertEqual(len(provider.schemas),3)
        self.assertEqual(caught.exception.audit['draft'],draft())
    def test_malformed_targets_do_not_enable_arbitrary_edits(self):
        for target in ({'section':'Missing','index':0,'reason':'x'},
                       {'section':'Decisions','index':True,'reason':'x'},
                       {'section':'Decisions','index':999,'reason':'x'}):
            verdict=rejection();verdict['targets']=[target]
            with self.assertRaises(s.SummaryError):self.summarize(Scripted(draft(),verdict))
    def test_duplicate_and_missing_edits_refused(self):
        targets={('Decisions',1):'bad'}
        edit={'section':'Decisions','index':1,'replacement':None}
        for edits in ([],[edit,edit]):
            with self.assertRaises(s.SummaryError):s.apply_repairs(draft(),targets,{'edits':edits})
    def test_bad_evidence_in_repair_is_not_sent_to_final_review(self):
        edit={'section':'Decisions','index':1,'replacement':{'text':'A new claim.','evidence':[999]}}
        provider=Scripted(draft(),rejection(),{'edits':[edit]})
        with self.assertRaises(s.SummaryError):self.summarize(provider)
        self.assertEqual(len(provider.schemas),3)
    def test_repair_exhaustion_prunes_only_flagged_item_and_reverifies(self):
        unchanged={'edits':[{'section':'Decisions','index':1,'replacement':draft()['sections']['Decisions'][1]}]}
        provider=Scripted(draft(),rejection(),unchanged,rejection(),unchanged,rejection(),PASS)
        md,audit=self.summarize(provider)
        self.assertEqual(len(provider.schemas),7)
        self.assertEqual([r['kind'] for r in audit['repairs']],['patch','patch','prune'])
        self.assertEqual(audit['draft']['sections']['Decisions'],draft()['sections']['Decisions'][:1])
        self.assertIn('Supported fixture decision.',md)
        self.assertNotIn('Unsupported ownership rationale.',md)
    def test_pruned_remainder_that_fails_verification_is_not_published(self):
        provider=Scripted(draft(),rejection(),rejection('Summary',0))
        with self.assertRaises(s.SummaryError) as caught:self.summarize(provider,{'repair_rounds':0})
        self.assertFalse(caught.exception.audit['verification']['valid'])
        self.assertEqual(len(provider.schemas),3)
    def test_cannot_publish_an_empty_core_summary(self):
        provider=Scripted(draft(),rejection('Summary',0))
        with self.assertRaises(s.SummaryError):self.summarize(provider,{'repair_rounds':0})
        self.assertEqual(len(provider.schemas),2)
    def test_contradictory_verdict_refused(self):
        verdict=rejection();verdict['valid']=True
        with self.assertRaises(s.SummaryError):self.summarize(Scripted(draft(),verdict))
    def test_matching_retained_draft_resumes_at_review(self):
        initial=draft();initial['sections']['Decisions']=initial['sections']['Decisions'][:1]
        provider=Scripted(PASS)
        md,audit=self.summarize(provider,{'_resume_draft':initial})
        self.assertEqual(provider.schemas,[s.VERIFY_SCHEMA])
        self.assertTrue(audit['resumed_draft'])
        self.assertTrue(s.can_resume(audit,self.text,metadata={'date':'2026-10-27'}))
        self.assertFalse(s.can_resume(audit,self.text+'Changed.'))
        self.assertFalse(s.can_resume(audit,self.text,'New prior context.'))
    def test_resume_binds_policy_generation_and_meeting_date(self):
        _,audit=self.summarize(Scripted(draft(),PASS))
        meta={'date':'2026-10-27'}
        self.assertTrue(s.can_resume(audit,self.text,metadata=meta))
        self.assertFalse(s.can_resume(audit,self.text,metadata={'date':'2026-10-28'}))
        self.assertFalse(s.can_resume(audit,self.text,metadata=meta,generation=1))
        audit['prompt_policy']='obsolete'
        self.assertFalse(s.can_resume(audit,self.text,metadata=meta))
    def test_login_and_execution_share_actual_deadline(self):
        from types import SimpleNamespace
        clock=[0.];timeouts=[]
        def run(command,**kwargs):
            timeouts.append(kwargs['timeout'])
            if command[1]=='login':
                clock[0]=1.5
                return SimpleNamespace(returncode=0,stdout='ChatGPT',stderr='')
            raise s.subprocess.TimeoutExpired(command,kwargs['timeout'])
        with patch.object(s.shutil,'which',return_value='/fixture/codex'), \
             patch.object(s.time,'monotonic',lambda:clock[0]), patch.object(s.subprocess,'run',run):
            with self.assertRaises(s.SummaryError):s.codex_request('fixture',s.SCHEMA,{'timeout':2,'_request_deadline':2})
        self.assertEqual(timeouts,[2,0.5])
    def test_provider_oserror_retains_draft_for_retry(self):
        with self.assertRaises(s.SummaryError) as caught:
            self.summarize(Scripted(draft(),OSError('fixture')))
        self.assertEqual(caught.exception.audit['draft'],draft())
        self.assertEqual(caught.exception.audit['requests'][-1]['status'],'failed')
    def test_time_and_request_limits_are_real_failure_controls(self):
        for value in (-1,4,True):
            provider=Scripted()
            with self.assertRaises(s.SummaryError):self.summarize(provider,{'repair_rounds':value})
            self.assertFalse(provider.schemas)
        clock=[0.]
        def request(*args): clock[0]=10.;return draft(),{}
        with patch.object(s.time,'monotonic',lambda:clock[0]):
            with self.assertRaises(s.SummaryError) as caught:self.summarize(request,{'max_summary_seconds':2})
        self.assertEqual(len(caught.exception.audit['requests']),1)
    def test_deletion_indices_are_simultaneous_not_shifted(self):
        initial=draft();initial['sections']['Decisions'].append({'text':'Third.','evidence':[1]})
        result=s.apply_repairs(initial,{('Decisions',0):'x',('Decisions',2):'y'},
            {'edits':[{'section':'Decisions','index':0,'replacement':None},
                      {'section':'Decisions','index':2,'replacement':None}]})
        self.assertEqual(result['sections']['Decisions'],initial['sections']['Decisions'][1:2])


class PipelineRepairTest(unittest.TestCase):
    def test_provider_failure_resumes_saved_draft_without_redownloading(self):
        from plaud_pipeline_test import Client,speech
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);client=Client();cfg={'plaud':{'since':'2026-10-27'}}
            data=draft();data['sections']['Decisions']=[]
            first=Scripted(data,s.SummaryError('fixture provider interruption'))
            def interrupted(text,meta,options,**kw):return s.summarize(text,meta,options,request=first,**kw)
            self.assertEqual(p.sync(root,cfg,client=client,speech=speech,summary=interrupted)['failed'],1)
            with p.database(root) as db:db.execute('update recordings set next_attempt=0')
            second=Scripted(PASS)
            def resume(text,meta,options,**kw):
                self.assertIn('_resume_draft',options)
                return s.summarize(text,meta,options,request=second,**kw)
            self.assertEqual(p.sync(root,cfg,client=client,speech=speech,summary=resume)['complete'],1)
            self.assertEqual(client.downloads,1)
            self.assertEqual(second.schemas,[s.VERIFY_SCHEMA])
    def test_success_invalidates_older_rejected_draft(self):
        from plaud_pipeline_test import Client,speech
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);client=Client();cfg={'plaud':{'since':'2026-10-27'}}
            def interrupted(text,meta,options,**kw):
                return s.summarize(text,meta,options,request=Scripted(draft(),s.SummaryError('interrupted')),**kw)
            p.sync(root,cfg,client=client,speech=speech,summary=interrupted)
            with p.database(root) as db:db.execute('update recordings set next_attempt=0')
            def accepted(text,meta,options,**kw):
                return s.summarize(text,meta,options,request=Scripted(PASS),**kw)
            self.assertEqual(p.sync(root,cfg,client=client,speech=speech,summary=accepted)['complete'],1)
            with p.database(root) as db:db.execute("update recordings set stage='transcribed',completed_at=null")
            def revision(text,meta,options,**kw):
                self.assertNotIn('_resume_draft',options)
                return s.summarize(text,meta,options,request=Scripted(draft(),PASS),**kw)
            self.assertEqual(p.sync(root,cfg,client=client,speech=speech,summary=revision)['complete'],1)
    def test_explicit_revision_increments_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with p.database(root) as db:
                db.execute("insert into recordings(id,folder,stage) values('fixture','fixture','complete')")
            with patch.object(sys,'argv',['pipeline','revise','--workspace',td,'--file-id','fixture','--from-stage','transcribed']):
                self.assertEqual(p.main(),0)
            with p.database(root) as db:
                self.assertEqual(db.execute('select summary_generation from recordings').fetchone()[0],1)
    def test_failed_repair_keeps_last_good_published_note(self):
        from plaud_pipeline_test import Client,speech
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);client=Client();cfg={'plaud':{'since':'2026-10-27'},'output':{'summaries_dir':str(root/'notes')}}
            good=draft();good['sections']['Decisions']=good['sections']['Decisions'][:1]
            def accepted(text,meta,options,**kw):return s.summarize(text,meta,options,request=Scripted(good,PASS),**kw)
            p.sync(root,cfg,client=client,speech=speech,summary=accepted)
            note=next((root/'notes').glob('*.md'));before=note.read_bytes()
            with p.database(root) as db:db.execute("update recordings set stage='transcribed',completed_at=null")
            bad=Scripted(draft(),rejection(),{'edits':[{'section':'Summary','index':0,'replacement':None}]})
            def rejected(text,meta,options,**kw):return s.summarize(text,meta,options,request=bad,**kw)
            self.assertEqual(p.sync(root,cfg,client=client,speech=speech,summary=rejected)['failed'],1)
            self.assertEqual(note.read_bytes(),before)
            self.assertEqual(client.downloads,1)


if __name__=='__main__':unittest.main()
