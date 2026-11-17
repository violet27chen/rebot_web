# 数据采集师交付目录

主入口是 `collect.py`。把整个文件夹交给采集师，不需要原 Web 项目。`vendor/` 包含当前修复后的 B601、Leader 驱动与遥测兼容代码；`collector_support/` 包含本机验证过的 LeRobot 录制入口与收工信号保护。这些属于源码关联文件，不能漏发。LeRobot、PyTorch、motorbridge 的已安装依赖仍由 Python 环境提供，没有把整个虚拟环境复制进来。

Ubuntu、当前 LeRobot 0.4.4 接口。现有环境可直接使用；新环境先安装本目录 requirements.txt。单独只复制 collect.py 时，仍兼容已配置好的原环境，但不会携带本次修复后的驱动，跨机器请发整个目录。

```bash
python collect.py --dry-run
python collect.py --check
python collect.py --skill push --episodes 10
python collect.py --skill flip_box --episodes 20
python collect.py --skill flip_bag --episodes 20
```

默认从臂左 ACM0、右 ACM1，主臂左 USB0、右 USB1；这只是显式参数默认值，不是自动识别左右。按实际接线用 --left-follower / --right-follower / --left-leader / --right-leader 修改，优先使用稳定设备路径。默认 RGB 三路：左=0、深度相机=4、右=6；相机名“深度相机”不代表深度图。修改相机名会改变数据特征，后续训练与执行必须一致。

预览 http://127.0.0.1:8765。终端 n 回车切换阶段，r 回车重录，q 回车保存结束；Ctrl+C 请求保存并慢速收工。每项技能独立数据集，不做扫码/OCR，不自动杀死其他设备进程。

默认数据写入 ~/datasets/rebot/collector/技能_时间。给训练师交接整个数据集目录，保留 meta、data、videos 全部内容。双臂动作 14 维，先左7关节后右7关节；状态是原始电机角度（度），动作是方向/夹爪比例转换之前的主臂角度（度）。不要把状态直接作为动作重放。交接模型时不得把每集的零位起落片段误当成通用技能入口。

`python verify_bundle.py` 校验交付源码是否完整。依赖版本见 dependency_versions.json；不包含用户数据集、校准文件或凭据。
