import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
import speaker_shadow_pilot as pilot
import plaud_pipeline as pipeline


class ShadowTest(unittest.TestCase):
    profiles = [{'name':'Alice','model':'fixture','embedding':[1.,0.,0.]},
                {'name':'Bob','model':'fixture','embedding':[0.,1.,0.]}]

    def sidecar(self):
        return {'emb_model':'fixture','names':{'SPEAKER_00':'SPEAKER_00','SPEAKER_01':'SPEAKER_01'},
                'segments':[{'start':0.,'end':10.,'speaker':'SPEAKER_00'},
                            {'start':0.,'end':2.,'speaker':'SPEAKER_01'}]}

    def test_overlap_split_does_not_propagate_identity_to_entire_cluster(self):
        source=self.sidecar();before=copy.deepcopy(source)
        segments,names,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:[1,0,0],{'Alice'})
        self.assertEqual(source,before)
        self.assertEqual(calls,1)
        self.assertEqual([(s['start'],s['end']) for s in segments if s['speaker'].startswith('PILOT_')],[(2,10)])
        self.assertEqual(names['SPEAKER_00'],'SPEAKER_00')
        self.assertEqual(decisions[0]['name'],'Alice')

    def test_existing_named_speech_never_relabelled(self):
        source=self.sidecar();source['names']['SPEAKER_00']='Existing Person'
        segments,names,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:self.fail('should not embed'),{'Alice'})
        self.assertEqual(names['SPEAKER_00'],'Existing Person');self.assertEqual(calls,0)

    def test_unapproved_and_ambiguous_names_remain_anonymous(self):
        source=self.sidecar()
        for vector,reason in [([0,1,0],'outside_pilot_names'),([.7,.7,0],'ambiguous_identity')]:
            segments,names,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:vector,{'Alice'})
            self.assertEqual(decisions[0]['reason'],reason)
            self.assertFalse(any(s['speaker'].startswith('PILOT_') for s in segments))

    def test_duplicate_ranges_embedded_only_once(self):
        source=self.sidecar();source['segments'].append(copy.deepcopy(source['segments'][0]))
        _,_,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:[1,0,0],{'Alice'})
        self.assertEqual(calls,1);self.assertEqual(len(decisions),1)

    def test_short_or_fully_overlapped_speech_cannot_acquire_identity(self):
        for end in (2,3.9):
            source=self.sidecar();source['segments'][0]['end']=end
            _,_,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:self.fail('short interval embedded'),{'Alice'})
            self.assertEqual(calls,0)

    def test_budget_exhaustion_keeps_all_remaining_speech_anonymous(self):
        source={'emb_model':'fixture','names':{'SPEAKER_00':'SPEAKER_00'},
                'segments':[{'start':i*6.,'end':i*6.+4.,'speaker':'SPEAKER_00'} for i in range(205)]}
        segments,names,decisions,calls=pilot.overlays(source,self.profiles,lambda a,b:[1,0,0],{'Alice'})
        self.assertEqual(calls,200)
        self.assertEqual(len(decisions),205)
        self.assertEqual(sum(r['reason']=='interval_budget_exhausted' for r in decisions),5)
        self.assertTrue(all(r['name'] is None for r in decisions[-5:]))
        self.assertEqual(sum(s['end']-s['start'] for s in segments),205*4)

    def test_snapshot_and_partial_run_controls_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();target=root/'pilot'
            for name in pilot.INPUTS:(source/name).write_text('{}')
            audio=pilot.digest(source/'original.audio')
            (source/'metadata.json').write_text(json.dumps({'file_id':'fixture','audio_sha256':audio}))
            (target/'snapshot').mkdir(parents=True)
            for name in pilot.INPUTS:(target/'snapshot'/name).write_bytes((source/name).read_bytes())
            (target/'source-hashes.json').write_text(json.dumps({n:pilot.digest(source/n) for n in pilot.INPUTS}))
            (target/'run-1').mkdir()
            with self.assertRaisesRegex(ValueError,'Incomplete'):
                pilot.run_record(source,target,{}, {},{'Alice'})
            (source/'transcript.json').write_text('changed')
            with self.assertRaisesRegex(ValueError,'Source changed'):
                pilot.run_record(source,target,{}, {},{'Alice'})

    def test_admission_is_bounded_and_policy_changes_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prod=root/'production';prod.mkdir();shadow=root/'shadow'
            base=root/'base.json';candidate=root/'candidate.json'
            base.write_text(json.dumps({'speakers':[self.profiles[0]]}));candidate.write_text(json.dumps({'speakers':self.profiles}))
            cfg={'mode':'shadow','max_recordings':2,'since':'2026-10-28','workspace':str(shadow),
                 'baseline_registry':str(base),'candidate_registry':str(candidate),'allowed_names':['Alice','Bob'],
                 'registry_sha256':{'baseline':pilot.digest(base),'candidate':pilot.digest(candidate)},
                 'implementation_sha256':{name:pilot.digest(Path(pilot.__file__).parent/name) for name in ('speaker_shadow_pilot.py','speaker_turn_identity.py')}}
            with pipeline.database(prod) as db:
                for i,(date,title) in enumerate([('2026-10-27','SMART older'),('2026-10-28','Personal'),('2026-10-28','SMART A'),('2026-10-28','FMDS B'),('2026-10-28','SMART C')]):
                    folder=prod/'meetings'/str(i);folder.mkdir(parents=True)
                    (folder/'metadata.json').write_text(json.dumps({'file_id':str(i),'date':date+'T10:00:00-04:00','title':title}))
                    db.execute("insert into recordings(id,folder,stage,first_seen) values (?,?,?,?)",(str(i),str(i),'complete',i))
            with patch.object(pilot,'run_record',return_value={'human_review':'required'}) as compare:
                r=pilot.run_pending(prod,cfg);self.assertEqual(r['admitted'],['2','3']);self.assertEqual(compare.call_count,2)
                self.assertEqual(pilot.run_pending(prod,cfg)['admitted'],['2','3'])
                with self.assertRaisesRegex(ValueError,'policy changed'):
                    pilot.run_pending(prod,{**cfg,'since':'2026-10-29'})
            candidate.write_text('changed')
            with self.assertRaisesRegex(ValueError,'registry changed'):pilot.run_pending(prod,cfg)

    def test_publishing_mode_is_rejected(self):
        with self.assertRaises(ValueError):pilot.run_pending(Path('/unused'),{'mode':'publish','max_recordings':2})

    def test_pilot_failure_is_visible_without_reclassifying_intake(self):
        from plaud_pipeline_test import PipelineTest
        fixture=PipelineTest();fixture.setUp()
        try:
            fixture.cfg['speaker_pilot']={'mode':'shadow'}
            with patch.object(pilot,'run_pending',side_effect=ValueError('injected failure')):
                result=fixture.sync()
            self.assertEqual(result['complete'],1)
            self.assertEqual(result['failed'],0)
            self.assertEqual(result['speaker_pilot']['status'],'failed')
        finally:fixture.tearDown()

    def test_status_process_fails_on_pilot_error_and_restored_control_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);data={'at':datetime.now(timezone.utc).isoformat(),'failed':0,'quarantined':0,
                              'speaker_pilot':{'status':'failed'}}
            command=[sys.executable,str(Path(pipeline.__file__)),'status','--workspace',str(p)]
            (p/'freshness.json').write_text(json.dumps(data))
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,1)
            data['speaker_pilot']['status']='awaiting_new_recordings'
            (p/'freshness.json').write_text(json.dumps(data))
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)


if __name__=='__main__':unittest.main()
