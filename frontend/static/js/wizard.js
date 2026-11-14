/* 启动向导：虚拟环境 → 串口授权 → 串口连接 → 进入主界面 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({
    active: "", minimal: true, gate: false,
    title: "启动向导",
    subtitle: "真机模式 · 所有结果来自真实系统，连接失败会明确报错"
  });
  A.attachLogPanel(A.el("logPanel"));
  A.mountEnvLocation("envLocationBox", { nameInput: "newEnvName" });
  A.mountLerobotCard("lerobotCard", {
    alert: "#envAlert",
    onUse: function () { loadEnv(true); }   // 复用后刷新门禁状态
  });

  var gate = new URLSearchParams(location.search).get("gate");
  if (gate === "env") {
    A.setAlert("#wizAlert", "必须先选择虚拟环境，才能进入主界面。", "warn");
  } else if (gate === "connect") {
    A.setAlert("#wizAlert", "请先在下方选择串口连接主从臂。", "warn");
  }

  var state = { envReady: false, connected: false, permOK: false,
                lerobot: false, activeEnv: "", dualMode: false };

  /* Windows 服务端：串口是 COM 端口，没有 /dev 与 sudo，把第 2 步改成「串口检测」 */
  var winMode = !!(A.status() && A.status().is_windows);
  function applyWinUI() {
    if (!winMode) return;
    var t = A.el("permTitle"), s = A.el("permSub"), n = A.el("permNote");
    if (t) t.textContent = "2 · 串口检测（Windows · COM 端口）";
    if (s) s.textContent = "Windows 无需 sudo 授权；端口由设备驱动提供，可随时重新扫描";
    if (n) n.innerHTML = "在「设备管理器 → 端口 (COM 和 LPT)」确认主从臂的 COM 编号与 USB 转串口驱动；" +
      "端口打不开时，先关闭占用它的其它程序再重新扫描。";
    var f = A.el("sudoPwdField"); if (f) f.hidden = true;
    var c = A.el("btnChmod"); if (c) c.textContent = "重新扫描串口";
    var k = A.el("btnCheckPerm"); if (k) k.textContent = "检查串口状态";
    var m = A.el("permModeTh"); if (m) m.textContent = "类型";
    var st = A.el("permStateTh"); if (st) st.textContent = "状态";
    var step = A.el("stepPerm"); if (step) step.textContent = "2 · 串口检测";
  }

  function setStep(id, cls) {
    var n = A.el(id);
    if (n) n.className = "s " + cls;
  }
  function updateGate() {
    var ready = state.envReady && state.connected;
    var btn = A.el("btnEnter");
    btn.style.pointerEvents = ready ? "auto" : "none";
    btn.style.opacity = ready ? "1" : ".5";
    A.el("readyHint").textContent = ready ? "前置条件已满足" : "等待前置步骤完成";
    var envLabel = state.activeEnv ? "虚拟环境 已选择" :
      (state.lerobot ? "LeRobot 已安装 · 直接复用" : "虚拟环境 未选择");
    A.el("readyDetail").textContent = envLabel + " · " +
      (state.dualMode ? "双臂 " : "机械臂 ") + (state.connected ? "已连接" : "未连接") + " · " +
      (winMode ? "串口检测 " : "串口权限 ") + (state.permOK ? "就绪" : "待确认");
    setStep("stepEnv", state.envReady ? "done" : "current");
    setStep("stepPerm", state.permOK ? "done" : (state.envReady ? "current" : ""));
    setStep("stepConn", state.connected ? "done" : (state.permOK ? "current" : ""));
    setStep("stepDone", ready ? "current" : "");
  }

  async function loadEnv(quiet) {
    A.showLoading("#envSelect", {
      title: "正在检测 conda 虚拟环境…",
      detail: "正在读取 /api/env/status 的真实环境列表"
    });
    try {
      var s = await A.api.get("/api/env/status");
      state.dualMode = Boolean(s.dual_arm && s.dual_arm.enabled);
      state.envReady = Boolean(s.ready);
      state.connected = state.dualMode ? Boolean(s.dual_connected) : Boolean(s.connected);
      state.lerobot = Boolean(s.lerobot_installed);
      var wizBanner = A.el("dualWizBanner");
      if (wizBanner) wizBanner.hidden = !state.dualMode;
      var connBtn = A.el("btnConnect");
      if (connBtn) connBtn.textContent = state.dualMode ? "前往设备页连接四路串口" : "连接";
      state.activeEnv = s.active || "";
      var sel = A.el("envSelect");
      if (s.envs.length) {
        A.clearState(sel);
        sel.innerHTML = s.envs.map(function (e) {
          // value 用完整路径：自定义位置下可能有同名环境，按路径选择才不会选错
          return '<option value="' + A.esc(e.path) + '"' + (e.active ? " selected" : "") +
            ">" + A.esc(e.name) + " — " + A.esc(e.path) + "</option>";
        }).join("");
      } else {
        A.loadEmpty(sel, "未检测到 conda 环境",
          "请先一键安装 Miniforge，或创建一个虚拟环境", function () { loadEnv(true); });
      }
      A.el("condaState").textContent = s.conda_found
        ? ("conda: " + s.conda_path) : "未检测到 conda，请先一键安装 Miniforge";
      A.el("connState").textContent = state.dualMode
        ? (state.connected ? "双臂四路串口已连接" : "双臂模式 · 待连接四路串口")
        : (s.connected ? ("已连接 " + s.leader_port + " / " + s.follower_port) : "未连接");
      updateGate();
      if (!quiet) A.ok("环境信息已刷新");
    } catch (e) {
      A.loadError("#envSelect", e, function () { loadEnv(true); });
      A.el("condaState").textContent = "环境状态读取失败";
      A.el("connState").textContent = "连接状态读取失败";
      A.setAlert("#envAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  async function loadPerm(quiet) {
    var body = A.q("#permTable tbody");
    A.showLoading(body, {
      title: winMode ? "正在扫描 COM 端口…" : "正在检查串口权限…",
      detail: winMode ? "正在枚举 Windows 设备管理器中的真实串口"
        : "正在读取 /dev/ttyUSB* 与 /dev/ttyACM* 的真实权限", rows: 3
    });
    try {
      var p = await A.api.get("/api/system/permissions");
      winMode = p.requires_chmod === false;
      applyWinUI();
      // Windows 不需要（也无法）chmod，权限这一步无条件视为已就绪
      state.permOK = winMode ? true : Boolean(p.all_writable);
      if (p.devices.length) {
        body.innerHTML = p.devices.map(function (d) {
          return "<tr><td><code>" + A.esc(d.device) + "</code></td><td>" + A.esc(d.mode || "-") +
            "</td><td>" + (d.writable
              ? (winMode ? '<span class="badge ok">可用 · 无需授权</span>'
                : '<span class="badge ok">可读写</span>')
              : (winMode ? '<span class="badge err">不可用</span>'
                : '<span class="badge err">无权限</span>')) + "</td></tr>";
        }).join("");
      } else {
        A.loadEmpty(body, winMode ? "未检测到 COM 端口" : "未发现串口设备",
          p.note || "请确认机械臂已接入主机", function () { loadPerm(true); });
      }
      updateGate();
      if (!quiet) A.ok(winMode ? "COM 端口扫描完成" : "串口权限检查完成");
    } catch (e) {
      A.loadError(body, e, function () { loadPerm(true); });
      A.setAlert("#permAlert", e.message, "err");
      if (!quiet) A.toast(e.message, "err");
    }
  }

  async function loadPorts(quiet) {
    var ids = ["leaderPort", "followerPort"];
    ids.forEach(function (id) {
      A.showLoading(id, {
        title: "正在扫描串口…", detail: "正在读取 /api/ports 的真实设备列表"
      });
    });
    try {
      // 记住上次使用的串口：优先用配置里保存的主/从臂端口
      var saved = { leaderPort: "", followerPort: "" };
      try {
        var c = await A.api.get("/api/config");
        saved.leaderPort = (c.leader && c.leader.port) || "";
        saved.followerPort = (c.follower && c.follower.port) || "";
      } catch (e) { /* 配置读不到就退回当前选择 */ }
      var r = await A.api.get("/api/ports");
      ids.forEach(function (id) {
        var sel = A.el(id), prev = sel.value || saved[id];
        if (r.ports.length) {
          A.clearState(sel);
          sel.innerHTML = r.ports.map(function (p) {
            return '<option value="' + A.esc(p.device) + '">' + A.esc(p.device) +
              " — " + A.esc(p.description || "无描述") +
              (p.writable ? "" : (winMode ? "（不可用）" : "（无权限）")) + "</option>";
          }).join("");
          var has = Array.prototype.some.call(sel.options, function (o) { return o.value === prev; });
          if (prev && has) sel.value = prev;
        } else {
          A.loadEmpty(sel, "未检测到串口",
            winMode ? "请确认主从臂已接入并安装 USB 转串口驱动" : "请确认主从臂已接入并完成授权",
            function () { loadPorts(true); });
        }
      });
      if (!quiet) A.ok("串口扫描完成，共 " + r.ports.length + " 个设备");
    } catch (e) {
      ids.forEach(function (id) {
        A.loadError(id, e, function () { loadPorts(true); });
      });
      A.setAlert("#connAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  ["leaderPort", "followerPort"].forEach(function (id) {
    var sel = A.el(id);
    if (!sel) return;
    sel.addEventListener("change", function () {
      var patch = {};
      var lp = A.el("leaderPort") ? A.el("leaderPort").value : "";
      var fp = A.el("followerPort") ? A.el("followerPort").value : "";
      if (lp) patch.leader = { port: lp };
      if (fp) patch.follower = { port: fp };
      if (Object.keys(patch).length) {
        A.api.post("/api/config", patch).catch(function () { /* 记住偏好失败不阻塞 */ });
      }
    });
  });

  A.click("btnSelectEnv", function () {
    A.action(async function () {
      var target = A.el("envSelect").value;
      if (!target) throw new Error("请先选择或创建一个 conda 虚拟环境");
      var r = await A.api.post("/api/env/select", { env: target });
      A.ok("已启用虚拟环境：" + (r.active || target), "#envAlert");
      await loadEnv(true);
    }, "#envAlert");
  });
  A.click("btnRefreshEnv", function () { loadEnv(); });
  A.click("btnCreateEnv", function () {
    A.action(async function () {
      A.clearAlert("#envAlert");
      var loc = A.envLocation() ? A.envLocation().value()
        : { location: "", mode: "default", target: "" };
      await A.api.post("/api/env/create", {
        name: A.el("newEnvName").value.trim(),
        python_version: A.el("newEnvPy").value.trim(),
        location: loc.location,
        location_mode: loc.mode
      });
      A.ok("创建任务已启动" + (loc.target ? "（" + loc.target + "）" : "（conda 默认位置）") +
        "，进度已在本卡片实时显示；完成后环境列表会自动刷新", "#envAlert");
    }, "#envAlert");
  });
  A.click("btnInstallConda", function () {
    A.action(async function () {
      A.clearAlert("#envAlert");
      await A.api.post("/api/setup/install-conda", {});
      A.ok("Miniforge 安装已启动，进度已在本卡片实时显示", "#envAlert");
    }, "#envAlert");
  });
  A.click("btnChmod", function () {
    if (winMode) { loadPerm(); loadPorts(); return; }
    A.action(async function () {
      A.clearAlert("#permAlert");
      var r = await A.api.post("/api/system/chmod", {
        password: A.el("sudoPwd").value, usb: false
      }, 35000);
      A.el("sudoPwd").value = "";
      A.ok("授权成功：" + r.devices.join(", "), "#permAlert");
      await loadPerm(true);
      await loadPorts(true);
    }, "#permAlert");
  });
  A.click("btnCheckPerm", function () { loadPerm(); });
  A.click("btnScanPorts", function () { loadPorts(); });
  A.click("btnConnect", function () {
    if (state.dualMode) { location.href = "/p/connect"; return; }
    A.action(async function () {
      A.clearAlert("#connAlert");
      var r = await A.api.post("/api/connect", {
        leader_port: A.el("leaderPort").value,
        follower_port: A.el("followerPort").value
      });
      state.connected = true;
      A.ok("连接成功：主臂 " + r.leader_port + "，从臂 " + r.follower_port, "#connAlert");
      A.el("connState").textContent = "已连接 " + r.leader_port + " / " + r.follower_port;
      updateGate();
      A.refreshStatus();
    }, "#connAlert");
  });
  A.click("btnDisconnect", function () {
    A.action(async function () {
      await A.api.post("/api/disconnect", {});
      state.connected = false;
      A.setAlert("#connAlert", "已断开连接", "warn");
      updateGate();
      A.refreshStatus();
    }, "#connAlert");
  });
  A.click("btnEnter", function (e) {
    if (!state.envReady || !state.connected) {
      e.preventDefault();
      A.toast("请先完成环境选择与串口连接", "warn");
    }
  });

  var timer = null;
  A.onLog(function (ev) {
    if (ev.type !== "state") return;
    if (ev.refresh_env || ev.connected !== undefined) {
      clearTimeout(timer);
      timer = setTimeout(function () { loadEnv(true); loadPerm(true); loadPorts(true); }, 600);
    }
  });

  var createToggle = A.el("btnToggleCreateEnv");
  if (createToggle) {
    createToggle.addEventListener("click", function () {
      var box = A.el("createEnvBox");
      if (!box) return;
      var show = box.hidden;
      box.hidden = !show;
      createToggle.textContent = show ? "收起创建环境" : "＋ 创建新环境";
      createToggle.setAttribute("aria-expanded", show ? "true" : "false");
    });
  }

  await loadEnv(true);
  await loadPerm(true);   // 权限检查独立加载，环境接口失败时也能显示自己的错误框
  await loadPorts(true);
})();
