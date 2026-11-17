import copy
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient
import backend.main as main
from backend.camera_manager import CameraManager, CameraError
from backend.waybill_process import WaybillProcess, ScanBusy, ScanError
from backend.waybill_sources import select_cameras


def decoder_echo(items):
    return [{'pid':os.getpid(),'name':item['name']} for item in items]


def decoder_hang(items):
    time.sleep(10)
    return []


class SourceTests(unittest.TestCase):
    def setUp(self):self.cameras=[{'name':'左','index_or_path':0},{'name':'右','index_or_path':'/dev/video2'}]
    def test_name_does_not_get_parsed_as_a_device_on_other_entries(self):
        self.assertEqual(select_cameras(self.cameras,'右'),[self.cameras[1]])
        self.assertEqual(select_cameras(self.cameras,'左'),[self.cameras[0]])
    def test_zero_and_canonical_source_alias_select_only_one_camera(self):
        self.assertEqual(select_cameras(self.cameras,0),[self.cameras[0]])
        self.assertEqual(select_cameras(self.cameras,'/dev/video0'),[self.cameras[0]])
        self.cameras.append({'name':'0','index_or_path':6})
        self.assertEqual(select_cameras(self.cameras,0),[self.cameras[0]])
    def test_unknown_or_ambiguous_selection_never_falls_back_to_all(self):
        for value in ['已移除','/dev/video99']:
            with self.assertRaises(CameraError):select_cameras(self.cameras,value)
        self.cameras.append({'name':'右','index_or_path':4})
        with self.assertRaises(CameraError):select_cameras(self.cameras,'右')


class ScannerTests(unittest.TestCase):
    def test_real_decoder_reads_jpeg_bytes_without_camera_access(self):
        qr=cv2.QRCodeEncoder_create().encode('SF123456789012')
        qr=cv2.copyMakeBorder(qr,4,4,4,4,cv2.BORDER_CONSTANT,value=255)
        image=cv2.cvtColor(cv2.resize(qr,None,fx=8,fy=8,interpolation=cv2.INTER_NEAREST),cv2.COLOR_GRAY2BGR)
        _,jpeg=cv2.imencode('.jpg',image)
        scanner=WaybillProcess(timeout=12)
        try:
            reply=scanner.scan([{'name':'左','source':'0','jpeg':jpeg.tobytes()}])
            self.assertTrue(reply[0]['ok']);self.assertEqual(reply[0]['codes'][0]['text'],'SF123456789012')
            self.assertTrue(reply[0]['preview'].startswith('data:image/jpeg;base64,'))
        finally:scanner.close()
    def test_decoder_is_a_separate_reused_process(self):
        scanner=WaybillProcess(timeout=8,decoder=decoder_echo)
        try:
            first=scanner.scan([{'name':'左'}]);second=scanner.scan([{'name':'右'}])
            self.assertNotEqual(first[0]['pid'],os.getpid());self.assertEqual(first[0]['pid'],second[0]['pid'])
        finally:scanner.close()
    def test_busy_returns_immediately_instead_of_building_a_queue(self):
        scanner=WaybillProcess();scanner.lock.acquire()
        try:
            started=time.monotonic()
            with self.assertRaises(ScanBusy):scanner.scan([])
            self.assertLess(time.monotonic()-started,.1)
        finally:scanner.lock.release();scanner.close()
    def test_hung_decoder_is_recycled_and_can_recover(self):
        scanner=WaybillProcess(timeout=.5,decoder=decoder_hang)
        try:
            with self.assertRaises(ScanError):scanner.scan([{}])
            self.assertIsNone(scanner.process)
            scanner.decoder=decoder_echo;scanner.timeout=8
            self.assertEqual(scanner.scan([{'name':'恢复'}])[0]['name'],'恢复')
        finally:scanner.close()


class SnapshotTests(unittest.TestCase):
    def test_recording_snapshots_do_not_open_reconfigure_or_consume_stream(self):
        from test_camera_manager import Capture
        opened=[]
        def factory(source):
            cap=Capture(source);opened.append(cap);return cap
        manager=CameraManager(factory,idle_seconds=1)
        lease=manager.acquire(0,{'width':640,'height':480,'fps':30})
        try:
            lease.read();position=lease.seq
            results=[manager.snapshot(0,{'width':1920,'height':1080,'fps':60},start_capture=False) for _ in range(3)]
            self.assertTrue(all(jpeg.startswith(b'\xff\xd8') for _,_,jpeg in results))
            self.assertEqual(lease.seq,position);self.assertEqual(len(opened),1)
            self.assertEqual(opened[0].props[cv2.CAP_PROP_FRAME_WIDTH],640)
            self.assertTrue(lease.read()[0])
            with self.assertRaises(CameraError):manager.snapshot(2,start_capture=False)
            self.assertEqual(len(opened),1)
        finally:lease.release();manager.close_all()


class ScanApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'config.json'
        cfg=copy.deepcopy(main.DEFAULT_CONFIG);cfg['cameras']=[{'name':'左','index_or_path':0},{'name':'右','index_or_path':2}]
        self.path.write_text(json.dumps(cfg))
        self.patches=[patch.object(main,'CONFIG_FILE',self.path),patch.object(main,'_WAYBILL_LOADED',True),patch.object(main,'_WAYBILL_HISTORY',[]),patch.object(main,'_WAYBILL_LATEST',None),patch.object(main,'DEVICES_RELEASED',False)]
        for p in self.patches:p.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_record_scan_uses_snapshot_only_and_never_persists_codes(self):
        reply=[{'name':'右','source':'2','ok':True,'codes':[{'text':'SF123456789','type':'Code128'}]}]
        with patch.object(main.STATE,'running',return_value=True),patch.object(main.STATE,'job',SimpleNamespace(kind='record')),patch.object(main.CAMERAS,'snapshot',return_value=(1,time.time(),b'jpeg')) as snapshot,patch.object(main.WAYBILL_SCANNER,'scan',return_value=reply),patch.object(main,'camera_probe') as probe,patch.object(main,'_persist_waybill_files') as persist:
            response=self.client.post('/api/waybill/scan',json={'camera':'右','save':False})
        self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['found'])
        self.assertEqual(snapshot.call_args.args[0],2);self.assertFalse(snapshot.call_args.kwargs['start_capture'])
        probe.assert_not_called();persist.assert_not_called()
    def test_scan_does_not_hold_job_lock_while_waiting_for_recognition(self):
        def scanner(items):
            with ThreadPoolExecutor(1) as pool:pool.submit(lambda:main.STATE.running()).result(timeout=.5)
            return []
        with patch.object(main.CAMERAS,'snapshot',return_value=(1,time.time(),b'jpeg')),patch.object(main.WAYBILL_SCANNER,'scan',side_effect=scanner):
            self.assertEqual(self.client.post('/api/waybill/scan',json={'camera':0,'save':False}).status_code,200)
    def test_unknown_camera_rejected_before_any_capture(self):
        with patch.object(main.CAMERAS,'snapshot') as snapshot:
            self.assertEqual(self.client.post('/api/waybill/scan',json={'camera':'不存在'}).status_code,400)
            snapshot.assert_not_called()
    def test_include_preview_false_skips_base64_and_keeps_dimensions(self):
        import cv2, numpy as np
        from backend.waybill_process import decode_batch
        img=np.zeros((180,320,3),dtype=np.uint8)
        ok,buf=cv2.imencode('.jpg',img)
        self.assertTrue(ok)
        res=decode_batch([{'name':'左','source':'0','jpeg':buf.tobytes(),'include_preview':False}])
        self.assertEqual(len(res),1)
        self.assertTrue(res[0]['ok'])
        self.assertEqual(res[0]['width'],320)
        self.assertEqual(res[0]['height'],180)
        self.assertEqual(res[0]['preview'],'')
        self.assertIsInstance(res[0]['boxes'],list)
