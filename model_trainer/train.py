#!/usr/bin/env python3
"""ACT 模型训练师单文件入口；复制本文件到已配置的 LeRobot 环境即可使用。

数据集引用（不会移动、修改原数据集）：
  python train.py --list-datasets
  python train.py --dataset-dir ~/datasets/rebot/collector/push_20261116_120000 --dry-run
  python train.py --dataset-dir ~/.cache/huggingface/lerobot/4 --steps 20000
  python train.py --dataset 快递4 --steps 20000
加新数据微调已有模型（新输出目录，保留原权重）：
  python train.py --dataset-dir /数据集目录 --base-model /模型目录/pretrained_model --learning-rate 0.000001
恢复中断的训练（同一数据集，保留优化器和训练步数）：
  python train.py --resume /模型目录/checkpoints/last/pretrained_model/train_config.json --steps 40000

默认扫描 ~/datasets/rebot 和 ~/.cache/huggingface/lerobot。也可在下面的
DATASET_REFERENCES 中直接填写数据集引用，再用 --dataset 别名选择。
数据集目录必须含 meta/info.json、data/ 和相机视频；请选择已经结束采集的数据。
输出默认 ~/models/rebot/数据集名_时间；会附带 dataset_reference.json 记录数据来源。
依赖：现有 LeRobot + PyTorch/torchvision + PyAV；无需 Web、相机或机械臂。
训练日志 step=优化器更新次数，loss=训练损失；不会把 9800 等步数缩写成 10K。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys


# 训练师可以在这里维护引用；也可以不改代码，直接传 --dataset-dir。
DATASET_REFERENCES = {
    '快递4': Path.home()/'.cache/huggingface/lerobot/4',
    '快递示教': Path.home()/'.cache/huggingface/lerobot/test',
}
DATASET_ROOTS = [Path.home()/'datasets/rebot', Path.home()/'.cache/huggingface/lerobot']
MODEL_ROOT = Path.home()/'models/rebot'


def read_json(path):
    try:
        value=json.loads(Path(path).read_text('utf-8'))
        if not isinstance(value,dict):raise ValueError('不是对象')
        return value
    except (OSError,ValueError) as error:
        raise ValueError(f'无法读取 {path}：{error}') from error


def datasets():
    references={}
    for alias,path in DATASET_REFERENCES.items():
        root=Path(path).expanduser().resolve()
        if (root/'meta/info.json').is_file():references[alias]=root
    seen=set(references.values())
    for base in DATASET_ROOTS:
        base=base.expanduser()
        if not base.is_dir():continue
        # Covers root/name and root/owner/name without walking image/video files.
        for pattern in ('*/meta/info.json','*/*/meta/info.json'):
            for info in sorted(base.glob(pattern)):
                root=info.parent.parent.resolve()
                if root in seen:continue
                alias=root.relative_to(base.resolve()).as_posix()
                if alias in references:alias=str(root)
                references[alias]=root;seen.add(root)
    return references


def metadata(root):
    root=Path(root).expanduser().resolve();info=read_json(root/'meta/info.json');features=info.get('features') or {}
    if int(info.get('total_episodes',0))<1 or int(info.get('total_frames',0))<1:
        raise ValueError('数据集没有已保存的剧集/帧，请先完成采集')
    if not list((root/'data').rglob('*.parquet')):raise ValueError('缺少 data/*.parquet 数据文件')
    if 'observation.state' not in features or 'action' not in features:raise ValueError('数据集缺少状态或动作特征')
    state=features['observation.state'].get('shape') or [];action=features['action'].get('shape') or []
    if len(state)!=1 or len(action)!=1 or min(state+action)<1:raise ValueError('状态/动作维度无效')
    cameras={key:spec for key,spec in features.items() if key.startswith('observation.images.')}
    if not cameras:raise ValueError('ACT 视觉采集数据集至少需要一路相机')
    shapes=set()
    for key,spec in cameras.items():
        shape=spec.get('shape') or []
        if len(shape)!=3 or shape[2]!=3 or min(shape)<1:raise ValueError(f'{key} 不是有效的 HWC RGB 图像')
        shapes.add(tuple(shape))
        if spec.get('dtype')!='video':raise ValueError('此单文件入口仅支持 LeRobot video 数据集')
        if not list((root/'videos'/key).rglob('*.mp4')):raise ValueError(f'缺少相机视频：{key}')
    if len(shapes)!=1:raise ValueError('ACT 的各路相机图像尺寸必须一致')
    return root,info


def checkpoint(source,filename):
    path=Path(source).expanduser().resolve()
    if path.is_file():path=path.parent
    for candidate in (path,path/'pretrained_model',path/'checkpoints/last/pretrained_model'):
        if (candidate/filename).is_file():return candidate
    raise ValueError(f'未找到 {filename}：{source}')


def compatible(policy,features):
    if policy.get('type')!='act':raise ValueError('此训练入口仅支持 ACT 检查点')
    expected={**policy.get('input_features',{}),**policy.get('output_features',{})}
    if 'action' not in expected or 'observation.state' not in expected:raise ValueError('检查点缺少动作/状态维度')
    if {k for k in expected if k.startswith('observation.images.')} != {k for k in features if k.startswith('observation.images.')}:
        raise ValueError('微调模型和新数据集的相机键/数量不一致；不会猜测相机对应关系')
    for key,spec in expected.items():
        actual=list((features.get(key) or {}).get('shape') or [])
        if key.startswith('observation.images.') and len(actual)==3:actual=[actual[2],actual[0],actual[1]]
        if actual!=list(spec.get('shape') or []):raise ValueError(f'特征不匹配：{key}，模型 {spec.get("shape")}，数据集 {actual}')


def parse(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    selection=parser.add_mutually_exclusive_group()
    selection.add_argument('--dataset',help='数据集引用别名，见 --list-datasets')
    selection.add_argument('--dataset-dir',type=Path,help='本地 LeRobot 数据集实际目录')
    parser.add_argument('--repo-id',help='可选的数据集标识；本地数据仍由 --dataset-dir 引用')
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--job-name')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--base-model',help='已有 ACT 检查点/训练输出目录，用于加数据微调')
    mode.add_argument('--resume',help='train_config.json 或包含它的检查点/训练输出目录')
    parser.add_argument('--steps',type=int,help='从头/微调默认 20000；续跑是目标总步数')
    parser.add_argument('--batch-size',type=int,help='从头/微调默认 4；续跑默认沿用原值')
    parser.add_argument('--device',choices=['cuda','cpu','mps'],help='默认 cuda；无显卡可选 cpu')
    parser.add_argument('--learning-rate',type=float,default=0,help='微调学习率；0 沿用模型默认值')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--save-every',type=int,default=1000)
    parser.add_argument('--log-every',type=int,default=20)
    parser.add_argument('--video-backend',choices=['pyav','torchcodec'],default='pyav')
    parser.add_argument('--chunk-size',type=int,help='仅从头训练；默认 100')
    parser.add_argument('--dim-model',type=int,help='仅从头训练；默认 ACT 架构')
    parser.add_argument('--dim-feedforward',type=int)
    parser.add_argument('--encoder-layers',type=int)
    parser.add_argument('--decoder-layers',type=int)
    parser.add_argument('--n-heads',type=int)
    parser.add_argument('--random-backbone',action='store_true',help='仅从头训练，不下载 ImageNet 初始权重')
    parser.add_argument('--list-datasets',action='store_true')
    parser.add_argument('--dry-run',action='store_true',help='校验数据/模型并显示引用、命令，不开始训练')
    parser.add_argument('--check',action='store_true',help='额外检查 Python/设备依赖，不开始训练')
    args=parser.parse_args(argv)
    if (args.steps is not None and args.steps<1) or (args.batch_size is not None and args.batch_size<1):parser.error('步数、批大小必须为正整数')
    if args.workers<0 or args.save_every<1 or args.log_every<1 or not math.isfinite(args.learning_rate) or args.learning_rate<0:parser.error('工作线程/保存频率/日志频率/学习率无效')
    architecture=[args.chunk_size,args.dim_model,args.dim_feedforward,args.encoder_layers,args.decoder_layers,args.n_heads]
    if any(v is not None and v<1 for v in architecture):parser.error('ACT 架构参数必须为正整数')
    if (args.base_model or args.resume) and (any(v is not None for v in architecture) or args.random_backbone):parser.error('微调/续跑保留检查点架构，不能修改 ACT 架构参数')
    if args.resume and (args.dataset or args.dataset_dir or args.repo_id or args.output_dir or args.learning_rate):parser.error('续跑沿用原数据集、输出目录及优化器；新增数据请用 --base-model')
    return args


def choose_dataset(args):
    if args.dataset_dir:return args.dataset_dir
    references=datasets()
    if args.dataset:
        if args.dataset not in references:raise ValueError('未找到数据集引用：'+args.dataset+'；请 --list-datasets 或使用 --dataset-dir')
        return references[args.dataset]
    if not references:raise ValueError('没有发现数据集，请指定 --dataset-dir /实际目录')
    entries=list(references.items())
    for index,(alias,path) in enumerate(entries,1):print(f'{index}. {alias}\n   {path}')
    if not sys.stdin.isatty():raise ValueError('非交互运行必须指定 --dataset / --dataset-dir')
    number=input('请选择要引用的数据集编号：').strip()
    if not number.isdigit() or not 1<=int(number)<=len(entries):raise ValueError('数据集编号无效')
    return entries[int(number)-1][1]


def prepare(args):
    if args.resume:
        model=checkpoint(args.resume,'train_config.json');config=model/'train_config.json';saved=read_json(config)
        if (saved.get('policy') or {}).get('type')!='act':raise ValueError('续跑配置不是 ACT')
        state=model.parent/'training_state';step=read_json(state/'training_step.json').get('step')
        for name in ('optimizer_state.safetensors','rng_state.safetensors'):
            if not (state/name).is_file():raise ValueError('续跑缺少训练状态 '+name+'；仅模型权重可用 --base-model 微调')
        if not (model/'model.safetensors').is_file():raise ValueError('续跑缺少模型权重')
        recorded_root=saved['dataset'].get('root')
        if not recorded_root:
            recorded_root=Path(os.environ.get('HF_LEROBOT_HOME',str(Path.home()/'.cache/huggingface/lerobot')))/saved['dataset']['repo_id']
        if not Path(recorded_root).expanduser().is_absolute():raise ValueError('续跑配置中的数据集引用为相对路径，请先改为实际绝对路径')
        root,info=metadata(recorded_root)
        compatible(read_json(model/'config.json'),info['features'])
        steps=args.steps or saved['steps']
        if steps<=int(step):raise ValueError(f'检查点已完成 {step} 步，目标总步数应更大')
        output=model.parent.parent.parent if model.parent.parent.name=='checkpoints' else Path(saved['output_dir']).expanduser().resolve()
        native=[f'--config_path={config}','--resume=true',f'--steps={steps}',f'--output_dir={output}',f'--dataset.root={root}']
        if args.device:native.append('--policy.device='+args.device)
        if args.batch_size:native.append('--batch_size='+str(args.batch_size))
        mode='resume';repo=saved['dataset']['repo_id'];device=args.device or saved['policy'].get('device') or 'cuda'
    else:
        root,info=metadata(choose_dataset(args))
        repo=args.repo_id or 'local/'+re.sub(r'[^A-Za-z0-9_-]','_',root.name)
        if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_-]+',repo):raise ValueError('repo-id 格式应为 用户/数据集；本地路径由 --dataset-dir 指定')
        name=args.job_name or root.name
        output=(args.output_dir or MODEL_ROOT/(re.sub(r'[^A-Za-z0-9_-]','_',name)+'_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))).expanduser().resolve()
        if output.exists():raise ValueError('输出目录已存在，不能覆盖已有模型；请换 --output-dir，或使用 --resume')
        native=[f'--dataset.repo_id={repo}',f'--dataset.root={root}',f'--dataset.video_backend={args.video_backend}']
        if args.base_model:
            model=checkpoint(args.base_model,'config.json');policy=read_json(model/'config.json');compatible(policy,info['features'])
            for filename in ('model.safetensors','policy_preprocessor.json','policy_postprocessor.json'):
                if not (model/filename).is_file():raise ValueError('微调检查点不完整，缺少 '+filename)
            if output==model or output.is_relative_to(model):raise ValueError('微调必须写入新的模型输出目录')
            native.append('--policy.path='+str(model));mode='finetune'
        else:
            native.append('--policy.type=act');mode='scratch'
            mapping={'chunk_size':args.chunk_size,'n_action_steps':args.chunk_size,'dim_model':args.dim_model,'dim_feedforward':args.dim_feedforward,'n_encoder_layers':args.encoder_layers,'n_decoder_layers':args.decoder_layers,'n_heads':args.n_heads}
            native.extend(f'--policy.{key}={value}' for key,value in mapping.items() if value is not None)
            if args.random_backbone:native.append('--policy.pretrained_backbone_weights=null')
        steps=args.steps or 20000;device=args.device or 'cuda'
        native += [f'--output_dir={output}',f'--job_name={name}',f'--policy.device={args.device or "cuda"}',f'--steps={steps}',f'--batch_size={args.batch_size or 4}',f'--num_workers={args.workers}',f'--save_freq={min(args.save_every,steps)}',f'--log_freq={args.log_every}','--eval_freq=0','--wandb.enable=false','--policy.push_to_hub=false']
        if args.learning_rate:native.extend([f'--policy.optimizer_lr={args.learning_rate}',f'--policy.optimizer_lr_backbone={args.learning_rate}'])
    reference={'mode':mode,'dataset_dir':str(root),'repo_id':repo,'episodes':info['total_episodes'],'frames':info['total_frames'],'state_dimension':info['features']['observation.state']['shape'][0],'action_dimension':info['features']['action']['shape'][0],'action_names':info['features']['action'].get('names'),'state_names':info['features']['observation.state'].get('names'),'cameras':{k:v['shape'] for k,v in info['features'].items() if k.startswith('observation.images.')},'metadata_sha256':hashlib.sha256((root/'meta/info.json').read_bytes()).hexdigest(),'base_model':str(model) if args.base_model or args.resume else None,'output_dir':str(output),'steps':steps,'device':device}
    return native,reference


def main(argv=None):
    args=parse(argv)
    if args.list_datasets:
        references=datasets()
        for alias,path in references.items():
            try:
                info=read_json(path/'meta/info.json');print(f'{alias}\n  {path}\n  {info.get("total_episodes",0)} 集 / {info.get("total_frames",0)} 帧')
            except ValueError as error:print(error,file=sys.stderr)
        if not references:print('未发现数据集，请在 DATASET_REFERENCES 添加引用，或使用 --dataset-dir。')
        return 0
    native,reference=prepare(args)
    print('本次训练的数据集引用：\n'+json.dumps(reference,ensure_ascii=False,indent=2))
    print('原生 LeRobot 参数：\n'+shlex.join([sys.executable,'-m','lerobot.scripts.lerobot_train',*native]))
    if args.dry_run:return 0
    for name in ('REBOT_TELEMETRY_SOCKET','REBOT_TELEMETRY_KEY','REBOT_TELEMETRY_PORTS','REBOT_CAMERA_BROKER_URL'):os.environ.pop(name,None)
    import torch
    from lerobot.utils.logging_utils import MetricsTracker
    from lerobot.scripts.lerobot_train import main as official_main
    device=reference['device']
    if device=='cuda' and not torch.cuda.is_available():raise RuntimeError('当前环境 CUDA 不可用，可明确指定 --device cpu')
    if args.check:print('LeRobot/PyTorch 及训练设备检查通过，未开始训练。');return 0
    original=MetricsTracker.__str__
    def exact_steps(tracker):return re.sub(r'^step:\S+','step:'+str(tracker.steps),original(tracker),count=1)
    MetricsTracker.__str__=exact_steps
    old_argv=sys.argv;sys.argv=['lerobot-train',*native]
    try:official_main()
    finally:
        sys.argv=old_argv;MetricsTracker.__str__=original
        output=Path(reference['output_dir'])
        if output.is_dir():
            try:
                target=output/'dataset_reference.json';temp=output/'.dataset_reference.tmp'
                temp.write_text(json.dumps(reference,ensure_ascii=False,indent=2)+'\n');os.replace(temp,target)
            except OSError as error:print('数据集引用记录保存失败：'+str(error),file=sys.stderr)
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:print('训练已中断；可用最近完整检查点 --resume 续跑。',file=sys.stderr);raise SystemExit(130)
    except Exception as error:print('训练未完成：'+str(error),file=sys.stderr);raise SystemExit(1)
