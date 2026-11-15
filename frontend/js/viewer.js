/* ComfyMediaViewer · js/viewer.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- lightbox ---------------- */

let lbTok=0;                                            // 竞态令牌：快速翻页时过期元数据不回填
/* V52（批次 G3）：大图相邻预取 —— 打开/翻页 600ms 后把左右邻居（仅图片）各预取一张。
   实测（uix_probe_20260927 M3）：缓存命中时翻页 50~78ms，但冷缓存 / 外接盘 / 数 MB PNG
   会放大到数百 ms；预取把这段挪到用户还在看当前张的时候。
   只预取图片：视频体积大，且 releaseStageVideo 已有完整释放纪律，不掺和。
   _pf 持有 Image 引用防 GC 掉请求；closeLb 清空（window.mvClearPrefetch）。 */
let _pf=[]; let _pfT=0;
function prefetchLb(){
  clearTimeout(_pfT);
  _pfT=setTimeout(()=>{
    _pf=[]; const v=curList(), i=curIdx();
    for(const d of [-1,1]){
      const n=i+d;
      if(n<0||n>=v.length) continue;
      if(!v[n] || v[n].type!=="image") continue;
      const im=new Image(); im.src=fileURL(v[n].path, v[n]); _pf.push(im);
    }
  }, 600);
}
window.mvPrefetchLb=prefetchLb;
/* F6-P1-1（2026-10-17）：**中止在途的图片下载** —— 与 ε-2 给视频立的纪律对齐。
   原来 `_pf=[]` 只是丢掉引用：规范里 UA 可以自行决定要不要中断（实测有图被完整下完）。
   翻页一快就变成「用户要看的那张排在几张已翻走的图后面」，外接盘还会被反复唤醒。

   ⚠️ 中止用 `removeAttribute("src")`，**不能**用 `im.src=""`：
   按 HTML 规范空串会解析成文档 URL ⇒ 反而对页面自身发一次请求（Chrome 上真会）。 */
function _abortImg(im){
  try{ if(im){ im.onload=null; im.onerror=null; im.removeAttribute("src"); } }catch(e){}
}
function clearPrefetch(){
  clearTimeout(_pfT);
  for(const im of _pf) _abortImg(im);
  _pf=[];
}
window.mvClearPrefetch=clearPrefetch;

/* 当前舞台上的那张图。切条目 / 关大图时显式中止 —— `stage.innerHTML=""` 只是摘 DOM，
   已起手的资源获取不保证停（与视频同一个理由）。 */
let _stageImg=null;
let _scrambleWorker=null;
const _scramblePreviews=new Map();
const SCRAMBLE_MAX_PIXELS = 32 * 1024 * 1024;

function releaseStageImg(){
  if(_scrambleWorker){
    _scrambleWorker.terminate();
    _scrambleWorker=null;
  }
  _abortImg(_stageImg);
  _stageImg=null;
}
window.mvReleaseStageImg=releaseStageImg;

function clearSoloView(){
  if(!S.solo) return;
  S.solo=null;
  document.body.classList.remove("solo");
}

function scrambleKey(it){
  return S.solo || !it ? it : fileURL(it.path, it);
}

function scramblePreviewFor(it){
  return _scramblePreviews.get(scrambleKey(it));
}

function clearScramblePreviews(keepKey){
  for(const [key, preview] of _scramblePreviews){
    if(key === keepKey) continue;
    URL.revokeObjectURL(preview.url);
    _scramblePreviews.delete(key);
  }
}

function supportsImageScramble(it){
  return it?.type==="image" && !/\.gif$/i.test(it.path);
}

function isFullStageImage(it){
  return _stageImg?.dataset.full==="1" && _stageImg.isConnected
    && _stageImg.dataset.source===fileURL(it.path, it);
}

function syncImageScramble(it){
  const visible = supportsImageScramble(it);
  const ready = visible && isFullStageImage(it);
  for(const btn of [$("#btnScramble"), $("#btnUnscramble")]){
    btn.style.display=visible ? "" : "none";
    btn.disabled=!ready || !!_scrambleWorker;
    btn.setAttribute("aria-busy", String(visible && !!_scrambleWorker));
  }
}

function transformStageImage(reverse){
  const it=curItem(), img=_stageImg;
  if(!supportsImageScramble(it) || _scrambleWorker) return;
  if(!isFullStageImage(it)){
    if(img?.isConnected && img.dataset.full==="1")
      toast("图片已更新，请重新打开后再处理", "warn");
    return;
  }
  const key=scrambleKey(it);
  const current=_scramblePreviews.get(key);
  const width=current?.width || img.naturalWidth, height=current?.height || img.naturalHeight;
  if(!width || !height || width * height > SCRAMBLE_MAX_PIXELS || Math.max(width,height)>32767){
    toast("图片尺寸超出混淆处理上限", "err");
    return;
  }
  const label=reverse ? "解混淆" : "混淆";
  let worker;
  try{
    worker=new Worker("/js/scramble-worker.js");
  }catch(e){
    toast(label+"失败："+e.message, "err");
    return;
  }
  _scrambleWorker=worker;
  syncImageScramble(it);

  const active=()=>_scrambleWorker===worker && _stageImg===img
    && img.isConnected && scrambleKey(curItem())===key;
  const finish=(message, kind)=>{
    if(_scrambleWorker!==worker) return;
    const showMessage=active() && message;
    worker.terminate();
    _scrambleWorker=null;
    syncImageScramble(curItem());
    if(showMessage) toast(message, kind);
  };
  worker.onerror=()=>finish(label+"失败：无法运行图片处理模块", "err");
  worker.onmessage=async ({data})=>{
    if(!active()){ finish(); return; }
    if(data.error){
      finish(label+"失败："+data.error, "err");
      return;
    }
    const url=URL.createObjectURL(data.blob);
    try{
      // 先解码新图，再换预览。失败或翻到别的图时保留原预览。
      const preview=new Image();
      preview.src=url;
      await preview.decode();
      if(!active()){
        URL.revokeObjectURL(url);
        finish();
        return;
      }
      _scramblePreviews.set(key, {url, width, height, reverse});
      img.src=url;
      if(current) URL.revokeObjectURL(current.url);
      refreshLbTips();
      finish(label+"完成", "ok");
    }catch(e){
      URL.revokeObjectURL(url);
      finish(label+"失败："+e.message, "err");
    }
  };
  worker.postMessage({url:current?.url || img.currentSrc || img.src, width, height, reverse});
}

$("#btnScramble").onclick=()=>transformStageImage(false);
$("#btnUnscramble").onclick=()=>transformStageImage(true);
let lbFocusBack=null;   // G2·V16③：打开大图前的焦点，关闭时归还（翻页时不要覆盖它）

/* ε-2（2026-09-20）：**显式释放舞台里的视频**。
   为什么要有它：`stage.innerHTML=""` 只是把节点摘出 DOM。规范里 UA 确实会
   隐式暂停，但**已开始的资源获取不会立刻中止** —— 连按方向键快速翻过几十个视频时，
   后台仍在一路下載已翻走的 src，白白吃带宽与内存。
   （对比模式 exitWipe() 早就这么释放了，这里只是把同一条纪律补到大图。）

   顺序**必须是 pause → removeAttribute("src") → load()**：
   先断 src 会让有的浏览器立刻把 readyState 甩到 HAVE_NOTHING 并吐一个 error。
   ⚠️ `load()` 会让 UA 走一遍资源选择算法；没有 src 时它只是清理，不会发请求。
   挂到 window 上给 nav.js 的 closeLb() 复用 —— 关大图与切条目是同一份释放逻辑，
   不该各写各的（nav.js 在 viewer.js 之后加载，调用时它一定已就位）。 */
function releaseStageVideo(){
  /* 2026-11-15：**#vidBar 也必须在这里收**。
     它不属于 stage 里的节点，此前只有 closeLb()（nav.js:301）摘它 ⇒
     「看一个视频 → 按 → 翻到图片」时，底部仍挂着上一段视频的进度条/时间/倍速/音量，
     而源已被 removeAttribute("src") 断掉，拖进度、点播放全都没反应，必须关掉大图才消失。
     viewer.js:726 的 onerror 分支早就手写过一次 remove，说明这个动作本来就该归这里。
     只在确实挂着时才做额外清理 —— 图片翻图片也走本函数，没必要每次重算提示条避让。 */
  const vb=$("#vidBar");
  if(vb && vb.classList.contains("on")){
    vb.classList.remove("on");
    S.vidInfo=null;
    if(window.mvSyncToastLift) window.mvSyncToastLift();   // 视频条收起 ⇒ 提示条落回
  }
  hideLbPlay();                       // M1：视频一走，中央播放按钮必须跟着收（否则切到图片还挂着）
  const v = stage.querySelector("video");
  if(!v) return;
  try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
}
window.mvReleaseStageVideo = releaseStageVideo;

/* B1（2026-10-08）→ B1b（2026-10-09）：视频「手动开始」—— 中央播放按钮的显隐/点击。
   为什么不做在 #stage 里：见 index.html #lbPlay 旁的说明（点 stage 空白会触发关大图）。
   唯一真相来源：按钮由 v 现推 —— 播放中**收起来**（别挡画面），暂停 / 首帧 / 播完显示 play。
   两轮报障的账都记在这一个按钮上，别再翻回去：
     · M1 把按钮做成「播放中整个收掉」，同时把 #vPlay / 空格也收窄成「只开始」
       ⇒ 三条路径全都没有暂停入口，用户报「暂停按钮失效」。
     · B1 把三条路径改成对称切换（正确，保留），却又把中央按钮改成**常驻**
       （`btn.hidden=false` 写死）⇒ 播放、暂停都挂着个大按钮挡画面，用户报「按钮不消失」。
   ⇒ 正解：切换入口归 #vPlay / 空格 / **点画面**（见 nav.js），中央按钮只在暂停态露面。
   事件用 addEventListener 而不是 `v.onplay=`：后者会**覆盖** extras.js 里 bindVidBar 设的
   同名 onplay/onpause（赋值式只有一个赢家）⇒ #vPlay 的图标从此不再翻转。
   closeLb 时仍由 releaseStageVideo 收掉（视频一走按钮必须跟着走）。 */
function hideLbPlay(){ const b=$("#lbPlay"); if(b) b.hidden=true; }

function lbPlayFor(v){
  const btn=$("#lbPlay");
  if(!btn) return;
  const sync=()=>{ btn.hidden=!v.paused; btn.innerHTML=ico(v.paused?"play":"pause"); };
  v.addEventListener("play",sync); v.addEventListener("pause",sync);
  v.addEventListener("ended",sync); sync();
  btn.onclick=()=>{ if(v.paused) v.play(); else v.pause(); };
}

/* L1（2026-09-27）：大图顶栏放不下时收进横向滚动（viewer.css 基础规则），
   滚动条已隐藏 ⇒ 纵向滚轮转成横向滚动（与 settings.js 里 #lbFilm 的同款交互）。 */
(function(){
  const bar=document.getElementById("lb-top");
  if(!bar) return;
  bar.addEventListener("wheel",e=>{
    if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){ e.preventDefault(); bar.scrollLeft+=e.deltaY; }
  });
})();

/* ---------------- 大图顶栏「说明」的唯一出口 ----------------
   顶栏 9 个控件的悬停说明收在这里，理由有三，缺一都会退回「说了等于没说」：
     1. **落点在变**：「下载」去哪取决于设置里的下载目录（留空 = 系统「下载」文件夹），
        「删除」去哪取决于删除去向（应用内回收站 / Windows 回收站）。
        写死在 HTML 里必然一半的时候是假话 —— 此前删除一直笼统写「移入回收站」，
        而本机实际是删进 Windows 回收站，用户在顶栏「回收站」里永远找不到它。
     2. **状态在变**：收藏态改了，「收藏」按钮说的是「收藏这张图」还是「取消收藏」
        会跟着变。⚠️ 未收藏时「收藏分类」下拉切了不生效（nav.js 的 if(it.fav) 守卫），
        2026-10-30 起按用户要求**只在气泡里写「收藏分类」**、不再附一段解释 ——
        这颗下拉在未收藏态仍是无声失败，属已知取舍，别在没读这行的情况下去「优化」它。
     3. **一个出口**：nav.js / settings.js 只调本函数，别再各自写一份文案。
   调用点：打开/翻页（openLb）、收藏态变化（gallery.js / viewer.js）、
   设置里改「下载保存位置」或「删除去向」（settings.js）。
   ⚠️ #lbCat / #lbMark 是 <select>：自绘气泡对表单控件不走（app.js 的 TIP_FORM），
     用户看到的是**原生 title** ⇒ 这两个必须写 title，只写 data-tip 等于没有提示。 */
function refreshLbTips(){
  const it=(typeof curItem==="function")?curItem():null;
  const dl=String(S.downloadDir||"").trim();
  const sys=delIsSystem();
  const tip=(sel,txt)=>{
    const e=$(sel); if(!e) return;
    if(e.dataset.tip!==txt) e.dataset.tip=txt;
    if(e.tagName==="SELECT" && e.getAttribute("title")!==txt) e.setAttribute("title",txt);
  };
  tip("#lbCat", (it&&it.fav)
    ? "收藏分类：切换即改（可选「＋新建分类」）"
    : "收藏分类");
  tip("#lbMark", "工作标记：待处理 / 待发布 / 待比较 / 待删除（与收藏独立的临时状态）");
  /* 2026-10-30 用户要求：收藏与两条工作流按钮的说明只留主句，删掉补充解释
     —— 气泡是「这是什么」，不是操作手册。 */
  tip("#btnFav", (it&&it.fav) ? "取消收藏" : "收藏这张图");
  tip("#btnOpen", "推送进 ComfyUI 画布（会新开一张，不动你正在编辑的）");
  tip("#btnDlWF", "导出工作流 JSON");
  tip("#btnCopy", "复制正向提示词到剪贴板（不含负向提示词与采样参数）");
  /* 落点如实写：填了目录就是那个目录（后端直写、toast 里也带完整路径）；
     留空就是系统「下载」文件夹（浏览器 / 桌面壳的默认下载行为，实测落盘位置）。 */
  tip("#btnDl", scramblePreviewFor(it) ? "下载当前处理结果（PNG）" : dl
    ? "下载到："+dl+"\n（PNG/JPG/WebP 会自动抹掉内嵌提示词）"
    : "下载到系统「下载」文件夹"
      +"\n（PNG/JPG/WebP 会自动抹掉内嵌提示词 · 改目录见 设置→行为）");
  tip("#btnOpenFolder", "在资源管理器中定位这张图（会选中该文件）");
  tip("#btnDel", sys
    ? "移入 Windows 回收站\n（在资源管理器的回收站里还原）"
    : "移入应用内回收站\n（顶栏「回收站」里可还原）");
}
window.refreshLbTips=refreshLbTips;      // settings.js / gallery.js 跨文件调用

/* ---------------- 顶栏溢出收纳（2026-10-31，用户 1080 窗口「删除被裁不见」） ----------------
   背景：`#lb-top` 是横向滚动容器（viewer.css 的 L1 注释），滚动条被隐藏 ⇒ 放不下时
   右侧按钮被裁在让位边界外，用户既看不到也不知道能滚。实测极限态（NSFW 归类按钮在场）
   `.acts` 自然宽 779px（@fsx=1.00），窗口内容区窄于「779 + 返回/文件名」就必然裁。
   ⚠️ 不是「被压扁」：10 个控件 flex-shrink 都是 1 但宽度一个没缩（压扁 0%）。

   策略（优先级从高到低）：
     常驻 = 收藏分类 / 工作标记 / 收藏 / 推送画布 / 复制 / 删除
     可收 = 存工作流 → 下载 → 定位 → NSFW 归类（依次让位，进「⋯」菜单）
            ⚠️ NSFW 归类是排最后的一位：720 档（1080 物理宽 @150%）三项全收完仍溢 68px，
               不收它就只能让「推送画布」被尾部组压掉半截 —— 那是主行动，不能残。
     删除 = 与「⋯」同处 .lb-tail 钉在裁切线内侧，任何宽度都不会被裁掉（viewer.css 同步规则）
   每次「打开 / 翻页 / 面板开合 / 改字号 / 窗口缩放」都重算一次，不做任何记忆 ——
   窗口一放宽就要立刻全部还回顶栏。 */
const LB_NEST = ["btnDlWF", "btnDl", "btnOpenFolder", "lbNsfwCls", "btnScramble", "btnUnscramble"];
const LB_NEST_META = {
  btnDlWF:       {label:"存工作流",      icon:"file"},
  btnDl:         {label:"下载",          icon:"download"},
  btnOpenFolder: {label:"在文件夹中定位", icon:"dirOpen"},
  btnScramble:   {label:"混淆"},
  btnUnscramble: {label:"解混淆"},
  /* NSFW 归类的文案随判定翻转（标为 NSFW / 移出 NSFW）⇒ label 取按钮当下的文字，
     不在这里写死第二份（否则两处口径迟早分叉）。 */
  lbNsfwCls:     {label:()=>{
    const e=document.getElementById("lbNsfwCls");
    return (e && e.textContent.trim()) || "NSFW 归类";
  }, icon:"flag"},
};
let _lbNested = [];            // 当前被收进「⋯」的 id（顺序 = 收纳顺序），供菜单渲染
let _fitSig = "";              // 上一次 mvLbFit 的输入签名（见下）

function mvLbFit(){
  const top=document.getElementById("lb-top");
  const more=document.getElementById("lbMore");
  const lb=document.getElementById("lb");
  /* 大图没开时不算：`#lb` 是 display:none，scrollWidth 恒为 0，
     算出来的「放得下」是假结果，还会把收纳态清掉。 */
  if(!top || !more || !lb || !lb.classList.contains("on")) return;
  /* 2026-10-31：**输入签名去重**。同一次开图里本函数会被调用两次 —— openLb 一次、
     renderMeta 收到 /api/meta 后再一次（后者为的是把刚改过 display 的参数按钮算进去）。
     两次之间若「容器宽度 / 收藏下拉宽度 / 各可见项的显隐与文案 / 复制按钮的显隐」
     全都没变，收纳结果必然与上一次逐项相同 ⇒ 整轮跳过。
     跳过省掉的是「复位 → 逐项收 → 每收一项读一次 scrollWidth」：每收一项都会让样式失效，
     下一次读 scrollWidth 就强制一次同步布局，一轮最多 5 次。
     签名只读一次 clientWidth + 一次 offsetWidth（同一趟布局），比整轮便宜一个数量级。
     ⚠️ 收纳态**只由本函数写**（全站没有第二处 add/remove "nested"），所以「输入同 ⇒ 输出同」成立；
        文案按长度入签名，防的是「标签改了字但显隐没变」这种宽度变化。 */
  const _cat=document.getElementById("lbCat");
  const _cp=document.getElementById("btnCopy");
  const _sig=top.clientWidth
    +"|"+(_cat?_cat.offsetWidth:-1)
    +"|"+(_cp?((_cp.style.display||"")+"~"+(_cp.textContent||"").length):"x")
    +"|"+LB_NEST.map(id=>{
      const e=document.getElementById(id);
      return e?((e.style.display||"")+"~"+(e.textContent||"").length):"x";
    }).join(",");
  if(_sig===_fitSig) return;
  _fitSig=_sig;
  /* ① 复位：全部还回顶栏，「⋯」先按「不需要」处理 */
  for(const id of LB_NEST){ const e=document.getElementById(id); if(e) e.classList.remove("nested"); }
  more.classList.add("nested");
  _lbNested=[];
  /* ② 只收「本来该看得见」的项 —— 显隐由 renderMeta 写在 inline style 上，这里只读不写，
        否则会和参数渲染抢同一个开关（无 prompt 的图不该凭空冒出一个「⋯」）。 */
  const cand=LB_NEST.filter(id=>{
    const e=document.getElementById(id);
    return e && e.style.display!=="none";
  });
  const over=()=>top.scrollWidth-top.clientWidth;
  if(over()<=0) return;              // 放得下：顶栏与改动前逐像素一致，不出现「⋯」
  /* ③ 先让「⋯」占位（它自己也要 42px），再按优先级逐项收纳，收一项测一次 */
  more.classList.remove("nested");
  for(const id of cand){
    document.getElementById(id).classList.add("nested");
    _lbNested.push(id);
    if(over()<=0) break;
  }
  /* ④ 一项都收不进去（溢出量比三项总和还大）⇒ 不给一个空菜单，靠 sticky 删除兜底 */
  if(!_lbNested.length) more.classList.add("nested");
}
window.mvLbFit=mvLbFit;

/* 顶栏「复制提示词」的显隐唯一出口：只在**右侧详情面板收起**时出现。
   面板展开时那一栏已有「复制正向提示词」，两个入口做同一件事；
   而它 108px 是窄窗口下压倒顶栏的最大单块。dataset.cp 由 renderMeta 写入。 */
function mvLbSyncCopy(){
  const b=document.getElementById("btnCopy");
  if(!b) return;
  const side=document.getElementById("side");
  const sideOpen=!!(side && !side.classList.contains("hidden"));
  const has=String(b.dataset.cp||"").trim().length>0;
  b.style.display=(has && !sideOpen) ? "" : "none";
  mvLbFit();
}
window.mvLbSyncCopy=mvLbSyncCopy;

/* ---------------- 大图右键：复制文件（2026-11-07） ----------------
   问题：大图上的右键原先交给浏览器原生菜单，其中的「复制图像」是把 <img> **已解码的
   像素**重新编码后放进剪贴板（Windows 上走 CF_DIB）—— ComfyUI 写在 PNG tEXt 块里的
   workflow / prompt 全部丢失。发出去的图对方再拖回 ComfyUI 也复现不出来。

   这里接管图片上的右键，给出两条分开写明的出口：
     · 复制文件（含元数据）：服务端把磁盘上那份文件本身放上剪贴板（CF_HDROP，等于
       资源管理器 Ctrl+C）。粘到聊天窗口是**文件**，粘到文件夹是复制，字节原样。
       这是主行动，所以排第一，也是用户这次要的那一项。
     · 复制图片（不含元数据）：等价于原来的原生行为，保留给「只想在聊天框里贴一张图」
       的场景 —— 名字里如实标注，不能让用户以为它也带元数据。

   ⚠️ 三条不接管的边界（都直接放行给原生菜单，行为与改动前一致）：
     ① 视频：原生菜单的「视频另存为」更有用，而复制文件对视频没有额外好处；
     ② 模糊态（NSFW 未揭开）：复制文件等于把原图绕过遮挡送出去，而「先点左上角眼睛」
        只多一次点击 —— 不留这条缝；
     ③ 远端（手机 / 另一台电脑）：剪贴板是服务端那台机器的，点了在自己机器上粘贴
        什么都没有，属于静默无效 ⇒ 连「复制文件」这一项都不显示（服务端也会 403 兜底）。 */
const LB_CTX_LOCAL_RE=/^(127\.0\.0\.1|localhost|\[::1\])$/;
function lbCtxIsLocal(){ return LB_CTX_LOCAL_RE.test(location.hostname); }

async function lbCopyFile(it){
  try{
    const j=await api("/api/copy_file",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:it.path})});
    if(j && j.error) throw new Error(j.error);
    /* 说清「粘出来的是什么」：粘贴到聊天窗口得到的是文件（接收方仍可拖回 ComfyUI 复现），
       不是一张贴图 —— 不写清楚用户会以为没粘上。 */
    toast("已复制文件（含元数据）","ok",3600,it.name+"　可直接粘贴到聊天窗口或文件夹");
  }catch(e){ toastErr("复制文件失败", e.message||String(e)); }
}

async function lbCopyImage(){
  /* 只用 DOM 里那张 <img> 重编码：与原生「复制图像」逐像素等价，且不额外发一次请求。
     放大 / 平移是 CSS transform，不影响 drawImage 取到的原始像素。 */
  const im=stage.querySelector("img");
  if(!im || !im.complete || !im.naturalWidth) throw new Error("图片还没加载完");
  if(!navigator.clipboard || !window.ClipboardItem)
    throw new Error("当前环境不支持写入图片剪贴板（请直接使用「复制文件」）");
  const c=document.createElement("canvas");
  c.width=im.naturalWidth; c.height=im.naturalHeight;
  c.getContext("2d").drawImage(im,0,0);
  const blob=await new Promise(r=>c.toBlob(r,"image/png"));
  if(!blob) throw new Error("编码失败");
  await navigator.clipboard.write([new ClipboardItem({"image/png":blob})]);
  toast("已复制图片（不含元数据）","ok",3600,"粘贴得到的是位图；要保留工作流请用「复制文件」");
}

/* 复制文件路径：路径是**文本**，写的是本机（浏览器/壳）的剪贴板，所以远端也能用 ——
   远端用户常常是「把路径发给我，我去取这张」。不进服务端，服务端没有可复制的正文。 */
async function lbCopyPath(it){
  const txt=it.path||"";
  if(!txt) throw new Error("这张图没有路径");
  /* 2026-11-15：收敛到 `mvCopyText`（app.js）—— 这里原本自带一份「writeText + textarea 兜底」，
     与 gallery.js / nav.js 走两套实现，局域网 http 下行为会分叉（那边能复制、这边报失败）。
     文案仍归本函数管：调用点 catch 后弹「复制路径失败：<message>」。 */
  try{ await window.mvCopyText(txt); }
  catch(e){ throw new Error("剪贴板不可用（可改用「在文件夹中定位」）"); }
  toast("已复制文件路径","ok",3600,txt);
}

/* 用默认图片查看器打开：本机拉起关联程序（服务端顶层窗口会带到前台）。
   远端点了会在**主机**屏幕上弹窗、自己这边什么都不发生 ⇒ 该菜单项只在本地出现。 */
async function lbOpenExternal(it){
  /* 空 body 也必须带 JSON Content-Type：服务端对所有 POST 做「非 JSON ⇒ 403」的纵深防御，
     空 body 裸 POST 会被它直接挡掉（表现为点了没反应）。传 `{}` 不影响 query 取参。 */
  const j=await api("/api/open_external?p="+encodeURIComponent(it.path),
    {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  if(j && j.error) throw new Error(j.error);
}

/* 另存为原件（保留元数据）：原生保存框选路径，服务端按字节复制。
   与顶栏「下载」的区别就在这两点 —— 下载会抹掉内嵌提示词、且固定落到下载目录。 */
async function lbSaveAs(it){
  let j=null;
  try{
    j=await api("/api/save_as",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:it.path})});
  }catch(err){
    /* 服务端 500 也会带 JSON 原因，但 api() 已经把它变成异常了 —— 这里统一抛出即可 */
    toastErr("另存为失败", err.message||String(err)); return;
  }
  if(j && j.ok){
    if(j.same) toast("这就是原文件，未复制","info",4000,j.path);
    else toast("已另存为原件（含元数据）","ok",6000,j.path);
    return;
  }
  if(j && j.canceled) return;                     // 用户在保存框里点了取消：什么都不做
  if(j && j.no_dialog){ await lbSaveAsViaBrowser(it); return; }
  toastErr("另存为失败", (j&&j.error)||"后端未返回原因");
}

/* 原生保存框弹不出来（非 Windows / 调用失败）时的回落：浏览器原样下载。
   必须**不带** clean=1 —— 这条路的语义就是「拿到原件」，抹了元数据就名不副实。 */
async function lbSaveAsViaBrowser(it){
  const r=await fetch(fileURL(it.path,it)+"&dl=1");
  if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.error||r.statusText); }
  const b=await r.blob();
  const a=document.createElement("a");
  a.href=URL.createObjectURL(b); a.download=it.name; a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),30000);
  toast("已下载原件（含元数据）","ok",6000,"保存框不可用，已改用浏览器下载");
}

stage.addEventListener("contextmenu", e=>{
  const im=(e.target && e.target.tagName==="IMG") ? e.target : null;
  const it=curItem();
  if(!im || !it || it.type!=="image") return;              // 视频 / 空白处：原生菜单
  if(stage.classList.contains("nsfw-blur")) return;          // 模糊态：不提供任何复制出口
  e.preventDefault();
  /* 必须 stopPropagation：popupMenu 会在 document 上挂一个 once 的 contextmenu 关闭监听，
     右键**第二次**时它会晚于本处理器触发，把刚刚新建的菜单又关掉（实测：连按右键
     菜单闪一下就没了）。旧菜单由 popupMenu 开头的 closeCatMenu() 负责收掉，不会漏。 */
  e.stopPropagation();
  /* 顺序按「用得多 → 用得少」，且**复制图片在前**（2026-11-08 用户指定）：
     多数时候只是要贴一张图给别人；要保留工作流才走「复制文件」。
     带 □ 的是本机专属（服务端弹框 / 服务端剪贴板 / 本机进程），远端一律不显示。 */
  const local=lbCtxIsLocal();
  const items=[];
  items.push({icon:"image", label:"复制图片（不含元数据）",
              fn:()=>lbCopyImage().catch(err=>toastErr("复制图片失败", err.message||String(err)))});
  if(local)
    items.push({icon:"copy", label:"复制文件（含元数据）", fn:()=>lbCopyFile(it)});
  items.push({icon:"file", label:"复制文件路径",
              fn:()=>lbCopyPath(it).catch(err=>toastErr("复制路径失败", err.message||String(err)))});
  if(local){
    items.push({icon:"openimg", label:"用默认图片查看器打开",
                fn:()=>lbOpenExternal(it).catch(err=>toastErr("打开失败", err.message||String(err)))});
    items.push({icon:"download", label:"另存为原件（保留元数据）", fn:()=>lbSaveAs(it)});
    items.push(null);
    items.push({icon:"refresh", label:"生成混淆副本（小番茄）", fn:()=>hunxiaoCopy(it,"encrypt")});
    items.push({icon:"arrowUpLeft", label:"还原混淆副本", fn:()=>hunxiaoCopy(it,"decrypt")});
  }
  items.push(null);
  items.push({icon:"dirOpen", label:"在文件夹中定位",
              fn:()=>{ const b=document.getElementById("btnOpenFolder"); if(b) b.click(); }});
  popupMenu(e.clientX, e.clientY, items);
});

/* 「⋯」菜单：点其中的条目 = 点原来那颗按钮（不复制事件逻辑，也就不存在两套行为） */
$("#lbMore").onclick=e=>{
  e.stopPropagation();
  if(!_lbNested.length) return;
  const r=e.currentTarget.getBoundingClientRect();
  popupMenu(r.left, r.bottom+4, _lbNested.map(id=>{
    const m=LB_NEST_META[id]||{label:id,icon:null};
    const label=(typeof m.label==="function")?m.label():m.label;
    return {icon:m.icon, label:label, fn:()=>{ const b=document.getElementById(id); if(b) b.click(); }};
  }));
};

async function openLb(i, solo){
  // solo=true：调用方（uploadSolo）已经填好 S.solo，这里只是别把它清掉。
  // 其余入口（网格 / 胶片条 / 翻页）一律回到库模式 —— 否则 S.solo 还在时
  // 会从「只有一张」的单图列表里取到下标相同的另一张，出现「点 A 打开 B」。
  if(!solo) clearSoloView();
  const it=curList()[i];
  if(!it || (it.type!=="image" && it.type!=="video")) return;   // 文件夹占位卡不可打开
  // 图库只保留当前图的处理结果，单图批次仍可在翻页后找回结果。
  if(!S.solo) clearScramblePreviews(scrambleKey(it));
  const tok=++lbTok;
  if(S.solo) S.solo.idx=i; else S.idx=i;
  if(!lb.classList.contains("on")){                        // 只有「第一次打开」才记触发点：
    lbFocusBack=(document.activeElement && document.activeElement!==document.body)
      ? document.activeElement : null;                     // 翻页（step）会反复调进来
  }
  lb.classList.add("on");
  { const c=$("#btnClose"); if(c && !lb.contains(document.activeElement)) c.focus(); }
  /* E5-2（2026-09-23）：顶栏文件名被 ellipsis 截断后，用户此前**没有任何办法看全**
     —— 补 data-tip 作出口（与 D2 定下的「全站只有 data-tip 一个出口」一致）。
     无条件挂、不做「截断才挂」：后者要读 scrollWidth 量一次，且窗口宽度变化后提示会变陈旧
     （变宽后多余、变窄后缺失，缺的正好是需要的那个）。 */
  { const nm=$("#lbName"); nm.textContent=it.name; nm.dataset.tip=it.name; }
  setIcoLabel($("#btnFav"), it.fav?"starFill":"star", it.fav?"取消收藏":"收藏",
                it.fav?"filled":"");
  { const ms=$("#lbMark"); if(ms) ms.value=MARK_MAP[it.path]||"none"; }
  refreshLbTips();                     // 说明书跟着这张的收藏态 / 当前设置走
  // 单图模式不记「最近使用」：临时副本会被暂存区清掉，记进去就是一条死链
  if(!S.solo) recordRecent(it.path, "preview");   // 打开大图 = 一次「最近使用」
  fillCatSelect($("#lbCat"), false);
  { const want=it.favcat||"默认";
    const opts=[...$("#lbCat").options];
    $("#lbCat").value=opts.some(o=>o.value===want)
      ? want : (opts.find(o=>o.value!=="__new__")||{value:"默认"}).value; }
  /* 2026-10-31：选中值定下来之后才量得出真实文字宽（fillCatSelect 末尾那次量的是旧值）；
     同时先清掉上一张留下的收纳态 —— 参数要等图读完才渲染，那段空窗期不该沿用旧布局。 */
  if(window.mvFitCatSelect) window.mvFitCatSelect($("#lbCat"));
  releaseStageVideo();                 // ε-2：切条目前先释放上一个视频（原只摘 DOM，下载仍在跑）
  releaseStageImg();                   // F6-P1-1：图片同理 —— 已翻走的那张不该继续占带宽
  stage.innerHTML="";
  $("#zoomBadge").style.display="none";
  S.vidInfo=null;
  if(it.type==="image"){
    /* ε-2（2026-09-20）：补 onerror。
       原写法只有 onload ⇒ 图加载失败（文件被移走/删掉、权限、损坏）时舞台**永久空白**，
       既没有提示也不报错，用户只能以为「卡住了」。
       对照：网格卡片（gallery.js）与回收站缩略图（extras.js）都有 onerror 兜底，
       唯独大图这条最该有反馈的路没有 —— 属于口径不一致。
       ⚠️ 处理器要在赋值 src **之前**挂上：src 是同步起手的，写在后面可能错过当次失败。 */
    const img=new Image();             // **最终图层**：原图（真正参与缩放与布局的那个）
    img.alt=it.name;                   // 读屏用户至少能拿到文件名
    img.dataset.source=fileURL(it.path, it);
    /* 2026-10-31 · **先缩略图后原图（双层 · 交叉淡化）**。
       上一版是「同一个 img 换 src」，实测有肉眼可见的闪烁，根因已定位（探针
       dev/_probe_lbflash.py 帧级 + dev/_probe_lbflash_frames.py 真实合成帧）：

         t=  1ms  src=thumb  nw=213   complete=1   rect=1052×661.6   boxShadow=none
         t=  6ms  src=file   nw=0     complete=0   rect=1052×661.6   boxShadow=none   ← 这一帧是空的
         t= 20ms  src=file   nw=1280  complete=1   rect= 441×661.5   boxShadow=有

       合成帧的舞台区亮度 28.2 → **9.1** → 28.2、锐度 1735 → **1.1** → 1735，而 `--lb-bg`
       的灰度正是 9 —— 也就是**整整一帧纯底色**。原因：表达式带着 `decoding="async"` 时，
       Chromium 在换 src 的瞬间就 `SetImage(null)` 丢掉旧帧，新图解码完成前**不绘制**。
       `drawImage` 读的是图片资源而不是合成结果，所以上一版检查脚本（O/S/Z）全部通过
       却漏掉了它 —— 这也是本批换成「看真实像素」的原因。

       ⚠️ 改前为什么不闪：当时是**新建元素**直接上原图，元素本来就无内容，「无→有」符合预期；
          换 src 后变成「有→无→有」，人眼对「内容消失」远比「不够清晰」敏感。

       所以这里改成**两个元素，绝不共用同一个资源槽**：
         · 占位层 `.lb-ph`：绝对定位、`object-fit:contain`、`pointer-events:none`、
           `box-shadow:none`，只负责「先有画面」，全程不换 src ⇒ 永远不会空；
         · 最终图层：从插入起就 `opacity:0` 但**照常参与布局**（尺寸/居中一次到位，
           避免上一版「占位期撑满 1052 再缩回 441」的收缩），原图 onload 后与占位层
           交叉淡化 140ms 并移除占位层。
       附带解决两件事：① `--stage-glow` 的投影跟着 opacity 一起淡入，不再「凭空亮起」；
       ② 「糊 → 清」由单帧突变变成 140ms 渐变（实测缩略图档锐度 1735 vs 原图 3172）。

       ⚠️ 占位图仍然**只取卡片上那张已经画出来的缩略图**（`currentSrc`，含 v= 指纹，
          必然是缓存命中）。不用 srcset：给了 `2x` 描述符后浏览器会把密度记在元素上，
          换回原图时 `naturalWidth` 掉成一半（1280→640，上一版实测踩过）。
       ⚠️ 两个元素并行加载：占位层不阻塞原图，原图也不再需要「等缩略图 onload 才起手」
          （上一版串行，白等一帧）。 */
    const _orig=scramblePreviewFor(it)?.url || img.dataset.source;
    const _ce=S.cardEl && S.cardEl.get(it);
    const _cim=(_ce && _ce.isConnected) ? _ce.querySelector("img") : null;
    const _phSrc=(_cim && _cim.complete && _cim.naturalWidth>0
                  && /\/api\/thumb/.test(_cim.currentSrc||"")) ? _cim.currentSrc : "";
    let _ph=null;                      // 占位层（独立元素）
    /* 占位层与原图**尺寸对齐**：原图 onload 时布局已按 naturalWidth 重排，
       把占位层挪到同一个 rect 上，随后两层完全重合地交叉淡化 —— 只看到清晰度变化，
       看不到任何位移。
       ⚠️ 绝对定位的包含块是 **padding box**，`left:0` 就对应 `getBoundingClientRect()` 的
          左边界（#stage 没有 border）⇒ **直接相减，不要再减 padding**。
          减了会整体偏移 var(--sp-2)（实测 8px），反而制造出它要消除的那种位移。 */
    const alignPh=()=>{
      if(!_ph || !_ph.isConnected) return;
      const r=img.getBoundingClientRect();
      if(!r.width || !r.height) return;
      const sr=stage.getBoundingClientRect();
      /* 1px 容差：预设尺寸（下面 metaMap 那段）已经摆正时不要再写一遍 ——
         写与不写虽然结果一样，但会多一次样式失效，也可能把取整误差变成肉眼可见的微移。 */
      const nx=r.left-sr.left, ny=r.top-sr.top;
      if(Math.abs(nx-parseFloat(_ph.style.left||"0"))>1)
        _ph.style.left=nx+"px";
      if(Math.abs(ny-parseFloat(_ph.style.top||"0"))>1)
        _ph.style.top=ny+"px";
      if(Math.abs(r.width-parseFloat(_ph.style.width||"0"))>1)
        _ph.style.width=r.width+"px";
      if(Math.abs(r.height-parseFloat(_ph.style.height||"0"))>1)
        _ph.style.height=r.height+"px";
    };
    /* 撤占位层：now=true 立即摘（失败态 / 切页），否则等上层淡入跑完再摘。
       ⚠️ 占位层**全程不透明、不做淡出**。两层同时半透明不是交叉淡化 ——
          alpha 合成是 source-over，下面还有一个不透明背景参与：
          t 时刻的亮度 = 占位×(1-t) + 原图×t² + 底色×t(1-t)，t=0.5 时整整**暗掉 25%**。
          实测踩过：舞台区亮度 28.6 → 23.4（-18%），看起来就是"淡入过程中闪一下暗"。
          正确做法是占位层按住不动、让**上层**原图淡入 ⇒ 亮度 = 原图×t + 占位×(1-t)，守恒。 */
    const dropPh=(now)=>{
      const p=_ph; _ph=null;
      if(!p) return;
      if(now){ p.remove(); return; }
      setTimeout(()=>{ if(p.isConnected) p.remove(); }, 240);
    };
    img.onerror=()=>{
      if(!img.isConnected) return;     // 已经翻到别的条目了，别再往新舞台上写
      dropPh(true);
      img.remove();
      stage.innerHTML='<div class="lb-fail">图片加载失败<div class="sub">「'+
        esc(it.name)+'」可能已被移动、删除，或格式已损坏</div></div>';
    };
    img.onload=()=>{
      if(!img.isConnected || img.dataset.full==="1") return;
      img.dataset.full="1";            // 原图就位（调试 / 门禁读取）
      bindZoom(img);                   // 缩放按原图 naturalWidth 算，必须在原图就位后绑
      syncImageScramble(it);
      if(_ph && _ph.isConnected){
        alignPh();
        /* 先让浏览器把原图画出一帧（此时占位层仍盖在上面），再开始交叉淡化。
           不这样做的话，第一帧 opacity 还是 0，而占位层已开始掉不透明度 ⇒ 中间偏暗。 */
        requestAnimationFrame(()=>{
          img.style.transition="opacity 140ms linear";
          img.style.opacity="1";
          dropPh(false);
        });
      }else{
        img.style.opacity="1";
      }
    };
    img.decoding="async";              // V45：解码让出主线程（大图翻页更顺）
    _stageImg=img;                     // F6-P1-1：登记给 releaseStageImg() 用（切页即中止）
    if(_phSrc){
      _ph=new Image();
      _ph.className="lb-ph";           // 样式见 viewer.css：#stage img.lb-ph
      _ph.alt="";
      _ph.decoding="async";
      _ph.onerror=()=>{ _ph && _ph.remove(); _ph=null; };   // 占位挂了也不能连累原图
      /* **预排尺寸**：能拿到原图宽高（`S.metaMap`，/api/meta 批量带回）时，占位层从一开始
         就摆在最终绘制区上。不预排的话，比舞台小的图（截图、缩略成图）会在原图 onload
         那一帧从「撑满舞台」突然缩回自然尺寸 —— 实测 480×320 出现一帧亮度 32→63、
         锐度 4044→1442 的跳变，正是一次可见的"抽一下"。
         拿不到尺寸就退回「撑满」，交给 onload 里的 alignPh 对齐（大图两者本来就重合）。 */
      const _mm=S.metaMap && S.metaMap[it.path];
      const _ow=(_mm && _mm.w)|0, _oh=(_mm && _mm.h)|0;
      if(_ow>0 && _oh>0 && stage.clientWidth>0 && stage.clientHeight>0){
        const _cs=getComputedStyle(stage);
        const _aw=stage.clientWidth -(parseFloat(_cs.paddingLeft)||0)-(parseFloat(_cs.paddingRight)||0);
        const _ah=stage.clientHeight-(parseFloat(_cs.paddingTop )||0)-(parseFloat(_cs.paddingBottom)||0);
        const _f=Math.min(1, _aw/Math.max(1,_ow), _ah/Math.max(1,_oh));
        const _w=Math.max(1,Math.round(_ow*_f)), _h=Math.max(1,Math.round(_oh*_f));
        _ph.style.width=_w+"px"; _ph.style.height=_h+"px";
        _ph.style.left=Math.round((stage.clientWidth -_w)/2)+"px";
        _ph.style.top =Math.round((stage.clientHeight-_h)/2)+"px";
      }
      _ph.src=_phSrc;
      img.style.opacity="0";           // 不可见但**参与布局**：尺寸/居中一次到位
      /* 层序：占位层在**下**、原图层在**上**。
         ⚠️ 不能靠 DOM 顺序 —— 绝对定位元素永远画在流内元素之上（同 z-index）。
            所以显式给占位层 z-index:0、原图层 relative+z-index:1。
            relative 不影响 flex 布局，alignPh 的 rect 也不受影响。 */
      img.style.position="relative";
      img.style.zIndex="1";
      stage.appendChild(_ph);
    }else{
      img.style.opacity="1";           // 没有缩略图能力：直接上原图（与改前一致）
    }
    img.src=_orig;
    stage.appendChild(img);
  }else if(it.playable){
    /* V46（批次 G3）：撤原生 controls —— 跟不了主题与 --fsx（与 V35 滑块同源问题）；
       进度 / 音量 / 时间由 #vidBar 自绘（extras.js bindVidBar），播放/倍速/循环/逐帧原本就有。
       M1（2026-10-08，用户令「视频不要自动播放，突然出声有点吓人」）：
        去掉了这里的**自动播放属性** —— 视频现在停在**首帧当海报**，等一次明确动作才播。
        ⚠️ 本注释**刻意不写出那个被删掉的属性名**：它含有 auto/play 字样，留在注释里会让
        「源码里不得出现自动播放属性」这类子串断言**假命中**（本项目「注释冒充代码」的老坑）。
        「要点一下」这次点击本身就是音量的明确大开，比静音自动播更彻底（后者一解除静音仍会突然出声，
        且 bindVidBar 每次把音量复位 ⇒ 翻页时屡次漏声）。 */
    const v=document.createElement("video"); v.playsInline=true;
    /* F6-P1-2（2026-10-17）：加载 / 解码失败必须给反馈。
       原来只有 onloadedmetadata —— 视频被移走、外接盘离线、格式浏览器解不了、文件损坏时
       舞台**永久全黑**，而 #vidBar 照常亮着，用户看到的是「黑屏 + 一套点了没反应的控件」。
       对照上面图片分支早就有 onerror ⇒ 同一块舞台两种媒介的质检口径不一致。
       ⚠️ 必须挂在 `v.src=` **之前**（src 同步起手）；失败时视频条与中央播放按钮要一起收。 */
    v.onerror=()=>{
      if(!v.isConnected) return;                  // 已翻到别的条目，别再往新舞台上写
      v.onerror=null;                             // 先摘：下面的清理会重置媒体元素
      const code=(v.error && v.error.code)||0;
      const WHY={1:"加载被中断",2:"网络错误",3:"解码失败",4:"格式不受支持或文件已损坏"};
      releaseStageVideo();
      v.remove();
      const vb=$("#vidBar"); if(vb) vb.classList.remove("on");
      if(window.mvSyncToastLift) window.mvSyncToastLift();
      hideLbPlay();
      stage.innerHTML='<div class="lb-fail">视频加载失败<div class="sub">「'+
        esc(it.name)+'」'+(WHY[code]||"可能已被移动、删除，或格式已损坏")+'</div></div>';
    };
    /* F6-P1-3（2026-10-17）：元信息回填走 addEventListener。
       `onXX` 是单值属性 —— 原来这里写 `v.onloadedmetadata=…`，把下面 bindVidBar(v) 刚挂上的
       `syncT` **整体覆盖**了（同一属性只有一个赢家）。后果：#vTime / #vSeek 在拿到元信息那
       一刻不再刷新，只能等播放中的 timeupdate 才补 ⇒ 自动播放被策略拦下、或元数据比首帧晚到时，
       时长与进度条一直停在 `0:00 / 0:00`。 */
    v.addEventListener("loadedmetadata", ()=>{
      S.vidInfo=`时长：${fmtDur(v.duration)} ｜ 分辨率：${v.videoWidth} x ${v.videoHeight}`;
      const r=meta.querySelector(".vidinfo"); if(r) r.textContent=S.vidInfo;
    });
    v.src=fileURL(it.path, it); stage.appendChild(v); bindVidBar(v);
    lbPlayFor(v);
  }else{
    stage.innerHTML='<div style="color:var(--tx2)">该视频格式浏览器无法直接播放（'+esc(it.name.split(".").pop())+'）</div>';
  }
  syncImageScramble(it);
  nsfwApplyLb(it);                                  // 内容安全：模糊 + 提示条
  mvLbFit();                                       // 按当前按钮重新计算收纳
  prefetchLb();                                     // V52：稳定后预取左右邻居
  meta.innerHTML='<div class="skel" style="height:14px;margin-bottom:8px"></div><div class="skel" style="height:12px;width:70%"></div>'; S.curMeta=null;
  try{
    const j=await api("/api/meta?p="+encodeURIComponent(it.path));
    if(tok!==lbTok || curIdx()!==i) return;          // 已切到别的条目，丢弃
    S.curMeta=j; renderMeta(j,it);
    // 单图模式的唯一目的就是看配方，落空时必须解释原因；库内不弹（见函数注释）
    if(S.solo && !hasGenMeta(j)) soloNoMetaToast(j);
  }catch(e){ if(tok!==lbTok) return;
    meta.innerHTML='<div class="row">元数据读取失败：'+esc(e.message)+"</div>"; }
}

/* ---------- 内容安全：大图（V2.11.0，V2.11.1 扩展，V2.11.2 删提示条）----------
   从网格点进大图是最容易意外曝光的一条路径 —— 所以这里**绝不自动揭开**，
   必须由用户再点一次。揭开只影响本次会话（见 state.js 里 NSFW_REVEAL 的注释）。

   大图上有两个安全相关的控件，状态**全部**由 nsfwApplyLb() 现算：
     #lbEye      舞台左上角的眼睛（和网格卡片的 .nv-eye 同一套语义：
                 盖着=红眼睛看原图，揭开=划线眼重新模糊）
     #lbNsfwCls  顶栏快速归类按钮（标签随当前判定翻转，双向）
   V2.11.2 删掉了横跨顶部的黄色提示条（#lbNsfw）：它的两个按钮（点击查看 /
   判错了）与眼睛、归类按钮完全重复，属于第三个入口，还占一整行高度。

   为什么不做「点舞台揭开」：大图上还有拖拽平移和滚轮缩放，拿 click 揭开的话
   「拖一下就露出来了」，那是把安全功能交给了手势误判 —— 揭开只走明确的按钮。

   同一个真相来源原则：控件的显隐/文案一律从 it + NSFW_MODE + NSFW_REVEAL
   现场推导，HTML 不预置任何状态。改判定（人工归类）后调 nsfwRefreshLb() 就能
   让控件 + 网格卡片 + 胶片条一起跟上，不存在某个控件忘了刷的死角。 */
function nsfwApplyLb(it){
  stage.classList.remove("nsfw-blur");
  nsfwSyncLbEye(it);
  nsfwSyncLbCls(it);
  if(nsfwShouldVeil(it)) stage.classList.add("nsfw-blur");
}
/* 大图正开着、且操作的就是当前这张 → 三个安全控件立刻重算。
   供 setNsfwManual（人工归类）在改完 it.n/it.ns 之后调用。 */
function nsfwRefreshLb(path){
  if(!lb.classList.contains("on")) return;
  const cur=curItem();
  if(!cur || cur.path!==path) return;
  nsfwApplyLb(cur);
}
/* 左上角眼睛：盖着/揭开两态**共用同一枚斜杠眼**（eyeOff）—— 和网格卡片完全同一套规则，
   唯一区别是这里没有中央遮挡层，模糊直接挂在 stage 上。
   δ-2c：按用户要求撤掉 δ-2b 临时加的「眼睛+×」（eyeHide），回到斜杠眼。 */
function nsfwSyncLbEye(it){
  const eye=$("#lbEye"); if(!eye) return;
  const show = isNsfw(it) && NSFW_MODE !== "show";
  eye.hidden = !show;
  if(!show) return;
  const on = NSFW_REVEAL.has(it.path);
  eye.classList.toggle("on", on);
  eye.innerHTML = ico("eyeOff");
  /* ⚠️ 只写 data-tip，不写 title —— D2 起全站提示只有 data-tip 一个出口，
     写 title 会与自绘气泡双显（v4_batchD2_check / v31_tip_step1_check 直接红）。 */
  eye.setAttribute("data-tip",
    on ? "重新模糊这张（只影响当前会话）" : "单独查看这张原图");
}
/* 顶栏快速归类按钮：当前是 NSFW → 「移出」；否则 → 「标为」。
   判定口径用 nsfwLabelOf()（与遮罩/菜单一致），避免界面和按钮各说各话。
   单图模式不显示：临时副本会被暂存区清掉，给它写人工纠正没有意义。 */
function nsfwSyncLbCls(it){
  const b=$("#lbNsfwCls"); if(!b) return;
  const media = it && (it.type==="image" || it.type==="video");
  /* 判据 nsfwReady() 而不是 nsfwOn()：没跑过检测时每张都是 unknown，标 NSFW/移出
     都不会有任何视觉变化（除非点「移出」的人想让这张躲过将来的检测）。
     引擎都没装就出现这个按钮更没道理 —— 它写的是人工纠正表，那是有检测结果之后的事。 */
  const show = nsfwReady() && !S.solo && media;
  b.style.display = show ? "" : "none";
  if(!show) return;
  const toNsfw = nsfwLabelOf(it) !== "nsfw";
  b.innerHTML = '<span class="ic">'+ico(toNsfw?"eyeOff":"eye")+'</span>'
    + (toNsfw ? "标为 NSFW" : "移出 NSFW");
  b.setAttribute("data-tip", toNsfw
    ? "把这张标为 NSFW（写入手动纠正表，重新检测也不会翻回去）"
    : "把这张移出 NSFW（写入手动纠正表，重新检测也不会翻回去）");
}
function nsfwToggleLbReveal(){
  const it=curItem(); if(!it) return;
  if(NSFW_REVEAL.has(it.path)) NSFW_REVEAL.delete(it.path);
  else NSFW_REVEAL.add(it.path);
  nsfwApplyLb(it);
  nsfwSyncGridCard(it.path);
}
/* 揭开后顺手把网格里那张卡也点亮（含左上角眼睛的图标态）：
   同一张图不该「大图看了、回网格又糊回去」。绘制逻辑统一在 nsfwPaintCard（filters.js）。 */
function nsfwSyncGridCard(path){
  if(typeof nsfwPaintCard === "function") nsfwPaintCard(path);
}
/* V2.11.2：随提示条一起删掉的还有 nsfwRevealLb（#lnShow 专用）与
   nsfwMarkSfw（#lnFix 专用）—— 职责已由眼睛的 nsfwToggleLbReveal 与
   顶栏按钮的 setNsfwManual 直调接管，不留死代码。 */
(function wireNsfwLb(){
  const eye=$("#lbEye"), cls=$("#lbNsfwCls");
  if(eye) eye.onclick=nsfwToggleLbReveal;
  if(cls) cls.onclick=()=>{ const it=curItem(); if(!it) return;
    setNsfwManual(it, nsfwLabelOf(it)==="nsfw" ? "sfw" : "nsfw"); };
})();

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}

/* 「有没有可看的东西」的唯一判定。renderMeta 的空态文案与单图模式的提示都用它，
   一处定义两处使用，免得出现「面板说没有、提示不说」这类漂移。

   ⚠️ 判定口径是「有没有**生成信息**」，不是「meta 对象空不空」：
   后端对**任何**图片都会塞进 IHDR（尺寸），所以 `Object.keys(meta).length` 恒 ≥1，
   拿它当条件的话这个分支永远不成立 —— 旧代码正是这么写的，于是那句空态文案
   在真实图片上从未显示过，用户点开一张没有配方的图只看到「修改时间｜大小｜尺寸」，
   没有任何解释。（IHDR / EXIF 这类技术字段也不该算「配方」。） */
function hasGenMeta(j){
  if(!j) return false;
  if(j.parsed) return true;                       // 已解析出模型 / 步数 / 提示词
  const m=j.meta||{};
  return !!(m.prompt || m.workflow || m.parameters);   // 原始块也算：面板里能看到
}

/* V8 · 长文本折叠：阈值 6 行。行数按「换行 + 长行折行估算」算，不写死 px。 */
const META_FOLD_LINES = 6;
function metaTextLines(t){
  if(!t) return 0;
  let n=0;
  for(const ln of String(t).split("\n")) n += Math.max(1, Math.ceil(ln.length/72));
  return n;
}
/* V8 · 展开/收起状态全局记忆：同一个面板里切图不跳回折叠态。 */
let META_FOLD = false;

/* 2026-11-03：提示词字数 —— 标题右侧小字（用户要求「在正向提示词后小字展示」）。
   口径必须跟中文语境下的直觉一致，别自作聪明：
     · 主数字 = **去掉所有空白后**的字符数。中文 1 字 = 1 字符；英文按字符算
       （ComfyUI 提示词里的换行 / 缩进 / 多余空格不是内容，算进去只会虚高）。
     · 悬停补完整口径：含空格与换行的字符数 + 有内容的行数。
   不报「词数」：中英混排时按空白切词毫无意义（一长串中文会被算成 1 个词），
   与其给个会被误读的数，不如不给。
   ⚠️ 不用 toLocaleString：千分位分隔符随浏览器语言环境变（德语是 .），
      同一张图在不同机器上显示不同字符 —— 手写插逗号，结果恒定。 */
function metaFmtNum(n){
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}
function metaCount(txt){
  const s = String(txt||"");
  return {
    bare: s.replace(/\s+/g,"").length,                            // 去空白后的字数（主数字）
    all: s.length,                                                // 含空格与换行的字符数
    lines: s.split(/\r?\n/).filter(x=>x.trim()).length            // 有内容的行数
  };
}
function metaCountChip(txt){
  const c = metaCount(txt);
  const tip = "去空白 "+metaFmtNum(c.bare)+" 字 · 含空格与换行 "+metaFmtNum(c.all)
    +" 字符 · 有内容的行 "+metaFmtNum(c.lines);
  /* 只给 data-tip：气泡由 app.js 的 attachTips 事件委托统一出（不写 title 双份）。 */
  return '<span class="mfcnt" data-tip="'+esc(tip)+'">'+metaFmtNum(c.bare)+' 字</span>';
}

/* V8 · 可折叠长文本块（提示词）。折叠高度由 --lh-loose 算出（见 viewer.css）。
   注意：仍输出 <pre class="ptxt"> 原样 —— 既有门禁按 'class="ptxt"' 精确串断言，
   且第一个 .ptxt 必须是正向提示词正文（copy_prompt_check「所见即所复制」）。
   第三参 showCount：只有「正向提示词」传 true（用户只要正向；负向保持原样，
   要一起显示改调用处即可）。 */
function metaFold(title, txt, showCount){
  if(!txt) return "";
  const long = metaTextLines(txt) > META_FOLD_LINES;
  const open = META_FOLD && long;
  return '<div class="mfold'+(long?(open?" open":""):" short")+'">'
    +'<div class="mfhd"><span>'+esc(title)+'</span>'
      +(showCount ? metaCountChip(txt) : "")
      +(long?'<button type="button" class="mfbtn" data-fold="1">'+(open?"收起":"展开")+'</button>':"")
    +'</div>'
    +'<pre class="ptxt">'+esc(txt)+'</pre>'
    +'<div class="mfact"><a href="javascript:void(0)" class="cp" data-cp="'+esc(txt)+'">复制'+esc(title)+'</a></div>'
    +'</div>';
}

/* 2026-11-03：多段采样流的两个区块 —— 模型列表 + 采样链路表。
   上一版（方案 B）把第 2 段起做成「主行下面的灰小字注解」（`2› 短名`），问题在**位置**：
   模型被关在字段表的 dd 里（值列只有 ~216px），第二段起只能缩着写，字重字号还和主行
   不一致 ⇒ 看着像「一条信息 + 几条补充」，不像一条流。现在升格为独立区块：
     · 模型：一段一行、全名、段号方块（与下面采样表共用同一套段号）；
     · 采样链路：序号 / 采样器 / [尺寸] / 步 / CFG / 种子 —— 靠**列对齐**读，
       不再堆成每段两行（6 段时 6 行，而不是 12 行）。
   ⚠️ 只在「段与段真有差异」时才出表：参数完全一致时沿用字段表那四行，
      单模型图与「多段但参数相同」的图外观一个像素都不变。
   数据：parsed.models = [{name, stage, sampler?, scheduler?, steps?, cfg?, seed?, w?, h?}]，
   后端只在 ≥2 段时才写 ⇒ 单模型图这里拿到 undefined，走原样分支。 */
function metaStageHas(o){
  return ["sampler","scheduler","steps","cfg","seed"].some(k=>o[k]!=null && o[k]!=="");
}
function metaStageSig(o){
  return ["sampler","scheduler","steps","cfg","seed"].map(k=>o[k]==null?"":o[k]).join("\u0001");
}

/* 模型区块：段号 + 全名（+ 第一段的 hash）。全名不再退化成短名 —— 一段一行放得下。 */
function metaModelBox(list, hash){
  const row=(o,i)=>{
    const nm=String(o.name||"");
    const cp=esc((i===0&&hash)?(nm+" ("+hash+")"):nm);
    return '<div class="a1row'+(i===0?" first":"")+'">'
      +'<span class="sno">'+esc(String(o.stage||i+1))+'</span>'
      +'<span class="nm cpv" data-tip="'+esc(nm+(i===0&&hash?" ("+hash+")":""))+'（点击复制）"'
        +' data-cp="'+cp+'">'+esc(nm)+'</span>'
      +(i===0&&hash?'<span class="hs">'+esc(hash)+'</span>':"")
      +'</div>';
  };
  return '<div class="mbx"><div class="lh"><span>模型</span>'
    +'<span class="cnt">'+list.length+' 段</span></div>'
    +list.map(row).join("")+'</div>';
}

/* 采样链路表。尺寸列**按情况出现**：各段尺寸一致时是 5 列（宽松好看，这也是常态）；
   只有真的不一致（hires 两段 1216×832 → 1824×1248）才加第 6 列 —— 实测 6 列时
   种子必须给到 80px、采样器列只剩 ~92px（`dpmpp_2m/karras` 刚好卡满），为少数图
   让所有图都挤成 6 列不划算。缺失值一律 —（不猜、不拿第 1 段的值顶）。 */
function metaSampTable(list){
  const sizes=list.map(o=>(o.w&&o.h)?(o.w+"×"+o.h):"");
  const sizeCol=new Set(sizes).size>1;
  const cell=v=>(v==null||v==="")?"—":String(v);
  const head='<div class="sgr head"><span></span><span>采样器</span>'
    +(sizeCol?'<span class="sz">尺寸</span>':"")
    +'<span class="n">步</span><span class="n">CFG</span><span class="sd">种子</span></div>';
  const body=list.map((o,i)=>{
    const sp=(o.sampler||"")+(o.scheduler?"/"+o.scheduler:"");
    return '<div class="sgr">'
      +'<span class="i">'+esc(String(o.stage||i+1))+'</span>'
      +'<span class="sp" data-tip="'+esc(sp?sp+"（点击复制）":"未读到")+'"'
        +' data-cp="'+esc(sp)+'">'+esc(cell(sp))+'</span>'
      +(sizeCol?'<span class="sz">'+esc(cell(sizes[i]))+'</span>':"")
      +'<span class="n">'+esc(cell(o.steps))+'</span>'
      +'<span class="n">'+esc(cell(o.cfg))+'</span>'
      +'<span class="sd cpv" data-tip="点击复制" data-cp="'+esc(cell(o.seed))+'">'
        +esc(cell(o.seed))+'</span></div>';
  }).join("");
  return '<div class="sgtab'+(sizeCol?" v1":"")+'"><div class="lh"><span>采样链路</span></div>'
    +head+body+'</div>';
}

/* V47（2026-10-08）：LoRA 结构化区块 —— 一条一行 + 权重右锚徽章。
   输入是后端拍平的 list[str]（"名字 (权重)"），这里回拆成 {n:名字, s:权重文本}。
   ⚠️ 名字用 title 属性保留全称：面板窄（内容 ~300px），长 LoRA 名必然省略，
      省略后 hover 能看全 —— 否则用户只知道「有这么个 LoRA」却读不出是哪个。
   ⚠️ 复制走 data-cp（沿用 .cpv 的既有复制机制）：点名字复制**完整原文**（含权重），
      避免「省略号看起来像名字的一部分」。 */
function metaLoras(list){
  const items = list.map(x=>{
    const s0 = String(x||"").trim();
    const m = /^(.*?)\s*\(([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?)\)$/i.exec(s0);
    return m ? { n: m[1], s: m[2] } : { n: s0, s: "" };
  }).filter(o=>o.n);
  if(!items.length) return "";
  return '<div class="lorabox">'
    + '<div class="lh"><span>LoRA</span><span class="cnt">'+items.length+'</span></div>'
    + items.map(o=>
        '<div class="lora-row">'
          + '<span class="lora-nm cpv" data-tip="点击复制" data-cp="'+esc(o.n+(o.s?" ("+o.s+")":""))+'">'+esc(o.n)+'</span>'
          + (o.s ? '<span class="lora-st">'+esc(o.s)+'</span>' : "")
          + '<span class="lora-sites">' + ['red', 'com'].map(site=>
              '<a class="lora-civitai" data-host="civitai.'+site+'" data-name="'+esc(o.n)+'" href="'+esc(loraSearchUrl(o.n, 'civitai.'+site))+'" target="_blank" rel="noopener noreferrer" title="在 C 站查找该 LoRA" aria-label="在 C 站查找该 LoRA">'+(site==='red'?'R':'C')+'</a>').join('') + '</span>'
        + '</div>').join("")
    + '</div>';
}

/* 2026-11-07：正向提示词是「运行时节点」在生成那一刻算出来的 —— ComfyUI 不把它
   写进 PNG（WD14 Tagger 用 {"ui":{"tags":…}} 回传，结果只写回节点 widget，要等**下一次**
   排队才被序列化）。实测 12CADAF8….png：文件里存的是 swimsuit / beach / volleyball，
   画面却是白裙血剑，一字节都对不上（那份是**上一次运行**的残留）。
   第二例 EFCFF29….png：CLIPTextEncode.text 直连 RandomPromptPicker，每次从
   krea2_prompts.txt 里随机抽一句，抽中的那句根本没落盘 —— 连「上次的残留」都没有。
   所以这块**只解释、不冒充正文**：说清为什么读不到；缓存另起一块、标题里写明「上次运行」，
   复制入口也只叫「复制缓存标签」—— 绝不能让人把上一张图的标签当成本图提示词去重跑。 */
function metaRuntimePrompt(P){
  if(!P || !P.positive_src) return "";
  /* 2026-11-08：节点名优先用 ComfyUI 里用户自己起的标题（`_meta.title`，如「✨图像反推提示词」），
     类名退到括号里 —— 只报 "ImageCaptionNode" 时，用户对着工作流找不到是哪一个节点。
     两者都没有才退回原来的泛称。 */
  const who = P.positive_title
    ? esc(P.positive_title) + (P.positive_node ? '（'+esc(P.positive_node)+'）' : "")
    : (P.positive_node ? esc(P.positive_node) : "打标 / 反推节点");
  /* 2026-11-08 `positive_partial`：正向**只读到一部分**。
     实测 E639D3B6….png：CLIPTextEncode ← ZML_SelectTextV2（文本1 = 字面量
     "best quality, highres"，文本3 ← ImageCaptionNode）。这一段字面量是文件里真有的，
     按旧逻辑却因为键名是中文整段丢掉；反过来只把这段当前正向显示、不解释，也同样误导
     （用户会以为这就是全部提示词）。所以两件事一起做：显示读到的那截 + 说清缺的那截是谁产的。 */
  let h = P.positive_partial
    ? '<div class="mnote"><b>这只是正向提示词的一部分</b>：文件里存着固定前缀 / 触发词那一截，'
      +'其余由「'+who+'」在生成的那一刻算出来，ComfyUI 只把结果送进采样器，不进 PNG。'
    : '<div class="mnote"><b>正向提示词没有随图片保存</b>：它由「'+who
      +'」在生成的那一刻算出来，ComfyUI 只把结果送进采样器，不进 PNG。';
  if(P.positive_cache){
    h += P.positive_partial
      ? '下面这段是该节点<strong>上一次运行</strong>留下的缓存（前缀与本图一致，正文仍是上一张的），'
        +'只能当参考。</div>'
      : '下面这段是该节点<strong>上一次运行</strong>留下的缓存，可能与本图无关。</div>';
  }else if(P.positive_file){
    /* 有 source 文件 = 这一类是「从清单里挑一条」的节点：连缓存都没有，
       只能告诉用户「挑的是哪一句，文件里没记」—— 不能再往深里编。 */
    h += '这个节点每次从 <code>'+esc(P.positive_file)+'</code> 里挑一句，'
      + '挑中的那句图片里没有记录。</div>';
  }else{
    h += '</div>';
  }
  if(P.positive_cache){
    /* 折叠不用 CSS 裁高度 —— 实测 `.ptxt` 的 max-height 与 pre 的实际行距对不上
       （收出来正好露第 7 行的上半截，半截字），改成**按字符截断**：
       预览 200 字 + 省略号，点「展开」由统一的 .mfbtn 处理器换成全文（见 renderMeta 末尾）。
       693 字的标签必须默认折叠，否则整屏都是它，负向与参数被挤走。 */
    const full = P.positive_cache, prev = full.length > 200 ? full.slice(0, 200) + "…" : full;
    const long = metaTextLines(full) > META_FOLD_LINES;
    h += '<div class="mfold'+(long?"":" short")+' mcache">'
      + '<div class="mfhd"><span>节点缓存（上次运行，仅供参考）</span>'
      + (long?'<button type="button" class="mfbtn" data-fold="1">展开</button>':"")
      + '</div>'
      + '<pre class="ptxt" data-full="'+esc(full)+'" data-prev="'+esc(prev)+'">'+esc(prev)+'</pre>'
      + '<div class="mfact"><a href="javascript:void(0)" class="cp" data-cp="'+esc(full)+'">复制缓存标签</a></div></div>';
  }
  return h;
}

/* V8 · 三层卡片：① 总览（定义列表左右对齐）② 生成参数（+ 长提示词折叠）③ 原始数据（<details>）。 */
function renderMeta(j,it){
  const m=j.meta||{}, P=j.parsed; let h="";
  /* ① 总览 —— 原来是一行「A ｜ B ｜ C」流水，现在拆成两列对齐 */
  const info=[["修改时间",fmtTime(it.mtime),""],["大小",fmtSize(it.size),""]];
  if(it.type==="image"&&m.IHDR) info.push(["尺寸",m.IHDR,""]);
  if(it.type==="video") info.push(["视频",S.vidInfo||"读取视频信息…","vidinfo"]);
  h+='<section class="mcard"><div class="k mh">总览</div><dl class="mdl">'
    +info.map(r=>'<div class="mrow"><dt>'+esc(r[0])+'</dt><dd'+(r[2]?' class="'+r[2]+'"':"")
      +'>'+esc(r[1])+'</dd></div>').join("")
    +'</dl></section>';
  /* ② 生成参数 */
  if(P){
    const rows=[];
    /* 2026-11-03：多段采样流（先底模出图、再换模型精修/放大）要把每段模型都显示出来。
       单模型图：**一个像素都不变** —— 仍是字段表里的「模型」行（全名 + hash + 点击复制）。
       多段图：模型升格为独立区块（metaModelBox），采样参数差异用表格承载（metaSampTable）。
       数据：parsed.models = [{name, stage, ...}]，**后端只在 ≥2 段时才写** ⇒ 单模型图这里
       拿到的是 undefined，走的分支与旧版完全一致。 */
    const MDLS=P.models||[];
    const STAGES=MDLS.length>1?MDLS:null;
    /* 出不出采样表，三条：① 至少两段读到了参数（否则没得比）；
       ② 每段都读到了 ⇒ 四者签名全等就**不出表**（沿用字段表那四行，外观不变）；
       ③ 只要有一段读不到 ⇒ 出表（缺的显示 —），因为「不显示」会被读成「和第一段一样」。 */
    let needTable=false;
    if(STAGES){
      const withP=STAGES.filter(metaStageHas);
      needTable = withP.length>=2
        && (withP.length<STAGES.length || new Set(STAGES.map(metaStageSig)).size>1);
    }
    if(P.model && !STAGES){
      const full=P.model+(P.model_hash?` (${P.model_hash})`:"");
      rows.push(["模型", full,
        '<span class="cpv" data-tip="点击复制" data-cp="'+esc(full)+'">'+esc(full)+'</span>']);
    }
    /* 出图尺寸：`size` 是潜变量源头的基础尺寸（= 写回 EmptyLatentImage 的值），
       `size_out` 只在链路上真的挂了放大节点时才有。两者不同就把成品标出来 ——
       只显示基础尺寸会让「1536×864 的图」看着像 1536×864，实际 PNG 是 2304×1296。 */
    if(P.size) rows.push(["出图尺寸",P.size+(P.size_out&&P.size_out!==P.size?" → "+P.size_out:"")]);
    /* 出表时这四项由表格按段承载 —— 留着会出现「两套互相矛盾的值」。 */
    if(!needTable){
      if(P.sampler) rows.push(["采样器",P.sampler+(P.scheduler?` / ${P.scheduler}`:"")]);
      if(P.steps!=null) rows.push(["步数",P.steps]);
      if(P.cfg!=null) rows.push(["CFG",P.cfg]);
      if(P.seed!=null) rows.push(["Seed",P.seed]);
    }
    if(P.wfk) rows.push(["工作流",({t2i:"文生图",i2i:"图生图",cn:"ControlNet"}[P.wfk]||P.wfk)]);
    /* V47（2026-10-08）：LoRA **不再**拼成单行塞进字段表。
       原来把 loras 用逗号拼成一个字符串值 —— 实测 4 个 LoRA 拼出 179 字符，
       而面板内容宽仅 ~300px（#side 340px 减内边距），换行成一坨且权重淹没在文本里；
       ⚠️ 本注释**刻意不写出那个被删掉的拼接表达式**：它含 join/comma 字样，
          留在注释里会让「源码里不得再拼单行」这类子串断言**假命中**
          （本项目「注释冒充代码」的老坑，M1 门禁已因此误报过一轮）。
       更根本的是它走 .mdl 网格（左 70px 字段名），LoRA 被当**单个值**渲染，天然分行不了。
       改法：升格为独立区块（metaLoras），一条一行 + 权重右锚徽章（见 viewer.css .lorabox 组）。
       ⚠️ 数据形态：parsed.loras 是 list[str]，元素形如 "名字.safetensors (0.8)" ——
          权重是**尾部括号里的文本**，不是结构化字段（server.py 的 _strength_fmt 已拍平）。
          所以这里必须正则回拆，而不是改后端（后端格式被索引/搜索/导出多处依赖，不动）。
       ⚠️ 正则用**非贪婪 + 锚定结尾**：LoRA 名字本身可能含括号（如 "foo (v2).safetensors (0.8)"），
          贪婪会把名字里的括号吃掉；`^(.*?)\s*\(([\d.]+)\)$` 只认**最后一组纯数字括号**，
          且 `([\d.]+)` 限定数字/点，名字里的 "(v2)" 不会误判成权重。 */
    const loraBox = (P.loras&&P.loras.length) ? metaLoras(P.loras) : "";
    let body = rows.length
      ? '<dl class="mdl">'+rows.map(r=>'<div class="mrow"><dt>'+esc(r[0])+'</dt><dd>'
          +(r[2]!=null ? r[2]
            : '<span class="cpv" data-tip="点击复制" data-cp="'+esc(r[1])+'">'+esc(r[1])+'</span>')
          +'</dd></div>').join("")+'</dl>'
      : "";
    /* 模型区块 / 采样表排在字段之后、提示词之前 —— 与定稿时的实物一致
       （出图尺寸在上，模型与采样链路两块相邻，靠段号上下对应）。 */
    if(STAGES) body += metaModelBox(STAGES, P.model_hash);
    if(STAGES && needTable) body += metaSampTable(STAGES);
    /* 2026-11-07：正向为空但链路是打标/反推节点 ⇒ 换成「为什么读不到」的说明块
       （有缓存则附一块明确标注的缓存）；两者都没有时 metaRuntimePrompt 返回空串，
       与旧行为完全一致。
       2026-11-08：正向非空但只读到一部分（positive_partial）时，提示块跟在正文后面
       —— 只显示那半截而不解释，用户会当成全部；标题也标「（不完整）」，免得
       「复制正向提示词」被当成本图配方拿去重跑。 */
    const _pTitle = P.positive_partial ? "正向提示词（不完整）" : "正向提示词";
    body += (P.positive
        ? metaFold(_pTitle,P.positive,1)+metaRuntimePrompt(P)
        : metaRuntimePrompt(P))
      + metaFold("负向提示词",P.negative) + loraBox;
    if(body) h+='<section class="mcard"><div class="k mh">生成参数'
      +(P.source?' · '+esc(P.source):"")+'</div>'+body+'</section>';
  }
  /* ③ 原始数据 */
  let raw="";
  const rawKeys=["prompt","workflow"];
  for(const k of rawKeys){
    if(m[k]==null) continue;
    let pretty=m[k];
    try{ pretty=JSON.stringify(JSON.parse(m[k]),null,2); }catch(e){}
    raw+='<details><summary>'+k+'（原始 JSON）</summary><pre>'+esc(pretty)+'</pre></details>';
  }
  if(m.parameters) raw+='<details><summary>parameters（原始文本）</summary><pre>'+esc(m.parameters)+'</pre></details>';
  for(const k in m){ if(rawKeys.includes(k)||k==="IHDR"||k==="parameters") continue;
    raw+='<div class="k">'+esc(k)+'</div><pre>'+esc(String(m[k]).slice(0,2000))+'</pre>'; }
  if(raw) h+='<section class="mcard"><div class="k mh">原始数据</div>'+raw+'</section>';
  /* 用 += 而非 =：总览是任何文件都该看到的，不能被一句空态文案冲掉。
     E5-7（2026-09-23）：**「读失败」与「本来就没有」必须分开说** —— 此前两种情形都打
     「没有内嵌生成信息」，把「我们读不到」讲成了「这张图本来就没有配方」（与事实相反）。
     服务端在 png_meta 读失败时登记旁路状态，API 回带 metaError；这里据此换一句话。 */
  /* 2026-11-08：空态从一行灰字（.row，与「大小 / 修改时间」同级）升格为 .mnote 黄框。
     理由：这一句是「这张图没有配方」—— 属于**结论**而不是一条元信息，平铺在总览下面
     会被当成普通字段略过；同一套 .mnote 已在「提示词读不出来的四种成因」里用熟，
     颜色 / 左边线 / 圆角都走既有令牌，不新增视觉语言。读失败那条同步升级为红框（.bad），
     保持「同样是读不到」看起来是同一类东西。 */
  if(!hasGenMeta(j)) h+= (j && j.metaError)
    ? '<div class="mnote bad"><b>元数据读取失败</b><span class="sub">'+esc(j.metaError)
      +' —— 文件可能正被其它程序占用，稍后可重试</span></div>'
    : '<div class="mnote"><b>这张图没有内嵌生成信息</b>'
      +'<span class="sub">提示词 / 模型 / 参数都不在文件里 —— 截图、二次导出或压缩转发会把它们剥掉</span></div>';
  meta.innerHTML=h;
  bindLoraLinks(meta, P);
  /* 折叠按钮：状态写回全局记忆（切图沿用） */
  meta.querySelectorAll(".mfbtn").forEach(b=>b.onclick=()=>{
    const box=b.closest(".mfold"); if(!box) return;
    const on=box.classList.toggle("open"); META_FOLD=on;
    b.textContent=on?"收起":"展开";
    /* 2026-11-07：缓存块是**按字符**折叠的（不靠 max-height），点开要把预览换成全文。
       别的折叠块没有 data-full，这里取到 null 就跳过 —— 原行为一个字节都不变。 */
    const pre=box.querySelector("pre[data-full]");
    if(pre) pre.textContent = on ? pre.dataset.full : pre.dataset.prev;
  });
  $("#btnOpen").style.display=m.prompt?"":"none";
  $("#btnDlWF").style.display=m.prompt?"":"none";
  // 「复制 prompt」复制的是解析后的正向提示词 → 没有它就别显示（避免给出会报错的死按钮）。
  const _pos=((P&&P.positive)||"").trim();
  $("#btnCopy").dataset.cp=_pos;
  setIcoLabel($("#btnCopy"), "copy", "复制提示词");   /* 批次 B：图标槽从 ICO 取，取代旧的字符勾 */
  /* 2026-10-31：显隐不再单看「有没有提示词」，还要看详情面板收没收起
     （面板展开时右侧已有「复制正向提示词」）。mvLbSyncCopy 内部顺带重算顶栏收纳 ——
     上面刚改过 #btnOpen / #btnDlWF 的 display，多算一次正好把「⋯」同步过来。 */
  mvLbSyncCopy();
  /* V14：两个复制入口都走 copyText，成功后由它给触发元素一次成功色微反馈 */
  meta.querySelectorAll("a.cp").forEach(a=>a.onclick=()=>copyText(a.dataset.cp, a));
  meta.querySelectorAll(".cpv").forEach(el=>el.onclick=()=>copyText(el.dataset.cp, el));  // 参数逐项点击复制
}

/* ---------------- 缩放：滚轮缩放 / 拖拽平移 / 双击 100% ↔ 适应 ---------------- */
/* 所有可缩放元素共享一套 window 级拖拽调度：
   原先每个 bind 各自覆写 window.onmousemove/onmouseup，大图页与对比页互相覆盖，
   且元素销毁后处理器残留。改为注册表 + 自动清理断连元素。 */
const _zoomDrags=new Map();   // img -> {move(e), up(e)}
function _dispatchDrags(kind,e){
  for(const [img,d] of [..._zoomDrags]){
    if(!img.isConnected){ _zoomDrags.delete(img); continue; }
    if(kind==="move") d.move(e); else d.up(e);
  }
}
window.addEventListener("mousemove",e=>_dispatchDrags("move",e));
window.addEventListener("mouseup",e=>_dispatchDrags("up",e));
/* F6-P2-3（2026-10-17）：触屏 / 触控笔。
   原来这里只有 mousemove/mouseup —— 而 CSS 已给 `#stage img` 设了 `touch-action:none`，
   等于把浏览器兜底的平移与捏合也一并禁掉，触屏设备上放大后**既不能平移也不能缩放**。
   现在鼠标仍走 mousedown 那条老路（行为零变化），触屏走 pointerdown/pointermove/pointerup。
   两条路对同一目标的位移都是「按起点绝对定位」，不做增量累加 ⇒ 即便浏览器补发兼容鼠标事件，
   重复调用也是幂等的，不会叠加。 */
window.addEventListener("pointermove",e=>{ if(e.pointerType!=="mouse") _dispatchDrags("move",e); });
window.addEventListener("pointerup",e=>{ if(e.pointerType!=="mouse") _dispatchDrags("up",e); });
window.addEventListener("pointercancel",e=>{ if(e.pointerType!=="mouse") _dispatchDrags("up",e); });

/* F6-P2-2（2026-10-17）：拖拽平移松手时会误关大图。
   触发链：`mousedown` 在 <img>、拖到图外后 `mouseup` 落在 `#stage` ⇒ 浏览器把 `click`
   派发到两者**最近的共同祖先** `#stage`，正好命中 nav.js 的「点空白关大图」判据
   ⇒ 用户拖着拖着图就没了，缩放与位置全丢。`e.preventDefault()` 只挡默认行为，不挡 click。
   规则：按下到松手位移 > 3px 就吞掉紧随的那一次 click（捕获阶段，抢在 nav.js 的处理器之前）。
   用**时间戳**而不是定时器清标志：click 与 mouseup/pointerup 在同一个任务里派发，
   而 `setTimeout(0)` 在后台/无头环境会被节流到几百毫秒，标志会多活一会儿、
   把用户的**下一次**真实点击也吞掉。时间戳方案不依赖定时器：超过 500ms 的 click 直接放行。 */
let _swallowClickAt=0;
window.addEventListener("click",e=>{
  if(!_swallowClickAt) return;
  const dt=performance.now()-_swallowClickAt;
  _swallowClickAt=0;                        // 一次性：无论放行还是吞掉都清
  if(dt>500) return;                        // 已经不是这次拖拽的尾巴了
  e.stopPropagation(); e.preventDefault();
}, true);
function _armSwallowClick(){ _swallowClickAt=performance.now(); }
const zoom={s:1,tx:0,ty:0};
let zoomFit=4;
/* 舞台内边距：`#stage` 有 padding，而 `clientWidth` 是**含 padding** 的。
   夹取平移边界时要减掉它，否则图会露出一截内边距，看着像「没贴边」。 */
let _stagePadX=0,_stagePadY=0;
function _measureStagePad(){
  try{
    const cs=getComputedStyle(stage);
    _stagePadX=(parseFloat(cs.paddingLeft)||0)+(parseFloat(cs.paddingRight)||0);
    _stagePadY=(parseFloat(cs.paddingTop)||0)+(parseFloat(cs.paddingBottom)||0);
  }catch(_){ _stagePadX=0; _stagePadY=0; }
}
function bindZoom(img){
  zoom.s=1; zoom.tx=0; zoom.ty=0; zoomFit=4;
  _measureStagePad();
  const box=img.parentElement||stage;
  img.classList.remove("grabbing");
  /* F6-P2-4（2026-10-17）：`zoomFit`（100% 像素所需的倍率）原来只在 bind 这一刻算一次 ——
     拖窗口改尺寸、按 P 开合详情面板、旋转屏幕之后舞台宽度变了，`#zoomBadge` 的百分比
     与「双击 ⇒ 100%」就都失真，放大上限 `Math.max(8,zoomFit)` 也还是老值。
     改成每次重排都按当前**布局**宽度重算：用 offsetWidth，不用 getBoundingClientRect
     （后者拿到的是变换后的盒，图一放大就跟着变，算出来恒等于当前倍率）。 */
  const refit=()=>{ const w=img.offsetWidth||1; if(img.naturalWidth) zoomFit=Math.max(1,img.naturalWidth/w); };
  /* O10（2026-10-28）：一次滚轮事件原本要读 **5 次布局** —— refit 的 offsetWidth、
     clampT 的 clientWidth/clientHeight/offsetWidth/offsetHeight、zoomBy 的 rect。
     更要命的是顺序：上一事件末尾刚给 #zoomBadge 赋过 textContent（布局失效），
     下一事件一读 offsetWidth 就先**强制一次同步布局** ⇒ 连续滚轮 = 连续同步重排。
     改法（**同步语义不变**，只是少读）：
       ① 布局尺寸只在 bind 与 ResizeObserver 里量：transform/徽标文字都不影响布局，
          所以两次尺寸变化之间 `--vw/--vh/--iw/--ih` 是常量，摊到每次事件里读纯属浪费；
       ② 徽标节点缓存 + 文本没变就不赋值（textContent 赋值会让布局失效，这是那一环的源头）；
       ③ apply 里不再调 refit()：尺寸变化只有 ResizeObserver 那一条路，
          在每次 apply 里重算 zoomFit 是重复劳动（且它读的就是那个会触发重排的 offsetWidth）。
     ⚠️ 别顺手把 apply 改成 rAF 合帧：`zoom.s` 与 transform 的读写目前是**同步可见**的，
        指针拖拽的「按过没按过」判定（drag.moved → 吞 click）就靠这个时序。 */
  let _vw=1,_vh=1,_iw=1,_ih=1;
  const measureBox=()=>{
    _vw=Math.max(1,(box.clientWidth||1)-_stagePadX);
    _vh=Math.max(1,(box.clientHeight||1)-_stagePadY);
    _iw=img.offsetWidth||1; _ih=img.offsetHeight||1;
  };
  measureBox();
  /* apply 不再算 zoomFit ⇒ 必须在这里先算一次（bindZoom 由 img.onload 调用，
     此时 naturalWidth 已可用）。之后只有 ResizeObserver 会改它。 */
  refit();
  /* 平移边界。原来 tx/ty 完全不夹取 ⇒ 图能被一路拖出视口再也拖不回来（只能双击复位）。
     内容比视口小时强制居中；比视口大时最多拖到边缘贴齐。 */
  const clampT=()=>{
    const w=_iw*zoom.s, h=_ih*zoom.s;
    const mx=Math.max(0,(w-_vw)/2), my=Math.max(0,(h-_vh)/2);
    zoom.tx=Math.min(mx,Math.max(-mx,zoom.tx));
    zoom.ty=Math.min(my,Math.max(-my,zoom.ty));
  };
  const zb=$("#zoomBadge");
  let _zbTxt=null;
  const apply=()=>{
    clampT();
    img.style.transform=`translate(${zoom.tx}px,${zoom.ty}px) scale(${zoom.s})`;
    if(zb){
      if(zoom.s>1.001){
        const t=Math.round(100*zoom.s/zoomFit)+"%";
        if(_zbTxt!==t){ zb.textContent=t; _zbTxt=t; }
        if(zb.style.display!=="block") zb.style.display="block";
      }else{
        if(zb.style.display!=="none") zb.style.display="none";
      }
    }
  };
  const center=()=>{ const r=img.getBoundingClientRect(); return {x:r.left+r.width/2, y:r.top+r.height/2}; };
  const zoomBy=(ratio,mx,my)=>{          // 以 (mx,my) 为锚做一次相对缩放（原 onwheel 公式的等价抽取）
    const c=center();
    zoom.tx=(mx-c.x)*(1-ratio)+zoom.tx*ratio;
    zoom.ty=(my-c.y)*(1-ratio)+zoom.ty*ratio;
  };
  img.onwheel=e=>{
    e.preventDefault();
    const ns=Math.min(Math.max(8,zoomFit), Math.max(1, zoom.s*(e.deltaY<0?1.2:1/1.2)));
    zoomBy(ns/zoom.s, e.clientX, e.clientY);
    zoom.s=ns; apply();
  };
  let drag=null;
  const startDrag=(x,y)=>{
    if(!(zoom.s>1.001)) return false;
    drag={x,y,tx:zoom.tx,ty:zoom.ty,moved:false};
    img.classList.add("grabbing");
    return true;
  };
  img.onmousedown=e=>{ if(startDrag(e.clientX,e.clientY)) e.preventDefault(); };
  /* F6-P2-3：单指平移 + 双指捏合（以两指中点为锚）。鼠标不走这里（pointerType==="mouse" 直接返回），
     仍由上面的 onmousedown 处理 —— 老路径零改动。 */
  const pts=new Map();      // pointerId -> {x,y}（只放非鼠标触点）
  let pinch=null;           // {d0,s0}：捏合起点的两指距离与倍率
  /* 2026-11-07：单指横滑翻页。手机上原来只能点左右两个小箭头，实际很难按准。
     只在 zoom.s≈1（没放大）时生效 —— 放大后横滑是「平移看细节」，抢过来会很难用。 */
  let swipe=null;           // {x,y,t}：单指按下时的起点与时刻
  const pdist=()=>{ const a=[...pts.values()]; return Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y)||1; };
  const pmid=()=>{ const a=[...pts.values()]; return {x:(a[0].x+a[1].x)/2, y:(a[0].y+a[1].y)/2}; };
  img.onpointerdown=e=>{
    if(e.pointerType==="mouse") return;
    try{ img.setPointerCapture(e.pointerId); }catch(_){}
    pts.set(e.pointerId,{x:e.clientX,y:e.clientY});
    if(pts.size>=2){ pinch={d0:pdist(), s0:zoom.s}; drag=null; swipe=null; img.classList.add("grabbing"); }
    else { startDrag(e.clientX,e.clientY); swipe={x:e.clientX,y:e.clientY,t:Date.now()}; }
    e.preventDefault();
  };
  _zoomDrags.set(img, {
    move:e=>{
      if(e && e.pointerId!=null && e.pointerType!=="mouse"){
        if(!pts.has(e.pointerId)) return;                 // 不是落在本元素上的触点
        pts.set(e.pointerId,{x:e.clientX,y:e.clientY});
        if(pinch && pts.size>=2){
          const ns=Math.min(Math.max(8,zoomFit), Math.max(1, pinch.s0*pdist()/pinch.d0));
          const m=pmid();
          zoomBy(ns/zoom.s, m.x, m.y);
          zoom.s=ns; apply();
          return;
        }
      }
      if(!drag) return;
      const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
      if(!drag.moved && Math.abs(dx)+Math.abs(dy)>3) drag.moved=true;   // 3px 阈值：手抖不算拖拽
      zoom.tx=drag.tx+dx; zoom.ty=drag.ty+dy;
      img.style.transform=`translate(${zoom.tx}px,${zoom.ty}px) scale(${zoom.s})`;
    },
    up:e=>{
      /* 横滑翻页判定放在最前面：它要在 pts 被删掉之前读到「刚才只有一指」。 */
      if(e && e.pointerId!=null && e.pointerType!=="mouse" && swipe && pts.size===1
         && zoom.s<=1.05){
        const dx=e.clientX-swipe.x, dy=e.clientY-swipe.y;
        if(Date.now()-swipe.t<=800 && Math.abs(dx)>=70 && Math.abs(dx)>Math.abs(dy)*1.4){
          swipe=null; drag=null;
          zoom.tx=0; zoom.ty=0; apply();          // 撤掉这一下带出来的平移
          if(typeof window.mvStep==="function") window.mvStep(dx<0 ? 1 : -1);
          if(pts.has(e.pointerId)) pts.delete(e.pointerId);
          if(pts.size===0) pinch=null;
          img.classList.remove("grabbing");
          return;
        }
      }
      if(e && e.pointerId!=null && e.pointerType!=="mouse" && pts.has(e.pointerId)){
        pts.delete(e.pointerId);
        if(pts.size<2) pinch=null;
        if(pts.size===1){                                  // 松开一指后，剩下那指从当前位继续拖，不跳变
          const p=[...pts.values()][0];
          drag = zoom.s>1.001 ? {x:p.x,y:p.y,tx:zoom.tx,ty:zoom.ty,moved:true} : null;
        }
      }
      if(drag){
        if(drag.moved){ _armSwallowClick(); apply(); }      // 拖过 ⇒ 吞掉紧随的 click，并把位移夹回边界
        drag=null; img.classList.remove("grabbing");
      }
      if(pts.size===0) pinch=null;
    }
  });
  img.ondblclick=()=>{
    if(zoom.s>1.05){ zoom.s=1; zoom.tx=0; zoom.ty=0; }
    else{ zoom.s=zoomFit; zoom.tx=0; zoom.ty=0; }
    apply();
  };
  /* F6-P2-4：尺寸一变就重算。观察 img 自身（它受 max-width/max-height 约束，舞台宽度变化时它跟着变）；
     transform 不改变布局尺寸，所以不会自激。元素断连后自行解绑。
     O10：这里是**唯一**重新测量布局尺寸的地方（apply 不再读 offsetWidth/clientWidth）。 */
  if(typeof ResizeObserver==="function"){
    let ro=null;
    ro=new ResizeObserver(()=>{
      if(!img.isConnected){ try{ ro.disconnect(); }catch(_){} return; }
      _measureStagePad();          // 舞台内边距也会随预览栏/详情面板开合变化
      measureBox();
      /* O10 补正（2026-10-28，`p1fix_nav_p2` B4a 实测抓出）：`refit()` 必须**每次都跑**。
         `zoomFit` 是徽标百分比（`zoom.s/zoomFit`）与「双击回 100%」的基准，
         原先它挂在 `apply()` 里顺带重算，现在 apply 不读布局了 ⇒ 这个 if/else 只剩
         「放大时才 apply」会让缩窗后徽标**停在旧百分比**（实测 149% 前后一模一样）。
         尺寸变化这条路径读一次 offsetWidth 是应该的（本来就变了），滚轮路径才不许读。 */
      refit();
      if(zoom.s>1.001) apply();
    });
    try{ ro.observe(img); }catch(_){}
  }
}

/* ---------------- 并排对比（批量模式下 2-4 张）---------------- */
function bindCmpZoom(img){
  const st={s:1,tx:0,ty:0};
  const fitW=img.getBoundingClientRect().width||1;
  const fit=img.naturalWidth? Math.max(1,img.naturalWidth/fitW):4;
  const apply=()=>{ img.style.transform=`translate(${st.tx}px,${st.ty}px) scale(${st.s})`; };
  img.onwheel=e=>{ e.preventDefault();
    const r=img.getBoundingClientRect();
    const dx=e.clientX-(r.left+r.width/2), dy=e.clientY-(r.top+r.height/2);
    const ns=Math.min(Math.max(8,fit), Math.max(1, st.s*(e.deltaY<0?1.2:1/1.2)));
    st.tx=dx*(1-ns/st.s)+st.tx*ns/st.s; st.ty=dy*(1-ns/st.s)+st.ty*ns/st.s; st.s=ns; apply(); };
  let drag=null;
  img.onmousedown=e=>{ if(st.s>1.001){ drag={x:e.clientX,y:e.clientY,tx:st.tx,ty:st.ty};
    img.classList.add("grabbing"); e.preventDefault(); } };
  _zoomDrags.set(img, {
    move:e=>{ if(!drag) return;
      st.tx=drag.tx+(e.clientX-drag.x); st.ty=drag.ty+(e.clientY-drag.y); apply(); },
    up:()=>{ if(drag){ drag=null; img.classList.remove("grabbing"); } }
  });
  img.ondblclick=()=>{ st.s=st.s>1.05?1:fit; st.tx=0; st.ty=0; apply(); };
}
function openCmp(){
  const paths=[...batchSel];
  if(paths.length<2||paths.length>4){ toast("并排对比需选中 2-4 张（当前 "+paths.length+" 张）","err"); return; }
  exitWipe();                                   // 若残留滑块模式，先复位回并排
  const g=$("#cmpGrid"); g.innerHTML="";
  $("#cmpN").textContent=`对比 ${paths.length} 张 · 每格：滚轮缩放 / 拖拽 / 双击 100%↔适应`;
  g.style.gridTemplateColumns = paths.length===3 ? "repeat(3,1fr)" : "repeat(2,1fr)";
  for(const p of paths){
    const cell=document.createElement("div"); cell.className="cp";
    const im=new Image(); im.src=fileURL(p);   // 带版本号：刚被替换过的图不拿旧缓存
    im.onload=()=>bindCmpZoom(im);
    const cap=document.createElement("div"); cap.className="cpc";
    cap.textContent=p.split(/[\\/]/).pop();
    cell.append(im,cap); g.appendChild(cell);
  }
  $("#cmpWipe").style.display = paths.length===2 ? "" : "none";   // 恰好 2 张才提供滑块模式
  if(!$("#cmp").classList.contains("on")){
    cmpFocusBack=(document.activeElement && document.activeElement!==document.body)
      ? document.activeElement : null;
  }
  $("#cmp").classList.add("on");
  { const x=$("#cmpExit"); if(x && !document.getElementById("cmp").contains(document.activeElement)) x.focus(); }
}
let cmpFocusBack=null;   // G2·V16③：对比层的触发点
function closeCmp(){ exitWipe(); $("#cmp").classList.remove("on"); $("#cmpGrid").innerHTML="";
  /* 2026-11-05 P2：对比层里的 img 注册过拖拽处理器（`_zoomDrags`），closeCmp 把 DOM
     清掉后这些 img 已断连，但 Map 仍持有它们直到下一次 mousemove 才被 `_dispatchDrags`
     顺手删。这里显式清一遍，别把已断连的元素留在闭包表里。 */
  for(const img of [..._zoomDrags.keys()]) if(!img.isConnected) _zoomDrags.delete(img);
  if(window.mvSyncInert) window.mvSyncInert();             // 先解冻再归还（同 #lb）
  if(cmpFocusBack && cmpFocusBack.isConnected && cmpFocusBack.focus) cmpFocusBack.focus();
  cmpFocusBack=null; }

/* ---------------- 滑块擦除对比（2 张图重叠，拖分隔线看细节差异） ---------------- */
const wipe={a:null,b:null,x:50,z:{s:1,tx:0,ty:0,fit:4},hDrag:false};
function applyWipe(){
  const t=`translate(${wipe.z.tx}px,${wipe.z.ty}px) scale(${wipe.z.s})`;
  $("#wipeA").style.transform=t;
  $("#wipeB").style.transform=t;
  /* 裁切写在**不参与 transform** 的 #wipeBClip 上，不写在被 scale 的 #wipeB 上：
     clip-path 按元素本地坐标裁、裁完再整体过 transform ⇒ 挂在图片上时那条裁切边会随
     缩放一起平移放大，和按 stage 百分比定死的 #wipeHandle 错位（用户 2026-10-09 报的
     「放大后滑块和画面里的分割线对不上」）。下移到裁切层后两者共用 stage 宽这个基宽，恒重合。 */
  $("#wipeBClip").style.clipPath=`inset(0 0 0 ${wipe.x}%)`;   // B 只露出分隔线右侧
  $("#wipeHandle").style.left=wipe.x+"%";
}
function wipeFit(){
  const A=$("#wipeA"), w=$("#wipeStage").clientWidth||1;
  if(A.naturalWidth) wipe.z.fit=Math.max(1,A.naturalWidth/w);
}
function openWipe(pa, pb){
  const A=$("#wipeA"), B=$("#wipeB");
  $("#cmpGrid").style.display="none";
  $("#wipeWrap").classList.add("on");
  setIcoLabel($("#cmpWipe"), "gridTable", "并排模式");
  wipe.a=pa; wipe.b=pb; wipe.x=50; wipe.z={s:1,tx:0,ty:0,fit:4};
  A.classList.add("draggable"); B.classList.add("draggable");
  A.src=fileURL(pa);
  B.src=fileURL(pb);
  A.onload=B.onload=()=>{ wipeFit(); if(wipe.z.s<=1.001) applyWipe(); };
  $("#wipeLblA").textContent=pa.split(/[\\/]/).pop();
  $("#wipeLblB").textContent=pb.split(/[\\/]/).pop();
  $("#cmpN").textContent=`滑块对比：左 = ${$("#wipeLblA").textContent} ｜ 右 = ${$("#wipeLblB").textContent}`;
  applyWipe();
}
function exitWipe(){
  if(!wipe.a) return;
  wipe.a=wipe.b=null; wipe.hDrag=false;
  const A=$("#wipeA"), B=$("#wipeB");
  A.removeAttribute("src"); B.removeAttribute("src");   // 释放原图请求/内存
  A.style.transform=""; B.style.transform="";
  $("#wipeBClip").style.clipPath="";                    // 裁切层也要复位，否则残留一条裁切窗口
  A.classList.remove("draggable","grabbing"); B.classList.remove("draggable","grabbing");
  $("#cmpGrid").style.display="";
  $("#wipeWrap").classList.remove("on");
  setIcoLabel($("#cmpWipe"), "swap", "滑块模式");
}
$("#cmpWipe").onclick=()=>{
  if(wipe.a){                                   // 滑块 → 并排
    exitWipe();
    $("#cmpN").textContent=`对比 ${[...batchSel].length} 张 · 每格：滚轮缩放 / 拖拽 / 双击 100%↔适应`;
    return;
  }
  const paths=[...batchSel];
  if(paths.length!==2){ toast("滑块对比需要正好选中 2 张","err"); return; }
  openWipe(paths[0], paths[1]);
};

const wStage=$("#wipeStage");
$("#wipeHandle").onmousedown=e=>{ wipe.hDrag=true; e.preventDefault(); e.stopPropagation(); };
window.addEventListener("mousemove",e=>{          // 拖分隔线（handle 之外继续跟随）
  if(!wipe.hDrag) return;
  const r=wStage.getBoundingClientRect();
  wipe.x=Math.max(0, Math.min(100, (e.clientX-r.left)/r.width*100));
  applyWipe();
});
window.addEventListener("mouseup",()=>wipe.hDrag=false);
wStage.onwheel=e=>{
  if(!wipe.a) return;
  e.preventDefault();
  const z=wipe.z, r=wStage.getBoundingClientRect();
  const dx=e.clientX-(r.left+r.width/2), dy=e.clientY-(r.top+r.height/2);
  const ns=Math.min(Math.max(8,z.fit), Math.max(1, z.s*(e.deltaY<0?1.2:1/1.2)));
  z.tx=dx*(1-ns/z.s)+z.tx*ns/z.s; z.ty=dy*(1-ns/z.s)+z.ty*ns/z.s; z.s=ns;
  applyWipe();
};
let wDrag=null;                                    // 放大后拖拽平移（两图同步）
for(const im of [$("#wipeA"), $("#wipeB")]){
  im.onmousedown=e=>{
    if(!wipe.a || wipe.z.s<=1.001) return;
    wDrag={x:e.clientX,y:e.clientY,tx:wipe.z.tx,ty:wipe.z.ty};
    $("#wipeA").classList.add("grabbing"); $("#wipeB").classList.add("grabbing");
    e.preventDefault();
  };
}
_zoomDrags.set(wStage,{                           // 复用共享拖拽调度，断连自动清理
  move:e=>{ if(!wDrag) return;
    wipe.z.tx=wDrag.tx+(e.clientX-wDrag.x); wipe.z.ty=wDrag.ty+(e.clientY-wDrag.y);
    applyWipe(); },
  up:()=>{ if(!wDrag) return; wDrag=null;
    $("#wipeA").classList.remove("grabbing"); $("#wipeB").classList.remove("grabbing"); }
});
wStage.ondblclick=()=>{
  if(!wipe.a) return;
  if(wipe.z.s>1.05) wipe.z={s:1,tx:0,ty:0,fit:wipe.z.fit};
  else{ wipeFit(); wipe.z={s:wipe.z.fit,tx:0,ty:0,fit:wipe.z.fit}; }
  applyWipe();
};
document.addEventListener("keydown",e=>{           // ←/→ 微调分隔线（Shift 大步进）
  if(!wipe.a || !$("#cmp").classList.contains("on")) return;
  const tag=e.target.tagName;
  if(tag==="INPUT"||tag==="SELECT"||tag==="TEXTAREA") return;
  if(e.key==="ArrowLeft"||e.key==="ArrowRight"){
    e.preventDefault();
    wipe.x=Math.max(0, Math.min(100, wipe.x+(e.key==="ArrowRight"?1:-1)*(e.shiftKey?10:2)));
    applyWipe();
  }
});


/* ---------------- 单图查看：打开库外图片（V2.10.7） ----------------
   用途：别人发来一张原图，想直接看它的提示词 / 配方，不必先把图拷进 output 目录。
   流程：选文件 -> POST /api/solo（后端按内容哈希落进数据目录的 solo/）-> 拿到绝对
   路径 -> 用「只有这一张」的 S.solo 打开大图面板，其余一切照旧。

   为什么不直接用 URL.createObjectURL(file) 把本地文件喂给 <img>：
   提示词解析、下载、导出工作流、送 ComfyUI 全都按**路径**走后端，blob: URL 在后端
   根本不存在，那一整条能力得重写一遍；而且刷新一次 blob 就失效。落盘换来的代价只是
   数据目录里多一个可清理的暂存区（设置 · 数据页有「清除单图暂存」和占用显示）。
   ★ 转发的图读不到提示词：微信 / QQ 不勾「原图」会重新编码，PNG 的 tEXt 块就没了 ——
   这时面板照常打开，落成一句话说明 + 一条解释原因的 toast（soloNoMetaToast）。 */
const SOLO_MAX = 64 * 1024 * 1024;      // 与服务端 SOLO_MAX_BYTES 保持一致
/* 与服务端 SOLO_KEEP_MAX(20) 对齐：一次最多收 20 张。
   再多就超过暂存区上限，后端会按 mtime 从旧删 —— 删的正是本批最早那几张，
   于是「拖进来的图自己消失」。宁可少收并说明，也不能收下再丢。 */
const SOLO_BATCH_MAX = 20;
const SOLO_IMG_RE = /\.(png|jpe?g|webp|gif|bmp)$/i;
function openSoloPicker(){
  const inp=$("#soloFile"); if(inp) inp.click();
}
/* 这个文件能不能进单图通道：返回 "image" / "video" / "dir" / "other"。
   以扩展名为主、MIME 为辅 —— Windows 上 .webp 的 type 有时是空串，
   只看 MIME 会把好好的图挡在外面（用户看到的却是「不支持」）。 */
function soloKind(f){
  if(!f) return "other";
  const n = f.name || "", t = (f.type || "").toLowerCase();
  if(SOLO_IMG_RE.test(n)) return "image";
  if(t.startsWith("image/")) return "image";
  if(/\.(mp4|webm|mov|mkv|avi|m4v)$/i.test(n) || t.startsWith("video/")) return "video";
  /* 目录经 dataTransfer.files 进来时是 size=0、type="", name 无扩展名的伪 File。
     拖整个文件夹进来很常见（「把这批图都打开」），必须给明确回复而不是静默失败。 */
  if(!t && f.size === 0 && !/\.[^./\\]+$/.test(n)) return "dir";
  return "other";
}
async function soloPostOne(file){
  const r=await fetch("/api/solo",{method:"POST",
    headers:{"Content-Type":file.type||"application/octet-stream"},body:file});
  const j=await r.json().catch(()=>({}));
  if(!r.ok || !j.ok) throw new Error(j.error||r.statusText);
  return { type:"image", path:j.path, name:file.name||j.name,
           size:file.size, mtime:Math.floor(Date.now()/1000) };
}
/* 收一批文件 -> 全部落 SOLO_DIR -> 打开第一张，其余用 ←/→ 翻。
   单张时与旧行为完全一致（S.solo.items 只有一条）。
   并发 3：串行上传 20 张大图要等到天荒地老，全并发又会让暂存区清理
   （solo_prune）在同一批里互相踩。3 是「够快且不出乱」的折中。 */
async function uploadSoloMany(list){
  const files=(list||[]).filter(Boolean);
  if(!files.length) return;
  const imgs=[], skipped=[];
  for(const f of files){
    if(soloKind(f)!=="image"){ skipped.push(f.name||"（无名）"); continue; }
    if(f.size>SOLO_MAX){ skipped.push((f.name||"?")+"（超 64 MB）"); continue; }
    imgs.push(f);
  }
  if(!imgs.length){
    toast(skipped.length
      ? "这些不是可打开的图片："+skipped.slice(0,3).join("、")+(skipped.length>3?" 等":"")
      : "没有可打开的文件","err");
    return;
  }
  const overflow=Math.max(0, imgs.length-SOLO_BATCH_MAX);
  if(overflow) imgs.length=SOLO_BATCH_MAX;
  setBotState("busy", imgs.length>1 ? ("正在打开 "+imgs.length+" 张…") : "正在打开图片…", null, false);

  const done=new Array(imgs.length), errs=[];
  let cur=0;
  const worker=async()=>{
    while(cur<imgs.length){
      const k=cur++;
      try{ done[k]=await soloPostOne(imgs[k]); }
      catch(e){ errs.push((imgs[k].name||"?")+"："+e.message); }
    }
  };
  await Promise.all(Array.from({length:Math.min(3,imgs.length)},worker));

  const items=done.filter(Boolean);
  if(!items.length){
    setBotState("err","打开图片失败",null,false);
    toast("打开图片失败："+(errs[0]||"未知原因"),"err");
    return;
  }
  clearSoloView();
  clearScramblePreviews();
  S.solo={ items, idx:0 };
  document.body.classList.add("solo");
  setBotState("idle","",null,false);
  await openLb(0, true);
  // 没有配方时不弹「已打开」：那会和 openLb 里弹出的解释互相打架，
  // 而此刻真正需要被看到的是「为什么没有」。
  if(hasGenMeta(S.curMeta)) toast("已打开："+items[0].name,"ok");

  const notes=[];
  if(skipped.length) notes.push("跳过 "+skipped.length+" 个非图片文件");
  if(errs.length)    notes.push(errs.length+" 张打开失败");
  if(overflow)       notes.push("一次最多 "+SOLO_BATCH_MAX+" 张，后 "+overflow+" 张未收");
  if(notes.length) toast(notes.join("；"),"warn");
}
function uploadSolo(file){ return uploadSoloMany([file]); }
/* 单图模式：一个字段都没解析出来时，必须解释「为什么」——
   这是核心诉求落空（打开就是为了看配方），不是普通的空面板。
   库内**故意不弹**：自己下载的壁纸 / 截图本来就没有生成信息，
   每点开一张都弹一次会变成噪音；那时面板里的空态文案已经说清楚了。 */
function soloNoMetaToast(j){
  /* E5-7（2026-09-23）：单图模式唯一的目的是看配方，落空时必须**说清是哪种落空**。
     读失败（文件被占用 / 权限 / 盘离线）与「本来就没有」是两回事，给的下一步也不同：
     前者重试就好，后者要回去找原图。此前一律按后者解释，等于把人往错方向指。 */
  if(j && j.metaError){
    toastAction("元数据读取失败："+j.metaError, "重试",
      ()=>openLb(curIdx(), !!S.solo), "err", 12000,
      "这次是**读不到**，不是这张图没有配方：\n"
      + "· 常见于文件正被其它程序占用（编辑器 / 云盘同步中 / 杀软扫描），稍后重试即可。\n"
      + "· 也可能是权限不足，或所在盘符/网络位置已离线。\n"
      + "\n点「重试」会立刻重新读一次。");
    return;
  }
  /* 2026-11-08：12s 偏长（一次点击要盯十几秒才能消失），压到 8s —— 与 err 同类
     的打断式提示，且右栏黄框已经常驻讲同一件事，不需要在屏幕上停那么久。 */
  toastAction("这张图没有内嵌提示词与生成参数", "重新选图", openSoloPicker, "warn", 8000,
    "常见原因：\n"
    + "· 转发时没勾「原图」：微信 / QQ 会重新编码图片，PNG 的 tEXt 块、JPEG 的 APP1 段\n"
    + "  被整段丢掉 —— 图还能看，配方却没了。\n"
    + "· 图被截图、二次编辑或过了在线压缩工具，保存时元数据被剥掉。\n"
    + "· 这张图本来就不是 ComfyUI / WebUI 生成的，没有配方可读。\n"
    + "\n"
    + "怎么办：让对方以「文件」方式发原图（微信勾「原图」再发送，或直接发压缩包），\n"
    + "重新打开一次即可。");
}

{ const b=$("#btnOpenImg"); if(b) b.onclick=openSoloPicker;
  const f=$("#soloFile");
  if(f) f.onchange=()=>{ const fl=(f.files&&f.files[0])||null;
    f.value="";                       // 先清空：同一个文件连选两次也要能触发 change
    uploadSolo(fl); };
}
