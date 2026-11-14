"""Persistent YOLO / ACT inference only; this process never imports a motor bus."""
import numpy as np
from .ipc import worker_pipes


def main():
    incoming,outgoing=worker_pipes();yolo=None;policy=None;pre=post=None
    while True:
        try:sequence,method,p=incoming.recv()
        except EOFError:return
        try:
            if method=='close':outgoing.send((sequence,True,True));return
            if method=='yolo-load':
                from ultralytics import YOLO
                yolo=YOLO(p['path']);yolo.predict(np.zeros((480,640,3),np.uint8),verbose=False,device=p.get('device','cpu'))
                value={'ready':True}
            elif method=='detect':
                if yolo is None:raise RuntimeError('尚未加载 YOLO')
                r=yolo.predict(p['frame'],conf=p.get('confidence',.5),device=p.get('device','cpu'),verbose=False)[0]
                value=[]
                if r.boxes is not None:
                    for b in r.boxes:
                        i=int(b.cls[0]);value.append({'name':str(r.names[i]),'conf':float(b.conf[0]),'xyxy':b.xyxy[0].cpu().tolist()})
            elif method=='act-load':
                from lerobot.policies.act.modeling_act import ACTPolicy
                from lerobot.policies.factory import make_pre_post_processors
                policy=ACTPolicy.from_pretrained(p['path']);policy.to(p['device']);policy.eval();policy.reset()
                pre,post=make_pre_post_processors(policy.config,pretrained_path=p['path'],
                    preprocessor_overrides={'device_processor':{'device':p['device']}})
                pre.reset();post.reset();value={'ready':True}
            elif method=='act':
                if policy is None:raise RuntimeError('尚未加载 ACT')
                import torch
                from lerobot.utils.control_utils import predict_action
                value=predict_action(observation=p['observation'],policy=policy,device=torch.device(p['device']),preprocessor=pre,postprocessor=post,
                    use_amp=False,task=p.get('task','')).detach().cpu().numpy().reshape(-1).tolist()
            else:raise RuntimeError('未知推理请求')
            outgoing.send((sequence,True,value))
        except Exception as exc:outgoing.send((sequence,False,str(exc)))


if __name__=='__main__':main()
