/* 已录制数据集查看：相机视频 + 状态/动作曲线 */
(async function () {
  "use strict";
  var A = window.APP;
  await A.init({ active: "viewer", title: "数据查看", subtitle: "回放已录制数据集" });

  var S = { repo: "", episodes: [], cameras: [], data: null, from: 0, to: 0, raf: null, media:[],playing:false,request:0,duration:0,preview:null };
  var COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#0aa5a5", "#f032e6"];

  function fmt(t) { return (Number(t) || 0).toFixed(2) + "s"; }

  async function loadDatasets() {
    var r = await A.api.get("/api/datasets");
    var list = (r.datasets || []).filter(function (d) { return (d.episodes || 0) > 0; });
    var sel = A.el("vRepo");
    sel.innerHTML = list.map(function (d) {
      return '<option value="' + A.esc(d.repo_id) + '">' + A.esc(d.repo_id) +
        "（" + d.episodes + " 集）</option>";
    }).join("");
    if (!list.length) { A.el("vMeta").textContent = "还没有录制过数据集"; return; }
    var q = new URLSearchParams(location.search).get("repo");
    if (q && list.some(function (d) { return d.repo_id === q; })) sel.value = q;
    if (!sel.value) sel.selectedIndex = 0;
    await loadEpisodes();
  }

  async function loadEpisodes() {
    stopVideos();
    S.repo = A.el("vRepo").value;
    if (!S.repo) return;
    var request=++S.request;
    var r = await A.api.get("/api/dataset/view/episodes?repo_id=" + encodeURIComponent(S.repo));
    if(request!==S.request)return;
    S.episodes = r.episodes || [];
    S.cameras = r.cameras || [];
    var sel = A.el("vEp");
    sel.innerHTML = S.episodes.map(function (e) {
      return '<option value="' + e.episode_index + '">第 ' + e.episode_index + " 集 · " +
        e.length + " 帧</option>";
    }).join("");
    if(S.episodes.length)await loadEpisode();
    else{A.el('videos').textContent='所选数据集还没有可查看的剧集';A.el('vCameraCount').textContent='0 路';}
  }

  async function loadEpisode() {
    stopVideos();var request=++S.request;
    A.el('videos').textContent='正在加载本集的相机视频…';A.el('vTime').textContent='等待视频元数据';
    ['btnPlay','btnPause','btnRestart','vSeek'].forEach(function(id){A.el(id).disabled=true;});
    var idx = Number(A.el("vEp").value);
    var e = S.episodes.filter(function (x) { return x.episode_index === idx; })[0];
    if (!e) return;
    var data = await A.api.get("/api/dataset/view/episode?repo_id=" +
      encodeURIComponent(S.repo) + "&episode=" + idx);
    if(request!==S.request)return;S.data=data;
    S.cameras = Object.keys(e.cameras || {});
    var box = A.el("videos");
    box.innerHTML = "";
    S.duration=e.length/(S.data.fps||30);
    A.el('vCameraCount').textContent=S.cameras.length+' 路已录制相机';
    S.from = 1e9; S.to = 0;
    S.cameras.forEach(function (cam) {
      var c = e.cameras[cam];
      if(Number.isFinite(c.from)&&Number.isFinite(c.to)){S.from=Math.min(S.from,c.from);S.to=Math.max(S.to,c.to);}
      // 后端转成 H.264 小片段，避免 AV1 + 逐段 seek 造成的卡顿
      var url = "/api/dataset/view/clip?repo_id=" + encodeURIComponent(S.repo) +
        "&camera=" + encodeURIComponent(cam) + "&chunk=" + c.chunk + "&file=" + c.file +
        "&start=" + c.from + "&end=" + c.to + "&episode=" + idx;
      var wrap = document.createElement("article");wrap.className='viewer-camera';
      wrap.innerHTML='<header><strong>'+A.esc(cam.replace('observation.images.',''))+'</strong><button class="btn subtle sm" type="button">放大</button></header><div class="viewer-video"></div><p class="hint" role="status">正在加载本路视频，首次查看需转码…</p>';
      box.appendChild(wrap);
      var note=wrap.querySelector('p'),expand=wrap.querySelector('button');
      if(c.available===false){note.textContent=c.error||'本集没有这一路相机的视频';expand.disabled=true;return;}
      var v=document.createElement('video');v.muted=true;v.playsInline=true;v.preload='metadata';v.setAttribute('aria-label',cam);wrap.querySelector('.viewer-video').appendChild(v);S.media.push(v);
      v.addEventListener('loadedmetadata',function(){note.textContent=v.videoWidth+' × '+v.videoHeight+' · '+fmt(v.duration);if(S.playing)v.play().catch(function(error){note.textContent=error.message;});});
      v.addEventListener('error',function(){note.textContent='本路视频加载失败，请检查文件或重新加载；其他相机仍可播放';expand.disabled=true;});
      v.addEventListener('waiting',function(){note.textContent='正在缓冲本路画面…';});
      v.addEventListener('playing',function(){note.textContent='正在同步播放';});
      v.src=url;expand.onclick=function(){enlarge(v,cam,wrap.querySelector('.viewer-video'));};
      v.addEventListener("timeupdate", updateTime);
      v.addEventListener("seeked", updateTime);
    });
    A.el("vMeta").textContent = "任务：" + (e.tasks || []).join(" / ") + " · " +
      e.length + " 帧 · " + S.data.fps + " fps";
    A.el("vSeek").value = 0;
    ['btnPlay','btnPause','btnRestart','vSeek'].forEach(function(id){A.el(id).disabled=!S.media.length;});
    drawCharts();
    updateTime();
  }

  function vids() { return S.media; }
  function stopVideos(){
    ++S.request;S.playing=false;cancelAnimationFrame(S.raf);
    if(S.preview){var preview=S.preview;preview.close();preview.remove();S.preview=null;}
    S.media.forEach(function(v){v.pause();v.removeAttribute('src');v.load();});S.media=[];
  }
  function enlarge(video,name,home){
    var doc=window.top.document,dialog=doc.createElement('dialog'),marker=document.createElement('span');
    marker.hidden=true;home.insertBefore(marker,video);dialog.className='viewer-preview-dialog';
    dialog.innerHTML='<header><h2></h2><button class="btn subtle" type="button" aria-label="关闭放大画面">×</button></header><div></div>';
    dialog.querySelector('h2').textContent=name.replace('observation.images.','');dialog.querySelector('div').appendChild(video);doc.body.appendChild(dialog);S.preview=dialog;
    dialog.querySelector('button').onclick=function(){dialog.close();};dialog.onclick=function(e){if(e.target===dialog)dialog.close();};
    dialog.addEventListener('close',function(){if(marker.isConnected){marker.parentNode.insertBefore(video,marker);marker.remove();if(S.playing)video.play().catch(function(){});}dialog.remove();if(S.preview===dialog)S.preview=null;});dialog.showModal();if(S.playing)video.play().catch(function(){});
  }

  function updateTime() {
    var v = vids().find(function(video){return !video.error&&video.readyState>=1;});
    if (!v) return;
    var dur = S.duration || v.duration || 1;
    A.el("vTime").textContent = fmt(v.currentTime) + " / " + fmt(dur);
    A.el("vSeek").value = Math.max(0, Math.min(1000, Math.round(v.currentTime / dur * 1000)));
  }

  function drawChart(id, rows) {
    var cv = A.el(id);
    if (!cv || !rows || !rows.length) return;
    var dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth || 600, h = cv.clientHeight || 180;
    cv.width = w * dpr; cv.height = h * dpr;
    var g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    var pad = { l: 40, r: 8, t: 8, b: 18 };
    var min = Infinity, max = -Infinity;
    rows.forEach(function (r) {
      r.forEach(function (v) { if (v < min) min = v; if (v > max) max = v; });
    });
    if (!isFinite(min)) { min = -1; max = 1; }
    if (max - min < 1e-6) { max = min + 1; }
    g.strokeStyle = "#e3e9f0"; g.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var y = pad.t + (h - pad.t - pad.b) * i / 4;
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
    }
    var cols = rows[0].length;
    for (var d = 0; d < cols; d++) {
      g.strokeStyle = COLORS[d % COLORS.length];
      g.lineWidth = 1.4;
      g.beginPath();
      rows.forEach(function (r, x) {
        var px = pad.l + (x / Math.max(1, rows.length - 1)) * (w - pad.l - pad.r);
        var py = h - pad.b - ((r[d] - min) / (max - min)) * (h - pad.t - pad.b);
        if (x === 0) g.moveTo(px, py); else g.lineTo(px, py);
      });
      g.stroke();
    }
    g.fillStyle = "#6b7a90"; g.font = "11px sans-serif";
    g.fillText(min.toFixed(1), 4, h - pad.b);
    g.fillText(max.toFixed(1), 4, pad.t + 10);
  }

  function drawCharts() {
    if (!S.data) return;
    drawChart("stateChart", S.data.state);
    drawChart("actionChart", S.data.action);
  }

  function seekTo(t) {
    vids().forEach(function (v) {
      var dur = Number.isFinite(v.duration)?v.duration:S.duration||1;
      try { v.currentTime = Math.max(0, Math.min(dur - 0.05, t)); } catch (err) { /* ignore */ }
    });
    updateTime();
  }

  A.change("vRepo", function () { loadEpisodes().catch(function (e) { A.toast(e.message, "err"); }); });
  A.change("vEp", function () { loadEpisode().catch(function (e) { A.toast(e.message, "err"); }); });
  A.click("btnReload", function () {
    loadDatasets().catch(function (e) { A.toast(e.message, "err"); });
  });
  A.click("btnPlay", function () {
    S.playing=true;
    vids().forEach(function (v) {
      var dur = v.duration || 0;
      if (dur && v.currentTime >= dur - 0.1) v.currentTime = 0;
      v.play().catch(function(e){A.setAlert('#viewerAlert','部分视频暂不能播放：'+e.message,'warn');});
    });
    cancelAnimationFrame(S.raf);
    function sync(){
      if(!S.playing)return;
      var master=vids().find(function(v){return !v.error&&v.readyState>=2;});
      if(master){
        vids().forEach(function(v){if(v!==master&&!v.error&&v.readyState>=2&&Math.abs(v.currentTime-master.currentTime)>.12){try{v.currentTime=Math.min(master.currentTime,Math.max(0,v.duration-.02));}catch(e){}}});
        if(master.ended){S.playing=false;vids().forEach(function(v){v.pause();});}
      }
      updateTime();S.raf=requestAnimationFrame(sync);
    }sync();
  });
  A.click("btnPause", function () { S.playing=false;cancelAnimationFrame(S.raf);vids().forEach(function (v) { v.pause(); }); });
  A.click("btnDelEp", function () {
    A.action(async function () {
      var idx = Number(A.el("vEp").value);
      if (!(await A.confirm("删除第 " + idx + " 集？删除后不可恢复。\n" +
        "如果只是个别帧动作错了，建议重新录这一集。", { title: "删除剧集" }))) return;
      var box = A.blocking("正在删除第 " + idx + " 集，请勿操作页面…", { title: "删除剧集" });
      try {
        await A.api.post("/api/dataset/delete-episode",
          { repo_id: S.repo, episode: idx, confirm: true });
        for (var i = 0; i < 120; i++) {
          await new Promise(function (r) { setTimeout(r, 1000); });
          var st = await A.api.get("/api/status");
          if (!st.running) break;
          box.set("正在删除第 " + idx + " 集（已等待 " + (i + 1) + " 秒）…");
        }
        box.close();
        A.ok("第 " + idx + " 集已删除，列表已刷新", "#viewerAlert");
        await loadEpisodes();
      } catch (e) {
        box.close();
        A.toast(e.message, "err");
      }
    }, "#viewerAlert");
  });
  A.click("btnRestart", function () { seekTo(0); });
  A.el("vSeek").addEventListener("input", function () {
    var v = vids()[0];
    var dur = S.duration || (v && v.duration) || 1;
    seekTo((Number(this.value) / 1000) * dur);
  });

  loadDatasets().catch(function (e) {
    A.setAlert(A.el("viewerAlert"), "加载失败：" + e.message, "err");
  });
  window.addEventListener('pagehide',stopVideos);
})();
