/* 视觉数据：YOLO 数据集导入 / 采集 / 标注 / 训练 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "yolo", title: "视觉数据", subtitle: "YOLO 数据导入、标注与训练" });
  A.attachLogPanel(A.el("logPanel"));

  var state = {
    datasets: [], ds: null, images: [], split: A.load("yolo.split", "train") === "val" ? "val" : "train",
    current: "", img: null, boxes: [], sel: -1, prefCls: 0
  };
  var dragMode = null, drag = null;

  function apiBase() { return "/api/yolo/datasets/" + encodeURIComponent(state.ds.name); }

  function collectYolo() {
    return {
      model: A.el("yModel").value.trim() || "yolov8n.pt",
      epochs: parseInt(A.el("yEpochs").value, 10) || 50,
      imgsz: parseInt(A.el("yImgsz").value, 10) || 640,
      batch: parseInt(A.el("yBatch").value, 10) || 8,
      device: A.el("yDevice").value.trim() || "cuda",
      camera_index: parseInt(A.el("camIndex").value, 10) || 0
    };
  }
  var yoloAutosave=null;
  function saveYolo(now){if(yoloAutosave){yoloAutosave.changed();if(now)return yoloAutosave.flush();}}
  async function loadConfig() {
    try {
      var c = await A.api.get("/api/config");
      var y = c.yolo || {};
      if (y.model) A.el("yModel").value = y.model;
      if (y.epochs) A.el("yEpochs").value = y.epochs;
      if (y.imgsz) A.el("yImgsz").value = y.imgsz;
      if (y.batch) A.el("yBatch").value = y.batch;
      if (y.device) A.el("yDevice").value = y.device;
      var cam = (y.camera_index !== undefined && y.camera_index !== null)
        ? y.camera_index : A.load("yolo.cam", 0);
      A.el("camIndex").value = cam;
      state.prefCls = parseInt(A.load("yolo.cls", 0), 10) || 0;
    } catch (e) { /* 保留默认值 */ }
  }

  function renderSplitOptions() {
    var sel = A.el("splitSel");
    if (!state.ds) {
      sel.innerHTML = '<option value="train">训练集 train</option>' +
        '<option value="val">验证集 val</option>';
      sel.value = state.split;
      return;
    }
    sel.innerHTML =
      '<option value="train">训练集 train（' + state.ds.images_train + '）</option>' +
      '<option value="val">验证集 val（' + state.ds.images_val + '）</option>';
    sel.value = state.split;
  }

  function renderStats() {
    var d = state.ds, el = A.el("dsStats");
    if (!d) { el.textContent = "还没有数据集，先新建或导入一个。"; return; }
    el.textContent = d.name + " · 训练 " + d.images_train + " 张 / 验证 " + d.images_val +
      " 张 · 已标注 " + d.labels_train + "（train）/ " + d.labels_val + "（val）· 类别：" +
      (d.classes.length ? d.classes.join("、") : "未设置") +
      (d.classes.length < 2 ? "（要标多种物体，先点「编辑类别」添加多个类别）" : "");
  }

  function renderClasses() {
    var sel = A.el("clsSel");
    var classes = state.ds ? state.ds.classes : [];
    if (!classes.length) { sel.innerHTML = '<option value="0">class 0</option>'; return; }
    sel.innerHTML = classes.map(function (n, i) {
      return '<option value="' + i + '">' + A.esc(n) + "</option>";
    }).join("");
    if (state.prefCls >= 0 && state.prefCls < classes.length) {
      sel.value = String(state.prefCls);
    }
  }

  function renderImageList() {
    var box = A.el("imgList");
    if (!state.ds) { A.loadEmpty(box, "还没有数据集", "先新建或导入"); return; }
    if (!state.images.length) {
      A.loadEmpty(box, "训练集里还没有图片", "导入 zip 或点「采集一张」", function () { loadImages(); });
      return;
    }
    box.innerHTML = state.images.map(function (it) {
      var cls = "yolo-item" + (it.file === state.current ? " active" : "");
      return '<button type="button" class="' + cls + '" data-img="' + A.esc(it.file) + '">' +
        (it.labeled ? "● " : "○ ") + A.esc(it.file) + "</button>";
    }).join("");
  }

  async function loadDatasets(keepName) {
    await autoSave(false);
    try {
      var r = await A.api.get("/api/yolo/datasets");
      state.datasets = r.datasets || [];
      var sel = A.el("dsSelect");
      sel.innerHTML = state.datasets.map(function (d) {
        return '<option value="' + A.esc(d.name) + '">' + A.esc(d.name) + " · " +
          d.images_train + " 张</option>";
      }).join("");
      var want = keepName || A.load("yolo.ds", "") ||
        (state.ds && state.ds.name) || (state.datasets[0] && state.datasets[0].name);
      var hit = state.datasets.filter(function (d) { return d.name === want; })[0];
      state.ds = hit || state.datasets[0] || null;
      if (state.ds) { sel.value = state.ds.name; A.store("yolo.ds", state.ds.name); }
      renderStats(); renderClasses(); renderSplitOptions();
      await loadImages();
    } catch (e) {
      A.setAlert("#yoloAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  async function loadImages(keepCurrent) {
    var prev = keepCurrent ? state.current : "";
    await autoSave(false);
    dirty = false;
    state.current = ""; state.img = null; state.boxes = []; state.sel = -1;
    A.el("annotHint").hidden = false;
    A.el("annotHint").textContent = "从左侧选择一张图片开始标注";
    A.el("selInfo").textContent = "未选中";
    A.el("labelText").value = "";
    var cv = A.el("annotCanvas");
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (!state.ds) { renderImageList(); return; }
    try {
      var r = await A.api.get(apiBase() + "/images?split=" + state.split);
      state.images = r.images || [];
    } catch (e) {
      state.images = [];
      A.setAlert("#yoloAlert", e.message, "err");
    }
    renderImageList();
    if (prev && state.images.some(function (it) { return it.file === prev; })) {
      openImage(prev);
    }
  }

  async function refreshCurrentDataset() {
    try {
      var r = await A.api.get("/api/yolo/datasets");
      state.datasets = r.datasets || [];
      var hit = state.datasets.filter(function (d) {
        return state.ds && d.name === state.ds.name;
      })[0];
      if (hit) state.ds = hit;
      if (state.ds) A.el("dsSelect").value = state.ds.name;
      renderStats();
      renderClasses();
      renderSplitOptions();
      draw();
    } catch (e) {
      A.setAlert("#yoloAlert", e.message, "err");
    }
  }

  /* 画布坐标 -> 图片像素坐标（画布 buffer 就是图片原始尺寸，CSS 只做缩放） */
  function canvasPoint(e) {
    var cv = A.el("annotCanvas");
    var rect = cv.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) * (cv.width / Math.max(rect.width, 1)),
      y: (e.clientY - rect.top) * (cv.height / Math.max(rect.height, 1))
    };
  }

  function draw() {
    var cv = A.el("annotCanvas"), ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (!state.img) return;
    ctx.drawImage(state.img, 0, 0);
    var classes = state.ds && state.ds.classes.length ? state.ds.classes : ["class 0"];
    var lw = Math.max(2, Math.round(cv.width / 400));
    var fs = Math.max(14, Math.round(cv.width / 55));
    state.boxes.forEach(function (b, i) {
      var x1 = Math.min(b.x1, b.x2), x2 = Math.max(b.x1, b.x2);
      var y1 = Math.min(b.y1, b.y2), y2 = Math.max(b.y1, b.y2);
      ctx.lineWidth = i === state.sel ? lw + 2 : lw;
      ctx.strokeStyle = i === state.sel ? "#ff7a00" : "#22c55e";
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      var label = classes[b.cid] || ("class " + b.cid);
      ctx.font = fs + "px sans-serif";
      var tw = ctx.measureText(label).width + fs * 0.7;
      var ty = Math.max(0, y1 - fs * 1.4);
      ctx.fillStyle = "rgba(0,0,0,.65)";
      ctx.fillRect(x1, ty, tw, fs * 1.4);
      ctx.fillStyle = "#fff";
      ctx.fillText(label, x1 + fs * 0.35, ty + fs * 1.05);
    });
    if (state.sel >= 0 && state.boxes[state.sel]) {
      var hp = boxHandles(state.boxes[state.sel]);
      var hs = Math.max(6, Math.round(cv.width / 120));
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "#ff7a00";
      ctx.lineWidth = 2;
      for (var hk in hp) {
        ctx.fillRect(hp[hk][0] - hs / 2, hp[hk][1] - hs / 2, hs, hs);
        ctx.strokeRect(hp[hk][0] - hs / 2, hp[hk][1] - hs / 2, hs, hs);
      }
    }
    if (dragMode === "new" && drag) {
      var d = drag;
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = "#3b82f6";
      ctx.lineWidth = lw;
      ctx.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1),
        Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
      ctx.setLineDash([]);
    }
  }

  function classLabel(cid) {
    var classes = state.ds && state.ds.classes.length ? state.ds.classes : ["class 0"];
    return classes[cid] || ("class " + cid);
  }

  function selectBox(i) {
    state.sel = i;
    var info = A.el("selInfo");
    if (i >= 0 && state.boxes[i]) {
      info.textContent = "第 " + (i + 1) + " 个框 · " + classLabel(state.boxes[i].cid) +
        "（改下拉可改类别；拖动框可移动，拖四角/边可缩放）";
    } else {
      info.textContent = "未选中";
    }
    draw();
  }

  function boxHandles(b) {
    var x1 = Math.min(b.x1, b.x2), x2 = Math.max(b.x1, b.x2);
    var y1 = Math.min(b.y1, b.y2), y2 = Math.max(b.y1, b.y2);
    var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    return {
      nw: [x1, y1], n: [mx, y1], ne: [x2, y1], e: [x2, my],
      se: [x2, y2], s: [mx, y2], sw: [x1, y2], w: [x1, my]
    };
  }

  function handleAt(x, y) {
    if (state.sel < 0 || !state.boxes[state.sel]) return "";
    var hp = boxHandles(state.boxes[state.sel]);
    var tol = Math.max(5, Math.round(A.el("annotCanvas").width / 130));
    for (var k in hp) {
      if (Math.abs(x - hp[k][0]) <= tol && Math.abs(y - hp[k][1]) <= tol) return k;
    }
    return "";
  }

  function hitBox(x, y) {
    var cv = A.el("annotCanvas");
    var fs = Math.max(14, Math.round(cv.width / 55));
    var tagH = fs * 1.4;
    var tagW = Math.max(60, fs * 8);
    var pad = Math.max(2, Math.round(cv.width / 500));
    for (var i = state.boxes.length - 1; i >= 0; i--) {
      var b = state.boxes[i];
      var x1 = Math.min(b.x1, b.x2), x2 = Math.max(b.x1, b.x2);
      var y1 = Math.min(b.y1, b.y2), y2 = Math.max(b.y1, b.y2);
      // 框上方的类别标签也能点中，避免“点框上方选不到”
      if (x >= x1 && x <= x1 + tagW && y >= y1 - tagH - pad && y <= y1) return i;
      // 框内 + 一点点外扩，边缘也容易选中
      if (x >= x1 - pad && x <= x2 + pad && y >= y1 - pad && y <= y2 + pad) return i;
    }
    return -1;
  }

  function textToBoxes(text) {
    var W = state.img ? state.img.naturalWidth : 1;
    var H = state.img ? state.img.naturalHeight : 1;
    var classes = state.ds ? state.ds.classes : [];
    var out = [], errors = [];
    String(text || "").split("\n").forEach(function (ln, idx) {
      var t = ln.trim();
      if (!t) return;
      var p = t.split(/\s+/);
      if (p.length !== 5) { errors.push("第 " + (idx + 1) + " 行需要 5 个字段：class cx cy w h"); return; }
      var cid = parseInt(p[0], 10);
      var vals = p.slice(1).map(function (x) { return parseFloat(x); });
      if (isNaN(cid) || vals.some(function (v) { return isNaN(v); })) {
        errors.push("第 " + (idx + 1) + " 行有非数字"); return;
      }
      if (vals.some(function (v) { return v < 0 || v > 1; })) {
        errors.push("第 " + (idx + 1) + " 行坐标必须在 0~1"); return;
      }
      if (classes.length && (cid < 0 || cid >= classes.length)) {
        errors.push("第 " + (idx + 1) + " 行类别 id 超出范围（0.." + (classes.length - 1) + "）"); return;
      }
      var cx = vals[0], cy = vals[1], bw = vals[2], bh = vals[3];
      out.push({
        cid: cid,
        x1: (cx - bw / 2) * W, y1: (cy - bh / 2) * H,
        x2: (cx + bw / 2) * W, y2: (cy + bh / 2) * H
      });
    });
    return { boxes: out, errors: errors };
  }

  function boxesToText() {
    var W = state.img ? state.img.naturalWidth : 1;
    var H = state.img ? state.img.naturalHeight : 1;
    return state.boxes.map(function (b) {
      var x1 = Math.min(b.x1, b.x2), x2 = Math.max(b.x1, b.x2);
      var y1 = Math.min(b.y1, b.y2), y2 = Math.max(b.y1, b.y2);
      var cx = ((x1 + x2) / 2) / W, cy = ((y1 + y2) / 2) / H;
      var bw = (x2 - x1) / W, bh = (y2 - y1) / H;
      return b.cid + " " + cx.toFixed(6) + " " + cy.toFixed(6) + " " +
        bw.toFixed(6) + " " + bh.toFixed(6);
    }).join("\n");
  }

  function syncText() { A.el("labelText").value = boxesToText(); }

  var autoTimer=null,dirty=false,saving=null;
  function setSaveState(t){A.el('saveState').textContent=t;}
  function annotationStatus(error){A.autosave.track('yolo-label',{dirty:dirty,saving:!!saving,error:error||'',flush:function(){return autoSave(false);}});}
  function draftKey(){return 'rebot.label.'+(state.ds?state.ds.name:'')+'.'+state.split+'.'+state.current;}
  function keepLabelDraft(){try{sessionStorage.setItem(draftKey(),A.el('labelText').value);}catch(e){}}
  function markDirty(){if(!state.img||!state.current)return;keepLabelDraft();dirty=true;setSaveState('正在自动保存…');annotationStatus();clearTimeout(autoTimer);autoTimer=setTimeout(function(){autoSave(false).catch(function(){});},600);}
  async function autoSave(force){
    if(saving){await saving;if(dirty||force)return autoSave(force);return;}
    if(!state.img||!state.current||(!force&&!dirty))return;
    var file=state.current,split=state.split,text=A.el('labelText').value,base=apiBase(),draft=draftKey();
    keepLabelDraft();
    var parsed=textToBoxes(text);if(parsed.errors.length){dirty=true;var invalid=new Error(parsed.errors.join('；'));annotationStatus(invalid.message);throw invalid;}
    saving=A.api.put(base+'/label',{split:split,file:file,text:text});annotationStatus();
    try{await saving;dirty=state.current===file&&A.el('labelText').value!==text;
      try{if(sessionStorage.getItem(draft)===text)sessionStorage.removeItem(draft);}catch(e){}
      setSaveState(dirty?'正在保存更新后的标注…':'已自动保存 · '+new Date().toLocaleTimeString());
      state.images.forEach(function(it){if(it.file===file)it.labeled=!!text.trim();});renderImageList();
    }catch(e){dirty=true;setSaveState('保存失败，标注已保留：'+e.message);annotationStatus(e.message);throw e;}
    finally{saving=null;annotationStatus(dirty?'存在尚未保存的标注':'');}
    if(dirty)return autoSave(false);
  }

  var imageRequest=0;
  async function openImage(file) {
    if (!state.ds) return;
    await autoSave(false);
    clearTimeout(autoTimer);
    dirty = false;
    state.current = file;
    state.img = null;
    var requestId=++imageRequest;
    A.el("annotHint").hidden=false;A.el("annotHint").textContent="正在载入图片与标注…";
    state.sel = -1;
    renderImageList();
    var img = new Image();
    img.onload = async function () {
      if(requestId!==imageRequest)return;
      state.img = img;
      var cv = A.el("annotCanvas");
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      A.el("annotHint").hidden = true;
      var text = "";
      try {
        var r = await A.api.get(apiBase() + "/label?split=" + state.split +
          "&file=" + encodeURIComponent(file));
        text = r.text || "";
      } catch (e) { /* 没标签就是空 */ }
      if(requestId!==imageRequest)return;
      var retained=null;try{retained=sessionStorage.getItem(draftKey());}catch(e){}
      if(retained!==null)text=retained;
      A.el("labelText").value = text.replace(/\s+$/, "");
      state.boxes = textToBoxes(text).boxes;
      draw();if(retained!==null)markDirty();
    };
    img.onerror = function () { A.toast("图片加载失败：" + file, "err"); };
    img.src = apiBase() + "/image?split=" + state.split + "&file=" + encodeURIComponent(file) +
      "&t=" + Date.now();
  }

  var cv = A.el("annotCanvas");
  cv.addEventListener("mousedown", function (e) {
    if (!state.img) return;
    e.preventDefault();
    var p = canvasPoint(e);
    var h = handleAt(p.x, p.y);
    if (h) {
      dragMode = "resize";
      drag = { handle: h, box: Object.assign({}, state.boxes[state.sel]) };
      return;
    }
    var hit = hitBox(p.x, p.y);
    if (hit >= 0) {
      selectBox(hit);
      dragMode = "move";
      drag = { startX: p.x, startY: p.y, box: Object.assign({}, state.boxes[hit]) };
      return;
    }
    state.sel = -1;
    dragMode = "new";
    drag = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  });
  cv.addEventListener("mousemove", function (e) {
    if (!dragMode) return;
    var p = canvasPoint(e);
    if (dragMode === "new") {
      drag.x1 = p.x;
      drag.y1 = p.y;
    } else if (dragMode === "move") {
      var b = state.boxes[state.sel];
      var dx = p.x - drag.startX, dy = p.y - drag.startY;
      b.x1 = drag.box.x1 + dx; b.y1 = drag.box.y1 + dy;
      b.x2 = drag.box.x2 + dx; b.y2 = drag.box.y2 + dy;
    } else if (dragMode === "resize") {
      var b = state.boxes[state.sel];
      var x1 = drag.box.x1, y1 = drag.box.y1, x2 = drag.box.x2, y2 = drag.box.y2;
      if (drag.handle.indexOf("n") >= 0) y1 = p.y;
      if (drag.handle.indexOf("s") >= 0) y2 = p.y;
      if (drag.handle.indexOf("w") >= 0) x1 = p.x;
      if (drag.handle.indexOf("e") >= 0) x2 = p.x;
      if (Math.abs(x2 - x1) < 4) { if (drag.handle.indexOf("w") >= 0) x1 = x2 - 4; else x2 = x1 + 4; }
      if (Math.abs(y2 - y1) < 4) { if (drag.handle.indexOf("n") >= 0) y1 = y2 - 4; else y2 = y1 + 4; }
      b.x1 = x1; b.y1 = y1; b.x2 = x2; b.y2 = y2;
    }
    draw();
  });
  window.addEventListener("mouseup", function () {
    if (!dragMode) return;
    if (dragMode === "new") {
      var d = drag;
      if (Math.abs(d.x1 - d.x0) < 4 || Math.abs(d.y1 - d.y0) < 4) {
        dragMode = null; drag = null; draw(); return;
      }
      var cid = parseInt(A.el("clsSel").value, 10) || 0;
      state.boxes.push({
        cid: cid,
        x1: Math.min(d.x0, d.x1), y1: Math.min(d.y0, d.y1),
        x2: Math.max(d.x0, d.x1), y2: Math.max(d.y0, d.y1)
      });
      syncText();
      state.sel = -1;
      A.el("selInfo").textContent = "未选中（点击框可选中后改类别/拖动/缩放）";
    } else {
      syncText();
    }
    markDirty();
    dragMode = null;
    drag = null;
    draw();
  });
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Delete" || e.key === "Backspace") && state.sel >= 0) {
      if (document.activeElement && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
      e.preventDefault();
      state.boxes.splice(state.sel, 1);
      state.sel = -1;
      syncText();
      draw();
    }
  });

  A.click("btnDelImage", function () {
    var file = state.current;
    if (!file) { A.toast("先选择一张图片", "warn"); return; }
    A.action(async function () {
      if (!(await A.confirm("确认删除图片 " + file + "（连同它的标注）？", { title: "删除图片" }))) return;
      clearTimeout(autoTimer);
      dirty = false;
      await A.api.del(apiBase() + "/image?split=" + state.split +
        "&file=" + encodeURIComponent(file));
      state.current = ""; state.img = null; state.boxes = []; state.sel = -1;
      A.el("labelText").value = "";
      A.el("selInfo").textContent = "未选中";
      A.el("annotHint").hidden = false;
      A.el("annotHint").textContent = "已删除 " + file + "，从左侧选择其它图片";
      var cv = A.el("annotCanvas");
      cv.getContext("2d").clearRect(0, 0, cv.width, cv.height);
      await loadImages();
      await refreshCurrentDataset();
      A.ok("已删除图片：" + file, "#yoloAlert");
    }, "#yoloAlert");
  });

  A.click("btnEditClasses", function () {
    A.action(async function () {
      if (!state.ds) throw new Error("先选择数据集");
      var v = await A.prompt("类别名（逗号分隔；改名会影响该类别下的所有框）：",
        state.ds.classes.join(", "), { title: "编辑类别" });
      if (v === null) return;
      var classes = v.split(/[,，]/).map(function (x) { return x.trim(); }).filter(Boolean);
      if (!classes.length) throw new Error("至少保留一个类别");
      await A.api.put(apiBase() + "/classes", { classes: classes });
      A.ok("类别已更新：" + classes.join("、"), "#yoloAlert");
      await refreshCurrentDataset();
    }, "#yoloAlert");
  });

  async function openClassManager() {
    if (!state.ds) { A.toast("先选择数据集", "warn"); return; }
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    function rows() {
      return state.ds.classes.map(function (n, i) {
        return '<div class="row" style="margin-top:8px;align-items:center">' +
          '<div class="field narrow"><label>id ' + i + '</label><div>' + A.esc(n) + '</div></div>' +
          '<div class="btn-row">' +
          '<button type="button" class="btn subtle sm" data-rename="' + i + '">改名</button>' +
          '<button type="button" class="btn danger sm" data-del="' + i + '">删除</button>' +
          '</div></div>';
      }).join("");
    }
    function paint() {
      mask.innerHTML =
        '<div class="notify-modal" style="width:min(560px,100%)" role="dialog" aria-modal="true">' +
          '<div class="notify-head"><span class="notify-title">类别管理（增删改）</span></div>' +
          '<div class="notify-msg">' +
            (state.ds.classes.length ? rows() : '<div class="hint">还没有类别</div>') +
            '<div class="row" style="margin-top:14px;align-items:end">' +
              '<div class="field wide"><label>新增类别名</label><input id="clsNew"></div>' +
              '<div class="btn-row"><button type="button" class="btn primary sm" id="clsAdd">添加</button></div>' +
            '</div>' +
            '<div class="hint" style="margin-top:8px">删除类别会同时删除所有该类别的框，并把后面的 id 前移；改名不影响已有框。</div>' +
          '</div>' +
          '<div class="notify-foot"><button type="button" class="btn subtle" id="clsClose">关闭</button></div>' +
        '</div>';
      mask.querySelector("#clsClose").addEventListener("click", close);
      mask.querySelector("#clsAdd").addEventListener("click", async function () {
        var v = mask.querySelector("#clsNew").value.trim();
        if (!v) return;
        try {
          var r = await A.api.post(apiBase() + "/classes/add", { name: v });
          state.ds = r.dataset;
          A.el("clsSel").innerHTML = state.ds.classes.map(function (n, i) {
            return '<option value="' + i + '">' + A.esc(n) + "</option>";
          }).join("");
          A.ok("已添加类别：" + v, "#yoloAlert");
          paint();
        } catch (e) { A.toast(e.message, "err"); }
      });
      mask.querySelectorAll("[data-rename]").forEach(function (b) {
        b.addEventListener("click", async function () {
          var i = parseInt(b.getAttribute("data-rename"), 10);
          var v = await A.prompt("新类别名：", state.ds.classes[i], { title: "改类别名" });
          if (v === null) return;
          v = v.trim();
          if (!v) return;
          try {
            var r = await A.api.put(apiBase() + "/classes/" + i, { name: v });
            state.ds = r.dataset;
            renderClasses();
            A.ok("类别已改名：" + v, "#yoloAlert");
            paint();
          } catch (e) { A.toast(e.message, "err"); }
        });
      });
      mask.querySelectorAll("[data-del]").forEach(function (b) {
        b.addEventListener("click", async function () {
          var i = parseInt(b.getAttribute("data-del"), 10);
          var nm = state.ds.classes[i];
          if (!(await A.confirm("删除类别「" + nm + "」？\n会同时删除所有该类别的框，并把后面的 id 前移。",
            { title: "删除类别" }))) return;
          try {
            var r = await A.api.del(apiBase() + "/classes/" + i + "?confirm=true");
            state.ds = r.dataset;
            renderClasses();
            A.ok("已删除类别：" + nm, "#yoloAlert");
            paint();
            if (state.current) openImage(state.current);
          } catch (e) { A.toast(e.message, "err"); }
        });
      });
    }
    function close() { if (mask.parentNode) mask.parentNode.removeChild(mask); }
    A.modalHost().appendChild(mask);
    mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
    paint();
  }

  A.click("btnClassMgr", openClassManager);
  A.click("btnAddClass", openClassManager);

  A.el("imgList").addEventListener("click", function (e) {
    var b = e.target.closest("[data-img]");
    if (b) openImage(b.getAttribute("data-img"));
  });

  A.click("btnPrev", function () {
    var i = state.images.findIndex(function (x) { return x.file === state.current; });
    if (i > 0) openImage(state.images[i - 1].file);
  });
  A.click("btnNext", function () {
    var i = state.images.findIndex(function (x) { return x.file === state.current; });
    if (i >= 0 && i < state.images.length - 1) openImage(state.images[i + 1].file);
  });
  A.click("btnDelBox", function () {
    if (state.sel < 0) { A.toast("先点击选中一个框", "warn"); return; }
    state.boxes.splice(state.sel, 1);
    state.sel = -1;
    syncText();
    draw();
    A.el("selInfo").textContent = "未选中";
    markDirty();
  });
  A.click("btnApplyClass", function () {
    if (!state.img) { A.toast("先选择一张图片", "warn"); return; }
    if (state.sel < 0) { A.toast("先点击选中要改类别的框", "warn"); return; }
    state.boxes[state.sel].cid = parseInt(A.el("clsSel").value, 10) || 0;
    syncText();
    selectBox(state.sel);
    markDirty();
    A.el("saveState").textContent = "已改类别：" + classLabel(state.boxes[state.sel].cid);
  });
  A.click("btnApplyText", function () {
    if (!state.img) { A.toast("先选择一张图片", "warn"); return; }
    var r = textToBoxes(A.el("labelText").value);
    if (r.errors.length) {
      A.setAlert("#yoloAlert", "标注文本有误：\n" + r.errors.join("\n"), "err");
      return;
    }
    state.boxes = r.boxes;
    state.sel = -1;
    syncText();
    draw();
    markDirty();
    A.ok("已应用手动标注：" + r.boxes.length + " 个框", "#yoloAlert");
  });
  A.click("btnSaveLabel", function () {
    A.action(async function () {
      if (!state.ds || !state.current) throw new Error("先选择一张图片");
      var parsed = textToBoxes(A.el("labelText").value);
      if (parsed.errors.length) {
        throw new Error("标注文本有误：\n" + parsed.errors.join("\n"));
      }
      await autoSave(true);
      A.ok("标注已保存：" + state.current, "#yoloAlert");
    }, "#yoloAlert");
  });

  A.click("btnDsCreate", function () {
    A.action(async function () {
      var name = A.el("newName").value.trim();
      var classes = A.el("newClasses").value.split(/[,，]/).map(function (s) { return s.trim(); })
        .filter(Boolean);
      if (!name) throw new Error("请输入数据集名");
      var r = await A.api.post("/api/yolo/datasets", { name: name, classes: classes });
      A.ok("已创建数据集 " + r.dataset.name, "#yoloAlert");
      A.el("newName").value = ""; A.el("newClasses").value = "";
      await loadDatasets(r.dataset.name);
    }, "#yoloAlert");
  });
  A.click("btnDsDelete", async function () {
    if (!state.ds) { A.toast("还没有数据集", "warn"); return; }
    if (!(await A.confirm("确认删除数据集 " + state.ds.name + "？所有图片和标注都会删除。",
      { title: "删除数据集" }))) return;
    A.action(async function () {
      await A.api.del(apiBase());
      A.ok("已删除数据集", "#yoloAlert");
      state.ds = null;
      await loadDatasets();
    }, "#yoloAlert");
  });
  A.click("btnDsRefresh", function () { loadDatasets(); });
  A.el('splitSel').addEventListener('change',function(){var next=this.value==='val'?'val':'train';A.action(async function(){try{await autoSave(false);state.split=next;A.store('yolo.split',next);await loadImages();}catch(e){A.el('splitSel').value=state.split;throw e;}},'#yoloAlert');});
  A.el('dsSelect').addEventListener('change',function(){var next=this.value;A.action(async function(){try{await autoSave(false);state.ds=state.datasets.find(function(d){return d.name===next;})||null;A.store('yolo.ds',next);renderStats();renderClasses();renderSplitOptions();await loadImages();}catch(e){if(state.ds)A.el('dsSelect').value=state.ds.name;throw e;}},'#yoloAlert');});
  A.el("clsSel").addEventListener("change", function () {
    state.prefCls = parseInt(this.value, 10) || 0;
    A.store("yolo.cls", state.prefCls);
    if (state.img && state.sel >= 0 && state.boxes[state.sel]) {
      state.boxes[state.sel].cid = state.prefCls;
      syncText();
      selectBox(state.sel);
      markDirty();
      A.el("saveState").textContent = "已改类别：" + classLabel(state.prefCls);
    }
  });
  ["camIndex", "yModel", "yEpochs", "yImgsz", "yBatch", "yDevice"].forEach(function (id) {
    var el = A.el(id);
    if (!el) return;
    el.addEventListener("change", function () {
      if (id === "camIndex") A.store("yolo.cam", parseInt(el.value, 10) || 0);
      saveYolo();
    });
  });

  A.click("btnImport", function () {
    A.action(async function () {
      if (!state.ds) throw new Error("先选择或新建一个数据集");
      var f = A.el("zipFile").files[0];
      if (!f) throw new Error("请选择一个 zip 文件");
      var fd = new FormData();
      fd.append("file", f);
      var r = await fetch(apiBase() + "/import", { method: "POST", body: fd });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(j.detail || j.message || ("导入失败（HTTP " + r.status + "）"));
      A.ok("导入完成：" + j.images + " 张图片 / " + j.labels + " 个标签", "#yoloAlert");
      A.el("zipFile").value = "";
      await refreshCurrentDataset();
      await loadImages(true);
    }, "#yoloAlert");
  });

  A.click("btnCapture", function () {
    if (!state.ds) { A.toast("先选择或新建一个数据集", "warn"); return; }
    var idx = parseInt(A.el("camIndex").value, 10) || 0;
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="notify-modal" style="width:min(780px,100%)" role="dialog" aria-modal="true">' +
        '<div class="notify-head"><span class="notify-title">采集预览 · 摄像头 ' + idx + '</span></div>' +
        '<div style="padding:10px 18px">' +
          '<img id="capPreview" alt="摄像头预览" ' +
          'style="width:100%;border-radius:8px;background:#111;min-height:180px" /></div>' +
        '<div class="notify-foot">' +
          '<button type="button" class="btn subtle" data-cancel>取消</button>' +
          '<button type="button" class="btn primary" data-ok disabled>确定并保存</button>' +
        '</div>' +
      '</div>';
    var img = mask.querySelector("#capPreview");
    var okBtn = mask.querySelector("[data-ok]");
    var hint = mask.querySelector(".notify-title");
    var candidates = [idx, 0, 1, 2, 3].filter(function (v, i, a) { return a.indexOf(v) === i; });
    var attempt = 0;
    function close() {
      img.src = "";
      if (mask.parentNode) mask.parentNode.removeChild(mask);
    }
    function loadStream() {
      hint.textContent = "采集预览 · 摄像头 " + candidates[attempt];
      img.src = "/api/camera/stream?index=" + candidates[attempt] + "&t=" + Date.now();
    }
    img.onload = function () {
      okBtn.disabled = false;
      if (candidates[attempt] !== idx) {
        A.el("camIndex").value = candidates[attempt];
        A.store("yolo.cam", candidates[attempt]);
        hint.textContent = "摄像头 " + idx + " 不可用，已自动切到 index=" + candidates[attempt];
      }
    };
    img.onerror = function () {
      attempt += 1;
      if (attempt < candidates.length) {
        loadStream();
      } else {
        hint.textContent = "摄像头打不开：可能被「数据采集」或其它摄像头页面占用，请关掉后重试";
      }
    };
    okBtn.addEventListener("click", function () {
      if (!img.naturalWidth) return;
      var cv = document.createElement("canvas");
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      cv.getContext("2d").drawImage(img, 0, 0);
      var data = cv.toDataURL("image/jpeg", 0.92);
      close();
      A.action(async function () {
        var r = await A.api.post(apiBase() + "/upload-image", { image: data });
        A.ok("已采集并保存：" + r.file, "#yoloAlert");
        await refreshCurrentDataset();
        await loadImages(true);
      }, "#yoloAlert");
    });
    mask.querySelector("[data-cancel]").addEventListener("click", close);
    mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
    document.addEventListener("keydown", function onKey(e) {
      if (e.key === "Escape") { document.removeEventListener("keydown", onKey); close(); }
    });
    A.modalHost().appendChild(mask);
    loadStream();
  });

  function valRatio() {
    var v = parseFloat(A.el("valRatio").value);
    if (isNaN(v)) v = 0.2;
    v = Math.min(0.5, Math.max(0.05, v));
    A.el("valRatio").value = v;
    A.store("yolo.ratio", v);
    return v;
  }
  A.el("valRatio").addEventListener("change", function () { valRatio(); });

  A.click("btnSplit", function () {
    A.action(async function () {
      if (!state.ds) throw new Error("先选择数据集");
      var ratio = valRatio();
      var r = await A.api.post(apiBase() + "/split", { ratio: ratio });
      A.ok("已按 " + Math.round(ratio * 100) + "% 划分 " + r.moved +
        " 张到验证集（" + r.dataset.images_train + " train / " +
        r.dataset.images_val + " val）", "#yoloAlert");
      await refreshCurrentDataset();
      await loadImages(true);
    }, "#yoloAlert");
  });

  A.click("btnTrainYolo", function () {
    A.action(async function () {
      if (!state.ds) throw new Error("先选择一个数据集");
      if (!state.ds.images_train) throw new Error("训练集还没有图片");
      if (!state.ds.images_val) {
        if (state.ds.images_train < 2) {
          throw new Error("训练集只有 " + state.ds.images_train +
            " 张图片，至少需要 2 张（1 张训练 + 1 张验证）");
        }
        var ratio = valRatio();
        await A.api.post(apiBase() + "/split", { ratio: ratio });
        await refreshCurrentDataset();
        A.ok("验证集为空，已按 " + Math.round(ratio * 100) + "% 自动划分 " +
          state.ds.images_val + " 张到 val", "#yoloAlert");
      }
      await saveYolo(true);
      var r = await A.api.post("/api/train/start", {
        backend: "yolo",
        params: {
          data_yaml: "", dataset_dir: state.ds.path,
          model: A.el("yModel").value.trim() || "yolov8n.pt",
          epochs: parseInt(A.el("yEpochs").value, 10) || 50,
          imgsz: parseInt(A.el("yImgsz").value, 10) || 640,
          batch: parseInt(A.el("yBatch").value, 10) || 8,
          device: A.el("yDevice").value.trim() || "cuda",
          output_dir: "outputs/train/yolo"
        }
      });
      A.ok("YOLO 训练已启动：" + r.name, "#yoloAlert");
      A.refreshStatus();
    }, "#yoloAlert");
  });

  async function loadModels() {
    try {
      var r = await A.api.get("/api/checkpoints");
      var pts = (r.checkpoints || []).filter(function (c) {
        return /\.pt$/i.test(c.path);
      }).map(function (c) { return c.path; });
      var sel = A.el("valModel");
      if (!pts.length) {
        sel.innerHTML = '<option value="">（还没有 .pt 模型，先训练一次）</option>';
        return;
      }
      sel.innerHTML = pts.map(function (p) {
        return '<option value="' + A.esc(p) + '">' + A.esc(p) + "</option>";
      }).join("");
      var want = A.load("yolo.model", "");
      if (want && pts.indexOf(want) >= 0) sel.value = want;
    } catch (e) { /* 忽略 */ }
  }

  A.click("btnReloadModels", function () { loadModels(); });

  A.click("btnValidate", function () {
    A.action(async function () {
      var model = A.el("valModel").value;
      if (!model) throw new Error("还没有 .pt 模型，先在下方训练一次");
      A.store("yolo.model", model);
      var payload = { model: model, conf: parseFloat(A.el("valConf").value) || 0.25 };
      var src = A.el("valSource").value;
      if (src === "camera") {
        payload.camera_index = parseInt(A.el("camIndex").value, 10) || 0;
      } else {
        if (!state.ds) throw new Error("先选择数据集");
        var split = src === "val" ? "val" : state.split;
        var file = "";
        if (src === "current") {
          if (!state.current) throw new Error("先打开一张图片");
          file = state.current;
        } else {
          var r = await A.api.get(apiBase() + "/images?split=val");
          var imgs = r.images || [];
          if (!imgs.length) throw new Error("验证集为空，先点「划分验证集」");
          file = imgs[Math.floor(Math.random() * imgs.length)].file;
        }
        payload.dataset = state.ds.name;
        payload.split = split;
        payload.file = file;
      }
      A.el("valHint").textContent = "正在推理…";
      var res = await A.api.post("/api/yolo/predict", payload, 60000);
      if (res.image) {
        var img = A.el("valImg");
        img.src = res.image;
        img.style.display = "block";
      }
      A.el("valHint").textContent = "检测到 " + res.count + " 个目标";
      A.el("valDets").textContent = res.detections.map(function (d) {
        return d.name + "  " + (d.conf * 100).toFixed(1) + "%";
      }).join(" · ");
    }, "#yoloAlert");
  });

  A.el("valRatio").value = A.load("yolo.ratio", 0.2);

  A.click("btnDino", function () {
    A.action(async function () {
      if (!state.ds || !state.current) throw new Error("先打开一张图片");
      if (!state.ds.classes.length) throw new Error("数据集还没有类别");
      var overlay = A.el("aiOverlay"), btn = A.el("btnDino");
      overlay.hidden = false;
      btn.disabled = true;
      A.el("llmState").textContent = "自动标注中…";
      try {
        var r = await A.api.post("/api/ai/grounding", {
          dataset: state.ds.name, split: state.split, file: state.current,
          conf: parseFloat(A.el("dinoConf").value) || 0.2,
          max_boxes: parseInt(A.el("dinoMax").value, 10) || 100
        }, 240000);
        A.el("llmState").textContent = "已标注 " + r.count + " 个框（提示词：" + (r.prompt || "") + "）";
        A.ok("本地 DINO 预标注完成：" + r.count + " 个框", "#yoloAlert");
        var cur = state.current;
        await openImage(cur);
      } finally {
        overlay.hidden = true;
        btn.disabled = false;
      }
    }, "#yoloAlert");
  });

  await loadConfig();
  await loadModels();
  await loadDatasets();
  A.autosave.preferences(['valRatio', 'dinoConf', 'dinoMax', 'valModel', 'valSource', 'valConf']);
  yoloAutosave=A.autosave.bind({id:'yolo-settings',section:'yolo',root:'#page',read:function(){return {yolo:collectYolo()};},apply:function(c){var y=c.yolo||{};[['yModel','model'],['yEpochs','epochs'],['yImgsz','imgsz'],['yBatch','batch'],['yDevice','device'],['camIndex','camera_index']].forEach(function(pair){if(y[pair[1]]!==undefined)A.el(pair[0]).value=y[pair[1]];});}});
  A.el('labelText').addEventListener('input',function(){if(!state.img)return;var parsed=textToBoxes(this.value);if(parsed.errors.length){keepLabelDraft();dirty=true;annotationStatus(parsed.errors.join('；'));return;}state.boxes=parsed.boxes;state.sel=-1;draw();markDirty();});
})();
