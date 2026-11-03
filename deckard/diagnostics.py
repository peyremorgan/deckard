"""Read-only, bounded summaries of controller and NAS performance logs."""
import json
from pathlib import Path

TAIL_BYTES=16*1048576
NOTE=('Elapsed costs overlap across nested calls and concurrent workers. '
      'RPC time outside NAS service includes SSH, serialization, scheduling and '
      'Mac backpressure; it is not a network-latency measurement. '
      'Sampled estimates are approximate; inspect active calls during stalls.')


def read_tail(path):
    with Path(path).open('rb') as stream:
        size=stream.seek(0,2)
        start=max(0,size-TAIL_BYTES)
        stream.seek(start)
        if start:stream.readline()
        data=stream.read(TAIL_BYTES)
    rows=[]
    for line in data.splitlines():
        try:row=json.loads(line)
        except (ValueError,UnicodeError):continue
        if isinstance(row,dict) and isinstance(row.get('time_unix'),(int,float)):
            rows.append(row)
    return rows,start>0


def cost_delta(first,last):
    result=[]
    for name,new in last.get('metrics',{}).items():
        old=first.get('metrics',{}).get(name,{})
        samples=max(0,new.get('calls',0)-old.get('calls',0))
        # Calls are counted at entry and timed at completion. A long call may
        # start before the window; never erase its observed completion cost.
        calls=max(samples,new.get('calls_total',new.get('calls',0))-old.get('calls_total',old.get('calls',0)))
        seconds=max(0,new.get('seconds',0)-old.get('seconds',0))
        if not samples:continue
        buckets=[max(0,b-a) for a,b in zip(old.get('latency_buckets',[0]*8),new.get('latency_buckets',[0]*8))]
        result.append({'operation':name,'estimated_seconds':seconds*calls/samples,
                       'sampled_seconds':seconds,'calls':calls,'samples':samples,
                       'mean_sampled_ms':1000*seconds/samples,'latency_buckets':buckets,
                       'errors':max(0,new.get('errors',0)-old.get('errors',0))})
    return sorted(result,key=lambda row:row['estimated_seconds'],reverse=True)


def window(rows,cutoff,elapsed_key):
    # A PID may be reused following reconnection. Use its latest generation.
    start=0
    for i in range(1,len(rows)):
        if rows[i].get(elapsed_key,0)<rows[i-1].get(elapsed_key,0):start=i
    rows=rows[start:]
    if not rows or rows[-1]['time_unix']<cutoff:return None
    before=[r for r in rows if r['time_unix']<=cutoff]
    first=before[-1] if before else rows[0]
    last=rows[-1]
    duration=last.get(elapsed_key,0)-first.get(elapsed_key,0)
    if duration<=0:return None
    return first,last,duration


def summarize(rows,seconds=60):
    samples=[r for r in rows if r.get('type') in ('heartbeat','final','source_metrics')]
    if not samples:return {'note':NOTE,'error':'No performance samples yet; wait for two heartbeats.'}
    latest=max(r['time_unix'] for r in samples);cutoff=latest-seconds
    mac=[r for r in samples if r.get('type') in ('heartbeat','final')]
    result={'latest_sample_unix':latest,'requested_window_seconds':seconds,'note':NOTE,'nas_workers':[]}
    # Include the start record as a zero baseline for short controller runs.
    starts=[r for r in rows if r.get('type')=='start']
    if starts:mac=[dict(starts[-1],elapsed_seconds=0,cpu_seconds=0,metrics={},counters={})]+mac
    if mac:
        selected=window(mac,cutoff,'elapsed_seconds')
        if selected:
            first,last,duration=selected
            old=first.get('counters',{});new=last.get('counters',{})
            delta={k:max(0,v-old.get(k,0)) for k,v in new.items()}
            rpc={}
            for key,value in delta.items():
                if not key.startswith('rpc_completed_') or not value:continue
                op=key[len('rpc_completed_'):]
                wall=delta.get('rpc_seconds_'+op,0);service=delta.get('nas_service_seconds_'+op,0)
                rpc[op]={'completed':value,'per_second':value/duration,
                         'mean_round_trip_ms':1000*wall/value,'mean_nas_service_ms':1000*service/value,
                         'mean_outside_nas_service_ms':1000*max(0,wall-service)/value}
                if op=='directories':
                    directories=delta.get('rpc_directories_in_batches',0)
                    rpc[op].update(directories=directories,directories_per_second=directories/duration,
                                   mean_directories_per_batch=directories/value,
                                   amortized_directory_ms=1000*wall/max(1,directories))
                if op in ('prefix_hashes','hashes_sample','hashes_full','validate'):
                    key = ('rpc_prefix_files_in_batches' if op=='prefix_hashes' else
                           'rpc_validation_files_in_batches' if op=='validate' else
                           'rpc_'+op+'_files_in_batches')
                    files=delta.get(key,0)
                    rpc[op].update(files=files,files_per_second=files/duration,
                                   mean_files_per_batch=files/value,
                                   amortized_file_ms=1000*wall/max(1,files))
            hashing=[]
            for key,files in delta.items():
                if not key.startswith('hash_') or not key.endswith('_files') or not files:continue
                prefix=key[:-len('files')]
                _,stage,band=key.split('_')[:3]
                wall=delta.get(prefix+'seconds',0)
                hashing.append({'stage':stage,'size_band':band,'files':files,
                    'files_per_second':files/duration,'mean_job_ms':1000*wall/files,
                    'read_bytes':delta.get(prefix+'read_bytes',0),
                    'candidate_bytes':delta.get(prefix+'candidate_bytes',0),
                    'cpu_seconds':delta.get(prefix+'cpu_seconds',0)})
            result['mac']={'hashing_by_size':hashing,'pid':last.get('pid'),'phase':last.get('phase'),'sample_seconds':duration,
                           'cpu_percent':100*(last.get('cpu_seconds',0)-first.get('cpu_seconds',0))/duration,
                           'rates':{k:v/duration for k,v in delta.items()},'rpc':rpc,
                           'costs':cost_delta(first,last),'active':last.get('active',[]),
                           'gauges':last.get('gauges',{}),'peak_rss_bytes':last.get('peak_rss_bytes')}
    groups={}
    for row in samples:
        if row.get('type')=='source_metrics':groups.setdefault(row.get('worker_pid'),[]).append(row)
    for pid,values in groups.items():
        selected=window(values,cutoff,'source_elapsed_seconds')
        if not selected:continue
        first,last,duration=selected
        io={k:max(0,v-first.get('source_io',{}).get(k,v))/duration for k,v in last.get('source_io',{}).items()}
        result['nas_workers'].append({'pid':pid,'sample_seconds':duration,
            'cpu_percent':100*(last['source_cpu_seconds']-first['source_cpu_seconds'])/duration,
            'physical_io_bytes_per_second':{k:io[k] for k in ('read_bytes','write_bytes') if k in io},
            'costs':cost_delta(first,last),'active':last.get('active',[]),
            'peak_rss_bytes':last.get('source_peak_rss_bytes')})
    return result


def diagnose(state,seconds=60,metrics_file=None,as_json=False):
    paths=sorted(Path(state).glob('metrics-*.jsonl'))
    path=Path(metrics_file) if metrics_file else (paths[-1] if paths else None)
    if path is None:raise ValueError('no metrics logs found')
    rows,truncated=read_tail(path)
    report=summarize(rows,seconds)
    report.update(metrics_file=str(path),tail_limited=truncated)
    if as_json:print(json.dumps(report,indent=2));return report
    print('Metrics: '+str(path))
    if 'error' in report:print(report['error']);return report
    for host in ([dict(report['mac'],host='Mac')] if 'mac' in report else [])+[
            dict(row,host='NAS') for row in report['nas_workers']]:
        print('%s pid=%s phase=%s sample=%.1fs CPU=%.1f%%'%(
            host['host'],host['pid'],host.get('phase','worker'),host['sample_seconds'],host['cpu_percent']))
        for cost in host['costs'][:8]:
            print('  %-28s %8.3fs estimated; %s/%s sampled calls'%(
                cost['operation'],cost['estimated_seconds'],cost['samples'],cost['calls']))
        for active in host['active'][:4]:
            print('  ACTIVE %s %.1fs %r'%(active['operation'],active['elapsed_seconds'],active.get('path')))
        if host['host']=='Mac':
            rates=host['rates']
            print('  %.1f entries/s; %.2f MiB/s source reads; %.2f KiB/s SSH receive'%(
                rates.get('entries',0),rates.get('bytes_read',0)/1048576,rates.get('wire_received_bytes',0)/1024))
            for op,rpc in host['rpc'].items():
                print('  RPC %s: %.2fms round trip, %.2fms NAS service, %.2fms outside service'%(
                    op,rpc['mean_round_trip_ms'],rpc['mean_nas_service_ms'],rpc['mean_outside_nas_service_ms']))
                if op=='directories':
                    print('    %.2f directories/batch; %.1f directories/s; %.2fms/directory amortized'%(
                        rpc['mean_directories_per_batch'],rpc['directories_per_second'],rpc['amortized_directory_ms']))
                if op in ('prefix_hashes','hashes_sample','hashes_full','validate'):
                    print('    %.2f files/batch; %.1f files/s; %.2fms/file amortized'%(
                        rpc['mean_files_per_batch'],rpc['files_per_second'],rpc['amortized_file_ms']))
            for row in host.get('hashing_by_size',[]):
                print('  HASH %s %s: %s files; %.2fms/file; %.2fMiB read; %.2fs CPU'%(
                    row['stage'],row['size_band'],row['files'],row['mean_job_ms'],
                    row['read_bytes']/1048576,row['cpu_seconds']))
    print(NOTE)
    if truncated:print('Only the last 16 MiB were read; samples may cover less than the requested window.')
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--seconds', default=60, type=float)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        diagnose(args.state_dir, args.seconds, as_json=args.json)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
