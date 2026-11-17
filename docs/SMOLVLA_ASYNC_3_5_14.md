# 3.5.14：SmolVLA 异步推理与反馈跟随

本次按用户当前使用的 `outputs/train/tese5/checkpoints/002000/pretrained_model`（type=smolvla）修复。WALL-X训练/部署工作暂放一边，相关新增文件保留，未安装其依赖或启动训练。

## 原因与修改

- 原Web验证函数只接受ACT，异步钩子也只接管ACT，子进程固定加载ACTPolicy。SmolVLA原来无法使用此异步选项；现在API、钩子和子进程均支持ACT/SmolVLA，按检查点type动态选择策略类。
- SmolVLA配置没有ACT的temporal_ensemble_coeff，不能直接访问。现在分别校验ACT时序集成及SmolVLA RTC，避免属性异常或重复启用两套调度。
- 保留检查点的归一化统计、语言分词器与三相机输入。在完整微调权重加载前关闭第二次VLM基座权重加载，缓存中的VLM配置/processor/分词器直接使用本地路径，避免重复下载或联网HEAD等待。
- 预热使用隔离随机数上下文，真实推理继承父进程初始seed。SmolVLA的随机扩散不再因预热而额外消耗随机序列；没有挑选固定生产seed或通过修改模型权重规避安全检查。
- 重叠预取不再覆盖未执行的旧块后缀。否则持续从新预测的静止前缀重来，可能始终执行不到后面的动作。0表示沿用检查点n_action_steps，不再默认截掉长ACT块后半段。显式设置短块仍会缩短执行范围。
- 命令按电机空间限速，并在实测反馈前方6°内推进；臂关节使用相同插值比例，夹爪接触独立处理。6°不是放宽物理范围，原有物理限位和30°失跟保护仍在。持续领先且2秒内向目标移动不足0.5°会触发停滞停止，不能靠不断保持来隐瞒电机不响应。
- 启动命令与子进程环境使用同一份配置快照。启动请求携带本次点击选中的评估参数，避免自动保存竞态改变实际模式。API与界面显示inference_mode和policy_type，明确区分SmolVLA/ACT与同步/异步。
- `sorting_operator/rebot_sort_support`同步更新；独立执行器也能加载同契约的SmolVLA，默认随包ACT权重未更换。

## 无硬件验证

使用真实 `tese5/002000` 权重、all_1中的三路录制RGB与初始状态，独立CUDA子进程返回 `[1,50,14]` 动作块。没有打开串口、CAN或摄像头，反馈来自虚拟跟随器，不代表实机包裹操作成功。

- 同样输入、同样seed=7，原生 `select_action` 首步与 `predict_action_chunk` 经相同预/后处理后的首步最大差值为0。说明动作格式、归一化和语言路径对齐。
- seed=7的8秒虚拟跟随测试：235帧有效输出，首个预测后等待帧0，过期块0；首个预测约168ms，控制调用最长约10.54ms。170帧触发反馈限速，最大命令步进约2°、最大领先约6°（float32误差为几微度）。性能仅适用于本次测量，不保证真实CAN/相机闭环时序。
- 一次未固定seed测试中，模型输出左肩电机目标+8.94°，超出当前[-170°,0°]配置，安全校验正确拒绝。该检查点训练2000步，不能因为某次种子测试通过就认定所有随机输出或四类分拣场景均可执行；没有放宽目标越界拦截。

日志：`/tmp/rebot-smolvla-async-seeded-gpu.log`、`/tmp/rebot-smolvla-async-gpu.log`、`/tmp/rebot-smolvla-native-compare.log`。`backend.async_diagnostics`分别报告模型是否返回预测和动作是否通过校验，避免把拒绝不安全动作误报为模型没推理。

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python -m backend.async_diagnostics \
  --model outputs/train/tese5/checkpoints/002000/pretrained_model \
  --dataset-dir /home/legion/.cache/huggingface/lerobot/all_1 \
  --device cuda --seconds 8 --seed 7
```

界面选择SmolVLA检查点，启用“异步推理（ACT / SmolVLA）”，每段步数设0即可沿用模型。只有启动新的评估任务才应用新模型/模式；本次没有自动启动实机评估或改动任何机械臂零点、方向、增益或力矩阈值。Web常驻进程需要重启后才能加载更新后的API代码。
