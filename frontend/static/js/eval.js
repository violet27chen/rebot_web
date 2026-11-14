/* 策略评估独立面板 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "eval", title: "快递分拣", subtitle: "定位 → 翻面 / 扫码 → 面单朝上与单号确认 → 分类出料" });
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
        sel.innerHTML = '<option value="">（暂无检查点，请先训练一次）</option>';
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
    try {
      var c = await A.api.get("/api/config");
      A.fillForm(A.el("evalForm"), c.eval || {});
      var repo = A.q('#evalForm [name="repo_id"]');
      if (repo && !repo.value.trim()) repo.value = evalRepoDefault();
      await loadCkptOptions(A.q('#evalForm [name="policy_path"]'),
        c.eval && c.eval.policy_path);
      A.clearState(A.el("evalForm"));
    } catch (e) {
      A.loadError(A.el("evalForm"), e, loadConfig, { attach: true });
      A.setAlert("#evalAlert", e.message, "err");
    }
  }

  A.el("evalForm").addEventListener("submit", function (e) {
    e.preventDefault();
    A.action(async function () {
      await A.autosave.flush();
      A.ok("评估参数已保存", "#evalAlert");
    }, "#evalAlert");
  });
  A.click("btnReloadCkpt", function () {
    A.action(async function () {
      var sel = A.q('#evalForm [name="policy_path"]');
      await loadCkptOptions(sel, sel && sel.value);
      A.ok("检查点列表已刷新", "#evalAlert");
    }, "#evalAlert");
  });
  A.click("btnNewEvalName", function () {
    var el = A.q('#evalForm [name="repo_id"]');
    if (el) el.value = evalRepoDefault();
    if (el) el.dispatchEvent(new Event("change", {bubbles:true}));
    A.ok("已生成新的评估数据集名：" + (el ? el.value : ""), "#evalAlert");
  });
  A.click("btnEval", function () {
    A.action(async function () {
      A.clearAlert("#evalAlert");
      var form = A.el("evalForm");
      if (!A.q('[name="policy_path"]', form).value) {
        throw new Error("请先选择策略检查点：还没有可用检查点，请先训练一次");
      }
      var repoInput = A.q('[name="repo_id"]', form);
      var exists = false;
      try {
        var r = await A.api.get("/api/datasets");
        exists = (r.datasets || []).some(function (d) {
          return d.repo_id === repoInput.value.trim();
        });
      } catch (e) { /* ignore */ }
      if (!repoInput.value.trim() || exists) {
        repoInput.value = evalRepoDefault();
        A.toast("评估数据集自动命名为 " + repoInput.value, "info");
      }
      await A.saveConfig({ eval: A.formData(form) });
      await A.api.post("/api/eval", {});
      A.ok("评估任务已启动", "#evalAlert");
      A.refreshStatus();
    }, "#evalAlert");
  });
  A.click("btnEvalStop", function () {
    A.action(async function () {
      await A.stop();
      A.ok("已请求停止；归零任务会从当前位置慢速收工", "#evalAlert");
    }, "#evalAlert");
  });
  A.click('btnEvalHomeDisable',function(){A.action(async function(){
    var host=window.parent!==window?window.parent:window;
    if(!host.JOB_CONSOLE||!host.JOB_CONSOLE.finishEvaluation)throw new Error('请刷新控制台加载一键收工功能');
    var result=await host.JOB_CONSOLE.finishEvaluation();
    if(result)A.ok('已启动慢速收工归零并失能，进度见全局任务日志','#evalAlert');
  },'#evalAlert');});

  await loadConfig();
  A.autosave.form('evalForm','eval');
  A.autosave.onConfig(function(){A.commandPreview('eval','#workflowCommand');});
  A.commandPreview('eval','#workflowCommand');
})();
