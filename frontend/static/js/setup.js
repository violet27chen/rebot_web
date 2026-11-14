/* 环境安装：conda / LeRobot（清华源 + gh-proxy）/ 文档命令中心 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "setup", title: "环境安装", subtitle: "一键安装与文档命令中心", gate: false });
  A.attachLogPanel(A.el("logPanel"));
  A.mountEnvLocation("envLocationBox", { nameInput: "setupEnvName" });
  var lerobotCard = A.mountLerobotCard("lerobotCard", {
    alert: "#setupAlert",
    onUse: function () { A.refreshStatus(); }
  });

  /* 文档命令中心里的 conda create 也跟随「创建位置」实时显示，避免两处命令不一致 */
  function createCmdText() {
    var py = (A.el("setupEnvPy") && A.el("setupEnvPy").value.trim()) || "3.12";
    var name = (A.el("setupEnvName") && A.el("setupEnvName").value.trim()) || "lerobot";
    var loc;
    try {
      loc = A.envLocation() ? A.envLocation().value()
        : { location: "", mode: "default", target: "" };
    } catch (e) {
      return "请先在上方选择或填写「创建位置」，再执行此命令";
    }
    return loc.mode === "path"
      ? "conda create -y -p " + loc.target + " python=" + py
      : "conda create -y -n " + name + " python=" + py;
  }
  function updateCreateCmd() {
    var n = A.el("cmdCreatePreview");
    if (n) n.textContent = createCmdText();
  }
  ["setupEnvName", "setupEnvPy", "envLocationInput"].forEach(function (id) {
    var node = A.el(id);
    if (!node) return;
    node.addEventListener("input", updateCreateCmd);
    node.addEventListener("change", updateCreateCmd);
  });

  async function loadConfig() {
    var f = A.el("setupForm");
    A.showLoading(f, {
      title: "正在读取安装源配置…", detail: "正在读取 config.json 的真实配置", attach: true
    });
    try {
      var c = await A.api.get("/api/config");
      A.fillForm(f, c.setup);
      A.clearState(f);
    } catch (e) {
      A.loadError(f, e, loadConfig, { attach: true });
      A.setAlert("#setupAlert", e.message, "err");
    }
  }

  async function loadCommands() {
    var body = A.q("#cmdTable tbody");
    A.showLoading(body, {
      title: "正在读取文档命令…", detail: "正在加载 Seeed 文档对应的真实命令", rows: 3
    });
    try {
      var r = await A.api.get("/api/install-commands");
      if (r.is_windows) {
        var sub = A.el("cmdSub");
        if (sub) sub.textContent = "Windows（cmd）：Linux 专属命令已隐藏，点击即在服务端真实执行";
        var sudoRow = A.el("sudoRow");
        if (sudoRow) sudoRow.hidden = true;
      }
      if (!r.commands.length) {
        A.loadEmpty(body, "暂无可执行命令", "请确认后端命令清单已正确加载",
          function () { loadCommands(); });
        return;
      }
      body.innerHTML = r.commands.map(function (c, i) {
        var isCreate = c.cmd.indexOf("conda create") === 0;
        return "<tr><td><span class=\"badge\">" + A.esc(c.group) + "</span></td>" +
          "<td><div>" + A.esc(c.label) +
            (isCreate ? ' <span class="hint">（随上方「创建位置」变化）</span>' : "") +
            "</div><code" + (isCreate ? ' id="cmdCreatePreview"' : "") + ">" +
            A.esc(c.cmd) + "</code></td>" +
          '<td><button class="btn sm" data-run="' + i + '">执行</button></td></tr>';
      }).join("");
      updateCreateCmd();
      A.qa("#cmdTable [data-run]").forEach(function (b) {
        b.onclick = function () {
          var c = r.commands[Number(b.dataset.run)];
          A.action(async function () {
            A.clearAlert("#setupAlert");
            // 文档里的 conda create 也走同一个创建接口，遵守用户选择的创建位置
            if (c.cmd.indexOf("conda create") === 0) {
              var loc = A.envLocation() ? A.envLocation().value()
                : { location: "", mode: "default", target: "" };
              await A.api.post("/api/env/create", {
                name: A.el("setupEnvName").value.trim(),
                python_version: A.el("setupEnvPy").value.trim(),
                location: loc.location,
                location_mode: loc.mode
              });
              A.ok("创建环境任务已启动" +
                (loc.target ? "（" + loc.target + "）" : "（conda 默认位置）"), "#setupAlert");
              A.refreshStatus();
              return;
            }
            if (c.cmd.indexOf("sudo") >= 0 &&
                !(await A.confirm("该命令需要 sudo 权限。将使用上方输入的密码执行，确认继续？",
                  { title: "sudo 命令确认" }))) return;
            await A.api.post("/api/command/run", {
              cmd: c.cmd, password: A.el("sudoPwd").value
            });
            A.el("sudoPwd").value = "";
            A.ok("已下发：「" + c.label + "」，真实输出已在本卡片实时显示", "#setupAlert");
            A.refreshStatus();
          }, "#setupAlert");
        };
      });
    } catch (e) {
      A.loadError(body, e, loadCommands);
      A.setAlert("#setupAlert", e.message, "err");
    }
  }

  A.el("setupForm").addEventListener("submit", function (e) {
    e.preventDefault();
    A.action(async function () {
      await A.autosave.flush();
      A.ok("安装源已保存", "#setupAlert");
    }, "#setupAlert");
  });
  A.click("btnInstallConda", function () {
    A.action(async function () {
      A.clearAlert("#setupAlert");
      await A.api.post("/api/setup/install-conda", {});
      A.ok("Miniforge 安装已启动", "#setupAlert");
      A.refreshStatus();
    }, "#setupAlert");
  });
  A.click("btnCreateEnv", function () {
    A.action(async function () {
      A.clearAlert("#setupAlert");
      var loc = A.envLocation() ? A.envLocation().value()
        : { location: "", mode: "default", target: "" };
      await A.api.post("/api/env/create", {
        name: A.el("setupEnvName").value.trim(),
        python_version: A.el("setupEnvPy").value.trim(),
        location: loc.location,
        location_mode: loc.mode
      });
      A.ok("创建环境任务已启动" + (loc.target ? "（" + loc.target + "）" : "（conda 默认位置）"),
        "#setupAlert");
      A.refreshStatus();
    }, "#setupAlert");
  });
  A.click("btnStop", function () {
    A.action(async function () {
      await A.stop();
      A.ok("已请求停止；归零任务会从当前位置慢速收工", "#setupAlert");
      A.refreshStatus();
    }, "#setupAlert");
  });

  await loadConfig();
  A.autosave.form('setupForm','setup');
  await loadCommands();
  A.autosave.preferences(['setupEnvName', 'setupEnvPy']);
  document.dispatchEvent(new Event('rebot:setup-ready'));
})();
