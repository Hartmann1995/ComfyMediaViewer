/* ComfyMediaViewer · js/newfeed.js（2026-11-04）
 *
 * 需求：一边往下翻老图，一边还能看到最新生成的图，不用反复拖回瀑布流顶部。
 *
 * 做法（P2 角落浮窗）：
 *   1. 身处深处（#gridwrap.scrollTop > ZONE）时检测到目录变化：**不刷新主网格**，
 *      拉一次 /api/list 做 diff，只有「纯新增」才收进左下角浮窗；
 *   2. 一旦出现删除 / 改名 / 替换（本地列表已经不真实），仍然刷新 —— 但走
 *      silentReload 的锚点恢复，位置不会丢（见 restoreAnchor）；
 *   3. 滚回顶部（≤ ZONE）自动并入；点浮窗缩略图 = 并入并打开这张。
 *
 * 为什么深处不能把新图插进主网格（设计稿阶段实测）：
 *   主网格是 CSS Grid 行优先布局，头部插入 N 张会让后续卡片整体顺移 N 格；
 *   滚动补偿只能锁住**垂直**位置，锁不住格子（实测 P1 手动并入：偏移 13px 纹丝不动，
 *   但锚点卡从 #1160 变成了同排的 #1163）。对正在看老图的人来说，那和跳走一样难受。
 *
 * 为什么判据用「锚点卡 + 它的视口偏移」而不是「视野第一张是谁」：
 *   新图合法占据第一格时，第一张当然会变，可你正在看的那张并没有动 ——
 *   用首张判会制造假警报，进而把「正常」误报成「打断了」。
 */
"use strict";
(function(){
  const ZONE = 240;          // 顶部区阈值（px）：≤ 它就算「人在顶部」
  const MAX  = 60;           // 队列上限，避免长时间挂着越攒越多
  const SHOW = 3;            // 浮窗里展示几张
  const LS   = "mv.newfeed.collapsed";

  const NF = {
    fresh: [],               // 倒序：fresh[0] 最新
    scope: null,
    items: null,
    collapsed: false,
    joining: false,
    busy: false,
    toasted: false,
    defer: null,             // 4.2-15：忙期间被吞掉的目录变化（忙完补一拍）
  };
  let box = null, shots = null, cnt = null, raf = 0;

  try{ NF.collapsed = localStorage.getItem(LS) === "1"; }catch(_){}

  /* ---------------- DOM ---------------- */
  function ensure(){
    if(box) return;
    box = document.createElement("div");
    box.id = "newPip";
    box.hidden = true;
    box.innerHTML =
      '<div class="nf-hd">'
      /* 收起后才显示的"新图"标志：四角星（✦），与「新生成」的语义对齐。
         之前收起态是一颗只有数字的空圆，看不出它是什么、也不知道能点开。 */
      + '<svg class="nf-ico" viewBox="0 0 24 24" aria-hidden="true">'
      +   '<path d="M12 2.4 13.95 10.05 21.6 12 13.95 13.95 12 21.6 10.05 13.95 2.4 12 10.05 10.05Z"'
      +   ' fill="currentColor"/></svg>'
      + '<span class="nf-lbl">最新</span>'
      + '<span class="nf-n" aria-live="polite"></span>'
      /* 收起按钮原来是个**没有内容的空 button**：既不显示任何图标，又被全局
         button 的 min-height/padding 撑到 28×24，把整个头部顶高。现在补图标并压尺寸。 */
      + '<button class="nf-mini" type="button" title="收起到角落" aria-label="收起新图浮窗">'
      +   '<svg viewBox="0 0 12 12" aria-hidden="true">'
      +   '<path d="M3.2 6h5.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
      +   '</svg></button>'
      + '</div>'
      + '<div class="nf-shots"></div>';
    document.body.appendChild(box);
    shots = box.querySelector(".nf-shots");
    cnt   = box.querySelector(".nf-n");

    box.querySelector(".nf-mini").addEventListener("click", ev=>{
      ev.stopPropagation();
      NF.collapsed = !NF.collapsed;
      saveCollapsed();
      render();
    });
    /* 收起态整块可点：展开 */
    box.addEventListener("click", ()=>{
      if(!NF.collapsed) return;
      NF.collapsed = false;
      saveCollapsed();
      render();
    });
  }
  function saveCollapsed(){
    try{ localStorage.setItem(LS, NF.collapsed ? "1" : "0"); }catch(_){}
  }

  function currentScope(){
    const path = normPath(S.path || S.root);
    return JSON.stringify([path, !!$("#rec")?.checked]);
  }

  function syncQueue(){
    const scope = currentScope();
    if(NF.scope === scope && NF.items === S.items) return;
    if(NF.scope !== scope) NF.defer = null;
    NF.scope = scope;
    NF.items = S.items;
    NF.fresh = [];
    NF.toasted = false;
  }

  /* ---------------- 渲染 ---------------- */
  function render(){
    syncQueue();
    ensure();
    /* 只在「普通浏览」里出现：收藏 / 最近 / 已标记 / 批量 / 单图 都各自有语义，
       在那里挂一个新图浮窗只会误导（那些视图的数据源根本不是当前目录）。 */
    const show = NF.fresh.length > 0 && !favMode && !libMode && !batchOn && !S.solo
                 && document.body.dataset.view !== "cmp";
    box.hidden = !show;
    if(!show) return;

    box.classList.toggle("collapsed", NF.collapsed);
    box.title = NF.collapsed ? "展开新图浮窗" : "";
    const n = NF.fresh.length;
    cnt.textContent = NF.collapsed ? (n > 99 ? "99+" : String(n))
                                   : "+" + (n > 99 ? "99+" : n);

    if(NF.collapsed){ shots.innerHTML = ""; return; }

    shots.innerHTML = "";
    for(const it of NF.fresh.slice(0, SHOW)){
      const d = document.createElement("div");
      /* 内容安全与网格卡片同一套判据：nsfwShouldVeil 已经包含「已揭开 / 显示模式」的豁免 */
      d.className = "nf-shot" + (nsfwShouldVeil(it) ? " veil" : "");
      d.dataset.p = it.path;
      d.title = (it.name || "") + " · 点击并入并打开";
      const im = document.createElement("img");
      im.alt = "";
      im.decoding = "async";
      im.loading = "lazy";
      im.src = S.hasThumb ? thumbURL(it, 2) : fileURL(it.path, it);
      im.onerror = ()=>{ im.style.visibility = "hidden"; };
      d.appendChild(im);
      d.addEventListener("click", ev=>{ ev.stopPropagation(); openFresh(it.path); });
      shots.appendChild(d);
    }
  }

  function bumpIn(){
    if(!box || box.hidden) return;
    box.classList.remove("nf-in");
    void box.offsetWidth;                 // 强制回流，动画可重放
    box.classList.add("nf-in");
  }

  /* ---------------- 收新图 ---------------- */
  function push(list){
    if(!list || !list.length) return 0;
    const seen = new Set(NF.fresh.map(x=>x.path));
    const add = list.filter(x => x && x.path && !seen.has(x.path));
    if(!add.length) return 0;
    NF.fresh = add.concat(NF.fresh).slice(0, MAX);
    render();
    bumpIn();
    /* 只提示第一次：每 5 秒弹一次会把「安静」这个卖点自己毁掉 */
    if(!NF.toasted){
      NF.toasted = true;
      try{
        toast(add.length + " 张新图在左下角；滚回顶部会自动并入", "ok", 5200);
      }catch(_){}
    }
    return add.length;
  }

  /* ---------------- 并入 ---------------- */
  async function join(){
    syncQueue();
    if(NF.joining || !NF.fresh.length) return 0;
    const scope = NF.scope, items = NF.items;
    NF.joining = true;
    const n = NF.fresh.length;
    /* A15（2026-11-05 审计复核）：先留底。旧代码把 `NF.fresh = []` 放在 try 之前，
       而 `load()` 的 catch 自己吞异常 ⇒ `await` 不抛 ≠ 刷新成功 ⇒ 一律 toast
       「n 张新图已并入」。真失败时这批图既没进列表、又从队列里没了 ⇒ **永久丢失**，
       只能等用户手动刷新目录才会再出现。 */
    const batch = NF.fresh.slice();
    NF.fresh = [];
    NF.toasted = false;
    render();
    let ok = true;
    try{
      /* 看返回值，不看「有没有抛」：silentReload / load 都以 false 表示没生效。 */
      const r = (typeof silentReload === "function")
        ? await silentReload()
        : await load(S.path || S.root, {silent:true});
      ok = (r !== false);
    }catch(e){
      ok = false;
    }
    try{
      if(scope !== currentScope()) ok = false;
      if(ok){
        toast(n + " 张新图已并入", "ok");
      }else if(scope === currentScope() && items === S.items){
        /* 放回队列，下次轮询自动重试。合并期间轮询可能又收进了新的 ⇒ 按 path 去重、
           新收的排前面，并保持与 push() 同一个 MAX 上限。 */
        const seen = new Set();
        NF.fresh = batch.concat(NF.fresh).filter(x => {
          if(!x || !x.path || seen.has(x.path)) return false;
          seen.add(x.path); return true;
        }).slice(0, MAX);
        render();
        toastErr("并入新图失败，稍后自动重试", "");
      }
    }catch(_){}
    NF.joining = false;
    _flushDefer();                       // 4.2-15：并入期间被吞掉的目录变化补一拍
    return ok ? n : 0;
  }

  /* ---------------- 从浮窗打开 → 关闭后回到原位 ----------------
     为什么必须有这一段：浮窗里点开的新图在**列表最前面**，`openLb(i)` 会把 S.idx
     指向它。关大图时 nav.js 的 closeLb 会调 mvScrollToCurrentCard()「让当前那张卡可见」，
     于是整页被 scrollIntoView 拽到列表头部 —— 实测深处 y 9576 → **0**，正好毁掉
     「一边翻老图一边看新图」这件事本身（那和直接点右下角回顶端没区别）。
     所以打开前先记下「你在哪」，关闭时归还，并且**不要让 closeLb 再去滚当前卡**。 */
  let backSpot = null;

  function takeBack(){
    const gw = document.getElementById("gridwrap");
    if(!gw) return null;
    return { dir: normPath(S.path || S.root), y: Math.round(gw.scrollTop),
             shown: S.shown, anchor: gridAnchor() };
  }

  /* 返回值语义：true = 这条关闭已经处理完，closeLb 不要再滚当前卡。 */
  function returnSpot(){
    const b = backSpot;
    if(!b) return false;
    backSpot = null;
    const gw = document.getElementById("gridwrap");
    if(!gw) return true;
    if(b.dir && normPath(S.path || S.root) !== b.dir) return true;   // 期间换了目录：别乱滚
    if(b.anchor && (S.view || []).some(it => it.path === b.anchor.path)){
      restoreAnchor(b.anchor, b.shown);        // 按锚点卡 + 原偏移精确归位
    }else if(Math.abs(gw.scrollTop - b.y) > 2){
      gw.scrollTop = b.y;                      // 锚点没了（被删/被筛）：退回到原像素
    }
    return true;
  }

  async function openFresh(path){
    const scope = currentScope();
    if(NF.fresh.some(x=>x.path === path)) await join();
    if(scope !== currentScope()) return;
    const i = (S.view || []).findIndex(it => it.path === path);
    if(i >= 0){
      backSpot = takeBack();                   // join 已完成，此刻的 scrollTop 就是正确位置
      openLb(i);
      return;
    }
    try{ toast("这张图已不在当前列表（可能被移走或被筛选掉了）", "err"); }catch(_){}
  }

  /* ---------------- 锚点：刷新时保住"你正在看的那张" ----------------
     只对「删除 / 改名 / 替换」这条路径生效（纯新增走浮窗，主网格压根不刷新）。 */
  function gridAnchor(){
    const gw = document.getElementById("gridwrap");
    if(!gw || !grid) return null;
    const g = gw.getBoundingClientRect();
    const kids = grid.children;
    for(let i=0;i<kids.length;i++){
      const c = kids[i], r = c.getBoundingClientRect();
      /* 第一個"底边越过视口顶"且有路径的卡片 —— 合成导航卡没有 data-p，跳过 */
      if(r.height && r.bottom > g.top + 8 && c.dataset.p){
        /* offsetTop 与 scrollTop 同一坐标系（#gridwrap 是 offsetParent），
           比 rect 稳定：不受滚动条/亚像素取整影响，恢复时可以直接算出唯一的 scrollTop。 */
        return { path:c.dataset.p, off:r.top - g.top, top:c.offsetTop };
      }
    }
    return null;
  }

  function restoreAnchor(a, wantShown){
    const gw = document.getElementById("gridwrap");
    if(!gw || !a || !grid) return;
    const idx = (S.view || []).findIndex(it => it.path === a.path);

    /* 补齐渲染量：至少到锚点，且不低于刷新前已渲染的数量。
       只补到锚点的话滚动条长度会突然缩水（看着像"后面的图没了"）；
       连带把「旧进度」还回来，也顺手满足静默刷新"回到原位"的既有口径。 */
    const cap = Math.max(0, Math.min((S.view || []).length - 1, (wantShown || 1) - 1));
    const target = idx >= 0 ? Math.max(idx, cap) : cap;
    if(target > 0 && S.shown <= target && typeof renderUpTo === "function"){
      try{ renderUpTo(target); }catch(_){}
    }
    if(idx < 0) return;               // 那张图本身被删了：无锚可对，位置交给补齐后的高度

    const sel = '[data-p="' + ((window.CSS && CSS.escape) ? CSS.escape(a.path) : a.path) + '"]';
    const fix = ()=>{
      const el = grid.querySelector(sel);
      if(!el) return false;
      /* 直接算目标值而不是"累加差值"：累加会把上一帧的取整误差一起带进下一帧。 */
      const want = el.offsetTop - a.off;
      if(Math.abs(gw.scrollTop - want) > 0.6) gw.scrollTop = want;
      return true;
    };
    fix();
    /* 多帧修正：第一批图挂上后行高会微调（缩略图解码、content-visibility 的
       --cv-h 重测），一帧不够 —— 实测只修两帧会残留约 15px 的漂移。 */
    requestAnimationFrame(()=>{ fix(); requestAnimationFrame(fix); });
    setTimeout(fix, 120);
    setTimeout(fix, 420);
  }

  /* ---------------- 目录变化：接管判断 ---------------- */
  /* 返回值语义（给 extras.js 的 pollDir 用）：
       true  = 这次变化已经处理完，**不要**刷新主网格
       false = 交回原有逻辑（silentReload） */
  /* 4.2-15（2026-11-05 审计）：busy / joining 期间这一拍会被 extras.js 的 lastSig
     消费掉 —— 只 return true 就永远不再来（要等下一次目录变化才补）。
     记下 path，忙完主动补一拍。 */
  function _flushDefer(){
    if(!NF.defer || NF.joining || NF.busy) return;
    const {path, scope} = NF.defer;
    NF.defer = null;
    setTimeout(async ()=>{
      if(scope !== currentScope() || !canFollow()) return;
      const gen = viewGen;
      try{
        const handled = await onDirChange(path);
        if(!handled && gen === viewGen && scope === currentScope() && canFollow()){
          await silentReload("检测到目录变化，已刷新");
        }
      }catch(_){}
    }, 0);
  }

  function canFollow(){
    return !favMode && !libMode && !batchOn && !S.solo
      && document.body.dataset.view !== "cmp";
  }

  async function onDirChange(path){
    syncQueue();
    if(normPath(path) !== normPath(S.path || S.root) || !canFollow()) return true;
    const scope = NF.scope;
    if(NF.joining || NF.busy){ NF.defer = {path, scope}; return true; }
    const gw = document.getElementById("gridwrap");
    if(!gw || gw.scrollTop <= ZONE) return false;       // 在顶部：新图就该直接出现

    const gen = viewGen;
    NF.busy = true;
    try{
      /* 与 load() 用同一个端点、同一组参数 —— 不能自己造一个"轻量列表"，
         否则 diff 口径会和主列表不一致（rec 开关一变就是两套数据）。 */
      const j = await api("/api/list?path=" + encodeURIComponent(path)
        + "&rec=" + ($("#rec").checked ? "1" : "0"));
      if(gen !== viewGen || scope !== currentScope() || !canFollow()) return true;
      if(!j || !Array.isArray(j.items) || !Array.isArray(j.dirs)) return false;
      if(normPath(j.path) !== normPath(S.path || S.root)) return false;

      const oldDirs = new Map((S.dirs || []).map(dir=>[dir.path, dir.n]));
      if(j.dirs.length !== oldDirs.size || j.dirs.some(dir=>!oldDirs.has(dir.path))) return false;

      const oldMap = new Map(S.items.map(it=>[it.path, it]));
      const newMap = new Map(j.items.map(it=>[it.path, it]));
      let removed = 0, changed = 0;
      for(const it of S.items){
        if(it.type === "dir") continue;
        if(!newMap.has(it.path)) removed++;
      }
      for(const it of j.items){
        const o = oldMap.get(it.path);
        if(o && (o.mtime !== it.mtime || o.size !== it.size)) changed++;
      }
      /* 有删除或内容替换：本地这份已经不真实（点开会 404 / 显示旧图），
         必须刷新。此时不接管，让 silentReload 带着锚点刷新。 */
      if(removed || changed) return false;

      const added = j.items.filter(it => it.type !== "dir" && !oldMap.has(it.path));
      if(!added.length && j.dirs.some(dir=>oldDirs.get(dir.path) !== dir.n)) return false;
      const remaining = NF.fresh.filter(it=>newMap.has(it.path));
      if(remaining.length !== NF.fresh.length){
        NF.fresh = remaining;
        if(!remaining.length) NF.toasted = false;
        render();
      }
      push(added);
      return true;
    }catch(_){
      return false;                        // 拉取失败：退回原逻辑，至少还能刷新
    }finally{
      NF.busy = false;
      _flushDefer();                     // 4.2-15：比对期间被吞掉的目录变化补一拍
    }
  }

  /* ---------------- 滚动：回顶自动并入 ---------------- */
  function onScroll(){
    /* 2026-11-05 P2：批量选择期间不回顶并入。join() → silentReload() → load()，
       而 app.js 里 `if(batchOn) setBatch(false)` 是无条件的 ⇒ 勾了 200 张、
       滚回顶部收新图就把勾选全清。extras.js 的定时刷新早就挡了 batchOn，这里漏了。 */
    if(raf || !NF.fresh.length || NF.joining || !canFollow()) return;
    raf = requestAnimationFrame(()=>{
      raf = 0;
      if(!NF.fresh.length || NF.joining || !canFollow()) return;
      const gw = document.getElementById("gridwrap");
      if(gw && gw.scrollTop <= ZONE) join();
    });
  }

  /* ---------------- 对外 ---------------- */
  window.nfOnDirChange   = onDirChange;
  window.nfGridAnchor    = gridAnchor;
  window.nfRestoreAnchor = restoreAnchor;
  window.nfReturnSpot    = returnSpot;      // 关大图时由 nav.js closeLb 调用
  window.nfRender        = render;

  const gw0 = document.getElementById("gridwrap");
  if(gw0) gw0.addEventListener("scroll", onScroll, {passive:true});

  render();
})();
