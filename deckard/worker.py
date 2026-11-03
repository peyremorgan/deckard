#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Read-only, stateless NAS worker. Python 2.7/3. No database is opened here."""
from __future__ import print_function
import argparse,base64,contextlib,hashlib,json,os,resource,stat,sys,threading,time
import duplicate_core as core

PROTOCOL = 1
MAX_FRAME = 16 * 1048576


def unpack(value):
    return base64.b64decode(value.encode('ascii') if isinstance(value, core.text_type) else value)


def relative(value):
    value = unpack(value)
    if os.path.isabs(value) or b'\x00' in value or b'..' in value.split(b'/'):
        raise ValueError('unsafe relative source path')
    return value


class WorkerMetrics(core.Profile):
    """Use the proven sampler without writing logs or SQLite on the NAS."""
    def __init__(self, send, sample, enabled=True):
        self.send, self.enabled, self.sample_every = send, enabled, sample
        self.local, self.call_counts = threading.local(), {}
        self.lock = threading.Lock()
        self.stats, self.counters, self.active, self.gauges = {}, {}, {}, {}
        self.last_slow, self.interval = {}, 10
        self.started, self.cpu_started = core.NOW(), core.CPU()
        self.previous_time, self.previous_cpu = self.started, self.cpu_started
        self.base, self.request = {}, None
        self.operation,self.stage=None,None
        self.last_full = self.started - 10
        self.job_started, self.job_cpu = self.started, self.cpu_started

    def begin(self, request, operation=None, stage=None):
        with self.lock:
            self.base = dict(self.counters)
            self.request = request
            self.operation,self.stage=operation,stage
            self.job_started, self.job_cpu = core.NOW(), core.CPU()

    def compact(self):
        now, cpu = core.NOW(), core.CPU()
        with self.lock:
            counts = dict((key, value - self.base.get(key, 0)) for key, value in self.counters.items())
            request = self.request
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {'request': request, 'counts': counts, 'worker_pid': os.getpid(),
                'operation':self.operation,'stage':self.stage,
                'source_cpu_seconds': cpu - self.cpu_started,
                'source_elapsed_seconds': now - self.started,
                'job_seconds': now - self.job_started, 'job_cpu_seconds': cpu - self.job_cpu,
                'source_peak_rss_bytes': usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)}

    def full(self):
        result = self.compact()
        now = core.NOW()
        with self.lock:
            result['metrics'] = dict((name, dict(row, latency_buckets=list(row['latency_buckets'])))
                                     for name, row in self.stats.items())
            totals = {}
            for values in self.call_counts.values():
                for name, value in list(values.items()):
                    totals[name] = totals.get(name, 0) + value
            for name, row in result['metrics'].items():
                row['calls_total'] = totals.get(name, row['calls'])
                row['estimated_seconds'] = row['seconds'] * row['calls_total'] / max(1, row['calls'])
            result['active'] = [dict(stack[-1], elapsed_seconds=now-stack[-1]['started'])
                                for stack in self.active.values() if stack]
        if sys.platform.startswith('linux'):
            try:
                with open('/proc/self/io') as stream:
                    result['source_io'] = dict((line.split(':',1)[0],int(line.split(':',1)[1])) for line in stream)
            except (IOError, OSError):
                pass
        result['source_time_unix'] = time.time()
        result['timing_sample_every'] = self.sample_every
        result['latency_bucket_upper_seconds'] = [.0001,.001,.01,.1,1,10,60,None]
        return result

    def emit(self, record):
        # Slow-call details are delivered in periodic aggregates, not millions
        # of per-job log records. Only the controller owns durable diagnostics.
        pass


class Server(object):
    def __init__(self, root, sample, timings=True):
        self.root = core.raw(os.path.realpath(os.path.expanduser(root)))
        if not stat.S_ISDIR(os.lstat(self.root).st_mode) or core.network_filesystem(self.root):
            raise ValueError('worker requires a local source directory')
        self.device = os.lstat(self.root).st_dev
        self.write_lock = threading.Lock()
        self.stop = threading.Event()
        self.metrics = WorkerMetrics(self.send, sample, timings)
        core.PROFILE = self.metrics

    def send(self, record):
        data = core.call('wire.encode',json.dumps,record,ensure_ascii=True,separators=(',', ':')) + '\n'
        if len(data) > MAX_FRAME:
            raise ValueError('protocol frame exceeds limit')
        try:
            core.call('wire.send',self.write,data)
        except (IOError, OSError):
            # This process has no durable state. Exit all threads when the Mac
            # disappears instead of leaving an orphan hashing a large file.
            os._exit(0)

    def write(self,data):
        with self.write_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def heartbeat(self):
        while not self.stop.wait(2):
            if self.metrics.request is None:
                continue
            if core.NOW() - self.metrics.last_full >= 10:
                record = self.metrics.full()
                self.metrics.last_full = core.NOW()
                record['event'] = 'source_metrics'
            else:
                record = self.metrics.compact()
                record['event'] = 'progress'
            self.send(record)

    def row(self, value):
        value = dict(value)
        value['path'] = relative(value.pop('path_base64'))
        return value

    def records(self, records):
        return [[row[0], core.b64(row[1])] + list(row[2:]) for row in records]

    def dispatch(self, message):
        op, request = message['op'], message['request']
        if op in ('directory','directories'):
            values = [message] if op == 'directory' else message['jobs']
            if not 1 <= len(values) <= 16:
                raise ValueError('directory batch must contain 1..16 jobs')
            jobs = [(int(job['directory_id']),relative(job['path_base64'])) for job in values]
            if len(set(job[0] for job in jobs)) != len(jobs):
                raise ValueError('duplicate directory IDs in batch')
            def emit(event):
                if event[0] == 'batch':
                    _, ident, parent, records = event
                    self.send({'event':'batch','request':request,'directory_id':ident,
                               'parent_base64':core.b64(parent),'records':self.records(records)})
                else:
                    _, ident, failed, parent, records = event
                    self.send({'event':'directory_done','request':request,'directory_id':ident,
                               'failed':failed,'parent_base64':core.b64(parent),'records':self.records(records)})
                return True
            work = core.inventory_worker(self.root, self.device)
            for job in jobs:
                if self.stop.is_set():break
                work(job,emit,self.stop)
        elif op in ('prefix_hashes', 'hashes'):
            stage = message.get('stage','prefix')
            if op == 'prefix_hashes' and stage != 'prefix':
                raise ValueError('prefix_hashes supports only the prefix stage')
            if stage not in ('prefix','sample','full'):
                raise ValueError('invalid hash stage')
            block = int(message['block'])
            if not 4096 <= block <= 8*1048576:
                raise ValueError('invalid hash read block')
            values = message['rows']
            if not 1 <= len(values) <= 16:
                raise ValueError('prefix hash batch must contain 1..16 rows')
            rows = [self.row(value) for value in values]
            if len(set(row['id'] for row in rows)) != len(rows):
                raise ValueError('duplicate file IDs in prefix hash batch')
            if stage == 'full' and sum(row['size'] for row in rows)>64*1048576:
                raise ValueError('full-hash batch exceeds byte limit')
            for row in rows:
                if self.stop.is_set():break
                value,full,error = None,None,None
                try:value,full=core.hash_file(self.root,row,stage,block)
                except (IOError,OSError) as exc:error=core.display(str(exc))
                self.send({'event':'hash_result','request':request,'file_id':row['id'],
                           'digest':value,'full':full,'error':error})
        elif op == 'hash':
            stage = message['stage']
            block = int(message['block'])
            if stage not in ('prefix','sample','full') or not 4096 <= block <= 8*1048576:
                raise ValueError('invalid hash request')
            digest, full = core.hash_file(self.root,self.row(message['row']),stage,block)
            self.send({'event':'hash_result','request':request,'digest':digest,'full':full})
        elif op == 'validate':
            rows = [self.row(row) for row in message['rows']]
            if not 1<=len(rows)<=1024 or len(set(row['id'] for row in rows))!=len(rows):
                raise ValueError('validation batch requires 1..1024 unique rows')
            errors = []
            for row,error in core.validate_report_rows(self.root,rows):
                errors.append([row['id'],error])
            self.send({'event':'validation','request':request,'errors':errors})
        elif op == 'probe_directory':
            result=core.probe_directory(self.root,relative(message['path_base64']))
            self.send({'event':'result','request':request,'result':result})
        elif op == 'probe_hash':
            limit,block=int(message['limit']),int(message['block'])
            if not (0<limit<=1024*1048576 and 4096<=block<=8*1048576):raise ValueError('invalid probe budget')
            result=core.probe_hash_file(self.root,relative(message['path_base64']),limit,block)
            self.send({'event':'result','request':request,'result':result})
        elif op == 'stat':
            path=os.path.join(self.root,relative(message['path_base64']))
            st=os.lstat(path)
            self.send({'event':'result','request':request,'result':{
                'size':st.st_size,'mode':st.st_mode,'device':str(st.st_dev),'inode':str(st.st_ino)}})
        elif op == 'metrics':
            record=self.metrics.full()
            record['event']='source_metrics'
            self.metrics.last_full=core.NOW()
            self.send(record)
        else:
            raise ValueError('unknown read-only operation')

    def run(self):
        st=os.lstat(self.root)
        code_hash = getattr(core, 'SOURCE_SHA256', None)
        if code_hash is None:
            with open(core.__file__.replace('.pyc','.py'),'rb') as stream:
                code_hash=hashlib.sha256(stream.read()).hexdigest()
        self.send({'event':'hello','agentless':True,'protocol':PROTOCOL,'implementation':core.IMPLEMENTATION,
                   'root_base64':core.b64(self.root),'device':str(st.st_dev),'inode':str(st.st_ino),
                   'worker_pid':os.getpid(),'core_sha256':code_hash,'python':sys.version,
                   'initial_metrics':self.metrics.full(),'inventory_batch_limit':16,'prefix_hash_batch_limit':16,'hash_batch_limit':16,'validation_batch_limit':1024,'cpu_clock':'getrusage' if not hasattr(time,'process_time') else 'process_time'})
        thread=threading.Thread(target=self.heartbeat,name='source-heartbeat')
        thread.daemon=True;thread.start()
        while True:
            line=core.call('wire.await_request',sys.stdin.readline,MAX_FRAME+1)
            if not line:break
            if len(line)>MAX_FRAME:raise ValueError('request too large')
            message=core.call('wire.decode',json.loads,line)
            request=message['request']
            self.metrics.begin(request,message.get('op'),message.get('stage'))
            try:
                self.dispatch(message)
            except (IOError,OSError) as exc:
                self.send({'event':'source_error','request':request,'message':str(exc)})
            except Exception as exc:
                self.send({'event':'worker_error','request':request,'message':str(exc)})
            if core.NOW() - self.metrics.last_full >= 10:
                record = self.metrics.full()
                self.metrics.last_full = core.NOW()
                record['event'] = 'source_metrics'
                self.send(record)
            final=self.metrics.compact()
            final['event']='done'
            self.send(final)
            with self.metrics.lock:self.metrics.request=None
        self.stop.set()
        return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root-base64',required=True)
    parser.add_argument('--timing-sample',type=int,default=16)
    parser.add_argument('--no-timings',action='store_true')
    args=parser.parse_args()
    if args.timing_sample<1:parser.error('timing sample must be positive')
    try:
        sys.exit(Server(unpack(args.root_base64),args.timing_sample,not args.no_timings).run())
    except (IOError,OSError,ValueError) as exc:
        print('Worker stopped: %s'%exc,file=sys.stderr)
        sys.exit(2)
