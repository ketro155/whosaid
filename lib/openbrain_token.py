"""Print a cached OpenBrain machine OAuth token; credentials stay outside Git."""
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
from plaud_client import NoRedirect
from openbrain_summary import atomic_write


def token(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("OAuth credential file must be private")
    cfg = json.loads(path.read_text())
    resource = cfg['resource']
    if resource != 'https://bqnifdpimxmmwpluzvyk.supabase.co/functions/v1/open-brain-mcp':
        raise ValueError('Unexpected OpenBrain audience')
    cache = path.with_name('openbrain-access-token.json')
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get('resource') == resource and data['expires_at'] > time.time() + 60:
            return data['access_token']
    body = urllib.parse.urlencode({'grant_type':'client_credentials',
        'client_id':cfg['client_id'],'client_secret':cfg['client_secret'],'resource':resource}).encode()
    req = urllib.request.Request(resource+'/token',data=body,
        headers={'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=25) as response:
        data = json.load(response)
    if not data.get('access_token','').startswith('ob_at') or data.get('expires_in',0) <= 60:
        raise ValueError('Invalid OAuth response')
    atomic_write(cache,json.dumps({'resource':resource,'access_token':data['access_token'],
        'expires_at':time.time()+data['expires_in']}))
    os.chmod(cache,0o600)
    return data['access_token']


if __name__ == '__main__':
    try:
        print(token(sys.argv[1]))
    except Exception:
        raise SystemExit('OpenBrain OAuth unavailable; response suppressed') from None
