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
      A.fillForm(f, c.dataset);
      A.el("vizRepo").value = c.viz.repo_id || c.dataset.repo_id || "";
      A.el("vizEp").value = c.viz.episode_index || 0;
      A.clearState(f);
    } catch (e) {
      A.loadError(f, e, loadConfig, { attach: true });
      A.setAlert("#dsAlert", e.message, "err");
    }
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
      A.el("dsRoot").textContent = "目录 " + r.root;
      if (!r.datasets.length) {
        A.loadEmpty(body, "数据集目录为空",
          "请先点「开始采集」录制数据，或刷新列表重试", function () { loadDatasets(true); });
        if (!quiet) A.ok("数据集列表已刷新（暂无数据）");
        return;
      }
      body.innerHTML = r.datasets.map(function (d) {
            return "<tr><td><code>" + A.esc(d.repo_id) + "</code></td><td>" +
              A.esc(d.episodes) + "</td><td>" + A.esc(d.size_mb) + " MB</td><td class=\"muted\">" +
              A.esc(d.path) + "</td><td><div class=\"actions\">" +
              '<button class="btn sm primary" data-view="' + A.esc(d.repo_id) + '">网页查看</button>' +
              '<button class="btn sm subtle" data-viz="' + A.esc(d.repo_id) + '">可视化</button>' +
              '<button class="btn sm" data-replay="' + A.esc(d.repo_id) + '">回放</button>' +
              '<button class="btn sm" data-fill="' + A.esc(d.path) + '">用作训练目录</button>' +
              '<button class="btn sm danger" data-del="' + A.esc(d.repo_id) + '">删除</button>' +
              "</div></td></tr>";
        }).join("");

      A.qa("#dsTable [data-view]").forEach(function (b) {
        b.addEventListener("click", function () {
          location.href = "/p/viewer?repo=" + encodeURIComponent(b.dataset.view);
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
            A.ok("数据集已删除", "#dsAlert");
            await loadDatasets(true);
          }, "#dsAlert");
        };
      });
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
    curEp: 1, phase: "init", /* init | recording | reset | saving */
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
    recModal.resetTimeS = Math.max(0, Number((f && f.elements.reset_time_s && f.elements.reset_time_s.value) || 10));
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
      recModal.phase = "saving";
      updateRecordModalUI();
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
    var now = Date.now();
    var elapsedPhase = (now - recModal.phaseStartMs) / 1000;

    /* 当真实日志因缓冲延迟未到达时，按时间自动推演阶段 전환 */
    if (recModal.phase === "recording" && elapsedPhase >= recModal.epTimeS) {
      if (recModal.curEp < recModal.numEpisodes && recModal.resetTimeS > 0) {
        recModal.phase = "reset";
        recModal.phaseStartMs = now;
        elapsedPhase = 0;
      } else if (recModal.curEp < recModal.numEpisodes) {
        recModal.curEp++;
        recModal.phaseStartMs = now;
        elapsedPhase = 0;
      } else {
        recModal.phase = "saving";
        recModal.phaseStartMs = now;
        elapsedPhase = 0;
      }
    } else if (recModal.phase === "reset" && elapsedPhase >= recModal.resetTimeS) {
      if (recModal.curEp < recModal.numEpisodes) {
        recModal.curEp++;
        recModal.phase = "recording";
        recModal.phaseStartMs = now;
        elapsedPhase = 0;
      } else {
        recModal.phase = "saving";
        recModal.phaseStartMs = now;
        elapsedPhase = 0;
      }
    }

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

    epInfo.textContent = "剧集 " + recModal.curEp + " / " + recModal.numEpisodes +
      (recModal.repoId ? (" · " + recModal.repoId) : "");
    epSub.textContent = "单集时长 " + recModal.epTimeS + "s";
    resetSub.textContent = recModal.curEp >= recModal.numEpisodes ? "最后一集无重置" : ("重置时长 " + recModal.resetTimeS + "s");

    var curEpLeft = recModal.epTimeS;
    var curResetLeft = recModal.curEp >= recModal.numEpisodes ? 0 : recModal.resetTimeS;
    var pct = 0;

    if (recModal.phase === "init") {
      badge.textContent = "启动与连接设备";
      badge.className = "badge";
      curEpLeft = recModal.epTimeS;
      curResetLeft = recModal.curEp >= recModal.numEpisodes ? 0 : recModal.resetTimeS;
      boxEp.style.borderColor = "var(--border,#e2e8f0)";
      boxEp.style.background = "transparent";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      pct = 2;
      hint.textContent = recModal.lastLog || "正在初始化主从臂与摄像头，进入录制后自动开始倒计时…";
    } else if (recModal.phase === "recording") {
      curEpLeft = Math.max(0, recModal.epTimeS - elapsedPhase);
      curResetLeft = recModal.curEp >= recModal.numEpisodes ? 0 : recModal.resetTimeS;
      badge.textContent = "正在录制第 " + recModal.curEp + " 集";
      badge.className = "badge ok";
      boxEp.style.borderColor = "var(--primary,#2563eb)";
      boxEp.style.background = "var(--bg-subtle,rgba(37,99,235,0.08))";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      pct = Math.min(100, Math.max(0, (elapsedPhase / recModal.epTimeS) * 100));
      hint.textContent = "请操作主臂完成动作示教；本集倒计时结束后将自动进入重置阶段。";
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
      hint.textContent = "请趁重置倒计时将桌面上的快递恢复到初始摆放位置，准备下一集录制。";
    } else {
      curEpLeft = 0;
      curResetLeft = 0;
      badge.textContent = "正在保存并编码视频";
      badge.className = "badge";
      boxEp.style.borderColor = "var(--border,#e2e8f0)";
      boxEp.style.background = "transparent";
      boxReset.style.borderColor = "var(--border,#e2e8f0)";
      boxReset.style.background = "transparent";
      pct = 100;
      hint.textContent = recModal.lastLog || "正在编码视频并安全收尾，完全结束后此窗口将自动关闭…";
    }

    epRemain.textContent = fmtSec(curEpLeft);
    resetRemain.textContent = fmtSec(curResetLeft);
    bar.style.width = pct.toFixed(1) + "%";

    var futureEps = Math.max(0, recModal.numEpisodes - recModal.curEp);
    var futureSec = futureEps * recModal.epTimeS + Math.max(0, futureEps - 1) * recModal.resetTimeS;
    var totalLeft = recModal.phase === "saving" ? 0 : (curEpLeft + curResetLeft + futureSec);
    totalInfo.textContent = recModal.phase === "saving" ? "正在收尾保存…" : ("整个采集预计剩余 " + fmtSec(totalLeft));
  }

  function openRecordModal() {
    readRecordParams();
    recModal.active = true;
    recModal.curEp = 1;
    recModal.phase = "init";
    recModal.phaseStartMs = Date.now();
    recModal.sessionStartMs = Date.now();
    recModal.lastLog = "";
    ensureRecordModal();
    var btn = recModal.mask.querySelector("#recModalStop");
    if (btn) { btn.disabled = false; btn.textContent = "停止录制"; }
    updateRecordModalUI();
    clearInterval(recModal.tickTimer);
    recModal.tickTimer = setInterval(updateRecordModalUI, 250);
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

  function onRecordLogLine(line) {
    if (!recModal.active || !line) return;
    recModal.lastLog = line;
    var mEp = line.match(/Recording episode\s+(\d+)/i);
    if (mEp) {
      recModal.curEp = Math.min(recModal.numEpisodes, Math.max(1, parseInt(mEp[1], 10) + 1));
      recModal.phase = "recording";
      recModal.phaseStartMs = Date.now();
      updateRecordModalUI();
      return;
    }
    if (/Reset the environment/i.test(line)) {
      recModal.phase = "reset";
      recModal.phaseStartMs = Date.now();
      updateRecordModalUI();
      return;
    }
    if (/Re-record episode/i.test(line)) {
      recModal.phase = "recording";
      recModal.phaseStartMs = Date.now();
      updateRecordModalUI();
      return;
    }
    if (/Stop recording|Finalize|Encoding|safe_record/i.test(line)) {
      recModal.phase = "saving";
      recModal.phaseStartMs = Date.now();
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
      await A.api.post("/api/record/start", {});
      openRecordModal();
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
  A.click("btnMerge", function () {
    A.action(async function () {
      var r = await A.api.get("/api/datasets");
      var list = (r.datasets || []).filter(function (d) { return (d.episodes || 0) > 0; });
      var mask = document.createElement("div");
      mask.className = "modal-mask";
      mask.innerHTML =
        '<div class="notify-modal" style="width:min(560px,100%)" role="dialog" aria-modal="true">' +
          '<div class="notify-head"><span class="notify-title">合并数据集</span></div>' +
          '<div class="notify-msg">' +
            list.map(function (d) {
              return '<label class="check" style="display:flex;cursor:pointer;margin-top:6px">' +
                '<input type="checkbox" value="' + A.esc(d.repo_id) + '"> ' +
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
          await loadDatasets(true);
        } catch (e) {
          box.close();
          A.toast(e.message, "err");
        }
      });
    }, "#dsAlert");
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
      if (ev.running && ev.kind === "record" && !recModal.active) {
        openRecordModal();
      } else if (ev.running === false) {
        if (recModal.active) closeRecordModal();
        clearTimeout(timer);
        timer = setTimeout(function () { loadDatasets(true); }, 800);
      }
    }
  });

  window.addEventListener("pagehide", function () { closeRecordModal(); });

  await loadConfig();
  var initSt = A.status();
  if (initSt && initSt.running && initSt.job && initSt.job.kind === "record") {
    openRecordModal();
  }
  A.autosave.form('recordForm','dataset');
  await loadDatasets(true);
  A.autosave.onConfig(function(){A.commandPreview('record','#workflowCommand');});
  A.commandPreview('record','#workflowCommand');
})();
