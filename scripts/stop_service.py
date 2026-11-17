"""Let an active task finish its normal cleanup before systemd stops the API.

This helper never sends forced-release, motor-enable, or homing commands.
"""
import argparse
import json
import time
import urllib.error
import urllib.request


def request(base, path, payload=None):
    req=urllib.request.Request(base.rstrip('/')+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as response:
        return json.load(response)


def stop(base, timeout, call=request, sleep=time.sleep, clock=time.monotonic):
    try: status=call(base,'/api/status')
    except (urllib.error.URLError,TimeoutError,OSError):
        print('API unavailable; systemd will send the configured graceful signal.',flush=True)
        return
    if not status.get('running'):return
    job=status.get('job') or {};identity=job.get('job_id')
    if job.get('kind')!='home-and-disable':
        call(base,'/api/stop',{})
    print('Waiting for the current task and its normal cleanup; no forced release.',flush=True)
    deadline=clock()+timeout
    while status.get('running'):
        if clock()>=deadline:raise TimeoutError('Task cleanup timed out; no forced-release command was sent')
        sleep(.5);status=call(base,'/api/status')
        if status.get('running') and identity and (status.get('job') or {}).get('job_id')!=identity:
            raise RuntimeError('Task identity changed while stopping the service')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    parser.add_argument('--timeout',type=float,default=1700)
    args=parser.parse_args()
    stop(args.url,args.timeout)
