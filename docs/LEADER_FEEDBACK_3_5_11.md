# 主臂近零反馈与无桌面录制控制 · 3.5.11

完整版本为 `3.5.11+20261116.02`。

## 已确认原因

先前把 SDK 的 `reliable=False` 全部解释为“无新回复”，导致正常零位被误判。SDK 的角度可靠性过滤器还会在原始角度接近零时暂时保持上次过滤后的角度，防止上电出现 A→0→B 跳变。它的零位阈值为 Rust `0.2_f32`，转换到 Python 双精度后是 `0.20000000298023224`；使用 Python 字面量 `0.2` 判断会误拒绝该边界，已通过 `/dev/ttyUSB1` 夹爪 ID 6 的只读反馈复现。

参考官方源码：[角度过滤](https://github.com/motorbridge/motorbridge-smart-servo/blob/main/smart_servo_core/src/reliability.rs)、[通信与丢包计数](https://github.com/motorbridge/motorbridge-smart-servo/blob/main/smart_servo_vendors/fashionstar/src/controller.rs)。不能只根据可靠性标志判断串口是否丢包，也不能直接接受不可靠样本里的 raw 零值作为目标。

## 修复行为

- 主臂 SDK 的软件丢包阈值设为 1。单次无回复立即抛出通信异常，避免 SDK 返回超时缓存；这个设置不写舵机寄存器或零点。
- 正常批量读取；缺少有效值时逐关节查询。已确认收到回复、原始角度在 SDK 零位范围内时继续采用 SDK 的 filtered 值，保留防抖，不将 raw 零值当作新目标。
- 比较零位边界时使用与 Rust 相同的 float32 值，兼容正负 0.2°。非有限值、范围外的不可靠样本、持续无回复继续拒绝。
- 重读使用完整的 250ms 时间预算，不再因固定四轮提前在 88ms 退出。该预算在各次 SDK 调用之间检查，单次底层调用仍受 SDK 自身超时控制；超时后的迟到结果也不会作为有效批次输出。
- 错误包含端口、缺失关节以及最近被拒绝的原始值、过滤值、可靠性标志。没有无限复用上次动作，没有自动重置主从臂零点。

正常收尾期间屏蔽再次到达的 SIGINT，避免重复普通停止打断慢速归零；收尾结束恢复原处理器。SIGTERM 未屏蔽，强制停止仍走原独立路径。遥操作清理使用 finally 保证从臂异常时仍能关闭主臂，不依赖解释器析构时清理串口。

## systemd 下的采集控制

Web 采集/评估入口提供基于其私有 stdin 管道的命令通道。原生录制完成连接与校准后才启动控制读取线程，因此不会抢读校准 input()。网页收到 READY 后才允许发送 right/left/esc；命令绑定具体 job_id，不注入全局桌面按键，也不发送停止信号。EOF 表示 Web 父进程退出，录制设置正常结束标志。

Web 使用自己的摄像头监视，不导入 pynput、不要求 DISPLAY。普通原生 LeRobot CLI 和独立采集师脚本仍可使用自己的输入控制。只有新版 Web 父进程明确传入能力标志时才安装此通道，避免旧后端/新子进程混用控制协议。

## 验证与生效

模拟测试覆盖近零过滤、正负 float32 边界、丢包后重读、超时、非有限样本、不重放旧会话角度、重复普通停止和主臂最终清理；网页命令测试不启动 xdotool，也不发信号或打开设备。实际只读诊断中，主臂 0～6 均响应，左右主臂各完成 30 轮读取。没有发送从臂动作、失能、置零或解锁指令。

驱动修复已经安装到当前 LeRobot 环境，重新启动采集/遥操作进程即可加载。新的网页命令路由和版本显示需要在没有任务运行时重启后端。重装环境后执行：

```bash
python scripts/apply_leader_startup.py
python scripts/apply_slow_homing.py
python scripts/apply_runtime_safety.py
```
