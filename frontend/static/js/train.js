/* 模型训练：引擎选择 → 对应真实训练代码分发 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "train", title: "模型训练", subtitle: "选择 OpenCV / YOLO / LeRobot 训练引擎" });
  A.attachLogPanel(A.el("logPanel"));

  var backends = [];
  var selected = "lerobot";
  var series=A.lossSeries(600), trainAutosave = null, checkpoints = [], inspectSerial = 0;

  function draw() {
    var losses=series.points(),axes=A.lossAxes(series.unit());
    A.chart(A.el("lossChart"), losses, axes);
    A.el('lossAxes').textContent='X：'+axes.xLabel+' · Y：'+axes.yLabel+' · 显示最近 600 个训练位置，同一步更新为最新值';
    var box = A.el("lossCount");
    if (!box) return;
    if (!losses.length) {
      box.textContent = "等待训练指标（loss）…";
      return;
    }
    var vals = losses.map(function (p) { return p.value; });
    var last = vals[vals.length - 1];
    var min = Math.min.apply(null, vals);
    var avg = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
    box.textContent = "最新 " + last.toFixed(4) + " · 最小 " + min.toFixed(4) +
      " · 平均 " + avg.toFixed(4) + " · " + vals.length + " 点 · 最新位置 " + losses[losses.length-1].step;
  }

  function num(id, fallback) {
    var v = Number(A.el(id).value);
    return isFinite(v) && A.el(id).value !== "" ? v : fallback;
  }
  function str(id) { return A.el(id).value.trim(); }

  function buildParams(engine) {
    engine = engine || selected;
    if (engine === "lerobot") {
      return {
        repo_id: str("l_repo_id"), policy_type: str("l_policy_type") || "act",
        dataset_dir: str("l_dataset_dir"), base_model: str("l_base_model"),
        training_mode: str('l_training_mode'), learning_rate: str('l_policy_type') === 'act' ? num('l_learning_rate', 0) : 0,
        steps: num("l_steps", 300000), batch_size: num("l_batch_size", 0),
        device: str("l_device") || "cuda", output_dir: str("l_output_dir"),
        job_name: str("l_job_name"), wandb: A.el("l_wandb").checked,
        video_backend: str("l_video_backend")
      };
    }
    if (engine === "yolo") {
      return {
        data_yaml: str("y_data_yaml"), dataset_dir: str("y_dataset_dir"),
        model: str("y_model") || "yolov8n.pt", epochs: num("y_epochs", 50),
        imgsz: num("y_imgsz", 640), batch: num("y_batch", 8),
        device: str("y_device") || "cpu", output_dir: str("y_output_dir") || "outputs/train/yolo"
      };
    }
    return {
      dataset_dir: str("o_dataset_dir"), classifier: str("o_classifier") || "svm",
      epochs: num("o_epochs", 20), img_size: num("o_img_size", 64),
      output_dir: str("o_output_dir") || "outputs/train/opencv"
    };
  }

  function current() {
    return backends.filter(function (b) { return b.key === selected; })[0] || null;
  }

  function updateDep() {
    syncBaseModelRow();
    var b = current(), box = A.el("depBox");
    if (!b) { box.className = "alert"; box.textContent = ""; return; }
    if (b.available) {
      A.setAlert(box, "依赖检测通过：" + b.engine + " 可以开始训练。", "ok");
    } else {
      A.setAlert(box, "依赖未就绪：\n" + b.problems.join("\n") +
        (b.install_hint ? "\n安装命令：" + b.install_hint : "\n请到「环境安装」页面安装 LeRobot"), "err");
    }
    var inst = A.el("btnInstallDeps");
    inst.disabled = (selected === "lerobot");
    inst.textContent = selected === "lerobot"
      ? "LeRobot 请到「环境安装」页面安装" : ("安装 " + selected + " 训练依赖（清华源）");
    A.el("btnResume").disabled = (selected !== "lerobot");
    var p = buildParams();
    var policyArg = p.training_mode === 'finetune' ? ("--policy.path=" + (p.base_model || '(请选择已有模型)'))
      : ("--policy.type=" + p.policy_type);
    var dsArg = p.dataset_dir
      ? ("--dataset.root=" + p.dataset_dir + " --dataset.repo_id=" +
         p.dataset_dir.replace(/\/+$/, "").split("/").pop())
      : ("--dataset.repo_id=" + p.repo_id);
    A.el("cmdPreview").textContent = selected === "lerobot"
      ? ("lerobot-train " + dsArg + " " + policyArg +
         " --steps=" + p.steps + " --job_name=" + (p.job_name || "(启动时自动生成)") +
         " --output_dir=" + p.output_dir + (p.learning_rate > 0 && p.policy_type === 'act' ? ' --policy.optimizer_lr=' + p.learning_rate + ' --policy.optimizer_lr_backbone=' + p.learning_rate : ''))
      : (selected === "yolo"
        ? ("python -u backend/trainers/yolo_trainer.py --data " + (p.data_yaml || "(自动生成)") +
           " --epochs " + p.epochs + " --output " + p.output_dir)
        : ("python -u backend/trainers/opencv_trainer.py --dataset " + p.dataset_dir +
           " --classifier " + p.classifier + " --epochs " + p.epochs + " --output " + p.output_dir));
  }

  function selectEngine(key) {
    selected = key;
    A.qa("#engineCards .radio-card").forEach(function (c) {
      c.className = "radio-card" + (c.dataset.key === key ? " sel" : "");
    });
    ["lerobot", "yolo", "opencv"].forEach(function (k) {
      A.el("params-" + k).hidden = (k !== key);
    });
    var b = current();
    A.el("paramsTitle").textContent = b ? b.label : key;
    A.el("engineDesc").textContent = b ? b.desc : "";
    updateDep();
  }

  async function loadBackends(quiet) {
    A.showLoading("#engineCards", {
      title: "正在检测训练引擎…",
      detail: "正在检测 OpenCV / YOLO / LeRobot 的真实依赖", rows: 3
    });
    A.el("engineDesc").textContent = "正在读取引擎信息 …";
    A.setAlert("#depBox", "正在读取依赖状态 …", "info");
    try {
      var r = await A.api.get("/api/train/backends" + (quiet ? "" : "?force=1"), 30000);
      backends = r.backends;
      A.el("engineCards").innerHTML = backends.map(function (b) {
        return '<div class="radio-card" data-key="' + A.esc(b.key) + '">' +
          "<strong>" + A.esc(b.label) + "</strong><small>" + A.esc(b.engine) + "</small>" +
          '<div style="margin-top:6px">' + (b.available
            ? '<span class="badge ok">依赖就绪</span>'
            : '<span class="badge err">依赖缺失</span>') + "</div></div>";
      }).join("");
      A.qa("#engineCards .radio-card").forEach(function (c) {
        c.onclick = function () { selectEngine(c.dataset.key); if(trainAutosave)trainAutosave.changed(); };
      });
      selectEngine(selected);
      if (!quiet) A.ok("训练引擎依赖已重新检测");
    } catch (e) {
      A.loadError("#engineCards", e, function () { loadBackends(true); });
      A.el("engineDesc").textContent = "引擎信息读取失败";
      A.setAlert("#depBox", "依赖状态读取失败：" + e.message, "err");
      A.setAlert("#trainAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  async function loadRepoIdOptions(preferred) {
    var sel = A.el("l_repo_id");
    try {
      var r = await A.api.get("/api/datasets");
      // 只列有剧集的数据集，避免把 0 集的空目录自动选成训练数据
      var ds = (r.datasets || []).filter(function (d) { return (d.episodes || 0) > 0; });
      if (!ds.length) {
        sel.innerHTML = '<option value="">（暂无有效数据集，请先到「训练数据」采集）</option>';
        return;
      }
      sel.innerHTML = ds.map(function (d) {
        return '<option value="' + A.esc(d.repo_id) + '">' + A.esc(d.repo_id) +
          " · " + d.episodes + " 集</option>";
      }).join("");
      var want = String(preferred || "").trim();
      var hit = ds.filter(function (d) { return d.repo_id === want; })[0];
      sel.value = hit ? want : ds[0].repo_id;
    } catch (e) {
      sel.innerHTML = '<option value="">读取数据集失败</option>';
    }
  }

  var HF_MIRROR = "https://hf-mirror.com";

  async function updateModelCacheStatus() {
    var box = A.el("hfStatus");
    if (!box) return;
    var mid = (A.el("l_base_model").value || "").trim();
    if (str('l_training_mode') !== 'finetune' || !mid) { box.textContent = "微调时复用已有模型；从头训练无需选择基础模型。"; return; }
    if (mid.startsWith('/') || mid.startsWith('.') || mid.startsWith('outputs/')) { box.textContent = '本地模型：' + mid + ' · 启动前检查权重与数据集兼容性'; return; }
    box.textContent = "正在检查本地模型…";
    try {
      var r = await A.api.get("/api/model/cache?model_id=" + encodeURIComponent(mid));
      if (r.cached) {
        box.textContent = "基础模型：" + mid + " · 本地已缓存" +
          (r.size_mb ? "（" + r.size_mb + " MB）" : "") + "，训练不会重复下载";
        box.className = "hint ok";
      } else {
        box.textContent = "基础模型：" + mid + " · 本地未缓存，训练时会从当前下载源拉取";
        box.className = "hint warn";
      }
    } catch (e) {
      box.textContent = "基础模型：" + mid + " · 缓存状态读取失败：" + e.message;
      box.className = "hint";
    }
  }

  function hfEndpointValue() {
    var mode = A.el("hfMode").value;
    if (mode === "mirror") return HF_MIRROR;
    if (mode === "custom") return (A.el("hfEndpoint").value || "").trim();
    return "";
  }

  async function saveHf() {
    var mode = A.el("hfMode").value;
    await A.saveConfig({ hf: { endpoint: hfEndpointValue(), auto: mode === "auto" } });
  }

  async function loadHf(cfg) {
    var hf = cfg.hf || {};
    var ep = String(hf.endpoint || "").trim();
    var auto = hf.auto !== false;
    var mode = "auto";
    if (ep && ep.replace(/\/$/, "") === HF_MIRROR) mode = "mirror";
    else if (ep) mode = "custom";
    else if (!auto) mode = "official";
    A.el("hfMode").value = mode;
    A.el("hfEndpoint").value = ep;
    A.el("hfCustomWrap").style.display = mode === "custom" ? "" : "none";
  }

  A.change('hfMode',function(){A.el('hfCustomWrap').style.display=A.el('hfMode').value==='custom'?'':'none';});
  A.click("btnHfTest", function () {
    A.action(async function () {
      A.el("hfStatus").textContent = "正在检测 huggingface.co 与 hf-mirror.com（最多 8 秒）…";
      var r = await A.api.get("/api/hf/status?probe=true");
      var parts = [];
      parts.push("官方：" + (r.official ? "✓ 可访问" : "✗ 不可访问"));
      parts.push("镜像：" + (r.mirror ? "✓ 可访问" : "✗ 不可访问"));
      var mode = A.el("hfMode").value;
      parts.push("当前选择：" + ({auto: "自动", official: "官方", mirror: "镜像", custom: "自定义"}[mode] || mode));
      A.el("hfStatus").textContent = parts.join(" · ");
      await updateModelCacheStatus();
    }, "#trainAlert");
  });

  async function syncDatasetDirFromRepo(save) {
    var repo = A.el("l_repo_id").value;
    if (!repo) return;
    try {
      var r = await A.api.get("/api/datasets");
      var d = (r.datasets || []).filter(function (x) { return x.repo_id === repo; })[0];
      if (!d || !d.path) return;
      A.el("l_dataset_dir").value = d.path;
      invalidateInspection();
      A.el("dsDirInfo").textContent = "当前目录：" + d.path + " · " +
        (d.episodes || 0) + " 集";
      if (save) {
        await A.saveConfig({ train: { dataset_dir: d.path, repo_id: repo } });
      }
      updateDep();
    } catch (e) { /* 目录自动填入失败不阻塞 */ }
  }

  async function loadConfig() {
    var c = await A.api.get("/api/config");
    var t = c.train || {}, y = c.yolo || {}, o = c.opencv || {};
    await loadRepoIdOptions(t.repo_id);
    A.el("l_policy_type").value = t.policy_type || "act";
    A.el('l_training_mode').value = t.training_mode || (t.base_model ? 'finetune' : 'scratch');
    syncBaseModelRow();
    A.el("l_base_model").value = t.base_model || "";
    A.el("l_dataset_dir").value = t.dataset_dir || "";
    if (!A.el("l_dataset_dir").value) {
      await syncDatasetDirFromRepo(false);
    }
    loadHf(c);
    updateModelCacheStatus();
    A.api.get("/api/dataset/root").then(function (r) {
      A.el("dsDirInfo").textContent = "平台录制目录：" + r.root +
        (A.el("l_dataset_dir").value ? " · 当前使用目录：" + A.el("l_dataset_dir").value : "");
    }).catch(function () {});
    A.el("l_steps").value = t.steps || 300000;
    A.el("l_batch_size").value = t.batch_size || 0;
    A.el('l_learning_rate').value = t.learning_rate || 0;
    A.el("l_device").value = t.device || "cuda";
    A.el("l_output_dir").value = t.output_dir || "";
    A.el("l_job_name").value = t.job_name || "";
    A.el("l_wandb").checked = Boolean(t.wandb);
    A.el("l_video_backend").value = t.video_backend || "";
    A.el("y_data_yaml").value = y.data_yaml || "";
    A.el("y_dataset_dir").value = y.dataset_dir || "";
    A.el("y_model").value = y.model || "yolov8n.pt";
    A.el("y_epochs").value = y.epochs || 50;
    A.el("y_imgsz").value = y.imgsz || 640;
    A.el("y_batch").value = y.batch || 8;
    A.el("y_device").value = y.device || "cpu";
    A.el("y_output_dir").value = y.output_dir || "outputs/train/yolo";
    A.el("o_dataset_dir").value = o.dataset_dir || "";
    A.el("o_classifier").value = o.classifier || "svm";
    A.el("o_epochs").value = o.epochs || 20;
    A.el("o_img_size").value = o.img_size || 64;
    A.el("o_output_dir").value = o.output_dir || "outputs/train/opencv";
    if (t.backend && ["lerobot", "yolo", "opencv"].indexOf(t.backend) >= 0) selected = t.backend;
    selectEngine(selected);
  }

  async function loadDatasets() {
    var sel = A.el("dsPicker");
    A.showLoading(sel, {
      title: "正在读取已录制数据集…", detail: "用于填充训练目录"
    });
    try {
      var r = await A.api.get("/api/datasets");
      if (r.datasets.length) {
        A.clearState(sel);
        sel.innerHTML = r.datasets.map(function (d) {
          return '<option value="' + A.esc(d.path) + '">' + A.esc(d.repo_id) + "</option>";
        }).join("");
      } else {
        A.loadEmpty(sel, "暂无已录制数据集",
          "请先到「训练数据」页采集", function () { loadDatasets(); });
      }
    } catch (e) {
      A.loadError(sel, e, loadDatasets);
      A.setAlert("#trainAlert", e.message, "err");
    }
  }

  function isPolicyCheckpoint(c){return c.can_finetune !== false && (c.policy_type || /(?:^|\/)pretrained_model\/?$/.test(c.path));}
  function populateCheckpoints(){
    var select=A.el('l_checkpoint'),type=str('l_policy_type'),base=str('l_base_model');
    var choices=checkpoints.filter(function(c){return isPolicyCheckpoint(c)&&(!c.policy_type||c.policy_type===type);});
    select.innerHTML='<option value="">手动输入模型路径</option>'+choices.map(function(c){return '<option value="'+A.esc(c.path)+'">'+A.esc(c.path)+'</option>';}).join('');
    select.value=choices.some(function(c){return c.path===base;})?base:'';
  }
  function invalidateInspection(){inspectSerial++;A.el('finetuneStatus').textContent='模型或数据集变更后，请重新检查兼容性。';}
  async function inspectFinetune(){
    var serial=++inspectSerial,box=A.el('finetuneStatus');box.textContent='正在检查模型权重和数据集特征…';
    try{
      var result=await A.api.post('/api/train/finetune/inspect',buildParams('lerobot'));
      if(serial!==inspectSerial)throw new Error('模型或数据集已变化，请重新检查后启动微调');
      box.textContent='检查通过：'+result.note;return result;
    }catch(error){if(serial===inspectSerial)box.textContent='检查未通过：'+error.message;throw error;}
  }
  function selectCheckpoint(path){
    var entry=checkpoints.find(function(c){return c.path===path;});
    if(entry&&entry.policy_type)A.el('l_policy_type').value=entry.policy_type;
    A.el('l_training_mode').value='finetune';A.el('l_base_model').value=path;
    selectEngine('lerobot');populateCheckpoints();invalidateInspection();updateModelCacheStatus();
    if(trainAutosave)trainAutosave.changed();
    A.action(inspectFinetune,'#trainAlert');
  }

  async function loadCheckpoints() {
    var body = A.q("#ckptTable tbody");
    A.showLoading(body, {
      title: "正在扫描模型产出…", detail: "正在扫描 outputs/ 下的真实文件", rows: 2
    });
    try {
      var r = await A.api.get("/api/checkpoints");
      checkpoints = r.checkpoints || [];
      populateCheckpoints();
      if (r.checkpoints.length) {
        body.innerHTML = r.checkpoints.map(function (c) {
          return "<tr><td><code>" + A.esc(c.path) + "</code>" +
            (c.config ? '<br><span class="muted">' + A.esc(c.config) + "</span>" : "") +
            '</td><td>' + (isPolicyCheckpoint(c) ? '<button type="button" class="btn sm" data-ckpt-finetune="' + A.esc(c.path) + '">用于微调</button> ' + (!c.policy_type || c.policy_type==='act' ? '<button type="button" class="btn sm" data-model-eval="'+A.esc(c.path)+'">离线评估</button> ' : '') : '') + '<button type="button" class="btn sm danger" data-app-nofb ' +
            'data-ckpt-del="' + A.esc(c.path) + '">删除</button></td></tr>';
        }).join("");
      } else {
        A.loadEmpty(body, "outputs/ 下暂无模型产出",
          "训练完成后可点「重新加载」刷新", function () { loadCheckpoints(); });
      }
      return r.checkpoints;
    } catch (e) {
      A.loadError(body, e, loadCheckpoints);
      A.setAlert("#trainAlert", e.message, "err");
      return [];
    }
  }

  document.addEventListener("click", async function (e) {
    var choose = e.target && e.target.closest ? e.target.closest('[data-ckpt-finetune]') : null;
    if (choose) {
      e.preventDefault();selectCheckpoint(choose.getAttribute('data-ckpt-finetune'));
      A.el('l_training_mode').scrollIntoView({block:'start',behavior:'instant'});
      return;
    }
    var b = e.target && e.target.closest ? e.target.closest("[data-ckpt-del]") : null;
    if (!b) return;
    e.preventDefault();
    var p = b.getAttribute("data-ckpt-del");
    if (!(await A.confirm("确认删除模型产出？\n" + p +
        "\n\n会删除该检查点目录（含 training_state），不可恢复。",
        { title: "删除模型产出" }))) return;
    A.action(async function () {
      var r = await A.api.del("/api/checkpoints/" + p + "?confirm=true", 30000);
      A.ok("已删除，释放 " + r.freed_mb + " MB", "#trainAlert");
      await loadCheckpoints();
    }, "#trainAlert");
  });

  A.click("btnCheckBackends", function () {
    A.action(loadBackends.bind(null, false), "#trainAlert");
  });
  A.click("btnInstallDeps", function () {
    A.action(async function () {
      A.clearAlert("#trainAlert");
      await A.api.post("/api/train/install-deps", { backend: selected });
      A.ok("依赖安装已启动，完成后请点「重新检测依赖」", "#trainAlert");
      A.refreshStatus();
    }, "#trainAlert");
  });
  async function chooseDatasetDir(path) {
    var r = await A.api.get("/api/dataset/inspect?path=" + encodeURIComponent(path));
    A.el("l_dataset_dir").value = r.path;
    invalidateInspection();
    A.el("dsDirInfo").textContent = "当前目录：" + r.path + " · " + r.episodes + " 集 · " +
      r.frames + " 帧 · 相机：" + (r.cameras || []).join(", ");
    await A.saveConfig({ train: { dataset_dir: r.path, repo_id: r.name } });
    A.ok("已选择数据集目录：" + r.path, "#trainAlert");
    updateDep();
  }

  function openDatasetDirPicker(startPath) {
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="notify-modal" style="width:min(640px,100%)" role="dialog" aria-modal="true">' +
        '<div class="notify-head"><span class="notify-title">选择数据集目录</span></div>' +
        '<div class="notify-msg">' +
          '<div class="field wide"><label>当前目录</label><div class="note" id="dpPath"></div></div>' +
          '<div class="btn-row" style="margin-top:10px">' +
            '<button type="button" class="btn subtle sm" data-root>平台录制目录</button>' +
            '<button type="button" class="btn subtle sm" data-up>上一级</button>' +
          '</div>' +
          '<div id="dpList" style="max-height:42vh;overflow:auto;margin-top:10px"></div>' +
          '<div class="hint" id="dpHint" style="margin-top:8px"></div>' +
        '</div>' +
        '<div class="notify-foot">' +
          '<button type="button" class="btn subtle" data-close>关闭</button>' +
          '<button type="button" class="btn primary" data-pick disabled>选择此目录</button>' +
        '</div>' +
      '</div>';
    A.modalHost().appendChild(mask);
    var cur = startPath || "";
    var rootPath = "";

    function paint(path) {
      A.api.get("/api/dataset/browse" + (path ? "?path=" + encodeURIComponent(path) : ""))
        .then(function (r) {
          cur = r.path;
          rootPath = r.root;
          mask.querySelector("#dpPath").textContent = r.path;
          var html = r.entries.map(function (e) {
            if (e.dataset) {
              return '<div class="row" style="margin-top:8px;align-items:center">' +
                '<div class="field wide"><div><b>' + A.esc(e.name) + '</b> ' +
                  '<span class="badge ok">数据集 ' + (e.episodes || 0) + ' 集</span></div></div>' +
                '<div class="btn-row">' +
                  '<button type="button" class="btn subtle sm" data-pick-row="' + A.esc(e.path) + '">选择</button>' +
                  '<button type="button" class="btn subtle sm" data-enter="' + A.esc(e.path) + '">进入</button>' +
                '</div></div>';
            }
            return '<div class="row" style="margin-top:8px;align-items:center">' +
              '<div class="field wide"><div>' + A.esc(e.name) + '</div></div>' +
              '<div class="btn-row"><button type="button" class="btn subtle sm" data-enter="' +
                A.esc(e.path) + '">进入</button></div></div>';
          }).join("");
          if (!html) html = '<div class="hint">空目录</div>';
          var dsEntries = r.entries.filter(function (e) { return e.dataset; });
          if (!r.parent && dsEntries.length >= 2) {
            html += '<div class="btn-row" style="margin-top:14px">' +
              '<button type="button" class="btn primary sm" data-merge-all="1">合并该目录下全部 ' +
              dsEntries.length + ' 个数据集</button></div>';
          }
          mask.querySelector("#dpList").innerHTML = html;
          mask.querySelector("[data-pick]").disabled = !r.is_dataset;
          mask.querySelector("#dpHint").textContent = r.is_dataset
            ? "当前目录是有效数据集，可直接选择"
            : "进入包含 meta/info.json 的数据集目录后才能选择";
          mask.querySelector("[data-pick]").onclick = function () {
            chooseDatasetDir(cur).then(close).catch(function (e) { A.toast(e.message, "err"); });
          };
          mask.querySelectorAll("[data-enter]").forEach(function (b) {
            b.addEventListener("click", function () { paint(b.getAttribute("data-enter")); });
          });
          mask.querySelectorAll("[data-pick-row]").forEach(function (b) {
            b.addEventListener("click", function () {
              chooseDatasetDir(b.getAttribute("data-pick-row")).then(close)
                .catch(function (e) { A.toast(e.message, "err"); });
            });
          });
          var mergeBtn = mask.querySelector("[data-merge-all]");
          if (mergeBtn) {
            mergeBtn.addEventListener("click", async function () {
              var ids = dsEntries.map(function (e) { return e.name; });
              if (!ids.length) return;
              if (!(await A.confirm("把该目录下 " + ids.length + " 个数据集合并成一个？\n" +
                ids.join("\n"), { title: "合并全部数据集" }))) return;
              try {
                await A.api.post("/api/dataset/merge",
                  { repo_ids: ids, new_repo_id: "train_all" });
                A.ok("合并任务已启动，完成后刷新数据集列表再选 train_all", "#trainAlert");
                close();
              } catch (e) { A.toast(e.message, "err"); }
            });
          }
        })
        .catch(function (e) { A.toast(e.message, "err"); });
    }

    function close() { if (mask.parentNode) mask.parentNode.removeChild(mask); }
    mask.querySelector("[data-close]").addEventListener("click", close);
    mask.querySelector("[data-root]").addEventListener("click", function () { paint(rootPath || ""); });
    mask.querySelector("[data-up]").addEventListener("click", function () {
      A.api.get("/api/dataset/browse" + (cur ? "?path=" + encodeURIComponent(cur) : ""))
        .then(function (r) { if (r.parent) paint(r.parent); else paint(""); })
        .catch(function (e) { A.toast(e.message, "err"); });
    });
    paint(startPath && startPath !== rootPath ? startPath : "");
  }

  A.click("btnBrowseDs", function () { openDatasetDirPicker(A.el("l_dataset_dir").value); });
  A.click("btnClearDs", function () {
    A.action(async function () {
      A.el("l_dataset_dir").value = "";
      invalidateInspection();
      await A.saveConfig({ train: { dataset_dir: "" } });
      A.ok("已清除目录，将使用上方 repo_id", "#trainAlert");
      updateDep();
    }, "#trainAlert");
  });

  A.click("btnTrain", function () {
    A.action(async function () {
      A.clearAlert("#trainAlert");
      if(trainAutosave)await trainAutosave.flush();
      var p = buildParams();
      if(selected === 'lerobot' && p.training_mode === 'scratch')p.base_model='';
      if (selected === "lerobot" && !p.repo_id) {
        throw new Error("请先选择数据集：还没有可用的已录制数据集，请到「训练数据」页采集");
      }
      if(selected === 'lerobot' && p.training_mode === 'finetune')await inspectFinetune();
      var r = await A.api.post("/api/train/start", { backend: selected, params: p });
      if (selected === "lerobot" && r.job_name) {
        A.el("l_job_name").value = r.job_name;
        if(r.output_dir)A.el('l_output_dir').value=r.output_dir;
        await A.saveConfig({ train: { job_name: r.job_name, output_dir: r.output_dir || p.output_dir } });
      }
      series.begin(r.job_id);draw();
      A.ok((p.training_mode === 'finetune' ? '微调' : '训练') + "已启动：" + r.name + (r.output_dir ? '\n保存到：' + r.output_dir : ''), "#trainAlert");
      A.refreshStatus();
    }, "#trainAlert");
  });
  A.click("btnStop", function () {
    A.action(async function () {
      await A.stop();
      A.ok("已请求停止；归零任务会从当前位置慢速收工", "#trainAlert");
      A.refreshStatus();
    }, "#trainAlert");
  });
  function syncBaseModelRow() {
    var row = A.el("baseModelRow");
    if (!row) return;
    var fine = str('l_training_mode') === 'finetune';
    row.hidden = !fine;
    A.el('trainingModeHint').textContent = fine ? '加载已有权重，用所选数据集继续学习；新建优化器和输出目录，原模型保留。' : '从零初始化策略，训练一个新模型；不加载下方保存的模型路径。';
    A.el('trainingStepsLabel').textContent = fine ? '本次微调步数' : '训练步数';
    A.el('btnTrain').textContent = selected === 'lerobot' && fine ? '开始微调' : '开始训练';
    A.el('l_learning_rate').disabled = str('l_policy_type') !== 'act';
  }
  A.change("l_policy_type", function () {
    syncBaseModelRow();
    var v = A.el("l_policy_type").value;
    if (v === "smolvla") {
      A.el('l_training_mode').value='finetune';syncBaseModelRow();
      if (!A.el("l_base_model").value.trim()) {
        A.el("l_base_model").value = "lerobot/smolvla_base";
      }
      // SmolVLA 微调常用：2 万步、batch 8；只有还是 ACT 默认值时才改，避免覆盖用户设置
      if (Number(A.el("l_steps").value) === 300000) A.el("l_steps").value = 20000;
      if (Number(A.el("l_batch_size").value) === 0) A.el("l_batch_size").value = 8;
    }
    updateDep();
    populateCheckpoints();invalidateInspection();
    updateModelCacheStatus();
  });
  A.change("l_base_model", function () { updateDep(); populateCheckpoints(); invalidateInspection(); updateModelCacheStatus(); });
  A.change('l_training_mode',function(){updateDep();updateModelCacheStatus();invalidateInspection();});
  A.change('l_checkpoint',function(){if(str('l_checkpoint'))selectCheckpoint(str('l_checkpoint'));});
  A.click('btnInspectFinetune',function(){A.action(inspectFinetune,'#trainAlert');});
  A.change("l_repo_id", function () {
    invalidateInspection();
    A.action(function () { return syncDatasetDirFromRepo(true); }, "#trainAlert");
  });

  A.click("btnResume", function () {
    A.action(async function () {
      A.clearAlert("#trainAlert");
      var list = await loadCheckpoints();
      var def = "";
      for (var i = 0; i < list.length; i++) { if (list[i].config) { def = list[i].config; break; } }
      var p = await A.prompt("断点续训会恢复原任务的步数、优化器及数据集。新增数据请选择‘在已有模型上微调’。\n请输入原任务 train_config.json 路径：", def,
        { title: "断点续训原任务" });
      if (!p) return;
      await A.api.post("/api/train/resume", { config_path: p });
      A.ok("恢复训练已启动", "#trainAlert");
      A.refreshStatus();
    }, "#trainAlert");
  });
  A.click("btnUseDs", function () {
    var v = A.el("dsPicker").value;
    if (!v) { A.toast("暂无已录制数据集", "warn"); return; }
    A.el("y_dataset_dir").value = v;
    A.el("o_dataset_dir").value = v;
    // 同时填到 LeRobot 的「数据集目录」，避免两个目录概念混淆
    A.el("l_dataset_dir").value = v;
    invalidateInspection();
    A.el("dsDirInfo").textContent = "当前目录：" + v;
    A.saveConfig({ train: { dataset_dir: v, repo_id: v.replace(/\/+$/, "").split("/").pop() } })
      .then(function () { updateDep(); })
      .catch(function () {});
    A.toast("已填入目录：" + v, "ok");
  });
  A.click("btnFillDs", function () {
    A.action(function () { return syncDatasetDirFromRepo(true); }, "#trainAlert");
  });
  A.click("btnClearLoss", function () { series.clear();draw(); });
  A.click("btnReloadCkpt", function () { loadCheckpoints(); });
  A.click('btnRefreshModels',function(){A.action(loadCheckpoints,'#trainAlert');});

  A.onStatus(function (s) {
    if (s) A.el("trainState").textContent = s.running
      ? ("运行中：" + (s.job ? s.job.name : "")) : "空闲";
  });
  A.onTheme(function () { draw(); });
  window.addEventListener("resize", draw);
  A.onLog(function (ev) {
    if(series.add(ev))draw();
  });
  A.logs().forEach(function (ev) {
    series.add(ev);
  });
  draw();

  await A.action(loadConfig, "#trainAlert");
  await loadBackends(true);
  await loadDatasets();
  await loadCheckpoints();

  function applyTraining(c) {
    var previous=JSON.stringify([str('l_base_model'),str('l_policy_type'),str('l_repo_id'),str('l_dataset_dir')]);
    var maps={train:{training_mode:'l_training_mode',learning_rate:'l_learning_rate',repo_id:'l_repo_id',policy_type:'l_policy_type',dataset_dir:'l_dataset_dir',base_model:'l_base_model',steps:'l_steps',batch_size:'l_batch_size',device:'l_device',output_dir:'l_output_dir',job_name:'l_job_name',wandb:'l_wandb',video_backend:'l_video_backend'},
      yolo:{data_yaml:'y_data_yaml',dataset_dir:'y_dataset_dir',model:'y_model',epochs:'y_epochs',imgsz:'y_imgsz',batch:'y_batch',device:'y_device',output_dir:'y_output_dir'},
      opencv:{dataset_dir:'o_dataset_dir',classifier:'o_classifier',epochs:'o_epochs',img_size:'o_img_size',output_dir:'o_output_dir'}};
    Object.keys(maps).forEach(function(section){var values=c[section]||{};Object.keys(maps[section]).forEach(function(key){
      if(values[key]===undefined)return;var n=A.el(maps[section][key]);
      if(n.type==='checkbox')n.checked=!!values[key];
      else if(n.tagName!=='SELECT'||Array.from(n.options).some(function(o){return o.value===String(values[key]);})){if(n.value!==String(values[key]))n.value=values[key];}
    });});
    if(c.train&&['lerobot','yolo','opencv'].indexOf(c.train.backend)>=0)selectEngine(c.train.backend);
    if(c.train&&!c.train.training_mode)A.el('l_training_mode').value=c.train.base_model?'finetune':'scratch';
    syncBaseModelRow();populateCheckpoints();
    if(previous!==JSON.stringify([str('l_base_model'),str('l_policy_type'),str('l_repo_id'),str('l_dataset_dir')]))invalidateInspection();
  }
  trainAutosave=A.autosave.bind({id:'training',root:'#page',
    read:function(){return {train:Object.assign({backend:selected},buildParams('lerobot')),yolo:buildParams('yolo'),opencv:buildParams('opencv')};},
    apply:applyTraining,
    validate:function(){A.qa('#params-'+selected+' input[type=number]').forEach(function(n){if(!n.checkValidity()||n.value==='')throw new Error('请填写有效的训练数值；默认批大小可填 0');});}
  });
  A.autosave.onConfig(function(){A.commandPreview('train','#trainCommandPreview');});
  A.el('hfMode').closest('.card').id='hfSettings';
  A.autosave.bind({id:'hf-source',section:'hf',root:'#hfSettings',read:function(){return {hf:{endpoint:hfEndpointValue(),auto:A.el('hfMode').value==='auto'}};},apply:loadHf});
  A.onLog(function(e){if(e.type==='state'&&e.running===false&&(!e.ts||e.ts>=Date.now()/1000-5))loadCheckpoints();});
})();
