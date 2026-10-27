import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from plaud_client import Plaud4Client, PlaudError, validate_url


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'session.json'
        self.path.write_text(json.dumps({'token':'fixture-access','tokenType':'Bearer','workspace_id':'fixture-ws',
                                        'expiresAt':(time.time()+3600)*1000,'refreshToken':'fixture-refresh'}))
        self.path.chmod(0o600)
        self.dns=patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]);self.dns.start()
        self.client=Plaud4Client(self.path)
    def tearDown(self):self.dns.stop();self.temp.cleanup()
    def test_refresh_persists_rotated_session_without_printing_it(self):
        self.client._request=lambda *a,**k:{'status':0,'data':{'workspace_token':'fixture-new','expires_in':3600,
                                                           'refresh_token':'fixture-rotated'}}
        self.client.refresh()
        self.assertEqual(json.loads(self.path.read_text())['token'],'fixture-new')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_refresh_rejection_keeps_existing_session(self):
        old=self.path.read_bytes()
        self.client._request=lambda *a,**k:{'status':-420}
        with self.assertRaises(PlaudError):self.client.refresh()
        self.assertEqual(self.path.read_bytes(),old)
    def test_pages_folders_deduplicate_stable_recording_id(self):
        replies=[{'items':[{'node_id':'folder','system_folder_type':1}], 'next_cursor':None},
                 {'items':[{'file_id':'one','created_at_show_ms':1}], 'next_cursor':'next'},
                 {'items':[{'file_id':'one','created_at_show_ms':1},{'file_id':'two','created_at_show_ms':2}], 'next_cursor':None}]
        paths=[]
        def get(path):paths.append(path);return replies.pop(0)
        self.client.get=get
        self.assertEqual({x['id'] for x in self.client.list_files()},{'one','two'})
        self.assertIn('cursor=next',paths[-1])
    def test_repeated_cursor_fails_instead_of_claiming_full_coverage(self):
        self.client.get=lambda path:{'items':[],'next_cursor':'repeated'}
        with self.assertRaises(PlaudError):self.client.list_files()
    def test_private_download_and_wrong_api_host_refused(self):
        with self.assertRaises(PlaudError):validate_url('https://attacker.example/',api=True)
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(PlaudError):validate_url('https://public.example/audio')


if __name__=='__main__':unittest.main()
