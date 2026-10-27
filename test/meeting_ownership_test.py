"""Ownership conflicts must fail before capture; shared receipts govern retries."""
import copy,json,shutil,sys,uuid
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from supported_pipeline_test import DeliveryTest,Rpc
import openbrain_delivery as d

class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.t=DeliveryTest();self.t.setUp();self.addCleanup(self.t.doCleanups)
    def owner(self):
        self.t.rpc.call('meeting_ownership',{'recording_id':'recording-fixture'})
        return self.t.rpc.ownership['recording-fixture']
    def test_native_owner_blocks_all_capture(self):
        self.owner()['owner']='native';self.t.enqueue()
        self.assertEqual(self.t.drain()['failed'],1);self.assertEqual(self.t.rpc.calls,0)
    def test_lookup_failure_does_not_fall_back(self):
        class Broken(Rpc):
            def call(self,name,args):
                if name=='meeting_ownership':return {'isError':True}
                return super().call(name,args)
        self.t.rpc=Broken();self.t.enqueue()
        self.assertEqual(self.t.drain()['failed'],1);self.assertEqual(self.t.rpc.calls,0)
    def test_second_workspace_replay_shares_existing_revision(self):
        self.t.enqueue();self.assertEqual(self.t.drain()['delivered'],1)
        other=self.t.root/'other';other.mkdir()
        d.enqueue(other,self.t.folder)
        self.assertEqual(d.drain(other,self.t.cfg,rpc=self.t.rpc)['delivered'],1)
        self.assertEqual(len(self.t.rpc.thoughts),1)
        self.assertEqual(self.owner()['revision'],1)
    def test_new_workspace_cannot_overwrite_newer_remote_summary(self):
        self.t.enqueue();self.t.drain()
        old=self.t.root/'old';shutil.copytree(self.t.folder,old)
        self.t.save('A newer supported revision.');self.t.enqueue();self.t.drain()
        calls=self.t.rpc.calls;other=self.t.root/'other';other.mkdir();d.enqueue(other,old)
        self.assertEqual(d.drain(other,self.t.cfg,rpc=self.t.rpc)['failed'],1)
        self.assertEqual(self.t.rpc.calls,calls)
    def test_lost_ack_request_is_not_reissued_after_epoch_change(self):
        self.t.enqueue();self.t.rpc.drop=True;self.t.drain()
        self.owner()['epoch']=2
        self.assertEqual(self.t.drain()['failed'],1);self.assertEqual(self.t.rpc.calls,1)
    def test_first_local_capture_reconciles_all_native_predecessors(self):
        parents=[str(uuid.uuid4()),str(uuid.uuid4())]
        for p in parents:self.t.rpc.thoughts[p]={'content':'Fixture native section','current':p}
        self.owner()['prior_thought_ids']=parents
        self.t.enqueue();self.assertEqual(self.t.drain()['delivered'],1)
        saved=self.owner()['thought_id']
        self.assertTrue(all(self.t.rpc.thoughts[p]['current']==saved for p in parents))
    def test_unknown_remote_revision_cannot_be_used_as_correction_parent(self):
        self.t.enqueue();self.t.drain();self.t.save('A correction.');self.t.enqueue()
        self.owner()['thought_id']=str(uuid.uuid4())
        self.assertEqual(self.t.drain()['failed'],1);self.assertEqual(self.t.rpc.calls,1)
    def test_content_readback_without_shared_receipt_never_acknowledges(self):
        class Wrong(Rpc):
            def call(self,name,args):
                result=super().call(name,args)
                if name=='capture_thought':self.ownership[args['meeting']['recording_id']]['content_sha256']='0'*64
                return result
        self.t.rpc=Wrong();self.t.enqueue();self.assertEqual(self.t.drain()['failed'],1)
        self.assertEqual(d.status(self.t.root)['delivered'],0)

if __name__=='__main__':unittest.main()
