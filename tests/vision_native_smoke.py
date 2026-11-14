"""Explicit camera-only smoke test; never constructs or moves a robot."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from backend.vision_process import VisionProcess
from backend.camera_manager import CameraError


def main():
    vision=VisionProcess(Path(__file__).resolve().parents[1]);a=b=None
    try:
        a=vision.depth.acquire();b=vision.depth.acquire()
        meta,jpeg,png=a.read();frame=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_UNCHANGED)
        assert frame.shape==(480,640) and frame.dtype==np.uint16
        rgb=vision.rgb.status();depth=vision.depth.status()
        assert rgb['capture_pid']==depth['capture_pid']==vision.process.pid
        assert depth['clients']==2
        print('PASS real native uint16-mm depth over shared-process RPC;',frame.shape,'capture PID',vision.process.pid,'valid',round(meta['valid_ratio']*100,1),'%',flush=True)
        vision.stop()
        try:a.read();raise AssertionError('dead vision lease accepted')
        except CameraError:pass
        a.release();b.release();a=b=None
        new=vision.depth.acquire()
        try:new.read();assert vision.depth.status()['clients']==1
        finally:new.release()
        print('PASS process recovery and old-lease invalidation',flush=True)
    finally:
        if a:a.release()
        if b:b.release()
        vision.stop()


if __name__=='__main__':main()
