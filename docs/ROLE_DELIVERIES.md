# 人员代码交付

| 人员 | 主入口 | 同目录关联文件 |
|---|---|---|
| 数据采集师 | data_collector/collect.py | 修复后的B601/Leader插件源码、独立录制与视频收尾、安全信号处理、依赖与说明 |
| 模型训练师 | model_trainer/train.py | export_handoff.py 技能入口范围导出、依赖、数据/模型交接说明 |
| 第四位分拣执行人员 | sorting_operator/run_sorting.py | 三路相机采集、ACT异步推理与动作缓冲、看门狗/慢速收工、扫码、官方DM模型和硬件配置 |

每个目录都是一个交付单元，运行一个主入口即可。项目第三方库仍由各自Python环境提供；本地修复后的关联代码已随目录携带，没有假装把整个LeRobot/PyTorch虚拟环境塞进一个Python文件。采集/训练源码包不含后台config.json、用户校准、数据集或密钥；分拣目录另包含当前单ACT演示所需的检查点和归一化文件。

数据采集师把完整 meta/data/videos 交给训练师；训练师把整个 pretrained_model、dataset_reference.json 和技能入口 JSON 交给执行人员。执行人员配置实际模型路径、三路相机键、工作区/出料区域、力矩阈值，再运行 --check。示例配置不会使能机械臂。

默认按用户选择以现有单个ACT连续演示完整流程；独立技能模型切换是后续选项。分拣业务主程序是本次新增执行入口，不能把已有评估代码换个文件名就视为比赛已部署。真实硬件执行仍需完成各目录 README 里的项目参数和现场验证；自动生成的空模型路径或猜测的轨迹不能安全取代它们。

每个目录带 bundle_manifest.json 和 verify_bundle.py，完整性校验通过后可整目录发送。压缩包放在 dist/role_bundles，重新生成使用：

```bash
python scripts/package_roles.py
```

脚本打包当前交付目录，不包含 __pycache__、临时日志、用户自定义 sorting.json、未列入交接清单的模型权重或运行数据。关联源码发生变更时需先同步对应交付目录，再重新生成校验清单和压缩包。
