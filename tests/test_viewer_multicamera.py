import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient
import backend.main as main


class ViewerCamerasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'demo/three'
        self.root.joinpath('meta/episodes/chunk-000').mkdir(parents=True)
        self.cameras=['observation.images.left','observation.images.right','observation.images.depth_rgb']
        self.root.joinpath('meta/info.json').write_text(json.dumps({'fps':30,'features':{name:{'dtype':'video','shape':[480,640,3]} for name in self.cameras}}))
        row={'episode_index':0,'length':2,'tasks':['test-only']}
        for cam in self.cameras:
            row.update({f'videos/{cam}/chunk_index':0,f'videos/{cam}/file_index':0,
                        f'videos/{cam}/from_timestamp':0.,f'videos/{cam}/to_timestamp':1.})
            for chunk in [0,1]:
                video=self.root/f'videos/{cam}/chunk-{chunk:03d}/file-000.mp4';video.parent.mkdir(parents=True,exist_ok=True);video.write_bytes(b'test-only')
        row[f'videos/{self.cameras[2]}/to_timestamp']=float('nan')
        pd.DataFrame([row]).to_parquet(self.root/'meta/episodes/chunk-000/file-000.parquet')
        for i in [0,1]:
            folder=self.root/f'data/chunk-{i:03d}';folder.mkdir(parents=True)
            pd.DataFrame([{'episode_index':0,'timestamp':i/30,'observation.state':[i]*14,'action':[i]*14}]).to_parquet(folder/'file-000.parquet')
        self.hub=patch.object(main,'LEROBOT_HOME',Path(self.temp.name));self.hub.start()
        self.cache=patch.object(main,'_VIEW_CACHE',Path(self.temp.name)/'cache');self.cache.start()
        self.client=TestClient(main.app)
    def tearDown(self):self.cache.stop();self.hub.stop();self.temp.cleanup()
    def test_three_cameras_are_independent_when_one_segment_is_missing(self):
        response=self.client.get('/api/dataset/view/episodes',params={'repo_id':'demo/three'})
        self.assertEqual(response.status_code,200,response.text)
        cams=response.json()['episodes'][0]['cameras'];self.assertEqual(len(cams),3)
        self.assertTrue(cams[self.cameras[0]]['available']);self.assertTrue(cams[self.cameras[1]]['available'])
        self.assertFalse(cams[self.cameras[2]]['available'])
    def test_episode_trajectory_includes_both_data_files(self):
        response=self.client.get('/api/dataset/view/episode',params={'repo_id':'demo/three','episode':0})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['frames'],2);self.assertEqual(len(response.json()['action']),2)
    def test_video_must_be_registered_in_this_dataset(self):
        for cam in ['observation.images.unknown','/tmp','../outside']:
            response=self.client.get('/api/dataset/view/video',params={'repo_id':'demo/three','camera':cam})
            self.assertEqual(response.status_code,400)
    def test_video_supports_http_range_for_browser_seeking(self):
        response=self.client.get('/api/dataset/view/video',params={'repo_id':'demo/three','camera':self.cameras[0]},headers={'Range':'bytes=0-3'})
        self.assertEqual(response.status_code,206,response.text)
        self.assertEqual(response.content,b'test')
    def test_clip_cache_distinguishes_chunks_and_time_ranges(self):
        def encode(cmd,**kwargs):Path(cmd[-1]).write_bytes(b'encoded-fixture');return SimpleNamespace(returncode=0,stderr=b'')
        files=[]
        with patch.object(main.subprocess,'run',side_effect=encode) as run:
            for chunk,start,end in [(0,0.,1.),(1,0.,1.),(0,.2,.8)]:
                response=main.api_dataset_view_clip('demo/three',self.cameras[0],chunk,0,start,end,0)
                files.append(str(response.path))
        self.assertEqual(len(set(files)),3);self.assertEqual(run.call_count,3)


if __name__=='__main__':unittest.main()
