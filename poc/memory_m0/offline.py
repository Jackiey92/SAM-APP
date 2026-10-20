"""Run inside a network namespace; log and reject every Python INET connection."""
import json, os, sys, socket, runpy
from pathlib import Path
root=Path(__file__).resolve().parent
assert os.environ.get('HF_HUB_OFFLINE')=='1'
log=root/'network-audit.jsonl'
log.write_text(json.dumps({'network_namespace':os.readlink('/proc/self/ns/net'),'interfaces':Path('/proc/net/dev').read_text(),'routes':Path('/proc/net/route').read_text()})+'\n')
def audit(event,args):
    if event=='socket.connect':
        sock,address=args
        with log.open('a') as f: f.write(json.dumps({'event':event,'address':str(address),'family':int(sock.family)})+'\n')
        if sock.family in (socket.AF_INET,socket.AF_INET6): raise RuntimeError('Offline audit: network connection forbidden')
sys.addaudithook(audit)
runpy.run_path(str(root/'run.py'),run_name='__main__')
with log.open('a') as f: f.write(json.dumps({'completed':True})+'\n')
