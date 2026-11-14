/* 总览 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "home", title: "总览", subtitle: "平台状态与快捷入口" });
  A.attachLogPanel(A.el("logPanel"));

  function kv(id, pairs) {
    var n = A.el(id);
    if (!n) return;
    n.innerHTML = pairs.map(function (p) {
      return "<dt>" + A.esc(p[0]) + "</dt><dd>" + A.esc(p[1]) + "</dd>";
    }).join("");
  }

  function render(s) {
    if (!s) return;
    var connectLink = A.el('homeConnectLink');
    if (connectLink) {
      connectLink.href = '/p/connect';
      connectLink.removeAttribute('aria-disabled'); connectLink.removeAttribute('tabindex');
    }
    kv("kvEnv", [
      ["真机系统", s.platform], ["虚拟环境", s.active_env || "未选择"],
      ["数据集目录", s.lerobot_home], ["输出目录", s.outputs_dir]
    ]);
    var dual = s.dual_arm || {};
    kv("kvArm", [
      ["运行模式", dual.enabled ? "双臂 · 复合机器人" : "单臂"],
      ["主臂", s.leader_type], ["从臂", s.follower_type],
      ["CAN 适配器", s.can_adapter],
      ["复合类型", dual.enabled ? (dual.robot_type || "未设置") : "-"],
      ["连接状态", dual.enabled
        ? (s.dual_connected ? "四路串口已连接" : "待连接四路串口")
        : (s.connected ? (s.leader_port + " / " + s.follower_port) : "未连接")],
      ["串口状态", s.serial && s.serial.requires_chmod === false
        ? ("无需授权 · Windows COM" + (s.serial.devices && s.serial.devices.length
          ? "（" + s.serial.devices.length + " 个端口）" : "（未检测到端口）"))
        : (s.serial && s.serial.all_writable ? "已就绪" : "需授权")]
    ]);
    kv("kvJob", [
      ["运行状态", s.running ? "运行中" : "空闲"],
      ["任务", s.job ? s.job.name : "-"],
      ["耗时", s.job ? (s.job.elapsed + " s") : "-"],
      ["命令", s.job ? s.job.command : "-"]
    ]);
  }

  /* 三个状态卡片在结果返回前显示加载状态框，失败时可就地重试 */
  var KV = [["kvEnv", "运行环境"], ["kvArm", "机械臂连接"], ["kvJob", "当前任务"]];
  async function loadStatus() {
    KV.forEach(function (p) {
      A.showLoading(p[0], {
        title: "正在加载" + p[1] + "…",
        detail: "正在读取 /api/status 的真实状态", rows: 3
      });
    });
    try {
      render(await A.api.get("/api/status"));
      A.clearAlert("#homeAlert");
    } catch (e) {
      KV.forEach(function (p) { A.loadError(p[0], e, loadStatus); });
      A.setAlert("#homeAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }
  A.onStatus(render);   // 每 4 秒状态轮询成功时会自动覆盖失败状态框
  loadStatus();

  /* 两端信息：服务端真机 vs 当前打开网页的设备，避免把服务器状态误读成本机状态 */
  function renderRuntime(r) {
    var s = r.server || {}, c = r.client || {}, d = A.clientPrefs();
    var ip = (s.lan_ips || [])[0] ||
      (s.listen_host && !/^0\.0\.0\.0$/.test(s.listen_host) ? s.listen_host : "127.0.0.1");
    kv("kvServer", [
      ["主机名", s.hostname || "-"],
      ["真机系统", s.platform || "-"],
      ["局域网地址", "http://" + ip + (s.port ? ":" + s.port : "")],
      ["Python", s.python || "-"],
      ["项目目录", s.root || "-"],
      ["后端进程", "PID " + (s.pid || "-")]
    ]);
    kv("kvClient", [
      ["本设备名称", d.alias || c.alias || (c.local ? "本机浏览器" : "未命名设备")],
      ["访问 IP", c.ip || "-"],
      ["浏览器 / 系统", (c.browser || "-") + " / " + (c.os || "-")],
      ["访问方式", c.local ? "在真机本机打开" : "局域网远程访问"],
      ["界面配置", "仅当前浏览器 localStorage"],
      ["浏览器语言", c.language || "-"]
    ]);
  }
  ["kvServer", "kvClient"].forEach(function (id) {
    A.showLoading(id, {
      title: "正在检测设备与访问来源…",
      detail: "正在读取 /api/runtime 的真实服务端与客户端信息", rows: 4
    });
  });
  A.onRuntime(renderRuntime, function (e) {
    ["kvServer", "kvClient"].forEach(function (id) {
      A.loadError(id, e, function () { A.refreshRuntime().catch(function () {}); });
    });
  });

  A.click("btnStop", function () {
    A.action(async function () {
      await A.stop();
      A.ok("急停已下发，正在终止当前任务", "#homeAlert");
      A.refreshStatus();
    }, "#homeAlert");
  });
})();
