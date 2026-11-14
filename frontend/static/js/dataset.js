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
      A.ok("采集任务已启动，录制窗口键盘控制见下方按钮", "#dsAlert");
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
    if (ev.type === "state" && ev.running === false) {
      clearTimeout(timer);
      timer = setTimeout(function () { loadDatasets(true); }, 800);
    }
  });

  await loadConfig();
  A.autosave.form('recordForm','dataset');
  await loadDatasets(true);
  A.autosave.onConfig(function(){A.commandPreview('record','#workflowCommand');});
  A.commandPreview('record','#workflowCommand');
})();
