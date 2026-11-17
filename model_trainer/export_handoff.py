#!/usr/bin/env python3
"""从技能数据集的各集起始实测状态导出入口范围；只读，不训练/连接设备。"""
import argparse
import json
from pathlib import Path


def export(root, skill):
    import numpy as np
    import pyarrow.parquet as pq
    root=Path(root).expanduser().resolve()
    info=json.loads((root/'meta/info.json').read_text())
    names=info['features']['observation.state'].get('names')
    if not names or len(names)!=14:raise ValueError('需要包含关节名称的14维双臂数据集')
    first={}
    for file in sorted((root/'data').rglob('*.parquet')):
        table=pq.read_table(file,columns=['episode_index','frame_index','observation.state'])
        for episode,index,state in zip(table['episode_index'].to_pylist(),table['frame_index'].to_pylist(),table['observation.state'].to_pylist()):
            if episode not in first or index<first[episode][0]:first[episode]=(index,state)
    if not first:raise ValueError('没有可导出的剧集')
    values=np.asarray([row[1] for row in first.values()],float)
    if values.shape[1]!=14 or not np.isfinite(values).all():raise ValueError('起始状态无效')
    return {'skill_entries':{skill:{'min_motor_deg':values.min(0).tolist(),'max_motor_deg':values.max(0).tolist()}},'state_names':names,'episodes':len(first),'dataset_dir':str(root),'note':'这是训练数据的入口范围，不代表任意中间姿态都可接入；不得扩大范围绕过交接验证'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset-dir',type=Path,required=True);p.add_argument('--skill',required=True);p.add_argument('--output',type=Path)
    args=p.parse_args();result=export(args.dataset_dir,args.skill)
    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        with args.output.open('x',encoding='utf-8') as stream:stream.write(text)
    else:print(text,end='')


if __name__=='__main__':main()
