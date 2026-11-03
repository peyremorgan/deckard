"""Mac-side bounded SSH protocol. No source-file data crosses this channel."""
import base64,hashlib,json,os,posixpath,select,shlex,subprocess,threading,time
from types import SimpleNamespace
from . import engine as core

MAX_FRAME=16*1048576


class Session:
    def __init__(self,command,core_hash,expected_root,timeout=180,bootstrap=None,environment=None):
        self.owner=threading.current_thread().ident
        self.command,self.timeout=command,timeout
        self.closed=False
        self.sequence=0
        self.buffer=bytearray()
        self.errors=bytearray()
        self.last_counts={}
        self.busy=False
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0,env=environment)
        os.set_blocking(self.process.stdin.fileno(),False)
        self.stderr_thread=threading.Thread(target=self._stderr,daemon=True)
        self.stderr_thread.start()
        try:
            if bootstrap:
                self._write(bootstrap)
            with core.span('ssh.connect'):
                self.hello=self._read()
            if self.hello.get('event')!='hello' or self.hello.get('protocol')!=1:
                raise core.SourceUnavailable('unexpected worker greeting')
            if self.hello.get('core_sha256')!=core_hash:
                raise ValueError('worker source integrity check failed')
            if expected_root is not None and self.hello.get('root_base64')!=expected_root:
                raise ValueError('worker canonical source root differs from configuration')
            if core.PROFILE is not None and self.hello.get('initial_metrics'):
                core.PROFILE.emit(dict(self.hello['initial_metrics'],type='source_metrics',event='source_start'))
        except BaseException:
            self.close()
            raise

    def _write(self,data):
        remaining=memoryview(data)
        deadline=time.monotonic()+self.timeout
        while remaining:
            if core.CANCEL_EVENT is not None and core.CANCEL_EVENT.is_set():
                raise KeyboardInterrupt()
            if self.closed or time.monotonic()>=deadline:
                raise core.SourceUnavailable('worker send timed out or connection closed')
            try:
                _,ready,_=select.select([],[self.process.stdin],[],1)
                if not ready:continue
                try:written=os.write(self.process.stdin.fileno(),remaining[:65536])
                except BlockingIOError:continue
                if not written:raise BrokenPipeError('short protocol write')
                remaining=remaining[written:]
                core.count('wire_sent_bytes',written)
            except (OSError,ValueError) as exc:
                raise core.SourceUnavailable('SSH write failed: '+str(exc))

    def _stderr(self):
        while True:
            try:chunk=self.process.stderr.read(1024)
            except (ValueError,OSError):return
            if not chunk:return
            self.errors.extend(chunk)
            if len(self.errors)>4096:del self.errors[:-4096]

    def _read(self):
        deadline=time.monotonic()+self.timeout
        while True:
            if core.CANCEL_EVENT is not None and core.CANCEL_EVENT.is_set():
                raise KeyboardInterrupt()
            offset=self.buffer.find(b'\n')
            if offset>=0:
                line=bytes(self.buffer[:offset]);del self.buffer[:offset+1]
                try:return core.call('wire.decode',json.loads,line)
                except (ValueError,UnicodeError) as exc:
                    raise core.SourceUnavailable('invalid worker protocol: '+str(exc))
            if self.closed:raise core.SourceUnavailable('source connection closed')
            if time.monotonic()>=deadline:
                raise core.SourceUnavailable('NAS worker heartbeat timed out')
            try:
                with core.span('wire.receive_wait'):
                    ready,_,_=select.select([self.process.stdout],[],[],min(1,max(0,deadline-time.monotonic())))
                    if not ready:continue
                    data=os.read(self.process.stdout.fileno(),65536)
            except (OSError,ValueError) as exc:
                raise core.SourceUnavailable('SSH read failed: '+str(exc))
            if not data:
                self.stderr_thread.join(.1)
                reason=bytes(self.errors).decode('utf-8','replace').strip()
                raise core.SourceUnavailable('NAS worker disconnected'+(': '+reason if reason else ''))
            self.buffer.extend(data)
            core.count('wire_received_bytes',len(data))
            if len(self.buffer)>MAX_FRAME:raise core.SourceUnavailable('worker frame exceeds limit')

    def _stats(self,frame,request):
        if frame.get('request')==request:
            for key,value in frame.get('counts',{}).items():
                previous=self.last_counts.get(key,0)
                if value>previous:core.count(key,value-previous)
            self.last_counts=dict(frame.get('counts',{}))
        profile=core.PROFILE
        if profile is not None:
            pid=frame.get('worker_pid',self.hello['worker_pid'])
            for key in ('source_cpu_seconds','source_elapsed_seconds','source_peak_rss_bytes'):
                if key in frame:profile.gauge('nas_%s_%s'%(pid,key),frame[key])
            if frame.get('event')=='source_metrics':
                profile.emit(dict(frame,type='source_metrics'))

    def request(self,op,**values):
        self.busy=True
        try:
            for frame in self._request(op,**values):yield frame
        finally:self.busy=False

    def _request(self,op,**values):
        started=time.monotonic()
        label=op+('_'+values['stage'] if op in ('hash','hashes') else '')
        self.sequence+=1
        request=self.sequence
        self.last_counts={}
        message=dict(values,op=op,request=request)
        encoded=(core.call('wire.encode',json.dumps,message,separators=(',',':'),ensure_ascii=True)+'\n').encode('ascii')
        if len(encoded)>MAX_FRAME:raise ValueError('request exceeds protocol limit')
        try:
            core.call('wire.send',self._write,encoded)
        except (BrokenPipeError,OSError,ValueError) as exc:
            raise core.SourceUnavailable('SSH request failed: '+str(exc))
        error=None
        while True:
            frame=self._read()
            event=frame.get('event')
            if event in ('progress','source_metrics'):
                self._stats(frame,request)
                continue
            if frame.get('request')!=request:
                raise core.SourceUnavailable('worker response does not match request')
            if event=='done':
                self._stats(frame,request)
                core.count('rpc_completed_'+label)
                core.count('rpc_seconds_'+label,time.monotonic()-started)
                core.count('nas_service_seconds_'+label,frame.get('job_seconds',0))
                core.count('nas_job_cpu_seconds_'+label,frame.get('job_cpu_seconds',0))
                if op=='directories':core.count('rpc_directories_in_batches',len(values['jobs']))
                if op=='prefix_hashes':core.count('rpc_prefix_files_in_batches',len(values['rows']))
                if op=='hashes':core.count('rpc_hashes_'+values['stage']+'_files_in_batches',len(values['rows']))
                if op=='validate':core.count('rpc_validation_files_in_batches',len(values['rows']))
                if error:
                    if error['event']=='source_error':raise OSError(error['message'])
                    raise RuntimeError('NAS worker: '+error['message'])
                return
            if event in ('source_error','worker_error'):
                error=frame
            else:yield frame

    def one(self,op,**values):
        value=None
        for frame in self.request(op,**values):
            if frame.get('event') in ('result','hash_result','validation'):value=frame
            else:raise RuntimeError('unexpected response '+str(frame.get('event')))
        if value is None:raise core.SourceUnavailable('worker response missing result')
        return value

    def close(self):
        if self.closed:return
        # Preserve the tail of short phases without delaying cancellation of
        # an active read/hash. This request performs no source I/O.
        cancelled=core.CANCEL_EVENT is not None and core.CANCEL_EVENT.is_set()
        if not cancelled and not self.busy and self.process.poll() is None and hasattr(self,'hello'):
            previous=self.timeout
            self.timeout=min(previous,1)
            try:list(self.request('metrics'))
            except (OSError,ValueError,RuntimeError):pass
            finally:self.timeout=previous
        self.closed=True
        try:self.process.stdin.close()
        except (OSError,ValueError):pass
        if self.process.poll() is None:self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=5)
        self.stderr_thread.join(.2)
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
            try:stream.close()
            except (OSError,ValueError):pass


class ProtocolSource:
    """Bounded worker operations shared by the streamed source adapter."""
    def root_stat(self,root):
        if core.raw(root).rstrip(b'/')!=self.root.rstrip(b'/'):raise ValueError('unexpected source root')
        return SimpleNamespace(st_dev=int(self.info['device']),st_ino=int(self.info['inode']))

    def inventory_worker(self,root,device):
        batched=self.inventory_batch_worker(root,device)
        def work(job,emit,stop):
            return batched([job],emit,stop)
        return work

    def inventory_batch_worker(self,root,device):
        def work(jobs,emit,stop):
            if not 1<=len(jobs)<=16 or len({job[0] for job in jobs})!=len(jobs):
                raise ValueError('directory batch must contain 1..16 unique jobs')
            completion=None
            position=0
            with core.span('rpc.directories',os.path.join(root,jobs[0][1]),'directories=%s'%len(jobs)):
                for frame in self.session().request('directories',jobs=[{'directory_id':ident,'path_base64':core.b64(path)} for ident,path in jobs]):
                    if stop.is_set():return
                    if position>=len(jobs):raise RuntimeError('extra directory response')
                    ident,path=jobs[position]
                    records=[tuple([row[0],base64.b64decode(row[1])]+row[2:]) for row in frame['records']]
                    parent=base64.b64decode(frame['parent_base64'])
                    if frame['directory_id']!=ident or parent!=path:raise RuntimeError('directory response identity mismatch')
                    if frame['event']=='batch':event=('batch',ident,parent,records)
                    elif frame['event']=='directory_done':event=('directory_done',ident,frame['failed'],parent,records)
                    else:raise RuntimeError('unexpected directory event')
                    # JSON arrays must become the native fingerprint tuples used
                    # by SQLite cache reconciliation.
                    records[:]=[tuple(list(row[:2])+[tuple(row[2])]+list(row[3:])) if row[0]=='file' else row for row in records]
                    if frame['event']=='directory_done':
                        position+=1
                        # Each earlier directory is fully streamed and can be
                        # checkpointed independently. Hold the last completion
                        # until protocol done/stats have also been consumed.
                        if position==len(jobs):completion=event
                        elif not emit(event):return
                    elif not emit(event):return
                if completion is None or position!=len(jobs):raise core.SourceUnavailable('directory batch completion missing')
                emit(completion)
        return work

    @staticmethod
    def row(row):
        result=dict(row)
        result['path_base64']=core.b64(result.pop('path'))
        return result

    def hash_file(self,root,row,stage,block):
        with core.span('rpc.hash',os.path.join(root,core.raw(row['path'])),stage):
            result=self.session().one('hash',row=self.row(row),stage=stage,block=block)
            return result['digest'],result['full']

    def hash_batch_worker(self,root,stage,block):
        # One oversized full read stays a standalone job. Bounded jobs use
        # the streamed protocol, including independent per-file checkpoints.
        op = 'prefix_hashes' if stage=='prefix' else 'hashes'
        fallback = core.local_hash_batch_worker(root,stage,block)
        def work(rows,emit,stop):
            if not 1<=len(rows)<=16 or len({row['id'] for row in rows})!=len(rows):
                raise ValueError('hash batch must contain 1..16 unique files')
            if stage=='full' and len(rows)==1 and rows[0]['size']>64*1048576:
                return fallback(rows,emit,stop)
            position=0
            completion=None
            with core.span('rpc.'+op,os.path.join(root,core.raw(rows[0]['path'])),stage+' files=%s'%len(rows)):
                for frame in self.session().request(op,rows=[self.row(row) for row in rows],stage=stage,block=block):
                    if stop.is_set():return
                    if position>=len(rows) or frame.get('event')!='hash_result':
                        raise RuntimeError('unexpected hash batch response')
                    row=rows[position]
                    if frame.get('file_id')!=row['id']:
                        raise RuntimeError('hash batch result identity mismatch')
                    value,full,error=frame.get('digest'),frame.get('full'),frame.get('error')
                    if error is None:
                        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                            raise RuntimeError('invalid batch hash digest')
                        is_full = stage=='full' or row['size']<=(core.SAMPLE if stage=='prefix' else 3*core.SAMPLE)
                        if full!=(value if is_full else None):
                            raise RuntimeError('invalid batch hash full-digest flag')
                    elif not isinstance(error,str) or value is not None or full is not None:
                        raise RuntimeError('invalid batch hash error response')
                    event=('hash_done',row,value,full,error)
                    position+=1
                    # Commit complete earlier files independently; retain the
                    # last file until done/stats confirm the RPC is complete.
                    if position==len(rows):completion=event
                    elif not emit(event):return
                if completion is None or position!=len(rows):
                    raise core.SourceUnavailable('hash batch ended without all results')
                emit(completion)
        return work

    def validate_rows(self,root,rows):
        with core.span('rpc.report_validation'):
            response=self.session().one('validate',rows=[self.row({key:row[key] for key in ('id','path','size','mtime','ctime','dev','ino')}) for row in rows])
        errors=response['errors']
        if [row['id'] for row in rows]!=[value[0] for value in errors]:
            raise RuntimeError('report validation response identity mismatch')
        if any(error is not None and not isinstance(error,str) for _,error in errors):
            raise RuntimeError('invalid report validation error')
        for row,(_,error) in zip(rows,errors):yield row,error

    def probe_directory(self,root,path):
        return self.session().one('probe_directory',path_base64=core.b64(path))['result']

    def probe_hash(self,root,path,limit,block):
        return self.session().one('probe_hash',path_base64=core.b64(path),limit=limit,block=block)['result']

    def choose_probe_file(self,root,value):
        path=core.raw(value)
        if path.startswith(root+b'/'):path=path[len(root)+1:]
        elif path.startswith(b'/'):raise ValueError('probe file must be inside the NAS root')
        info=self.session().one('stat',path_base64=core.b64(path))['result']
        return path,info['size']

    def cancel_threads(self,threads):
        owners={thread.ident for thread in threads}
        with self.lock:sessions=[session for session in self.sessions if session.owner in owners]
        for session in sessions:session.close()

    def close(self):
        with self.lock:sessions=list(self.sessions)
        for session in sessions:session.close()

    def install(self):
        values={'source_root_stat':self.root_stat,'inventory_worker':self.inventory_worker,
                'inventory_batch_worker':self.inventory_batch_worker,
                'hash_file':self.hash_file,'hash_batch_worker':self.hash_batch_worker,'validate_report_rows':self.validate_rows,
                'probe_directory':self.probe_directory,'probe_hash_file':self.probe_hash,
                'choose_probe_file':self.choose_probe_file,'pool_cancel':self.cancel_threads}
        originals={name:getattr(core,name) for name in values}
        for name,value in values.items():setattr(core,name,value)
        return originals

    @staticmethod
    def restore(originals):
        for name,value in originals.items():setattr(core,name,value)
