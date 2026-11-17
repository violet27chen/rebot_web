import unittest
from scripts.stop_service import stop


class ServiceStopTests(unittest.TestCase):
    def test_stops_active_eval_then_waits_for_cleanup_without_forcing_release(self):
        states=iter([{'running':True,'job':{'kind':'eval','job_id':'a'}},
                     {'running':True,'job':{'kind':'eval','job_id':'a'}},{'running':False}])
        calls=[]
        def call(base,path,payload=None):
            calls.append((path,payload))
            return next(states) if path=='/api/status' else {'ok':True}
        stop('http://localhost:8000',10,call=call,sleep=lambda s:None,clock=lambda:0)
        self.assertEqual(calls,[('/api/status',None),('/api/stop',{}),('/api/status',None),('/api/status',None)])
    def test_existing_homing_completes_without_interrupting_it(self):
        states=iter([{'running':True,'job':{'kind':'home-and-disable','job_id':'a'}},{'running':False}])
        calls=[]
        def call(base,path,payload=None):calls.append(path);return next(states)
        stop('http://localhost:8000',10,call=call,sleep=lambda s:None,clock=lambda:0)
        self.assertEqual(calls,['/api/status','/api/status'])
    def test_idle_service_sends_no_stop_or_homing_command(self):
        calls=[]
        def call(base,path,payload=None):calls.append(path);return {'running':False}
        stop('http://localhost:8000',10,call=call)
        self.assertEqual(calls,['/api/status'])


if __name__=='__main__':unittest.main()
