/* 遥操作 / 评估 / 异步推理 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "teleop", title: "遥操作", subtitle: "遥操作启停与异步推理" });
  A.attachLogPanel(A.el("logPanel"));

  function ckptLabel(p) {
    return String(p || "").replace(/^outputs\/train\//, "").replace(/\/pretrained_model$/, "");
  }

  async function loadCkptOptions(sel, preferred) {
    if (!sel) return;
    try {
      var r = await A.api.get("/api/checkpoints");
      var list = r.checkpoints || [];
      if (!list.length) {
        sel.innerHTML = '<option value="">（暂无检查点，请先训练）</option>';
        return;
      }
      sel.innerHTML = list.map(function (c) {
        return '<option value="' + A.esc(c.path) + '">' + A.esc(ckptLabel(c.path)) + "</option>";
      }).join("");
      var want = String(preferred || "").trim();
      var hit = list.filter(function (c) { return c.path === want; })[0];
      sel.value = hit ? want : list[0].path;
    } catch (e) {
      sel.innerHTML = '<option value="">检查点读取失败</option>';
    }
  }

  function evalRepoDefault() {
    var d = new Date();
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return "seeed/eval_" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
      "_" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  async function loadConfig() {
    var f = A.el("evalForm");
    if (f) {
      A.showLoading(f, {
        title: "正在读取遥操作参数…", detail: "正在读取 config.json 的真实配置", attach: true
      });
    }
    A.el("cmdPreview").textContent = "正在生成命令预览…";
    try {
      var c = await A.api.get("/api/config");
      A.fillForm(A.el("asyncForm"), c.async_inference);
      await loadCkptOptions(A.q('#asyncForm [name="model_path"]'), c.async_inference && c.async_inference.model_path);

      await A.commandPreview("teleoperate", "#cmdPreview");
      if (f) A.clearState(f);
    } catch (e) {
      A.loadError(f, e, loadConfig, { attach: true });
      A.el("cmdPreview").textContent = "命令预览读取失败";
      A.setAlert("#telAlert", e.message, "err");
    }
  }

  A.click("btnTeleop", function () {
    A.action(async function () {
      A.clearAlert("#telAlert");
      await A.api.post("/api/teleop/start", {});
      A.ok("遥操作已启动，请按急停安全退出循环", "#telAlert");
      A.refreshStatus();
    }, "#telAlert");
  });
  A.click("btnStop", function () {
    A.action(async function () {
      A.el("btnStop").disabled = true;
      try {
        await A.api.post("/api/stop", { force: false }, 60000);
        A.ok("已请求停止。任务结束后，可点击顶部“收工归零并失能”确认收工状态", "#telAlert");
      } finally {
        A.el("btnStop").disabled = false;
      }
      A.refreshStatus();
    }, "#telAlert");
  });
  A.el("asyncForm").addEventListener("submit", function (e) {
    e.preventDefault();
    A.action(async function () {
      await A.autosave.flush();
      A.ok("异步推理参数已保存", "#telAlert");
    }, "#telAlert");
  });
  ["btnServer:/api/policy-server", "btnClient:/api/robot-client"].forEach(function (p) {
    var parts = p.split(":");
    A.click(parts[0], function () {
      A.action(async function () {
        A.clearAlert("#telAlert");
        if (parts[1] === "/api/policy-server" &&
            !A.q('#asyncForm [name="model_path"]').value) {
          throw new Error("请先选择模型检查点：还没有可用的检查点，请先训练一次");
        }
        await A.autosave.flush();
        await A.api.post(parts[1], {});
        A.ok("命令已下发，输出已在本卡片实时显示", "#telAlert");
        A.refreshStatus();
      }, "#telAlert");
    });
  });

  await loadConfig();
  A.autosave.form('asyncForm','async_inference');
  A.autosave.onConfig(function(){A.commandPreview('teleoperate','#cmdPreview');});
})();
