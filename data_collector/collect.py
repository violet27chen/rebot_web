#!/usr/bin/env python3
"""双臂快递技能采集器：复制本文件即可使用，不导入 rebot_web。

环境：Ubuntu + 已配置的 LeRobot / B601-DM / reBot 102 Leader 驱动。
首次先检查参数（不打开设备）：
  conda activate lerobot
  python collect.py --dry-run
  python collect.py --check
开始采集，默认左从 ACM0、左主 USB0，右从 ACM1、右主 USB1；务必核实：
  python collect.py --skill push --episodes 10
  python collect.py --skill flip_box --left-leader /dev/ttyUSB1 --right-leader /dev/ttyUSB0
自定义相机 / 续录：
  python collect.py --camera 左=0 --camera 顶部=4 --camera 右=6
  python collect.py --resume --repo-id collector/push_20261116_120000

终端输入 n 回车=下一阶段，r 回车=重录，q 回车=保存并结束。
Ctrl+C 同样请求保存后慢速收工；不要用强杀代替正常停止。
默认预览：http://127.0.0.1:8765 ，实时画面不等待扫码，不含 OCR/模型训练。
相机名“深度相机”仅录制该 UVC 设备的 RGB，不把 RGB 冒充深度。
使用 --help 查看全部参数。各技能请使用独立数据集。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import html
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import select
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The folder distribution includes the patched local drivers. A copied single
# entry file still uses the operator's already configured LeRobot environment.
_VENDOR = Path(__file__).resolve().parent/'vendor'
sys.path.insert(0,str(Path(__file__).resolve().parent))
if _VENDOR.is_dir():sys.path.insert(0,str(_VENDOR))


def recording_module():
    if (Path(__file__).resolve().parent/'collector_support/native_record.py').is_file():
        from collector_support import native_record
        from collector_support.video_cleanup import install
        install(native_record)
        return native_record
    from lerobot.scripts import lerobot_record
    return lerobot_record

DEFAULT_CAMERAS = [('左', '0'), ('深度相机', '4'), ('右', '6')]


def size(value):
    try:
        width, height = map(int, value.lower().split('x'))
        if not (16 <= width <= 7680 and 16 <= height <= 4320 and width % 2 == height % 2 == 0):
            raise ValueError
        return width, height
    except ValueError:
        raise argparse.ArgumentTypeError('尺寸示例：640x360；宽高必须为有效偶数')


def camera_source(value):
    if re.fullmatch(r'\d+', value):
        return '/dev/video'+str(int(value))
    if re.fullmatch(r'/dev/video\d+', value) or value.startswith('/dev/v4l/'):
        return value
    raise ValueError('相机必须为非负编号、/dev/videoN 或 /dev/v4l/ 下的设备路径')


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--skill', default='push', help='单项技能名，例如 push / flip_box / flip_bag')
    parser.add_argument('--task', help='当前技能的任务描述')
    parser.add_argument('--repo-id', help='新数据集默认 collector/技能_时间；续录必须明确指定')
    parser.add_argument('--root', type=Path, help='数据集实际目录；默认 ~/datasets/rebot/REPO_ID')
    parser.add_argument('--episodes', type=int, default=10, help='本次新增剧集数')
    parser.add_argument('--episode-seconds', type=float, default=30)
    parser.add_argument('--reset-seconds', type=float, default=5)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--size', type=size, default=(640, 360), help='录制尺寸，默认 640x360')
    parser.add_argument('--capture-size', type=size, default=(1920, 1080), help='物理相机请求尺寸，默认 1920x1080')
    parser.add_argument('--camera', action='append', help='可重复：名称=设备，默认 左=0、深度相机=4、右=6')
    parser.add_argument('--left-follower', default='/dev/ttyACM0')
    parser.add_argument('--right-follower', default='/dev/ttyACM1')
    parser.add_argument('--left-leader', default='/dev/ttyUSB0')
    parser.add_argument('--right-leader', default='/dev/ttyUSB1')
    parser.add_argument('--left-follower-id', default='left_follower')
    parser.add_argument('--right-follower-id', default='right_follower')
    parser.add_argument('--left-leader-id', default='left_leader')
    parser.add_argument('--right-leader-id', default='right_leader')
    parser.add_argument('--gripper-torque-ratio', type=float, default=.12)
    parser.add_argument('--preview-port', type=int, default=8765, help='本机连续预览端口；0 关闭网页预览')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--yes', action='store_true', help='已核对设备和收工路径，跳过开始前的终端确认')
    parser.add_argument('--dry-run', action='store_true', help='只显示参数，不导入驱动、不打开设备')
    parser.add_argument('--check', action='store_true', help='只检查依赖和驱动兼容性，不打开设备')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'linux': raise ValueError('控制与采集必须在 Ubuntu/Linux 真机运行')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', args.skill): raise ValueError('技能名请使用字母、数字、下划线')
        if args.resume and not args.repo_id: raise ValueError('续录必须指定 --repo-id')
        args.repo_id = args.repo_id or 'collector/'+args.skill+'_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        if len(args.repo_id.split('/')) != 2 or any(not re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in args.repo_id.split('/')):
            raise ValueError('repo-id 格式为 用户/数据集，只使用字母、数字、下划线和短横线')
        args.root = (args.root or Path.home()/'datasets/rebot'/args.repo_id).expanduser().resolve()
        args.task = args.task or {'push':'双臂协同推拢快递', 'flip_box':'双臂协同翻转快递盒，使面单朝上', 'flip_bag':'双臂协同翻转快递袋，使面单朝上'}.get(args.skill, args.skill)
        if not (1 <= args.episodes <= 10000 and 1 <= args.fps <= 60 and 0 < args.episode_seconds <= 3600 and 0 <= args.reset_seconds <= 3600):
            raise ValueError('剧集数、FPS 或录制/重置时长无效')
        if not all(math.isfinite(x) for x in (args.episode_seconds, args.reset_seconds, args.gripper_torque_ratio)) or not .01 <= args.gripper_torque_ratio <= 1:
            raise ValueError('无效的时长或夹爪力矩比例')
        if not 0 <= args.preview_port <= 65535: raise ValueError('预览端口无效')
        cameras = [item.split('=', 1) for item in args.camera] if args.camera else DEFAULT_CAMERAS
        args.cameras = [{'name':name.strip(), 'source':camera_source(source.strip())} for name,source in cameras]
        if any(not c['name'] or '.' in c['name'] for c in args.cameras) or len({c['name'] for c in args.cameras}) != len(args.cameras):
            raise ValueError('相机名称不能为空、重复或含点号')
        paths = [c['source'] for c in args.cameras] + [getattr(args, side+'_'+kind) for side in ('left','right') for kind in ('follower','leader')]
        if any(not path.startswith('/dev/') for path in paths):raise ValueError('机械臂串口必须是 /dev/ 下的设备路径')
        if len(set(map(os.path.realpath, paths))) != len(paths): raise ValueError('相机/串口存在重复设备或别名')
        args.codec = 'h264'
        if args.resume:
            info = json.loads((args.root/'meta/info.json').read_text())
            specs = [info['features']['observation.images.left_'+c['name']] for c in args.cameras]
            sizes = {(feature['shape'][1], feature['shape'][0]) for feature in specs}
            codecs = {feature['info']['video.codec'] for feature in specs}
            if len(sizes) != 1 or len(codecs) != 1: raise ValueError('续录相机尺寸/编码不一致')
            args.size = sizes.pop();args.fps = int(info['fps'])
            args.codec = {'h264':'h264','av1':'libsvtav1','hevc':'hevc'}[codecs.pop()]
            args.size = size(f'{args.size[0]}x{args.size[1]}')
            if not 1 <= args.fps <= 60:raise ValueError('原数据集 FPS 超出采集器支持范围')
        elif args.root.exists(): raise ValueError('输出目录已经存在；续录请指定 --resume，或换新的 --root / --repo-id')
    except (ValueError, OSError, KeyError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    return args


def check_dependencies():
    import inspect
    from lerobot_robot_seeed_b601 import BiSeeedB601DMFollower
    from lerobot_robot_seeed_b601.seeed_b601_follower import SeeedB601FollowerBase
    from lerobot_teleoperator_rebot_arm_102 import BiRebotArm102Leader
    DatasetRecordConfig = recording_module().DatasetRecordConfig
    import cv2, numpy, av  # noqa: F401
    if 'vcodec' not in DatasetRecordConfig.__dataclass_fields__:
        raise RuntimeError('需要支持 vcodec 的当前 LeRobot 采集版本')
    if 'Slow measured-pose return' not in inspect.getsource(SeeedB601FollowerBase.safe_zero) or 'Coordinated parallel shutdown' not in inspect.getsource(BiSeeedB601DMFollower.disconnect):
        raise RuntimeError('请使用项目已配置的 LeRobot 环境：B601 驱动需包含慢速、双臂并行收工，不能用快速归零版本代替')


def check_owners(paths):
    wanted = {os.path.realpath(p) for p in paths}
    owners = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid(): continue
        try:
            for fd in (entry/'fd').iterdir():
                try:
                    link = os.readlink(fd)
                    if link.startswith('/dev/') and os.path.realpath(link) in wanted:
                        owners.append((entry.name, link))
                except OSError: pass
        except (OSError, PermissionError): pass
    if owners:
        raise RuntimeError('设备正在被其他进程占用，先结束原任务并断开设备，未杀死任何进程：'+str(sorted(set(owners))))


def capture_worker(nodes, capture_size, fps, stop, ready, commands, port, owner_pid):
    """Only this process opens cameras; stream preview reuses the same buffers."""
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    def capture(node):
        cap = None
        try:
            cap = cv2.VideoCapture(node['source'], cv2.CAP_V4L2)
            if not cap.isOpened(): raise RuntimeError('无法打开 '+node['source'])
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, capture_size[0]);cap.set(cv2.CAP_PROP_FRAME_HEIGHT, capture_size[1])
            cap.set(cv2.CAP_PROP_FPS, fps);cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            actual = cap.get(cv2.CAP_PROP_FPS)
            if actual > 0 and actual + 1 < fps: raise RuntimeError(f'实际 FPS {actual:g} 低于请求值 {fps}，请降低 --fps')
            target = np.frombuffer(node['pixels'], np.uint8).reshape(node['height'],node['width'],3)
            while not stop.is_set():
                valid, frame = cap.read()
                if not valid: raise RuntimeError('相机未返回图像：'+node['source'])
                frame = cv2.resize(frame,(node['width'],node['height']),interpolation=cv2.INTER_AREA)
                frame = cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
                with node['condition']:
                    target[:] = frame;node['stamp'].value = time.monotonic();node['sequence'].value += 1
                    node['condition'].notify_all()
        except Exception as error:
            with node['condition']:
                node['error'].value = str(error).encode('utf-8')[:500];node['condition'].notify_all()
            print('[相机失败]',node['name'],error,flush=True)
        finally:
            if cap is not None:cap.release()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            action = self.path.removeprefix('/control/')
            if action not in ('n','r','q') or not ready.is_set():self.send_error(409,'Collector is not ready');return
            try:commands.put_nowait(action)
            except queue.Full:self.send_error(409,'Command queue is busy');return
            self.send_response(204);self.end_headers()
        def do_GET(self):
            if self.path == '/':
                cards = ''.join('<article><h3>'+html.escape(n['name'])+'</h3><img src="/stream/'+str(i)+'"></article>' for i,n in enumerate(nodes))
                body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>双臂数据采集 · 实时画面</title>'
                        '<style>body{font:16px system-ui;background:#f4f6fa;margin:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}article{background:white;padding:16px;border-radius:12px}img{width:100%;height:auto}button{padding:10px;margin:6px}p{color:#465166}</style>'
                        '<h1>双臂技能采集 · 实时画面</h1><p>图像独立连续刷新。采集状态及保存进度见终端。左/右设备配置请先在终端核实。</p>'
                        '<button onclick="send(\'n\')">下一阶段</button><button onclick="send(\'r\')">重录本集</button><button onclick="if(confirm(\'保存当前集并结束，随后慢速收工归零？\'))send(\'q\')">保存并结束</button><span id="status"></span><main>'+cards+'</main>'
                        '<script>async function send(k){let r=await fetch("/control/"+k,{method:"POST"});document.querySelector("#status").textContent=r.ok?"请求已发送，等待采集进程处理":"采集尚未就绪或请求失败"}</script>').encode()
                self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.end_headers();self.wfile.write(body);return
            try:
                match=re.fullmatch(r'/stream/(\d+)',self.path)
                if not match:raise ValueError
                node = nodes[int(match[1])]
            except (ValueError,IndexError):self.send_error(404);return
            self.send_response(200);self.send_header('Cache-Control','no-store');self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers()
            last = 0
            try:
                while not stop.is_set():
                    with node['condition']:
                        node['condition'].wait_for(lambda:node['sequence'].value != last or node['error'].value or stop.is_set(),timeout=1)
                        if node['error'].value:break
                        if not node['sequence'].value:continue
                        last = node['sequence'].value
                        frame = np.frombuffer(node['pixels'],np.uint8).reshape(node['height'],node['width'],3).copy()
                    valid, jpg = cv2.imencode('.jpg',cv2.cvtColor(frame,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,75])
                    if valid:self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpg.tobytes()+b'\r\n');self.wfile.flush()
                    stop.wait(1/15)
            except (OSError,ConnectionError):pass
    server = None
    try:
        if port:
            server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
            threading.Thread(target=server.serve_forever,daemon=True).start()
        for node in nodes:threading.Thread(target=capture,args=(node,),daemon=True).start()
        while not stop.wait(.2):
            if os.getppid() != owner_pid:break
    except Exception as error:
        for node in nodes:
            with node['condition']:node['error'].value=str(error).encode()[:500];node['condition'].notify_all()
    finally:
        stop.set()
        if server:server.shutdown();server.server_close()


class CaptureService:
    def __init__(self,args):
        self.args=args;self.ctx=mp.get_context('spawn');self.stop=self.ctx.Event();self.ready=self.ctx.Event();self.commands=self.ctx.Queue(16)
        width,height=args.size
        self.nodes=[{**c,'width':width,'height':height,'pixels':self.ctx.RawArray('B',width*height*3),
                     'stamp':self.ctx.RawValue('d',0),'sequence':self.ctx.RawValue('Q',0),'error':self.ctx.RawArray('c',512),'condition':self.ctx.Condition()} for c in args.cameras]
        self.process=None
    def start(self):
        self.process=self.ctx.Process(target=capture_worker,args=(self.nodes,self.args.capture_size,self.args.fps,self.stop,self.ready,self.commands,self.args.preview_port,os.getpid()),name='collector-cameras')
        self.process.start();deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            for n in self.nodes:
                if n['error'].value:raise RuntimeError(n['error'].value.decode(errors='replace'))
            if not self.process.is_alive():raise RuntimeError('相机进程提前退出')
            if all(n['sequence'].value for n in self.nodes):return
            time.sleep(.05)
        raise TimeoutError('相机预检超时，尚未连接任何机械臂')
    def close(self):
        self.ready.clear();self.stop.set()
        if self.process:
            self.process.join(2)
            if self.process.is_alive():self.process.terminate();self.process.join(1)
            if self.process.is_alive():self.process.kill();self.process.join(1)
        self.commands.close();self.commands.join_thread()


def record(args,service):
    import numpy as np
    from lerobot.cameras.camera import Camera
    from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
    from lerobot_robot_seeed_b601 import BiSeeedB601DMFollower, BiSeeedB601DMFollowerConfig, BiSeeedArmConfig
    from lerobot_teleoperator_rebot_arm_102 import BiRebotArm102Leader, BiRebotArm102LeaderConfig, BiRebotArmConfig
    native = recording_module()
    from lerobot.datasets import lerobot_dataset
    class MemoryCamera(Camera):
        def __init__(self,config,node):super().__init__(config);self.node=node;self.connected=False;self.consumed=0
        @property
        def is_connected(self):return self.connected
        @staticmethod
        def find_cameras():return []
        def connect(self,warmup=True):self.async_read(timeout_ms=2000);self.connected=True
        def disconnect(self):self.connected=False
        def read(self):return self.async_read()
        def async_read(self,timeout_ms=1000):
            n=self.node
            with n['condition']:
                n['condition'].wait_for(lambda:n['sequence'].value!=self.consumed or n['error'].value or service.stop.is_set(),timeout_ms/1000)
                if n['error'].value:raise RuntimeError(n['error'].value.decode(errors='replace'))
                if service.stop.is_set() or not service.process.is_alive() or time.monotonic()-n['stamp'].value>2:raise RuntimeError('相机无新鲜反馈，未使用旧帧继续采集')
                self.consumed=n['sequence'].value
                return np.frombuffer(n['pixels'],np.uint8).reshape(n['height'],n['width'],3).copy()
    cameras={c['name']:OpenCVCameraConfig(index_or_path=Path(c['source']),fps=args.fps,width=args.size[0],height=args.size[1],fourcc='MJPG') for c in args.cameras}
    arms={side:BiSeeedArmConfig(port=getattr(args,side+'_follower'),id=getattr(args,side+'_follower_id'),can_adapter='damiao',force_pos_torque_ration=args.gripper_torque_ratio) for side in ('left','right')}
    leaders={side:BiRebotArmConfig(port=getattr(args,side+'_leader'),id=getattr(args,side+'_leader_id')) for side in ('left','right')}
    robot_cfg=BiSeeedB601DMFollowerConfig(left_arm=arms['left'],right_arm=arms['right'],cameras=cameras)
    leader_cfg=BiRebotArm102LeaderConfig(left_arm=leaders['left'],right_arm=leaders['right'])
    robot=BiSeeedB601DMFollower(robot_cfg);leader=BiRebotArm102Leader(leader_cfg)
    robot.left_arm.cameras={n['name']:MemoryCamera(cameras[n['name']],n) for n in service.nodes};robot.cameras=robot.left_arm.cameras
    events={'exit_early':False,'rerecord_episode':False,'stop_recording':False};finished=threading.Event()
    class Listener:
        def stop(self):finished.set();service.ready.clear()
    listener=Listener()
    def controls():
        while not finished.is_set():
            key=None
            try:key=service.commands.get(timeout=.05)
            except queue.Empty:pass
            if key is None:
                try:
                    if select.select([sys.stdin],[],[],0)[0]:key=sys.stdin.readline().strip().lower()
                except (OSError,ValueError):pass
            if key in ('n','r','q'):
                events['exit_early']=True
                if key=='r':events['rerecord_episode']=True
                if key=='q':events['stop_recording']=True
    def init_controls():
        service.ready.set();threading.Thread(target=controls,daemon=True,name='collector-input').start()
        print('采集就绪：n 回车下一阶段，r 回车重录，q 回车保存结束；Ctrl+C 正常收工。',flush=True)
        return listener,events
    def interrupt(*_):
        if not service.ready.is_set():raise KeyboardInterrupt
        events['stop_recording']=events['exit_early']=True
        print('已请求保存当前集并慢速收工，请等待进程结束。',flush=True)
    old_signals={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
    for sig in old_signals:signal.signal(sig,interrupt)
    native.make_robot_from_config=lambda _:robot;native.make_teleoperator_from_config=lambda _:leader;native.init_keyboard_listener=init_controls
    # Do not fork the multithreaded CAN process to encode videos.
    save=lerobot_dataset.LeRobotDataset.save_episode
    def save_episode(self,*a,**kw):
        if len(a)<2:kw.setdefault('parallel_encoding',False)
        start=time.monotonic();print('正在写盘、统计并编码当前集，请等待…',flush=True)
        result=save(self,*a,**kw);print(f'当前集保存完成，用时 {time.monotonic()-start:.1f}s',flush=True);return result
    lerobot_dataset.LeRobotDataset.save_episode=save_episode
    try:
        # Validate leaders before the native recorder can connect followers.
        leader.connect();leader.connect=lambda calibrate=True:None
        cfg=native.RecordConfig(robot=robot_cfg,teleop=leader_cfg,dataset=native.DatasetRecordConfig(repo_id=args.repo_id,root=args.root,single_task=args.task,fps=args.fps,num_episodes=args.episodes,episode_time_s=args.episode_seconds,reset_time_s=args.reset_seconds,push_to_hub=False,video=True,vcodec=args.codec,video_encoding_batch_size=1,num_image_writer_threads_per_camera=2),resume=args.resume,display_data=False,play_sounds=False)
        native.record(cfg)
    finally:
        listener.stop()
        try:
            if any(arm.bus is not None for arm in (robot.left_arm,robot.right_arm)):robot.disconnect()
        finally:
            for arm in (leader.left_arm,leader.right_arm):
                if arm.is_connected:
                    try:arm.disconnect()
                    except Exception as error:print('主臂串口清理失败：',error,file=sys.stderr)
            for sig,handler in old_signals.items():signal.signal(sig,handler)


def main(argv=None):
    args=arguments(argv)
    summary={key:str(value) if isinstance(value,Path) else value for key,value in vars(args).items() if key not in ('yes','check','dry_run','camera')}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if args.dry_run:return 0
    # Inherit no Web telemetry/broker connection; this file owns its resources.
    for name in ('REBOT_TELEMETRY_SOCKET','REBOT_TELEMETRY_KEY','REBOT_TELEMETRY_PORTS','REBOT_CAMERA_BROKER_URL'):os.environ.pop(name,None)
    check_dependencies()
    if args.check:print('依赖与慢速双臂收工驱动检查通过，未打开任何设备。');return 0
    if not args.yes and input('请核实四路串口、相机、机械臂支撑及收工路径。输入 START 开始：').strip()!='START':print('已取消，未打开设备。');return 0
    paths=[c['source'] for c in args.cameras]+[getattr(args,s+'_'+k) for s in ('left','right') for k in ('follower','leader')]
    import fcntl
    with contextlib.ExitStack() as locks:
        for device in sorted(map(os.path.realpath,paths)):
            handle=locks.enter_context(open('/tmp/rebot-collector-'+hashlib.sha256(device.encode()).hexdigest()[:20]+'.lock','a'))
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('另一采集器已锁定设备：'+device)
        check_owners(paths)
        service=CaptureService(args)
        try:
            service.start()
            if args.preview_port:print(f'实时预览：http://127.0.0.1:{args.preview_port}（只在本机监听）',flush=True)
            record(args,service)
        finally:service.close()
    return 0


if __name__=='__main__':
    mp.freeze_support()
    try:raise SystemExit(main())
    except KeyboardInterrupt:print('启动已取消，设备清理已执行。',file=sys.stderr);raise SystemExit(130)
    except Exception as error:print('采集失败：'+str(error),file=sys.stderr);raise SystemExit(1)
