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

  /* ---------------- 快递单号自动识别与持久化保存（页面仅显示数量，单号写入文件供下载） ---------------- */
  var waybillBusy = false;
  var lastAnnouncedCount = -1;

  function populateWaybillCameras(cameras) {
    var sel = A.el("waybillCamSelect");
    if (!sel) return;
    var cur = sel.value || "all";
    var opts = ['<option value="all">全部已配置相机（自动轮询）</option>'];
    (cameras || []).forEach(function (c) {
      var name = String(c.name || c.index_or_path || "");
      var src = String(c.index_or_path !== undefined ? c.index_or_path : "");
      opts.push('<option value="' + A.esc(name || src) + '">' +
        A.esc(name + (src ? " (" + src + ")" : "")) + "</option>");
    });
    sel.innerHTML = opts.join("");
    if (Array.from(sel.options).some(function (o) { return o.value === cur; })) {
      sel.value = cur;
    } else {
      sel.value = "all";
    }
  }

  function renderWaybillState(data, isManual) {
    if (!data) return;
    var banner = A.el("waybillBanner");
    var labelEl = A.el("waybillBannerLabel");
    var codeEl = A.el("waybillBannerCode");
    var metaEl = A.el("waybillBannerMeta");
    var countEl = A.el("waybillCountNum");
    var stateEl = A.el("waybillScanState");

    var totalCount = Number(data.count || 0);
    var latest = data.latest;
    var foundNow = Boolean(data.found);

    if (countEl) {
      countEl.textContent = totalCount + " 个";
    }

    if (foundNow || (latest && totalCount > 0)) {
      if (banner) {
        banner.style.borderColor = "#16a34a";
        banner.style.background = "rgba(22,163,74,0.10)";
      }
      if (labelEl) {
        labelEl.textContent = foundNow ? "✅ 画面当前已识别到快递单号" : "✅ 最近已识别并保存快递单号";
        labelEl.style.color = "#15803d";
        labelEl.style.fontWeight = "600";
      }
      if (codeEl) {
        codeEl.textContent = "已识别到快递单号（累计已扫 " + totalCount + " 个）";
        codeEl.style.color = "#14532d";
      }
      if (metaEl && latest) {
        metaEl.innerHTML =
          '<span class="badge ok">' + A.esc(latest.type || "Barcode") + "</span> · " +
          "<span>识别相机：" + A.esc(latest.camera || "-") + "</span> · " +
          "<span>最近识别时间：" + A.esc(latest.time || "-") + "</span> · " +
          "<span>具体单号已自动保存，可点击下方按钮下载文件查看</span>";
      }
    } else {
      if (banner) {
        banner.style.borderColor = "var(--border,#cbd5e1)";
        banner.style.background = "var(--bg-subtle,rgba(148,163,184,0.08))";
      }
      if (labelEl) {
        labelEl.textContent = "当前识别状态";
        labelEl.style.color = "var(--muted,#64748b)";
        labelEl.style.fontWeight = "400";
      }
      if (codeEl) {
        codeEl.textContent = "正在自动扫描快递单号…（已扫 0 个）";
        codeEl.style.color = "";
      }
      if (metaEl) {
        metaEl.textContent = "将快递面单朝向任意已配置摄像头即可自动识别并保存到单号文件";
      }
    }

    if (stateEl && data.scanned_cameras && data.scanned_cameras.length) {
      var camSummary = data.scanned_cameras.map(function (sc) {
        return sc.name + (sc.ok ? ("(清晰度 " + sc.sharpness + ")") : "(未就绪)");
      }).join(" / ");
      stateEl.textContent = "自动扫码中：" + camSummary + " · 已自动保存 " + totalCount + " 个单号";
    }

    var newCodes = data.new_codes || [];
    if (newCodes.length && totalCount !== lastAnnouncedCount) {
      lastAnnouncedCount = totalCount;
      A.toast("已识别并保存新的快递单号（累计已扫 " + totalCount + " 个）", "ok");
    } else if (isManual) {
      if (foundNow) {
        A.toast("已识别到快递单号（累计已扫 " + totalCount + " 个）", "ok");
      } else if (data.error) {
        A.toast("扫码未成功：" + data.error, "warn");
      } else {
        A.toast("当前画面未检测到面单条码/二维码", "info");
      }
    }
  }

  async function scanWaybillOnce(isManual) {
    if (waybillBusy) return;
    waybillBusy = true;
    var btn = A.el("btnScanWaybill");
    if (isManual && btn) {
      btn.disabled = true;
      btn.textContent = "正在扫描…";
    }
    try {
      var camSel = A.el("waybillCamSelect");
      var cam = (camSel && camSel.value) || "all";
      var r = await A.api.post("/api/waybill/scan", { camera: cam, save: true });
      renderWaybillState(r, isManual);
    } catch (e) {
      if (isManual) A.toast("扫描失败：" + e.message, "err");
    } finally {
      waybillBusy = false;
      if (isManual && btn) {
        btn.disabled = false;
        btn.textContent = "立即扫一次";
      }
    }
  }

  function downloadWaybillFile(fmt) {
    var a = document.createElement("a");
    a.href = "/api/waybill/download?format=" + encodeURIComponent(fmt || "csv");
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    A.ok("正在下载已扫快递单号文件（." + (fmt || "csv") + "）", "#evalAlert");
  }

  async function loadWaybillHistory() {
    try {
      var r = await A.api.get("/api/waybill/history");
      lastAnnouncedCount = Number(r.count || 0);
      renderWaybillState(r, false);
    } catch (e) { /* ignore */ }
  }

  if (A.el("btnDownloadWaybillCsv")) {
    A.click("btnDownloadWaybillCsv", function () { downloadWaybillFile("csv"); });
  }
  if (A.el("btnDownloadWaybillTxt")) {
    A.click("btnDownloadWaybillTxt", function () { downloadWaybillFile("txt"); });
  }
  if (A.el("btnScanWaybill")) {
    A.click("btnScanWaybill", function () { scanWaybillOnce(true); });
  }
  if (A.el("btnClearWaybill")) {
    A.click("btnClearWaybill", function () {
      A.action(async function () {
        var r = await A.api.post("/api/waybill/clear", {});
        lastAnnouncedCount = 0;
        renderWaybillState(r, false);
        A.ok("已清空保存的快递单号记录", "#evalAlert");
      }, "#evalAlert");
    });
  }

  var waybillTimer = setInterval(function () {
    if (document.hidden) return;
    var autoEval = A.el("chkAutoScanWaybill") ? A.el("chkAutoScanWaybill").checked : true;
    var autoIdle = A.el("chkIdleScanWaybill") ? A.el("chkIdleScanWaybill").checked : true;
    var st = A.status();
    var isEvalRunning = Boolean(st && st.running && st.job && (st.job.kind === "eval" || st.job.kind === "record"));
    if ((isEvalRunning && autoEval) || autoIdle) {
      scanWaybillOnce(false);
    }
  }, 1200);
  window.addEventListener("pagehide", function () { clearInterval(waybillTimer); });

  function syncEvalModeUI() {
    var chk = A.el("chkContinuousSort");
    var contRow = A.el("continuousSortRow");
    var fixedRow = A.el("fixedEpisodeRow");
    var btn = A.el("btnEval");
    var isCont = chk ? chk.checked : false;
    if (contRow) contRow.style.opacity = isCont ? "1" : "0.5";
    if (fixedRow) fixedRow.style.opacity = isCont ? "0.55" : "1";
    if (btn) btn.textContent = isCont ? "开始持续分拣" : "开始评估";
  }

  if (A.el("chkContinuousSort")) {
    A.el("chkContinuousSort").addEventListener("change", syncEvalModeUI);
  }

  async function loadConfig() {
    try {
      var c = await A.api.get("/api/config");
      populateWaybillCameras(c.cameras || []);
      var evCfg = Object.assign({ continuous_sort: true, idle_stop_s: 8, max_sort_time_s: 600 }, c.eval || {});
      A.fillForm(A.el("evalForm"), evCfg);
      syncEvalModeUI();
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
  await loadWaybillHistory();
  var evalFormBinding = A.autosave.form('evalForm', 'eval', {
    apply: function (cfg) {
      A.fillForm(A.el('evalForm'), (cfg && cfg.eval) || cfg || {});
      syncEvalModeUI();
    }
  });
  if (evalFormBinding && evalFormBinding.accept) evalFormBinding.accept();
  try {
    await A.autosave.preferences(['waybillCamSelect', 'chkAutoScanWaybill', 'chkIdleScanWaybill', 'logAuto'], 'eval');
  } catch (e) { /* ignore */ }
  A.autosave.onConfig(function(){A.commandPreview('eval','#workflowCommand');});
  A.commandPreview('eval','#workflowCommand');
  scanWaybillOnce(false);
})();
