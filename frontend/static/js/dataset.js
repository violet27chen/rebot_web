/* 训练数据：采集、列表、可视化、回放、删除 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "dataset", title: "训练数据", subtitle: "数据采集与数据集管理" });
  A.attachLogPanel(A.el("logPanel"));

  async function loadConfig() {
    var f = A.el("recordForm");
    A.showLoading(f, {
      title: "正在读取采集参数…", detail: "正在读取 config.json 的真实配置", attach: true
    });
    try {
      var c = await A.api.get("/api/config");
      populateDsWaybillCameras(c.cameras || [], ((c.ui_preferences || {}).dataset || {}).dsWaybillCamSelect);
      A.fillForm(f, c.dataset);
      A.el("vizRepo").value = c.viz.repo_id || c.dataset.repo_id || "";
      A.el("vizEp").value = c.viz.episode_index || 0;
      A.clearState(f);
    } catch (e) {
      A.loadError(f, e, loadConfig, { attach: true });
      A.setAlert("#dsAlert", e.message, "err");
    }
  }

  /* ---------------- 采集页：快递单号自动识别（仅显示状态与数量，采集不保存单号） ---------------- */
  var dsWaybillBusy = false;
  var dsSessionCodes = new Set();
  var dsLatestWaybill = null;

  function populateDsWaybillCameras(cameras, preferred) {
    var sel = A.el("dsWaybillCamSelect");
    if (!sel) return;
    var cur = preferred || sel.value || "all";
    var legacy = (cameras || []).find(function(c){return String(c.name) === cur;});
    if (legacy && !(cameras || []).some(function(c){return String(c.index_or_path)===cur;})) cur = String(legacy.index_or_path);
    var opts = ['<option value="all">全部已配置相机（自动轮询）</option>'];
    (cameras || []).forEach(function (c) {
      var name = String(c.name || c.index_or_path || "");
      var src = String(c.index_or_path !== undefined ? c.index_or_path : "");
      opts.push('<option value="' + A.esc(src) + '">' +
        A.esc(name + (src ? " (" + src + ")" : "")) + "</option>");
    });
    sel.innerHTML = opts.join("");
    if (Array.from(sel.options).some(function (o) { return o.value === cur; })) {
      sel.value = cur;
    } else {
      var stale = document.createElement('option');stale.value=cur;stale.textContent='已选相机已移除：'+cur;sel.appendChild(stale);sel.value=cur;
    }
  }

  function updateModalWaybillUI() {
    var mask = recModal.mask;
    if (!mask) return;
    var box = mask.querySelector("#recModalWaybillBox");
    var txt = mask.querySelector("#recModalWaybillText");
    var meta = mask.querySelector("#recModalWaybillMeta");
    if (!box || !txt) return;
    if (dsLatestWaybill) {
      box.style.borderColor = "#16a34a";
      box.style.background = "rgba(22,163,74,0.10)";
      txt.textContent = "✅ 已识别到快递单号（本轮已扫 " + dsSessionCodes.size + " 个）";
      txt.style.color = "#15803d";
      if (meta) {
        meta.textContent = (dsLatestWaybill.type || "Barcode") + " · 相机：" +
          (dsLatestWaybill.camera || "-") + " · " + (dsLatestWaybill.time || "") + "（采集模式不保存单号）";
      }
    } else {
      box.style.borderColor = "var(--border,#e2e8f0)";
      box.style.background = "var(--bg-subtle,rgba(148,163,184,0.06))";
      txt.textContent = "📦 快递单号：正在自动扫描面单…（本轮已扫 " + dsSessionCodes.size + " 个）";
      txt.style.color = "";
      if (meta) {
        meta.textContent = "将快递面单朝向摄像头即可自动识别（采集不保存单号）";
      }
    }
  }

  function renderDsWaybillState(data, isManual) {
    if (!data) return;
    if(data.processing){A.el('dsWaybillScanState').textContent=data.message||'扫码识别正在进行，采集继续运行';return;}
    if(data.error)A.el('dsWaybillScanState').textContent='扫码暂不可用：'+data.error;
    var codes = data.codes || [];
    var addedNew = false;
    codes.forEach(function (c) {
      if (c && c.text && !dsSessionCodes.has(c.text)) {
        dsSessionCodes.add(c.text);
        addedNew = true;
      }
    });
    if (data.latest) {
      dsLatestWaybill = data.latest;
    }

    var banner = A.el("dsWaybillBanner");
    var labelEl = A.el("dsWaybillBannerLabel");
    var codeEl = A.el("dsWaybillBannerCode");
    var metaEl = A.el("dsWaybillBannerMeta");
    var countEl = A.el("dsWaybillCountNum");
    var stateEl = A.el("dsWaybillScanState");
    var count = dsSessionCodes.size;

    if (countEl) {
      countEl.textContent = count + " 个";
    }

    if (data.found || dsLatestWaybill) {
      if (banner) {
        banner.style.borderColor = "#16a34a";
        banner.style.background = "rgba(22,163,74,0.10)";
      }
      if (labelEl) {
        labelEl.textContent = data.found ? "✅ 画面当前已识别到快递单号" : "✅ 最近已识别到快递单号";
        labelEl.style.color = "#15803d";
        labelEl.style.fontWeight = "600";
      }
      if (codeEl) {
        codeEl.textContent = "已识别到快递单号（本轮已扫 " + count + " 个）";
        codeEl.style.color = "#14532d";
      }
      if (metaEl && dsLatestWaybill) {
        metaEl.innerHTML =
          '<span class="badge ok">' + A.esc(dsLatestWaybill.type || "Barcode") + "</span> · " +
          "<span>相机：" + A.esc(dsLatestWaybill.camera || "-") + "</span> · " +
          "<span>时间：" + A.esc(dsLatestWaybill.time || "-") + "</span> · " +
          "<span>采集模式仅实时提示，不保存单号文件</span>";
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
        codeEl.textContent = "正在自动扫描快递单号…（本轮已扫 " + count + " 个）";
        codeEl.style.color = "";
      }
      if (metaEl) {
        metaEl.textContent = "采集时将快递面单朝向摄像头即可自动识别（采集不保存单号）";
      }
    }

    if (stateEl && data.scanned_cameras && data.scanned_cameras.length) {
      var camSummary = data.scanned_cameras.map(function (sc) {
        return sc.name + (sc.ok ? ("(清晰度 " + sc.sharpness + ")") : "(未就绪)");
      }).join(" / ");
      stateEl.textContent = "自动扫码中：" + camSummary + " · 本轮已扫 " + count + " 个（采集不落盘）";
    }

    var dsPreviewGrid = A.el("dsWaybillPreviewGrid");
    if (dsPreviewGrid && data.scanned_cameras && data.scanned_cameras.length) {
      A.renderWaybillPreview(dsPreviewGrid, data.scanned_cameras, {
        compact: true,
        foundHint: "已识别快递单号（采集模式不保存单号）"
      });
    }

    updateModalWaybillUI();

    if (addedNew) {
      A.toast("已识别到快递单号（本轮第 " + count + " 个）", "ok");
    } else if (isManual) {
      if (data.found) {
        A.toast("已识别到快递单号（本轮已扫 " + count + " 个）", "ok");
      } else if (data.error) {
        A.toast("扫码未成功：" + data.error, "warn");
      } else {
        A.toast("当前画面未检测到面单条码/二维码", "info");
      }
    }
  }

  async function scanDsWaybillOnce(isManual) {
    if (dsWaybillBusy) return;
    dsWaybillBusy = true;
    var btn = A.el("btnDsScanWaybill");
    var mBtn = recModal.mask && recModal.mask.querySelector("#recModalScanBtn");
    if (isManual) {
      if (btn) { btn.disabled = true; btn.textContent = "正在扫描…"; }
      if (mBtn) { mBtn.disabled = true; mBtn.textContent = "扫描中…"; }
    }
    try {
      var camSel = A.el("dsWaybillCamSelect");
      var cam = (camSel && camSel.value) || "all";
      /* 采集页面传 save: false，只识别不保存快递单号 */
      var r = await A.api.post("/api/waybill/scan", { camera: cam, save: false, include_preview: false });
      renderDsWaybillState(r, isManual);
    } catch (e) {
      A.el('dsWaybillScanState').textContent='扫码失败：'+e.message;
      if (isManual) A.toast("扫描失败：" + e.message, "err");
    } finally {
      dsWaybillBusy = false;
      if (isManual) {
        if (btn) { btn.disabled = false; btn.textContent = "立即扫一次"; }
        if (mBtn) { mBtn.disabled = false; mBtn.textContent = "立即扫码"; }
      }
    }
  }

  if (A.el("btnDsScanWaybill")) {
    A.click("btnDsScanWaybill", function () { scanDsWaybillOnce(true); });
  }
  if (A.el("btnDsClearWaybill")) {
    A.click("btnDsClearWaybill", function () {
      dsSessionCodes.clear();
      dsLatestWaybill = null;
      renderDsWaybillState({ found: false, codes: [] }, false);
      A.ok("已重置本轮扫码计数", "#dsAlert");
    });
  }

  var dsWaybillTimer = setInterval(function () {
    if (document.hidden) return;
    var autoRec = A.el("chkDsAutoScanWaybill") ? A.el("chkDsAutoScanWaybill").checked : true;
    var autoIdle = A.el("chkDsIdleScanWaybill") ? A.el("chkDsIdleScanWaybill").checked : true;
    var st = A.status();
    var isRunning = Boolean(recModal.active || (st && st.running && st.job && st.job.kind === "record"));
    if (recModal.active && ["preparing_next", "saving", "reset", "closing"].includes(recModal.phase)) {
      A.el("dsWaybillScanState").textContent = "剧集切换 / 保存中，自动扫码暂缓；不等待扫到单号";
      return;
    }
    if (isRunning ? autoRec : autoIdle) {
      scanDsWaybillOnce(false);
    }
  }, 1200);

  var cachedDatasets = [];
  var selectedRepoIds = new Set();

  function updateBatchBar() {
    var count = selectedRepoIds.size;
    var total = cachedDatasets.length;
    var lbl = A.el("dsSelectedCount");
    if (lbl) {
      lbl.textContent = count > 0 ? ("已选择 " + count + " 项" + (total > 0 ? "（共 " + total + " 项）" : "")) : "已选择 0 项";
    }
    var btnMerge = A.el("btnBatchMerge");
    if (btnMerge) btnMerge.disabled = (count < 2);
    var btnExpAll = A.el("btnBatchExportAll");
    if (btnExpAll) btnExpAll.disabled = (count === 0);
    var btnExpVid = A.el("btnBatchExportVid");
    if (btnExpVid) btnExpVid.disabled = (count === 0);
    var btnDel = A.el("btnBatchDelete");
    if (btnDel) btnDel.disabled = (count === 0);
    var btnClr = A.el("btnBatchDeselect");
    if (btnClr) btnClr.style.display = count > 0 ? "" : "none";

    var allChk = A.el("dsSelectAll");
    if (allChk) {
      allChk.checked = (total > 0 && count === total);
      allChk.indeterminate = (count > 0 && count < total);
    }
  }

  function syncSelectionUI() {
    A.qa("#dsTable tbody tr").forEach(function (tr) {
      var chk = tr.querySelector(".ds-row-chk");
      if (chk) {
        var isChecked = selectedRepoIds.has(chk.value);
        chk.checked = isChecked;
        tr.classList.toggle("is-selected", isChecked);
      }
    });
    updateBatchBar();
  }

  function triggerDatasetExport(repoId, mode) {
    var rid = String(repoId || "").trim();
    if (!rid) {
      var f = A.el("recordForm");
      rid = (f && f.elements.repo_id && f.elements.repo_id.value.trim()) ||
        (cachedDatasets[0] && cachedDatasets[0].repo_id) || "";
    }
    if (!rid) {
      A.toast("请先指定或选择一个已录制数据集", "warn");
      return;
    }
    var url = "/api/dataset/export?repo_id=" + encodeURIComponent(rid) + "&mode=" + encodeURIComponent(mode || "all");
    var a = document.createElement("a");
    a.href = url;
    a.download = rid.replace(/[^A-Za-z0-9._-]+/g, "_") + (mode === "videos" ? "_videos.zip" : "_full.zip");
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    A.ok("正在打包下载数据集 " + rid + "（" + (mode === "videos" ? "仅视频 MP4" : "完整数据+视频") + "）", "#dsAlert");
  }

  function triggerBatchExport(mode) {
    var ids = Array.from(selectedRepoIds);
    if (!ids.length) {
      A.toast("请先勾选要导出的数据集", "warn");
      return;
    }
    var url = "/api/dataset/export?repo_ids=" + encodeURIComponent(ids.join(",")) + "&mode=" + encodeURIComponent(mode || "all");
    var a = document.createElement("a");
    a.href = url;
    a.download = "batch_" + ids.length + "_datasets_" + (mode === "videos" ? "videos.zip" : "full.zip");
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    A.ok("正在打包下载选中的 " + ids.length + " 个数据集（" + (mode === "videos" ? "仅视频 MP4" : "完整数据+视频") + "）", "#dsAlert");
  }

  if (A.el("btnExportCurrentAll")) {
    A.click("btnExportCurrentAll", function () { triggerDatasetExport("", "all"); });
  }
  if (A.el("btnExportCurrentVid")) {
    A.click("btnExportCurrentVid", function () { triggerDatasetExport("", "videos"); });
  }

  if (A.el("btnBatchExportAll")) {
    A.click("btnBatchExportAll", function () { triggerBatchExport("all"); });
  }
  if (A.el("btnBatchExportVid")) {
    A.click("btnBatchExportVid", function () { triggerBatchExport("videos"); });
  }

  if (A.el("btnBatchDeselect")) {
    A.click("btnBatchDeselect", function () {
      selectedRepoIds.clear();
      syncSelectionUI();
    });
  }

  if (A.el("btnBatchDelete")) {
    A.click("btnBatchDelete", async function () {
      var ids = Array.from(selectedRepoIds);
      if (!ids.length) {
        A.toast("请先勾选要删除的数据集", "warn");
        return;
      }
      var confirmMsg = "确认永久删除选中的 " + ids.length + " 个数据集？此操作不可撤销。\n\n" +
        ids.slice(0, 8).join("\n") + (ids.length > 8 ? "\n...等共 " + ids.length + " 个" : "");
      if (!(await A.confirm(confirmMsg, { title: "批量删除数据集" }))) return;
      A.action(async function () {
        var r = await A.api.post("/api/datasets/batch-delete", { repo_ids: ids, confirm: true });
        var deletedCount = (r.deleted || []).length;
        var failedList = r.failed || [];
        if (failedList.length > 0) {
          A.toast("已删除 " + deletedCount + " 个，失败 " + failedList.length + " 个", "warn");
        } else {
          A.ok("成功批量删除 " + deletedCount + " 个数据集", "#dsAlert");
        }
        selectedRepoIds.clear();
        await loadDatasets(true);
      }, "#dsAlert");
    });
  }

  var dsSelectAll = A.el("dsSelectAll");
  if (dsSelectAll) {
    dsSelectAll.addEventListener("change", function () {
      if (dsSelectAll.checked) {
        cachedDatasets.forEach(function (d) { if (d.repo_id) selectedRepoIds.add(d.repo_id); });
      } else {
        selectedRepoIds.clear();
      }
      syncSelectionUI();
    });
  }

  async function loadDatasets(quiet) {
    var body = A.q("#dsTable tbody");
    A.showLoading(body, {
      title: "正在读取数据集列表…",
      detail: "正在扫描真实的数据集目录，请稍候", rows: 3
    });
    A.el("dsRoot").textContent = "目录读取中…";
    try {
      var r = await A.api.get("/api/datasets");
      cachedDatasets = r.datasets || [];
      // 清理已不存在的数据集 ID
      var validIds = new Set(cachedDatasets.map(function (d) { return d.repo_id; }));
      Array.from(selectedRepoIds).forEach(function (id) {
        if (!validIds.has(id)) selectedRepoIds.delete(id);
      });

      A.el("dsRoot").textContent = "目录 " + r.root;
      if (!r.datasets.length) {
        selectedRepoIds.clear();
        updateBatchBar();
        A.loadEmpty(body, "数据集目录为空",
          "请先点「开始采集」录制数据，或刷新列表重试", function () { loadDatasets(true); });
        if (!quiet) A.ok("数据集列表已刷新（暂无数据）");
        return;
      }
      body.innerHTML = r.datasets.map(function (d) {
        var isChecked = selectedRepoIds.has(d.repo_id);
        return "<tr class=\"" + (isChecked ? "is-selected" : "") + "\" data-rid=\"" + A.esc(d.repo_id) + "\">" +
          "<td style=\"text-align:center\"><input type=\"checkbox\" class=\"ds-row-chk\" value=\"" + A.esc(d.repo_id) + "\"" + (isChecked ? " checked" : "") + " aria-label=\"选择 " + A.esc(d.repo_id) + "\"></td>" +
          "<td><code>" + A.esc(d.repo_id) + "</code></td><td>" +
          A.esc(d.episodes) + "</td><td>" + A.esc(d.size_mb) + " MB" +
          (d.storage ? '<div class="hint">视频 ' + A.esc(d.storage.videos_mb) + ' MB · 临时帧 ' + A.esc(d.storage.images_mb) + ' MB</div>' : '') + "</td><td class=\"muted\">" +
          A.esc(d.path) + "</td><td><div class=\"actions\">" +
          '<button class="btn sm primary" data-view="' + A.esc(d.repo_id) + '">网页查看</button>' +
          '<button class="btn sm subtle" data-export-all="' + A.esc(d.repo_id) + '">导出数据+视频</button>' +
          '<button class="btn sm subtle" data-export-vid="' + A.esc(d.repo_id) + '">仅导出视频</button>' +
          '<button class="btn sm subtle" data-viz="' + A.esc(d.repo_id) + '">可视化</button>' +
          '<button class="btn sm" data-replay="' + A.esc(d.repo_id) + '">回放</button>' +
          '<button class="btn sm" data-fill="' + A.esc(d.path) + '">用作训练目录</button>' +
          '<button class="btn sm danger" data-del="' + A.esc(d.repo_id) + '">删除</button>' +
          "</div></td></tr>";
      }).join("");

      A.qa("#dsTable .ds-row-chk").forEach(function (chk) {
        chk.addEventListener("change", function () {
          if (chk.checked) selectedRepoIds.add(chk.value);
          else selectedRepoIds.delete(chk.value);
          var tr = chk.closest("tr");
          if (tr) tr.classList.toggle("is-selected", chk.checked);
          updateBatchBar();
        });
      });

      A.qa("#dsTable tbody tr").forEach(function (tr) {
        tr.addEventListener("click", function (e) {
          if (e.target.closest("button") || e.target.closest("a") || e.target.closest("input")) return;
          var chk = tr.querySelector(".ds-row-chk");
          if (chk) {
            chk.checked = !chk.checked;
            chk.dispatchEvent(new Event("change"));
          }
        });
      });

      A.qa("#dsTable [data-view]").forEach(function (b) {
        b.addEventListener("click", function () {
          location.href = "/p/viewer?repo=" + encodeURIComponent(b.dataset.view);
        });
      });
      A.qa("#dsTable [data-export-all]").forEach(function (b) {
        b.addEventListener("click", function () {
          triggerDatasetExport(b.dataset.exportAll, "all");
        });
      });
      A.qa("#dsTable [data-export-vid]").forEach(function (b) {
        b.addEventListener("click", function () {
          triggerDatasetExport(b.dataset.exportVid, "videos");
        });
      });
      A.qa("#dsTable [data-viz]").forEach(function (b) {
        b.onclick = function () {
          A.el("vizRepo").value = b.dataset.viz;
          A.action(async function () {
            await A.api.post("/api/dataset/viz", {
              repo_id: b.dataset.viz, episode_index: Number(A.el("vizEp").value) || 0
            });
            A.ok("可视化任务已启动", "#dsAlert");
          }, "#dsAlert");
        };
      });
      A.qa("#dsTable [data-replay]").forEach(function (b) {
        b.onclick = function () {
          A.el("vizRepo").value = b.dataset.replay;
          A.action(async function () {
            await A.api.post("/api/dataset/replay", {
              repo_id: b.dataset.replay, episode: Number(A.el("vizEp").value) || 0
            });
            A.ok("回放任务已启动", "#dsAlert");
          }, "#dsAlert");
        };
      });
      A.qa("#dsTable [data-fill]").forEach(function (b) {
        b.onclick = function () {
          A.toast("已记录数据集路径：" + b.dataset.fill + "，请到训练页填入对应参数", "ok");
          try { sessionStorage.setItem("arm.datasetPath", b.dataset.fill); } catch (e) {}
        };
      });
      A.qa("#dsTable [data-del]").forEach(function (b) {
        b.onclick = function () {
          A.action(async function () {
            if (!(await A.confirm("确认永久删除数据集「" + b.dataset.del + "」？此操作不可撤销。", { title: "删除数据集" }))) return;
            await A.api.del("/api/datasets/" + b.dataset.del + "?confirm=true");
            selectedRepoIds.delete(b.dataset.del);
            A.ok("数据集已删除", "#dsAlert");
            await loadDatasets(true);
          }, "#dsAlert");
        };
      });
      updateBatchBar();
      if (!quiet) A.ok("数据集列表已刷新");
    } catch (e) {
      A.loadError(body, e, function () { loadDatasets(true); });
      A.el("dsRoot").textContent = "目录读取失败";
      A.setAlert("#dsAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  /* ---------------- 数据采集全过程倒计时模态框 ---------------- */
  var recModal = {
    mask: null, tickTimer: null, active: false,
    numEpisodes: 1, epTimeS: 30, resetTimeS: 10, repoId: "",
    curEp: 1, firstRawEp: null,
    jobId: "", jobStartedAt: 0, sequence: -1, authoritative: false, rawEp: null, rerecord: false,
    hasStarted: false, startupError: "",
    phase: "init", /* init | recording | reset | preparing_next | saving */
    phaseStartMs: 0, sessionStartMs: 0, lastLog: ""
  };

  function fmtSec(sec) {
    var s = Math.max(0, Math.ceil(sec));
    var m = Math.floor(s / 60), r = s % 60;
    return (m < 10 ? "0" + m : String(m)) + ":" + (r < 10 ? "0" + r : String(r));
  }

  function readRecordParams() {
    var f = A.el("recordForm");
    recModal.repoId = (f && f.elements.repo_id && f.elements.repo_id.value) || "";
    recModal.numEpisodes = Math.max(1, Number((f && f.elements.num_episodes && f.elements.num_episodes.value) || 1));
    recModal.epTimeS = Math.max(1, Number((f && f.elements.episode_time_s && f.elements.episode_time_s.value) || 30));
    var reset = f && f.elements.reset_time_s && f.elements.reset_time_s.value;
    recModal.resetTimeS = Math.max(0, Number(reset === "" || reset == null ? 10 : reset));
  }

  function ensureRecordModal() {
    if (recModal.mask && recModal.mask.parentNode) return;
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.id = "recordCountdownModal";
    mask.innerHTML =
      '<div class="notify-modal" style="width:min(540px,96vw)" role="dialog" aria-modal="true" aria-labelledby="recModalTitle">' +
        '<div class="notify-head" style="display:flex;justify-content:space-between;align-items:center">' +
          '<span class="notify-title" id="recModalTitle">正在采集数据</span>' +
          '<span class="badge" id="recModalBadge">准备中</span>' +
        '</div>' +
        '<div class="notify-msg" style="padding:16px 20px">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">' +
            '<span id="recModalEpInfo" style="font-weight:600;font-size:15px">剧集 1 / 1</span>' +
            '<span id="recModalTotalInfo" class="muted" style="font-size:13px">总剩余约 --:--</span>' +
          '</div>' +
          '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0">' +
            '<div id="recBoxEp" style="border:2px solid var(--primary,#2563eb);border-radius:10px;padding:12px;text-align:center;background:var(--bg-subtle,rgba(37,99,235,0.06))">' +
              '<div style="font-size:12.5px;color:var(--muted,#64748b);margin-bottom:4px">当前集录制剩余时间</div>' +
              '<div id="recEpRemain" style="font-size:32px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15">00:30</div>' +
              '<div id="recEpSub" style="font-size:12px;color:var(--muted,#64748b);margin-top:4px">单集时长 30s</div>' +
            '</div>' +
            '<div id="recBoxReset" style="border:2px solid var(--border,#e2e8f0);border-radius:10px;padding:12px;text-align:center">' +
              '<div style="font-size:12.5px;color:var(--muted,#64748b);margin-bottom:4px">重置环境剩余时间</div>' +
              '<div id="recResetRemain" style="font-size:32px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15">00:10</div>' +
              '<div id="recResetSub" style="font-size:12px;color:var(--muted,#64748b);margin-top:4px">重置时长 10s</div>' +
            '</div>' +
          '</div>' +
          '<div id="recModalWaybillBox" style="border:1.5px solid var(--border,#e2e8f0);border-radius:8px;padding:8px 12px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;background:var(--bg-subtle,rgba(148,163,184,0.06))">' +
            '<div style="min-width:0;flex:1">' +
              '<div id="recModalWaybillText" style="font-weight:700;font-size:14px;word-break:break-all">📦 快递单号：正在扫描面单条码/二维码…</div>' +
              '<div id="recModalWaybillMeta" class="muted" style="font-size:11.5px;margin-top:2px">将快递面单朝向摄像头即可自动识别</div>' +
            '</div>' +
            '<button type="button" class="btn subtle sm" id="recModalScanBtn">立即扫码</button>' +
          '</div>' +
          '<div class="fb-bar" style="height:8px;border-radius:4px;overflow:hidden;margin:10px 0"><i id="recModalBar" style="display:block;height:100%;width:0%;background:var(--primary,#2563eb);transition:width .25s"></i></div>' +
          '<div id="recModalHint" class="note" style="margin-top:8px;font-size:12.5px">正在连接主从臂与摄像头，请稍候…</div>' +
        '</div>' +
        '<div class="notify-foot" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">' +
          '<div class="btn-row" style="gap:6px">' +
            '<button type="button" class="btn subtle sm" data-rec-key="right" title="提前结束当前阶段进入下一阶段">→ 下一集</button>' +
            '<button type="button" class="btn subtle sm" data-rec-key="left" title="作废当前集并重录">← 重录本集</button>' +
          '</div>' +
          '<div class="btn-row" style="gap:8px">' +
            '<button type="button" class="btn danger" id="recModalStop">停止录制</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    A.modalHost().appendChild(mask);
    recModal.mask = mask;
    var scanBtn = mask.querySelector("#recModalScanBtn");
    if (scanBtn) {
      scanBtn.addEventListener("click", function () { scanDsWaybillOnce(true); });
    }
    updateModalWaybillUI();

    Array.from(mask.querySelectorAll("[data-rec-key]")).forEach(function (b) {
      b.addEventListener("click", async function () {
        var k = b.getAttribute("data-rec-key");
        try {
          await A.api.post("/api/record/key", { key: k });
          A.toast("已发送按键 " + k.toUpperCase(), "ok");
        } catch (e) { A.toast(e.message, "err"); }
      });
    });

    mask.querySelector("#recModalStop").addEventListener("click", async function () {
      var btn = mask.querySelector("#recModalStop");
      btn.disabled = true;
      btn.textContent = "正在停止并保存…";
      try {
        var s = A.status();
        var j = (s && s.job) || {};
        if (s && s.running && j.kind === "record") {
          await A.api.post("/api/record/key", { key: "esc" });
          A.ok("已发送停止指令（ESC）：正在保存并编码当前剧集，结束后自动关闭", "#dsAlert");
        } else {
          await A.stop();
          closeRecordModal();
        }
        A.refreshStatus();
      } catch (e) {
        btn.disabled = false;
        btn.textContent = "停止录制";
        A.toast(e.message, "err");
      }
    });
  }

  function updateRecordModalUI() {
    var mask = recModal.mask;
    if (!mask || !recModal.active) return;
    var now = performance.now();
    var elapsedPhase = (now - recModal.phaseStartMs) / 1000;
    // Rendering never advances the recorder. Zero means waiting for its next
    // actual phase event, not starting another countdown speculatively.

    var badge = mask.querySelector("#recModalBadge");
    var epInfo = mask.querySelector("#recModalEpInfo");
    var totalInfo = mask.querySelector("#recModalTotalInfo");
    var epRemain = mask.querySelector("#recEpRemain");
    var epSub = mask.querySelector("#recEpSub");
    var resetRemain = mask.querySelector("#recResetRemain");
    var resetSub = mask.querySelector("#recResetSub");
    var boxEp = mask.querySelector("#recBoxEp");
    var boxReset = mask.querySelector("#recBoxReset");
    var bar = mask.querySelector("#recModalBar");
    var hint = mask.querySelector("#recModalHint");

    var displayEp = recModal.curEp;
    epInfo.textContent = "剧集 " + displayEp + " / " + recModal.numEpisodes +
      (recModal.repoId ? (" · " + recModal.repoId) : "");

    var curEpLeft = recModal.epTimeS;
    var curResetLeft = recModal.curEp >= recModal.numEpisodes ? 0 : recModal.resetTimeS;
    var pct = 0;

    if (recModal.phase === "init") {
      badge.textContent = "准备中 · 连接设备";
      badge.className = "badge warn";
      epRemain.textContent = "准备中…";
      epSub.textContent = "真实开始采集后显示 " + recModal.epTimeS + "s 倒计时";
      resetRemain.textContent = "等待开始";
      resetSub.textContent = "重置时长 " + recModal.resetTimeS + "s";
      boxEp.style.borderColor = "#f59e0b";
      boxEp.style.background = "rgba(245,158,11,0.08)";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      bar.style.width = "4%";
      totalInfo.textContent = "等待真机进入采集后开始计时…";
      hint.textContent = recModal.lastLog || "正在初始化主从臂与摄像头，真实开始采集后自动显示倒计时…";
      return;
    }

    if (recModal.phase === "preparing_next") {
      var nextEp = recModal.curEp;
      badge.textContent = "准备中 · 重录第 " + nextEp + " 集";
      badge.className = "badge warn";
      epRemain.textContent = "准备中…";
      epSub.textContent = "第 " + nextEp + " 集就绪后开始 " + recModal.epTimeS + "s 倒计时";
      resetRemain.textContent = "00:00";
      resetSub.textContent = "正在准备重录当前集";
      boxEp.style.borderColor = "#f59e0b";
      boxEp.style.background = "rgba(245,158,11,0.08)";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      bar.style.width = "100%";
      var remEps = Math.max(1, recModal.numEpisodes - recModal.curEp + 1);
      var remSec = remEps * recModal.epTimeS + Math.max(0, remEps - 1) * recModal.resetTimeS;
      totalInfo.textContent = "正在准备第 " + nextEp + " 集 · 剩余约 " + fmtSec(remSec);
      hint.textContent = "等待采集进程实际开始重录，当前不提前倒计时。";
      return;
    }

    if (recModal.phase === "recording") {
      curEpLeft = Math.max(0, recModal.epTimeS - elapsedPhase);
      curResetLeft = recModal.curEp >= recModal.numEpisodes ? 0 : recModal.resetTimeS;
      badge.textContent = "正在采集第 " + recModal.curEp + " 集";
      badge.className = "badge ok";
      boxEp.style.borderColor = "var(--primary,#2563eb)";
      boxEp.style.background = "var(--bg-subtle,rgba(37,99,235,0.08))";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      pct = Math.min(100, Math.max(0, (elapsedPhase / recModal.epTimeS) * 100));
      epRemain.textContent = fmtSec(curEpLeft);
      epSub.textContent = "单集时长 " + recModal.epTimeS + "s";
      resetRemain.textContent = recModal.curEp >= recModal.numEpisodes ? "无重置" : fmtSec(curResetLeft);
      resetSub.textContent = recModal.curEp >= recModal.numEpisodes ? "最后一集无重置" : ("重置时长 " + recModal.resetTimeS + "s");
      hint.textContent = curEpLeft > 0 ? "请操作主臂完成动作示教；阶段按采集进程的实际输出切换。" : "计划采集时间已到，等待采集进程结束本集；尚未开始重置倒计时。";
    } else if (recModal.phase === "reset") {
      curEpLeft = 0;
      curResetLeft = Math.max(0, recModal.resetTimeS - elapsedPhase);
      badge.textContent = "重置环境倒计时";
      badge.className = "badge warn";
      boxEp.style.borderColor = "var(--border,#e2e8f0)";
      boxEp.style.background = "transparent";
      boxReset.style.borderColor = "#f59e0b";
      boxReset.style.background = "rgba(245,158,11,0.08)";
      pct = recModal.resetTimeS > 0 ? Math.min(100, Math.max(0, (elapsedPhase / recModal.resetTimeS) * 100)) : 100;
      epRemain.textContent = "本集录制结束";
      epSub.textContent = "单集时长 " + recModal.epTimeS + "s";
      resetRemain.textContent = fmtSec(curResetLeft);
      resetSub.textContent = "重置时长 " + recModal.resetTimeS + "s（结束后进入下一集准备）";
      hint.textContent = curResetLeft > 0 ? "请将包裹恢复到初始位置；重置结束后保存当前集，再开始下一集。" : "重置时间已到，等待采集进程进入保存阶段。";
    } else {
      curEpLeft = 0;
      curResetLeft = 0;
      var startupAborted = recModal.phase === "closing" && !recModal.hasStarted;
      badge.textContent = startupAborted ? (recModal.startupError ? "启动失败 · 正在清理设备" : "启动未完成 · 正在清理设备") : recModal.phase === "closing" ? "正在结束采集并收尾" : "正在保存第 " + recModal.curEp + " 集";
      badge.className = "badge";
      boxEp.style.borderColor = "var(--border,#e2e8f0)";
      boxEp.style.background = "transparent";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      pct = startupAborted ? 0 : 100;
      epRemain.textContent = startupAborted ? "未开始采集" : recModal.phase === "closing" ? "采集已停止" : "保存中…";
      epSub.textContent = startupAborted ? "尚未开始任何剧集，未计为采集完成" : recModal.phase === "closing" ? "等待保存和设备收工完成" : "当前集正在写盘、统计与编码";
      resetRemain.textContent = startupAborted ? "未开始" : "00:00";
      resetSub.textContent = startupAborted ? "等待设备清理完成" : "正在保存或收尾";
      hint.textContent = recModal.startupError || recModal.lastLog || "正在编码视频并安全收尾，完全结束后此窗口将自动关闭…";
    }

    bar.style.width = pct.toFixed(1) + "%";

    var futureEps = Math.max(0, recModal.numEpisodes - recModal.curEp);
    var futureSec = futureEps * recModal.epTimeS + Math.max(0, futureEps - 1) * recModal.resetTimeS;
    var totalLeft = recModal.phase === "saving" ? 0 : (curEpLeft + curResetLeft + futureSec);
    if (recModal.phase === "closing") totalInfo.textContent = "正在收尾，等待采集进程结束…";
    else if (recModal.phase === "saving") totalInfo.textContent = futureEps ? "保存当前集 · 尚有 " + futureEps + " 集，录制/重置约 " + fmtSec(futureSec) + "（不含保存耗时）" : "最后一集正在保存…";
    else totalInfo.textContent = "录制/重置预计剩余 " + fmtSec(totalLeft) + "（不含保存耗时）";
  }

  function openRecordModal(job) {
    job = job || {};
    var jobId = String(job.job_id || recModal.jobId || "");
    if (recModal.active && jobId === recModal.jobId) return;
    readRecordParams();
    recModal.jobId = jobId;
    recModal.jobStartedAt = Number(job.started_at || 0);
    recModal.authoritative = false; recModal.sequence = -1; recModal.rawEp = null; recModal.rerecord = false;
    recModal.hasStarted = false; recModal.startupError = "";
    recModal.active = true;
    recModal.curEp = 1;
    recModal.firstRawEp = null;
    recModal.phase = "init";
    recModal.phaseStartMs = performance.now();
    recModal.sessionStartMs = performance.now();
    recModal.lastLog = "";
    ensureRecordModal();
    var btn = recModal.mask.querySelector("#recModalStop");
    if (btn) { btn.disabled = false; btn.textContent = "停止录制"; }
    updateRecordModalUI();
    clearInterval(recModal.tickTimer);
    recModal.tickTimer = setInterval(updateRecordModalUI, 200);
  }

  function closeRecordModal() {
    recModal.active = false;
    clearInterval(recModal.tickTimer);
    recModal.tickTimer = null;
    if (recModal.mask && recModal.mask.parentNode) {
      recModal.mask.parentNode.removeChild(recModal.mask);
    }
    recModal.mask = null;
  }

  function syncRecordJob(job) {
    if (!job || job.kind !== "record" || !job.job_id) return;
    openRecordModal(job);
    var progress = job.recording;
    if (!progress || !Number.isInteger(progress.sequence) || !["init", "recording", "reset", "preparing_next", "saving", "closing"].includes(progress.phase)) return;
    if (progress.sequence < recModal.sequence) return;
    var samePhase = recModal.authoritative && progress.sequence === recModal.sequence;
    var elapsed = Math.max(0, Number(progress.elapsed_s) || 0);
    if (samePhase) elapsed = Math.max(elapsed, (performance.now() - recModal.phaseStartMs) / 1000);
    recModal.phaseStartMs = performance.now() - elapsed * 1000;
    recModal.phase = progress.phase;
    recModal.sequence = progress.sequence;
    recModal.authoritative = true;
    recModal.curEp = progress.episode;
    recModal.numEpisodes = progress.total;
    recModal.epTimeS = progress.episode_time_s;
    recModal.resetTimeS = progress.reset_time_s;
    recModal.repoId = progress.repo_id;
    recModal.hasStarted = progress.has_started !== undefined ? progress.has_started : progress.raw_episode != null || progress.phase === "recording" || recModal.hasStarted;
    recModal.startupError = progress.startup_error || "";
    updateRecordModalUI();
  }

  function onRecordLogLine(line) {
    if (!recModal.active || !line) return;
    recModal.lastLog = line;
    if (recModal.authoritative || /^\[进度\]/.test(line)) return;
    // Compatibility for an older running backend: accept only actual phase
    // lines and deduplicate them. The current backend supplies structured state.
    line = line.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").trim();
    var mEp = line.match(/(?:^|\s)Recording episode\s+(\d+)\s*$/i);
    if (mEp) {
      var rawEp = parseInt(mEp[1], 10);
      if (!isNaN(rawEp)) {
        if (recModal.rawEp !== null && (rawEp < recModal.rawEp || (rawEp === recModal.rawEp && !recModal.rerecord))) return;
        if (recModal.firstRawEp === null) recModal.firstRawEp = rawEp;
        recModal.curEp = Math.min(recModal.numEpisodes, Math.max(1, (rawEp - recModal.firstRawEp) + 1));
        recModal.rawEp = rawEp; recModal.rerecord = false;
        recModal.hasStarted = true;
      }
      recModal.phase = "recording";
      recModal.phaseStartMs = performance.now();
      updateRecordModalUI();
      return;
    }
    if (/(?:^|\s)Reset the environment\s*$/i.test(line) && recModal.phase === "recording") {
      recModal.phase = "reset";
      recModal.phaseStartMs = performance.now();
      updateRecordModalUI();
      return;
    }
    if (/(?:^|\s)Re-record episode\s*$/i.test(line) && ["recording", "reset"].includes(recModal.phase)) {
      recModal.phase = "preparing_next";
      recModal.rerecord = true;
      recModal.phaseStartMs = performance.now();
      updateRecordModalUI();
      return;
    }
    if (/Encoding videos for episode|\[剧集保存\]/i.test(line)) {
      if (recModal.phase === "saving" || recModal.phase === "closing") return;
      recModal.phase = "saving";
      recModal.phaseStartMs = performance.now();
      updateRecordModalUI();
      return;
    }
    if (/(?:^|\s)(?:Stop recording|Recording stopped)\s*$/i.test(line) && recModal.phase !== "closing") {
      recModal.phase = "closing";
      recModal.phaseStartMs = performance.now();
      updateRecordModalUI();
    }
  }

  A.el("recordForm").addEventListener("submit", function (e) {
    e.preventDefault();
    A.action(async function () {
      await A.autosave.flush();
      A.ok("采集参数已保存", "#dsAlert");
    }, "#dsAlert");
  });
  A.click("btnRecord", function () {
    A.action(async function () {
      A.clearAlert("#dsAlert");
      await A.autosave.flush();
      var started = await A.api.post("/api/record/start", {});
      openRecordModal(started);
      A.ok("采集任务已启动", "#dsAlert");
      A.refreshStatus();
    }, "#dsAlert");
  });
  A.click("btnStop", function () {
    A.action(async function () {
      var s = A.status();
      var j = (s && s.job) || {};
      if (s && s.running && j.kind === "record") {
        // 录制不能用 /api/stop（SIGTERM）：当前剧集来不及保存编码。
        // 发 ESC 给录制进程，由 lerobot-record 自己收尾。
        await A.api.post("/api/record/key", { key: "esc" });
        A.ok("已发送 ESC：录制进程会保存并编码当前剧集后退出", "#dsAlert");
      } else {
        await A.stop();
        A.ok("已停止任务", "#dsAlert");
      }
      A.refreshStatus();
    }, "#dsAlert");
  });
  ["keyRight:right", "keyLeft:left", "keyEsc:esc"].forEach(function (p) {
    var parts = p.split(":");
    A.click(parts[0], function () {
      A.action(async function () {
        await A.api.post("/api/record/key", { key: parts[1] });
        A.ok("已发送按键 " + parts[1].toUpperCase(), "#dsAlert");
      }, "#dsAlert");
    });
  });
  function openMergeDialog(preselectedIds) {
    A.action(async function () {
      var r = await A.api.get("/api/datasets");
      var list = (r.datasets || []).filter(function (d) { return (d.episodes || 0) > 0; });
      var preSet = new Set(preselectedIds || []);
      var mask = document.createElement("div");
      mask.className = "modal-mask";
      mask.innerHTML =
        '<div class="notify-modal" style="width:min(560px,100%)" role="dialog" aria-modal="true">' +
          '<div class="notify-head"><span class="notify-title">合并数据集</span></div>' +
          '<div class="notify-msg">' +
            list.map(function (d) {
              var isChecked = preSet.has(d.repo_id) ? " checked" : "";
              return '<label class="check" style="display:flex;cursor:pointer;margin-top:6px">' +
                '<input type="checkbox" value="' + A.esc(d.repo_id) + '"' + isChecked + '> ' +
                A.esc(d.repo_id) + "（" + d.episodes + " 集）</label>";
            }).join("") +
            '<div class="field wide" style="margin-top:14px"><label>合并后的名称</label>' +
              '<input id="mergeName" placeholder="如 merged_all"></div>' +
            '<div class="hint" style="margin-top:8px">至少勾选 2 个数据集；当前共 ' + list.length +
              ' 个。合并要求相机 / 关节结构一致，名称不能与已有数据集重复。</div>' +
          '</div>' +
          '<div class="notify-foot">' +
            '<button type="button" class="btn subtle" data-cancel>取消</button>' +
            '<button type="button" class="btn primary" data-ok>开始合并</button>' +
          '</div>' +
        '</div>';
      A.modalHost().appendChild(mask);
      var existingNames = list.map(function (d) { return d.repo_id; });
      var defName = "merged_all";
      if (existingNames.indexOf(defName) >= 0) {
        defName = "merged_" + Date.now().toString().slice(-6);
      }
      mask.querySelector("#mergeName").value = defName;
      mask.querySelector("[data-cancel]").addEventListener("click", function () {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
      });
      mask.querySelector("[data-ok]").addEventListener("click", async function () {
        var ids = Array.prototype.slice.call(mask.querySelectorAll("input:checked"))
          .map(function (x) { return x.value; });
        var name = mask.querySelector("#mergeName").value.trim();
        if (ids.length < 2) { A.toast("至少选两个数据集", "warn"); return; }
        if (!name) { A.toast("请填写合并后的名称", "warn"); return; }
        if (existingNames.indexOf(name) >= 0) {
          A.toast("已存在同名数据集：" + name + "，请换一个（如 " + name + "_all）", "warn");
          return;
        }
        if (ids.indexOf(name) >= 0) {
          A.toast("合并后的名称不能和输入数据集重名", "warn");
          return;
        }
        var box = A.blocking("正在合并 " + ids.length + " 个数据集，请勿操作页面…",
          { title: "合并数据集" });
        try {
          await A.api.post("/api/dataset/merge", { repo_ids: ids, new_repo_id: name });
          for (var i = 0; i < 300; i++) {
            await new Promise(function (r) { setTimeout(r, 1000); });
            var st = await A.api.get("/api/status");
            if (!st.running) break;
            box.set("正在合并 " + ids.length + " 个数据集（已等待 " + (i + 1) + " 秒）…");
          }
          box.close();
          if (mask.parentNode) mask.parentNode.removeChild(mask);
          A.ok("合并完成，列表已刷新", "#dsAlert");
          selectedRepoIds.clear();
          await loadDatasets(true);
        } catch (e) {
          box.close();
          A.toast(e.message, "err");
        }
      });
    }, "#dsAlert");
  }

  if (A.el("btnBatchMerge")) {
    A.click("btnBatchMerge", function () {
      var ids = Array.from(selectedRepoIds);
      if (ids.length < 2) {
        A.toast("合并至少需要勾选 2 个数据集", "warn");
        return;
      }
      openMergeDialog(ids);
    });
  }

  A.click("btnMerge", function () {
    var ids = Array.from(selectedRepoIds);
    openMergeDialog(ids);
  });
  A.click("btnReloadDs", function () { loadDatasets(); });

  async function refreshVizWeb() {
    try {
      var s = await A.api.get("/api/viz/web");
      var n = A.el("vizWebState");
      if (n) n.textContent = s.running ? ("已启动 · 浏览器端口 " + s.web_port) : "未启动";
      return s;
    } catch (e) { return null; }
  }
  function vizWebUrl(s) {
    var host = location.hostname || "127.0.0.1";
    return "http://" + host + ":" + s.web_port + "/?url=" +
      encodeURIComponent("rerun+http://" + host + ":" + s.grpc_port + "/proxy");
  }
  function openCamDock(s) {
    var dock = A.el("camDock"), fr = A.el("camDockFrame"), link = A.el("camDockOpen");
    if (!dock || !fr) return;
    var url = vizWebUrl(s);
    if (fr.getAttribute("src") !== url) fr.src = url;
    if (link) link.href = url;
    dock.hidden = false;
    document.body.classList.add("cam-dock-open");
  }
  A.click("btnCamDockClose", function () {
    var dock = A.el("camDock");
    if (dock) dock.hidden = true;
    document.body.classList.remove("cam-dock-open");
  });
  A.click("btnVizWeb", function () {
    A.action(async function () {
      var s = await A.api.post("/api/viz/web/start", {}, 15000);
      A.el("vizWebState").textContent = "已启动 · 浏览器端口 " + s.web_port;
      openCamDock(s);
      A.ok("网页可视化已启动：画面固定在右侧，现在点「开始采集」", "#dsAlert");
    }, "#dsAlert");
  });
  A.click("btnVizWebStop", function () {
    A.action(async function () {
      await A.api.post("/api/viz/web/stop", {});
      A.el("vizWebState").textContent = "未启动";
      var dock = A.el("camDock");
      if (dock) dock.hidden = true;
      document.body.classList.remove("cam-dock-open");
      A.ok("已关闭网页可视化", "#dsAlert");
    }, "#dsAlert");
  });
  refreshVizWeb().then(function (s) { if (s && s.running) openCamDock(s); });

  A.click("btnViz", function () {
    A.action(async function () {
      await A.api.post("/api/dataset/viz", {
        repo_id: A.el("vizRepo").value, episode_index: Number(A.el("vizEp").value) || 0
      });
      A.ok("可视化任务已启动", "#dsAlert");
    }, "#dsAlert");
  });
  A.click("btnReplay", function () {
    A.action(async function () {
      await A.api.post("/api/dataset/replay", {
        repo_id: A.el("vizRepo").value, episode: Number(A.el("vizEp").value) || 0
      });
      A.ok("回放任务已启动", "#dsAlert");
    }, "#dsAlert");
  });

  var timer = null;
  A.onLog(function (ev) {
    if (ev.type === "log" && ev.line) {
      onRecordLogLine(ev.line);
    }
    if (ev.type === "state") {
      if (ev.running && ev.kind === "record" && ev.job_id) {
        if (recModal.active && recModal.jobId && ev.job_id !== recModal.jobId) { A.refreshStatus(); return; }
        syncRecordJob(ev);
      } else if (ev.running === false && ev.job_id === recModal.jobId) {
        if (recModal.active) closeRecordModal();
        clearTimeout(timer);
        timer = setTimeout(function () { loadDatasets(true); }, 800);
      }
    }
  });
  A.onStatus(function (status) {
    if (!status || (recModal.jobStartedAt && status.status_ts < recModal.jobStartedAt)) return;
    if (status.running && status.job && status.job.kind === "record") syncRecordJob(status.job);
    else if (recModal.active) closeRecordModal();
  });

  window.addEventListener("pagehide", function () {
    clearInterval(dsWaybillTimer);
    A.stopWaybillPreview(A.el("dsWaybillPreviewGrid"));
    closeRecordModal();
  });

  await loadConfig();
  var initSt = A.status();
  if (initSt && initSt.running && initSt.job && initSt.job.kind === "record") {
    syncRecordJob(initSt.job);
  }
  A.autosave.form('recordForm','dataset');
  A.autosave.form('vizForm','viz');
  try {
    await A.autosave.preferences(['dsWaybillCamSelect', 'chkDsAutoScanWaybill', 'chkDsIdleScanWaybill', 'logAuto'], 'dataset');
  } catch (e) { /* ignore */ }
  await loadDatasets(true);
  A.autosave.onConfig(function(){A.commandPreview('record','#workflowCommand');});
  A.commandPreview('record','#workflowCommand');
  scanDsWaybillOnce(false);
})();
