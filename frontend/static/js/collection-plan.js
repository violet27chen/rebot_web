/* Collection-specific cooperation planning. */
(async function(){
 'use strict';var A=window.APP, plan=null,timer=null,phaseIdx=0,phaseLeft=0,alertSel='#dsAlert';
 var esc=A.esc, planRequest=0;
 function val(id){var n=A.el(id);return n?n.value:'';}
 function setv(id,v){var n=A.el(id);if(n)n.value=v;}
  async function loadPlan(key, quiet) {
    var requestId=++planRequest;
    try {
      var r = await A.api.get("/api/collection/plan?coordination=" + encodeURIComponent(key || "sync"));
      if(requestId!==planRequest)return;
      plan = r;
      var tpl = A.el("planTemplate");
      if (!tpl.options.length) {
        tpl.innerHTML = (r.templates || []).map(function (t) {
          return '<option value="' + esc(t.key) + '">' + esc(t.label) + "（" +
            esc(t.total_seconds) + "s）</option>";
        }).join("");
      }
      tpl.value = r.coordination;
      setv("coordination", r.coordination);
      A.el("planMeta").textContent = r.label + " · " + r.total_seconds + "s · " +
        r.phases.length + " 阶段 · " + r.desc;
      A.q("#phaseTable tbody").innerHTML = (r.phases || []).map(function (p, i) {
        return "<tr data-phase=\"" + i + "\"><td>" + esc(p.name) + "</td><td>" +
          esc(p.seconds) + "</td><td>" + esc(p.left) + "</td><td>" + esc(p.right) +
          "</td><td>" + (p.sync ? '<span class="phase-sync">两臂同步到位</span>'
            : '<span class="muted">各自动作</span>') + "</td></tr>";
      }).join("");
      stopTimer(); renderClock("未开始", -1); A.el("btnPhaseTimer").textContent="开始计时";
      if (!quiet) A.ok("已载入协作模板：" + r.label, alertSel);
    } catch (e) {
      A.toast(e.message, "err");
      A.setAlert(alertSel, e.message, "err");
    }
  }

  function stopTimer() {
    if (timer) { clearInterval(timer); timer = null; }
    phaseIdx = 0; phaseLeft = 0;
  }

  function renderClock(text, activeIdx) {
    A.el("phaseClock").textContent = text;
    A.qa("#phaseTable tbody tr").forEach(function (tr) {
      tr.style.opacity = (activeIdx >= 0 && Number(tr.dataset.phase) === activeIdx) ? "1" : ".62";
      tr.style.background = (activeIdx >= 0 && Number(tr.dataset.phase) === activeIdx) ?
        "var(--accent-soft)" : "";
    });
  }

  function toggleTimer() {
    if (timer) {
      stopTimer(); renderClock("未开始", -1); A.el("btnPhaseTimer").textContent="开始计时";
      renderClock("计时已停止", -1);
      A.el("btnPhaseTimer").textContent = "开始计时";
      return;
    }
    if (!plan || !plan.phases || !plan.phases.length) { A.toast("请先载入协作模板", "warn"); return; }
    phaseIdx = 0;
    phaseLeft = Number(plan.phases[0].seconds) || 0;
    A.el("btnPhaseTimer").textContent = "停止计时";
    renderClock("第 1 阶段 · " + plan.phases[0].name + " · 剩余 " + phaseLeft + "s", 0);
    timer = setInterval(function () {
      if (!plan || !plan.phases.length) { stopTimer(); return; }
      phaseLeft -= 1;
      if (phaseLeft <= 0) {
        phaseIdx += 1;
        if (phaseIdx >= plan.phases.length) {
          stopTimer(); renderClock("未开始", -1); A.el("btnPhaseTimer").textContent="开始计时";
          renderClock("编排计时完成（共 " + plan.total_seconds + "s）", -1);
          A.el("btnPhaseTimer").textContent = "开始计时";
          A.toast("协作编排计时结束", "ok");
          return;
        }
        phaseLeft = Number(plan.phases[phaseIdx].seconds) || 0;
      }
      renderClock("第 " + (phaseIdx + 1) + " 阶段 · " + plan.phases[phaseIdx].name +
        " · 剩余 " + phaseLeft + "s", phaseIdx);
    }, 1000);
  }


 A.onStatus(function(s){A.el('collectionPlan').hidden=!(s&&s.dual_arm&&s.dual_arm.enabled);});
 try {
  var cfg=await A.api.get('/api/config');await loadPlan((cfg.dual_arm||{}).coordination||'sync',true);
  A.el('applyTask').checked=!!(cfg.dataset||{}).plan_apply_task;
  var binding=A.autosave.bind({id:'collection-plan',root:'#collectionPlan',
   read:function(){return {dual_arm:{coordination:val('planTemplate')},dataset:{plan_apply_task:A.el('applyTask').checked}};},
   apply:function(c){var key=(c.dual_arm||{}).coordination||'sync';if(val('planTemplate')!==key)loadPlan(key,true);A.el('applyTask').checked=!!(c.dataset||{}).plan_apply_task;},
   write:async function(delta,full){if(full.dataset.plan_apply_task){var template=await A.api.get('/api/collection/plan?coordination='+encodeURIComponent(full.dual_arm.coordination));delta=A.autosave.merge(delta,{dataset:{single_task:template.task}});}await A.saveConfig(delta);}
  });
  A.change('planTemplate',function(){loadPlan(val('planTemplate'),true);});
  A.click('btnPhaseTimer',toggleTimer);
  A.click('btnExportPlan',function(){A.action(async function(){await binding.flush();var r=await A.api.post('/api/collection/plan',{coordination:val('planTemplate'),apply_task:A.el('applyTask').checked});A.ok('编排已导出：'+r.path,alertSel);},alertSel);});
 }catch(e){A.setAlert(alertSel,e.message,'err');}
 window.addEventListener('pagehide',stopTimer);
})();
