import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from diarize_sherpa import build_turns, render_outputs


class WordAttribution(unittest.TestCase):
    def test_switch_inside_whisper_segment_and_silence(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'transcript.json'
            p.write_text(json.dumps({'segments':[{'start':0,'end':5,'text':'Alice yes Bob silence', 'words':[
                {'start':0,'end':1,'word':'Alice'}, {'start':1,'end':2,'word':' yes'},
                {'start':2,'end':3,'word':' Bob'}, {'start':4,'end':5,'word':' silence'}]}]}))
            turns=build_turns([{'start':0,'end':2,'speaker':'SPEAKER_00'},
                               {'start':2,'end':3,'speaker':'SPEAKER_01'}],str(p))
            self.assertEqual([x[0] for x in turns],['SPEAKER_00','SPEAKER_01','UNASSIGNED'])
            self.assertEqual(''.join(t for turn in turns for t in turn[2]),'Alice yes Bob silence')
            render_outputs(Path(td),'out',[],[],{},[('UNASSIGNED',4,['Unknown speech.'])],0,'fixture')
            self.assertIn('UNASSIGNED', (Path(td)/'out.speakers.txt').read_text())
    def test_no_diarization_never_invents_identity(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.json';p.write_text(json.dumps({'segments':[{'start':0,'end':1,'text':'Hello'}]}))
            self.assertEqual(build_turns([],str(p))[0][0],'UNASSIGNED')


if __name__=='__main__':unittest.main()
