/* ============================================================
   公共脚本：导航外壳 / 主题 / API 错误封装 / 日志总线 / 表单 / 图表
   所有页面通过 APP.init({...}) 初始化
   ============================================================ */
(function () {
  "use strict";
  var pageLeaving=false;
  window.addEventListener('pagehide',function(){pageLeaving=true;});
  window.addEventListener('pageshow',function(){pageLeaving=false;});

  var NAV = [
    { id: "home",    label: "总览",       href: "/p/home",    ico: "home" },
    { id: "connect", label: "设备连接与校准", href: "/p/connect", ico: "plug" },
    { id: "camera",  label: "摄像头",     href: "/p/camera",  ico: "camera" },
    { id: "teleop",  label: "遥操作",     href: "/p/teleop",  ico: "gamepad" },
    { id: "dataset", label: "训练数据",   href: "/p/dataset", ico: "database" },
    { id: "viewer",  label: "数据查看",   href: "/p/viewer",  ico: "play" },
    { id: "yolo",    label: "视觉数据",   href: "/p/yolo",    ico: "grid" },
    { id: "train",   label: "模型训练",   href: "/p/train",   ico: "cpu" },
    { id: "eval",    label: "快递分拣",   href: "/p/eval",    ico: "chart" },
    { id: "setup",   label: "环境安装",   href: "/p/setup",   ico: "package" },
    { id: "logs",    label: "日志与命令", href: "/p/logs",    ico: "terminal" }
  ];

  /* ---------------- 图标库：全部为内联 SVG 线条图标，不使用 emoji ---------------- */
  var ICONS = {
    brand:    '<path d="M3 20.5h6.5"/><path d="M6.2 20.5v-5.2l6-4"/>' +
              '<circle cx="6.2" cy="15.3" r="1.6"/><path d="M12.2 11.3 16 5.2"/>' +
              '<circle cx="12.2" cy="11.3" r="1.6"/>' +
              '<path d="M16 5.2l3.7 1.6M19.7 6.8l-.6 3.8M16 5.2l2.9-2"/>',
    home:     '<path d="M3.2 10.6 12 3.4l8.8 7.2"/><path d="M5.5 9.4v11.2h13V9.4"/>' +
              '<path d="M9.6 20.6v-6.2h4.8v6.2"/>',
    plug:     '<path d="M8.5 3v5M15.5 3v5"/><rect x="5" y="8" width="14" height="6.5" rx="1.6"/>' +
              '<path d="M12 14.5V21M8.5 21h7"/>',
    camera:   '<rect x="2.5" y="6.3" width="14" height="11.4" rx="2"/>' +
              '<path d="M16.5 10.4l5-2.7v8.6l-5-2.7z"/><circle cx="9.5" cy="12" r="2.7"/>',
    gamepad:  '<path d="M7.5 8h9a4.5 4.5 0 0 1 4.3 5.8l-.8 3A2.6 2.6 0 0 1 15.4 18l-1.4-2.5H10L8.6 18a2.6 2.6 0 0 1-4.6-1.2l-.8-3A4.5 4.5 0 0 1 7.5 8Z"/>' +
              '<path d="M7 11.5v3M5.5 13h3"/><circle cx="16" cy="12.5" r=".9"/>' +
              '<circle cx="18" cy="14.5" r=".9"/>',
    database: '<ellipse cx="12" cy="5.6" rx="7.8" ry="2.9"/>' +
              '<path d="M4.2 5.6v6c0 1.6 3.5 2.9 7.8 2.9s7.8-1.3 7.8-2.9v-6"/>' +
              '<path d="M4.2 11.6v6c0 1.6 3.5 2.9 7.8 2.9s7.8-1.3 7.8-2.9v-6"/>',
    cpu:      '<rect x="6.2" y="6.2" width="11.6" height="11.6" rx="1.8"/>' +
              '<rect x="9.6" y="9.6" width="4.8" height="4.8" rx="1"/>' +
              '<path d="M9.4 2.8v3.4M14.6 2.8v3.4M9.4 17.8v3.4M14.6 17.8v3.4' +
              'M2.8 9.4h3.4M2.8 14.6h3.4M17.8 9.4h3.4M17.8 14.6h3.4"/>',
    package:  '<path d="M12 2.8 20.5 7v10L12 21.2 3.5 17V7z"/>' +
              '<path d="M3.5 7 12 11.5 20.5 7M12 11.5v9.7"/>',
    terminal: '<rect x="2.6" y="4" width="18.8" height="16" rx="2"/>' +
              '<path d="m6.6 9.2 3 2.8-3 2.8M12.2 15h5"/>',
    refresh:  '<path d="M20.5 12a8.5 8.5 0 1 1-2.4-5.9"/><path d="M20.5 3.8v5.4h-5.4"/>',
    shield:   '<path d="M12 2.8 20 6.2v5.5c0 4.8-3.3 7.9-8 9.5-4.7-1.6-8-4.7-8-9.5V6.2z"/>' +
              '<path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
    folder:   '<path d="M3 6.6A1.6 1.6 0 0 1 4.6 5H9l2 2h8.4A1.6 1.6 0 0 1 21 8.6v8.8a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 17.4z"/>',
    chevron:  '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    layers:   '<path d="m12 3 9 4.8-9 4.8-9-4.8z"/><path d="m3 12 9 4.8 9-4.8"/>' +
              '<path d="m3 16.5 9 4.8 9-4.8"/>',
    dual:     '<path d="M3.4 20.5h4.2M5.5 20.5v-5.1l3.1-2.3"/><circle cx="5.5" cy="15.4" r="1.4"/>' +
              '<path d="M8.6 13.1 10.4 9.2l2.4-1.3"/><path d="M20.6 20.5h-4.2M18.5 20.5v-5.1l-3.1-2.3"/>' +
              '<circle cx="18.5" cy="15.4" r="1.4"/><path d="M15.4 13.1 13.6 9.2l-2.4-1.3"/>' +
              '<path d="M10.4 9.2h3.2"/>',
    grid:     '<rect x="3.5" y="3.5" width="7" height="7" rx="1.4"/>' +
              '<rect x="13.5" y="3.5" width="7" height="7" rx="1.4"/>' +
              '<rect x="3.5" y="13.5" width="7" height="7" rx="1.4"/>' +
              '<rect x="13.5" y="13.5" width="7" height="7" rx="1.4"/>',
    activity: '<path d="M2.8 12h4L9.5 5l4.5 14 2.8-7h4.4"/>',
    play:     '<path d="M7.5 4.8 19 12 7.5 19.2z"/>',
    stop:     '<rect x="6" y="6" width="12" height="12" rx="1.6"/>',
    download: '<path d="M12 3.5v11M7.5 10.5 12 15l4.5-4.5"/>' +
              '<path d="M4 17.5v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2"/>',
    plus:     '<path d="M12 5v14M5 12h14"/>',
    save:     '<path d="M5 3.8h11.5L20 7.3V19a1.2 1.2 0 0 1-1.2 1.2H5A1.2 1.2 0 0 1 3.8 19V5A1.2 1.2 0 0 1 5 3.8z"/>' +
              '<path d="M7.5 3.8v5.5h8V3.8M7.5 20.2v-6h9v6"/>',
    arrow:    '<path d="M4 12h15M13 6l6 6-6 6"/>',
    arrowLeft:'<path d="M20 12H5M11 6l-6 6 6 6"/>',
    chart:    '<path d="M3.5 20h17M5 16.2l4.5-5.4 3.2 2.4 5.8-7"/>',
    trash:    '<path d="M4.5 6.5h15M9 6.5V4h6v2.5"/>' +
              '<path d="M6.5 6.5l.8 13a1.2 1.2 0 0 0 1.2 1.1h7a1.2 1.2 0 0 0 1.2-1.1l.8-13"/>',
    flag:     '<path d="M5.5 21V3.5M5.5 4.5h12l-2.5 4 2.5 4h-12"/>',
    alert:    '<path d="M12 3.8 2.8 20h18.4z"/><path d="M12 9.5v5"/><circle cx="12" cy="17.3" r=".8"/>',
    inbox:    '<path d="M3.5 13h4l1.5 2.5h6L16.5 13h4"/>' +
              '<path d="M5.5 4.5h13l2 8.5v5a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18v-5z"/>',
    search:   '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    monitor:  '<rect x="2.6" y="4" width="18.8" height="13" rx="1.8"/>' +
              '<path d="M8.5 20.6h7M12 17v3.6"/>',
    server:   '<rect x="3" y="3.5" width="18" height="7" rx="1.6"/>' +
              '<rect x="3" y="13.5" width="18" height="7" rx="1.6"/>' +
              '<path d="M6.5 7h.01M6.5 17h.01M10 7h5M10 17h5"/>',
    settings: '<circle cx="12" cy="12" r="3.1"/>' +
              '<path d="M19.4 14.5a1.6 1.6 0 0 0 .3 1.8l.1.1-1.7 2.9-.1-.1a1.6 1.6 0 0 0-1.8-.3' +
              ' 1.6 1.6 0 0 0-1 1.5v.2h-3.4v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1' +
              '-1.7-2.9.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H5.8v-3.4h.2a1.6 1.6 0 0 0 1.5-1' +
              ' 1.6 1.6 0 0 0-.3-1.8l-.1-.1 1.7-2.9.1.1a1.6 1.6 0 0 0 1.8.3 1.6 1.6 0 0 0 1-1.5V3.5h3.4v.2' +
              'a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1 1.7 2.9-.1.1a1.6 1.6 0 0 0-.3 1.8' +
              ' 1.6 1.6 0 0 0 1.5 1h.2v3.4h-.2a1.6 1.6 0 0 0-1.5 1z"/>',
    close:    '<path d="M5.5 5.5 18.5 18.5M18.5 5.5 5.5 18.5"/>'
  };
  function icon(name, cls) {
    return '<svg class="ico' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      (ICONS[name] || ICONS.grid) + "</svg>";
  }

  /* ---------------- 前端图标装饰：品牌 / 导航 / 标题 / 按钮 ---------------- */
  var CARD_ICONS = [
    [/执行日志|训练日志|终端日志|最近日志|日志与命令|文档命令/, "terminal"],
    [/安装状态|安装源|一键安装|虚拟环境|运行环境|环境安装/, "package"],
    [/串口授权|权限/, "shield"],
    [/串口|连接|机械臂连接|校准|设备参数/, "plug"],
    [/摄像头|相机|实时画面/, "camera"],
    [/遥操作|异步推理|录制键盘|键盘控制/, "gamepad"],
    [/数据|可视化|回放|采集/, "database"],
    [/Loss|曲线|指标/, "chart"],
    [/训练|模型产出|引擎|训练参数/, "cpu"],
    [/快捷操作/, "grid"],
    [/当前任务|运行状态|运行状态|环境信息/, "activity"],
    [/进入主界面/, "arrow"],
    [/策略评估|^执行$/, "play"],
    [/设备与访问|访问设备|运行位置|真机信息/, "monitor"]
  ];
  var BTN_ICONS = [
    [/本机设置|切换真机|设备设置/, "settings"],
    [/急停|停止|断开|结束会话|ESC/, "stop"],
    [/进入主界面|前往训练|下一集/, "arrow"],
    [/返回/, "arrowLeft"],
    [/重新检测|刷新|扫描|环境检查/, "refresh"],
    [/检查权限|授权/, "shield"],
    [/安装|下载/, "download"],
    [/创建|添加|新建|填入/, "plus"],
    [/保存|应用|配置/, "save"],
    [/清空|清理/, "trash"],
    [/抓拍|摄像头|画面/, "camera"],
    [/回放|可视化|开始|启动|执行|采集|评估|训练|遥操作|重录/, "play"],
    [/连接|启用|校准|选择/, "plug"]
  ];
  function pickIcon(text, rules, fallback) {
    var s = String(text || "").trim();
    for (var i = 0; i < rules.length; i++) {
      if (rules[i][0].test(s)) return rules[i][1];
    }
    return fallback;
  }
  function decorateIcons(opts) {
    opts = opts || {};
    // 图标按钮：HTML 里只声明 data-icon，图标统一由这里注入，避免散落的 SVG 副本
    qa("#page [data-icon]").forEach(function (b) {
      b.innerHTML = icon(b.getAttribute("data-icon"));
    });
    var pageIcon = {
      home: "home", connect: "plug", camera: "camera", teleop: "gamepad",
      dataset: "database", train: "cpu", setup: "package", logs: "terminal"
    };
    qa(".top h1, .wizard-head h1").forEach(function (h) {
      if (q(".ico", h)) return;
      h.insertAdjacentHTML("afterbegin", icon(pageIcon[opts.active] || "flag", "head-ico"));
    });
    qa("#page .card-h h2").forEach(function (h) {
      if (q(".ico", h)) return;
      h.insertAdjacentHTML("afterbegin", icon(pickIcon(h.textContent, CARD_ICONS, "grid")));
    });
    qa("#page button, #page a.btn").forEach(function (b) {
      if (q(".ico", b) || b.classList.contains("sm") || b.classList.contains("link") ||
          b.hasAttribute("data-app-retry") || b.hasAttribute("data-lero") ||
          b.classList.contains("icon") || b.classList.contains("combo-caret") ||
          b.classList.contains("combo-opt") || b.classList.contains("dir-item")) return;
      var text = (b.textContent || "").trim();
      if (!text) return;
      b.insertAdjacentHTML("afterbegin", icon(pickIcon(text, BTN_ICONS, "play")));
    });
  }
  var THEME_KEY = "arm.theme";
  var LOG_HISTORY = [];
  /* 按钮就地反馈：进行中状态与实时日志显示在按钮所在容器内 */
  var fbBoxes = new WeakMap(), fbActive = null, apiPending = 0;
  var fbResultMsg = "", fbIdleTimer = null, fbAutoTimer = null;
  var jobProgress = null, jobExpectTimer = null;
  var BOOT_TS = Date.now() / 1000 - 2;
  var statusCallbacks = [];
  var themeCallbacks = [];
  var appStatus = null;
  var statusFailed = false;

  /* ---------------- 基础工具 ---------------- */
  function esc(s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function modalDocument() {
    try {
      var topWindow = window.top;
      if (topWindow !== window && topWindow.document &&
          topWindow.location.origin === window.location.origin) return topWindow.document;
    } catch (e) { /* Cross-origin frames keep their own modal layer. */ }
    return document;
  }
  function modalHost() { return modalDocument().body; }
  function el(id) {
    var node = document.getElementById(id);
    if (node) return node;
    var hostDoc = modalDocument();
    return hostDoc === document ? null : hostDoc.getElementById(id);
  }
  function q(sel, root) {
    var base = root || document, node = base.querySelector(sel);
    if (node || root || sel.charAt(0) !== "#") return node;
    var hostDoc = modalDocument();
    return hostDoc === document ? null : hostDoc.querySelector(sel);
  }
  function qa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  /* ---------------- 统一 API（含超时与错误规范化） ---------------- */
  async function request(method, url, body, timeoutMs) {
    if(method==='POST' && /^\/api\/(?:teleop\/start|record\/start|train\/(?:start|resume)|eval$|connect$|calibrate\/|dual\/calibrate|robot-client$)/.test(url) && window.APP && window.APP.autosave) await window.APP.autosave.flush();
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 20000);
    apiPending++;
    fbTouchPending();
    try {
      var opt = { method: method, signal: ctrl.signal, headers: {} };
      // 让后端日志能区分是哪台访问设备发起的操作（仅是名称，不含任何硬件状态）
      // HTTP 头只允许 ASCII：中文设备名必须 URL 编码，否则 fetch 会直接抛错
      var who = clientPrefs().alias.trim().slice(0, 40);
      if (who) opt.headers["X-Arm-Client"] = encodeURIComponent(who);
      if (body !== undefined && body !== null) {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
        if(url==='/api/config'&&opt.body.length<50000)opt.keepalive=true;
      }
      var res = await fetch(url, opt);
      var text = await res.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; }
      catch (e) { data = { ok: false, error: text || ("HTTP " + res.status) }; }
      if (!res.ok || (data && data.ok === false)) {
        var apiError=new Error((data&&(data.error||(typeof data.detail==='string'&&data.detail))) || ("HTTP " + res.status + " " + res.statusText));
        apiError.status=res.status;apiError.path=url;
        apiError.serverVersion=res.headers.get('x-rebot-version')||'';
        throw apiError;
      }
      // 启动类接口返回后台任务：先别把按钮容器标成「完成」，等真实进度到达
      if (method !== "GET" && data && (data.name || data.steps || data.running === true)) {
        expectJob();
      }
      return data;
    } catch (e) {
      if (e && e.name === "AbortError") {
        throw new Error("请求超时，请确认后端服务正在运行且命令未卡住");
      }
      if (e instanceof TypeError) {
        throw new Error("无法连接后端服务，请确认已执行 python start.py");
      }
      throw e;
    } finally {
      clearTimeout(timer);
      apiPending--;
      fbTouchPending();
    }
  }
  var API = {
    get: function (u, t) { return request("GET", u, undefined, t); },
    post: function (u, b, t) { return request("POST", u, b === undefined ? {} : b, t); },
    put: function (u, b, t) { return request("PUT", u, b === undefined ? {} : b, t); },
    del: function (u, t) { return request("DELETE", u, undefined, t); },
    request: request
  };

  /* ---------------- 按钮就地反馈：进度显示在当前按钮所在容器 ---------------- */
  function fbBox(host, btn) {
    var o = fbBoxes.get(host);
    if (o && !host.contains(o.wrap)) { fbBoxes.delete(host); o = null; }
    if (o) return o;
    var wrap = document.createElement("div");
    wrap.className = "inline-feedback";
    var status = document.createElement("div");
    status.className = "fb-status";
    var prog = document.createElement("div");
    prog.className = "fb-progress";
    prog.hidden = true;
    prog.innerHTML =
      '<div class="fb-bar"><i></i></div>' +
      '<div class="fb-detail"><span class="fb-step"></span><span class="fb-time"></span>' +
        '<button type="button" class="btn subtle icon jp-stop" data-jp-stop data-app-nofb ' +
          'title="终止当前任务" aria-label="终止当前任务">' + icon("stop") + "</button></div>" +
      '<div class="fb-last"></div><div class="fb-hint"></div>';
    var log = document.createElement("div");
    log.className = "fb-log";
    wrap.appendChild(status);
    wrap.appendChild(prog);
    wrap.appendChild(log);
    // 放在触发按钮所在区块的正下方（区块可能是卡片内的一个子容器），
    // 找不到时退回到卡片标题下方，保证「进度就在当前按钮容器里」。
    var anchor = btn.closest(".row") || btn.closest(".btn-row") || btn;
    var node = anchor;
    while (node && node.parentNode && node.parentNode !== host) node = node.parentNode;
    var ref = (node && node.parentNode === host) ? node.nextSibling : null;
    if (ref && ref.parentNode !== host) ref = null;   // 防止拿到已重新渲染的旧节点
    if (!ref) {
      var head = q(".card-h", host);
      ref = (head && head.parentNode === host) ? head.nextSibling : null;
    }
    if (ref && ref.parentNode === host) host.insertBefore(wrap, ref);
    else host.appendChild(wrap);
    o = { wrap: wrap, status: status, log: log, prog: prog, hideTimer: null };
    fbBoxes.set(host, o);
    return o;
  }
  function fbShow(o) {
    o.wrap.classList.add("show");
    if (o.hideTimer) { clearTimeout(o.hideTimer); o.hideTimer = null; }
  }
  function fbSet(o, text, type) {
    o.status.className = "fb-status " + (type || "busy");
    o.status.textContent = text;
  }
  var jobBox = null;
  function fbAppend(ev) {
    var o = (jobRunning() && jobBox) ? jobBox : (fbActive && fbActive.box);
    if (!o) return;
    var line = document.createElement("div");
    line.className = "fb-line " + (ev.level || "info");
    line.textContent = ev.line || "";
    o.log.appendChild(line);
    while (o.log.childNodes.length > 6) o.log.removeChild(o.log.firstChild);
    o.log.scrollTop = o.log.scrollHeight;
  }
  function fbBusy(on) {
    if (fbActive && fbActive.btn) fbActive.btn.classList.toggle("is-busy", on);
  }
  function fbFinish(type, msg) {
    if (!fbActive) return;
    var o = fbActive.box;
    fbBusy(false);
    fbActive.done = true;   // 后续状态轮询不能把「完成」覆盖回「进行中」
    if (fbAutoTimer) { clearTimeout(fbAutoTimer); fbAutoTimer = null; }
    if (fbIdleTimer) { clearTimeout(fbIdleTimer); fbIdleTimer = null; }
    var text = type === "err" ? ("失败 · " + msg)
      : type === "warn" ? ("提示 · " + msg)
      : ("完成 · " + (msg || fbActive.label));
    fbSet(o, text, type === "err" ? "err" : (type === "warn" ? "warn" : "ok"));
    if (fbActive.box.prog) fbActive.box.prog.hidden = true;
    if (o.hideTimer) clearTimeout(o.hideTimer);
    o.hideTimer = setTimeout(function () {
      o.wrap.classList.remove("show");
      o.hideTimer = null;
    }, 15000);
  }
  function fbTouchPending() {
    if (!fbActive) return;
    if (apiPending > 0) {
      fbBusy(true);
      fbShow(fbActive.box);
      if (fbActive.done) return;   // 已收尾，别被后台轮询改回「进行中」
      if (jobRunning() && jobProgress) renderProgress(jobProgress);
      else fbSet(fbActive.box, "进行中 · " + fbActive.label, "busy");
      return;
    }
    if (jobRunning()) {   // 后台任务还在跑，保持「运行中」，不提前标成完成
      fbBusy(true);
      fbShow(fbActive.box);
      if (jobProgress) renderProgress(jobProgress);
      return;
    }
    fbBusy(false);
    if (fbAutoTimer) clearTimeout(fbAutoTimer);
    fbAutoTimer = setTimeout(function () {
      fbAutoTimer = null;
      if (fbIdleTimer || jobRunning()) return;   // 后台任务日志还在推送，保持「运行中」
      fbFinish("ok", fbResultMsg || "");
    }, 700);
  }
  function fbOnEvent(ev) {
    if (!fbActive || ev.type !== "log") return;
    var ts = Number(ev.ts) || 0;
    if (ts && ts < fbActive.startedAt) return;   // 忽略 WS 连接时的历史日志回放
    var o = fbActive.box;
    fbShow(o);
    fbAppend(ev);
    if (fbActive.done) return;   // 任务已收尾：日志继续回显，但不改完成结论
    if (fbAutoTimer) { clearTimeout(fbAutoTimer); fbAutoTimer = null; }
    if (fbIdleTimer) { clearTimeout(fbIdleTimer); fbIdleTimer = null; }
    if (jobRunning()) {   // 有真实进度在推送，日志静默 4s 也不算任务结束
      if (jobProgress) renderProgress(jobProgress);
      else fbSet(o, "进行中 · " + fbActive.label, "busy");
      return;
    }
    fbSet(o, "运行中 · " + String(ev.line || "").slice(0, 72), "busy");
    fbIdleTimer = setTimeout(function () {
      fbIdleTimer = null;
      fbFinish("ok", fbResultMsg || "后台任务已结束");
    }, 4000);
  }
  function fbStart(btn) {
    var host = btn.closest(".card") || btn.closest(".page") || el("page") || document.body;
    var o = fbBox(host, btn);
    o.log.innerHTML = "";
    if (o.prog) o.prog.hidden = true;
    fbResultMsg = "";
    if (fbIdleTimer) { clearTimeout(fbIdleTimer); fbIdleTimer = null; }
    if (fbAutoTimer) { clearTimeout(fbAutoTimer); fbAutoTimer = null; }
    fbActive = {
      host: host, box: o, btn: btn,
      // 图标按钮没有文字，优先读 data-label / aria-label / title
      label: (btn.dataset.label || btn.getAttribute("aria-label") || btn.title ||
              btn.textContent || btn.value || "操作").trim().slice(0, 24),
      startedAt: Date.now() / 1000 - 0.5,
      expectJob: false, jobBound: false, done: false
    };
    fbShow(o);
    fbBusy(true);
    fbSet(o, "进行中 · " + fbActive.label, "busy");
  }

  /* ---------------- 后台任务进度：按钮容器内 + 日志卡片全局兜底 ----------------
     /api/status 轮询与 WebSocket state 事件驱动同一份进度；
     命令长时间没输出时后端仍每 2s 推心跳，界面不会出现「点了没反应」。 */
  function mmss(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function jobRunning() {
    return !!(jobProgress && jobProgress.running) || !!(fbActive && fbActive.expectJob);
  }
  function clearJobExpect() {
    if (jobExpectTimer) { clearTimeout(jobExpectTimer); jobExpectTimer = null; }
    if (fbActive) fbActive.expectJob = false;
  }
  function expectJob() {
    if (!fbActive) return;
    // 记住"是哪个操作发起的任务"：后续点其它按钮时，任务日志仍留在这个操作里
    jobBox = fbActive.box || jobBox;
    fbActive.expectJob = true;
    fbActive.jobBound = true;
    if (jobExpectTimer) clearTimeout(jobExpectTimer);
    // 兜底：8 秒内既没收到进度事件也没查到运行任务，就正常收尾，不永久挂着
    jobExpectTimer = setTimeout(function () {
      jobExpectTimer = null;
      if (fbActive) fbActive.expectJob = false;
      if (!jobRunning() && fbActive) fbFinish("ok", fbResultMsg || "后台任务已启动");
    }, 8000);
    refreshStatus();
  }
  function fromStatus(s) {
    if (!s || !s.running || !s.job) return null;
    var j = s.job;
    return {
      running: true, job: j.name || "", kind: j.kind || "",
      step: Number(j.step) || 0, step_total: Number(j.step_total) || 0,
      step_label: j.step_label || "", elapsed: Number(j.elapsed) || 0,
      step_elapsed: Number(j.step_elapsed) || 0, step_limit: Number(j.step_limit) || 0,
      last_line: j.last_line || "", silent_for: Number(j.silent_for) || 0,
      awaiting_input: !!j.awaiting_input, prompt: j.prompt || "",
      exit_code: j.exit_code
    };
  }
  function fromState(ev) {
    return {
      running: true, job: ev.job || "", kind: ev.kind || "",
      step: Number(ev.step) || 0, step_total: Number(ev.step_total) || 0,
      step_label: ev.step_label || "", elapsed: Number(ev.elapsed) || 0,
      step_elapsed: Number(ev.step_elapsed) || 0, step_limit: Number(ev.step_limit) || 0,
      last_line: ev.last_line || "", silent_for: Number(ev.silent_for) || 0,
      awaiting_input: !!ev.awaiting_input, prompt: ev.prompt || "",
      exit_code: ev.exit_code
    };
  }
  function progressPercent(p) {
    if (p.step_total > 0) {
      var base = (Math.max(1, p.step) - 1) / p.step_total;
      var lim = Number(p.step_limit) || 0;
      var frac = lim > 0 ? Math.min(0.92, Math.max(0.06, (Number(p.step_elapsed) || 0) / lim)) : 0.12;
      return Math.max(3, Math.min(99, (base + frac / p.step_total) * 100));
    }
    return 6 + Math.min(88, ((Number(p.elapsed) || 0) % 60) / 60 * 88);
  }
  function progressTitle(p) {
    return (p.step_total > 0 ? "步骤 " + p.step + "/" + p.step_total + " · " : "") +
      (p.step_label || p.job || "后台任务");
  }
  function progressHint(p) {
    var silent = Math.floor(Number(p.silent_for) || 0);
    return silent > 12
      ? "已 " + silent + "s 无新输出，任务仍在运行（后端每 2s 推送心跳）；怀疑网络卡住可点右侧急停"
      : "后端实时推送步骤与输出；即使命令暂时无输出，也会持续显示心跳与耗时";
  }
  function globalProgressNode() {
    var lp = el("logPanel");
    var host = lp && lp.closest ? lp.closest(".card") : null;
    if (!host) return null;
    var gp = q(".job-progress", host);
    if (!gp) {
      gp = document.createElement("div");
      gp.className = "job-progress";
      gp.hidden = true;
      gp.innerHTML =
        '<div class="jp-head"><span class="jp-ico">' + icon("activity") + "</span>" +
          '<strong class="jp-title"></strong><span class="jp-time"></span>' +
          '<button type="button" class="btn subtle icon jp-stop" data-jp-stop data-app-nofb ' +
            'title="终止当前任务" aria-label="终止当前任务">' + icon("stop") + "</button></div>" +
        '<div class="jp-bar"><i></i></div>' +
        '<div class="jp-detail"><span class="jp-step"></span><code class="jp-last"></code></div>' +
        '<div class="jp-input">' +
          '<span class="jp-input-ico">' + icon("terminal") + "</span>" +
          '<button type="button" class="btn sm primary jp-open" data-app-nofb>打开命令输入框</button>' +
          '<span class="jp-input-hint"></span>' +
        "</div>" +
        '<div class="jp-hint"></div>';
      var head = q(".card-h", host);
      if (head && head.nextSibling) host.insertBefore(gp, head.nextSibling);
      else host.appendChild(gp);

      q('.jp-open',gp).onclick=function(){
        var controller=window.parent.JOB_CONSOLE||window.JOB_CONSOLE;
        if(controller)controller.open();
      };
    }
    return gp;
  }
  var lastAwaiting = false;
  function syncTermRow(p) {
    var gp = globalProgressNode();
    if (!gp) return;
    var box = q(".jp-input", gp);
    if (!box) return;
    var aw = !!(p && p.awaiting_input);
    box.hidden = !p;
    box.classList.toggle("await", aw);
    var hint = q(".jp-input-hint", gp);
    if (hint) {
      hint.textContent = aw
        ? ("等待输入：" + ((p && p.prompt) || "请在全局输入框中回答"))
        : "需要回车或填写 c 等内容时，打开全局命令输入框。";
    }
    // Automatic prompts belong to the persistent top-level job console.
    lastAwaiting = aw;
  }
  function renderProgress(p) {
    var pct = progressPercent(p);
    var title = progressTitle(p), hint = progressHint(p);
    var last = p.last_line || "等待命令首条输出…";
    if (fbActive && fbActive.jobBound && fbActive.box && fbActive.box.prog) {
      var pr = fbActive.box.prog;
      pr.hidden = false;
      q(".fb-bar i", pr).style.width = pct + "%";
      q(".fb-step", pr).textContent = title;
      q(".fb-time", pr).textContent = "已用时 " + mmss(p.elapsed) +
        (p.step_total ? " · " + pct.toFixed(0) + "%" : "");
      q(".fb-last", pr).textContent = "最近输出：" + last;
      q(".fb-hint", pr).textContent = hint;
      fbShow(fbActive.box);
      fbSet(fbActive.box, "运行中 · " + title + " · 已用时 " + mmss(p.elapsed), "busy");
    }
    var gp = globalProgressNode();
    if (gp) {
      gp.hidden = false;
      gp.classList.remove("done", "failed");
      q(".jp-title", gp).textContent = "后台任务运行中";
      q(".jp-time", gp).textContent = "已用时 " + mmss(p.elapsed);
      q(".jp-bar i", gp).style.width = pct + "%";
      q(".jp-step", gp).textContent = title + " · " + pct.toFixed(0) + "%";
      q(".jp-last", gp).textContent = last;
      q(".jp-hint", gp).textContent = hint;
      syncTermRow(p);
    }
  }
  function finishGlobalView(code, msg) {
    var gp = globalProgressNode();
    if (!gp) return;
    var bad = code !== undefined && code !== null && Number(code) !== 0;
    gp.hidden = false;
    gp.classList.toggle("failed", bad);
    gp.classList.toggle("done", !bad);
    var box = q(".jp-input", gp);
    if (box) { box.hidden = true; box.classList.remove("await"); }
    lastAwaiting = false;
    q(".jp-title", gp).textContent = Number(code) === 130 ? "任务已终止"
      : (bad ? "后台任务失败" : "后台任务已完成");
    q(".jp-time", gp).textContent = "已结束";
    q(".jp-bar i", gp).style.width = "100%";
    q(".jp-step", gp).textContent = Number(code) === 130 ? "用户急停" : (bad ? "未完成" : "全部完成");
    q(".jp-last", gp).textContent = msg;
    q(".jp-hint", gp).textContent = "详细输出见下方执行日志；如需重试请检查日志里的最后一条错误";
  }
  function setJobProgress(p, ev, opts) {
    opts = opts || {};
    var wasRunning = jobRunning();
    if (p && p.running) {
      clearJobExpect();
      jobProgress = p;
      renderProgress(p);
      return;
    }
    jobProgress = null;
    if (!opts.finish) return;
    // 状态轮询恰好在任务刚下发时返回「空闲」：不抢在真实进度事件之前收尾
    if (!opts.state && fbActive && fbActive.expectJob) return;
    var bound = !!(fbActive && fbActive.jobBound);
    var hadTask = wasRunning || bound;
    if (!hadTask) return;   // 页面本来就没有任务，不凭空造一个「已完成」面板
    clearJobExpect();
    if (fbActive) fbActive.jobBound = false;
    var code = ev && ev.exit_code !== undefined ? Number(ev.exit_code)
      : (p && p.exit_code !== undefined ? Number(p.exit_code) : 0);
    var msg = Number(code) === 130 ? "任务已由急停终止"
      : (code !== 0 ? "任务失败（退出码 " + code + "），请查看执行日志"
        : (fbResultMsg || "后台任务已完成"));
    if (wasRunning && bound) {
      fbFinish(code === 0 ? "ok" : (Number(code) === 130 ? "warn" : "err"), msg);
    }
    finishGlobalView(code, msg);
  }
  function applyStateEvent(ev) {
    // WebSocket 连接时会回放历史事件，用页面启动时间过滤，避免旧任务误显示
    if (ev.ts && Number(ev.ts) < BOOT_TS) return;
    if (ev.running === true) setJobProgress(fromState(ev), ev, { finish: false, state: true });
    else if (ev.running === false) setJobProgress(null, ev, { finish: true, state: true });
  }

  // 进度面板里的急停：两处面板共用，且不会触发新的按钮就地反馈
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest ? e.target.closest("[data-jp-stop]") : null;
    if (!b) return;
    e.preventDefault();
    action(async function () {
      await API.post("/api/stop", {});
      ok("已请求停止，正在结束动作与慢速收工");
    });
  });

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var btn = t.closest("button, .btn");
    if (!btn || btn.disabled || btn.id === "themeSelect" ||
        btn.hasAttribute("data-app-retry") || btn.hasAttribute("data-app-nofb")) return;
    if (btn.tagName === "A") {
      var href = btn.getAttribute("href") || "";
      if (href && href.charAt(0) !== "#") return;   // 跳转按钮不做就地反馈
    }
    if (btn.hasAttribute("data-job-feedback") || /开始|启动|安装|训练|校准/.test(btn.textContent)) fbStart(btn);
  }, true);

  /* ---------------- 提示：统一为模态通知（带遮罩、居中、需确认/自动关闭） ---------------- */
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    var m = r && r.message ? r.message : String(r || "");
    if (m) notify("未处理的异常：" + m, "err");
  });
  window.addEventListener("error", function (e) {
    if (e && e.error && e.error.message) notify("运行错误：" + e.error.message, "err");
  });

  var lastNotifyKey = "", lastNotifyAt = 0;
  function notify(msg, type) {
    if(pageLeaving)return;
    var text = String(msg == null ? "" : msg), key = (type || 'ok') + '|' + text;
    if (!text || (key === lastNotifyKey && Date.now() - lastNotifyAt < 1500)) return;
    lastNotifyKey = key; lastNotifyAt = Date.now();
    var host = el('toasts');
    if (!host) { host = document.createElement('div'); host.id = 'toasts'; modalHost().appendChild(host); }
    var item = document.createElement('div'); item.className = 'toast ' + (type || 'ok');
    item.setAttribute('role', type === 'err' ? 'alert' : 'status');
    var message = document.createElement('span'); message.textContent = text;
    var close = document.createElement('button'); close.type = 'button'; close.textContent = '关闭';
    close.setAttribute('aria-label', '关闭通知'); close.setAttribute('data-app-nofb', '');
    item.appendChild(message); item.appendChild(close); host.appendChild(item);
    while (host.children.length > 4) host.firstChild.remove();
    var timer;
    function dismiss() { clearTimeout(timer); item.remove(); }
    function schedule() { timer = setTimeout(dismiss, type === 'err' ? 12000 : 5000); }
    close.onclick = dismiss;
    item.onmouseenter = item.onfocusin = function () { clearTimeout(timer); };
    item.onmouseleave = item.onfocusout = schedule;
    schedule();
  }
  function toast(msg, type) { notify(msg, type); }

  /* localStorage 偏好：前端选择记忆 */
  function store(key, value) {
    try { localStorage.setItem("arm." + key, JSON.stringify(value)); } catch (e) { /* ignore */ }
  }
  function load(key, fallback) {
    try {
      var v = localStorage.getItem("arm." + key);
      return v === null ? fallback : JSON.parse(v);
    } catch (e) { return fallback; }
  }

  /* 统一风格的输入框：返回 Promise<string|null> */
  function promptModal(message, defaultValue, options) {
    if(pageLeaving)return Promise.resolve(null);
    var o = options || {};
    return new Promise(function (resolve) {
      var modalDoc = modalDocument();
      var mask = document.createElement("div");
      mask.className = "modal-mask";
      mask.innerHTML =
        '<div class="notify-modal" role="dialog" aria-modal="true">' +
          '<div class="notify-head"><span class="notify-title"></span></div>' +
          '<div class="notify-msg"></div>' +
          '<div class="field" style="padding:2px 18px 0"><input id="promptInput" type="text"></div>' +
          '<div class="notify-foot">' +
            '<button type="button" class="btn subtle" data-cancel>取消</button>' +
            '<button type="button" class="btn primary" data-ok>确定</button>' +
          '</div>' +
        '</div>';
      mask.querySelector(".notify-title").textContent = o.title || "请输入";
      mask.querySelector(".notify-msg").textContent = String(message || "");
      var input = mask.querySelector("#promptInput");
      input.value = defaultValue == null ? "" : String(defaultValue);
      var finished = false;
      function finish(v) {
        if (finished) return;
        finished = true;
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        modalDoc.removeEventListener("keydown", onKey);
        resolve(v);
      }
      function onKey(e) {
        if (e.key === "Escape") finish(null);
        else if (e.key === "Enter") finish(input.value);
      }
      mask.querySelector("[data-ok]").addEventListener("click", function () { finish(input.value); });
      mask.querySelector("[data-cancel]").addEventListener("click", function () { finish(null); });
      mask.addEventListener("click", function (e) { if (e.target === mask) finish(null); });
      modalDoc.addEventListener("keydown", onKey);
      modalDoc.body.appendChild(mask);
      try { input.focus(); input.select(); } catch (e) { /* ignore */ }
    });
  }

  /* 统一风格的确认框：返回 Promise<boolean> */
  function setupAutoSaveForms() {
    function debounce(fn, ms) {
      var t = null;
      return function () { clearTimeout(t); t = setTimeout(fn, ms); };
    }
    function bind(formId, section) {
      var f = el(formId);
      if (!f) return;
      var save = debounce(function () {
        try {
          API.post("/api/config", (function () {
            var body = {};
            body[section] = formData(f);
            return body;
          })()).catch(function () {});
        } catch (e) { /* ignore */ }
      }, 700);
      f.addEventListener("change", save);
      f.addEventListener("input", save);
    }
    bind("recordForm", "dataset");
    bind("evalForm", "eval");
    var af = el("armForm");
    if (af) {
      var saveArm = debounce(function () {
        var fol = af.elements.follower_id, can = af.elements.can_adapter,
            lea = af.elements.leader_id;
        var lp = el("leaderPort"), fp = el("followerPort");
        API.post("/api/config", {
          follower: { id: fol ? fol.value : "", can_adapter: can ? can.value : "",
                      port: fp ? fp.value : "" },
          leader: { id: lea ? lea.value : "", port: lp ? lp.value : "" }
        }).catch(function () {});
      }, 700);
      af.addEventListener("change", saveArm);
      af.addEventListener("input", saveArm);
      ["leaderPort", "followerPort"].forEach(function (id) {
        var sel = el(id);
        if (sel) sel.addEventListener("change", saveArm);
      });
    }
  }

  function setupColumnResizers() {
    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
    var app = q(".app");
    if (!app) return;
    // 变量挂到 <html> 上：列宽手柄在 body 下，才能和 .app 共用同一组变量
    var root = document.documentElement;
    var sideW = clamp(parseInt(localStorage.getItem("rebot-side-w") || "220", 10) || 220, 170, 360);
    var railW = clamp(parseInt(localStorage.getItem("rebot-rail-w") || "340", 10) || 340, 260, 560);
    root.style.setProperty("--side-w", sideW + "px");
    root.style.setProperty("--rail-w", railW + "px");

    function makeHandle(id, kind) {
      var h = document.createElement("div");
      h.className = "col-resizer";
      h.id = id;
      h.title = "拖动调整列宽";
      document.body.appendChild(h);
      var dragging = false;
      h.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        if (dragging) return;
        dragging = true;
        h.classList.add("dragging");
        var startX = ev.clientX;
        var startSide = sideW, startRail = railW;
        function move(e) {
          if (!dragging) return;
          if (kind === "side") {
            sideW = clamp(startSide + (e.clientX - startX), 170, 360);
            root.style.setProperty("--side-w", sideW + "px");
          } else {
            railW = clamp(startRail - (e.clientX - startX), 260, 560);
            root.style.setProperty("--rail-w", railW + "px");
          }
        }
        function up() {
          if (!dragging) return;
          dragging = false;
          h.classList.remove("dragging");
          document.removeEventListener("pointermove", move);
          document.removeEventListener("pointerup", up);
          localStorage.setItem("rebot-side-w", String(sideW));
          localStorage.setItem("rebot-rail-w", String(railW));
        }
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", up);
      });
    }

    makeHandle("resizeSide", "side");
    if (window.innerWidth > 1400) makeHandle("resizeRail", "rail");
  }

  function blockingModal(message, options) {
    var o = options || {};
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="notify-modal" style="width:min(440px,100%)" role="alertdialog" aria-modal="true">' +
        '<div class="notify-head"><span class="notify-title"></span></div>' +
        '<div class="notify-msg" style="display:flex;align-items:center;gap:12px">' +
          '<span class="spinner"></span><span class="blocking-text"></span>' +
        '</div>' +
      '</div>';
    mask.querySelector(".notify-title").textContent = o.title || "处理中";
    mask.querySelector(".blocking-text").textContent = String(message || "正在处理，请稍候…");
    modalHost().appendChild(mask);
    return {
      set: function (t) {
        var el = mask.querySelector(".blocking-text");
        if (el) el.textContent = String(t || "");
      },
      close: function () {
        if (mask.parentNode) mask.parentNode.removeChild(mask);
      }
    };
  }

  function confirmModal(message, options) {
    if(pageLeaving)return Promise.resolve(false);
    var o = options || {};
    return new Promise(function (resolve) {
      var modalDoc = modalDocument();
      var mask = document.createElement("div");
      mask.className = "modal-mask";
      mask.innerHTML =
        '<div class="notify-modal confirm" role="dialog" aria-modal="true">' +
          '<div class="notify-head"><span class="notify-title"></span></div>' +
          '<div class="notify-msg"></div>' +
          '<div class="notify-foot">' +
            '<button type="button" class="btn subtle" data-cancel>取消</button>' +
            '<button type="button" class="btn danger" data-ok>确认</button>' +
          '</div>' +
        '</div>';
      mask.querySelector(".notify-title").textContent = o.title || "请确认";
      mask.querySelector(".notify-msg").textContent = String(message || "");
      var previousFocus = document.activeElement;
      var finished = false;
      function finish(v) {
        if (finished) return;
        finished = true;
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        modalDoc.removeEventListener("keydown", onKey);
        if (previousFocus && previousFocus.isConnected) previousFocus.focus();
        resolve(v);
      }
      function onKey(e) {
        if (e.key === "Escape") finish(false);
        if (e.key === "Tab") {
          var first = mask.querySelector('[data-cancel]'), last = mask.querySelector('[data-ok]');
          if (e.shiftKey && modalDoc.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && modalDoc.activeElement === last) { e.preventDefault(); first.focus(); }
        }

      }
      mask.querySelector("[data-ok]").addEventListener("click", function () { finish(true); });
      mask.querySelector("[data-cancel]").addEventListener("click", function () { finish(false); });
      mask.addEventListener("click", function (e) { if (e.target === mask) finish(false); });
      modalDoc.addEventListener("keydown", onKey);
      modalDoc.body.appendChild(mask);
      try { mask.querySelector("[data-cancel]").focus(); } catch (e) { /* ignore */ }
    });
  }
  function setAlert(target, msg, type) {
    var node = typeof target === "string" ? q(target) : target;
    if (!node) return;
    var t = type || "err";
    node.className = "alert " + t;
    node.textContent = String(msg || "");
    node.setAttribute("role", t === "err" ? "alert" : "status");
  }
  function clearAlert(target) {
    var node = typeof target === "string" ? q(target) : target;
    if (!node) return;
    node.className = "alert";
    node.textContent = "";
  }
  var clickedAction = null;
  document.addEventListener('click', function(e){
    var b=e.target.closest&&e.target.closest('button');
    if(!b)return;
    if(b.dataset.actionBusy){e.preventDefault();e.stopImmediatePropagation();return;}
    clickedAction=b;setTimeout(function(){if(clickedAction===b)clickedAction=null;},0);
  },true);
  async function action(fn, alertTarget) {
    var trigger=clickedAction, elapsedTimer=null, actionStarted=Date.now();
    if(trigger){trigger.dataset.actionBusy='true';trigger.setAttribute('aria-busy','true');trigger.classList.add('action-pending');
      elapsedTimer=setInterval(function(){trigger.dataset.waiting=Math.floor((Date.now()-actionStarted)/1000)+'s';},1000);}

    clearAlert(alertTarget);
    fbResultMsg = "";
    try {
      var r = await fn();
      if (fbActive && !fbIdleTimer && !jobRunning()) fbFinish("ok", fbResultMsg || "");
      return r;
    } catch (e) {
      var msg = (e && e.message) ? e.message : String(e);
      toast(msg, "err");
      setAlert(alertTarget, msg, "err");
      fbResultMsg = msg;
      fbFinish("err", msg);
      return null;
    } finally {
      clearInterval(elapsedTimer);
      if(trigger){delete trigger.dataset.actionBusy;delete trigger.dataset.waiting;trigger.removeAttribute('aria-busy');trigger.classList.remove('action-pending');}
    }
  }
  function ok(msg, alertTarget) {
    if (alertTarget) setAlert(alertTarget, msg, "ok");
    toast(msg, "ok");
    fbResultMsg = String(msg || "");
    if (fbActive && !fbIdleTimer && !jobRunning()) fbSet(fbActive.box, "完成 · " + fbResultMsg, "ok");
  }

  /* ---------------- 数据加载状态框：加载中 / 失败 / 空数据 ----------------
     任何依赖后端接口的数据区域，在结果返回前都显示状态框，绝不留下空白。
     支持普通容器、表格 tbody、下拉 select，以及 attach（不覆盖原有表单内容）。
     -------------------------------------------------------------------- */
  var retryHandlers = new WeakMap();
  var stateSeq = 0;
  function stateNode(t) {
    return typeof t === "string" ? (el(t) || q(t)) : t;
  }
  function stateBox(kind, opts) {
    opts = opts || {};
    var iconHtml = kind === "loading"
      ? '<span class="state-spin" aria-hidden="true"></span>'
      : '<span class="state-ico">' + icon(kind === "error" ? "alert" : "inbox") + "</span>";
    var retry = ((kind === "error" || kind === "empty") && opts.retry)
      ? '<button type="button" class="btn sm" data-app-retry>' + esc(opts.retryLabel || "重试加载") + "</button>"
      : "";
    var skeleton = "";
    if (kind === "loading" && opts.rows !== 0 && !opts.attach) {
      var n = opts.rows || 3;
      for (var i = 0; i < n; i++) {
        skeleton += '<div class="state-skeleton" style="width:' + Math.max(38, 96 - i * 16) + '%"></div>';
      }
    }
    var defaultTitle = kind === "loading" ? "正在加载数据…"
      : (kind === "error" ? "数据加载失败" : "暂无数据");
    return '<div class="state-box ' + kind + (opts.attach ? " compact" : "") +
      '" data-app-state="' + kind + '" role="status">' +
      '<div class="state-head">' + iconHtml +
        '<div class="state-text"><strong>' + esc(opts.title || defaultTitle) + "</strong>" +
        (opts.detail ? "<small>" + esc(opts.detail) + "</small>" : "") +
        "</div>" + retry + "</div>" +
      (skeleton ? '<div class="state-skeleton-wrap">' + skeleton + "</div>" : "") +
      "</div>";
  }
  function stateColspan(node, opts) {
    if (opts && opts.colspan) return opts.colspan;
    var table = node.closest ? node.closest("table") : null;
    var n = table ? table.querySelectorAll("thead th").length : 0;
    if (!n) {
      var tr = node.querySelector("tr");
      n = tr ? tr.children.length : 1;
    }
    return Math.max(1, n);
  }
  function bindRetry(box, fn) {
    if (!box || !fn || !box.querySelector) return;
    var btn = q("[data-app-retry]", box);
    if (btn) retryHandlers.set(btn, fn);
  }
  function selectState(node, kind, opts) {
    var key = node.dataset.stateKey || ("st" + (++stateSeq));
    node.dataset.stateKey = key;
    if (node.classList) node.classList.toggle("is-loading", kind === "loading");
    var label = kind === "loading" ? "正在加载…"
      : (kind === "error" ? "加载失败，请点下方重试" : "（暂无数据）");
    node.innerHTML = '<option value="">' + esc(label) + "</option>";
    var host = node.parentNode;
    if (!host) return;
    var old = q('.state-box[data-state-for="' + key + '"]', host);
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var tmp = document.createElement("div");
    tmp.innerHTML = stateBox(kind, {
      title: opts.title, detail: opts.detail, attach: true, rows: 0,
      retry: opts.retry, retryLabel: opts.retryLabel
    });
    var box = tmp.firstChild;
    box.setAttribute("data-state-for", key);
    if (node.nextSibling) host.insertBefore(box, node.nextSibling);
    else host.appendChild(box);
    bindRetry(box, opts.retry);
  }
  function putState(node, kind, opts) {
    opts = opts || {};
    if (node.tagName === "TBODY") {
      node.innerHTML = '<tr class="state-row"><td colspan="' + stateColspan(node, opts) + '">' +
        stateBox(kind, opts) + "</td></tr>";
      bindRetry(q("[data-app-state]", node), opts.retry);
      return;
    }
    if (node.tagName === "SELECT") { selectState(node, kind, opts); return; }
    var box;
    if (opts.attach) {
      qa("[data-app-state]", node).forEach(function (n) {
        if (n.parentNode) n.parentNode.removeChild(n);
      });
      node.insertAdjacentHTML("afterbegin", stateBox(kind, opts));
      box = node.firstElementChild;
    } else {
      node.innerHTML = stateBox(kind, opts);
      box = node.firstElementChild;
    }
    bindRetry(box, opts.retry);
  }
  function showLoading(target, opts) {
    if (typeof opts === "string") opts = { title: opts };
    opts = opts || {};
    var node = stateNode(target);
    if (!node) return;
    putState(node, "loading", {
      title: opts.title || "正在加载数据…",
      detail: opts.detail === undefined ? "正在向后端请求真实数据，请稍候" : opts.detail,
      rows: opts.rows, colspan: opts.colspan, attach: opts.attach,
      retry: opts.retry, retryLabel: opts.retryLabel
    });
  }
  function loadError(target, err, retry, opts) {
    opts = opts || {};
    var node = stateNode(target);
    if (!node) return;
    var msg = err && err.message ? err.message : String(err || "未知错误");
    notify(msg, "err");
    var fn = retry || opts.retry || function () { location.reload(); };
    putState(node, "error", {
      title: opts.title || "数据加载失败",
      detail: msg,
      retry: fn,
      retryLabel: opts.retryLabel || (retry || opts.retry ? "重试加载" : "刷新页面重试"),
      colspan: opts.colspan, attach: opts.attach, rows: 0
    });
  }
  function loadEmpty(target, title, detail, retry) {
    var node = stateNode(target);
    if (!node) return;
    putState(node, "empty", {
      title: title || "暂无数据", detail: detail || "", rows: 0,
      retry: retry, retryLabel: retry ? "重新加载" : ""
    });
  }
  function clearState(target) {
    var node = stateNode(target);
    if (!node) return;
    if (node.tagName === "TBODY") {
      qa("tr.state-row", node).forEach(function (r) {
        if (r.parentNode) r.parentNode.removeChild(r);
      });
    } else {
      qa("[data-app-state]", node).forEach(function (n) {
        if (n.parentNode) n.parentNode.removeChild(n);
      });
    }
    if (node.classList) node.classList.remove("is-loading");
    if (node.parentNode && node.dataset && node.dataset.stateKey) {
      var box = q('.state-box[data-state-for="' + node.dataset.stateKey + '"]', node.parentNode);
      if (box && box.parentNode) box.parentNode.removeChild(box);
    }
  }
  function waitingLog(node, title, detail) {
    if (!node) return;
    putState(node, "empty", {
      title: title || "等待日志输出",
      detail: detail || "执行任意操作后，真实终端输出会实时显示在这里", rows: 0
    });
  }
  /* 首屏兜底：外壳一渲染完就给所有待填充的数据容器放加载框，
     页面脚本随后会用更具体的文案 / 真实数据 / 错误框替换它，绝不出现空白。 */
  function presetStates() {
    var cards = [
      ["#kvEnv", "正在加载运行环境…"], ["#kvArm", "正在加载机械臂连接状态…"],
      ["#kvJob", "正在加载当前任务…"], ["#kvStatus", "正在读取运行状态…"],
      ["#engineCards", "正在检测训练引擎…"], ["#camGrid", "正在连接摄像头画面…"],
      ["#envLocationBox", "正在检测可用创建位置…"],
      ["#lerobotCard", "正在检测 LeRobot 是否已安装…"]
    ];
    cards.forEach(function (p) {
      var n = q(p[0]);
      if (n && !n.childNodes.length) showLoading(n, { title: p[1], rows: 2 });
    });
    ["#permTable tbody", "#portTable tbody", "#dsTable tbody",
     "#cmdTable tbody", "#ckptTable tbody", "#camTable tbody"].forEach(function (s) {
      var n = q(s);
      if (n && !n.querySelector("tr.state-row")) showLoading(n, { rows: 2 });
    });
    ["#envSelect", "#leaderPort", "#followerPort", "#dsPicker"].forEach(function (s) {
      var n = q(s);
      if (n && !n.querySelector("option:not(:first-child)")) {
        showLoading(n, { title: "正在加载可选数据…" });
      }
    });
    /* 配置表单：不覆盖输入控件，在表单顶部挂一条加载状态 */
    ["#armForm", "#recordForm", "#evalForm", "#setupForm"].forEach(function (s) {
      var n = q(s);
      if (n && !n.querySelector("[data-app-state]")) {
        showLoading(n, {
          title: "正在读取配置…", detail: "正在读取 config.json 的真实配置", attach: true
        });
      }
    });
    var lp = q("#logPanel");
    if (lp && !lp.childNodes.length) {
      showLoading(lp, {
        title: "正在连接日志通道…",
        detail: "执行任意操作后，真实终端输出会实时显示在这里", rows: 0
      });
    }
  }

  /* 状态框里的「重试」按钮不触发按钮就地反馈，避免误显示成操作进度 */
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest ? e.target.closest("[data-app-retry]") : null;
    if (!b) return;
    e.preventDefault();
    var fn = retryHandlers.get(b);
    try {
      if (fn) fn(); else location.reload();
    } catch (err) {
      toast(err && err.message ? err.message : String(err), "err");
    }
  });

  /* 兜底：页面脚本出现未捕获异常时，把仍在空转的加载框改成可刷新的错误框 */
  window.addEventListener("error", function (e) {
    if (!e || !e.message) return;   // 图片等资源加载错误不在此处理
    qa(".state-box.loading[data-app-state]").forEach(function (box) {
      var compact = box.classList.contains("compact");
      box.setAttribute("data-app-state", "error");
      box.className = "state-box error" + (compact ? " compact" : "");
      var spin = q(".state-spin", box);
      if (spin) {
        spin.outerHTML = '<span class="state-ico">' + icon("alert") + "</span>";
      }
      var strong = q(".state-text strong", box), small = q(".state-text small", box);
      if (strong) strong.textContent = "数据加载失败";
      if (small) small.textContent = "页面脚本异常：" + e.message + "，请刷新页面重试";
      var head = q(".state-head", box);
      if (head && !q("[data-app-retry]", head)) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn sm";
        btn.textContent = "刷新页面";
        btn.setAttribute("data-app-retry", "");
        retryHandlers.set(btn, function () { location.reload(); });
        head.appendChild(btn);
      }
      var sk = q(".state-skeleton-wrap", box);
      if (sk && sk.parentNode) sk.parentNode.removeChild(sk);
    });
  });

  /* ---------------- 客户端（当前浏览器）独立配置 ----------------
     服务端只有一份真机状态：串口 / 摄像头 / conda / 数据集 / 训练进程都属于
     运行 python start.py 的那台机器。这里的设置只写进当前浏览器的 localStorage，
     不上传服务器、不写入 config.json，也不会影响另一台设备打开的页面。 */
  var CLIENT_KEY = "arm.client.v1";
  function clientPrefs() {
    var d = {};
    try { d = JSON.parse(localStorage.getItem(CLIENT_KEY) || "{}") || {}; }
    catch (e) { d = {}; }
    return {
      alias: typeof d.alias === "string" ? d.alias.slice(0, 40) : "",
      showBar: d.showBar !== false,
      density: d.density === "compact" ? "compact" : "normal",
      lastHost: typeof d.lastHost === "string" ? d.lastHost : ""
    };
  }
  function setClientPrefs(patch) {
    var d = clientPrefs();
    Object.keys(patch || {}).forEach(function (k) { d[k] = patch[k]; });
    try { localStorage.setItem(CLIENT_KEY, JSON.stringify(d)); } catch (e) {}
    applyClientPrefs(d);
    return d;
  }
  function applyClientPrefs(d) {
    d = d || clientPrefs();
    document.body.classList.toggle("density-compact", d.density === "compact");
    var bar = el("runtimeBar");
    if (bar) bar.hidden = !d.showBar;
    var note = el("runtimeNote");
    if (note) note.hidden = !d.showBar;
  }
  applyClientPrefs();

  /* ---------------- 主题：亮 / 暗 / 跟随系统 ---------------- */
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function themePref() {
    try { return localStorage.getItem(THEME_KEY) || "auto"; }
    catch (e) { return "auto"; }
  }
  function applyTheme(pref) {
    var dark = pref === "dark" || (pref === "auto" && media && media.matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    themeCallbacks.forEach(function (cb) { try { cb(dark ? "dark" : "light"); } catch (e) {} });
  }
  function setTheme(pref) {
    try { localStorage.setItem(THEME_KEY, pref); } catch (e) {}
    applyTheme(pref);
  }
  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (themePref() === "auto") applyTheme("auto");
    });
  }
  applyTheme(themePref());

  /* ---------------- 状态轮询 ---------------- */
  /* 服务端系统：Windows 使用 COM 端口，没有 sudo / chmod，相关 UI 自动隐藏 */
  var serverIsWindows = false;
  var dualMode = false;
  function applyPlatformUI() {
    if (!serverIsWindows) return;
    ["railSudo", "railChmod", "railChmodUsb"].forEach(function (id) {
      var n = el(id);
      if (n) n.style.display = "none";
    });
    var b = el("railCheckPerm");
    if (b) b.textContent = "串口状态";
    var t = el("railSerialTitle");
    if (t) t.textContent = "串口（COM 端口）";
  }
  function notifyStatus(s) {
    appStatus = s;
    if (s && typeof s.is_windows === "boolean") {
      serverIsWindows = s.is_windows;
      applyPlatformUI();
    }
    if (s && s.dual_arm && typeof s.dual_arm.enabled === "boolean") {
      dualMode = s.dual_arm.enabled;
      paintRobotContext(s);
      var rt = el("railSerialTitle");
      if (rt && dualMode) rt.textContent = "串口（双臂四路）";
    }
    setJobProgress(fromStatus(s), null, { finish: !!(s && s.running === false) });
    statusCallbacks.forEach(function (cb) { try { cb(s); } catch (e) {} });
  }
  var statusFlight = null;
  function refreshStatus(){if(statusFlight)return statusFlight;statusFlight=readStatus().finally(function(){statusFlight=null;});return statusFlight;}
  async function readStatus() {
    try {
      var s = await API.get("/api/status", 8000);
      statusFailed = false;
      notifyStatus(s);
      var ce = el("chipEnv"), cp = el("chipPort"), cj = el("chipJob");
      if (ce) {
        ce.textContent = s.env_ready
          ? (s.active_env ? ("环境 " + s.active_env)
            : (s.lerobot_installed ? "LeRobot 已装 · 直接复用" : "环境已就绪"))
          : "未选择环境";
        ce.className = "chip " + (s.env_ready ? "ok" : "warn");
      }
      if (cp) {
        var linked = !!(s.connected || s.dual_connected);
        cp.textContent = s.dual_connected ? "双臂已连接"
          : (s.connected ? "机械臂已连接" : (dualMode ? "双臂未连接" : "机械臂未连接"));
        cp.className = "chip " + (linked ? "ok" : "warn");
      }
      if (cj) {
        var j = s.job || {};
        cj.textContent = s.running
          ? ("运行中 " + (j.step_total ? "[" + j.step + "/" + j.step_total + "] " : "") + (j.name || ""))
          : "空闲";
        cj.className = "chip " + (s.running ? "warn" : "");
      }
      return s;
    } catch (e) {
      if (!statusFailed) {
        statusFailed = true;
        toast("状态获取失败：" + e.message, "warn");
      }
      ["chipEnv", "chipPort", "chipJob"].forEach(function (id) {
        var c = el(id);
        if (c) { c.textContent = "服务不可用"; c.className = "chip err"; }
      });
      return null;
    }
  }

  /* ---------------- WebSocket 日志总线（连不上自动降级为轮询） ---------------- */
  var wsFailures = 0, logPollTimer = null, lastLogTs = 0, wsRetryTimer = null;
  function onLog(cb) {
    var h = function (e) { cb(e.detail); };
    document.addEventListener("arm:log", h);
    return function () { document.removeEventListener("arm:log", h); };
  }
  function emitLog(ev) {
    LOG_HISTORY.push(ev);
    if (LOG_HISTORY.length > 2000) LOG_HISTORY.shift();
    document.dispatchEvent(new CustomEvent("arm:log", { detail: ev }));
    if (ev.type === "state") applyStateEvent(ev);
    fbOnEvent(ev);
    if (ev.type === "state" && (!ev.ts || Number(ev.ts)>=BOOT_TS) && (ev.running!==undefined || ev.connected!==undefined || ev.dual_connected!==undefined)) refreshStatus();
  }
  function stopLogPolling() {
    if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; }
  }
  function startLogPolling() {
    if (logPollTimer) return;
    toast("实时日志连接不可用，已切换为轮询模式（请确认已安装 websockets）", "warn");
    logPollTimer = setInterval(async function () {
      try {
        var r = await API.get("/api/logs?after=" + lastLogTs, 6000);
        (r.events || []).forEach(function (ev) {
          lastLogTs = Math.max(lastLogTs, Number(ev.ts) || 0);
          emitLog(ev);
        });
      } catch (e) { /* 轮询失败不刷屏，等待下一次 */ }
    }, 2500);
  }
  function scheduleLogRetry() {
    if (wsRetryTimer) clearTimeout(wsRetryTimer);
    wsRetryTimer = setTimeout(connectLogs, 5000);
  }
  function connectLogs() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var ws;
    try { ws = new WebSocket(proto + "://" + location.host + "/ws/logs"); }
    catch (e) {
      wsFailures++;
      if (wsFailures >= 2) startLogPolling();
      scheduleLogRetry();
      return;
    }
    ws.onmessage = function (e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      emitLog(ev);
    };
    ws.onopen = function () {
      wsFailures = 0;
      if (logPollTimer) { stopLogPolling(); toast("实时日志已恢复", "ok"); }
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
    ws.onclose = function () {
      wsFailures++;
      if (wsFailures >= 2) startLogPolling();
      scheduleLogRetry();
    };
  }
  function attachLogPanel(node, opts) {
    if (!node) return function () {};
    opts = opts || {};
    var max = opts.max || 400;
    var filter = opts.filter || function () { return true; };
    function clearWaiting() {
      qa("[data-app-state]", node).forEach(function (n) {
        if (n.parentNode) n.parentNode.removeChild(n);
      });
    }
    function append(ev) {
      if (ev.type !== "log" || !filter(ev)) return;
      clearWaiting();   // 首条真实日志到达时移除「等待日志」状态框
      var line = document.createElement("span");
      line.className = "l " + (ev.level || "info");
      var t = new Date((ev.ts || Date.now() / 1000) * 1000);
      var stamp = t.toLocaleTimeString("zh-CN", { hour12: false });
      line.innerHTML = '<span class="t">' + esc(stamp) + "</span>" + esc(ev.line || "");
      node.appendChild(line);
      while (node.childNodes.length > max) node.removeChild(node.firstChild);
      if (node.dataset.autoscroll !== "off") node.scrollTop = node.scrollHeight;
    }
    LOG_HISTORY.forEach(append);
    // 没有历史日志时，把首屏「正在连接日志通道」加载框收尾为等待框
    if (!node.querySelector(".l")) { clearWaiting(); waitingLog(node); }
    if (el("logAuto")) {
      el("logAuto").onchange = function () {
        node.dataset.autoscroll = this.checked ? "on" : "off";
      };
    }
    return onLog(append);
  }

  /* ---------------- 表单 ---------------- */
  function formData(form) {
    var out = {};
    qa("[name]", form).forEach(function (f) {
      if (!f.name || f.disabled) return;
      if (f.type === "checkbox") out[f.name] = f.checked;
      else if (f.type === "number" || f.dataset.type === "number") {
        out[f.name] = f.value === "" ? 0 : Number(f.value);
      } else out[f.name] = f.value;
    });
    return out;
  }
  function fillForm(form, data) {
    Object.keys(data || {}).forEach(function (k) {
      var f = q('[name="' + k + '"]', form);
      if (!f) return;
      if (f.type === "checkbox") f.checked = Boolean(data[k]);
      else f.value = (data[k] === undefined || data[k] === null) ? "" : String(data[k]);
    });
  }
  function click(id, fn) {
    var n = el(id);
    if (n) n.addEventListener("click", fn);
  }
  function change(id, fn) {
    var n = el(id);
    if (n) n.addEventListener("change", fn);
  }

  /* ---------------- conda 虚拟环境「创建位置」：可选可输入的组合框 ----------------
     同一个输入框既能直接输入绝对路径，也能从下拉里选 1–2 个常用位置；
     候选来自后端真实文件系统（/api/env/locations），不再提供目录浏览。 */
  var envLocCtl = null;
  var DEFAULT_LOC_LABEL = "conda 默认位置";
  function joinEnvPath(base, name) {
    var b = String(base || "").replace(/[\\/]+$/, "");
    if (!b) return name;
    var sep = b.indexOf("\\") >= 0 && b.indexOf("/") < 0 ? "\\" : "/";
    return b + sep + name;
  }
  function normEnvPath(p) {
    return String(p || "").replace(/[\\/]+$/, "").replace(/\\/g, "/").toLowerCase();
  }
  function mountEnvLocation(boxId, opts) {
    opts = opts || {};
    var box = el(boxId);
    if (!box) return null;
    var nameInput = opts.nameInput ? el(opts.nameInput) : null;
    var locData = null, loading = true;
    box.innerHTML =
      '<div class="env-loc">' +
        '<div class="row">' +
          '<div class="field wide">' +
            '<label>创建位置（环境目录的父目录，可直接输入）</label>' +
            '<div class="combo" id="envLocationCombo">' +
              '<span class="combo-lead">' + icon("folder") + '</span>' +
              '<input id="envLocationInput" class="combo-input" autocomplete="off" spellcheck="false" ' +
                'placeholder="留空 = conda 默认位置；也可直接输入绝对路径">' +
              '<button type="button" class="combo-caret" id="btnEnvLocToggle" data-app-nofb ' +
                'aria-expanded="false" aria-label="展开常用创建位置" title="常用创建位置">' +
                icon("chevron") + '</button>' +
              '<div class="combo-menu" id="envLocationMenu" hidden></div>' +
            '</div>' +
          '</div>' +
          '<div class="btn-row">' +
            '<button type="button" class="btn icon" id="btnRefreshLoc" data-app-nofb ' +
              'title="重新检测创建位置" aria-label="重新检测创建位置" ' +
              'data-label="重新检测创建位置">' + icon("refresh") + '</button>' +
          '</div>' +
        '</div>' +
        '<div class="env-loc-state" id="envLocationState"></div>' +
        '<div class="hint env-loc-preview" id="envLocationPreview">正在检测创建位置…</div>' +
      '</div>';
    var input = el("envLocationInput"), combo = el("envLocationCombo"),
        caret = el("btnEnvLocToggle"), menu = el("envLocationMenu"),
        state = el("envLocationState"), preview = el("envLocationPreview");

    function currentName() { return (nameInput && nameInput.value.trim()) || "lerobot"; }
    function isDefault() {
      var v = input.value.trim();
      return !v || v === DEFAULT_LOC_LABEL || v === "__default__";
    }
    function currentBase() { return isDefault() ? "" : input.value.trim(); }
    function updatePreview() {
      if (!preview) return;
      if (loading) { preview.textContent = "正在检测创建位置…"; return; }
      var base = currentBase();
      var def = (locData && locData.default) || "";
      var path = base ? joinEnvPath(base, currentName())
        : (def ? joinEnvPath(def, currentName()) : "由 conda 决定");
      preview.innerHTML = (base ? "环境将创建到：" : "环境交由 conda 放到默认 envs 目录（预览）：") +
        "<code>" + esc(path) + "</code>" +
        (base ? " · 使用 <code>conda create -p</code> 精确落盘"
              : " · 使用 <code>conda create -n</code>") +
        (locData && !locData.conda_found
          ? ' · <span class="badge err">未检测到 conda，请先安装 Miniforge</span>' : "");
    }
    function closeMenu() {
      menu.hidden = true;
      caret.setAttribute("aria-expanded", "false");
      combo.classList.remove("open");
    }
    function openMenu() {
      menu.hidden = false;
      caret.setAttribute("aria-expanded", "true");
      combo.classList.add("open");
    }
    function renderMenu() {
      var items = [{
        value: "", label: DEFAULT_LOC_LABEL, ico: "layers",
        desc: (locData && locData.default) || "由 conda 决定，留空即可"
      }];
      var second = null;
      if (locData) {
        var saved = String(locData.saved || "").trim();
        if (saved && normEnvPath(saved) !== normEnvPath(locData.default)) {
          second = { value: saved, label: "上次使用的创建位置", ico: "folder", desc: saved };
        } else if (locData.project_root) {
          var proj = joinEnvPath(locData.project_root, "envs");
          second = { value: proj, label: "项目目录 envs", ico: "folder",
                     desc: proj + " · 便于随项目迁移" };
        }
      }
      if (second) items.push(second);
      menu.innerHTML = '<div class="combo-menu-head">常用创建位置</div>' +
        items.map(function (it) {
          return '<button type="button" class="combo-opt" data-loc="' + esc(it.value) +
            '" data-app-nofb><span class="combo-opt-ico">' + icon(it.ico) + '</span>' +
            '<span class="combo-opt-text"><strong>' + esc(it.label) + '</strong>' +
            '<small>' + esc(it.desc) + '</small></span></button>';
        }).join("") +
        '<div class="combo-tip">其它位置直接在上方输入框填写绝对路径，无需切换模式。</div>';
    }
    async function loadLocations() {
      loading = true;
      closeMenu();
      showLoading(state, {
        title: "正在检测创建位置…",
        detail: "读取后端 conda envs_dirs 与真实存在的目录", rows: 0,
        retry: loadLocations
      });
      updatePreview();
      try {
        var r = await API.get("/api/env/locations", 15000);
        locData = r;
        clearState(state);
        renderMenu();
        var saved = String(r.saved || "").trim();
        // 恢复上次位置，但用户已经手动输入过的内容不覆盖
        if (saved && !input.value.trim()) input.value = saved;
        loading = false;
        ctl.ready = true;
        updatePreview();
        input.dispatchEvent(new Event("input", { bubbles: true }));
      } catch (e) {
        loading = false;
        // 接口失败也不能把用户卡死：输入框始终可用，只用错误框提示并提供重试
        renderMenu();
        loadError(state, e, loadLocations, { title: "创建位置检测失败", rows: 0 });
        updatePreview();
      }
    }
    var ctl = {
      ready: false,
      value: function () {
        var raw = input.value.trim();
        if (!raw || raw === DEFAULT_LOC_LABEL || raw === "__default__") {
          return { location: "", mode: "default", target: "" };
        }
        if (!/^([A-Za-z]:[\\/]|\/)/.test(raw)) {
          throw new Error("创建位置请填写绝对路径，例如 /home/user/conda_envs 或 D:/conda/envs");
        }
        return { location: raw, mode: "path", target: joinEnvPath(raw, currentName()) };
      },
      reload: loadLocations,
      data: function () { return locData; }
    };
    envLocCtl = ctl;
    input.addEventListener("input", updatePreview);
    input.addEventListener("change", updatePreview);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.key === "Enter") closeMenu();
    });
    if (nameInput) nameInput.addEventListener("input", updatePreview);
    caret.addEventListener("click", function () {
      if (menu.hidden) { openMenu(); try { input.focus(); } catch (e) {} }
      else closeMenu();
    });
    menu.addEventListener("click", function (e) {
      var b = e.target && e.target.closest ? e.target.closest("[data-loc]") : null;
      if (!b) return;
      input.value = b.getAttribute("data-loc") || "";
      closeMenu();
      updatePreview();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      try { input.focus(); } catch (e2) {}
    });
    document.addEventListener("click", function (e) {
      if (!menu.hidden && !combo.contains(e.target)) closeMenu();
    });
    click("btnRefreshLoc", loadLocations);
    loadLocations();
    return ctl;
  }

  /* ---------------- LeRobot 已安装检测卡（向导 / 环境安装共用） ---------------- */
  function mountLerobotCard(boxId, opts) {
    opts = opts || {};
    var box = el(boxId);
    if (!box) return null;
    var data = null, installing = false;

    /* 全站唯一的安装入口：检测卡里的按钮直接发起真实安装，不再与页面下方
       另一个「一键安装 LeRobot」重复，也不再靠跳转到 /p/setup 触发。 */
    async function runInstall(force) {
      if (installing) return;
      installing = true;
      try {
        await action(async function () {
          clearAlert(opts.alert);
          var r = await API.post("/api/setup/install-lerobot", { force: !!force }, 45000);
          if (r.already_installed) {
            ok(r.message || "检测到已安装 LeRobot，直接复用，无需重新安装", opts.alert);
            refreshStatus();
            await load(true);
            return;
          }
          var info = data || {};
          ok((force ? "强制重新安装已启动" : "LeRobot 安装已启动") +
            (info.installed ? "（复用主程序，仅补缺失依赖）" : "（清华源 + gh-proxy）"), opts.alert);
          refreshStatus();
        }, opts.alert);
      } finally {
        installing = false;
      }
    }

    function lines(info) {
      var out = [
        ["来源", info.source || "-"],
        ["Python", info.python || "-"],
        ["lerobot 模块", info.module || "-"],
        ["lerobot-train", info.cli || "未找到（可执行安装后重新检测）"]
      ];
      if (info.missing && info.missing.length) {
        out.push(["待补依赖", info.missing.join(", ")]);
      }
      return '<div class="lero-lines">' + out.map(function (p) {
        return "<div><span>" + esc(p[0]) + "</span><code>" + esc(p[1]) + "</code></div>";
      }).join("") + "</div>";
    }

    function render(info) {
      data = info;
      clearState(box);
      box.className = "lerobot-box";
      if (info.installed) {
        box.innerHTML =
          '<div class="lero-title"><span class="lero-ico">' + icon("package") + '</span>' +
            '<span class="badge ok">已安装 · 可直接复用</span>' +
            '<span class="muted">lerobot ' + esc(info.version || "") + "</span></div>" +
          lines(info) +
          '<div class="btn-row">' +
            '<button class="btn primary" data-lero="use">' + icon("arrow") +
              '直接使用已安装的 LeRobot</button>' +
            '<button class="btn subtle icon" data-lero="recheck" data-app-nofb ' +
              'title="重新检测 LeRobot" aria-label="重新检测 LeRobot" ' +
              'data-label="重新检测 LeRobot">' + icon("refresh") + '</button>' +
            '<button class="btn danger" data-lero="force">' + icon("download") +
              '强制重新安装</button>' +
          "</div>" +
          '<div class="hint">' + (info.missing && info.missing.length
            ? "主程序已装好，不会重复克隆仓库；点「一键安装 LeRobot」只会补齐上面缺失的依赖。"
            : "依赖齐全，无需再执行安装；平台所有 lerobot-* 命令都会使用这个环境。") + "</div>";
      } else {
        box.innerHTML =
          '<div class="lero-title"><span class="lero-ico">' + icon("package") + '</span>' +
            '<span class="badge warn">未检测到 LeRobot</span>' +
            '<span class="muted">已检查 ' + ((info.checked || []).length) + " 个真实 Python 环境</span></div>" +
          '<div class="note">' + esc(info.note || "没有找到 lerobot 模块") +
            "。若装在自定义 venv 里，请让后端进程能访问该环境后重新检测；" +
            "否则点「一键安装 LeRobot」走完整安装链（gh-proxy 克隆 + 清华源）。</div>" +
          '<div class="btn-row">' +
            '<button class="btn primary" data-lero="install">' + icon("download") +
              '一键安装 LeRobot</button>' +
            '<button class="btn subtle icon" data-lero="recheck" data-app-nofb ' +
              'title="重新检测 LeRobot" aria-label="重新检测 LeRobot" ' +
              'data-label="重新检测 LeRobot">' + icon("refresh") + '</button>' +
          "</div>";
      }
      qa('[data-lero]', box).forEach(function (b) {
        b.onclick = function () {
          var act = b.dataset.lero;
          if (act === "recheck") { load(true); return; }
          if (act === "install") { runInstall(false); return; }
          if (act === "force") { runInstall(true); return; }
          action(async function () {
            var r = await API.post("/api/lerobot/use", {}, 45000);
            ok("已直接复用：" + ((r.lerobot && r.lerobot.source) || "已安装的 LeRobot"),
              opts.alert);
            refreshStatus();
            if (opts.onUse) opts.onUse();
            await load(true);
          }, opts.alert);
        };
      });
    }

    async function load(force) {
      showLoading(box, {
        title: "正在检测 LeRobot 是否已安装…",
        detail: "并行检查 conda 环境 / 当前 Python / PATH 的真实 site-packages 与发行包元数据",
        rows: 2
      });
      try {
        var r = await API.get("/api/lerobot/detect" + (force ? "?force=1" : ""), 45000);
        render(r.lerobot || {});
      } catch (e) {
        loadError(box, e, function () { load(true); });
        if (opts.alert) setAlert(opts.alert, e.message, "err");
      }
    }

    load(false);
    return { reload: load, value: function () { return data; } };
  }

  /* ---------------- Loss 折线图 ---------------- */
  function lossSeries(limit) {
    var points=[],run=null,retired=new Set(),lastStep=-1,lastTs=0,unit='step';
    function begin(id){
      if(!id||id===run||retired.has(id))return false;
      if(run)retired.add(run);if(retired.size>20)retired.delete(retired.values().next().value);
      run=id;points=[];lastStep=-1;lastTs=0;unit='step';return true;
    }
    return {begin:begin,clear:function(){points=[];},points:function(){return points.slice();},unit:function(){return unit;},
      add:function(ev){
        if(ev.type!=='loss'||typeof ev.value!=='number'||!Number.isFinite(ev.value)||ev.kind&&!ev.kind.startsWith('train'))return false;
        if(ev.job_id&&retired.has(ev.job_id))return false;
        var step=Number(ev.step),nextUnit=ev.step_unit||'step';
        if(ev.step===null||ev.step===undefined||!Number.isFinite(step)||step<0)return false;
        if(ev.job_id)begin(ev.job_id);
        if(step<lastStep||ev.ts&&ev.ts<lastTs)return false;
        if(points.length&&nextUnit!==unit)return false;
        var last=points[points.length-1];
        if(last&&last.step===step){if(last.value===ev.value)return false;last.value=ev.value;}
        else points.push({step:step,value:ev.value});
        lastStep=step;lastTs=Number(ev.ts)||lastTs;unit=nextUnit;
        if(points.length>(limit||600))points.shift();return true;
      }};
  }
  function lossAxes(unit){return {xLabel:unit==='epoch'?'训练轮次（Epoch）':unit==='metric'?'指标序号':'训练步数（Step）',yLabel:'损失值（Loss）'};}
  function chart(canvas, data, options) {
    if (!canvas) return;
    options=options||{};var xLabel=options.xLabel||'训练步数（Step）',yLabel=options.yLabel||'损失值（Loss）';
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 600, h = canvas.clientHeight || 220;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var cs = getComputedStyle(document.documentElement);
    var grid = (cs.getPropertyValue("--border") || "#e4e7ec").trim();
    var line = (cs.getPropertyValue("--accent") || "#2563eb").trim();
    var muted = (cs.getPropertyValue("--text-3") || "#8b93a0").trim();
    var text = (cs.getPropertyValue("--text-2") || "#5b6b82").trim();
    var surf = (cs.getPropertyValue("--surface") || "#ffffff").trim();
    ctx.clearRect(0, 0, w, h);

    var raw = (data || []).map(function (d, i) {
      if (d && typeof d === "object") {
        return { x: Number(d.step !== undefined ? d.step : i), y: Number(d.value) };
      }
      return { x: i, y: Number(d) };
    }).filter(function (p) { return isFinite(p.x) && isFinite(p.y); });
    raw.sort(function (a, b) { return a.x - b.x; });
    // 同一步可能打多条 loss：按 step 求平均，避免竖线/锯齿
    var pts = [];
    raw.forEach(function (p) {
      var last = pts[pts.length - 1];
      if (last && Math.abs(last.x - p.x) < 1e-9) {
        last._sum += p.y;
        last._n += 1;
        last.y = last._sum / last._n;
      } else {
        pts.push({ x: p.x, y: p.y, _sum: p.y, _n: 1 });
      }
    });

    var pad = { l: 80, r: 24, t: 14, b: 56 };
    var iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    ctx.strokeStyle = grid;
    ctx.fillStyle = muted;
    ctx.font = "11px -apple-system, Segoe UI, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    function axes(){
      ctx.save();ctx.fillStyle=text;ctx.font='12px -apple-system, Segoe UI, sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
      ctx.fillText(xLabel,pad.l+iw/2,h-12);ctx.translate(14,pad.t+ih/2);ctx.rotate(-Math.PI/2);ctx.fillText(yLabel,0,0);ctx.restore();
      canvas.setAttribute('role','img');canvas.setAttribute('aria-label','X 轴：'+xLabel+'；Y 轴：'+yLabel+(pts.length?'；范围 '+pts[0].x+' 至 '+pts[pts.length-1].x:'；等待指标'));
    }

    if (!pts.length) {
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = grid;
      for (var g = 0; g <= 4; g++) {
        var gy = pad.t + ih * g / 4;
        ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(w - pad.r, gy); ctx.stroke();
      }
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.fillText("等待训练指标（loss）…", w / 2, h / 2);
      axes();
      return;
    }

    var x0 = pts[0].x, x1 = pts[pts.length - 1].x;
    if (x1 === x0) { x0 -= 1; x1 += 1; }
    var ymin = Infinity, ymax = -Infinity;
    pts.forEach(function (p) {
      if (p.y < ymin) ymin = p.y;
      if (p.y > ymax) ymax = p.y;
    });
    if (ymax - ymin < 1e-9) { ymin -= 0.01; ymax += 0.01; }
    var ypad = (ymax - ymin) * 0.1;
    ymin -= ypad; ymax += ypad;

    function X(v) { return pad.l + (v - x0) / (x1 - x0) * iw; }
    function Y(v) { return pad.t + (1 - (v - ymin) / (ymax - ymin)) * ih; }
    function fmt(v) {
      if (Math.abs(v) >= 1000) return String(Math.round(v));
      if (Math.abs(v) >= 10) return v.toFixed(1);
      return v.toFixed(4);
    }

    // 横向网格 + Y 轴刻度
    for (var i = 0; i <= 4; i++) {
      var yv = ymin + (ymax - ymin) * i / 4;
      var yy = Y(yv);
      ctx.strokeStyle = grid;
      ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
      ctx.fillStyle = muted;
      ctx.textAlign = "right";
      ctx.fillText(fmt(yv), pad.l - 8, yy);
    }
    // X 轴刻度：直接取真实数据点的 step，避免四舍五入出现重复标签
    var ticks = Math.min(w<400?2:5, Math.max(1, pts.length - 1));
    var seen = {};
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (var t = 0; t <= ticks; t++) {
      var idx = Math.round((pts.length - 1) * t / ticks);
      var xv = pts[Math.min(pts.length - 1, Math.max(0, idx))].x;
      var label = Math.abs(xv - Math.round(xv)) < 1e-9
        ? String(Math.round(xv)) : xv.toFixed(2);
      if (seen[label]) continue;
      seen[label] = true;
      ctx.fillStyle = muted;
      ctx.fillText(label, X(xv), h - pad.b + 8);
    }
    ctx.textBaseline = "middle";

    // 折线 + 面积
    ctx.beginPath();
    pts.forEach(function (p, i) {
      var px = X(p.x), py = Y(p.y);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = line;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();
    if (pts.length > 1) {
      ctx.lineTo(X(pts[pts.length - 1].x), Y(ymin));
      ctx.lineTo(X(pts[0].x), Y(ymin));
      ctx.closePath();
      ctx.fillStyle = line + "14";
      ctx.fill();
    }
    // 数据点（少的时候画点）
    if (pts.length <= 120) {
      pts.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(X(p.x), Y(p.y), 2.4, 0, Math.PI * 2);
        ctx.fillStyle = line;
        ctx.fill();
      });
    }
    // 最新点高亮 + 数值
    var last = pts[pts.length - 1];
    var lx = X(last.x), ly = Y(last.y);
    ctx.beginPath(); ctx.arc(lx, ly, 4, 0, Math.PI * 2);
    ctx.fillStyle = line; ctx.fill();
    ctx.strokeStyle = surf; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.fillStyle = line;
    ctx.textAlign = lx > w - 90 ? "right" : "left";
    ctx.fillText(fmt(last.y), lx + (lx > w - 90 ? -8 : 8), ly - 10);
    axes();
  }

  /* ---------------- 运行位置：服务端真机 vs 当前访问设备 ----------------
     浏览器没有权限读取访问端本机的串口 / 摄像头 / conda，所以硬件状态只来自
     运行 python start.py 的那台真机。这里把两端信息都摆出来，避免误读来源。 */
  var runtimeInfo = null, runtimeError = null, runtimeCallbacks = [];

  function runtimeHost(s) {
    var ip = (s && s.lan_ips || [])[0] ||
      (s && s.listen_host && !/^0\.0\.0\.0$/.test(s.listen_host) ? s.listen_host : "127.0.0.1");
    return "http://" + ip + (s && s.port ? ":" + s.port : "");
  }
  function paintRuntime(r, err) {
    var srv = el("rbServer"), cli = el("rbClient");
    var c = (r && r.client) || {}, s = (r && r.server) || {};
    if (srv) {
      srv.textContent = r
        ? ((s.platform || "未知系统") + " · " + (s.hostname || "未知主机") + " · " + runtimeHost(s))
        : ("服务端信息获取失败：" + (err && err.message ? err.message : "未知错误"));
    }
    if (cli) {
      var name = clientPrefs().alias || c.alias ||
        (c.local ? "本机浏览器" : "未命名设备");
      cli.textContent = r
        ? (name + " · " + (c.ip || "IP 未知") + " · " + (c.browser || "浏览器") +
           " / " + (c.os || "未知系统") + (c.local ? " · 真机本机打开" : " · 局域网远程访问"))
        : ("当前浏览器打开的是 " + location.host);
    }
  }
  function refreshRuntime() {
    return API.get("/api/runtime", 8000).then(function (r) {
      runtimeInfo = r;
      runtimeError = null;
      paintRuntime(r, null);
      runtimeCallbacks.forEach(function (c) { try { c.ok && c.ok(r); } catch (e) {} });
      return r;
    }).catch(function (e) {
      runtimeError = e;
      paintRuntime(null, e);
      runtimeCallbacks.forEach(function (c) { try { c.err && c.err(e); } catch (x) {} });
      throw e;
    });
  }
  function onRuntime(cb, errCb) {
    runtimeCallbacks.push({ ok: cb, err: errCb });
    if (runtimeInfo) { try { cb(runtimeInfo); } catch (e) {} }
    else if (runtimeError && errCb) { try { errCb(runtimeError); } catch (e) {} }
  }
  function mountRuntime() {
    var shell = el("shell");
    var content = q(".content", shell || document);
    if (!content || el("runtimeBar")) return;
    var bar = document.createElement("div");
    bar.id = "runtimeBar";
    bar.className = "runtime-bar";
    bar.innerHTML =
      '<span class="rb-ico">' + icon("server") + '</span>' +
      '<div class="rb-col"><strong>状态来自服务端真机</strong><small id="rbServer">正在检测…</small></div>' +
      '<span class="rb-div"></span>' +
      '<span class="rb-ico">' + icon("monitor") + '</span>' +
      '<div class="rb-col"><strong>当前访问设备</strong><small id="rbClient">正在检测…</small></div>' +
      '';
    var note = document.createElement("div");
    note.id = "runtimeNote";
    note.className = "runtime-note";
    note.innerHTML =
      '串口、摄像头、conda 环境、数据集与训练进程都在上面的真机上，所有访问设备看到的是同一份真实状态。' +
      '要控制另一台设备上的硬件，请在那台设备上运行 <code>python start.py --lan</code>，' +
      '再用它的局域网地址打开本页；设备名称、主题等界面偏好只保存在当前浏览器。';
    var details = document.createElement('details'); details.className = 'runtime-details';
    details.innerHTML = '<summary>运行位置与访问设备</summary>';
    details.appendChild(bar); details.appendChild(note);
    content.appendChild(details);
    var page = el('page'); if (page) page.after(details);
    applyClientPrefs();
    if (!el('btnClientSettings')) {
      var settings = document.createElement('button'); settings.className = 'btn subtle sm';
      settings.id = 'btnClientSettings'; settings.textContent = '界面设置';
      q('.top-right').appendChild(settings);
    }
    click("btnClientSettings", function () { openClientSettings(); });
  }

  /* ---------------- 本机设置弹层：纯客户端配置，不写服务端 ---------------- */
  function syncClientForm() {
    var d = clientPrefs();
    if (el("cmAlias")) el("cmAlias").value = d.alias;
    if (el("cmTheme")) el("cmTheme").value = themePref();
    if (el("cmDensity")) el("cmDensity").value = d.density;
    if (el("cmShowBar")) el("cmShowBar").checked = d.showBar;
    if (el("cmHost")) el("cmHost").value = d.lastHost || "";
  }
  function openClientSettings() {
    var m = el("clientModal");
    if (m) { m.hidden = false; syncClientForm(); return; }
    m = document.createElement("div");
    m.id = "clientModal";
    m.className = "modal-mask";
    m.innerHTML =
      '<div class="client-modal" role="dialog" aria-modal="true" aria-label="本机设置">' +
        '<div class="cm-head"><h2>' + icon("settings") + '本机设置</h2>' +
          '<button type="button" class="btn subtle icon" id="cmClose" data-app-nofb ' +
            'title="关闭" aria-label="关闭本机设置">' + icon("close") + '</button></div>' +
        '<div class="cm-body">' +
          '<p class="cm-lead">这些设置只保存在当前浏览器，不会写入服务端配置，也不会同步到其它设备。</p>' +
          '<div class="field"><label>本设备名称（显示在顶部状态条与服务端操作日志里）</label>' +
            '<input id="cmAlias" maxlength="40" placeholder="例如：工位 Ubuntu / 我的笔记本"></div>' +
          '<div class="row">' +
            '<div class="field narrow"><label>主题</label><select id="cmTheme">' +
              '<option value="light">亮色</option><option value="dark">暗色</option>' +
              '<option value="auto">跟随系统</option></select></div>' +
            '<div class="field narrow"><label>界面密度</label><select id="cmDensity">' +
              '<option value="normal">舒适</option><option value="compact">紧凑</option></select></div>' +
          '</div>' +
          '<label class="check"><input type="checkbox" id="cmShowBar"> 显示顶部运行位置条</label>' +
          '<hr>' +
          '<div class="field"><label>切换到另一台真机（先在那台机器上运行 ' +
            '<code>python start.py --lan</code>）</label>' +
            '<div class="row"><input id="cmHost" ' +
              'placeholder="http://192.168.1.20:8000">' +
              '<button type="button" class="btn primary" id="cmGo" ' +
                'data-app-nofb>打开该真机</button></div></div>' +
          '<div class="note">浏览器无法读取访问端本机的串口、摄像头或 conda 环境。' +
            '要控制另一台设备上的硬件，必须把后端运行在那一台机器上，' +
            '再从它的局域网地址打开本页；本页所有状态与按钮都作用于同一个后端。</div>' +
        '</div>' +
        '<div class="cm-foot"><span class="muted">配置位置：当前浏览器 localStorage</span>' +
          '<div class="btn-row">' +
            '<button type="button" class="btn subtle" id="cmCancel" data-app-nofb>取消</button>' +
            '<button type="button" class="btn primary" id="cmSave" data-app-nofb>保存本机设置</button>' +
          '</div></div>' +
      '</div>';
    modalHost().appendChild(m);
    syncClientForm();
    function close() { m.hidden = true; }
    el("cmClose").addEventListener("click", close);
    el("cmCancel").addEventListener("click", close);
    m.addEventListener("click", function (e) { if (e.target === m) close(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { var mm = el("clientModal"); if (mm) mm.hidden = true; }
    });
    el("cmSave").addEventListener("click", function () {
      var alias = (el("cmAlias").value || "").trim().slice(0, 40);
      var pref = el("cmTheme").value;
      setClientPrefs({ alias: alias, density: el("cmDensity").value,
                       showBar: el("cmShowBar").checked });
      setTheme(pref);
      var ts = el("themeSelect");
      if (ts) ts.value = pref;
      close();
      toast("本机设置已保存，仅对当前浏览器生效", "ok");
      refreshRuntime().catch(function () {});
    });
    el("cmGo").addEventListener("click", function () {
      var raw = (el("cmHost").value || "").trim();
      if (!raw) { toast("请输入另一台真机的地址", "warn"); return; }
      if (!/^https?:\/\//i.test(raw)) raw = "http://" + raw;
      var u;
      try { u = new URL(raw); } catch (e) { toast("地址格式不正确", "err"); return; }
      if (!u.hostname) { toast("地址格式不正确", "err"); return; }
      setClientPrefs({ lastHost: u.origin });
      location.href = u.origin + "/";
    });
  }

  /* ---------------- 外壳 ---------------- */
  function renderShell(opts) {
    var shell = el("shell");
    if (!shell) return;
    var pref = themePref();
    var themeSelect =
      '<select id="themeSelect" style="width:auto;padding:4px 8px;font-size:12px">' +
        '<option value="light"' + (pref === "light" ? " selected" : "") + ">亮色</option>" +
        '<option value="dark"' + (pref === "dark" ? " selected" : "") + ">暗色</option>" +
        '<option value="auto"' + (pref === "auto" ? " selected" : "") + ">跟随系统</option>" +
      "</select>";
    if (opts.minimal) {
      shell.innerHTML =
        '<div class="app" style="grid-template-columns:1fr">' +
          '<div class="main">' +
            '<header class="top">' +
              '<div><h1>' + esc(opts.title || "") + "</h1><p>" + esc(opts.subtitle || "") + "</p></div>" +
              '<div class="top-right"><span class="chip is-loading" id="chipPort">读取中…</span>' +
                '<span class="chip is-loading" id="chipEnv">读取中…</span>' + themeSelect + "</div>" +
            "</header>" +
            '<div class="content"></div>' +
          "</div>" +
        "</div>";
      var pg = el("page"), ct = q(".content", shell);
      if (pg && ct) ct.appendChild(pg);
      document.body.classList.add("ready");
      presetStates();
      decorateIcons(opts);
      var t0 = el("themeSelect");
      if (t0) t0.onchange = function () { setTheme(this.value); toast("主题已切换", "ok"); };
      return;
    }
    var groups = [
      ["工作台", ["home", "competition"]],
      ["设备", ["connect", "camera"]],
      ["数据与训练", ["dataset", "viewer", "yolo", "train"]],
      ["运行", ["teleop", "eval"]],
      ["工具", ["sort", "setup", "logs"]]
    ];
    var nav = groups.map(function (group) {
      var links = NAV.filter(function (n) { return group[1].indexOf(n.id) >= 0; });
      if (!links.length) return "";
      return '<div class="nav-group"><div class="nav-label">' + group[0] + '</div>' + links.map(function (n) {
        return '<a href="' + n.href + '"' + (n.id === opts.active ? ' aria-current="page"' : '') +
          ' class="' + (n.id === opts.active ? 'active' : '') + '">' + icon(n.ico, 'nav-ico') + esc(n.label) + '</a>';
      }).join('') + '</div>';
    }).join('');
    shell.innerHTML = `
      <a class="skip-link" href="#page">跳到页面内容</a>
      <div class="app console-layout">
        <button class="shell-backdrop" id="navBackdrop" aria-label="关闭导航" hidden data-app-nofb></button>
        <aside class="side" id="mainNavigation" aria-label="主导航">
          <a class="brand" href="/p/home"><span class="brand-mark">${icon('brand')}</span>
            <span><strong>reBot 控制台</strong><small>双臂操作与训练 · v3.3.0</small></span></a>
          <nav class="nav">${nav}</nav>
          <div class="side-foot"><span>v3.3.0 · Ubuntu 硬件控制台</span><a href="/">启动向导</a></div>
        </aside>
        <div class="main">
          <header class="top">
            <div class="top-heading"><button class="btn subtle nav-toggle" id="btnNavToggle" type="button"
              aria-controls="mainNavigation" aria-expanded="false" data-app-nofb>导航</button>
              <div><h1>${esc(opts.title || '')}</h1><p>${esc(opts.subtitle || '')}</p></div></div>
            <div class="top-right">
              <span class="chip" id="chipJob">读取中…</span><span class="chip" id="chipPort">读取中…</span>
              <span class="chip" id="chipEnv">读取中…</span>
              <button class="btn subtle" id="btnRailToggle" aria-controls="rail" aria-expanded="false" data-app-nofb>${icon('terminal')}任务日志</button>
              <button class="btn subtle icon" id="btnClientSettings" aria-label="界面设置" title="界面设置" data-app-nofb>${icon('settings')}</button>
              <button class="btn danger" id="railStop" data-app-nofb>停止全部</button>
            </div>
          </header>
          <div class="content">${['teleop','dataset','train','eval'].indexOf(opts.active)>=0?'<section class="robot-context" id="robotContext"><span class="badge" data-context-mode>读取模式中…</span><span data-context-detail>正在获取设备状态</span><a href="/p/connect" class="btn subtle sm">设备连接与校准</a></section>':''}</div>
        </div>
        <button class="shell-backdrop" id="railBackdrop" aria-label="关闭任务日志" hidden data-app-nofb></button>
        <aside class="rail console-drawer" id="rail" role="dialog" aria-modal="true" aria-label="任务日志" hidden>
          <div class="rail-head"><span>任务日志</span><button class="btn subtle" id="btnRailClose" data-app-nofb>关闭</button></div>
          <div class="rail-body"><span class="chip" id="railState">空闲</span><div class="rail-job" id="railJob">当前没有任务</div>
            <div class="rail-last" id="railLast"></div><button class="btn subtle sm" id="railRefresh" data-app-nofb>刷新状态</button></div>
          <div class="rail-log-head"><label class="check"><input type="checkbox" id="railFollow" checked>自动跟随</label>
            <button class="btn subtle sm" id="railJump" data-app-nofb>回到底部</button><a href="/p/logs">完整日志</a></div>
          <div class="rail-log" id="railLog"></div>
        </aside>
      </div>`;
    function closePanels() {
      el('rail').hidden = el('railBackdrop').hidden = el('navBackdrop').hidden = true;
      document.body.classList.remove('nav-open');
      el('btnRailToggle').setAttribute('aria-expanded', 'false');
      el('btnNavToggle').setAttribute('aria-expanded', 'false');
    }
    el('btnRailToggle').onclick = function () {
      var opening = el('rail').hidden; closePanels();
      el('rail').hidden = el('railBackdrop').hidden = !opening;
      this.setAttribute('aria-expanded', String(opening));
      if (opening) el('btnRailClose').focus();
    };
    el('btnRailClose').onclick = function () { closePanels(); el('btnRailToggle').focus(); };
    el('railBackdrop').onclick = closePanels;
    el('navBackdrop').onclick = closePanels;
    el('btnNavToggle').onclick = function () {
      var opening = !document.body.classList.contains('nav-open'); closePanels();
      document.body.classList.toggle('nav-open', opening); el('navBackdrop').hidden = !opening;
      this.setAttribute('aria-expanded', String(opening));
    };
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Tab' && !el('rail').hidden) {
        var controls = qa('a[href], button:not(:disabled), input:not(:disabled)', el('rail'));
        var first = controls[0], last = controls[controls.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      if (e.key === 'Escape') {
        var fromRail = !el('rail').hidden, fromNav = document.body.classList.contains('nav-open');
        closePanels();
        if (fromRail) el('btnRailToggle').focus(); else if (fromNav) el('btnNavToggle').focus();
      }
    });
    var page = el("page");
    var content = q(".content", shell);
    if (page && content) content.appendChild(page);
    // 右侧固定栏：任务状态 + 实时日志（所有页面共用）
    var railState = el("railState"), railJob = el("railJob"),
        railLast = el("railLast"), railLog = el("railLog");
    function railLogLine(ev) {
      if (!railLog || !ev || ev.type !== "log") return;
      var empty = q(".rail-empty", railLog);
      if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
      var line = document.createElement("div");
      line.textContent = String(ev.line || "");
      if (ev.level) line.className = "lv-" + ev.level;
      railLog.appendChild(line);
      while (railLog.children.length > 500) railLog.removeChild(railLog.firstChild);
      var follow = el("railFollow");
      if (!follow || follow.checked) railLog.scrollTop = railLog.scrollHeight;
    }
    function railUpdate(st) {
      st = st || {};
      var j = st.job || {};
      if (railState) {
        railState.textContent = st.running ? "运行中" : "空闲";
        railState.className = "chip" + (st.running ? " is-run" : "");
      }
      if (railJob) {
        railJob.textContent = st.running && j.name
          ? (j.name + (j.elapsed ? " · " + j.elapsed + "s" : ""))
          : "当前没有任务";
      }
      if (railLast) railLast.textContent = j.last_line || "";
    }
    onLog(railLogLine);
    LOG_HISTORY.forEach(railLogLine);
    if (railLog && !q(".rail-empty", railLog) && !railLog.children.length) {
      var emptyBox = document.createElement("div");
      emptyBox.className = "rail-empty";
      emptyBox.textContent = "暂无日志，开始任务后这里会实时显示";
      railLog.appendChild(emptyBox);
    }
    if (el("railStop")) {
      el("railStop").onclick = function () {
        API.post("/api/stop", { force: true }).then(function () {
          toast("急停已下发", "ok");
          refreshStatus();
        }).catch(function (e) { toast(e.message, "err"); });
      };
    }
    if (el("railRefresh")) el("railRefresh").onclick = refreshStatus;

    if (el("railJump")) {
      el("railJump").onclick = function () {
        var f = el("railFollow");
        if (f) f.checked = true;
        if (railLog) railLog.scrollTop = railLog.scrollHeight;
      };
    }
    if (railLog) {
      railLog.addEventListener("scroll", function () {
        var near = railLog.scrollTop + railLog.clientHeight >= railLog.scrollHeight - 24;
        var f = el("railFollow");
        if (f && f.checked !== near) f.checked = near;
      });
    }
    statusCallbacks.push(railUpdate);
    if (appStatus) railUpdate(appStatus);
    // Fixed navigation and optional log drawer need no draggable columns.
    // Forms save explicitly through their page actions.
    document.body.classList.add("ready");
    presetStates();
    decorateIcons(opts);
    var ts = el("themeSelect");
    if (ts) ts.onchange = function () { setTheme(this.value); toast("主题已切换", "ok"); };
  }

  function paintRobotContext(s) {
    var box=el('robotContext'); if(!box||!s)return;
    var page=document.body.dataset.page, dual=!!(s.dual_arm&&s.dual_arm.enabled), connected=dual?s.dual_connected:s.connected;
    box.querySelector('[data-context-mode]').textContent=dual?'双臂 · 左右协同':'单臂 · 主从控制';
    box.querySelector('[data-context-detail]').textContent=page==='train'
      ? '训练使用所选数据集的动作与状态维度，无需连接机械臂。'
      : connected?'设备已连接，可在本页开始'+({teleop:'遥操作',dataset:'采集',eval:'评估'}[page]||'操作')+'。'
        :'设备尚未连接，请在设备连接与校准页完成'+(dual?'左右四路串口':'主从臂串口')+'连接。';
    box.querySelector('[data-context-mode]').className='badge '+(page==='train'||connected?'ok':'warn');
  }
  async function commandPreview(kind, target) {
    var node=typeof target==='string'?q(target):target;if(!node)return;
    node.textContent='正在生成已保存配置的命令…';
    try { var r=await API.get('/api/commands/preview?kind='+encodeURIComponent(kind));node.textContent=r.error||('$ '+r.command); }
    catch(e){node.textContent='命令预览暂不可用：'+e.message;}
  }

  async function gate() {
    try {
      var s = await API.get("/api/env/status", 8000);
      if (!s.ready) {
        location.replace("/?gate=env");
        return false;
      }
      return true;
    } catch (e) {
      return true; // 后端不可达时交给状态轮询提示，不阻断页面
    }
  }

  async function init(opts) {
    opts = opts || {};
    if(window.parent!==window){opts.minimal=true;document.body.classList.add('embedded-console');}
    document.body.dataset.page = opts.active || "wizard";
    renderShell(opts);
    mountRuntime();
    if(window.APP.autosave) window.APP.autosave.mount();
    if (!el('toasts')) {
      var toasts = document.createElement('div'); toasts.id = 'toasts'; document.body.appendChild(toasts);
    }
    connectLogs();
    refreshRuntime().catch(function () {});
    refreshStatus();
    setInterval(refreshStatus, 2000);
    if (opts.gate !== false) await gate();
    return appStatus;
  }

  if(window.parent!==window){
    document.addEventListener('click',function(event){var a=event.target.closest&&event.target.closest('a[href]');if(!a||a.target==='_blank'||event.ctrlKey||event.metaKey)return;var url=new URL(a.href,location.origin);if(url.origin===location.origin&&(url.pathname.startsWith('/p/')||url.pathname==='/')){event.preventDefault();parent.postMessage({type:'rebot:navigate',path:url.pathname+url.search+url.hash},location.origin);}});
    window.addEventListener('load',function(){
      // documentElement.scrollHeight is at least the iframe viewport height;
      // measuring it makes a previously tall page impossible to shrink.
      var last=0,scheduled=false;
      function report(){scheduled=false;var app=document.querySelector('.app');if(!app)return;
        var height=Math.ceil(app.getBoundingClientRect().height);
        if(height!==last){last=height;parent.postMessage({type:'rebot:layout',height:height,path:location.pathname+location.search},location.origin);}
      }
      function schedule(){if(!scheduled){scheduled=true;requestAnimationFrame(report);}}
      var observer=new ResizeObserver(schedule);observer.observe(document.querySelector('.app')||document.body);
      window.addEventListener('resize',schedule);schedule();
    });
  }
  window.APP = {
    commandPreview: commandPreview, init: init, api: API, esc: esc, el: el, q: q, qa: qa, icon: icon,
    toast: toast, notify: notify, confirm: confirmModal, prompt: promptModal,
    blocking: blockingModal,
    store: store, load: load,
    setAlert: setAlert, clearAlert: clearAlert, action: action, ok: ok,
    showLoading: showLoading, loadError: loadError, loadEmpty: loadEmpty,
    clearState: clearState, waitingLog: waitingLog,
    onStatus: function (cb) { statusCallbacks.push(cb); if (appStatus) cb(appStatus); },
    onTheme: function (cb) { themeCallbacks.push(cb); },
    status: function () { return appStatus; }, refreshStatus: refreshStatus,
    isWindows: function () { return serverIsWindows; },
    isDual: function () { return dualMode; },
    onLog: onLog, logs: function () { return LOG_HISTORY.slice(); },
    attachLogPanel: attachLogPanel,
    formData: formData, fillForm: fillForm, click: click, change: change,
    mountEnvLocation: mountEnvLocation,
    mountLerobotCard: mountLerobotCard,
    envLocation: function () { return envLocCtl; },
    chart: chart, lossSeries: lossSeries, lossAxes: lossAxes, setTheme: setTheme, themePref: themePref,
    modalHost: modalHost,
    clientPrefs: clientPrefs, setClientPrefs: setClientPrefs,
    runtime: function () { return runtimeInfo; },
    onRuntime: onRuntime, refreshRuntime: refreshRuntime,
    openClientSettings: openClientSettings,
    saveConfig: function (patch) { return API.post("/api/config", patch); },
    stop: function () { return API.post("/api/stop", {}); }
  };
})();
