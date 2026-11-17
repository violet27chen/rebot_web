(async function () {
  'use strict';
  var A = window.APP, busy = false, lastMode = null, stopped = false, serviceReady = false, checking = null;
  var buttons = ['motorRead', 'motorApply', 'motorSaveFlash'];
  var decelTouched = false;

  function status(s) {
    var dual = !!(s && s.dual_arm && s.dual_arm.enabled), select = A.el('motorArm');
    if (lastMode !== dual) {
      var old = select.value;
      select.innerHTML = dual
        ? '<option value="left">左从臂</option><option value="right">右从臂</option>'
        : '<option value="left">当前从臂</option>';
      select.value = dual && (old === 'left' || old === 'right') ? old : 'left';
      lastMode = dual;
    } else if (!dual && select.value !== 'left') {
      select.value = 'left';
    }
    buttons.forEach(function (id) { A.el(id).disabled = !serviceReady || busy || !!(s && s.running); });
    select.disabled = busy;
    A.el('motorSelectAll').disabled = busy;
    ['motorMaxSpeed', 'motorAccel', 'motorDecel'].forEach(function (id) { A.el(id).disabled = busy; });
    A.qa('#motorJoints input').forEach(function (node) { node.disabled = busy; });
  }

  function errorMessage(error, phase) {
    if (error.status === 404 && phase !== 'result') return '电机高级设置接口未加载：当前后端与界面版本不一致，请重启控制服务并刷新界面。';
    if (error.status === 404 && phase === 'result') return '本次电机设置任务结果已失效，请重新读取参数确认。';
    return error.message || '电机设置服务请求失败';
  }

  function checkService() {
    if (checking) return checking;
    A.el('motorService').textContent = '正在检查电机设置服务…';
    A.el('motorRetryService').disabled = true;
    checking = (async function () {
      try {
        var result = await A.api.get('/api/devices/motor-settings/capabilities', 8000);
        if (result.api_version !== 1 || !Array.isArray(result.actions) || !['read', 'write'].every(function (a) { return result.actions.includes(a); })) {
          throw new Error('电机设置服务接口不兼容，请更新并重启控制服务');
        }
        if (!result.worker_available) throw new Error('电机设置程序缺失，请恢复 backend/motor_settings_worker.py 后重试');
        serviceReady = true;
        A.el('motorService').textContent = '电机设置服务已就绪 · ' + result.version + '+' + result.build;
        A.el('motorRetryService').hidden = true;
        A.clearAlert('#motorAlert');
        return true;
      } catch (error) {
        serviceReady = false;
        A.el('motorService').textContent = '电机设置服务未就绪';
        A.el('motorRetryService').hidden = false;
        A.setAlert('#motorAlert', errorMessage(error, 'service'), 'err');
        return false;
      } finally {
        checking = null;
        A.el('motorRetryService').disabled = false;
        status(A.status());
      }
    })();
    return checking;
  }

  function selected() {
    return A.qa('#motorJoints input:checked').map(function (n) { return Number(n.value); });
  }

  function paint(result) {
    A.el('motorReadings').innerHTML = (result.joints || []).map(function (j) {
      var st = (j.status_code !== undefined && j.status_code !== null && ![0, 1].includes(j.status_code))
        ? '故障码 ' + j.status_code
        : (j.disabled === true ? '已失能' : (j.disabled === false ? '已使能' : '-'));
      return '<tr><td>' + j.id + '</td><td>' + Number(j.max_speed).toFixed(2) + ' rad/s</td><td>' +
        Number(j.acceleration).toFixed(2) + ' rad/s²</td><td>' + Number(j.deceleration).toFixed(2) +
        ' rad/s²</td><td>' + st + '</td></tr>';
    }).join('');
    A.el('motorState').textContent = (result.side === 'right' ? '右从臂' : '当前 / 左从臂') + ' · ' +
      result.port + ' · ' + new Date(result.timestamp * 1000).toLocaleTimeString() + ' 回读';
  }

  async function run(action, persist) {
    if (busy) return;
    if (!serviceReady) { await checkService(); return; }
    var joints = selected();
    if (!joints.length) throw new Error('请至少选择一个关节');
    await A.autosave.flush();
    var cfg = await A.api.get('/api/config'), dual = !!(cfg.dual_arm || {}).enabled;
    var side = dual ? (A.el('motorArm').value || 'left') : 'left';
    if (!dual && A.el('motorArm').value !== 'left') A.el('motorArm').value = 'left';
    var arm = dual ? (cfg.dual_arm[side] || {}) : (cfg.follower || {}), port = dual ? arm.follower_port : arm.port;
    var who = (dual ? (side === 'left' ? '左从臂' : '右从臂') : '当前从臂') + ' · ' + port;
    if (!port) throw new Error('请先配置目标从臂串口');

    var req = { side: side, action: action, joints: joints, persist: !!persist, expected_port: port };
    if (action === 'write') {
      var spd = Number(A.el('motorMaxSpeed').value);
      var acc = Number(A.el('motorAccel').value);
      var dec = Number(A.el('motorDecel').value);
      if (!Number.isFinite(spd) || spd <= 0 || spd > 100) throw new Error('最大速度 (MAX_SPD) 需在 (0, 100] rad/s 范围内');
      if (!Number.isFinite(acc) || acc <= 0 || acc > 100) throw new Error('加速度 (ACC) 需在 (0, 100] rad/s² 范围内');
      if (!Number.isFinite(dec) || dec >= 0 || dec < -100) throw new Error('减速度 (DEC) 需在 [-100, 0) rad/s² 范围内（负值）');
      var text = '目标：' + who + '\n关节：' + joints.join('、') +
        '\n最大速度 (MAX_SPD)：' + spd + ' rad/s\n加速度 (ACC)：' + acc + ' rad/s²\n减速度 (DEC)：' + dec + ' rad/s²\n' +
        (persist ? '将写入并保存到电机 Flash（要求整臂已失能，永久生效）。' : '将写入电机寄存器（立即生效，断电后恢复）。');
      if (!await A.confirm(text, { title: persist ? '二次确认：写入并保存到 Flash' : '二次确认：应用电机速度与加速度' })) return;
      req.confirm = true;
      req.max_speed = spd;
      req.acceleration = acc;
      req.deceleration = dec;
    }

    busy = true;
    status(A.status());
    A.clearAlert('#motorAlert');
    var phase = 'start';
    try {
      var r = await A.api.post('/api/devices/motor-settings/start', req);
      if (!r || typeof r.job_id !== 'string' || !r.job_id) {
        throw new Error('后端没有返回电机设置任务编号，请更新控制服务后重试');
      }
      phase = 'result';
      A.el('motorState').textContent = '操作已启动，正在等待电机反馈…';
      var end = Date.now() + 90000;
      while (!stopped && Date.now() < end) {
        var progress = await A.api.get('/api/devices/motor-settings/result?job_id=' + encodeURIComponent(r.job_id));
        if (progress.finished) {
          if (progress.exit_code !== 0 || !progress.result) {
            if (progress.result && progress.result.joints) paint(progress.result);
            throw new Error((progress.result || {}).error || progress.last_line || '操作失败，请查看全局任务日志');
          }
          paint(progress.result);
          A.setAlert('#motorAlert', action === 'write'
            ? (persist ? '所选关节速度与加速度已写入、保存到 Flash 并回读确认。' : '所选关节速度与加速度已写入并回读确认（临时生效）。')
            : '所选关节当前速度与加速度参数已更新。', 'ok');
          break;
        }
        A.el('motorState').textContent = progress.last_line || '正在等待电机参数响应…';
        await new Promise(function (resolve) { setTimeout(resolve, 400); });
      }
      if (!stopped && Date.now() >= end) throw new Error('操作等待超时，请查看全局任务日志');
    } catch (error) {
      if (error.status === 404 && phase === 'start') {
        serviceReady = false;
        A.el('motorService').textContent = '电机设置接口未加载';
        A.el('motorRetryService').hidden = false;
      }
      A.el('motorState').textContent = phase === 'result' ? '本次操作结果未确认，请查看错误说明' : '操作未完成';
      throw new Error(errorMessage(error, phase));
    } finally {
      busy = false;
      await A.refreshStatus();
      status(A.status());
    }
  }

  A.onStatus(status);
  status(A.status());
  A.click('motorRetryService', checkService);
  A.change('motorArm', function () {
    A.el('motorReadings').innerHTML = '<tr><td colspan="5">目标已变化，请重新读取当前参数。</td></tr>';
    A.el('motorState').textContent = '未读取当前所选从臂';
    A.clearAlert('#motorAlert');
  });
  A.el('motorAccel').addEventListener('input', function () {
    if (!decelTouched) {
      var v = Number(A.el('motorAccel').value);
      if (Number.isFinite(v) && v > 0) A.el('motorDecel').value = (-v).toFixed(1).replace(/\.0$/, '');
    }
  });
  A.el('motorDecel').addEventListener('input', function () { decelTouched = true; });
  A.click('motorRead', function () { A.action(function () { return run('read', false); }, '#motorAlert'); });
  A.click('motorApply', function () { A.action(function () { return run('write', false); }, '#motorAlert'); });
  A.click('motorSaveFlash', function () { A.action(function () { return run('write', true); }, '#motorAlert'); });
  A.click('motorSelectAll', function () {
    A.qa('#motorJoints input').forEach(function (n) {
      n.checked = true;
      n.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
  window.addEventListener('pagehide', function () { stopped = true; });
  checkService();
  try {
    await A.autosave.preferences(
      ['motorArm', 'motorMaxSpeed', 'motorAccel', 'motorDecel',
       'motorJoint1', 'motorJoint2', 'motorJoint3', 'motorJoint4', 'motorJoint5', 'motorJoint6', 'motorJoint7'],
      'motor-settings'
    );
    var cur = A.status();
    if (!(cur && cur.dual_arm && cur.dual_arm.enabled) || !A.el('motorArm').value) {
      A.el('motorArm').value = 'left';
    }
  } catch (error) {
    A.setAlert('#motorAlert', error.message, 'err');
  }
})();
