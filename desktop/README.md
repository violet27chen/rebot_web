# reBot 桌面客户端 v3.3.0

这是 Electron 桌面客户端：独立窗口、系统菜单、任务栏图标，无需打开浏览器。Windows 与 Ubuntu 均可在本机启动控制服务；相机采集、串口/CAN 控制、推理和任务都运行在本机后端。

## Ubuntu 安装

```bash
sudo apt install ./dist/rebot-console_3.3.0_amd64.deb
```

安装后从应用列表打开 **reBot 机械臂控制台**，或运行 `rebot-console`。默认连接 `http://127.0.0.1:8000`，优先复用已有后端。服务未启动时，可自动启动本机项目；Linux 默认使用 `~/miniconda3/envs/lerobot/bin/python`，Windows 默认使用 `~/miniconda3/envs/lerobot/python.exe`。项目和 Python 路径可在“文件 → 连接设置”修改并自动保存。

本包包含桌面运行时与客户端，不包含 CUDA、LeRobot、机器人 SDK、模型、数据集或私有 `backend/config.json`。当前 Ubuntu 上已安装的环境和配置直接复用；换到一台新机器时仍需要部署 Ubuntu 控制后端。

安装包按正常系统沙箱方式运行，没有添加 `--no-sandbox`。构建阶段不自动执行 sudo 安装，也不修改串口或 USB 权限；安装时由 apt 完成标准桌面组件与沙箱配置。

## Windows 使用

运行 `dist/reBot-Console-3.3.0-Windows-x64.exe`（免安装版），保持地址为 `http://127.0.0.1:8000`，启用“服务未启动时，自动启动本机控制后端”。在本机启动设置中选择 `rebot_web` 项目目录和 LeRobot 环境的 `python.exe`。启动后，Web 服务、串口、相机和硬件控制均由 Windows 本机执行；USB 转串口设备需要先在设备管理器中安装驱动并分配 COM 端口。

Windows 免安装程序未进行商业代码签名；本地硬件控制仍需在目标 Windows 主机上安装依赖并完成设备验收。

## 操作

- 所有控制页面在桌面窗口内使用，常驻摄像头、真实深度和 3D 场景继续工作。
- 原生“工作区”菜单切换平台功能，通过已有导航操作保留监视连接。
- 连接地址、项目位置、Python 路径、启动选项和窗口尺寸保存到当前用户的 `rebot-console/connection.json` 应用配置目录。
- 修改连接设置会取消旧的等待连接；错误显示具体原因，可重试，不会永久显示加载占位。
- 桌面客户端的未提交草稿持久保留，网络中断或关闭窗口后可在下次打开对应页面恢复；退出客户端前刷新待保存设置；本机控制后端作为独立服务继续存在。需要释放设备时，使用顶部“断开全部设备”，或原生菜单“文件 → 断开设备并退出”。该操作使用现有全设备释放逻辑。
- 重新加载界面：Ctrl+R；连接设置：Ctrl+,；开发者工具：Ctrl+Shift+I。

## 构建

```bash
npm ci
node build-icon.cjs
npm run build:deb
npm run build:win
```

依赖版本在 `package-lock.json` 固定。图标从项目原有 SVG 转换，没有另绘机器人模型或品牌资产。

`build:win` 生成免安装 EXE，支持 Linux 交叉打包；`build:win-installer` 是可选 NSIS 安装器构建，在 Linux 上需要 Wine，不是本次交付物。`build:win-zip` 可另外生成 ZIP。

## 代码检查

```bash
npm test
node --check src/main.cjs
```

只检查配置隔离、Ubuntu 服务复用和打包内容，不执行遥操作、抓取、串口连接或其他真机验收。

安全配置参考 Electron 官方文档：[沙箱](https://www.electronjs.org/docs/latest/tutorial/sandbox)、[上下文隔离](https://www.electronjs.org/docs/latest/tutorial/context-isolation)。平台页面禁用 Node integration，开启 contextIsolation、sandbox 和 webSecurity；进程/配置 IPC 仅开放给安装包自带的本地连接设置页。
