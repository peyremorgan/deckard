import contextlib,io,json,tempfile,unittest
from pathlib import Path
from deckard import diagnostics

def record(at,kind='heartbeat',seconds=None,cpu=0,metrics=None,**extra):
    result=dict(type=kind,time_unix=at,pid=1,worker_pid=2,metrics=metrics or {},
                counters={},cpu_seconds=cpu,source_cpu_seconds=cpu,
                elapsed_seconds=at if seconds is None else seconds,
                source_elapsed_seconds=at if seconds is None else seconds)
    result.update(extra);return result

def metric(samples,calls,seconds):
    return dict(calls=samples,calls_total=calls,seconds=seconds,latency_buckets=[0,samples,0,0,0,0,0,0])

class DiagnosticsTests(unittest.TestCase):
    def test_window_uses_deltas_and_keeps_hosts_separate(self):
        rows=[record(10,metrics={'sql.commit':metric(2,2,1)}),
              record(20,cpu=5,metrics={'sql.commit':metric(4,4,2)},
                     counters={'rpc_completed_directory':10,'rpc_seconds_directory':2,'nas_service_seconds_directory':1.5}),
              record(10,'source_metrics',metrics={'fs.entry.stat':metric(1,16,.1)}),
              record(20,'source_metrics',cpu=8,metrics={'fs.entry.stat':metric(3,48,.5)})]
        result=diagnostics.summarize(rows,10)
        self.assertEqual(result['mac']['costs'][0]['estimated_seconds'],1)
        self.assertEqual(result['nas_workers'][0]['costs'][0]['estimated_seconds'],6.4)
        self.assertEqual(result['nas_workers'][0]['cpu_percent'],80)
        self.assertEqual(result['mac']['rpc']['directory']['mean_outside_nas_service_ms'],50)

    def test_pid_reuse_and_empty_window_do_not_generate_negative_rates(self):
        rows=[record(1,'source_metrics',seconds=100,cpu=50),
              record(10,'source_metrics',seconds=1,cpu=.5),record(20,'source_metrics',seconds=11,cpu=2.5)]
        result=diagnostics.summarize(rows,60)
        self.assertEqual(result['nas_workers'][0]['cpu_percent'],20)
        self.assertIn('error',diagnostics.summarize([],60))
        self.assertEqual(diagnostics.summarize([record(1)],60).get('nas_workers'),[])

    def test_bounded_tail_and_partial_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'metrics-one.jsonl'
            path.write_bytes(b'x'*2000+b'\n'+json.dumps(record(10)).encode()+b'\n'+json.dumps(record(20)).encode()+b'\n{"partial":')
            previous=diagnostics.TAIL_BYTES;diagnostics.TAIL_BYTES=1024
            try:
                rows,truncated=diagnostics.read_tail(path)
                self.assertEqual(len(rows),2)
                self.assertTrue(truncated)
                with contextlib.redirect_stdout(io.StringIO()):report=diagnostics.diagnose(temp,as_json=True)
                self.assertEqual(report['mac']['sample_seconds'],10)
            finally:diagnostics.TAIL_BYTES=previous

    def test_call_crossing_window_keeps_its_observed_time(self):
        before={'metrics':{'fs.read':metric(0,1,0)}}
        after={'metrics':{'fs.read':metric(1,1,3)}}
        cost=diagnostics.cost_delta(before,after)[0]
        self.assertEqual(cost['estimated_seconds'],3)
        self.assertEqual(cost['calls'],1)

    def test_prefix_rpc_reports_files_and_amortized_cost(self):
        rows=[record(10),record(20,counters={'rpc_completed_prefix_hashes':2,
            'rpc_prefix_files_in_batches':32,'rpc_seconds_prefix_hashes':.64,'nas_service_seconds_prefix_hashes':.5})]
        rpc=diagnostics.summarize(rows)['mac']['rpc']['prefix_hashes']
        self.assertEqual(rpc['mean_files_per_batch'],16)
        self.assertEqual(rpc['amortized_file_ms'],20)

    def test_batch_rpc_reports_directory_counts_and_amortized_cost(self):
        rows=[record(10),record(20,counters={'rpc_completed_directories':2,
              'rpc_directories_in_batches':32,'rpc_seconds_directories':.64,'nas_service_seconds_directories':.5})]
        rpc=diagnostics.summarize(rows)['mac']['rpc']['directories']
        self.assertEqual(rpc['mean_directories_per_batch'],16)
        self.assertEqual(rpc['amortized_directory_ms'],20)

if __name__=='__main__':unittest.main()
