/* ComfyMediaViewer · js/justified.js（2026-11-04）
   「原比例铺满」布局（justified layout）—— 学界/画廊类看图器常用的排法。

   与方格布局的差别只有一条：**每张卡片的宽高比 = 图片本身的宽高比**，
   于是图片铺满格子、行内没有任何横向空白，每行右边缘对齐。
   实现上不是纯 CSS 能做到的：一行放几张、行高多少，取决于这一行所有图片的比例之和，
   必须按容器宽度现算。

   ── 算法（一次贪心扫过全量 S.view）──────────────────────────────
   目标行高 targetH = 密度档位 × K（K=1.35，见下），逐张累加比例 r：
        sum += r;  cnt++
        当 sum*targetH + gap*(cnt-1) >= W 时收行
   收行时把行高反解出来：h = (W - gap*(cnt-1)) / sum
   —— 这个 h 让该行**精确铺满** W（不是近似），末张再吃掉亚像素余量防右缘 1px 缝。

   K 为什么是 1.35 而不是 1.5：实测（dev/justify_bench.py，真实库 11,641 张）
       k=1.35 → 单卡面积 ≈ 现状 1.06×，密度滑块每一档的「密度感」不变，只是留白变成图；
       k=1.50 → 单卡面积 1.31×，260 档会把行高推到 369px、一屏只剩 2 行 11 张。
   也就是说 K 是「卡片多大」和「一屏看几张」的旋钮，1.35 是唯一不改变现有手感的值。

   ── 为什么整表预算，而不是只算已渲染的 ─────────────────────────
   瀑布流是分批续载的（每批 100 张）。若只对已渲染部分切行，新的一批进来会把上一批的
   末行重新切开 ⇒ 行高变、总高变、scrollTop 被顶着走（这正是方格布局「滚动条长度
   随续载变长」的老毛病）。整表一次算完（11,641 张约 3ms）⇒ 行边界与总高从第一帧起
   就是最终值，续载只影响「插入多少张卡片」，不影响几何。

   ── 与既有机制的四个接缝 ────────────────────────────────────────
   1. #grid 保持 static，卡片绝对定位，offsetParent 仍是 #gridwrap（见 justified.css 注释）。
   2. #grid 高度写成内容总高（内联），否则塌成 0 ⇒ 续载哨兵 #sent 跑到顶部 ⇒ 一次渲染上万张。
   3. content-visibility 的 --cv-h 体系在本模式下无意义（每张卡都有显式宽高），
      mvCvMeasure/mvCvRelayout 里已加短路，重排改由本文件的 jfRelayout 负责。
   4. 「返回上级」卡不在 S.view 里，但它占网格首格 ⇒ 布局为它预留 slot 0（off=1），
      卡片通过 dataset.jfb 认领，重排时用同一个 off 才能对得上。
*/
"use strict";
(function(){
  const K       = 1.35;      // 目标行高 = 密度档位 × K
  const MIN_K   = 0.62;      // 行高下限（相对 targetH）：再矮就退一张，避免一条细带
  const MAX_K   = 1.55;      // 行高上限：再高就补一张，避免一行只有两三张
  const PREF    = "mv.arrange";

  let ON      = true;        // 是否用「原比例」（默认开；初始化时按存储覆盖）
  let mode    = "grid";      // 视图模式（由 settings.js 同步过来）
  let dirty   = true;        // 布局是否需要重算
  let viewRef = null;        // 上次布局对应的 S.view 引用
  let lastW   = -1;
  let off     = 0;           // 首格是否被「返回上级」卡占用（0/1）
  let X = null, Y = null, WD = null, HT = null;
  let totalH = 0, originX = 0, originY = 0;
  let relayoutT = 0;

  const $g = () => document.getElementById("grid");
  const active = () => ON && mode === "grid";

  function cssPx(name, dflt){
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const n = parseFloat(v);
    return isFinite(n) && n > 0 ? n : dflt;
  }

  /* 单张卡片的比例。优先级：服务端列表自带的 w/h → 元数据映射 → 1:1。
     注意：**不**做「图片加载完后按 naturalWidth 修正」—— 那会让所在行重排、
     整块内容上下跳，代价远大于「某一张按方格子显示」。
     视频 / gif / bmp 不在提示词索引里，落进 1:1 分支，行为是稳定可预期的。 */
  function ratio(it){
    if(!it) return 1;
    if(it.type !== "image" && it.type !== "video") return 1;   // 目录卡 / 聚合卡
    if(it.w > 0 && it.h > 0) return it.w / it.h;
    /* ⚠️ 不要写 window.S —— state.js 里是 `let S`，属于全局**词法绑定**，
       不会挂到 window 上（实测：window.S === undefined，布局因此拿到空表、整页无坐标）。
       直接引用 S 即可，state.js 先于本文件加载。 */
    const m = S.metaMap ? S.metaMap[it.path] : null;
    if(m && m.w > 0 && m.h > 0) return m.w / m.h;
    return 1;
  }

  function build(W, offN){
    const items = S.view || [];
    const n = items.length + offN;
    X = new Float32Array(n); Y = new Float32Array(n);
    WD = new Float32Array(n); HT = new Float32Array(n);
    if(!n){ totalH = 0; return; }

    const gap     = cssPx("--grid-gap", cssPx("--gap-card", 12));
    const targetH = cssPx("--card-min", 200) * K;
    const R = p => (p < offN ? 1 : ratio(items[p - offN]));

    let y = 0, start = 0;
    while(start < n){
      let cnt = 0, sum = 0, idx = start, full = false;
      /* 累加到「这一行装不下」为止。只用乘法与加法，不读 DOM —— 一万张也就几毫秒。 */
      while(idx < n){
        sum += R(idx); cnt++; idx++;
        if(sum * targetH + gap * (cnt - 1) >= W){ full = true; break; }
      }
      /* 行太矮（宽图扎堆，一行塞了太多）→ 退最后一张重解，直到进区间。
         full 为 false 说明是末行（图不够铺满），保持 targetH，左对齐收尾。 */
      while(full && cnt > 1 && (W - gap * (cnt - 1)) / sum < targetH * MIN_K){
        cnt--; idx--; sum -= R(start + cnt);
      }
      /* 行太高（一行太短、被拉伸）→ 再补一张 */
      while(full && idx < n && (W - gap * (cnt - 1)) / sum > targetH * MAX_K){
        sum += R(idx); cnt++; idx++;
      }

      const h = full ? (W - gap * (cnt - 1)) / sum : targetH;
      let x = 0;
      for(let k = 0; k < cnt; k++){
        const p = start + k;
        /* 末张吃掉取整余量：不然 1/3 + 1/3 + 1/3 会累积出右缘 1px 缝。 */
        const w = (k === cnt - 1 && full)
          ? Math.max(1, W - x)
          : Math.min(R(p) * h, Math.max(1, W - x));
        X[p] = x; Y[p] = y; WD[p] = w; HT[p] = h;
        x += w + gap;
      }
      y += h + gap;
      start += cnt;
    }
    totalH = Math.max(0, y - gap);
  }

  /* 唯一的「把总高写进 #grid」入口。
     为什么必须唯一：卡片是绝对定位、不参与文档流 ⇒ 这里不写高度，#grid 的高度就是 0，
     兄弟节点 #sent（续载哨兵）落在文档顶部，renderMore 末尾「还在预载范围就继续渲染」
     永远成立 —— 一次把全部条目渲染出来（真实库 11,641 张直接卡死）。
     三个调用点（首次布局 / 换宽度换密度 / 切换排列）漏掉任何一处都会重现该故障，
     所以合并成一个函数，别让它们各自写。 */
  function applyHeight(g){ g.style.height = totalH + "px"; }

  function apply(el, p){
    if(!X || p < 0 || p >= X.length) return;
    el.style.position = "absolute";
    el.style.left     = (originX + X[p]) + "px";
    el.style.top      = (originY + Y[p]) + "px";
    el.style.width    = WD[p] + "px";
    el.style.height   = HT[p] + "px";
    el.dataset.jfi    = String(p);
  }

  /* ---------- 冷启动补尺寸 ----------
     索引是后台线程建的（/api/list 只负责踢一脚）。首屏那一刻若索引还没就绪，
     服务端列表里就没有 w/h ⇒ 全部按 1:1 排，看起来像「功能没生效」。
     这里静默补数：POST /api/dims 只回宽高，拿到后**不重建 DOM**，只重算并改写坐标。

     2026-11-04 修正：原来是「最多 3 次、递增等待」⇒ 重试额度会在索引就绪前用光
     （真实库 1 万多张首建可能十几秒），结果一直排成方图。改成**连续未命中计数**：
     补上就一直补，连续 12 轮一条都补不到才停（那些是真拿不到，如损坏文件）。 */
  let coverMiss = 0, coverBusy = false;

  /* 缺尺寸的条目 —— **只看已经渲染到 DOM 的卡片**。
     ⚠️ 这里踩过一个坑（2026-11-04 用户反馈「刚打开卡几秒、文件夹也不显示」）：
     早先遍历的是 S.view 全量（一万多条），冷启动时全都缺尺寸 ⇒ 一次性把一万多个路径
     发给服务端，服务端逐个读文件头，磁盘被这一个请求吃掉几秒到几十秒，
     首屏的目录列表（含侧栏文件夹）全被拖住。
     现在按 DOM 取样：首屏就补一两百张，滚动时新渲染的再进来，效果一样但成本可控。
     ⚠️ 早退条件**不能**用「覆盖率 ≥90%」：真实库 11,641 张里，刚出的那几张
     只占 0.03%，覆盖率永远 ≥90% ⇒ 一次都不会来补，新图永远排成方格。
     视频不算缺项：mp4 的 moov 可能在文件尾部，读头也拿不到，布局对它们本来
     就是 1:1；否则每轮都会带上它们，永远补不完。 */
  function missingDims(){
    const g = $g();
    if(!g) return [];
    const out = [], seen = new Set();
    const take = (it)=>{
      if(!it || it.type !== "image" || (it.w > 0 && it.h > 0)) return;
      if(!it.path || seen.has(it.path)) return;
      seen.add(it.path); out.push(it);
    };
    let seenCard = 0;
    for(const el of g.children){
      if(el.dataset && el.dataset.jfb) continue;      // 「返回上级」卡不是库内条目
      const k = el.dataset && el.dataset.jfi;
      if(k === undefined) continue;
      seenCard++;
      take((S.view || [])[+k - off]);                // jfi 是**布局槽位**（= 下标 + off），
      // 有「返回上级」卡时 off=1，槽位比下标多 1 —— 不减回来就会整段错位，
      // 补的是一批「已经渲染过 / 还没渲染」的别的图（2026-11-04 复核时发现）。
    }
    if(!seenCard){
      /* 首帧：begin() 是在卡片插入**之前**调用的，此刻 #grid 还是空的。
         不回退就会判成「没有缺项」直接收工，补数一次都不发（门禁当场翻红）。 */
      const v = S.view || [];
      const lim = Math.min(v.length, Math.max(1, S.shown || 120));
      for(let i = 0; i < lim; i++) take(v[i]);
    }
    return out;
  }

  function coverDims(){
    if(coverBusy || !active()) return;
    const miss = missingDims();
    if(!miss.length) return;
    const paths = miss.map(it => it.path);
    if(typeof api !== "function") return;
    coverBusy = true;
    api("/api/dims", {method:"POST", headers:{"Content-Type":"application/json"},
                      body: JSON.stringify({paths})}).then(j=>{
      coverBusy = false;
      if(!j || typeof j !== "object" || j.error){ maybeCover(); return; }
      let hit = 0;
      for(const it of miss){
        const d = j[it.path];
        if(d && d[0] > 0 && d[1] > 0 && !(it.w > 0)){ it.w = d[0]; it.h = d[1]; hit++; }
      }
      if(!hit){ maybeCover(); return; }      // 这批还是取不到，下一轮再来
      coverMiss = 0;                         // 有进账就重置：滚动到新的一段还能继续补
      invalidate();
      relayout(0);                 // 只重排已在 DOM 的卡片，不回顶
    }).catch(()=>{ coverBusy = false; maybeCover(); });
  }

  function maybeCover(){
    if(!missingDims().length) return;
    if(coverMiss >= 12) return;              // 连续 12 轮零命中：再试也一样，停手
    coverMiss++;
    setTimeout(coverDims, Math.min(6000, 700 + 900 * coverMiss));
  }

  function reapply(){
    const g = $g(); if(!g) return;
    for(const el of g.children){
      const p = el.dataset && el.dataset.jfi;
      if(p === undefined) continue;
      apply(el, +p);
    }
  }

  function clearBox(){
    const g = $g(); if(!g) return;
    for(const el of g.children){
      el.style.position = ""; el.style.left = ""; el.style.top = "";
      el.style.width = "";    el.style.height = "";
      delete el.dataset.jfi;
    }
    g.style.height = "";
  }

  function syncBody(){
    document.body.classList.toggle("jf", active());
  }

  /* renderMore() 在插入卡片**之前**调它：算好整张表、写好 #grid 总高。
     hasBack 必须由调用方给（渲染函数里才知道这一帧会不会插入「返回上级」卡）。 */
  function begin(hasBack){
    if(!active()) return;
    const g = $g(); if(!g) return;
    const W = g.clientWidth;
    if(W < 40) return;                       // 侧栏动画/隐藏等瞬时状态：这一帧不算
    if(!dirty && viewRef === S.view && Math.abs(W - lastW) < 0.5) return;

    const isNew = (viewRef !== S.view);
    if(dirty) off = hasBack ? 1 : 0;
    originX = g.offsetLeft; originY = g.offsetTop;
    try{
      build(W, off);
    }catch(err){
      /* 兜底：宁可退回方格，也不能让 #grid 高度为空（那会触发一次全量渲染）。 */
      console.error("[justified] 布局失败，已退回方格布局", err);
      ON = false; dirty = true; viewRef = null; syncBody();
      /* 已经定过位的卡片要清掉内联坐标，否则退回方格后它们仍然是绝对定位。 */
      try{ clearBox(); }catch(_){}
      return;
    }
    applyHeight(g);
    lastW = W; viewRef = S.view; dirty = false;
    if(isNew){ coverMiss = 0; }
    maybeCover();
    /* 卡片是在本函数返回之后才插入的 ⇒ 推到下一个任务再判断「视野够不够」。 */
    setTimeout(tick, 0);
  }

  /* card() 建好一张就调它。i = S.view 下标；i<0 表示「返回上级」卡（占 slot 0）。 */
  function place(el, i){
    if(!active() || !X) return;
    const p = (i < 0) ? 0 : (i + off);
    if(p >= X.length) return;
    apply(el, p);
  }

  function invalidate(){
    dirty = true;
  }

  /* 换宽度 / 换密度：重算 + 只重排**已在 DOM 里的**卡片（不重建 DOM，滚动位置不丢）。
     去抖交给调用方（resize 260ms / 密度 180ms，与 mvCvRelayout 原节奏一致）。 */
  function relayout(delay){
    clearTimeout(relayoutT);
    relayoutT = setTimeout(()=>{
      if(!active()) return;
      const g = $g(); if(!g || !g.children.length) return;
      const W = g.clientWidth; if(W < 40) return;
      originX = g.offsetLeft; originY = g.offsetTop;
      build(W, off);
      applyHeight(g);
      lastW = W; viewRef = S.view; dirty = false;
      reapply();
      /* 2026-11-05 P2：窗口变高（720p → 4K / 折叠侧栏）后 `gw.clientHeight` 变大，
         而 frontierBottom() 没变 ⇒ 底部留一片空白，必须滚一下才补。tick 只看
         「已渲染前沿 vs 视口」的关系，这里补一拍即可（它自己会按需续帧）。 */
      tick();
    }, delay || 120);
  }

  /* 视图模式切换（settings.js 的 sync 里调）。离开网格必须把内联坐标清干净，
     否则列表视图会继承绝对定位与像素宽高，整屏错位。 */
  function viewMode(m){
    const was = active();
    mode = (m === "list" || m === "time") ? m : "grid";
    syncBody();
    if(was && !active()) clearBox();
    dirty = true;
  }

  function paintSeg(){
    const seg = document.getElementById("arrangeSeg");
    if(!seg) return;
    for(const b of seg.querySelectorAll(".segbtn")){
      const on = b.dataset.a === (ON ? "justify" : "grid");
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }

  /* 开关本身。打开时**不重建网格**：直接按当前 S.view 算一遍、重排在 DOM 的卡片，
     滚动位置与已加载进度都保留。 */
  function setArrange(v, persist){
    ON = (v === "justify");
    if(persist !== false){
      try{ localStorage.setItem(PREF, ON ? "justify" : "grid"); }catch(_){}
    }
    paintSeg();
    syncBody();
    if(!active()){
      clearBox();
      /* 切回方格：**必须重测 --cv-h**。
         默认开启原比例后，新用户第一次切回方格时 `_cvH` 还是 0（原比例期间
         mvCvMeasure 一直早退），于是 applyFilter 那次渲染会顺手量一次 ——
         而那一刻列宽/滚动条还在过渡态，实测量到 203、最终是 201（差 2px/行，
         2000 张累计 ~0.9% 总高 ⇒ 滚到底会看到位置回缩）。
         mvCvRelayout 会把卡片全量同步排一遍再 force 重测，量的是稳定后的盒子。 */
      if(typeof mvCvRelayout === "function") mvCvRelayout(0);
      return;
    }

    const g = $g(); if(!g) return;
    off = g.querySelector(":scope > [data-jfb]") ? 1 : 0;
    dirty = true;
    const W = g.clientWidth;
    if(W < 40 || !S.view || !S.view.length) return;
    originX = g.offsetLeft; originY = g.offsetTop;
    build(W, off);
    applyHeight(g);
    lastW = W; viewRef = S.view; dirty = false;
    reapply();
    coverMiss = 0; maybeCover();
  }

  /* 空状态：renderMore 清空网格时调 —— 旧的总高必须撤掉，否则一片空白还能滚。 */
  function clearBoxHeight(){
    const g = $g(); if(g) g.style.height = "";
  }

  /* ---------- 续载：盯「已渲染前沿」，不盯哨兵 ----------
     为什么不能靠 #sent：它靠文档流定位，而原比例模式把 #grid 写成**全量**高度
     （11,641 张 ≈ 几十万像素），哨兵于是被顶到最底 —— 用户滚过第一批（约 5,000px）
     之后看到的是大片空白，必须一路拖到估测底端才会触发下一批。现象就是
     「只有一页、往下拉不刷新」（2026-11-04 用户截图）。
     这里按真实前沿算：可见底边接近「最后一张已渲染卡的底边」就补一批；
     拖动滚动条跳到中段时会连续补（每帧上限 STEP 张），直到盖住视野。
     纯滚动监听，不动 batch.js 的哨兵逻辑（方格模式仍走原路）。 */
  const STEP = 240;              // 每次补齐上限：一帧塞太多会卡住主线程
  let kickT = 0;

  function frontierBottom(){
    if(!Y || !HT) return 0;
    const last = off + (S.shown || 0) - 1;
    if(last < 0 || last >= Y.length) return 0;
    return Y[last] + HT[last];
  }

  function tick(){
    if(!active()) return;
    const gw = document.getElementById("gridwrap");
    const total = (S.view || []).length;
    /* shown=0 时交给 renderMore 走首屏逻辑（返回上级卡 / 空状态），别在这里抢跑 */
    if(!gw || !total || !(S.shown > 0) || S.shown >= total) return;
    const visible = gw.scrollTop + gw.clientHeight + 800;   // 提前 800px 备货
    if(visible < frontierBottom()) return;
    const target = Math.min(total - 1, (S.shown || 0) + STEP - 1);
    if(typeof renderUpTo === "function") renderUpTo(target);
    else if(typeof renderMore === "function") renderMore();
    /* 还落后于视野（拖动滚动条远跳）→ 下一帧继续，不阻塞当前这帧。 */
    if(visible > frontierBottom() && (S.shown || 0) < total) setTimeout(tick, 16);
  }

  function onScroll(){
    if(!active()) return;
    const now = Date.now();
    if(now - kickT < 120) return;      // 滚动事件很密，节流即可；tick 自己会继续
    kickT = now;
    tick();
  }

  /* ---------------- 初始化 ----------------
     2026-11-04 用户要求：**默认开启原比例**（此前默认方格）。
     存储值仍以用户显式选择为准 —— 只有写过 "grid" 的才回方格；
     没写过（新装 / 老用户首次升级）一律走原比例。
     ⚠️ 别写成 `getItem(PREF) === "justify"`：那样默认就是关，等于回退这次决定。 */
  try{ ON = localStorage.getItem(PREF) !== "grid"; }catch(_){ ON = true; }
  try{
    const mv = localStorage.getItem("mv.view") || "grid";
    mode = (mv === "list" || mv === "time") ? mv : "grid";
  }catch(_){ mode = "grid"; }

  const seg = document.getElementById("arrangeSeg");
  if(seg){
    seg.addEventListener("click", e=>{
      const b = e.target.closest ? e.target.closest(".segbtn") : null;
      if(!b || !b.dataset.a) return;
      setArrange(b.dataset.a);
      /* 开关只改排列，不改筛选条件；但页数/行数口径变了，回到第一页最稳。 */
      if(typeof applyFilter === "function") applyFilter();
    });
  }
  paintSeg();
  syncBody();

  /* 续载监听挂在滚动容器上（#gridwrap 是实际的滚动者，不是 window）。 */
  const _gw = document.getElementById("gridwrap");
  if(_gw) _gw.addEventListener("scroll", onScroll, {passive:true});

  window.jfActive      = active;
  window.jfBegin       = begin;
  window.jfPlaceCard   = place;
  window.jfInvalidate  = invalidate;
  window.jfRelayout    = relayout;
  window.jfViewMode    = viewMode;
  window.jfSetArrange  = setArrange;
  window.jfClearBox    = clearBoxHeight;
})();
