# 3.5.16：明确配置 n_action_steps

LeRobot 的正式参数名是 `n_action_steps`（复数）。评估页的“每块执行步数”现在使用这个名称和配置键，避免把它与模型预测长度 `chunk_size` 混淆。

- `chunk_size`：模型每次生成的预测长度，本次不改检查点与网络结构。
- `n_action_steps`：每块采用的前N个动作。比如模型 `chunk_size=50`，设置16后，每块采用前16步；同步运行随后重新预测，Async则在执行过程中提前预取后续块。
- 输入0：沿用检查点保存的 `n_action_steps`。显式正整数1–1000会限制在 `chunk_size` 内；它可以大于原检查点的执行长度，但不能大于预测长度。
- 正式启动命令传递 `--policy.n_action_steps=16`。异步进程重新读取检查点JSON后会再次应用覆盖，避免回退为旧执行长度；API、CLI和异步环境使用同一配置快照。
- 配置兼容：新 `eval.n_action_steps` 优先于旧 `eval.async_chunk_size`，显式0也优先。只有缺少新字段时读取旧字段。界面自动读取旧值并在自动保存时写入新键；没有两个互相冲突的可见执行长度输入。
- 就绪状态显示 `n_action_steps=N / chunk_size=M`。修改运行中表单只影响下次启动，不能中途改变已提交的动作块。

可先比较16步与当前50步的表现，Flow Matching保持0沿用原模型10次，Async保持开启。执行步数变小会提高推理请求频率，不能据此保证模型效果改善或没有等待。既有力矩、限位、反馈、速度及收工保护保持启用。

独立执行器使用 `sorting.json` 的 `inference.n_action_steps`，同样兼容旧 `inference.chunk_size`。诊断入口增加 `--n-action-steps 16`：

```bash
python -m backend.async_diagnostics \
  --model outputs/train/tese5/checkpoints/002000/pretrained_model \
  --dataset-dir /home/legion/.cache/huggingface/lerobot/all_1 \
  --device cuda --seconds 5 --seed 7 --n-action-steps 16
```

本次验证为53项针对性回归测试，包含真实独立CPU模型进程返回16步、保持20步模型预测长度、同步/异步命令、旧参数兼容、显式0优先、子进程覆盖与安全保护。没有运行上述GPU诊断命令：当时检测到现有真机评估任务正在运行，未另启GPU推理、终止评估或重启服务。没有打开串口、CAN或摄像头。

常驻服务需在现有任务收尾结束后重启，才能加载新API和界面：

```bash
sudo systemctl restart rebot-web
```
