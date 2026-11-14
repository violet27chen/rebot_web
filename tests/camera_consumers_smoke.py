"""Explicit camera-only clients; NO robot object, serial open or motor command."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from lerobot_camera_rebot_shared import RebotSharedCamera,RebotSharedCameraConfig


def main():
    base=os.environ.get('REBOT_TEST_URL','http://127.0.0.1:8000')
    cameras=[RebotSharedCamera(RebotSharedCameraConfig(source=str(n),broker_url=base,width=640,height=480,fps=30)) for n in (2,4,6)]
    try:
        with ThreadPoolExecutor(3) as pool:list(pool.map(lambda c:c.connect(),cameras))
        print('CAMERAS_READY',flush=True)
        start=time.monotonic();count=0
        while time.monotonic()-start<4:
            for camera in cameras:assert camera.async_read(timeout_ms=2000).shape==(480,640,3)
            count+=1
        print('PASS three real LeRobot consumers, frames per camera: '+str(count),flush=True)
    finally:
        for camera in cameras:camera.disconnect()


if __name__=='__main__':main()
