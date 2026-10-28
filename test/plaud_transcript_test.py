import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from plaud_mcp import PlaudMcpClient,PlaudError
from plaud_transcript import timing_report

def page(offset=0,cursor=None):
    return {'file_id':'f_fixture','block':'transaction','total':2,'offset':offset,'returned':1,'next_cursor':cursor,
            'segments':[{'content':'Fixture words.','start_time':offset*10000,'end_time':(offset+1)*10000,'speaker':'Speaker 1'}]}
class RawTranscriptTest(unittest.TestCase):
    def test_complete_raw_transcript_pagination_and_diagnostic_overlap(self):
        class Rpc:
            def call(self,name,args):
                assert name=='get_transcript' and args['block']=='transaction'
                return page(1) if args.get('cursor') else page(0,'next')
        raw=PlaudMcpClient({},rpc=Rpc()).transcript('f_fixture')
        report=timing_report(raw,[{'start':0,'end':5,'speaker':'A'},{'start':5,'end':10,'speaker':'B'}])
        self.assertEqual(report['segments'],2);self.assertEqual(report['median_segment_seconds'],10)
        self.assertEqual(report['overlapping_multiple_local_speakers'],1)
        self.assertFalse(report['automatic_asr_replacement_enabled'])
    def test_truncated_cyclic_changed_identity_and_shifted_pages_refuse_completion(self):
        for kind in ('truncated','cycle','changed','identity','offset'):
            class Rpc:
                def call(self,name,args):
                    x=page(0,'next')
                    if kind=='truncated':x['next_cursor']=None
                    if args.get('cursor'):
                        if kind=='changed':x=page(1);x['total']=3
                        if kind=='identity':x=page(1);x['file_id']='other'
                        if kind=='offset':x=page(0)
                    return x
            with self.assertRaises(PlaudError):PlaudMcpClient({'max_pages':3},rpc=Rpc()).transcript('f_fixture')
    def test_polished_and_invalid_timing_are_not_raw_alignment(self):
        raw={'file_id':'fixture','block':'transaction','segments':page()['segments']}
        for start,end in ((True,10),(0,float('nan')),(5,2),(-1,4)):
            value=copy.deepcopy(raw);value['segments'][0].update(start_time=start,end_time=end)
            with self.assertRaises(ValueError):timing_report(value)
        raw['block']='transaction_polish'
        with self.assertRaises(ValueError):timing_report(raw)
if __name__=='__main__':unittest.main()
