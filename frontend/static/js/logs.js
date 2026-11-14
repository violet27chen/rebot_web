/* 日志与命令：全局状态、终端日志、Loss 指标 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "logs", title: "日志与命令", subtitle: "真实终端输出与训练指标" });
  A.attachLogPanel(A.el("logPanel"));

  var series=A.lossSeries(600);

  function draw() {
    var losses=series.points(),axes=A.lossAxes(series.unit());
    A.chart(A.el("lossChart"), losses,axes);
    A.el('lossAxes').textContent='X：'+axes.xLabel+' · Y：'+axes.yLabel+' · 显示最近 600 个训练位置';
    A.el("lossCount").textContent = losses.length + " 个点";
  }

  function renderStatus(s) {
    if (!s) return;
    var rt = A.runtime() || {}, rc = rt.client || {}, d = A.clientPrefs();
    A.el("kvStatus").innerHTML = [
      ["真机系统", s.platform], ["运行环境", s.active_env || "未选择"],
      ["访问设备", (d.alias || rc.alias || (rc.local ? "本机浏览器" : "未命名设备")) +
        " · " + (rc.ip || "IP 未知")],
      ["机械臂", s.connected ? (s.leader_port + " / " + s.follower_port) : "未连接"],
      ["串口状态", s.serial && s.serial.requires_chmod === false
        ? "无需授权（Windows COM 端口）"
        : (s.serial && s.serial.all_writable ? "已就绪" : "需授权")],
      ["当前任务", s.job ? s.job.name : "无"],
      ["耗时", s.job ? (s.job.elapsed + " s") : "-"],
      ["退出码", s.job && s.job.exit_code !== null ? s.job.exit_code : "-"]
    ].map(function (p) {
      return "<dt>" + A.esc(p[0]) + "</dt><dd>" + A.esc(p[1]) + "</dd>";
    }).join("");
  }

  var STATUS_LOADING = {
    title: "正在读取运行状态…",
    detail: "正在读取 /api/status 的真实状态", rows: 3
  };
  async function loadStatus() {
    A.showLoading("#kvStatus", STATUS_LOADING);
    try {
      renderStatus(await A.api.get("/api/status"));
    } catch (e) {
      A.loadError("#kvStatus", e, loadStatus);
    }
  }
  A.onStatus(renderStatus);   // 状态轮询成功时自动覆盖加载/失败状态框
  A.onTheme(function () { draw(); });
  A.onLog(function (ev) {
    if(series.add(ev))draw();
  });
  A.logs().forEach(function (ev) {
    series.add(ev);
  });
  draw();

  A.click("btnStop", function () {
    A.action(async function () {
      await A.stop();
      A.ok("已请求停止；归零任务会从当前位置慢速收工", "#logsAlert");
      A.refreshStatus();
    }, "#logsAlert");
  });
  A.click("btnRefresh", function () {
    A.action(async function () {
      A.showLoading("#kvStatus", STATUS_LOADING);
      try {
        renderStatus(await A.api.get("/api/status"));
        A.ok("状态已刷新", "#logsAlert");
      } catch (e) {
        A.loadError("#kvStatus", e, function () { A.el("btnRefresh").click(); });
        throw e;
      }
    }, "#logsAlert");
  });
  A.click("btnClearLog", function () {
    A.el("logPanel").innerHTML = "";
    A.waitingLog(A.el("logPanel"), "显示已清空", "历史日志仍保留在后端，新日志会继续实时显示");
    A.ok("已清空显示（历史日志仍在后端）", "#logsAlert");
  });
  window.addEventListener("resize", draw);
  loadStatus();
})();
