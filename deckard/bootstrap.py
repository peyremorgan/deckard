"""Stream a standard-library worker into RAM; never upload a remote file."""
import base64
import hashlib
import json
from pathlib import Path
import shlex

# Compatible with Python 2.7 and 3. Read the source frame from the same buffered
# stream the worker will use for RPC, preserving any read-ahead bytes.
LOADER = '''import sys,os,json,base64,types,hashlib
sys.dont_write_bytecode=True
s=getattr(sys.stdin,'buffer',sys.stdin)
n=int(s.readline())
if n<1 or n>2097152: raise ValueError('invalid Deckard source frame')
parts=[]
while n:
 b=s.read(n)
 if not b: raise EOFError('incomplete Deckard worker')
 parts.append(b);n-=len(b)
p=json.loads(b''.join(parts).decode('ascii'))
c=base64.b64decode(p['core']);w=base64.b64decode(p['worker'])
m=types.ModuleType('duplicate_core');m.__file__='<deckard-core>'
sys.modules['duplicate_core']=m
exec(compile(c,'<deckard-core>','exec'),m.__dict__)
m.SOURCE_SHA256=hashlib.sha256(c).hexdigest()
g={'__name__':'__main__','__file__':'<deckard-worker>'}
exec(compile(w,'<deckard-worker>','exec'),g)
'''


def payload():
    folder = Path(__file__).parent
    core = (folder / 'engine.py').read_bytes()
    worker = (folder / 'worker.py').read_bytes()
    data = json.dumps({'core': base64.b64encode(core).decode('ascii'),
                       'worker': base64.b64encode(worker).decode('ascii')},
                      separators=(',', ':')).encode('ascii')
    return str(len(data)).encode('ascii') + b'\n' + data, hashlib.sha256(core).hexdigest()


def remote_command(args):
    tail = ' '.join(shlex.quote(str(arg)) for arg in args)
    invoke = '-E -S -B -u -c ' + shlex.quote(LOADER) + ' ' + tail
    script = ('if command -v python3 >/dev/null 2>&1; then exec python3 ' + invoke +
              '; elif command -v python >/dev/null 2>&1; then exec python ' + invoke +
              '; else echo "Deckard needs an existing Python 3 or Python 2.7 interpreter on this host" >&2; exit 127; fi')
    return 'exec sh -c ' + shlex.quote(script)
