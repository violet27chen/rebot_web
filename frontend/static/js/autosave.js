/* Serialized, observable autosave. Remote updates merge around local edits. */
(function () {
  'use strict';
  var A = window.APP, external = new Map(), bindings = [], writeQueue = Promise.resolve(), listeners = [];
  var revision = 0, pullTimer, pulling = false, pullAgain = false, retryTimer, directWrites=0, writeError='';
  var draftStorage = /Electron\//.test(navigator.userAgent) ? localStorage : sessionStorage;
  var draftKey = 'rebot.autosave.' + location.origin + location.pathname;
  var latestConfig = null;
  function copy(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  function same(a,b){
    if(a===b)return true;if(!a||!b||typeof a!=='object'||typeof b!=='object')return false;
    if(Array.isArray(a)!==Array.isArray(b))return false;
    var ak=Object.keys(a).sort(),bk=Object.keys(b).sort();if(ak.length!==bk.length)return false;
    return ak.every(function(k,i){return k===bk[i]&&same(a[k],b[k]);});
  }
  function diff(base, value) {
    if (same(base, value)) return undefined;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      var out = {};
      Object.keys(value).forEach(function (key) {
        var delta = diff((base || {})[key], value[key]);
        if (delta !== undefined) out[key] = delta;
      });
      return Object.keys(out).length ? out : undefined;
    }
    return copy(value);
  }
  function merge(base, patch) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return copy(patch);
    var out = Object.assign({}, base || {});
    Object.keys(patch).forEach(function (k) { out[k] = merge(out[k], patch[k]); });
    return out;
  }
  function drafts() {
    try { return JSON.parse(draftStorage.getItem(draftKey) || '{}'); } catch (e) { return {}; }
  }
  function remember(binding) {
    try {
      var data = drafts(), delta = diff(binding.baseline, binding.read());
      if (delta !== undefined) data[binding.id] = delta; else delete data[binding.id];
      draftStorage.setItem(draftKey, JSON.stringify(data));
    } catch (e) { /* Quota failures do not replace the visible server error. */ }
  }
  function status() {
    var node = A.el('autosaveState'); if (!node) return;
    var states=bindings.concat(Array.from(external.values()));
    var error = states.find(function (b) { return b.error; }) || (writeError?{error:writeError}:null);
    var saving = directWrites>0 || states.some(function (b) { return b.saving; });
    var pending = states.some(function (b) { return b.dirty; });
    node.className = 'autosave-status ' + (error ? 'err' : saving ? 'saving' : pending ? 'pending' : 'ok');
    node.querySelector('[data-save-label]').textContent = error ? '保存失败 · 点击重试' : saving ? '正在保存…' : pending ? '等待自动保存…' : '所有更改已保存';
    node.title = error ? error.error : '配置自动保存，任务仍由操作按钮启动';
    node.setAttribute('aria-busy', String(saving));
    if(window.parent!==window)parent.postMessage({type:'rebot:save-state',path:location.pathname,saving:saving,dirty:pending,error:error?error.error:''},location.origin);
    var detail=A.el('autosaveError');if(detail){detail.hidden=!error;detail.textContent=error?('自动保存未完成：'+error.error+'。内容已保留，点击顶部保存状态可重试。'):'';}
  }
  function mount() {
    if (A.el('autosaveState')) return;
    var node = document.createElement('button');
    node.id = 'autosaveState'; node.type = 'button'; node.className = 'autosave-status ok';
    node.setAttribute('data-app-nofb', ''); node.setAttribute('aria-live', 'polite');
    node.innerHTML = '<span class="save-indicator" aria-hidden="true"></span><span data-save-label>所有更改已保存</span>';
    node.onclick = function () { flushAll().catch(function () {}); };
    var header = A.q('.top-right'); if (header) header.prepend(node);
    var detail=document.createElement('div');detail.id='autosaveError';detail.className='alert err autosave-error';detail.hidden=true;detail.setAttribute('role','alert');
    var content=A.q('.content');if(content)content.prepend(detail);
  }
  function enqueue(fn) {
    var result = writeQueue.catch(function () {}).then(fn);
    writeQueue = result.catch(function () {}); return result;
  }
  function write(patch) {
    directWrites++;writeError='';mount();status();
    return enqueue(async function () {
      var result = await A.api.post('/api/config', patch);
      latestConfig = result.config || merge(latestConfig, patch);
      schedulePull(); return result;
    }).catch(function(e){writeError=e.message;throw e;}).finally(function(){directWrites--;status();});
  }
  async function flush(binding) {
    clearTimeout(binding.timer);
    if (binding.saving) { await binding.saving; if (binding.dirty) return flush(binding); return; }
    var value = copy(binding.read()), delta = diff(binding.baseline, value);
    if (delta === undefined) { binding.dirty = false; binding.error = ''; remember(binding); status(); return; }
    binding.dirty = true;
    try { if (binding.validate) binding.validate(value); }
    catch (e) { binding.error = e.message; status(); throw e; }
    binding.error = '';
    binding.saving = (async function () {
      await (binding.write || write)(delta, value);
      binding.baseline = value;
      binding.dirty = diff(binding.baseline, binding.read()) !== undefined;
      binding.error = '';writeError=''; remember(binding);
    })();
    status();
    try { await binding.saving; }
    catch (e) { binding.error = e.message; binding.dirty = true; remember(binding); throw e; }
    finally { binding.saving = null; status(); }
    if (binding.dirty) return flush(binding);
  }
  function schedule(binding) {
    binding.dirty = diff(binding.baseline, binding.read()) !== undefined;
    binding.error = ''; remember(binding); status(); clearTimeout(binding.timer);
    if (binding.dirty) binding.timer = setTimeout(function () { flush(binding).catch(function () {}); }, binding.delay || 600);
  }
  function bind(options) {
    var old = bindings.find(function (b) { return b.id === options.id; }); if (old) return old;
    var b = Object.assign({dirty: false, error: '', saving: null}, options);
    b.baseline = copy(b.read());
    b.changed = function () { schedule(b); };
    b.flush = function () { return flush(b); };
    b.accept = function () { b.baseline = copy(b.read()); b.dirty = false; b.error = ''; remember(b); status(); };
    bindings.push(b);
    var root = typeof b.root === 'string' ? A.q(b.root) : b.root;
    if (root) ['input','change'].forEach(function (event) {
      root.addEventListener(event, function (e) {
        if (!e.target.matches('input,select,textarea') || e.target.type === 'password' || e.target.type === 'file') return;
        schedule(b);
      });
    });
    var draft = drafts()[b.id];
    if (draft && b.apply) { b.apply(merge(b.read(), draft)); schedule(b); }
    mount(); status(); return b;
  }
  function form(formId, section, options) {
    var element = A.el(formId); if (!element) return;
    var opts = options || {};
    var b = bind(Object.assign({id: section + ':' + formId, root: element, section: section,
      read: function () { var p = {}; p[section] = A.formData(element); return p; },
      apply: function (cfg) { A.fillForm(element, cfg[section] || {}); },
      validate: function () { if (!element.checkValidity()) throw new Error('请检查表单中的必填项和数值范围'); }
    }, opts));
    var submit = element.querySelector('button[type="submit"]');
    if (submit) { submit.textContent = '立即保存'; submit.classList.remove('primary'); submit.classList.add('subtle'); }
    return b;
  }
  async function flushAll() {
    writeError='';
    for (var i = 0; i < bindings.length; i++) if (bindings[i].dirty || bindings[i].saving) await flush(bindings[i]);
    for(var value of external.values())if((value.dirty||value.saving)&&value.flush)await value.flush();
  }
  function notifyConfig(config) {
    latestConfig = config;
    bindings.forEach(function (b) {
      if (!b.apply || b.saving) return;
      if(b.section && same(b.remoteSection,config[b.section]))return;
      if(b.section)b.remoteSection=copy(config[b.section]);
      var local = diff(b.baseline, b.read());
      if(b.atomicArray && local!==undefined)return;
      // Apply the newest server state, then restore only fields edited locally.
      b.apply(config);
      var serverValue = copy(b.read());
      if (local !== undefined) b.apply(merge(serverValue, local));
      b.baseline = serverValue;
      b.dirty = diff(serverValue, b.read()) !== undefined;
      remember(b);
    });
    listeners.forEach(function (fn) { try { fn(config); } catch (e) { A.toast(e.message, 'err'); } });
    status();
  }
  async function pull() {
    if (pulling) { pullAgain = true; return; }
    pulling = true;
    try { notifyConfig(await A.api.get('/api/config', 8000)); }
    catch (e) { /* Connection state is reported by status polling. Pending edits remain. */ }
    finally { pulling = false; if (pullAgain) { pullAgain = false; schedulePull(); } }
  }
  function schedulePull() { clearTimeout(pullTimer); pullTimer = setTimeout(pull, 180); }
  A.onLog(function (e) { if (e.type === 'config' && e.revision !== revision) { revision = e.revision; schedulePull(); } });
  setInterval(async function () {
    if (document.hidden) return;
    try {
      var data = await A.api.get('/api/config/revision', 5000);
      if (data.revision !== revision) { revision = data.revision; schedulePull(); }
    } catch (e) { /* Existing connection status remains authoritative. */ }
    bindings.forEach(function(b){if(!b.saving&&!b.dirty&&diff(b.baseline,b.read())!==undefined)schedule(b);});
    clearTimeout(retryTimer);
    if (navigator.onLine && bindings.some(function (b) { return b.error && b.dirty; })) retryTimer = setTimeout(function () { flushAll().catch(function () {}); }, 1000);
  }, 2500);
  window.addEventListener('online', function () { flushAll().catch(function () {}); schedulePull(); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { A.refreshStatus(); schedulePull(); } });
  window.addEventListener('pagehide',function(){bindings.forEach(remember);flushAll().catch(function(){});});
  window.addEventListener('message',function(event){
    if(event.origin!==location.origin||event.source!==window.parent)return;
    if(event.data&&event.data.type==='rebot:flush')flushAll().then(function(){parent.postMessage({type:'rebot:flush-result',id:event.data.id,ok:true},location.origin);}).catch(function(e){parent.postMessage({type:'rebot:flush-result',id:event.data.id,ok:false,error:e.message},location.origin);});
  });
  async function preferences(fields, name) {
    var page=name||document.body.dataset.page, config=await A.api.get('/api/config');
    function read(){var values={};fields.forEach(function(id){var n=A.el(id);if(n&&n.type!=='password'&&n.type!=='file')values[id]=n.type==='checkbox'?n.checked:n.value;});var result={ui_preferences:{}};result.ui_preferences[page]=values;return result;}
    function apply(c){var values=(c.ui_preferences||{})[page]||{};fields.forEach(function(id){var n=A.el(id);if(!n||values[id]===undefined)return;if(n.type==='checkbox')n.checked=!!values[id];else if(n.tagName!=='SELECT'||Array.from(n.options).some(function(o){return o.value===String(values[id]);}))n.value=values[id];});}
    apply(config);return bind({id:'preferences:'+page,root:'#page',read:read,apply:apply,validate:function(){fields.forEach(function(id){var n=A.el(id);if(n&&!n.checkValidity())throw new Error('请检查输入参数的数值范围');});}});
  }
  A.autosave = {preferences:preferences,track:function(id,values){external.set(id,Object.assign(external.get(id)||{},values));status();},mount: mount, bind: bind, form: form, write: write, flush: flushAll, changed: schedule, pull: pull,
    onConfig: function (fn) { listeners.push(fn); if (latestConfig) fn(latestConfig); },
    state: function () { return bindings.map(function (b) { return {id:b.id, dirty:b.dirty, saving:!!b.saving, error:b.error}; }); },
    diff: diff, merge: merge};
  A.saveConfig = write;
})();
