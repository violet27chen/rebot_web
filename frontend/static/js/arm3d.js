/* 从臂 3D 模型：官方 URDFLoader + 本机 Seeed B601-DM 的 URDF/STL。
   电机读数（rad）→ URDF 关节角：joint2 取反（电机方向与 URDF 轴相反，其余同向）。 */
(function () {
  "use strict";
  var SIGN = { joint1: 1, joint2: -1, joint3: 1, joint4: 1, joint5: 1, joint6: 1 };

  function init(canvasId, urdfUrl) {
    if (!window.THREE || !window.URDFLoader) return null;
    var canvas = document.getElementById(canvasId);
    if (!canvas) return null;
    var renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(45, 4 / 3, 0.05, 12);
    var TARGET = new THREE.Vector3(0, 0.25, 0);
    var sph = new THREE.Spherical(0.95, 1.02, -0.9);
    function applyCamera() {
      camera.position.setFromSpherical(sph).add(TARGET);
      camera.lookAt(TARGET);
    }
    applyCamera();

    scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    var dl = new THREE.DirectionalLight(0xffffff, 0.85);
    dl.position.set(1.5, 2.5, 1.2);
    scene.add(dl);
    var dl2 = new THREE.DirectionalLight(0xffffff, 0.3);
    dl2.position.set(-1.2, 0.8, -1);
    scene.add(dl2);
    var grid = new THREE.GridHelper(0.8, 16, 0x8899aa, 0xccdddd);
    scene.add(grid);

    var statusCb = null;
    var robot = null;
    var loader = new URDFLoader();
    loader.packages = { "": "/static/arm_urdf/" };   // URDF 里 filename="meshes/xxx.STL"
    // 0.13 的 loadMeshCb 签名是 (path, manager, material, done)
    loader.loadMeshCb = function (path, manager, material, done) {
      new THREE.STLLoader().load(path, function (geo) {
        geo.computeVertexNormals();
        done(new THREE.Mesh(geo, material || new THREE.MeshStandardMaterial({
          color: 0x8fa8cf, metalness: 0.2, roughness: 0.7 })));
      }, undefined, function (err) { done(null, err); });
    };
    loader.load(urdfUrl, function (r) {
      robot = r;
      robot.rotation.x = -Math.PI / 2;   // URDF z-up → three.js y-up
      scene.add(robot);
      if (statusCb) statusCb();
    });

    function resize() {
      var w = canvas.clientWidth || 420, h = canvas.clientHeight || 320;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      applyCamera();
    }
    resize();
    window.addEventListener("resize", resize);
    if(window.ResizeObserver)new ResizeObserver(resize).observe(canvas);

    var dragging = false, lx = 0, ly = 0;
    canvas.addEventListener("mousedown", function (e) { dragging = true; lx = e.clientX; ly = e.clientY; });
    window.addEventListener("mouseup", function () { dragging = false; });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      sph.theta -= (e.clientX - lx) * 0.008;
      sph.phi = Math.max(0.12, Math.min(1.5, sph.phi - (e.clientY - ly) * 0.008));
      lx = e.clientX; ly = e.clientY;
      applyCamera();
    });
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      sph.radius = Math.max(0.35, Math.min(2.6, sph.radius + e.deltaY * 0.0015));
      applyCamera();
    }, { passive: false });
    canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });

    function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); }
    animate();

    return {
      resize: resize,
      onLoad: function (cb) { statusCb = cb; if (robot) cb(); },
      set: function (q) {
        if (!robot || !q) return;
        for (var i = 0; i < 6; i++) {
          var j = robot.joints["joint" + (i + 1)];
          if (j && q[i] !== undefined) {
            j.setJointValue((SIGN["joint" + (i + 1)] || 1) * q[i]);
          }
        }
      }
    };
  }

  /* One world scene, two official URDF instances, one measured baseline. */
  function initWorkspace(canvasId, urdfUrl) {
    if(!window.THREE||!window.URDFLoader)return null;
    var canvas=document.getElementById(canvasId);if(!canvas)return null;
    var renderer=new THREE.WebGLRenderer({canvas:canvas,antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));
    var scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(42,1,.02,30),target=new THREE.Vector3(0,.2,-.1);
    var spherical=new THREE.Spherical(1.5,1.03,.18),group=new THREE.Group();scene.add(group);
    scene.add(new THREE.AmbientLight(0xffffff,.85));
    [[1.5,2.5,1.2,.85],[-1.2,.8,-1,.3]].forEach(function(a){var light=new THREE.DirectionalLight(0xffffff,a[3]);light.position.set(a[0],a[1],a[2]);scene.add(light);});
    var grid=new THREE.GridHelper(2,20,0x8899aa,0xccdddd);scene.add(grid);
    var forward=new THREE.ArrowHelper(new THREE.Vector3(0,0,-1),new THREE.Vector3(0,.003,.12),.3,0x3478df,.05,.025);scene.add(forward);
    var leftRoot=new THREE.Group(),rightRoot=new THREE.Group();group.add(leftRoot,rightRoot);
    // Both URDFs use the same rigid rotation: z-up to y-up, +X forward to -Z.
    leftRoot.rotation.y=rightRoot.rotation.y=Math.PI/2;
    var robots={},angles={},dual=false,distance=null,onLoaded,onError,loaded=false,demonstrationDistance=0,basePositions=null;
    leftRoot.name='left_follower_root';rightRoot.name='right_follower_root';
    var bound={},measured={},stale={left:true,right:true},labels={};
    ['left','right'].forEach(function(side){var n=document.createElement('div');n.className='monitor-model-binding is-stale';n.dataset.side=side;n.hidden=true;labels[side]=n;});
    function bind(side,value){if(side!=='left'&&side!=='right')return;bound[side]=Object.assign({},value);var label=labels[side];label.textContent=(value.label||(side==='left'?'左从臂':'右从臂'))+' · '+(value.canonical_port||value.port||'未配置');label.title=(value.port||'未配置')+(value.canonical_port&&value.port!==value.canonical_port?' → '+value.canonical_port:'');label.dataset.port=value.canonical_port||value.port||'';if(robots[side])robots[side].userData.port=label.dataset.port;}
    function clear(side){if(side!=='left'&&side!=='right')return;setJoint(side,[0,0,0,0,0,0]);measured[side]=false;stale[side]=true;}
    function labelPositions(){
      if(!loaded)return;var parent=canvas.parentElement;if(!parent)return;
      var rect=canvas.getBoundingClientRect(),host=parent.getBoundingClientRect();group.updateMatrixWorld(true);camera.updateMatrixWorld(true);
      ['left','right'].forEach(function(side){var n=labels[side],root=side==='left'?leftRoot:rightRoot;if(n.parentElement!==parent)parent.appendChild(n);n.hidden=side==='right'&&!dual;if(n.hidden)return;
        var point=root.localToWorld(new THREE.Vector3(0,.025,0)).project(camera);n.hidden=point.z<-1||point.z>1;if(n.hidden)return;
        n.style.left=(rect.left-host.left+Math.max(8,Math.min(rect.width-8,(point.x+1)*rect.width/2)))+'px';n.style.top=(rect.top-host.top+Math.max(8,Math.min(rect.height-25,(1-point.y)*rect.height/2)))+'px';n.classList.toggle('is-stale',stale[side]||!measured[side]);
      });
    }

    var lineGeometry=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(),new THREE.Vector3()]);
    var baseline=new THREE.Line(lineGeometry,new THREE.LineBasicMaterial({color:0x3478df}));scene.add(baseline);
    function view(){camera.position.setFromSpherical(spherical).add(target);camera.lookAt(target);}
    function setJoint(side,q){if(side!=='left'&&side!=='right')return;angles[side]=q&&q.slice();measured[side]=!!q;stale[side]=!q;var robot=robots[side];if(!robot||!q)return;
      for(var i=0;i<6;i++){var joint=robot.joints['joint'+(i+1)];if(joint&&q[i]!==undefined)joint.setJointValue(SIGN['joint'+(i+1)]*q[i]);}}
    function fit(){if(!loaded)return;group.updateMatrixWorld(true);var box=new THREE.Box3().setFromObject(leftRoot),size=new THREE.Vector3();if(dual)box.union(new THREE.Box3().setFromObject(rightRoot));box.getSize(size);box.getCenter(target);
      var horizontal=size.x/Math.max(camera.aspect,.25),dimension=Math.max(size.y,horizontal,size.z*.7,.25);
      spherical.radius=Math.max(.65,dimension/(2*Math.tan((camera.fov*Math.PI/360)))*1.45);view();}
    function position(){
      if(!loaded)return;var spacing=distance===null?demonstrationDistance:distance/1000;
      rightRoot.visible=dual;
      if(dual&&basePositions){
        var l=basePositions.left,r=basePositions.right,mid=l.map(function(v,i){return (v+r[i])/2;});
        leftRoot.position.set((l[0]-mid[0])/1000,(l[2]-mid[2])/1000,-(l[1]-mid[1])/1000);
        rightRoot.position.set((r[0]-mid[0])/1000,(r[2]-mid[2])/1000,-(r[1]-mid[1])/1000);
      }else{leftRoot.position.set(dual?-spacing/2:0,0,0);rightRoot.position.set(spacing/2,0,0);}
      baseline.visible=dual&&distance!==null;
      var points=baseline.geometry.attributes.position;points.setXYZ(0,leftRoot.position.x,leftRoot.position.y+.002,leftRoot.position.z);points.setXYZ(1,rightRoot.position.x,rightRoot.position.y+.002,rightRoot.position.z);points.needsUpdate=true;baseline.geometry.computeBoundingSphere();
      grid.position.y=Math.min(leftRoot.position.y,dual?rightRoot.position.y:leftRoot.position.y);forward.position.y=grid.position.y+.003;
      var scale=Math.max(1,(spacing+1)/2);grid.scale.set(scale,1,scale);fit();
    }
    var manager=new THREE.LoadingManager(),template,resourceError=null;
    manager.onLoad=function(){if(!template||loaded||resourceError)return;
      robots.left=template;robots.right=template.clone(true);
      for(var id in robots.left.joints){if(robots.left.joints[id]===robots.right.joints[id]){resourceError='左右官方模型关节实例未隔离';if(onError)onError(resourceError);return;}}
      ['left','right'].forEach(function(side){robots[side].name=side+'_follower_model';robots[side].userData.side=side;if(bound[side])bind(side,bound[side]);});
      robots.left.rotation.x=robots.right.rotation.x=-Math.PI/2;leftRoot.add(robots.left);rightRoot.add(robots.right);
      // This layout-only spacing is NEVER saved or presented as a real baseline.
      var bounds=new THREE.Box3().setFromObject(leftRoot),size=new THREE.Vector3();bounds.getSize(size);demonstrationDistance=Math.max(size.x,size.z)*1.35;
      loaded=true;position();Object.keys(angles).forEach(function(side){var known=measured[side],wasStale=stale[side];setJoint(side,angles[side]);measured[side]=known;stale[side]=wasStale;});if(onLoaded)onLoaded();
    };
    manager.onError=function(url){resourceError='官方模型资源未加载：'+url;if(onError)onError(resourceError);};
    var loader=new URDFLoader(manager);loader.packages={'':'/static/arm_urdf/'};
    loader.loadMeshCb=function(path,manager,material,done){new THREE.STLLoader(manager).load(path,function(geometry){geometry.computeVertexNormals();done(new THREE.Mesh(geometry,material||new THREE.MeshStandardMaterial({color:0x8fa8cf,metalness:.2,roughness:.7})));},undefined,function(error){done(null,error);});};
    loader.load(urdfUrl,function(robot){template=robot;},undefined,function(){if(onError)onError('官方 URDF 加载失败');});
    function resize(){var w=Math.max(1,canvas.clientWidth),h=Math.max(1,canvas.clientHeight);renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();if(loaded)fit();else view();}
    var observer=new ResizeObserver(resize);observer.observe(canvas);window.addEventListener('resize',resize);resize();
    var pointers=new Map(),lastPinch=0;
    canvas.addEventListener('pointerdown',function(e){pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',function(e){var previous=pointers.get(e.pointerId);if(!previous)return;
      if(pointers.size===1){spherical.theta-=(e.clientX-previous.x)*.008;spherical.phi=Math.max(.1,Math.min(1.5,spherical.phi-(e.clientY-previous.y)*.008));}
      pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
      if(pointers.size===2){var values=Array.from(pointers.values()),d=Math.hypot(values[0].x-values[1].x,values[0].y-values[1].y);if(lastPinch)spherical.radius=Math.max(.3,Math.min(12,spherical.radius*lastPinch/d));lastPinch=d;}view();
    });
    function end(e){pointers.delete(e.pointerId);if(pointers.size<2)lastPinch=0;}
    canvas.addEventListener('pointerup',end);canvas.addEventListener('pointercancel',end);
    canvas.addEventListener('wheel',function(e){e.preventDefault();spherical.radius=Math.max(.3,Math.min(12,spherical.radius*(1+e.deltaY*.001)));view();},{passive:false});
    canvas.addEventListener('contextmenu',function(e){e.preventDefault();});
    function animate(){requestAnimationFrame(animate);labelPositions();renderer.render(scene,camera);}animate();
    return {bind:bind,clear:clear,markStale:function(side,value){stale[side]=!!value;},getState:function(side){var r=robots[side];return {side:side,port:(bound[side]||{}).canonical_port||(bound[side]||{}).port||'',measured:!!measured[side],modelId:r?r.uuid:null,joints:r?[1,2,3,4,5,6].map(function(i){return r.joints['joint'+i].jointValue[0];}):null};},set:setJoint,resize:resize,fit:fit,layout:function(settings){var change=dual!==!!settings.dual||distance!==settings.distance_mm||JSON.stringify(basePositions)!==JSON.stringify(settings.base_positions_mm||null);dual=!!settings.dual;distance=settings.distance_mm;basePositions=settings.base_positions_mm||null;if(change)position();},
      onLoad:function(cb){onLoaded=cb;if(loaded)cb();},onError:function(cb){onError=cb;}};
  }

  window.ARM3D = { initWorkspace:initWorkspace, init: init, set: function (inst, q) { if (inst) inst.set(q); } };
})();
