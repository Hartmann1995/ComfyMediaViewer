/* ============================================================================
   像素快查 · 大图模式（2026-11-09 第二版）
   ----------------------------------------------------------------------------
   大图里图是「适应窗口」缩放的（1280×1920 通常只占屏幕高度），看的是插值后的
   糊图；想判锐度 / 噪点 / 压缩痕迹就得双击进 100% 再拖，一进一出位置全丢。

   本模块：**在大图上按住 Ctrl，指针旁开一个方形镜片**，不用进 100% 就能判像素；
   中键单击把镜片钉住（钉住后可松手慢慢看），Esc / 再按中键关闭。

   发现性（2026-11-09 补）：一个只有手势的功能等于没有功能 —— 谁也不会凭空想到
   按着 Ctrl 看图。所以左下角有一颗常驻「像素快查」按钮：点一下进入**跟随模式**
   （不按 Ctrl 也跟随，指针移到哪看到哪），再点 / Esc 退出；另外首次进大图弹一条
   一次性提示（localStorage 记过就不再出现）。Ctrl 悬停仍是原来的临时口径。

   三条尺寸口径（都相对「屏幕上渲染出来的那张图」，不相对原图）：

   · 镜片边长 = 渲染短边 × 0.8，夹在 [300px, 0.9 × 视口短边]。
     图大镜片就大，图小镜片就小 —— 固定 680px 在小图上会比图本身还大、在大图上又太小。
   · 放大倍数 = 屏幕上「1 图像像素占几个物理像素」× 2，向上取整，且不低于 1。
     为什么不是死守 1:1：小图（640×480）在大图里本来就是 1:1 显示的，再给 1:1 等于
     什么都没放大，镜片里的东西比全屏那张还小。取整是为了保住像素边界 ——
     1.34 倍会用双线性插值把每个像素糊成一片，那正是这个功能要验的东西。
   · 镜片是**正方形**：横竖取同一个边长，看局部时视线不用在长宽之间换算。

   四条设计约束（写错任何一条这个功能就废了）：

   ① 画布按**物理像素**对齐。Windows 150%/200% 缩放下若按 CSS 像素画，1 个图像像素
      会被拉成 1.5/2 个物理像素，看到的是浏览器插值出来的糊图 —— 而那正是用户要用
      这个功能来判断的东西。所以 canvas 的 backing store = CSS 尺寸 × dpr，
      放大倍数 k 也按**物理**像素算。

   ② 坐标必须用 `<img>` 自己的 rect。大图里的缩放是 CSS transform（translate +
      scale），`getBoundingClientRect()` 拿到的就是**变换后**的框，正好是从屏幕
      坐标反推图像像素所需的那个矩形；用 #stage 的框会整体错位。

   ③ 镜片必须 pointer-events: none。否则它一盖住画面，指针的 target 就变成镜片
      → 不再命中 <img> → 关窗 → 指针又落回画面 → 开窗，抖成开关闪烁。

   ④ 只用舞台里**已经解码好的那张图**画。
      不从 /api/file 重新拉原图：① 大图本来就是原图，重复下载浪费；② 混淆预览
      是 blob，重新拉只会拿到未混淆的原图 —— 看到的和屏幕上的不是一张，等于说谎。

   安全 / 边界（沿用既有规矩，一条都不能少）：
   · 舞台处于 `nsfw-blur`（模糊态）时**绝不触发** —— 否则 canvas 直接画出未模糊
     的原图，等于给了一条绕过模糊的旁路；
   · 原图还没就位（`dataset.full!=="1"`，屏幕上仍是缩略图）时只显示「解码中…」，
     绝不把缩略图放大冒充 1:1；
   · 视频 / 无图舞台不触发；指针移出画面、滚轮缩放、拖拽、翻页、关大图一律关窗。
   ========================================================================== */
(function(){
  "use strict";
  if(window.__mvPeekInit) return;          // 幂等：门禁重复注入 / 热重载不重复挂
  window.__mvPeekInit = true;

  /* ---------------- 纯函数（门禁直接喂数字断言，不依赖浏览器） ---------------- */
  /* 屏幕坐标 → 图像像素坐标。rect 取 <img> 的 getBoundingClientRect()。 */
  function peekPoint(rect, nw, nh, cx, cy){
    if(!rect || !(rect.width > 0) || !(rect.height > 0) || !(nw > 0) || !(nh > 0))
      return {x: 0, y: 0};
    return {x: (cx - rect.left) / rect.width * nw,
            y: (cy - rect.top)  / rect.height * nh};
  }
  /* 屏幕上「一个图像像素」占几个物理像素。
     < 1 ⇒ 屏幕上是缩小显示；≥ 1 ⇒ 已经是原像素或更大。 */
  function physScale(rect, nw, dpr){
    if(!rect || !(rect.width > 0) || !(nw > 0)) return 0;
    return rect.width / nw * (dpr || 1);
  }
  /* 镜片里的放大倍数 k：1 图像像素画成 k 个**物理**像素。
     目标 = 屏幕渲染倍数 × RENDER_FACTOR，向上取整，最低 1（绝不缩小）。
       · 2000×1500 缩到 996 宽（0.50 物理/像素）→ 0.50×2 = 1.00 → k=1（= 1:1）
       · 640×480 在舞台里 1:1（1.00）        → 2.00       → k=2（比全屏那张还大）
       · 4K 图缩到 25%（0.25）               → 0.50       → k=1（相对屏幕仍是 4 倍） */
  function lensScale(rect, nw, dpr){
    const base = physScale(rect, nw, dpr);
    if(!(base > 0)) return 1;
    return Math.max(1, Math.ceil(base * RENDER_FACTOR - 1e-6));
  }
  /* 镜片边长（正方形）：渲染短边 × LENS_RATIO，夹 [LENS_MIN, 0.9 × 视口短边]。 */
  function lensSide(rect, vw, vh){
    const short = Math.min((rect && rect.width) || 0, (rect && rect.height) || 0);
    const vmin = Math.min(vw || 0, vh || 0);
    const v = Math.min(Math.max(short * LENS_RATIO, LENS_MIN), vmin * LENS_VMIN_RATIO);
    return Math.max(1, Math.round(v));
  }
  window.peek100 = {peekPoint: peekPoint, physScale: physScale,
                    lensScale: lensScale, lensSide: lensSide,
                    /* 调试 / 门禁用：按钮入口与跟随模式的状态 */
                    toggleArm: toggleArm, setArmed: setArmed,
                    isArmed: () => armed};

  /* ---------------- 常量 ---------------- */
  const EDGE = 10;              // 镜片与视口边缘的安全距离
  const RENDER_FACTOR = 2;      // 镜片相对屏幕渲染至少放大 2 倍
  const LENS_RATIO = 0.8;       // 镜片边长 = 渲染短边 × 0.8
  const LENS_MIN = 300;         // 再小的图也别给个看不清的小方块
  const LENS_VMIN_RATIO = 0.9;  // 但不超过视口短边的 90%
  const TIP_KEY = "cmv.peek.hint.v1";   // 一次性提示：看过就不再打扰

  /* ---------------- 状态 ---------------- */
  let el=null, cv=null, ctx=null;
  let hudDim=null, hudMeta=null, msgEl=null;
  let on=false, pinned=false, ctrl=false;
  /* armed = 按钮开启的「跟随模式」：不需要按住 Ctrl，指针在画面上就跟着看。
     与 pinned 是两回事：pinned 冻结在当前这一块，armed 是常驻但跟随。 */
  let armed=false;
  let tipEl=null, tipTimer=0, tipTries=0;
  let srcImg=null, waitEl=null;     // srcImg = 正在快查的那张舞台图；waitEl = 已挂过 load 监听的那张
  let ptrX=0, ptrY=0;
  let raf=0;
  let boxW=0, boxH=0, boxX=-1, boxY=-1, curSide=0;
  let hudD="", hudM="";

  /* ---------------- DOM ---------------- */
  function build(){
    el = document.createElement("div");
    el.id = "peek";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML =
      '<canvas id="peekCv"></canvas>'
      + '<div class="peek-msg" id="peekMsg"></div>'
      + '<div class="peek-hud"><span class="ph-d"></span><span class="ph-m"></span></div>'
      + '<div class="peek-pin">已钉住 · 中键或 Esc 关闭</div>';
    document.body.appendChild(el);
    cv  = el.querySelector("#peekCv");
    ctx = cv.getContext("2d", {alpha: true});
    hudDim  = el.querySelector(".ph-d");
    hudMeta = el.querySelector(".ph-m");
    msgEl   = el.querySelector("#peekMsg");
  }

  /* ---------------- 舞台状态 ---------------- */
  function lbOn(){
    const lb = document.getElementById("lb");
    return !!lb && lb.classList.contains("on");
  }
  function stageEl(){ return document.getElementById("stage"); }
  function stageBlurred(){
    const st = stageEl();
    return !st || st.classList.contains("nsfw-blur");
  }
  /* 指针落点是不是「舞台里的那张图」——锁死在 <img> 上：
     顶栏、胶片条、两侧箭头、详情面板都不会命中，天然不触发。 */
  function shotOf(t){
    if(!t || t.tagName !== "IMG") return null;
    const st = stageEl();
    if(!st || !st.contains(t)) return null;
    if(t.classList.contains("lb-ph")) return null;   // 缩略图占位层不是原图
    return t;
  }
  /* 舞台上当前那张图（不看指针）。按钮入口用它判断「这张能不能快查」：
     视频没有 <img>，缩略图占位层也不算。 */
  function stageImg(){
    const st = stageEl();
    if(!st) return null;
    const im = st.querySelector("img");
    if(!im || im.classList.contains("lb-ph")) return null;
    return im;
  }
  /* 当前能不能快查（不含「原图解码好没有」这一条，那条在 open/refresh 里单独说） */
  function allowed(){
    return lbOn() && !stageBlurred();
  }

  /* ---------------- 开 / 关 ---------------- */
  function open(im){
    if(on || !im || !allowed()) return;
    on = true;
    pinned = false;
    srcImg = im;
    el.classList.add("on");
    el.classList.remove("pinned");
    applyLayout();           // 正方形边长按这张图的渲染尺寸现算
    place();
    refresh();
  }

  function close(){
    if(!on) return;
    on = false;
    pinned = false;
    if(raf){ cancelAnimationFrame(raf); raf = 0; }
    el.classList.remove("on", "pinned");
    el.style.left = ""; el.style.top = "";
    boxX = -1; boxY = -1;                 // 下次开窗必须重新写坐标（否则同位置会被去重跳过）
    if(ctx && cv) ctx.clearRect(0, 0, cv.width, cv.height);
    showMsg("");
    srcImg = null; waitEl = null;
    hudD = ""; hudM = "";
  }

  /* ---------------- 入口按钮 / 首次提示 ---------------- */
  function setArmed(v){
    v = !!v;
    if(v === armed) return;
    armed = v;
    const b = document.getElementById("peekTgl");
    if(b){
      b.classList.toggle("on", armed);
      b.setAttribute("aria-pressed", armed ? "true" : "false");
      b.dataset.tip = armed
        ? "像素快查已开启 · 指针移到画面上看细节（再点或 Esc 关闭）"
        : "像素快查：点一下开启，指针移到哪就看到哪（也可按住 Ctrl 临时看）";
    }
    if(armed) hideHint();          // 用户已经找到入口了，别再举着那张纸条
  }

  /* 按钮：开 / 关跟随模式。没有指针位置（刚进大图、从工具栏点的）时
     以画面中心为起点，至少让人看见镜片真的出来了。 */
  function toggleArm(){
    if(armed){ setArmed(false); close(); return; }
    if(!lbOn()) return;
    const im = stageImg();
    if(!im){ toast("这张是视频，像素快查只对图片有效", "warn"); return; }
    if(stageBlurred()){
      toast("这张处于模糊状态，先点左上角眼睛看原图", "warn");
      return;
    }
    setArmed(true);
    const r = im.getBoundingClientRect();
    const inside = ptrX >= r.left && ptrX <= r.right && ptrY >= r.top && ptrY <= r.bottom;
    if(!inside){ ptrX = r.left + r.width / 2; ptrY = r.top + r.height / 2; }
    open(im);
    if(on) toast("像素快查已开启 · 移动指针看细节，Esc 关闭", "ok");
  }

  /* 视频 / 无图时按钮不露面（点了也没用，留着只是噪音）。
     display 由 CSS 显式写死，所以 hidden 必须自己配一条规则（见 peek.css）。 */
  function syncBtn(){
    const b = document.getElementById("peekTgl");
    if(!b) return;
    const want = !(lbOn() && !!stageImg());
    if(b.hidden !== want) b.hidden = want;   // 只在变化时写，避免每次翻页都触发一次重排
  }

  function hideHint(){
    if(tipTimer){ clearTimeout(tipTimer); tipTimer = 0; }
    if(tipEl) tipEl.classList.remove("on");
  }
  /* 首次进大图的一次性提示。三个前提缺一不可：没看过、有图、没被模糊 ——
     在视频或模糊卡上弹「按 Ctrl 看细节」是错的，那种情况下也根本触发不了。 */
  function maybeHint(){
    try{ if(localStorage.getItem(TIP_KEY)) return; }catch(e){ return; }
    if(!lbOn() || stageBlurred() || !tipEl) return;
    const im = stageImg();
    if(!im) return;
    /* 图还没解码完也别说：镜片此刻只会显示「解码中…」，说了等于骗人。
       最多等 5 次，别为一张坏图排一条永久定时器。 */
    if(im.dataset.full !== "1"){
      if(tipTries++ < 5) tipTimer = setTimeout(maybeHint, 1200);
      return;
    }
    tipTries = 0;
    tipEl.classList.add("on");
    try{ localStorage.setItem(TIP_KEY, "1"); }catch(e){}
    /* 4s 足够读完 8 个字；再久就是挡在图上碍事（按钮常驻，错过也不怕） */
    tipTimer = setTimeout(hideHint, 4000);
  }
  function scheduleHint(){
    if(tipTimer){ clearTimeout(tipTimer); tipTimer = 0; }
    tipTimer = setTimeout(maybeHint, 700);
  }

  /* 量一次尺寸（会触发 layout）。只在边长变化时调，别每帧调。 */
  function measure(){
    boxW = el.offsetWidth;
    boxH = el.offsetHeight;
  }

  /* 按当前**渲染尺寸**摆好正方形镜片，返回 <img> 的 rect（省一次重复读取）。
     图大镜片就大：渲染短边 × 0.8，夹在 [300, 90% 视口短边]。 */
  function applyLayout(){
    if(!srcImg) return null;
    const r = srcImg.getBoundingClientRect();
    const side = lensSide(r, window.innerWidth, window.innerHeight);
    if(side !== curSide){
      curSide = side;
      el.style.width  = side + "px";
      el.style.height = side + "px";
      measure();
    }
    return r;
  }

  function place(){
    /* 位置每帧都算（纯算术，不碰 layout）。镜片以**指针为中心**：
       右下角放法会让指针落在窗口外，用户看到的中心点与指针所指不是同一个像素
       （第一版在网格里实测偏移 18px，就是这么来的）。 */
    const vw = window.innerWidth, vh = window.innerHeight;
    let x = ptrX - boxW / 2;
    let y = ptrY - boxH / 2;
    x = Math.max(EDGE, Math.min(x, vw - boxW - EDGE));
    y = Math.max(EDGE, Math.min(y, vh - boxH - EDGE));
    if(x === boxX && y === boxY) return;             // 没变就别写 style（省一次样式失效）
    boxX = x; boxY = y;
    el.style.left = x + "px";
    el.style.top  = y + "px";
  }

  /* 原图就位 / 变更后重算。只处理「屏幕上这张图本身」，不发任何请求。 */
  function refresh(){
    if(!on || !srcImg) return;
    if(!srcImg.isConnected || !allowed()){ close(); return; }
    if(srcImg.dataset.full !== "1" || !srcImg.naturalWidth){
      showMsg("原图还在解码…");
      updateHud(null, null);
      if(waitEl !== srcImg){
        waitEl = srcImg;
        /* 一次性：原图 onload 会把 dataset.full 翻成 "1"，届时换成真画面 */
        srcImg.addEventListener("load", () => {
          if(waitEl === srcImg){ waitEl = null; refresh(); }
        }, {once: true});
      }
      return;
    }
    showMsg("");
    schedule();
  }

  /* ---------------- 绘制 ---------------- */
  function schedule(){
    if(raf) return;
    raf = requestAnimationFrame(render);
  }

  function render(){
    raf = 0;
    if(!on || !srcImg){ return; }
    if(!srcImg.isConnected || !allowed() || srcImg.dataset.full !== "1"){ refresh(); return; }

    const nw = srcImg.naturalWidth, nh = srcImg.naturalHeight;
    if(!(nw > 0) || !(nh > 0)){ refresh(); return; }

    const dpr = window.devicePixelRatio || 1;
    const r = applyLayout() || srcImg.getBoundingClientRect();
    place();                       // 镜片每帧跟着指针（纯算术，无 layout）

    const bw = Math.max(1, Math.round(boxW * dpr));
    const bh = Math.max(1, Math.round(boxH * dpr));
    if(cv.width !== bw || cv.height !== bh){
      cv.width = bw; cv.height = bh;
      cv.style.width  = boxW + "px";
      cv.style.height = boxH + "px";
    }

    const rect = {left: r.left, top: r.top, width: r.width, height: r.height};
    const k = lensScale(rect, nw, dpr);        // 1 图像像素 → k 个物理像素
    const pt = peekPoint(rect, nw, nh, ptrX, ptrY);

    /* 指针在镜片里的偏移（设备像素）：镜片内这个位置显示的，正是指针所指的那个
       图像像素 —— 于是镜片左上角对应图像坐标 (pt - 偏移/k)。 */
    const ox = (ptrX - boxX) * dpr;
    const oy = (ptrY - boxY) * dpr;
    let sx = Math.round(pt.x - ox / k);
    let sy = Math.round(pt.y - oy / k);
    let sw = Math.ceil(bw / k);                // 镜片能装下的图像像素数
    let sh = Math.ceil(bh / k);

    /* k 是整数、sx/sy 也取整 ⇒ 像素边界仍然对齐，放大后每个像素是规整的 k×k 方块。
       越界时裁掉并把目标矩形同量平移，保证剩下的部分仍然逐像素对齐（不能拉伸）。 */
    let dx = 0, dy = 0;
    if(sx < 0){ dx = -sx * k; sw += sx; sx = 0; }
    if(sy < 0){ dy = -sy * k; sh += sy; sy = 0; }
    if(sx + sw > nw) sw = nw - sx;
    if(sy + sh > nh) sh = nh - sy;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, bw, bh);
    if(sw > 0 && sh > 0){
      ctx.imageSmoothingEnabled = false;         // 整数倍放大：不要插值，要方块
      ctx.drawImage(srcImg, sx, sy, sw, sh, dx, dy, sw * k, sh * k);
    }

    /* 调试 / 门禁用：这两个值就是这一帧真正用于绘制的口径 */
    el.dataset.k = String(k);
    el.dataset.side = String(Math.round(boxW));
    updateHud(rect, pt, k);
  }

  /* ---------------- HUD ---------------- */
  function updateHud(rect, pt, k){
    if(!hudDim) return;
    const nw = srcImg ? (srcImg.naturalWidth | 0) : 0;
    const nh = srcImg ? (srcImg.naturalHeight | 0) : 0;
    const pct = (rect && rect.width && nw) ? Math.round(rect.width / nw * 100) : 0;

    const d = nw > 0 ? (nw + "×" + nh + (pct ? " · 屏幕 " + pct + "%" : "")) : "…";
    if(d !== hudD){ hudDim.textContent = d; hudD = d; }

    const bits = [];
    const p = (srcImg && srcImg.dataset.source) || "";
    const ext = (p.split(".").pop() || "").toUpperCase();
    if(ext && ext.length <= 5) bits.push(ext);
    try{
      const i = (typeof curItem === "function") ? curItem() : null;
      if(i && i.size) bits.push(fmtSize(i.size));
    }catch(e){}
    if(pt && nw > 0){
      bits.push("@" + Math.min(nw - 1, Math.max(0, Math.floor(pt.x)))
                    + "," + Math.min(nh - 1, Math.max(0, Math.floor(pt.y))));
    }
    bits.push(k ? (k === 1 ? "1:1 物理像素" : k + "× 物理像素") : "…");
    const m = bits.join(" · ");
    if(m !== hudM){ hudMeta.textContent = m; hudM = m; }
  }
  function showMsg(t, strong){
    if(!msgEl) return;
    msgEl.textContent = t || "";
    msgEl.classList.toggle("on", !!t);
    msgEl.classList.toggle("bad", !!strong);
  }

  /* ---------------- 事件 ---------------- */
  function onMove(e){
    ptrX = e.clientX; ptrY = e.clientY;

    if(pinned) return;               // 钉住：不跟随、也不因指针乱跑而关（Esc / 中键才关）

    if(!ctrl && !armed){
      if(on) close();                // Ctrl 已松 / 跟随模式没开（有人先松键再动鼠标）
      return;
    }
    const im = shotOf(e.target);
    if(!im || !allowed()){ if(on) close(); return; }

    if(!on) open(im);
    else if(srcImg !== im){          // 翻页换了 <img>：重绑到新的这张
      srcImg = im; waitEl = null; refresh();
    }
    schedule();
  }

  function onKeyDown(e){
    if(e.key === "Escape"){
      /* 只关镜片，不关大图：nav.js 的 Esc 处理器挂在 document 冒泡阶段，
         这里在 window 捕获阶段截住，用户按一次 Esc 退回「看大图」而不是「回网格」。
         跟随模式（armed）下 Esc 一并退出 —— 否则镜片关了按钮还亮着，人会以为坏了。 */
      if(on || armed){
        e.preventDefault(); e.stopPropagation();
        setArmed(false);
        close();
      }
      return;
    }
    if(e.key !== "Control" && e.key !== "Meta") return;
    /* 在输入框里按 Ctrl+A / Ctrl+C 不该弹窗：只认「焦点不在可编辑元素」的情况 */
    const a = document.activeElement;
    if(a){
      const tag = a.tagName;
      if(tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || a.isContentEditable) return;
    }
    if(ctrl) return;
    ctrl = true;
    /* 先悬停、后按 Ctrl 的路径：此刻指针已经停在画面上，要立刻开窗 */
    if(on || !allowed()) return;
    let under = null;
    try{ under = document.elementFromPoint(ptrX, ptrY); }catch(err){ under = null; }
    const im = shotOf(under);
    if(im) open(im);
  }

  function onKeyUp(e){
    if(e.key !== "Control" && e.key !== "Meta") return;
    ctrl = false;
    if(on && !pinned && !armed) close();
  }

  function onDown(e){
    if(e.button === 1){                     // 中键：切换「钉住」
      if(!on) return;
      e.preventDefault();
      if(pinned){ close(); }
      else{
        pinned = true;
        el.classList.add("pinned");
        toast("已钉住这一块 · 中键或 Esc 关闭", "ok");
      }
      return;
    }
    if(on) close();                          // 左键 / 右键：要么拖图、要么出菜单，先关
  }

  function invalidate(){
    /* 滚轮缩放 / 窗口尺寸变化后画面几何全变了，镜片留着就是错的：直接关最省事。
       但**跟随模式**下关掉就再也不回来了 —— 滚轮不伴随 pointermove，指针没动过
       就没有下一次开窗。所以延迟一拍按新几何重开。 */
    if(!on) return;
    const reopen = armed && !pinned;
    close();
    if(!reopen) return;
    setTimeout(() => {
      if(!armed || on || !lbOn()) return;
      let under = null;
      try{ under = document.elementFromPoint(ptrX, ptrY); }catch(e){ under = null; }
      const im = shotOf(under);
      if(im) open(im);
    }, 160);
  }

  function watch(){
    if(typeof MutationObserver !== "function") return;
    /* 舞台换图（翻页 / 混淆预览 / 视频）与关大图都不一定伴随指针移动，
       靠 observer 兜住：图断了就关，换了新图就重绑；按钮也跟着显隐。 */
    const st = stageEl();
    if(st){
      new MutationObserver(() => {
        syncBtn();
        if(!on) return;
        if(!srcImg || !srcImg.isConnected){ close(); return; }
        refresh();
      }).observe(st, {childList: true});
    }
    const lb = document.getElementById("lb");
    if(lb){
      let wasOn = lbOn();
      new MutationObserver(() => {
        const nowOn = lbOn();
        if(nowOn === wasOn){ if(nowOn) syncBtn(); return; }
        wasOn = nowOn;
        if(!nowOn){                 // 关大图：镜片、跟随模式、提示全部收掉
          if(on) close();
          setArmed(false);
          hideHint();
          syncBtn();
        }else{
          syncBtn();
          scheduleHint();
        }
      }).observe(lb, {attributes: true, attributeFilter: ["class"]});
    }
  }

  function bind(){
    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("blur", () => { ctrl = false; if(on && !pinned && !armed) close(); });
    window.addEventListener("resize", invalidate);
    window.addEventListener("wheel", invalidate, {capture: true, passive: true});
    window.addEventListener("dragstart", invalidate, true);
  }

  function bindBtn(){
    const b = document.getElementById("peekTgl");
    if(!b) return;
    b.addEventListener("click", e => {
      e.preventDefault();
      e.stopPropagation();
      toggleArm();
    });
  }

  function init(){
    build();
    tipEl = document.getElementById("peekHint");
    bind();
    bindBtn();
    watch();
    syncBtn();
  }

  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
