/* ComfyMediaViewer · js/settings.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ================= 适配层 v2（新布局交互 · 2026-09-07） ================= */

/* 0. 线性图标填充（顶栏 / 工具条按钮；侧栏列表由各自渲染函数生成） */
(function setupIcons(){
  const map={ "#btnUp .ic":"up", "#btnAR .ic":"refresh",
              "#btnTrash .ic":"trash", "#btnSettings .ic":"gear",   /* V2/V1：回收站 / 设置入口 */
              "#btnOpenImg .ic":"openimg",                            /* V2.10.7：单图查看入口 */
              "#btnRefresh .ic":"refresh",                            /* V2.10.7：手动刷新，与设置页 setRescan 同一实现 */
              "#viewGrid .ic":"grid", "#viewList .ic":"list", "#viewTime .ic":"timeline",
              "#flowPage .ic":"pager", "#flowInf .ic":"masonry",
              "#btnBatch .ic":"batch",
              "#rootBtn .rs-ico":"dir",
              /* ---------- V4 图标批次 2：大图页与对比页（静态槽位） ---------- */
              "#btnClose .ic":"arrowLeft", "#btnFav .ic":"star",
              /* 图标得和动作对上：「推送画布」不是刷新（refresh 会被读成「打开/重载」），
                 用节点图 flow；「存工作流」是导出 json 文件（file），原来的 download
                 图标会被误认成「下载」—— 而真正的下载按钮就在它右边。 */
              "#btnOpen .ic":"flow", "#btnDlWF .ic":"file",
              "#btnOpenFolder .ic":"dirOpen",
              /* 2026-10-31：顶栏溢出收纳的「⋯」（顶栏放不下时才会露面） */
              "#lbMore .ic":"more",
              "#navL":"chevronLeft", "#navR":"chevronRight",
              /* 2026-11-09：像素快查入口 —— 放大镜是这个动作最直白的符号 */
              "#peekTgl .ic":"search",
              "#filmTgl":"caretDown",
              "#vPlay":"play", "#vLoop":"loop",
              "#vFrameL":"skipBack", "#vFrameR":"skipFwd", "#vMute":"volume",
              "#sideTglX":"chevronRight", "#sideTgl":"chevronLeft",
              "#cmpWipe .ic":"swap", "#cmpExit .ic":"x",
              "#wipeHandle .knob":"swap",
              /* ---------- V4 图标批次 3：弹窗与菜单（静态槽位） ---------- */
              "#btnRestart .ic":"refresh",
              "#hintClose .ic":"x",
              "#btnDupes .ic":"dupe", "#btnStats .ic":"chart",
              /* H5（2026-10-06）：查重 / 出图统计的界面入口从顶栏「工具▾」搬到设置面板
                 「数据」页；隐藏宿主按钮的图标槽（上一行）仍保留，两个新按钮复用同一图标 */
              "#setDupes .ic":"dupe", "#setStats .ic":"chart",
              /* ---------- V4 图标批次 4：左栏与空状态（静态槽位） ---------- */
              "#rootBtn .rs-car":"caretDown", "#leftTgl":"chevronLeft",
              "#tagManage":"gear", "#hintTrig":"keyboard", "#toTop":"up" };
  for(const sel in map){ const el=$(sel); if(el) el.innerHTML=ico(map[sel]); }
  /* 左栏分区标题的折叠箭头：7 个分区各一个槽（原由 CSS 伪元素 content:"▾" 画，
     伪元素塞不进 svg ⇒ 改成真实槽位，展开/收起仍靠 CSS 旋转） */
  document.querySelectorAll("#left h4 .secar").forEach(e=>{ e.innerHTML=ico("caretDown"); });
  /* 批次 B：搜索框左端的放大镜（原由 layout.css 的 .qico::before/::after 两个伪元素手绘） */
  document.querySelectorAll(".qico").forEach(e=>{ e.innerHTML=ico("search"); });
})();

/* 1. 详情面板开关（Tab / 边缘把手） */
let sideHidden=false;
function toggleSide(force){
  sideHidden = (force!==undefined)?force:!sideHidden;
  document.getElementById("side").classList.toggle("hidden", sideHidden);
  /* 2026-10-31：面板一开一合会改变顶栏的可用宽度 ——
     收起时「复制提示词」回到顶栏、展开时让位给面板里那颗同一个动作的按钮。
     宽度变了还可能要多收/少收一个「⋯」项，mvLbSyncCopy 内部一并重算。 */
  if(window.mvLbSyncCopy) window.mvLbSyncCopy();
  setTimeout(centerFilmOn,230);   // 面板滑出动画结束后，当前缩略图重新居中（对齐大图中轴）
}
function centerFilmOn(){          // 当前预览缩略图水平居中（即大图正下方）
  const c=$("#lbFilm .on"); if(c) c.scrollIntoView({block:"nearest",inline:"center"});
}
$("#sideTgl").onclick=()=>toggleSide(false);
$("#sideTglX").onclick=()=>toggleSide(true);

/* 1.5 底部预览栏收起/展开（F 键 / 右下角开关），状态记忆 */
function toggleFilm(){
  const off=$("#lbFilm").classList.toggle("off");
  $("#filmTgl").innerHTML = ico(off?"caretUp":"caretDown");
  $("#filmTgl").dataset.tip = off?"展开预览栏 (F)":"收起预览栏 (F)";
  savePref("mv.film", off?"0":"1");
}
$("#filmTgl").onclick=toggleFilm;
try{ if(localStorage.getItem("mv.film")==="0"){
  $("#lbFilm").classList.add("off");
  $("#filmTgl").innerHTML=ico("caretUp"); $("#filmTgl").dataset.tip="展开预览栏 (F)";
} }catch(_){}
/* ε-11 §6.4：缩略图「裁切/适应」持久化（mv.fit）—— 抽屉里此前唯一不落盘的设置。
   B2（2026-10-30）：不再改 #fit 的 textContent（它已是 .seg 分段控件），
   选中档交给 nav.js 的 mvSyncFit 同步；nav.js 在 settings.js 之前加载，可直接调。 */
try{ if(localStorage.getItem("mv.fit")==="1"){
  document.body.classList.add("cover");
  if(window.mvSyncFit) window.mvSyncFit(true);
} }catch(_){}
/* UI-03（2026-10-23）：卡片文件名显隐。两个**独立**开关而不是一个三态循环按钮 ——
   循环按钮每次点之前都得读文案才知道到了哪一档。
     关 + 开 = 按需（默认）：滚动 / 悬停 / 键盘聚焦时出现
     开 + 开 = 常显（老观感，安全阀）
     关 + 关 = 全隐藏，只留右下角百分比刻度 */
try{
  if(localStorage.getItem("mv.cardinfo")==="1") document.body.classList.add("cardinfo");
  if(localStorage.getItem("mv.cardinfoscroll")==="0") document.body.classList.add("cardinfo-noscroll");
}catch(_){}
/* B2（2026-10-30 · UI 美化 B 组）：这两个控件从「常显文件名：关 / 滚动时显示：开」
   文字按钮改成开关行。状态改由 **aria-checked** 承载 —— 文案不再随状态变，
   行宽不跳、无障碍语义也对（role="switch"）。CSS 侧 [aria-checked="true"] 画轨道。 */
function applyCardInfo(){
  const always = document.body.classList.contains("cardinfo");
  const b1 = $("#cardInfo"), b2 = $("#cardInfoScr");
  if(b1) b1.setAttribute("aria-checked", String(always));
  if(b2){
    b2.setAttribute("aria-checked",
      String(!document.body.classList.contains("cardinfo-noscroll")));
    b2.disabled = always;      /* 常显时它无意义。**只置灰不改存储值** —— 关掉常显后回到原选择 */
  }
}
if($("#cardInfo")) $("#cardInfo").onclick = () => {
  const on = document.body.classList.toggle("cardinfo");
  savePref("mv.cardinfo", on ? "1" : "0");
  applyCardInfo();
};
if($("#cardInfoScr")) $("#cardInfoScr").onclick = () => {
  const off = document.body.classList.toggle("cardinfo-noscroll");
  savePref("mv.cardinfoscroll", off ? "0" : "1");
  applyCardInfo();
};
applyCardInfo();
$("#lbFilm").addEventListener("wheel",e=>{          // 纵向滚轮 → 横向滚动预览栏
  if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){ e.preventDefault(); $("#lbFilm").scrollLeft+=e.deltaY; }
},{passive:false});

/* 2. 网格密度滑块（连续调节卡片大小）
      默认 200px = 设计规范 1.2 的 minmax(200px,1fr)；间距在 8~16px 之间随卡片大小线性变化，
      200px 时正好 16px（规范 1.2 的 grid-gap）。选择结果记在 localStorage（mv.dens）。 */
const densRange=$("#densRange");
if(densRange){
  const applyDens=v=>{
    document.documentElement.style.setProperty("--card-min", v+"px");
    document.documentElement.style.setProperty("--grid-gap", Math.max(8,Math.min(16,Math.round(v/12.5)))+"px");
    const lv=$("#densVal"); if(lv) lv.textContent=v+"px";
    /* 2026-10-31：列宽变了必须重算 --cv-h 并刷新离屏卡片的尺寸记忆，
       否则 contain-intrinsic-size:auto 会按旧列宽的卡高占位（实测 110px 档 +77%）。
       函数内部自己去抖 180ms —— 拖滑块时不能每帧全量重排。 */
    if(window.mvCvRelayout) window.mvCvRelayout();
  };
  let savedDens=parseInt(densRange.value,10)||200;
  try{ savedDens=parseInt(localStorage.getItem("mv.dens")||savedDens,10)||savedDens; }catch(_){}
  if(savedDens>=+densRange.min && savedDens<=+densRange.max) densRange.value=savedDens;
  densRange.oninput=()=>applyDens(+densRange.value);
  densRange.onchange=()=>{ savePref("mv.dens", densRange.value); };
  applyDens(+densRange.value);
  /* V21：密度档位（紧凑 / 舒适 / 宽松）按下时经这里把值灌回滑块。
     滑块是唯一真相、档位只是它的三个快捷键 —— 两套状态各记一份必然互相打架。 */
  window.mvSetDens=v=>{
    if(v<+densRange.min || v>+densRange.max) return;
    densRange.value=String(v);
    applyDens(v);
    savePref("mv.dens", String(v));
  };
  window.mvGetDens=()=>+densRange.value;
}

/* 视图切换：网格 / 列表 / 时间轴（V2.8.5 三态）。
   LIST_VIEW 仍是**派生值**，其它 5 处读它的地方一行都不用改。 */
(function setupViewToggle(){
  const btns=[$("#viewGrid"),$("#viewList"),$("#viewTime")].filter(Boolean);
  if(btns.length<2) return;
  const sync=mode=>{
    VIEW_MODE = (mode==="list"||mode==="time") ? mode : "grid";
    const list = VIEW_MODE==="list";
    LIST_VIEW=list;
    document.body.classList.toggle("list-view",list);
    /* 原比例铺满布局只在网格视图生效。离开网格时由它把卡片上的内联坐标清干净
       —— 否则列表视图会继承 position:absolute 与像素宽高，整屏错位。 */
    if(window.jfViewMode) window.jfViewMode(VIEW_MODE);
    const lh=$("#listHead"); if(lh) lh.hidden=!list;   // 列表视图表头
    for(const b of btns){
      const on=b.dataset.v===VIEW_MODE;
      b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
    }
    /* 时间轴与「展开文件夹」互斥：展开文件夹时序列按文件夹数算（seqN），
       两套分组口径叠在一起会互相打架，所以选中时间轴时自动收起文件夹。 */
    if(VIEW_MODE==="time" && S.expandDirs) S.expandDirs=false;
  };
  let mode="grid";
  try{ mode=localStorage.getItem("mv.view")||"grid"; }catch(_){}
  if(!["grid","list","time"].includes(mode)) mode="grid";
  sync(mode);
  window.setViewMode = m=>{
    if(m===VIEW_MODE) return;
    sync(m);
    savePref("mv.view",VIEW_MODE);
    if(LIST_VIEW && !S.metaMap) loadMetaMap();       // 顺带取尺寸/模型/采样器
    PAGE=1;                                          // 视图换了 → 行数口径变了 → 回第 1 页
    applyFilter();                                   // 重建行（列表要多渲染字段列 / 时间轴要重新分组）
    /* C-P1：广播视图变化 —— columns.js 据此把「列显隐」重新贴到列表行上。
       为什么需要：columns.js 的数据行处理已改为「网格视图短路 + 只维护新增节点」，
       而网格 ⇄ 列表切换时若卡片被复用（未重建），就不会有 childList 变化 ⇒ 补类落空。
       放在 applyFilter() **之后**：先让新行就位，再补类。 */
    try{ window.dispatchEvent(new CustomEvent("mv:viewchange", { detail:{ view:VIEW_MODE } })); }catch(_){}
    requestAnimationFrame(playViewEnter);            // V28：网格就位后播一次转场
  };
  /* V28：切视图时播一次淡入 + 微位移。只在「切视图」触发 —— 翻页 / 筛选不播，
     否则回到第 1 页会重复动画（构想取舍）。只动 opacity/transform，合成层不重排。 */
  function playViewEnter(){
    const gw=$("#gridwrap"); if(!gw) return;
    gw.classList.remove("view-enter"); void gw.offsetWidth; gw.classList.add("view-enter");
    setTimeout(()=>gw.classList.remove("view-enter"), 400);
  }
  for(const b of btns) b.onclick=()=>window.setViewMode(b.dataset.v);
})();

/* ---------------- 时间轴视图（V2.8.5）----------------
   工具条（粒度 / 回到今天 / 日期索引）+ [` `]` 跳组 + T 切视图。
   快捷键沿用既有守卫：焦点不在输入控件、且**不在大图内**（大图里 T 已被「聚焦标签输入」占用）。 */
let TL_PENDING=null;                     // 分页模式下点索引时暂存目标组，切到瀑布流后补跳
let TL_SUPPRESS=0;                       // 程序化跳组后的短暂抑制窗：期间不让滚动高亮覆盖点选结果

function renderTlUI(on){
  TL_JOB++;                              // 视图重建 → 作废仍在跑的分帧补齐任务（避免跳到旧位置）
  document.body.classList.toggle("timeline", !!on);
  const bar=$("#tlBar"); if(bar) bar.hidden=!on;
  const idx=$("#tlIndex");
  if(!on){
    if(idx){ idx.hidden=true; idx.classList.remove("open"); }
    TL_GROUPS=[]; return;
  }
  if(idx) idx.hidden = TL_GROUPS.length===0;
  syncTlGrain(); renderTlIndexList(); updateTlTodayBtn(); updateTlIndexActive();
  if(TL_PENDING!==null){ const g=TL_PENDING; TL_PENDING=null;
    requestAnimationFrame(()=>tlJump(g)); }
}
function syncTlGrain(){
  const box=$("#tlGrain"); if(!box) return;
  for(const b of box.querySelectorAll("button[data-g]")){
    const on=b.dataset.g===TL_GRAIN;
    b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
  }
}
/* 日期索引栏：只列**当前结果集里真实存在**的组（不猜、不补空日期）
   F1 P2-1：applyFilter 的**每次**调用都会走到这里（翻页 / 排序 / 搜索防抖 /
   切粒度 / 元数据到位），而大库在 hour·batch 粒度下 TL_GROUPS 可达数千组 ⇒
   原来每次都要清空 + 逐个 appendChild 新建数千节点与闭包。三处提速：
     · 组签名未变直接跳过重建（同 loadStats 的 _sig 思路）；
     · 用 DocumentFragment 一次性插入（原来逐个 append 各自触发重排）；
     · 顺便建 gi→节点 的映射表，供 tlSetIndexActive 做 O(1) 高亮。 */
let _tlIdxMap = null, _tlIdxOn = null, _tlIdxAnchor = null;
function renderTlIndexList(){
  const box=$("#tlIdxList"); if(!box) return;
  const wrap=$("#tlIndex"), keep=wrap?wrap.scrollTop:0;
  const sig=TL_GRAIN+"|"+TL_GROUPS.length+"|"
    +TL_GROUPS.map(m=>m.gi+","+m.title+","+m.n).join(";");
  // A5（2026-10-30 · UI 美化 A 组）：索引栏按月插分隔标题后，子节点数**不再等于组数**。
  // 原先的「安全校验」写的是 children.length===TL_GROUPS.length，加月份后会永远不相等
  // ⇒ 每次 applyFilter 都整栏重建（大库数千组 = 每帧数千节点）。改记自己上一次的节点数。
  if(sig===renderTlIndexList._sig && box.children.length===renderTlIndexList._n){
    if(wrap) wrap.scrollTop=keep;
    return;
  }
  renderTlIndexList._sig=sig;
  box.innerHTML="";
  _tlIdxMap=new Map(); _tlIdxOn=null; _tlIdxAnchor=null;   // 节点全换 ⇒ 旧缓存全部作废
  const frag=document.createDocumentFragment();
  /* A5：几十行「10月28日 41」连续排下来，跨月没有断点，扫读时找不到「这个月从哪开始」。
     ⚠️ d0 是 **'2026-10-30' 这样的日期字符串**（tlDayKey 的返回值），不是 YYYYMMDD 整数 ——
     第一版按 Math.floor(d0/100) 取月份，字符串相除得 NaN，整段静默不生效（实机 0 个 .ti-m 才发现）。
     用 title 解析也不可靠：title 随粒度变，批次粒度下是「批次 14:32」，根本没有月份。
     月 / 年粒度本身已按月、年分组，再加一层就是重复，所以跳过。 */
  const withMonth = TL_GRAIN!=="month" && TL_GRAIN!=="year";
  let lastYm="";
  for(const m of TL_GROUPS){
    if(withMonth){
      const mm=/^(\d{4})-(\d{2})/.exec(String(m.d0||""));
      if(mm){
        const ym=mm[1]+"-"+mm[2];
        if(ym!==lastYm){
          lastYm=ym;
          const hm=document.createElement("div");
          hm.className="ti-m";
          hm.textContent=mm[1]+" 年 "+(+mm[2])+" 月";
          frag.appendChild(hm);
        }
      }
    }
    const b=document.createElement("button");
    b.type="button"; b.className="ti"; b.dataset.gi=String(m.gi);
    b.dataset.tip=m.title+" · "+m.n+" 张";
    const d=document.createElement("span"); d.className="ti-d"; d.textContent=m.title;
    const n=document.createElement("span"); n.className="ti-n"; n.textContent=String(m.n);
    b.appendChild(d); b.appendChild(n);
    b.onclick=()=>tlJump(m.gi);
    frag.appendChild(b);
    _tlIdxMap.set(String(m.gi), b);
  }
  box.appendChild(frag);                       // 一次性入 DOM
  _tlIdxAnchor=box.querySelector(".ti");       // 存活探针：tlSetIndexActive 用它认「栏还是这一栏」
  renderTlIndexList._n=box.children.length;     // 含月份标题 ⇒ 必须按实际节点数校验
  if(wrap) wrap.scrollTop=keep;
}
function tlTodayGi(){                    // 结果里真实包含「今天」的那一组
  const k=tlDayKey(Date.now()/1000);
  for(const m of TL_GROUPS) if(m.d0<=k && k<=m.d1) return m.gi;
  return -1;
}
function updateTlTodayBtn(){
  const b=$("#tlToday"); if(!b) return;
  const gi=tlTodayGi();
  b.disabled = gi<0;
  b.dataset.tip = gi<0 ? "当前筛选结果里没有今天的图" : "跳到今天那一组";
}
/* 跳组：瀑布流下目标组头可能还没渲染出来（S.view 是全量，DOM 是分批的）→ 先按需补齐再跳；
   分页模式下目标可能真的不在本页（S.view 已切片）→ 切到瀑布流后补跳。
   跨度小就同步补齐（点击即到位）；跨度中等走分帧（不冻页面）；
   跨度极大（跨半个库）先把中间组折叠起来 —— 网格要连续渲染，否则得插几千张卡、实测 7500 张要 6.6s。 */
const TL_SYNC_MAX=300;                   // 同步补齐上限（行）；超过则分帧
const TL_FOLD_MIN=800;                   // 超过这个跨度就先折叠中间组（行数骤降后才谈得上「跳」）
function tlJump(gi){
  if(!TL_GROUPS.some(m=>m.gi===gi)) return;
  const el=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
  if(el){ tlScrollTo(el); return; }
  /* F1 P2-2：分页模式下目标组不在本页时，原实现是 `$("#flowInf").click()` ——
     走的是和用户手动点按钮**完全相同**的 set(true)：① 展示方式被改成无限瀑布流；
     ② `mv.flow` 写进 localStorage ⇒ 下次启动还是瀑布流。用户只是想跳个日期，
     展示偏好被顺手改掉了（而 toast 那句「已切换到无限瀑布流」在他盯着日期时
     基本读不到）。改为：按全量行索引算出目标页码，**只切页、不动展示方式**。 */
  if(!FLOW){
    const ri=TL_ROWIDX[gi];
    if(typeof ri==="number" && ri>=0){
      const p=Math.floor(ri/PER)+1;
      if(p>=1 && p!==PAGE){
        TL_PENDING=gi;                          // 先挂上：applyFilter → renderTlUI 会取走并补跳
        setPage(p);
        toast("该日期在第 "+p+" 页，已为你翻过去");
        return;
      }
    }
    // 拿不到行索引（异常兜底）：保持「会跳到位」的能力，但把副作用说清楚
    TL_PENDING=gi; $("#flowInf").click();
    toast("该日期不在当前页，已临时切到瀑布流以定位；可在工具条切回分页");
    return;
  }
  const i=S.view.findIndex(x=>x.type==="tlhead" && x.gi===gi);
  if(i<0){ toast("该分组不在当前页"); return; }
  // 补到「组头 + 一批」：只补到组头的话它下方是空的，滚动会被内容高度夹住 → 贴不到顶
  const target=Math.min(i+BATCH, S.view.length-1);
  if(target+1-S.shown>TL_FOLD_MIN){
    let folded=0;
    for(const m of TL_GROUPS){
      if(m.gi>=gi) break;                // 只折目标组之前的
      if(!TL_COLLAPSED[m.gkey]){ TL_COLLAPSED[m.gkey]=true; folded++; }
    }
    if(folded){
      TL_PENDING=gi;                     // 先挂上：applyFilter → renderTlUI 会取走它并在新视图里补跳
      applyFilter();                     // 重建行（折叠后行数骤降）
      toast("已折叠中间 "+folded+" 组（点组标题可展开）");
      return;
    }
  }
  if(target+1-S.shown<=TL_SYNC_MAX){
    renderUpTo(target);
    const e=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
    if(e){ tlScrollTo(e); return; }
  }
  const job=++TL_JOB;                    // 大跨度：分帧补齐，过程中页面保持响应
  const tgt=TL_GROUPS.find(x=>x.gi===gi);
  toast("正在定位到 "+(tgt?tgt.title:"该日期")+"…");
  tlFillAsync(target, job, ()=>{
    if(job!==TL_JOB) return;
    const e=document.querySelector('#grid .tl-head[data-gi="'+gi+'"]');
    if(e) tlScrollTo(e);
  });
}
/* 组头是 sticky：吸顶状态下它的 rect / offsetTop 都被改写（实测 tl_probe.py），
   拿它算目标位置会「点了没反应」。所以先把吸顶临时摘掉量一次静态位置，再滚过去。 */
function tlStaticTop(el){
  const prev=el.style.position;
  el.style.position="static";
  const off=el.offsetTop;                  // 读 offsetTop 会强制一次同步布局，拿到的就是静态位置
  el.style.position=prev;
  return off;
}
function tlScrollTo(el){
  const gw=$("#gridwrap"); if(!gw||!el) return;
  if(typeof mvScrollProg==="function") mvScrollProg(400);   // UI-03：程序化跳转，别闪文件名
  gw.scrollTop=tlStaticTop(el);
  for(let k=0;k<3;k++){                    // 微调：末组滚不到顶时会被 scrollTop 上限夹住，自然收敛
    const d=el.getBoundingClientRect().top-gw.getBoundingClientRect().top;
    if(Math.abs(d)<=1) break;
    gw.scrollTop+=d;
  }
  TL_SUPPRESS=performance.now()+700;       // 这次滚动是程序触发的：别让滚动高亮把「用户点的那一组」抢走
  tlSetIndexActive(+el.dataset.gi);
}
/* 当前视口顶部所在的组 = **最后一个已经吸顶的组头**。
   组头是 sticky 且都是 #grid 的直接子项，滚过的组头会叠在顶部（视觉上后一个盖住前一个），
   于是它们的 rect 全被改写成吸附位置 —— 只能取文档序里最后一个「贴住顶部」的那个。
   注意：末尾组下方内容不足时贴不到顶，此时几何上「视口顶部」确实还在上一组，
   所以跳转后靠 TL_SUPPRESS 保住用户点选的高亮（见 tlScrollTo）。 */
let _tlCurHead=null;                       // 上一次命中的组头（就近扫描的锚点）
function tlCurrentGi(){
  const gw=$("#gridwrap"); if(!gw) return -1;
  const top=gw.getBoundingClientRect().top+1;
  const heads=gw.querySelectorAll(".tl-head");
  const n=heads.length;
  if(!n) return -1;
  /* F1 P2-1：原来每帧从头遍历全部组头（每项一次 getBoundingClientRect ⇒ 强制布局）。
     组头沿文档序的 top 单调不减（已吸顶的那批都等于吸附位置，之后递增），
     所以「最后一个 top<=视口顶」这个边界只有一处 ⇒ 从上次命中的组头出发
     双向扩几步就够（滚动时通常只移动 0~2 格）。锚点失效（网格重建）则回退全扫。 */
  let k=-1;
  if(_tlCurHead) for(let i=0;i<n;i++) if(heads[i]===_tlCurHead){ k=i; break; }
  if(k<0) k=n-1;
  while(k>=0 && heads[k].getBoundingClientRect().top>top) k--;
  while(k+1<n && heads[k+1].getBoundingClientRect().top<=top) k++;
  if(k<0){ _tlCurHead=heads[0]; return +heads[0].dataset.gi; }
  _tlCurHead=heads[k];
  return +heads[k].dataset.gi;
}
/* 高亮指定组（并把索引栏里那一行滚进可见区） */
function tlSetIndexActive(gi){
  if(VIEW_MODE!=="time") return;
  const box=$("#tlIdxList"), wrap=$("#tlIndex");
  if(!box||!wrap) return;
  /* F1 P2-1：滚动时 rAF 每帧都会调这里，原来是遍历全部索引行做 classList.toggle
     （大库数千次 DOM 写）。现在查映射表，只动「旧命中 -1、新命中 +1」两个节点。
     A5（2026-10-30）：这里的「映射表过期」兜底**不能再数子节点**——索引栏按月插了
     `.ti-m` 标题后 children.length 恒 = 组数 + 月份数 ⇒ 判据永远不会成立（永远过期），
     于是每帧都重建、每帧把 _tlIdxOn 置空 ⇒ 上一行的高亮永远移不掉，`.ti.on` 越滚越多，
     测试里 querySelector('.ti.on') 只能摸到最上面那行（表现为「高亮不跟随」）。
     改判「上次记的首个 .ti 是否还挂在同一个 box 上」：O(1)，且真换栏（innerHTML 被清）时必失效。 */
  if(!_tlIdxMap || (!!_tlIdxAnchor && _tlIdxAnchor.parentNode!==box)){
    _tlIdxMap=new Map(); _tlIdxOn=null;        // 兜底：映射表缺失/过期就现建一次
    for(const b of box.children) if(b.dataset.gi) _tlIdxMap.set(b.dataset.gi, b);
    _tlIdxAnchor=box.querySelector(".ti");
  }
  const hit=_tlIdxMap.get(String(gi)) || null;
  if(hit!==_tlIdxOn){
    if(_tlIdxOn) _tlIdxOn.classList.remove("on");
    if(hit) hit.classList.add("on");
    _tlIdxOn=hit;
  }
  if(!hit) return;
  const r=hit.getBoundingClientRect(), pr=wrap.getBoundingClientRect();   // 索引栏内自动滚到可见
  if(r.top<pr.top+26) wrap.scrollTop-=(pr.top+26-r.top);
  else if(r.bottom>pr.bottom-4) wrap.scrollTop+=(r.bottom-pr.bottom+4);
}
function updateTlIndexActive(){ tlSetIndexActive(tlCurrentGi()); }
function tlStepGroup(d){
  if(VIEW_MODE!=="time" || !TL_GROUPS.length) return;
  let i=TL_GROUPS.findIndex(m=>m.gi===tlCurrentGi());
  if(i<0) i=0;
  const n=Math.max(0, Math.min(TL_GROUPS.length-1, i+d));
  if(n!==i) tlJump(TL_GROUPS[n].gi);
}

(function setupTimeline(){
  const box=$("#tlGrain");
  try{ const g=localStorage.getItem("mv.tlgrain");
       if(["hour","day","week","month","batch"].includes(g)) TL_GRAIN=g; }catch(_){}
  if(box) for(const b of box.querySelectorAll("button[data-g]")){
    b.onclick=()=>{
      const g=b.dataset.g;
      if(g===TL_GRAIN) return;
      TL_GRAIN=g; TL_COLLAPSED={}; TL_SHOWN={}; TL_PENDING=null;
      savePref("mv.tlgrain",g);
      PAGE=1; applyFilter();
    };
  }
  const tt=$("#tlToday");
  if(tt) tt.onclick=()=>{
    const gi=tlTodayGi();
    if(gi<0){ toast("当前筛选结果里没有今天的图"); return; }
    tlJump(gi);
  };
  const tg=$("#tlIdxTgl");
  if(tg) tg.onclick=e=>{ e.stopPropagation();
    const idx=$("#tlIndex"); if(idx) idx.classList.toggle("open"); };
  const gw=$("#gridwrap");
  if(gw){
    let raf=0;                            // 滚动高亮：rAF 节流，避免每像素都重算
    gw.addEventListener("scroll",()=>{
      if(VIEW_MODE!=="time" || raf) return;
      if(performance.now()<TL_SUPPRESS) return;      // 跳组引起的程序化滚动，别抢高亮
      raf=requestAnimationFrame(()=>{ raf=0; updateTlIndexActive(); });
    },{passive:true});
    gw.addEventListener("click",()=>{     // 窄屏抽屉：点内容区即收起
      const idx=$("#tlIndex");
      if(idx && idx.classList.contains("open") && innerWidth<=900) idx.classList.remove("open");
    });
  }
  document.addEventListener("keydown",e=>{
    const el=e.target, tg2=(el.tagName||"").toUpperCase();
    if(tg2==="INPUT"||tg2==="TEXTAREA"||tg2==="SELECT"||el.isContentEditable) return;
    if(lb.classList.contains("on")) return;                      // 大图内有自己那套（T=聚焦标签输入）
    if(document.querySelector(".bigMask")||document.getElementById("catInputMask")) return;
    if(e.key==="t"||e.key==="T"){ e.preventDefault();
      window.setViewMode(VIEW_MODE==="time"?"grid":"time"); return; }
    if(VIEW_MODE!=="time") return;
    if(e.key==="["){ e.preventDefault(); tlStepGroup(-1); return; }
    if(e.key==="]"){ e.preventDefault(); tlStepGroup(1); }
  });
})();

/* 2a. 展示方式：分页 ⇄ 无限瀑布流（工具条「批量」左侧）
   分页：applyFilter 只取当前页，底栏显示页码与每页数量
   瀑布流：保留整份结果，renderMore 每批 100 张续载，底栏分页与每页数量隐藏 */
(function setupFlowMode(){
  const seg=$("#flowSeg"), pageBtn=$("#flowPage"), infBtn=$("#flowInf");
  if(!seg) return;
  const sync=()=>{
    document.body.classList.toggle("flow", FLOW);
    pageBtn.classList.toggle("on", !FLOW);
    infBtn.classList.toggle("on", FLOW);
    pageBtn.setAttribute("aria-pressed", String(!FLOW));
    infBtn.setAttribute("aria-pressed", String(FLOW));
    const pg=$("#pager"), pp=$("#perPage");
    if(pg) pg.hidden = FLOW;                 // 无限流：底栏不显示页码
    if(pp) pp.hidden = FLOW;                 // 也不显示每页数量
  };
  sync();
  const set=m=>{
    if(m===FLOW) return;
    FLOW=m;
    savePref("mv.flow", m?"1":"0");
    PAGE=1;
    sync();
    applyFilter();                            // 两种模式的切片/续载逻辑不同，重建视图
    toast(m ? "已切换到无限瀑布流（滚到底自动续载）" : "已切换到分页展示");
  };
  pageBtn.onclick=()=>set(false);
  infBtn.onclick=()=>set(true);
})();

/* 2b. 字体大小滑块：整页字号缩放（--fsx 同时驱动顶栏/侧栏/面板等承载文字的尺寸，
       因此放大字号时边框跟着长，不会挤成两行或截断）。

       拖动时只回显右上角数字、不立即应用：字号一变，上方区块高度和侧栏宽度都会变，
       滑块会被布局重排推离鼠标（不跟手）。松开鼠标（change）才应用并记忆；
       键盘方向键会同时触发 input+change，所以键盘操作仍是即时生效。 */
const fsRange=$("#fsRange");
if(fsRange){
  const fsVal=$("#fsVal");
  const applyFs=v=>{
    document.documentElement.style.setProperty("--fsx",(v/100).toFixed(3));
    if(fsVal) fsVal.textContent=v+"%";
    if(LAST_PAGER) renderPager(LAST_PAGER[0], LAST_PAGER[1]);   // 字号变了 -> 底栏可用宽度变了 -> 重排槽位
    /* 2026-10-31：两个量都跟着 --fsx 走，改完必须重量 ——
       ① 分类下拉的宽度算式里有 fsx（量一次文字宽 + 固定开销）；
       ② 顶栏放不放得下直接随字号变（实测 1080 宽下 1.25 档就多溢出 208px）。 */
    if(window.mvFitCatSelect) window.mvFitCatSelect();
    if(window.mvLbFit) window.mvLbFit();
  };
  let savedFs=100;
  try{ savedFs=parseInt(localStorage.getItem("mv.fs")||"100",10)||100; }catch(_){}
  if(savedFs>=+fsRange.min && savedFs<=+fsRange.max) fsRange.value=savedFs;
  applyFs(+fsRange.value);                       // 初始化：按记住的值应用一次
  fsRange.oninput=()=>{                          // 拖动中：只回显数字，保持滑块在指针下
    if(fsVal) fsVal.textContent=fsRange.value+"%";
  };
  fsRange.onchange=()=>{                         // 松手/键盘确认：应用并记住
    applyFs(+fsRange.value);
    savePref("mv.fs",fsRange.value);
  };
}

/* 2c. 主题：深色 / 亮色 / 跟随系统（大图页与对比页固定深色，见 CSS） */
(function(){
  const sel=$("#themeSel"); if(!sel) return;
  const mq=window.matchMedia("(prefers-color-scheme: light)");
  const apply=pref=>{
    const use = pref==="auto" ? (mq.matches?"light":"dark") : pref;
    document.documentElement.setAttribute("data-theme", use);
  };
  let pref="dark";                               // 默认深色（设计规范的主题口径）；侧栏固定深色，见 tokens.css
  try{ pref=localStorage.getItem("mv.theme")||"dark"; }catch(_){}
  if(!["dark","light","auto"].includes(pref)) pref="light";
  sel.value=pref; apply(pref);
  sel.onchange=()=>{
    pref=sel.value;
    savePref("mv.theme", pref);
    apply(pref);
    toast(`主题：${{dark:"深色",light:"亮色",auto:"跟随系统"}[pref]}`);
  };
  mq.addEventListener("change",()=>{ if(pref==="auto") apply(pref); });
})();

/* 2d. 删除去向：应用内回收站 / Windows 回收站
   权威值在服务端 settings.json（删除是后端执行的）；这里只负责 UI 读写与
   全局 DEL_MODE 缓存 —— 各删除入口的确认文案、按钮提示都跟着它走。 */
(function setupDelMode(){
  const sel=$("#delModeSel"); if(!sel) return;
  /* 只改本地：DEL_MODE 缓存 + 下拉值 + 两处删除按钮 tip。**不碰服务端**。 */
  const applyLocal=(m)=>{
    DEL_MODE = (m==="system") ? "system" : "app";
    if(sel.value!==DEL_MODE) sel.value=DEL_MODE;
    const sys = DEL_MODE==="system";
    const tip = sys ? "删除（移入 Windows 回收站，可在资源管理器里还原）"
                    : "删除（移入应用内回收站，可在顶栏「回收站」还原）";
    /* 大图顶栏那颗删除的说明由 refreshLbTips() 统一生成（它要按去向分两行写，
       还要跟着收藏态/下载目录一起刷）—— 这里只留批量条那一份，别写第二遍。 */
    if(window.refreshLbTips) refreshLbTips();
    const bdel=$("#bDel");                        // 批量条删除（V31 第②③步后全站只走 data-tip，不再写 title）
    if(bdel) bdel.dataset.tip=tip;
    const tb=$("#btnTrash");
    if(tb) tb.dataset.tip = sys
      ? "应用内回收站（当前删除去向是 Windows 回收站，这里不会有新条目）"
      : "应用内回收站：还原或彻底删除";
  };
  /* P1（2026-10-09）：「先落盘、成功后再改本地」。
     旧实现是**先改本地**（DEL_MODE / 下拉值 / 删除按钮 tip 在请求发出**之前**就翻转），
     失败分支只弹一条 3.2 秒自动消失的 toast 且什么都不回滚 ⇒ `DEL_MODE` 停在错值。
     而 `delIsSystem()` 驱动**所有**删除入口的确认文案（batch / extras / gallery / nav）——
     用户在确认框里看到「移入 Windows 回收站」，文件实际进了应用内回收站（或反之）。
     旧实现还在保存结果未知时就置 `DEL_MODE_READY=true`，主动关掉了别的模块
     「以服务端为准」的兜底说明，与它自己下方注释写的治理意图正好相反。
     现在：失败 → 回滚到切换前的值 + 底栏 warn + toastErr，且**不**置 READY。 */
  let _delSaving=false;
  const saveDelMode=(want)=>{
    if(_delSaving) return;            // 无防重入时连点两次会并发两个 POST，后到者定 toast
    const prev=DEL_MODE;
    _delSaving=true;
    sel.disabled=true;
    api("/api/prefs",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({delete_mode:want})})
      .then(r=>{
        const got=(r.delete_mode==="system") ? "system" : "app";
        DEL_MODE_READY=true;          // 只有服务端确认过才算「已同步」
        applyLocal(got);              // 以**服务端返回值**为准，不是请求值
        if(got!==want) toast("删除去向：服务端仍为"
          +(got==="system"?"Windows 回收站":"应用内回收站"),"warn");
        else toast(`删除去向：${got==="system"?"Windows 回收站":"应用内回收站"}`);
      })
      .catch(e=>{
        applyLocal(prev);             // 回滚：界面与服务端重新一致
        setBotState("warn","删除去向未能保存，仍按上一次的去向执行",null,false);
        toastErr("设置保存失败", String((e&&e.message)||e));
      })
      .finally(()=>{ _delSaving=false; sel.disabled=false; });
  };
  /* F2-P2-5（2026-10-19）：用户在「首读失败 → 900ms 后重试」的窗口里手动改了下拉，
     重试回来的**服务端旧值**会无条件 applyLocal() 把它盖回去 —— 下拉跳回旧值、
     DEL_MODE 与所有删除 tip 一起回退，而用户刚才那次 POST 其实已经成功了。
     用户看到「我选了 A，界面自己变回 B」。用一个闸门记录「人手改过没有」。 */
  let _delModeUserSet=false;
  sel.onchange=()=>{ _delModeUserSet=true; saveDelMode(sel.value); };
  /* F1 P2-4 / F3 P2-1：原来失败被 `.catch(()=>{})` 完全吞掉 —— 没有任何提示，
     下拉停在默认「应用内回收站」，而服务端若存的是 system，文件其实进了 Windows
     回收站，用户会去应用内回收站里找刚删的文件（找不到）。属于「用户以为 A、
     实际发生 B」的一类。改为：失败重试一次；仍失败则明确反馈（底栏 warn + toast +
     下拉提示），并保持 DEL_MODE_READY=false 供删除入口判断。 */
  const pullDelMode=(retry)=>{
    api("/api/info").then(j=>{
      DEL_MODE_READY=true;
      sel.removeAttribute("data-tip");
      /* F2-P2-5：用户在这 900ms 里已经手动选过 ⇒ 只收 READY 与 tip，
         不再用服务端旧值覆盖他的选择（他的 POST 才是最新意图）。 */
      if(_delModeUserSet) return;
      applyLocal(j.delete_mode);
    }).catch(e=>{
      if(retry>0){ setTimeout(()=>pullDelMode(retry-1), 900); return; }
      DEL_MODE_READY=false;
      sel.dataset.tip="未能读取服务端「删除去向」设置（已重试一次）：界面显示的可能"
        +"不是服务端实际使用的去向；重新选择一次即可写入";
      setBotState("warn", "未能读取服务端「删除去向」设置，删除实际去向以服务端为准", null, false);
      toastErr("删除去向设置读取失败",
        "已重试一次仍失败：" + String((e&&e.message)||e));
    });
  };
  pullDelMode(1);
})();

/* 2e. 下载保存位置：留空 = 浏览器默认下载；填了 = 服务端直接落盘到该目录。
   权威值同样在服务端 settings.json（S.downloadDir 只是缓存），下载入口按它分叉。 */
(function setupDlDir(){
  const inp=$("#dlDirInp");
  let _dlSaving=false;
  const save=async()=>{
    if(!inp || _dlSaving) return;      // F2-P2-4：连点「保存」/连按 Enter 会并发多个 POST
    _dlSaving=true;
    const dlBtn=$("#dlDirSave"); if(dlBtn) dlBtn.disabled=true;
    const v=(inp.value||"").trim();
    try{
      const r=await api("/api/prefs",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({download_dir:v})});
      const got=String(r.download_dir||"").trim();
      /* F2-P2-4（2026-10-19）：服务端把路径**归一成空串**时，旧代码把它当成
         「用户想恢复浏览器默认下载」，弹一条 ok 提示并把输入框清空 ——
         实际是服务端**拒绝了这个路径**（不存在 / 不可写 / 非法字符），
         拒绝原因从未显示，用户以为自己改成功了。
         判据：用户确实填了东西（v 非空）而服务端回空 ⇒ 视为被拒绝。 */
      if(v && !got){
        S.downloadDir="";
        inp.value=v;                   // 保留用户的输入，别让他重打一遍
        try{ setBotState("warn", "下载目录未被服务端接受（路径不存在或不可写）", null, false); }catch(_){}
        toastErr("下载目录未被保存",
          "服务端没有接受这个路径（可能不存在、不可写或含非法字符）：" + v);
        return;
      }
      S.downloadDir = got;
      inp.value = S.downloadDir;
      /* 大图顶栏「下载」的说明书要立刻改成新的落点（用户改完设置可能马上回去下载）。 */
      if(window.refreshLbTips) refreshLbTips();
      toast(S.downloadDir ? "下载将保存到："+S.downloadDir : "已恢复浏览器默认下载", "ok", 5000);
    }catch(e){ toast("保存失败："+e.message,"err"); }
    finally{ _dlSaving=false; if(dlBtn) dlBtn.disabled=false; }
  };
  if(inp){
    if($("#dlDirSave")) $("#dlDirSave").onclick=save;
    inp.addEventListener("keydown",e=>{ if(e.key==="Enter") save(); });
  }
  // S.downloadDir 的初始化与 DOM 无关，必须无条件跑（下载入口依赖它）
  api("/api/info").then(j=>{
    S.downloadDir=j.download_dir||"";
    if(inp) inp.value=S.downloadDir;
    /* 首屏兜底文案是「系统「下载」文件夹」；真值回来得比大图打开晚时，
       这里补刷一次，别让说明书停在猜测上。 */
    if(window.refreshLbTips) refreshLbTips();
  }).catch(()=>{});
})();

/* 3b. 重启程序（后端 execv 原地重启；页面轮询等它回来再刷新） */
(function bindRestart(){
  const btn=$("#btnRestart"); if(!btn) return;
  let restarting=false;             /* ε-13（§6.6）：防重入 —— 数据目录切换后的
       「立即重启」走 window.mvRestart 直调，绕过工具栏按钮的 disabled 守卫；
       用户在等待期连点 toast 动作键 / 底栏动作条，会并发发多次 /api/restart，
       后端起出多个新进程抢同一端口。模块级标志一次重启只放行一发。 */
  const doRestart=async()=>{
    if(restarting) return;
    restarting=true;
    btn.disabled=true; setIcoLabel(btn, "refresh", "重启中…");
    toast("正在重启后端…","ok");
    /* P0-11（10-15 全代码审计，两线同款）：原 tick 第一件事是探 `/api/stats`，
       而**旧进程**在等待期一直是 200（它要等新进程就位才关监听）⇒ 第一次 tick
       （800ms 后）就无条件 `location.reload()`，排在它后面的 `restart_failed` 分支
       **永远不可达** ——「起新进程失败」被当成「重启成功」，页面刷新回一个没重启的界面。
       现在按后端自报的 `pid`（/api/info）判「换代」，并**先**看 restart_failed：
       只有「失败原因为空 且 pid 与重启前不同」才算新进程接管。
       ⚠️ 旧进程在「起新进程成功」后自己会 server_close + 退出，所以不能靠「/api/info 通」判代，
       必须靠 pid；而「起进程失败」时监听压根没关过，pid 不变、restart_failed 非空。 */
    let oldPid=null;
    try{
      const r0=await fetch("/api/info",{cache:"no-store"});
      if(r0.ok){ const j0=await r0.json(); oldPid=(j0 && j0.pid)||null; }
    }catch(_){}
    try{ await api("/api/restart",{method:"POST"}); }
    catch(e){
      /* F2-P2-2（2026-10-19）：原来是 `catch(_){}` —— 把「连接被重启掐断」和
         「服务端明确拒绝」混为一谈，一律进 90 秒轮询。期间按钮禁用、restarting=true
         封住 window.mvRestart，用户重试不了，界面只有一条「正在重启后端…」，
         90 秒后才等到一句「超时」。
         现在按抛出的异常形态分流：网络中断类（TypeError / 取消）继续轮询，
         其余（HTTP 500 / 403 / 超时）立刻恢复按钮并说明原因。 */
      const m = String((e && e.message) || e);
      const netish = (e instanceof TypeError)
        || /failed to fetch|network|load failed|请求已取消|NetworkError/i.test(m);
      if(!netish){
        restarting=false;
        btn.disabled=false; setIcoLabel(btn, "refresh", "重启程序");
        try{ setBotState("err", "重启请求被拒绝：" + m, null, false); }catch(_){}
        try{ toastErr("重启失败", m); }catch(_){ toast("重启失败：" + m, "err"); }
        return;
      }
      /* 网络类：连接可能已被重启掐断，继续走下面的 pid 轮询。 */
    }
    const t0=Date.now();
    const tick=async()=>{
      let info=null;
      try{
        const ir=await fetch("/api/info",{cache:"no-store"});
        if(ir.ok) info=await ir.json();
      }catch(_){}
      // ① 先判失败：起新进程失败时旧进程仍在本进程服务，并如实自报 restart_failed。
      // P1-34：旧实现只写日志，用户看到的是「点了重启界面就死了」。
      if(info && info.restart_failed){
        restarting=false;                 /* ε-13：放行下一次尝试 */
        btn.disabled=false; setIcoLabel(btn, "refresh", "重启程序");
        toast("重启失败："+info.restart_failed+"（程序仍在运行）","err");
        return;
      }
      // ② 再判换代：pid 变了才是新进程接管（拿不到旧 pid 的极罕见情况退回「能通即算」，
      //    与旧行为一致，不做更坏的假设）。
      if(info && info.pid && (oldPid==null || info.pid!==oldPid)){
        location.reload(); return;
      }
      if(Date.now()-t0>90000){
        restarting=false;                 /* ε-13：放行下一次尝试 */
        btn.disabled=false; setIcoLabel(btn, "refresh", "重启程序");
        toast("重启超时，请手动重新启动程序","err");
        return;
      }
      setTimeout(tick,600);
    };
    setTimeout(tick,800);
  };
  window.mvRestart=doRestart;        // 供其它模块调用（F28 数据目录切换后引导重启）
  btn.onclick=()=>{
    const dlg=bigDialog("重启程序");
    dlg.body.innerHTML=
      '<div style="font-size:calc(13px*var(--fsx));line-height:1.75">'
      +'后端进程会立刻重启（约 1~3 秒），期间页面短暂断开，重启完成后自动刷新。</div>'
      +'<div style="font-size:calc(12px*var(--fsx));color:var(--tx3);margin-top:6px">'
      +'若解析版本号有变化，启动后会重建一次提示词索引（约 10 秒）。</div>'
      +'<div style="display:flex;justify-content:flex-end;gap:calc(8px*var(--fsx));margin-top:calc(16px*var(--fsx))">'
      +'<button id="rstCancel">取消</button><button class="primary" id="rstGo">重启</button></div>';
    dlg.body.querySelector("#rstCancel").onclick=dlg.close;
    dlg.body.querySelector("#rstGo").onclick=()=>{ dlg.close(); doRestart(); };
  };
})();

/* 3. 快捷键说明：V17 起改为分类面板（原先是切换一行提示条的 display），
      入口 #hintTrig 仍保留，绑定统一在文件末尾的 setupKeysPanel() 里。 */

/* 4. 左侧栏：宽屏=折叠；≤900px=覆盖式抽屉（把手开合 + 遮罩关闭） */
const isNarrow=()=>window.matchMedia("(max-width:900px)").matches;
function closeNav(){ document.body.classList.remove("nav-open"); }

/* 宽屏收放（2026-10-21 三次优化：布局一次到位 + 卡片 FLIP）
   前两版各自留下的问题：
     ① 逐帧过渡 margin-left ⇒ 100 张卡每帧全量重排（实测 50ms 布局 / p95 18.7ms，
        contain / 锁列数 / content-visibility 三种降本手段都压不下来）；
     ② 动画期零布局、结束才提交 ⇒ 主区宽度单帧跳 260px，主观就是「滑完抖一下」。
   现在：点击当帧提交布局（一次重排），侧栏用 Web Animations 从「当前视觉位置」滑到终态，
   卡片同时 FLIP（从旧位置补到新位置，只动 transform）⇒ 视觉连续，且动画期零逐帧布局。
   窄屏抽屉是另一套机制（position:fixed + translateX），不经过这里。 */
let _navTimer=0, _navAnims=[];
/* 时长取 --dur-slow 令牌（位移已交给 WAAPI，读不到 transition-duration 了）；
   系统开「减弱动效」时直接 0 ⇒ 到位不动画。 */
function _navMs(){
  try{
    if(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return 0;
    const raw=(getComputedStyle(document.documentElement).getPropertyValue("--dur-slow")||"").trim();
    const m=/^([\d.]+)(m?s)$/.exec(raw);
    return m ? parseFloat(m[1])*(m[2]==="s"?1000:1) : 280;
  }catch(e){ return 280; }
}
const _NAV_EASE="cubic-bezier(.4,0,.2,1)";
function _navStop(){
  for(const a of _navAnims){ try{ a.cancel(); }catch(e){} }
  _navAnims=[];
}
/* 卡片 FLIP：先拉回旧位置再放开 —— 浏览器只做合成，不产生布局。
   ⚠️ 必须「先把所有新位置读完，再统一建动画」：一边 animate 一边 getBoundingClientRect，
      会让每张卡都重新布局一次（实测 85 次布局 / 27.8ms，CPU×4 下 362ms 长帧）。
   只处理可见区（屏幕外的卡瞬移看不出来），并把数量封顶，避免 4K 下挂出上百个合成层。 */
function _navFlip(olds,dur){
  const g=$("#grid");
  if(!g || !olds || !olds.length || !dur) return;
  const kids=g.children, lim=window.innerHeight+200;
  /* 上限按「可见行列 + 一行缓冲」自适应：写死数字在 4K 上会让屏幕内的卡瞬移。 */
  let cap=40;
  try{
    const c0=kids[0];
    const cw=Math.max(120,c0.offsetWidth), ch=Math.max(120,c0.offsetHeight);
    cap=Math.min(72, (Math.ceil(g.clientWidth/cw)+1)*(Math.ceil(window.innerHeight/ch)+1));
  }catch(e){}
  const moves=[];
  for(let i=0;i<kids.length && moves.length<cap;i++){
    const o=olds[i];
    if(!o || o.top>lim || o.bottom<-200) continue;   // 先用旧位置筛掉屏幕外的
    const el=kids[i];
    if(!el || !el.animate) continue;
    const n=el.getBoundingClientRect();
    const dx=Math.round(o.left-n.left), dy=Math.round(o.top-n.top);
    if(!dx && !dy) continue;
    moves.push([el,dx,dy]);
  }
  for(let i=0;i<moves.length;i++){
    const m=moves[i];
    _navAnims.push(m[0].animate(
      [{transform:"translate("+m[1]+"px,"+m[2]+"px)"},{transform:"none"}],
      {duration:dur,easing:_NAV_EASE}));
  }
}
function navFold(closing){
  const l=$("#left"), t=$("#leftTgl"), g=$("#grid");
  clearTimeout(_navTimer);
  const dur=_navMs();
  /* 起点一律取「当前视觉位置」：动画中途反向时 rect 已含没走完的位移，这样能无缝续上。 */
  const lOld=l.getBoundingClientRect().left;
  const tOld=t.getBoundingClientRect().left;
  const olds=(g && g.children.length)
    ? Array.prototype.map.call(g.children,el=>el.getBoundingClientRect()) : [];
  _navStop();
  t.innerHTML=ico(closing?"chevronRight":"chevronLeft");
  /* 当帧提交布局（这一次重排）：侧栏/把手/网格位置全部到位，再用 transform 补偿回原处放开。
     读新位置 → 建卡片补间 → 再建侧栏补间：动画对象必须在所有 rect 读完后再创建（见 _navFlip）。 */
  l.classList.toggle("collapsed", closing);
  const dx=Math.round(lOld-l.getBoundingClientRect().left);
  const tx=Math.round(tOld-t.getBoundingClientRect().left);
  l.classList.add("side-anim"); t.classList.add("side-anim");
  _navFlip(olds,dur);
  if(dur && dx){
    _navAnims.push(l.animate([{transform:"translateX("+dx+"px)"},{transform:"none"}],
      {duration:dur,easing:_NAV_EASE}));
  }
  if(dur && tx){
    /* 把手的平移量按 CSS 基线换算：终点仍是 0，视觉先钉回原处。 */
    _navAnims.push(t.animate([{transform:"translateX("+tx+"px)"},{transform:"none"}],
      {duration:dur,easing:_NAV_EASE}));
  }
  _navTimer=setTimeout(()=>{
    l.classList.remove("side-anim"); t.classList.remove("side-anim");
    _navAnims=[];
    /* 2026-10-31：侧栏收放改了网格宽度 ⇒ 列宽变 ⇒ 离屏卡片的 CV 尺寸记忆要刷。
       放在补间结束之后（补间还没走完时重排会把 FLIP 的卡片定到旧位置）。 */
    if(window.mvCvRelayout) window.mvCvRelayout(60);
  }, dur+60);
}
/* 动画期用户一滚动就把补间收掉（否则卡片会被定格在旧位置）。
   UI-03（2026-10-23）：同一个监听器转调 mvGridScroll()（文件名显形 + 位置刻度）——
   不新建第二个 scroll 监听，滚动是热路径。 */
$("#gridwrap").addEventListener("scroll",()=>{
  if(_navAnims.length) _navStop();
  if(typeof mvGridScroll==="function") mvGridScroll();
},{passive:true});
function toggleNav(){
  if(isNarrow()){ document.body.classList.toggle("nav-open"); return; }
  /* collapsed 现在是**当帧**提交的，所以它本身就代表最新的意图目标：
     展开态（无 collapsed）/ 收起动画未走完（已有 collapsed）再点 = 收起；反之 = 展开。 */
  navFold(!$("#left").classList.contains("collapsed"));
}
$("#leftTgl").onclick=toggleNav;
$("#navBack").onclick=closeNav;
/* C-P4：rAF 节流 —— 这里每次 resize 都会调 isNarrow()（读 innerWidth，强制样式重算） */
window.addEventListener("resize",(window.mvThrottleRaf||(f=>f))(()=>{
  if(!isNarrow()){ closeNav(); return; }
  /* 宽屏动画中途缩到窄屏：必须收掉补间（WAAPI 的 transform 会跟抽屉的 translateX(-103%) 打架），
     否则卡片会停在半路。动画取消后元素自动回到 CSS 终态，不需要额外纠正位置。 */
  const l=$("#left"), t=$("#leftTgl");
  if(l.classList.contains("side-anim")){
    clearTimeout(_navTimer); _navTimer=0;
    _navStop();
    l.classList.remove("side-anim");
    t.classList.remove("side-anim");
  }
}));
// 窄屏下点导航项/常用目录（不含下拉按钮）后自动收起抽屉
$("#left").addEventListener("click",e=>{
  if(!isNarrow()) return;
  if(e.target.closest("#rootBtn")) return;
  if(e.target.closest(".litem")||e.target.closest(".rs-btn")) closeNav();
});

/* 5. 左栏:位置导航(当前路径层级;子文件夹跳转走网格目录卡/面包屑,不在此列冗余) */
/* 2026-11-16：「位置」区标题右侧的当前目录张数。
   背景：左栏「库」的数字全部是**全库**口径（nav_stats()，根目录含子目录），
   切换目录本来就不该变；而紧邻的「格式」区是当前结果集口径、会跟着目录变 ⇒
   同一列里两套口径，用户实测提问「切目录后库的数字不变」。
   处理：库区标题标「全库」，当前目录的张数改由本函数落在「位置」区标题上。
   口径与网格同源（S.items，/api/list 按「含子文件夹」开关返回），不带任何筛选 ——
   搜索 / 标签 / 格式勾选都不影响它，避免和「格式」区的可筛张数混淆。
   跨目录视图（收藏 / 最近使用 / 已标记）下 S.items 不是本目录内容，一律不显示。 */
let _prevPosCnt = "";
function syncPosCnt(){
  const pc=$("#posCnt"); if(!pc) return;
  const root=S.root||"", cur=S.path||"";
  const crossView = favMode || libMode==="recent" || libMode==="marked";
  let txt="", tip="";
  if(root && cur && normPath(cur)!==normPath(root) && !crossView){
    txt="当前 "+(S.items||[]).length+" 项";
    tip="当前目录里的媒体张数（跟随「含子文件夹」开关，不受搜索与筛选影响）。\n"
       +"左栏「库」那组数字是全库口径，切换目录不会变。";
  }
  if(txt===_prevPosCnt && pc.textContent===txt) return;   // 无变化不碰 DOM
  _prevPosCnt=txt;
  pc.textContent=txt;
  if(tip) pc.setAttribute("data-tip", tip); else pc.removeAttribute("data-tip");
}

function buildSideNav(){
  const nav=$("#sideNav"); if(!nav) return;
  syncPosCnt();                       // 各分支（顶层 / 子目录 / 换根残留）都要跟着复位
  const root=S.root||"", cur=S.path||"";
  const btn=$("#rootBtn");
  const hint=(btn && btn.querySelector(".rs-hint")) || null;   // A 方案：根目录总数落点
  /* ⚠️ /api/list 返回**之前** S.root 是空串，而 loadStats()（统计先到一步）会调到这里 ——
     此刻 root=cur="" ⇒ cur===root 成立 ⇒ 走进「顶层」分支，画出一行没有名字、没有
     data-tip 的幽灵行（诊断 4 实测 t=2.00s 采样：data-p="" data-tip="" .nm=""）。
     用户侧表现为左栏闪一下空白条目；门禁侧表现为 B1 探针偶发假红（正好落在窗口内）。
     根路径未知时先空着，等已知路径的那次重建（同 buildLibNav 的「…」占位思路）。 */
  if(!root){ nav.innerHTML=""; if(hint) hint.textContent=""; return; }
  /* A 方案（2026-10-07 · 用户要求）：**顶层不再画根行**。
     那一行与下方 #rootBtn 显示的是同一个根名（output），加上顶栏面包屑就是三处 ⇒
     用户反馈「一排好几个 output，我都搞不清具体干什么的」。改由 #rootBtn 单独承担
     「根名 + 总数」，点它仍是**切换根目录**的唯一入口。
       · 总数走 S.stats.total（nav_stats() 的全局口径，与左栏「库 · 全部」同源），
         而不是 S.view.length（那是筛选后的当前页）。
       · 统计未到位时不写（同 buildLibNav 的 `…` 占位思路，避免先闪一个假的 0）。
     ⚠️ 只在**真在顶层**（cur===root）时带张数 —— 进了子目录后用户看的是子目录内容，
        再挂根目录总数会误导（首轮门禁 B2 的同类教训：那一行曾错挂当前目录张数）。 */
  /* F2-P2-7（2026-10-19）：这里原来用**严格字符串相等**判「是否在顶层」，
     再用 `cur.slice(root.length)` 定长切段。两者都假定 S.path 与 S.root 形态完全一致。
     实际会不一致的场合：大小写（`G:/Output` vs `G:/output`）、分隔符（`/` vs `\`）、
     换根后 S.root 已更新而 S.path 还是旧根残留。
     后果分两种：长度一致 ⇒ 段名虽对但 acc 用 root 重拼；长度不一致 ⇒ 切出无意义片段，
     `data-p` 指向不存在/越权的路径。现在统一按 normPath 比语义，并先断言前缀。 */
  const _nr = normPath(root), _nc = normPath(cur);
  if(_nc===_nr){
    nav.innerHTML="";
    const totN=(S.stats && typeof S.stats.total === "number") ? S.stats.total : null;
    /* B7-P1-1：根目录读不到时 total 是 0（不是「真的空了」）—— 「0 项」会被读成图没了。 */
    const _ce=!!(S.stats && S.stats.census_error);
    if(hint) hint.textContent = _ce ? "—" : ((totN===null) ? "" : (totN+" 项"));
    return;
  }
  if(hint) hint.textContent="";
  /* 形态不一致且 S.path 不在 S.root 之下（换根残留）⇒ 无法构造可信层级，按顶层处理。
     宁可少一行导航，也不能给出指向错误目录的 data-p（点下去会「目录不存在或在根目录之外」）。 */
  if(_nc.indexOf(_nr) !== 0){
    try{ console.warn("[sidenav] S.path 不在 S.root 之下，按顶层处理", {root:root, path:cur}); }catch(_){}
    nav.innerHTML="";
    return;
  }
  /* 子目录：保留完整层级（含根行）—— 这一行此刻是「点一下回到根目录」的导航项，
     与 #rootBtn 的「切换根目录」职责不同；且窄屏下 #crumbs 被 responsive.css 隐藏时，
     这里是唯一的路径导航，所以这一分支**不能省**（方案 C「直接砍掉」因此被否）。
     E5-1：根名走 rootDisplayName —— UNC 根（`\\NAS\photos`）要显示整段，否则看不出在哪台机器上。 */
  const rn=(rootDisplayName(root)||root);
  let h='<div class="litem root" data-p="'+esc(root)+'" data-tip="'+esc(root)+'">'
    +'<span class="ic">'+ico("dir")+'</span><span class="nm">'+esc(rn)+'</span>'
    +'<span class="hint">根目录</span></div>';
  /* 切出「相对部分」：优先按原串切（保留真实大小写，段名显示才正确）；
     形态不一致时退回用 normPath 的偏移切 —— 长度一定对得上。 */
  const _relSrc = (String(cur).toLowerCase().indexOf(String(root).toLowerCase()) === 0)
    ? cur.slice(root.length)
    : _nc.slice(_nr.length);
  const parts=_relSrc.replace(/^[\\/]/,"").split(/[\\/]/).filter(Boolean);
  let acc=root;
  parts.forEach((seg,idx)=>{
    acc=acc.replace(/[\\/]+$/,"")+"\\"+seg;
    const last = idx===parts.length-1;
    const pad = Math.min(20+idx*13, 62);          // 逐级缩进，太深就封顶
    h+='<div class="litem seg'+(normPath(acc)===_nc?" on":"")+'" data-p="'+esc(acc)+'"'
      +' style="padding-left:calc('+pad+'px*var(--fsx))" data-tip="'+esc(acc)+'">'
      +'<span class="ic">'+(last?"└":"├")+'</span><span class="nm">'+esc(seg)+'</span></div>';
  });
  nav.innerHTML=h;
  nav.querySelectorAll(".litem").forEach(el=>el.onclick=()=>load(el.dataset.p));
  const on=nav.querySelector(".litem.on");        // 路径很深时把当前项滚进视野
  if(on && on.scrollIntoView) on.scrollIntoView({block:"nearest"});
}

/* 6. 左栏:收藏分类入口 */
function buildFavNav(){
  const nav=$("#favNav"); if(!nav) return;
  const totalFav = S.stats ? S.stats.favorites : null;
  /* F2-P2-6（2026-10-19）：这两处是本区间**唯一**「服务端来源 + 未 esc + 进 innerHTML」
     的拼接点（同文件 buildWfNav 的 `n`、buildLibNav 的 `cnt` 早已 `esc(String(...))`）。
     当前 `c.count`/`favorites` 都是 JSON 数值，所以不可利用；但一旦后端把它变成字符串
     （带单位的 `1.2k`、错误时的 `-`），任何 `<` / `"` 都会直接进 DOM。
     统一走 `esc(String(...))`，并且「只渲染数字形态」，非数字一律不渲染计数。 */
  const _cnt = (v) => (typeof v === "number" && isFinite(v)) ? esc(String(v)) : "";
  let h='<div class="litem" data-cat="*"><span class="ic">'+ico("starFill","filled")+'</span><span class="nm">全部收藏</span>'
    +(totalFav===null?"":'<span class="cnt">'+_cnt(totalFav)+'</span>')+'</div>';
  for(const c of catList())
    h+='<div class="litem" data-cat="'+esc(c.name)+'" data-tip="'
      +(c.name==="默认"?"兜底分类（未分类的收藏都在这里）":"右键：重命名 / 删除")+'">'
      +'<span class="ic">'+ico("star")+'</span><span class="nm">'+esc(c.name)+'</span>'
      +(c.count?'<span class="cnt">'+_cnt(c.count)+'</span>':"")+'</div>';
  nav.innerHTML=h;
  nav.querySelectorAll(".litem").forEach(el=>{
    const cat=el.dataset.cat||"*";
    el.onclick=()=>{
      /* H2（2026-10-07）：分类状态已从 `#favcat` 外迁到 `S.filters.favcat`。 */
      // 再点一次当前已选中的分类（含「全部收藏」）= 取消收藏夹视图，退回目录列表
      if(favMode && (S.filters.favcat||"*")===cat){ load(S.path||S.root); return; }
      /* F5-P2-8（2026-10-20）：改走 `setFavCatFilter` —— 它是 favcat 的唯一写入口
         （内部会推筛选控件、并在 favMode 下调 loadFavView）。直接写 S.filters.favcat
         再手动调 loadFavView，等于绕开契约、让 setter 的分支变成死代码。 */
      setFavCatFilter(cat==="*" ? "*" : cat, true);   // true = 意图是进入/切换收藏夹视图
      // 高亮
      nav.querySelectorAll(".litem").forEach(x=>x.classList.remove("on"));
      el.classList.add("on");
    };
    /* 2026-11-08：分类项右键 = 重命名 / 删除。
       「全部收藏」不是分类（没有可改的名字、也删不掉），不给菜单。 */
    if(cat!=="*") el.oncontextmenu=e=>{
      e.preventDefault(); e.stopPropagation();
      favCatMenu(e.clientX, e.clientY, cat);
    };
  });
  /* H3（2026-10-06）顺带修复 ——「进了收藏夹，左栏高亮闪一下就没」：
     本函数用 innerHTML 整片重写 #favNav，会把 .on 一起抹掉；而下方 _lightTick
     （350ms 轮询）只要感知到 favMode 翻转就重建这一区（`p=(S.path||"")+(favMode?"·fav":"")`
     变了），于是点击时同步加上的高亮在几百毫秒后被抹掉、且此后不再恢复。
     探针实测：点击后 50ms .on 在 → 200ms 已被 MutationObserver 记录的重建抹除。
     ⇒ 渲染完立刻按当前状态补一次高亮，让**所有**重建路径都自带正确高亮。 */
  syncFavNavSel();
}

/* 收藏分类高亮：跟随 favMode 与当前分类（离开收藏夹视图时自动清除，避免「全部收藏」一直亮着） */
function syncFavNavSel(){
  const nav=$("#favNav"); if(!nav) return;
  const cur=S.filters.favcat||"*";            // H2：状态外迁后不再读 `#favcat`
  nav.querySelectorAll(".litem").forEach(el=>{
    el.classList.toggle("on", favMode && (el.dataset.cat||"*")===cur);
  });
}

/* 6b-0. 左栏「工作流 / 格式」分区（内容区不再放重复的类型/格式 chips：
   类型走「库」，格式走「格式」分区，内容区工具条只留视图相关控件） */
const FMTS = new Set();                         // 格式筛选（可多选，空集=全部）
let WFK = "";                                   // 工作流类型：""=全部 / t2i / i2i / cn / other
const fmtLabel = ext => String(ext).replace(".", "").toUpperCase();
/* F1 P2-3：计数改用**当前结果集**口径（filters.applyFilter 里现算，见 state.js 注释）。
   S.stats.formats 是根目录全树的全局口径，在子目录/收藏/最近视图里与可筛结果对不上。 */
const fmtCounts = () => FMT_COUNTS || {};
const fmtKeys = () => Object.keys(fmtCounts()).sort((a, b) => fmtCounts()[b] - fmtCounts()[a]);
/* F1 P2-3 口径 = 「当前结果集」里该格式**可筛**的张数（勾上后能看到的数）。
   P0-8（审计第 4 级）：提示词搜索现也走 filters.js 的 `_applyCommonFilters` 里
   同一段 FMT_COUNTS 统计 ⇒ 左栏数字在搜索态同样自洽，不再停留在上一次目录筛选的数。 */
function recountFmt(v) {
  FMT_COUNTS = {};
  for (const it of (v || [])) {
    if (it.type === "dir") continue;
    const e = (it.path.match(/\.[^.\\/]+$/) || [""])[0].toLowerCase();
    if (e) FMT_COUNTS[e] = (FMT_COUNTS[e] || 0) + 1;
  }
}

function setFmt(f) {                             // 空串 = 清空（全部格式）
  if (!f) FMTS.clear();
  else if (FMTS.has(f)) FMTS.delete(f);
  else FMTS.add(f);
  applyFilter();
}

function buildFmtNav() {
  const nav = $("#fmtNav"); if (!nav) return;
  const c = fmtCounts();
  const keys = fmtKeys();
  if (!keys.length) { nav.innerHTML = '<div class="lchk" style="cursor:default">暂无数据</div>'; return; }
  nav.innerHTML = keys.map(k =>
    `<label class="lchk${FMTS.has(k) ? " on" : ""}" data-tip="${esc(fmtLabel(k))}（可多选；数字为当前视图可筛张数）">`
    + `<input type="checkbox" data-f="${esc(k)}"${FMTS.has(k) ? " checked" : ""}>`
    + `<span class="nm">${esc(fmtLabel(k))}</span>`
    + `<span class="cnt">${c[k]}</span></label>`).join("");
  nav.querySelectorAll("input[data-f]").forEach(inp => inp.onchange = () => setFmt(inp.dataset.f));
}

function buildWfNav() {
  const nav = $("#wfNav"); if (!nav) return;
  const c = (S.stats && S.stats.wfk) || {};
  const total = S.stats ? (c.t2i || 0) + (c.i2i || 0) + (c.cn || 0) + (c.other || 0) : null;
  const rows = [["", "grid", "全部", total, "不按类型细分（含无生成参数的媒体）"],
                ["t2i", "write", "文生图", c.t2i, "纯提示词出图（latent 来自 EmptyLatent）"],
                ["i2i", "image", "图生图", c.i2i, "有输入图片（latent 来自 VAEEncode / LoadImage）"],
                ["cn", "cn", "ControlNet", c.cn, "工作流里用了 ControlNet 节点"],
                ["other", "dot", "其他 / 无生成参数", c.other, "认不出工作流类型，或没有生成参数"]];
  nav.innerHTML = rows.map(([k, ic, nm, n, tip]) =>
    `<div class="litem${WFK === k ? " on" : ""}" data-w="${k}" data-tip="${esc(tip)}">`
    + `<span class="ic">${ico(ic)}</span><span class="nm">${nm}</span>`
    + `<span class="cnt">${n == null ? "…" : n}</span></div>`).join("");
  nav.querySelectorAll(".litem").forEach(el => el.onclick = () => setWfk(el.dataset.w));
}

function setWfk(k) {
  WFK = k || "";
  applyFilter();                                // 类型筛选走提示词索引里的 wfk（无需进入工作流主视图）
}

/* P1-8：清空格式 + 工作流类型筛选。
   「清除全部筛选」以前漏了这两个，结果是**筛空之后点清除、网格还是空的**——
   看起来像按钮坏了（左栏分区还可折叠，收起时连线索都没有）。
   统一从这里清，保证「清全部」语义完整。 */
function clearFmtWfk() {
  const had = FMTS.size > 0 || !!WFK;
  FMTS.clear();
  WFK = "";
  /* ⚠️ 这里**不能**调用 applyFilter()：它函数体里会读 FMTS/WFK（filters.js 的 _sig 计算），
     而它被 app.js 的 load() 在「换目录」分支里调用时，data 与筛选状态正在重建，
     提前重算会拿到半成品状态。刷新的责任在调用方：
       · filters.js 的 clearAllFilters() —— 清完自己调 applyFilter()
       · app.js 的 load() —— 清完自己调 applyFilter()
     两边都不许省，否则「点了清除网格还是空的」。 */
  if (had) { buildFmtNav(); buildWfNav(); }      // 勾选态/高亮态跟着归位
  return had;
}

var LAST_PAGER = null;          // 最近一次 renderPager 的 (total, pages)，供 resize / 改字号后重排槽位

/* ---------- 分页条槽位：按底栏实际可用宽度算，不写死个数 ----------
   ① 底栏宽 - 两侧预留（--pg-side，与 CSS 的 #pager max-width 同源，常量只有一份）
   ② 再减掉 ‹ › 两个箭头及其间距
   ③ 取奇数，下限 5、上限按屏宽分 15 / 21 / 31：偶数会让「当前页」不在正中；太少没得挑，
      太多是视觉噪音（真要跳去远处，用右侧「跳至」输入框更快）
   槽位数只随「窗口宽度 / 字号缩放」变化，不随翻页变化 ——
   所以翻页时整行宽度仍然恒定，不会左右抖（这是旧版固定 7 个槽位的初衷，
   现在改成「按屏算个数」，把宽屏白白空着的几千像素用起来）。 */
function pagerSlots(){
  const bot=$("#bot"); if(!bot) return 7;
  const cs=getComputedStyle(document.documentElement);
  const fsx=parseFloat(cs.getPropertyValue("--fsx"))||1;
  const gap=8*fsx;                        // 底栏 / #pager 的 gap（--sp-2）
  const slotW=26*fsx+4*fsx;               // 单槽宽 + .pgnums 内部的 gap（--sp-1）
  const navW=2*(24*fsx)+2*gap;            // ‹ › 两个箭头 + 两侧间距
  const wOf=sel=>{ const e=bot.querySelector(sel); return e ? e.getBoundingClientRect().width : 0; };
  const left=wOf("#botState");                                   // 左侧「已就绪 / N 项」
  const right=wOf(".pgjump")+wOf("#perPage")+wOf("#hintTrig");   // 右侧跳页框 / 每页数量 / 快捷键
  const pg=$("#pager");
  let avail;
  if(pg && getComputedStyle(pg).position==="absolute"){
    /* 宽屏：#pager 绝对定位居中，两侧固定件不参与它的排布 ——
       居中意味着左右各留 (W-宽度)/2，所以只要它 ≥ **较宽的那一侧**就不会压住，
       而不是「两侧宽度之和」（旧算法按和算，右侧一堆控件时会把页码挤宽到重叠）。 */
    avail=bot.clientWidth-2*(Math.max(left, right)+2*gap+32*fsx);
  }else{
    /* 窄屏（≤620px，见 responsive.css）：#pager 改为 flex 子项，两侧各占自己那份 */
    avail=bot.clientWidth-left-right-4*gap-16*fsx;
  }
  let n=Math.floor((avail-navW)/slotW);
  if(n%2===0) n-=1;                       // 取奇数：当前页才在正中
  /* 上限随底栏宽度分级：4K 一行能舒舒服服放下 30+ 个，只给 20 个等于继续浪费；
     但也不设成无上限 —— 页码太多反而难扫视，跳到远处用右侧「跳至」框更快。 */
  const cap = bot.clientWidth >= 2400 ? 31 : bot.clientWidth >= 1600 ? 21 : 15;
  return Math.max(5, Math.min(cap, n));
}

/* 页码序列：长度恒等于 slots（总页数不够填满时就是 pages 个）。
   贴左：1 2 3 4 … 末页 ／ 中间：1 … c-1 c c+1 … 末页 ／ 贴右：1 … 末页-1 末页
   三个分支长度都等于 slots，所以翻页时槽位个数不变、整行宽度不变。 */
function pageSeq(cur, pages, slots){
  if(pages<=slots){ const a=[]; for(let n=1;n<=pages;n++) a.push(n); return a; }
  const mid=slots-4, half=(mid-1)>>1;     // 两个省略号都在场时，中间数字的个数
  const s0=cur-half, e0=s0+mid-1;
  if(s0<=2){                              // 贴左：前 slots-2 个数字 + 省略号 + 末页
    const a=[]; for(let n=1;n<=slots-2;n++) a.push(n);
    a.push(null, pages); return a;
  }
  if(e0>=pages-1){                        // 贴右：首页 + 省略号 + 末尾 slots-2 个数字
    const a=[1, null];
    for(let n=pages-(slots-2)+1;n<=pages;n++) a.push(n);
    return a;
  }
  const a=[1, null];                      // 中间：首页 + 省略号 + 窗口 + 省略号 + 末页
  for(let n=s0;n<=e0;n++) a.push(n);
  a.push(null, pages);
  return a;
}

/* 6b-1. 底栏分页（客户端切页；整表已在 applyFilter 里排好序） */
function setPage(n){
  const p=Math.max(1, parseInt(n,10)||1);
  if(p===PAGE) return;
  PAGE=p; applyFilter();
  const gw=$("#gridwrap"); if(gw){
    if(typeof mvScrollProg==="function") mvScrollProg();   // UI-03：翻页回顶是程序化滚动
    gw.scrollTop=0;
  }
}
function renderPager(total, pages){
  const box=$("#pager"); if(!box) return;
  LAST_PAGER=[total, pages];                   // 记下来：窗口缩放 / 改字号后按新宽度重排
  if(FLOW){ box.innerHTML=""; return; }        // 无限瀑布流：不分页，底栏页码区留空
  const cur=Math.min(PAGE, pages);
  const btn=(n,label,dis,cls)=>
    `<button data-p="${n}"${dis?" disabled":""}${cls?` class="${cls}"`:""}>${label}</button>`;
  const GAP='<span class="gap">…</span>';
  const parts=pageSeq(cur, pages, pagerSlots());
  /* 结构：[‹] [页码（可横向滚动）] [›]
     左右箭头是 .pgnums 的兄弟节点而不是列表首尾 —— 页码很多时箭头固定不动，
     也不会紧挨着「1 / 尾页」导致误点。 */
  const inner=parts.map(n=>n===null?GAP:btn(n,String(n),false,n===cur?"on":"")).join("");
  /* 批次 B：箭头从「‹ / › 字符」改走 ICO（chevronLeft / chevronRight）——
     字符箭头在不同 Windows 字体下字形差异明显，且是全站唯一不跟 ICO 体系的导航箭头。 */
  box.innerHTML = btn(cur-1,ico("chevronLeft"),cur<=1,"pgnav")
    + '<span class="pgnums">'+inner+'</span>'
    + btn(cur+1,ico("chevronRight"),cur>=pages,"pgnav");
  box.querySelectorAll("button[data-p]").forEach(b=>b.onclick=()=>setPage(+b.dataset.p));
  /* 跳页输入框已挪到 #bot 静态结构（右侧与「每页数量」并排），这里只同步范围与当前值 */
  const jump=$("#pgJump"), jt=$("#pgTotal");
  if(jt) jt.textContent=pages;
  if(jump && document.activeElement!==jump){ jump.max=pages; jump.value=cur; }
  const onNum=box.querySelector(".pgnums .on");        // 当前页滚进可视区（页码多时）
  if(onNum && onNum.scrollIntoView) onNum.scrollIntoView({block:"nearest",inline:"center"});
  const sel=$("#perPage"); if(sel && String(PER)!==sel.value) sel.value=String(PER);
  /* 条目总数只显示在顶栏 #count 一处（此前底栏 #botTotal 是同一个数字的重复显示） */
}
/* 窗口尺寸变化 -> 槽位数重算（只重排分页条，不重绘网格）。
   改字号（--fsx）不触发 resize，所以 applyFs 里也调一次。 */
(function bindPagerResize(){
  let t=null;
  /* C-P4：rAF 节流 + 原有 120ms 去抖叠加（原本只有后者 —— 拖窗口时仍会每 120ms
     重算一次分页条，且重算前每次都先 clearTimeout 再排，等于按 120ms 节流）。 */
  const onResize = (window.mvThrottleRaf || (f => f))(()=>{
    if(!LAST_PAGER || FLOW) return;
    clearTimeout(t);
    t=setTimeout(()=>{ if(LAST_PAGER) renderPager(LAST_PAGER[0], LAST_PAGER[1]); }, 120);
  });
  window.addEventListener("resize", onResize);
})();
(function bindPerPage(){
  const sel=$("#perPage"); if(!sel) return;
  sel.value=String(PER);
  sel.onchange=()=>{
    PER=parseInt(sel.value,10)||20;
    savePref("mv.per", String(PER));
    PAGE=1; applyFilter();
  };
})();
/* 跳页输入（#bot 静态框）：回车直达，非法输入回弹当前页 */
(function bindJump(){
  const jump=$("#pgJump"); if(!jump) return;
  jump.onkeydown=e=>{
    if(e.key!=="Enter") return;
    const pages=parseInt(jump.max,10)||1, v=parseInt(jump.value,10);
    if(v>=1 && v<=pages) setPage(v);
    else jump.value=PAGE;
  };
  jump.onblur=()=>{ jump.value=PAGE; };
})();

/* 6b. 左栏:库数量（全部/图片/视频/工作流）——统一取 /api/stats 的 nav 口径 */
function refreshStatsSoon(){
  clearTimeout(statsTimer);                        // 收藏/标签/删除连点时合并成一次请求
  statsTimer=setTimeout(()=>loadStats(), 400);
}
/* 索引构建期，统计读取连续失败几次后彻底停机（免无限请求） */
const STATS_RETRY_MAX=3;
/* 2026-11-05 P2：点过「重建索引」但还没确认建完的标记（见 loadStats 的 else 分支）。 */
let reindexPendingAt = 0;
async function loadStats(){
  const tok=++statsTok;
  let j;
  try{ j=await api("/api/stats"); }
  catch(e){
    /* P1（2026-10-09）：失败不能裸 return —— 续轮询的 setTimeout 只写在下面的
       **成功分支**里，一次失败轮询链就永久断开；底栏还挂着上一轮写入的 sticky
       busy（sticky 不走 4.2s 自动回落）「建立提示词索引 1234/9700」，用户以为
       还在建（实际可能早已失败或早已建完）。失败本身也完全静默：无 toast、
       无 warn、无重试 —— 与下方 pullDelMode 的治理标准（失败重试 + 明确反馈）
       正好不一致。现在：构建预算内自动重试；重试用尽或不在构建期则落非 sticky
       的 warn，不再假装还在进行。 */
    if(tok!==statsTok) return;                     // 已有更新的请求，丢弃
    loadStats._fails=(loadStats._fails||0)+1;
    const _building=!!(S.stats && S.stats.index_building);
    if(!statsBuildAt) statsBuildAt=Date.now();     // 失败也起预算，免得无限重试
    if(_building && loadStats._fails<=STATS_RETRY_MAX
       && Date.now()-statsBuildAt<120000){
      setBotState("warn","统计读取失败，索引进度暂时未知（自动重试中）",null,true);
      setTimeout(()=>{ if(tok===statsTok) loadStats(); }, 2000);
      return;
    }
    setBotState("warn","统计读取失败，底栏数字可能不是最新的",null,false);
    return;
  }
  if(tok!==statsTok) return;                       // 已有更新的请求，丢弃
  loadStats._fails=0;                              // 成功即清零连续失败计数
  if(j.mig) mvMigrateReport();                     // 迁移完成待领取：随 stats 捎信，取走即清（P4，V2.12.1）
  S.stats=j.nav||null;
  /* B7-P1-1（2026-10-17）：根目录读不到时左栏显示「—」，但只有一个破折号容易被当成
     界面坏了 —— 首次出现时把原因说清楚（同一条错误只提示一次；盘/权限恢复后复位，
     下次再掉线还会提醒）。 */
  const _ce=(S.stats && S.stats.census_error)||"";
  if(_ce && _ce!==loadStats._censusErr){
    loadStats._censusErr=_ce;
    toast(_ce+"；左栏数量暂时显示为「—」，盘符或权限恢复后会自动回来","warn",10000);
  }else if(!_ce){
    loadStats._censusErr="";
  }
  if(S.stats && S.stats.mark_map){
    MARK_MAP=S.stats.mark_map;                     // 卡片角标/大图下拉用
    /* ⚠️ 别整表覆盖到「本地刚写的标记」头上：这份 mark_map 是**一次扫描的快照**，
       而统计在索引构建期每 2s 轮询一次 ⇒ 晚到的旧快照会把刚写入的标记抹掉，
       紧接着下面的 syncAllCardMarks() 摘掉角标 —— 2026-09-17 实测表现是
       「标记完角标闪一下不见了，过一会儿又回来」，且会让 v4_batch1_check §② 间歇性红
       （门禁 `tests/mark_badge_check.py` 已确定性取证：注入一份不含该路径的 stats 响应 ⇒
       MARK_MAP 掉键 + 角标消失；真实响应回来 ⇒ 恢复）。
       ⚠️ 判据是「**确认收敛**」，不是墙钟宽限期（那版 2026-09-17 当天就被 §② 的间歇红打回）：
       宽限期固定 3s，可是一次 loadStats 的返回时刻由网络与主线程决定，定时器被节流时会晚到
       远超 3s ⇒ 宽限期一过期，迟到的旧快照照样抹掉标记（batch1 在回归里红、单独跑绿，就是
       窗口被拉长那几次）。改成：**只要快照还没反映出这次本地写入，就继续以本地为准；一旦
       快照出现了它（标记 = 同值 / 取消 = 键不在）立刻交还服务端**。覆盖时长由「服务端何时
       读到」决定，与墙钟无关。MARK_TTL_MS 只在另一端改了同一张时兜底。*/
    const _now=Date.now();
    for(const [p,v] of MARK_LOCAL){
      const caughtUp = v.st ? MARK_MAP[p] === v.st : MARK_MAP[p] === undefined;
      if(caughtUp || _now-v.t > MARK_TTL_MS){ MARK_LOCAL.delete(p); continue; }
      if(v.st) MARK_MAP[p]=v.st; else delete MARK_MAP[p];
    }
  }
  buildLibNav();
  buildFavNav();                                   // 收藏分类计数与「全部收藏」同步刷新
  /* 格式 / 工作流类型的计数同样来自 /api/stats，而首次 buildFmtNav 是在统计到达
     之前（applyFilter 里）跑的，那时 S.stats 还是空 —— 不补这一下，首屏「格式」
     会一直显示「暂无数据」。只在内容真的变了才重建：索引构建期 loadStats 每 2s
     轮询一次，无脑重建会把用户正在点的复选框冲掉。 */
  /* F1 P2-3：格式计数据换成 FMT_COUNTS（现算，不再等 /api/stats）⇒ 这里跟着换，
     否则统计到达后会误判「内容变了」把用户正在点的复选框冲掉。 */
  const _sig=JSON.stringify(FMT_COUNTS||{})
    +"|"+JSON.stringify((S.stats&&S.stats.wfk)||{})
    +"|"+((S.stats&&S.stats.index_building)?1:0)+"|"+Array.from(FMTS).sort().join(",");
  if(_sig!==(loadStats._sig||"")){ loadStats._sig=_sig; buildFmtNav(); buildWfNav(); }
  syncAllCardMarks();                                          // 已渲染的卡片补上角标（单次遍历）
  /* V30（D3）：左栏「位置」顶层那行的张数取自 S.stats.total —— 而首次 buildSideNav()
     发生在统计到达**之前**，此后只有「路径 / 收藏 / 筛选变化」才重建（下方 350ms 轮询），
     ⇒ 数字一直空缺（首轮门禁 B1 抓到：total=240 已到位，那行却仍是旧文案）。
     这里按 total 变化补一次重建：变了才调，避免索引构建期每 2s 轮询都冲掉侧栏 DOM。 */
  if((S.stats ? S.stats.total : null) !== loadStats._lastTotal){
    loadStats._lastTotal = S.stats ? S.stats.total : null;
    buildSideNav();
  }
  if(S.stats && S.stats.index_building){           // 索引在建：工作流数先显示「…」，建完回填
    const _d=S.stats.index_done||0, _t=S.stats.index_total||0;
    /* F1-P1-1：目录正在加载时底栏让位（LOAD_BUSY 由 app.js 的 _loadBusy 维护）——
       否则慢盘读一次目录的几秒里，每 2 秒一次的进度轮询会把「正在加载目录…」抢走。 */
    if(!LOAD_BUSY)
      setBotState("busy", _t ? ("建立提示词索引 "+_d+"/"+_t) : "正在扫描生成参数…", null, true);   // V3
    /* §6.1：预算原按「调用次数」算（statsPolls++ < 40），而**任何一次** loadStats 都吃一格 ——
       用户在索引构建期切页 / 改筛选 / 拖时间轴几次就能把 40 格吃完，轮询提前停止，
       进度条卡在旧数字（索引其实还在后台建，界面却不再报数）。
       改成按**墙钟**算：以首次进入构建态的时刻为基准，120s 封顶，手动调用不吃预算。 */
    const _now = Date.now();
    if(!statsBuildAt) statsBuildAt = _now;
    if(_now - statsBuildAt < 120000)               // 最多轮询 ~120s，避免异常时无限请求
      setTimeout(()=>{ if(tok===statsTok) loadStats(); }, 2000);
    else if(!LOAD_BUSY) setBotState("warn","索引仍在后台构建",null,false);   // 停轮询后别再挂着「进行中」
  }else{
    /* 2026-11-05 P2 探针（4.2-3）：`statsBuildAt` 只在**见过一次 building=true** 时
       才置位。小库（实测 1 张）会在第一次 /api/stats 返回前就建完 ⇒ 这个 else
       以前一次都不执行，底栏 sticky 的「正在重建提示词索引…」就一直挂着（12s 不动）。
       `reindexPendingAt` 兜住这一种时序：点过重建就置位，见到一次「不在建」即复位。
       构建期这条分支每 2s 打一次，复位后不会影响后续轮询。 */
    if(!LOAD_BUSY && (statsBuildAt || reindexPendingAt)){
      setBotState("idle","已就绪",null,true);    // 建完落回就绪（V3）
      reindexPendingAt = 0;
    }
    statsBuildAt=0;
  }
}
function buildLibNav(){
  const nav=$("#libNav"); if(!nav) return;
  const st=S.stats, f=S.filters.type;      // H2：类型状态来自 S.filters（原 `#filter` 已删）
  const wf = !st ? "…" : (st.index_building && !st.workflow ? "…" : st.workflow);
  const inDir = !favMode && libMode!=="recent" && libMode!=="marked";
  /* B7-P1-1（2026-10-17）：nav_stats 此前把 census 的 stale / 读不到的目录数全丢了 ——
     界面上的数字看起来永远是「此刻的真值」。现在分三种状态说话：
       · 根目录根本读不到（盘符消失 / 无权限 / 离线）⇒ 显示「—」，**不能显示 0**
         （0 会被读成「我的图没了」，紧接着就是重建索引之类的补救性误操作）；
       · 索引构建期复用旧值 ⇒ 前缀「≈」，并说明大约每分钟刷新一次；
       · 个别子目录没读出来 ⇒ 数字照给，但提示「可能偏小」。 */
  const _err = (st && st.census_error) ? String(st.census_error) : "";
  const _stale = !!(st && st.stale);
  const _unread = (st && st.unreadable) || 0;
  const _tail = _err ? ("；" + _err)
             : _stale ? "；索引构建中，数字是最近一次扫描的结果（约每分钟更新一次）"
             : _unread ? ("；有 " + _unread + " 个目录未能读取，数量可能偏小") : "";
  const num = v => (!st ? "…" : (_err ? "—" : (_stale ? "≈" : "") + v));
  const rows=[
    ["all","grid","全部", num(st?st.total:0), inDir && libMode==="" && f==="all",
      "全库 · 根目录下全部图片和视频（含子目录），切换目录不会变" + _tail],
    ["image","image","图片", num(st?st.images:0), inDir && libMode==="" && f==="image",
      "全库 · 只显示图片（根目录含子目录，切换目录不会变）" + _tail],
    ["video","video","视频", num(st?st.videos:0), inDir && libMode==="" && f==="video",
      "全库 · 只显示视频（根目录含子目录，切换目录不会变）" + _tail],
    ["workflow","flow","工作流", wf, libMode==="workflow",
      /* B7-P2-5（2026-10-17）：这三个数与「图片」不同源（提示词索引只覆盖
         png/jpg/jpeg/webp），用户会拿两组数相减问「剩下那些去哪了」—— 说清楚口径。 */
      "全库 · 按生成参数筛选：有完整工作流 / 只有提示词 / 无生成参数（依赖提示词索引；"
      + "只覆盖 png / jpg / jpeg / webp，gif / bmp 等格式不计入）"],
    ["recent","clock","最近使用", st?st.recent:"…", libMode==="recent",
      "跨目录 · 最近预览 / 下载 / 复制提示词 / 打开过的图（最多 200 条，按路径去重）"],
    ["marked","flag","已标记", st?st.marked:"…", libMode==="marked",
      "跨目录 · 手动标记的临时状态：待处理 / 待发布 / 待比较 / 待删除（与收藏分开）"],
  ];
  nav.innerHTML=rows.map(([k,ic,nm,cnt,on,tip])=>
    '<div class="litem'+(on?" on":"")+'" data-k="'+k+'" data-tip="'+esc(tip)+'">'
      +'<span class="ic">'+ico(ic)+'</span><span class="nm">'+nm+'</span>'
      +'<span class="cnt">'+esc(String(cnt))+'</span></div>').join("");
  nav.querySelectorAll(".litem").forEach(el=>el.onclick=()=>setLibFilter(el.dataset.k));
}
async function setLibFilter(kind){
  if(kind==="recent" || kind==="marked"){ await loadLibView(kind); return; }
  // 收藏夹视图（favMode）同样要先退回目录列表：否则「全部 / 图片 / 视频 / 工作流」
  // 点下去只是改了筛选条件，网格还是收藏夹那批数据，看起来像按钮失灵
  const leaving = favMode || libMode==="recent" || libMode==="marked";
  if(leaving) await load(S.path||S.root,{silent:true});   // 先回到目录列表（load 会复位 favMode/libMode）
  if(kind==="workflow"){
    if(!S.metaMap){                                // 首次点「工作流」：拉元数据（顺带触发索引构建）
      await loadMetaMap();
      if(!S.metaMap){ toast("当前目录没有可解析生成参数的图片"); return; }
    }
    libMode="workflow";                            // 「库」各行是互斥主视图
    $("#wfClass").style.display="";                // H2：显隐仍按 libMode 管（值由 syncFiltersUI 推）
    if($("#markState")) $("#markState").style.display="none";
    S.filters.type="all"; syncFiltersUI();
    applyFilter(); return;
  }
  libMode="";
  $("#wfClass").style.display="none"; if($("#markState")) $("#markState").style.display="none";
  S.filters.type = kind==="all" ? "all" : kind;
  syncFiltersUI();
  applyFilter();
}

/* 6c. 跨目录视图：最近使用 / 已标记（服务端过滤，前端只叠加类型与搜索） */
async function loadLibView(kind, keepPath){
  /* F2-P2-1（2026-10-19）：这是**权威视图加载**（整片重写 S.items / S.dirs /
     favMode / libMode / 面包屑），必须与 load() / loadFavView() 同款：
     开头 ++viewGen，await 回来先判令牌。
     旧实现没有令牌 ⇒ 在「最近使用 / 已标记」请求在途时点左栏某个目录，
     慢的旧响应后到会无条件 applyFilter()，把刚切到的目录盖成列表视图，
     而面包屑与路径框仍是目录态 ⇒ 界面自相矛盾（看着像「点目录没反应」）。
     `keepPath` 为真时表示「刷新当前列表视图」，同样由令牌裁决胜负。 */
  const tok=++viewGen;
  const url = kind==="recent"
    ? "/api/recent_view"
    : "/api/marks_view?state="+encodeURIComponent(S.filters.markState||"*");
  const j=await api(url).catch(e=>({error:e.message}));
  if(tok!==viewGen) return;                  // 期间已切到别的视图 ⇒ 本次结果作废
  if(j.error){ toast(j.error,"err"); return; }
  favMode=false; libMode=kind; S.metaMap=null; resetMetaFilters();
  if(tagFilterActive || TAG_FILTER.length || TAG_EXCLUDE.length){   // 与标签筛选不叠加
    TAG_FILTER=[]; TAG_EXCLUDE=[]; tagFilterActive=false; S.tagView=null;
    renderTagNav();
  }
  S.dirs=[];
  S.items=(j.items||[]).map(it=>{                 // 卡片下沿显示来源与相对路径
    const rel=(S.root && it.path.startsWith(S.root))
      ? it.path.slice(S.root.length).replace(/^[\\/]/,"") : it.path;
    /* V4 批次 4：来源前缀由图标承担（it.snipIcon），snippet 只留纯文本 */
    if(kind==="recent"){ it.snipIcon="clock";
      it.snippet=(RECENT_LABELS[it.ract]||"使用")+" · "+rel; }
    else { it.snipIcon="flag";
      it.snippet=(it.marklabel||"已标记")+" · "+rel; }
    if(it.mark) MARK_MAP[it.path]=it.mark;
    return it;
  });
  if(batchOn) setBatch(false);
  /* H2（2026-10-07）：`#favcat` 已删除；`#wfClass` / `#markState` 的显隐仍按 libMode 管，
     它们的**值**由 applyFilter → syncFiltersUI() 按 S.filters 推下来。 */
  $("#wfClass").style.display="none";
  $("#markState").style.display = kind==="marked" ? "" : "none";
  applyFilter();
  if(keepPath) keepIdxByPath(keepPath);   // 取消标记后当前图会被移出「已标记」→ S.idx 复位/关大图
  mkCrumbs($("#crumbs"),                                 // 跨目录视图：自定义面包屑（V37 统一构造）
    [{label: kind==="recent" ? "最近使用" : "已标记"}],
    {back:{label:"返回目录",onclick:()=>load(S.path||"")}});
  toast((kind==="recent"?"最近使用":"已标记")+`：${S.items.length} 项`,"ok");
}

/* 记录一次「最近使用」（失败不打扰用户，数量随后自动刷新） */
function recordRecent(path, action){
  if(!path) return;
  api("/api/recent",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path, action})}).catch(()=>{});
  refreshStatsSoon();
}

/* 从「最近使用」里移除单条（2026-11-11）。只删记录，不动原图。
   菜单来自网格卡片（gallery.js 的 openCardMenu），所以这里必须能就地更新
   S.items —— 整片重载会闪一下，而且用户刚点的就是这一张。 */
async function removeRecent(it){
  if(!it || !it.path) return;
  const j=await api("/api/recent",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({op:"remove", path:it.path})}).catch(e=>({error:e.message}));
  if(j.error || j.ok===false){ toastErr("移除失败", j.error||"写入未落盘"); return; }
  /* 幂等（removed:false）也按「不在列表里」处理：界面与现实对齐，不报错 */
  S.items=(S.items||[]).filter(x=>x && x.path!==it.path);
  if(libMode==="recent") applyFilter();
  refreshStatsSoon();
  toast("已从最近使用中移除","ok");
}

/* 列表视图「标签」列文本（**纯文本**：喂 title 悬停提示，不能含 svg）
   V4 批次 1：标记状态不再带字符 ⚑，改由图标承担（见 fillLvTags） */
function listTagText(it){
  const parts=[];
  const st=it && MARK_MAP[it.path];
  if(st) parts.push(MARK_LABELS[st]||"");
  for(const t of ((it&&it.tags)||[])) parts.push(t);
  return parts.join(" · ");
}
/* V4 批次 1：列表视图「标签」列的内容 = 标记矢量旗子 + 纯文本。
   图标走 inline-flex，这样单元格的 nowrap + text-overflow:ellipsis 仍然生效。 */
function fillLvTags(cell, it){
  if(!cell) return;
  const txt=listTagText(it);
  cell.textContent="";
  cell.dataset.tip=txt||"—";
  if(it && MARK_MAP[it.path]){
    const ic=document.createElement("span"); ic.className="lvmk";
    ic.innerHTML=ico("flag");
    cell.appendChild(ic);
  }
  cell.appendChild(document.createTextNode(txt||"—"));
}
function makeLvTags(it){
  const cell=document.createElement("span"); cell.className="lv-tags lv-cell";  /* V10：列显隐锚点 */
  cell.dataset.col="tags";
  fillLvTags(cell, it);
  return cell;
}

/* 列表视图：元数据到位后把已渲染行的尺寸/模型/采样器补上（不重排 DOM） */
function patchListMeta(){
  if(!LIST_VIEW || !S.metaMap) return;
  const g=$("#grid"); if(!g) return;
  g.querySelectorAll(".card[data-p]").forEach(el=>{
    const m=S.metaMap[el.dataset.p];
    const set=(sel,txt,title)=>{ const c=el.querySelector(sel); if(!c) return;
      c.textContent=txt; if(title!==undefined) c.dataset.tip=title; };
    set(".lv-dim", (m&&m.w&&m.h)?(m.w+"×"+m.h):"—", "尺寸");
    set(".lv-model", m?(m.model||"—"):"—", m&&m.model?m.model:"");
    set(".lv-sampler", m?(m.sampler||"—"):"—", m&&m.sampler?m.sampler:"");
  });
}

/* 网格卡片：元数据到位后把缩略图角标上的尺寸补上 */
function patchCardDim(){
  if(!S.metaMap) return;
  const g=$("#grid"); if(!g) return;
  g.querySelectorAll(".card[data-p] .wicon i").forEach(el=>{
    const card=el.closest(".card"), m=card && S.metaMap[card.dataset.p];
    el.textContent=(m&&m.w&&m.h)?(m.w+"×"+m.h):"";
  });
}

/* 卡片上的标记角标（网格）／列表视图则并进「标签」列 */
function syncCardMark(path, state){
  const g=$("#grid"); if(!g) return;
  const el=[...g.querySelectorAll("[data-p]")].find(c=>c.dataset.p===path);
  if(!el) return;
  syncCardMarkEl(el, state);
}
/* 全量同步：一次遍历卡片、按 MARK_MAP 查表。
   此前 loadStats 对每个已标记路径各扫一遍网格 = O(标记数 × 卡片数)，索引构建期每 2 秒一次。 */
function syncAllCardMarks(){
  const g=$("#grid"); if(!g) return;
  for(const el of g.querySelectorAll("[data-p]")) syncCardMarkEl(el, MARK_MAP[el.dataset.p]);
}
function syncCardMarkEl(el, state){
  const path=el.dataset.p;
  if(LIST_VIEW){
    const cell=el.querySelector(".lv-tags"); if(!cell) return;
    const it=(S.view||[]).find(x=>x.path===path);
    fillLvTags(cell, it||{path, tags:[]});   // V4：标记走图标，不再回写 ⚑ 字符
    return;
  }
  let b=el.querySelector(".mkb");
  if(!state){ if(b) b.remove(); return; }
  if(!b){ b=document.createElement("span"); b.className="mkb"; el.appendChild(b); }
  /* 图标是常量（靠 innerHTML 挂 svg），标签走 textContent —— 两者分开，标签不进 HTML 解析。
     ⚠ 这里是卡片的第二个渲染点：改了 makeCard 的角标就必须同步改这里。 */
  b.innerHTML=ico("flag");
  b.appendChild(document.createTextNode(MARK_LABELS[state]||""));
  b.dataset.tip="已标记："+(MARK_LABELS[state]||state);
}

/* 设置 / 取消标记：同步内存映射、角标、大图下拉与数量 */
async function setMark(it, state){
  if(!it || !it.path) return;
  const j=await api("/api/mark",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:it.path, state:state||"none"})}).catch(e=>({error:e.message}));
  /* P1-4：后端现在会因「没落盘」返回 ok:false —— 这里必须**先判失败再动本地状态**，
     否则角标与 MARK_MAP 会被改成磁盘上并不存在的状态（刷新就消失，且过程中
     syncCardMark 已经把角标画上去了，用户看到的是一次「成功」假象）。 */
  if(j.error || j.ok===false){ toastErr("标记失败", j.error||"写入未落盘"); return; }
  const st=j.state||"";
  if(st) MARK_MAP[it.path]=st; else delete MARK_MAP[it.path];
  MARK_LOCAL.set(it.path,{st, t:Date.now()});      // 记下本地写入时刻，供 loadStats 防覆盖（见那里注释）
  it.mark=st; it.marklabel=MARK_LABELS[st]||"";
  const sel=$("#lbMark"); if(sel) sel.value=st||"none";
  toast(st?`已标记为「${MARK_LABELS[st]}」`:"已取消标记","ok");
  refreshStatsSoon();
  if(libMode==="marked") loadLibView("marked", it.path);    // 已标记视图：状态变化即时重排（大图保持指向同张）
  else syncCardMark(it.path, st);
}

/* 7. filmstrip:大图打开/切换时渲染当前视图前后缩略图 */
function buildFilm(){
  const film=$("#lbFilm"); if(!film) return;
  if(!lb.classList.contains("on")) return;
  if(S.solo){ film.innerHTML=""; return; }   // 单图模式：没有「前后邻居」可翻
  const idx=S.idx, v=S.view; if(idx<0||!v) return;
  const start=Math.max(0,idx-8), end=Math.min(v.length,idx+12);
  let h='<div class="fsp"></div>';
  for(let i=start;i<end;i++){
    const it=v[i];
    if(!it||(it.type!=="image"&&it.type!=="video")) continue;
    const med = it.type==="image"
      ? '<img src="'+(S.hasThumb?thumbURL(it):fileURL(it.path,it))+'" alt="">'
      /* V47（2026-10-08）：视频格子**渲染真首帧**，不再是「黑底 + 播放图标」。
         原因：后端 /api/thumb 只认图片后缀（server.py 的 thumb_for 里 `not in IMAGE_EXTS`
         直接返回 None），视频请求一律 404；而这里此前压根没挂过 <video>，只放了个图标 ⇒
         格子必然是「纯黑 + 半透明播放按钮」（黑底来自 #lbFilm .f 的 --canvas 底色）。
         做法：挂 <video preload="metadata" muted> + **`#t=0.001` 媒体片段** —— 让浏览器
         直接 seek 到第 1 毫秒解码出首帧，比「JS 里设 currentTime 再等 seeked」稳
         （后者在 loadeddata 之前设值会被忽略，要退回 0 帧）。0.001 而不是 0：部分编码器
         在第 0 帧给的是空白关键帧。
         muted + 无 controls + playsInline：纯展示，不参与播放（点格子仍是 openLb 翻页）。
         ⚠️ 不做懒加载观察器：胶片条最多 ~20 格且**只在选中项附近**（前后各 8/12），
            总量小、且用户已经在看大图，留几个 metadata 请求可接受（主网格那边有 vObs 管）。 */
      : '<video preload="metadata" muted playsinline src="'+fileURL(it.path,it)+'#t=0.001"></video>';
    /* 内容安全（V2.11.1）：NSFW 缩略图同样上模糊（「显示」档除外）。
       data-p 供 nsfwPaintCard 在大图里揭开/盖回时同步 —— 隐藏模式下
       NSFW 早被 nsfwFilter 从 S.view 里剔掉了，胶片条天然不会出现它们。 */
    const nsfwCls = isNsfw(it) && NSFW_MODE!=="show" ? " nsfw" : "";
    const revCls = nsfwCls && NSFW_REVEAL.has(it.path) ? " revealed" : "";
    h+='<div class="f'+(i===idx?" on":"")+nsfwCls+revCls+'" data-i="'+i+'" data-p="'+esc(it.path)+'" data-tip="'+esc(it.name)+'">'+med+'</div>';
  }
  h+='<div class="fsp"></div>';
  film.innerHTML=h;
  film.querySelectorAll(".f").forEach(el=>el.onclick=()=>{ const n=+el.dataset.i; if(!isNaN(n)) openLb(n); });
  centerFilmOn();
}

/* 8. 轻量轮询:原 JS 状态无事件,检测变化后刷新左栏/胶片条
   C-P6（批次 C，2026-10-06）三处改造：
   ① **标签签名不再每 tick 无条件做 O(N) 拼接**。原实现每 350ms 执行
      `(SUGGEST_TAGS||[]).map(t=>t.name+":"+t.count).join(",")` —— 每秒 2.86 次 ×
      O(标签数) 的字符串分配（本票唯一没被消除的规模依赖开销）。
      现在先比**数组引用本身**：tags.js 的 4 个写入点（:6/:410/:427/:558）都是
      「整组重建后重新赋值」，引用一变即内容一变 ⇒ 引用相同就直接跳过，零分配。
   ② 页面不可见时**不计时**（保留状态令牌，回到前台立刻补对一次）。
      长期挂后台 / 长时间看大图时没必要每 350ms 重建三处导航 DOM。
   ③ 补 clearInterval（pagehide 释放）—— 原实现是全站唯一一个没有清理的 setInterval
      （其余 7 处都有守卫）。 */
let _prevPath="",_prevIdx=-2,_prevFavCats=-1,_prevFilter="",
    _prevTagSig="",_prevTagRef=null,_prevTagSigApplied="\u0000";   /* 首帧必须触发一次 renderTagNav */
function _lightTick(){
  if(document.hidden) return;              /* C-P6 ②：后台不跑 */
  const p=(S.path||"")+(favMode?"·fav":"");
  const fc=(S.favCats||[]).length;
  const fl=S.filters.type;                 // H2（2026-10-07）：状态来自 S.filters（原 `#filter` 已删）
  if(p!==_prevPath||fc!==_prevFavCats||fl!==_prevFilter){
    _prevPath=p; _prevFavCats=fc; _prevFilter=fl;
    buildSideNav(); buildFavNav();
  }
  /* 2026-11-16：「位置」区的当前目录张数要在**同目录**内也跟得上 ——
     删图 / 新增 / 批量操作只改 S.items，不改 S.path，上面的重建条件不会触发。
     本函数自己比较字符串，值没变就不碰 DOM，每 tick 调用的成本可忽略。 */
  syncPosCnt();
  /* C-P6 ①：引用未变 ⇒ 内容未变，连 join 都不做 */
  const tref = SUGGEST_TAGS || null;
  if(tref!==_prevTagRef){
    _prevTagRef = tref;
    _prevTagSig = (tref||[]).map(t=>t.name+":"+t.count).join(",");
  }
  if(_prevTagSig!==_prevTagSigApplied){ _prevTagSigApplied=_prevTagSig; renderTagNav(); }
  if(S.idx!==_prevIdx){ _prevIdx=S.idx; buildFilm(); }
}
/* 常驻定时器预算（门禁 `perf_timerbudget_check` A1 ≤ 8）：
   启动/重建都走**同一个** `setInterval` 调用点，别为了 pageshow 再写一处。 */
let _lightTimer = 0;
function _startLightTick(){
  if(!_lightTimer && typeof _lightTick === "function") _lightTimer = setInterval(_lightTick, 350);
}
_startLightTick();
/* 回到前台时立即对一次，避免切回来时导航与胶片条滞后才更新 */
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) _lightTick(); });
/* C-P6 ③：页面卷起来时释放定时器（长时间看图不占一个轮询）。
   ⚠️ F3-P2-3（2026-10-19）：只在 `pagehide` 里 clear 而**不重建**，一旦文档进了
   bfcache（跳到外链再按后退回来）就会回来一个「定时器已经被杀」的页面 ——
   左栏「位置」树不再扩建、胶片条不跟大图走、标签导航不重绘，且没有任何自愈途径。
   所以 `pageshow` 必须配一对（`persisted` 为真时重建）。 */
window.addEventListener("pagehide", ()=>{ if(_lightTimer){ clearInterval(_lightTimer); _lightTimer=0; } });
window.addEventListener("pageshow", ()=>{
  _startLightTick();
  try{ _lightTick(); }catch(_){}
});

/* 9. 包装 openLb:打开大图时刷新维度 + 胶片条 + 详情面板默认开 */
const _openLbOrig=window.openLb;
window.openLb=function(i, solo){     // ⚠️ 第二个参数必须透传：丢了它单图会被当库内条目打开
  /* 2026-11-15：只在**大图原本关着**（= 真正的一次打开）时才把详情面板撑开。
     原先每次调用都无条件 toggleSide(false)，而翻页（←/→、点胶片条）也走 openLb
     ⇒ 用户嫌挤按 P 收起面板，按 → 看下一张时它又滑出来，再收再翻再出来，
     明确的「收起」意图被每一次翻页覆盖。首次打开仍保持「默认展开」不变。 */
  if(!lb.classList.contains("on")) toggleSide(false);
  const dim=$("#lbDim");
  if(dim) dim.textContent="";        // 先清掉上一张的尺寸：加载期间宁可空着，也别张冠李戴
  const r=_openLbOrig(i, solo);
  /* 2026-11-15：#lbDim（文件名后面的分辨率位）此前**永远是空的** ——
     原代码在 `_openLbOrig()` 返回的那一刻同步去读 `S.curMeta`，而 openLb 在第一个
     `await` 之前就已把 `S.curMeta` 置成 null（viewer.js:750 的神骨架占位），
     真正赋值要等 /api/meta 回来（:754）⇒ 同步读到的必然是 null。
     改成等 promise 落定后再填；落定后再核一次 curIdx，避免快速翻页时把 A 的尺寸贴到 B 上
     （与 openLb 内部丢弃迟到响应用的是同一个判据）。 */
  if(r && typeof r.then==="function"){
    const _i=i;
    r.then(()=>{
      const d=$("#lbDim"); if(!d) return;
      if(typeof curIdx==="function" && curIdx()!==_i) return;
      const ih=S.curMeta&&S.curMeta.meta&&S.curMeta.meta.IHDR;
      d.textContent=ih||"";
    }).catch(()=>{});
  }
  setTimeout(()=>{ buildFilm(); if(window._renderTagPanelOnOpen) _renderTagPanelOnOpen(); },120);   // 等原逻辑填入 stage
  return r;
};
/* 关大图时清胶片 */
const _closeLbOrig=window.closeLb;
window.closeLb=function(){
  const r=_closeLbOrig();
  const film=$("#lbFilm"); if(film) film.innerHTML="";
  return r;
};

/* ==================================================================
   V1 独立设置面板（批次 2）
   原先「外观与操作」整块塞在左栏底部 —— 8 个分区里它一个就装了 7 个控件，
   而且和高频变化的「去哪儿看 / 看哪些」混在同一条滚动动线上。
   现在搬进右侧抽屉，按 外观 / 行为 / 数据 / 关于 四页签归类：左栏瘦身为 7 个纯浏览分区，
   今后任何新设置都有地方放，左栏不再变长。控件 id 全部沿用（绑定按 id 走，搬家不动绑定）。
   ================================================================== */
(function setupSettingsPanel(){
  const mask=$("#setMask"), panel=$("#setPanel");
  if(!mask || !panel) return;
  mask.hidden=false;                 // hidden 只作初始态；开合一律走 .on 才能过渡
  let lastFocus=null;
  let sysTimer=0;                    // F26 索引进度轮询（面板关掉 / 建完即停）
  /* F3-P2-2（2026-10-19）：失败重试计数。原来 catch 里只改一格文字就 return，
     续轮询的收尾在函数尾部 ⇒ **一次失败就把链断掉**，进度条永久定格在旧数字。 */
  let sysFails=0;
  const SYS_RETRY_MAX=5;             // 5 × 1.5s ≈ 7.5 秒，够熬过「后端线程正忙」
  const ddState={dir:"",want:"user",targets:{}};   // F28 数据目录位置（真值只来自 /api/sysinfo）
  const isOpen=()=>mask.classList.contains("on");

  function open(){
    if(isOpen()) return;
    lastFocus=document.activeElement;
    mask.classList.add("on");
    syncSysinfo();                   // 数据 / 关于页每次打开都重新取真值
    setTimeout(()=>{ const t=panel.querySelector(".set-tab.on")||panel.querySelector(".set-tab");
      if(t) t.focus(); },60);
  }
  function close(){
    if(!isOpen()) return;
    clearTimeout(sysTimer);          // 面板关了就别再轮询（F26）
    mask.classList.remove("on");
    /* G2·V16③：**先同步解冻再归还焦点** —— 触发元素在 #content/#main 里，
       G1 的 inert 没撤时它聚焦不了，`focus()` 会静默失败（实测焦点落回 BODY）。
       注意 MutationObserver 是下一拍才撤 inert，等不起，必须同步调。 */
    if(window.mvSyncInert) window.mvSyncInert();
    if(lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus=null;
  }
  window.mvOpenSettings=open;        // 供其它模块调用（例如将来快捷键呼出）
  window.mvCloseSettings=close;

  /* ---- 页签：只切 class，不重建 DOM —— 页内控件状态天然保留 ---- */
  const tabs=[].slice.call(panel.querySelectorAll(".set-tab"));
  const pages=[].slice.call(panel.querySelectorAll(".set-page"));
  function showTab(name){
    for(const t of tabs){
      const on=t.dataset.tab===name;
      t.classList.toggle("on",on); t.setAttribute("aria-selected",String(on));
      t.tabIndex = on ? 0 : -1;                  // V42：roving 与 aria-selected 同步
    }
    for(const p of pages) p.classList.toggle("on", p.dataset.page===name);
    const b=panel.querySelector(".set-body"); if(b) b.scrollTop=0;
    if(name==="data") syncSysinfo();   // 切到数据页即取一次真值（F26：进度/占用/缓存）
    if(name==="nsfw") nsfwLoadStatus(); // 切到内容安全页取一次真值（引擎/档位/阈值/进度）
    if(name==="behave") loadHiddenDirs();  // 2026-11-08：隐藏名单的真值只在服务端
  }
  for(const t of tabs) t.onclick=()=>showTab(t.dataset.tab);

  /* G2·V42：WAI-ARIA 页签键盘模型 —— 未选中页签 tabindex=-1（否则「半套 ARIA」做不成），
     所以必须同时提供 ←/→ 与 Home/End，否则页签从键盘上「消失」了。 */
  function tabRoving(){
    tabs.forEach(t=>{ t.tabIndex = t.classList.contains("on") ? 0 : -1; });
  }
  tabs.forEach((t,i)=>{
    t.onkeydown=e=>{
      let n=-1;
      if(e.key==="ArrowRight") n=(i+1)%tabs.length;
      else if(e.key==="ArrowLeft") n=(i-1+tabs.length)%tabs.length;
      else if(e.key==="Home") n=0;
      else if(e.key==="End") n=tabs.length-1;
      if(n<0) return;
      e.preventDefault();
      showTab(tabs[n].dataset.tab);
      tabs[n].focus();
    };
  });
  tabRoving();

  const bs=$("#btnSettings"); if(bs) bs.onclick=open;

  /* ---- 隐藏的文件夹（2026-11-08）----
     清单的真值在服务端（/api/hidden → settings.json），这里只负责画出来与恢复。
     不在打开抽屉时预拉：绝大多数会话不会看这一块。切到「行为」页再拉。
     ⚠️ 隐藏是**服务端**行为 —— 索引 / 搜索 / 统计 / 查重一起跳过。所以「恢复」之后
        必须重扫（后端已做），前端这边只要把列表与数字重取一次。 */
  async function loadHiddenDirs(){
    const box=$("#hidList"); if(!box) return;
    let j=null;
    try{ j=await api("/api/hidden"); }catch(e){ j={error:String((e&&e.message)||e)}; }
    if(!j || j.error){
      box.innerHTML='<div class="set-row"><span>名单读取失败</span><b></b></div>';
      return;
    }
    const dirs=Array.isArray(j.dirs)?j.dirs:[];
    const rootTxt=j.root||"";
    /* 左栏那个入口顺手点亮/熄灭：名单非空时用户得能一眼看出「有东西被藏起来了」 */
    const rhb=$("#recHideBtn"); if(rhb) rhb.dataset.on = dirs.length ? "1" : "0";
    const hc=$("#hidClear"); if(hc) hc.disabled = !dirs.length;
    box.innerHTML="";
    if(!dirs.length){
      const row=document.createElement("div"); row.className="set-row";
      const sp=document.createElement("span"); sp.textContent="没有隐藏任何文件夹";
      row.appendChild(sp); row.appendChild(document.createElement("b"));
      box.appendChild(row); return;
    }
    for(const rel of dirs){
      const row=document.createElement("div"); row.className="set-row";
      const sp=document.createElement("span");
      sp.textContent=rel;
      /* 悬停给完整绝对路径：清单里存的是相对根的串，光看名字未必分得清是哪一层 */
      sp.title=rootTxt ? (rootTxt+"\\"+String(rel).replace(/\//g,"\\")) : String(rel);
      sp.setAttribute("data-tip", sp.title);
      const b=document.createElement("b");
      const btn=document.createElement("button");
      btn.className="sm"; btn.type="button"; btn.textContent="恢复";
      btn.setAttribute("data-tip","恢复后这个文件夹及其中的图会重新出现在列表、搜索与统计里");
      btn.onclick=async()=>{
        btn.disabled=true;
        await unhideDir(rel, String(rel).split("/").pop());
        await loadHiddenDirs();          // unhideDir 内部也会刷，这里再取一次保证按钮状态正确
      };
      b.appendChild(btn); row.appendChild(sp); row.appendChild(b);
      box.appendChild(row);
    }
  }
  window.loadHiddenDirs=loadHiddenDirs;     // gallery.js 的隐藏/恢复之后要同步这一块

  const hc=$("#hidClear");
  if(hc) hc.onclick=async()=>{
    hc.disabled=true;
    try{
      const r=await api("/api/hidden",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"clear"})});
      if(r && r.ok && !r.error){
        toast("已恢复全部隐藏的文件夹","ok");
        refreshAfterHiddenChange();
        loadHiddenDirs();
      }else{
        toastErr("恢复失败",(r&&(r.reason||r.error))||"服务端未返回原因");
      }
    }catch(e){ toastErr("恢复失败", String((e&&e.message)||e)); }
    hc.disabled=false;
  };

  /* 最近使用：清空（单条移除在网格右键菜单里，见 removeRecent） */
  const rc=$("#recentClear");
  if(rc) rc.onclick=async()=>{
    rc.disabled=true;
    try{
      const r=await api("/api/recent",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"clear"})});
      if(r && r.ok && !r.error){
        toast("已清空最近使用记录","ok");
        refreshStatsSoon();                       // 左栏「最近使用」计数归零
        if(libMode==="recent") loadLibView("recent");
      }else{
        toastErr("清空失败",(r&&r.error)||"服务端未返回原因");
      }
    }catch(e){ toastErr("清空失败", String((e&&e.message)||e)); }
    rc.disabled=false;
  };

  /* 左栏「含子文件夹」右侧的小入口：图标只能在 JS 侧注入（静态 HTML 拿不到 ICO 表） */
  const rhb=$("#recHideBtn");
  if(rhb){
    rhb.innerHTML='<span class="ic">'+ico("eyeOff")+'</span>';
    rhb.onclick=()=>{ open(); showTab("behave"); };
  }
  /* 供别处（例如「你在隐藏目录里」的提示条）直接呼出到指定页签 */
  window.mvOpenSettingsTab=(name)=>{ open(); showTab(name||"behave"); };

  /* H5（2026-10-06）：顶栏「工具▾」下拉拆掉，查重 / 出图统计的入口落在「数据」页。
     两项都会开一个全屏弹窗（bigDialog），若抽屉还盖在上面就成了「两层浮层叠着」，
     而且 Esc 的归属也含糊 —— 所以**先关抽屉，再开弹窗**。
     真正的实现仍绑在隐藏宿主 #btnDupes / #btnStats 上（见 js/extras.js），
     这里只转发一次 click ⇒ 不改任何业务逻辑，只是换了个门。
     ⚠️ 关闭要放在 click 处理器里同步做：close() 会把焦点还给 #btnSettings，
        弹窗随后接管焦点，顺序天然正确。 */
  [["setDupes", "btnDupes"], ["setStats", "btnStats"]].forEach(([btnId, hostId])=>{
    const b=$("#"+btnId), h=$("#"+hostId);
    if(b && h) b.onclick=()=>{ close(); h.click(); };
  });
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });   // 点遮罩空白处关闭
  /* Esc 关闭 + 焦点陷阱：面板打开时 Tab 只在面板内循环，不逃到背后的网格上 */
  document.addEventListener("keydown",e=>{
    if(!isOpen()) return;
    /* ε-2（2026-09-20）：**上层还有浮层时，Esc 让给它**。
       本监听注册在**捕获阶段**（焦点陷阱需要），原来的写法一进来就 stopPropagation ⇒
       抽屉开着时 Esc 到不了**任何**别的处理器。于是：数据页点「清除单图暂存」等
       二次确认弹出后按 Esc —— 关不掉上面那层，反而把底下的抽屉关走了，
       用户得重新打开抽屉找回刚才的位置。
       （⚠️ 口径：confirmDialog 建的节点是 `#catInputMask`，不是 `.bigMask`；
         `mvTopOverlay()` 两个族都认，这里不必区分。）

       修法是加一道「我是不是最顶层」的判断，真相来源复用 app.js 的 `mvTopOverlay()`
       （与 attachInert / attachDynamicOverlayTrap 同一份浮层清单）。
       ⚠️ 只在**有上层浮层**时 return；没有上层就照旧 stopPropagation 并关闭，
       不改变原有的「Esc 关抽屉 + Tab 只在面板内循环」行为。 */
    if(e.key==="Escape"){
      if(window.mvTopOverlay && window.mvTopOverlay()!==mask) return;
      e.stopPropagation(); e.preventDefault(); close(); return;
    }
    if(e.key!=="Tab") return;
    /* ε-2（F2 P2-1 **下半段**）：Tab 同样要让给上层 —— 光给 Esc 让位修不全这条。
       抽屉的焦点陷阱会把焦点从上面的确认框**搬回抽屉面板**，于是那个确认框
       变成收不到任何键盘事件的孤儿（Esc 分支已经让位了，可 Tab 还在抢），
       而它恰好是「清除缩略图缓存」「清除单图暂存」这类不可逆操作 ——
       用户被迫改用鼠标点「取消/删除」，误触风险实打实。
       判据与 Esc 那道共用 mvTopOverlay()：只有上层真的没浮层时才照旧循环。 */
    if(window.mvTopOverlay && window.mvTopOverlay()!==mask) return;
    const f=[].slice.call(panel.querySelectorAll("button,select,input,[tabindex]"))
      .filter(el=>!el.disabled && el.tabIndex>=0 && el.offsetParent!==null);
    if(!f.length) return;
    const first=f[0], last=f[f.length-1];
    if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
  },true);

  /* ---- V21 密度档位：三档预设 ⇄ 滑块 ----
     滑块是唯一真相，档位只是它的三个快捷键 —— 两套状态各记一份必然打架。
     当前值不落在三档上时三档全灭（只显示滑块数值），不伪造「选中」。 */
  const preset=$("#densPreset");
  function syncPreset(){
    if(!preset || !window.mvGetDens) return;
    const v=window.mvGetDens();
    let hit=false;
    for(const b of preset.querySelectorAll(".segbtn")){
      const on=(+b.dataset.d===v);
      b.classList.toggle("on",on); b.setAttribute("aria-pressed",String(on));
      if(on) hit=true;
    }
    preset.dataset.free = hit ? "" : "1";
  }
  if(preset){
    for(const b of preset.querySelectorAll(".segbtn"))
      b.onclick=()=>{ if(window.mvSetDens) window.mvSetDens(+b.dataset.d); syncPreset(); };
    const dr=$("#densRange");
    if(dr){ dr.addEventListener("input",syncPreset); dr.addEventListener("change",syncPreset); }
    syncPreset();
  }

  /* ---- 数据 / 关于页：值全部来自 /api/sysinfo 与 /api/bridge/status，前端不自造 ---- */
  const fmtBytes=b=>{
    b=b||0;
    if(!b) return "0 B";
    const u=["B","KB","MB","GB"]; let i=0, v=b;
    while(v>=1024 && i<u.length-1){ v/=1024; i++; }
    return (i===0 ? String(v) : v.toFixed(v<10?1:0)) + " " + u[i];
  };
  const setTxt=(id,t)=>{ const el=document.getElementById(id); if(el) el.textContent=t; };
  /* ---- 审计 F3-P1-1：重建索引的单飞守卫 ----
     后端的 force_reindex 会先 `IDX_GEN+=1`、把 `IDX_THREAD` 置 None，再调 ensure_index()；
     而 ensure_index 只在「线程登记还活着」时才 return ⇒ 连点第二次必定再起一条线程。
     两条线程同扫一棵树、已扫进度被清零重来，外接盘上等于 N 份并发随机读。
     禁用态必须由**派生函数**算（看 building + 本地 busy），不能只写在点击回调里 ——
     否则任何一次 syncSysinfo 都会把它解掉（同 updateBatchBar 那条纪律）。
     解锁条件带 6 秒宽限：小库可能瞬间建完，也可能线程还没登记进 building，
     此时过早解锁就退化成「连点照旧」。 */
  let reindexBusy=false, reindexSeenBuild=false, reindexAt=0;
  function syncReindexBtn(building){
    const b=document.getElementById("setReindex");
    if(building) reindexSeenBuild=true;
    if(!building && reindexBusy && (reindexSeenBuild || Date.now()-reindexAt>6000)){
      reindexBusy=false; reindexSeenBuild=false;      // 见过它真的在跑，或宽限期已过
    }
    if(b) b.disabled = reindexBusy || !!building;
  }
  let aboutLinks=null;          // 关于页项目链接（/api/sysinfo 下发，前端不自造）
  let sysTok=0;                 // 4.2-2：/api/sysinfo 的世代号（过期响应一律丢弃）
  async function syncSysinfo(){
    const _tok=++sysTok;
    let j;
    try{ j=await api("/api/sysinfo"); }
    catch(e){
      if(_tok!==sysTok) return;           // 已被更新的一次调用顶掉：别改状态、别续链
      sysFails++;
      setTxt("setDataDir","（读取失败）");
      clearTimeout(sysTimer);
      if(isOpen() && sysFails<=SYS_RETRY_MAX){
        /* 面板还开着就续链（带上限，绝不写无限重试）—— 索引在建时后端线程紧张，
           偶发一次超时不该让进度条永远停住。 */
        sysTimer=setTimeout(syncSysinfo, 1500);
        if(sysFails===1) setBotState("warn", "设置数据读取失败，正在重试…", null, false);
      }else if(sysFails===SYS_RETRY_MAX+1){
        setBotState("err", "设置数据读取失败，进度已停止更新", null, false);
        toast("设置数据读取失败，索引进度暂不更新（重新打开设置面板可重试）","err");
      }
      return;
    }
    if(_tok!==sysTok) return;            // 过期响应：不写状态、不续链
    sysFails=0;
    setTxt("setDataDir", j.data_dir||"—");
    /* P1-3：真值只来自后端（settings.json 是权威，环境变量还能再覆盖一层），
       前端不自造。拿到后就地回填并刷新那句说明。 */
    if(tkInp && j.trash_keep_days!=null && document.activeElement!==tkInp){
      tkInp.value=j.trash_keep_days;
    }
    renderTrashKeep();
    setTxt("setVer", "V"+(j.version||""));
    setTxt("setAboutVer", "V"+(j.version||"—"));
    setTxt("setPil", j.has_pil ? "已就绪" : "未安装（缩略图与查重不可用）");
    /* 关于页项目入口：地址只认后端下发的 links（同「值全部来自 /api/sysinfo」的纪律）。
       取不到就保持「—」并让按钮继续禁用 —— 宁可点不动，也不给死链。 */
    aboutLinks = (j.links && typeof j.links === "object") ? j.links : null;
    const repoUrl = (aboutLinks && aboutLinks.repo) || "";
    const repoEl = document.getElementById("setRepoUrl");
    if(repoEl){
      repoEl.textContent = repoUrl.replace(/^https?:\/\//, "") || "—";
      repoEl.title = repoUrl;
    }
    document.querySelectorAll("[data-about-open],[data-about-copy]").forEach(b=>{
      const key = b.dataset.aboutOpen || b.dataset.aboutCopy;
      b.disabled = !(aboutLinks && aboutLinks[key]);
    });
    /* ---- F28 数据目录位置：分段控件反映**磁盘上的选择** ----
       pending=True 时磁盘选择已改、但这个进程还跑在旧目录（切换后未重启），此时按
       磁盘选择点亮、并在说明里点明「重启后生效」—— 让界面跟着用户刚才的操作走，
       而不是跟着一个即将失效的进程状态走，否则用户会以为没切成功。 */
    const dd=j.datadir||{};
    ddState.dir=dd.dir||"";
    ddState.targets=dd.targets||{};
    const ddWant=(dd.pending ? (dd.flag_exists?"portable":"user") : (dd.mode||"user"));
    ddState.want=ddWant;
    const ddSeg=$("#ddMode");
    if(ddSeg){
      [].forEach.call(ddSeg.querySelectorAll(".segbtn"),
        b=>b.classList.toggle("on", b.dataset.m===ddWant));
    }
    /* 文案要如实说清「东西放哪、什么会丢」——
       用户目录在系统盘：重装系统 / 换机会丢，但换程序版本、挪程序位置都不受影响；
       程序目录跟着程序走：拷到 U 盘就能带走，反过来删了 / 挪了程序数据也跟着走。
       两边各有代价，不存在「哪种绝对安全」，所以不写「不受影响」这种一边倒的话。 */
    /* B7（2026-10-07 文案梳理）：两种模式的取舍已写在按钮 tooltip 上，此处不再逐字重复，
       只留一行「当前在哪 / 是否需要重启」的状态。
       ⚠️ 措辞必须含「用户目录」/「程序目录」—— tests/f28v17_check.py 的 3b 断言锚它。 */
    const ddHint=$("#ddHint");
    if(ddHint) ddHint.textContent = dd.pending
      ? "已切换，重启程序后生效（当前这次会话仍在旧目录）。"
      : (ddWant==="portable"
          ? "当前放在程序目录（cmv-data，随程序文件夹一起走）。"
          : "当前放在用户目录（系统盘的用户配置里）。");

    /* ---- B1-3：数据目录写不进去 ⇒ 必须红字说出来，不能只留日志 ----
       save_favs / save_tags / _save_settings_locked 失败原本只往数据目录里的
       日志文件写一行 —— 而那个目录恰好就是写不进去的那个。于是常用目录、
       上次目录、删除偏好全都「看着改了、重启回原样」，用户无从察觉。
       data_write 由后端给：ok=False 是**最近一次实际写盘**失败，
       dir_writable=False 是**启动自检**就没写进去。任一为假都要告警。 */
    const dw=j.data_write||{};
    const ddWarn=$("#ddWarn");
    if(ddWarn){
      const bad = dw.ok===false || dw.dir_writable===false;
      ddWarn.hidden = !bad;
      if(bad){
        ddWarn.textContent =
          "数据目录写不进去" + (dw.error ? "（"+dw.error+"）" : "") +
          "：设置 / 收藏 / 标签的改动会在重启后丢失。"
          + "请检查该目录是否只读、或所在盘是否掉线。";
      }
    }

    /* ---- F26 索引进度：done/total 来自后端 _index_builder，前端只负责画 ---- */
    const ix=j.index||{}, pr=ix.progress||{};
    const building=!!pr.building, done=pr.done|0, total=pr.total|0;
    syncReindexBtn(building);                        // F3-P1-1：按钮禁用态跟着真值走
    const prog=$("#setIdxProg"), fill=$("#setIdxFill");
    const pct = total ? Math.max(0,Math.min(100,Math.round(done*100/total))) : (building?0:100);
    if(prog){
      prog.classList.toggle("building", building);
      prog.classList.toggle("ready", !building && !!ix.count);
    }
    /* 进行中至少给 2% 的可见宽度：total 还没数出来时（扫描阶段）条子不能是空的，
       否则用户看到「正在建立」配一条空槽，会以为卡死了。 */
    if(fill) fill.style.width = (building ? Math.max(pct,2) : (ix.count?100:0)) + "%";
    setTxt("setIdxState", building ? "正在建立提示词索引…"
      : (ix.count ? "已就绪（有新增时会自动补充）" : "尚未建立索引"));
    setTxt("setIdxNum", total ? (done+" / "+total) : ((ix.count||0)+" 条"));

    /* ---- 占用：缩略图 / 索引 / 回收站（索引行取的是 IDX_FILE 实际大小） ---- */
    const rows=$("#setRows");
    if(rows){
      /* 「单图暂存」= 「打开图片」复制进来的临时副本（上限 20 张，哈希命名同图复用） */
      const data=[[ "缩略图缓存", j.thumbs ], [ "提示词索引", ix ], [ "回收站", j.trash ],
                  [ "单图暂存", j.solo ]];
      rows.innerHTML=data.map(function(row){
        const l=row[0], o=row[1]||{};
        const n=(o.count!=null) ? (" · "+o.count+" 个") : "";
        return '<div class="set-row"><span>'+esc(l+n)+'</span><b>'+esc(fmtBytes(o.bytes))+'</b></div>';
      }).join("");
    }


    const hint=$("#setDataHint");
    if(hint) hint.textContent = ix.root
      ? ("当前索引根目录：" + ix.root)
      : "尚未建立索引（搜索提示词或点开「工作流」筛选即会构建）。";

    /* 建索引是慢活：面板开着才跟着刷，关掉或建完立即停 —— 空转请求没意义（F26） */
    clearTimeout(sysTimer);
    if(isOpen() && building) sysTimer=setTimeout(syncSysinfo, 700);
  }
  /* F3-P2-6（2026-10-19）：`syncSysinfo` 是 IIFE 内部的函数声明，**不挂 window** ——
     而 extras.js 的 `if(typeof syncSysinfo==="function") syncSysinfo();` 因此永远静默跳过
     （那行是死代码）。这里按 `window.mvSyncInert` 的既有做法显式导出。 */
  window.mvSyncSysinfo = syncSysinfo;
  (async function(){                                  // 关于页：桥接安装/在线状态
    try{
      const b=await api("/api/bridge/status");
      setTxt("setBridge", b.installed
        ? (b.active ? "已安装 · 页面在线" : "已安装 · 页面未打开")
        : "未安装（在 ComfyUI 页面里点「安装桥接」）");
    }catch(e){ setTxt("setBridge","—"); }
  })();
  setTxt("setDataDir","—");
  /* ---- 关于页：GitHub 入口 ----
     打开方式按运行形态分流：
       · 浏览器顶层页：window.open 新标签（不打断当前页）；
       · 桌面壳（iframe 内嵌 / Tauri 顶层）：页面拿不到窗口 API，交给本地后端
         用系统默认浏览器打开。后端只认枚举 key，不接受任意 URL。 */
  async function aboutOpen(key){
    const url = aboutLinks && aboutLinks[key];
    if(!url) return;
    if(window.self === window.top && !window.__TAURI__){
      let w = null;
      try{ w = window.open(url, "_blank"); }catch(e){ w = null; }
      if(w){ try{ w.opener = null; }catch(e){} return; }
      /* 弹窗被拦：不装死，退回复制（与局域网「复制地址」同一条出口） */
      try{ await window.mvCopyText(url); toast("浏览器拦了新窗口，链接已复制","warn"); }
      catch(e){ toast("请手动复制地址","err"); }
      return;
    }
    try{
      await api("/api/open_url", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({k:key})});
    }catch(e){
      try{ await window.mvCopyText(url); toast("没能唤起浏览器，链接已复制","warn"); }
      catch(_){ toast("打开失败："+e.message,"err"); }
    }
  }
  document.querySelectorAll("[data-about-open]").forEach(b=>{
    b.addEventListener("click", ()=>aboutOpen(b.dataset.aboutOpen));
  });
  document.querySelectorAll("[data-about-copy]").forEach(b=>{
    b.addEventListener("click", async ()=>{
      const url = aboutLinks && aboutLinks[b.dataset.aboutCopy];
      if(!url) return;
      try{ await window.mvCopyText(url); toast("GitHub 地址已复制","ok"); }
      catch(e){ toast("复制失败，请手动选中地址","err"); }
    });
  });

  /* ---- P1-3 回收站自动清理：保留天数可查、可配、可关（0=关闭）----
     此前是写死的 30 天常量，到点就在启动时静默永久删除，用户既不知道也无从关闭。
     这里不新增一套「设置项」概念，直接复用 /api/prefs（delete_mode 已在用）。 */
  const tkInp=$("#trashKeepDays"), tkHint=$("#trashKeepHint");
  /* 2026-11-05 P2：`<input type=number>` 的 value 允许 "1e3"，而 `parseInt("1e3",10)`
     等于 **1** —— 用户想填 1000 天，实际存成 1 天，启动时把超过 1 天的回收站条目
     **永久删除**。这里用 Number() 认指数形式，但：
       ① 空串 / 纯空白必须视为无效（`Number("") === 0` 会被当成「关闭」）；
       ② 非整数（"1.5"）也拒 —— 天数只能是整数。 */
  function tkDays(v){
    const s=String(v==null?"":v).trim();
    if(!s) return NaN;
    const n=Number(s);
    return Number.isFinite(n) && Number.isInteger(n) ? n : NaN;
  }
  function renderTrashKeep(){
    if(!tkHint) return;
    const v=tkDays(tkInp && tkInp.value);
    if(!isFinite(v)){ tkHint.textContent="请填 0-3650 的整数。"; return; }
    tkHint.textContent = v===0
      ? "已关闭：回收站里的文件不会被自动删除，只能手工彻底删除。"
      : `超过 ${v} 天的条目会在程序启动时被自动彻底删除（不可恢复）。`;
  }
  if(tkInp){
    tkInp.addEventListener("input", renderTrashKeep);
    tkInp.addEventListener("change", async()=>{
      const v=tkDays(tkInp.value);
      if(!isFinite(v) || v<0 || v>3650){
        toast("保留天数需要是 0-3650 的整数","err"); syncSysinfo(); return;
      }
      const j=await api("/api/prefs",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({trash_keep_days:v})}).catch(e=>({error:e.message}));
      if(j.error){ toastErr("回收站清理设置保存失败", j.error); syncSysinfo(); return; }
      /* 审计 F3-P1-3：**生效值**只认响应里的 `trash_keep_days`（= load_trash_keep_days()），
         不是用户敲的那个数 —— POST 成功只代表「写进 settings.json 了」，而环境变量
         CMV_TRASH_KEEP_DAYS 的优先级更高。此前输入框回填真值、提示行写真值，
         唯独成功 toast 用输入值 ⇒ 界面自己打架，而这条设置管的是启动时**永久删除**，
         报错一个数就是让用户按错的预期丢文件。 */
      const eff=tkDays(j.trash_keep_days);
      const applied=isFinite(eff) ? eff : v;
      tkInp.value=applied;
      renderTrashKeep();
      if(applied!==v){
        toastErr(`保留天数仍为 ${applied} 天`,
          "当前环境通过外部配置固定了这个值（环境变量 CMV_TRASH_KEEP_DAYS 优先于设置），在这里改动不会生效");
      }else{
        toast(applied===0?"已关闭回收站自动清理":`回收站保留天数：${applied} 天`,"ok");
      }
    });
  }

  const mk=(id,fn)=>{ const b=document.getElementById(id); if(b) b.onclick=fn; };
  mk("setOpenDir", async()=>{
    const j=await api("/api/open_data",{method:"POST"}).catch(e=>({error:e.message}));
    if(j.error) toast("打开失败："+j.error,"err");
  });
  mk("setReindex", ()=>{
    /* 点进来时 building 可能还没被 syncSysinfo 观察到（面板刚打开）⇒ 这里再挡一层。
       禁用按钮本身不派发 click，两层是互补的，不是重复。 */
    if(reindexBusy){ toast("索引正在重建中，请稍候","ok"); return; }
    confirmDialog("重建提示词索引",
      "会丢弃现有索引缓存并重新扫描当前根目录（不动任何原图）。"
      +"重建期间按提示词搜索会暂时为空，大目录可能要数十秒。", async()=>{
      if(reindexBusy) return;                        // 两个确认框先后落地时也只放一个过去
      reindexBusy=true; reindexSeenBuild=false; reindexAt=Date.now();
      syncReindexBtn(false);                         // 立即置灰：不让人在请求回来前再点一次
      const j=await api("/api/reindex",{method:"POST"}).catch(e=>({error:e.message}));
      if(j.error){
        toast(j.error,"err");
        reindexBusy=false; syncReindexBtn(false);
        return;
      }
      toast("已开始重建索引","ok");
      setBotState("busy","正在重建提示词索引…",null,true);
      reindexPendingAt=Date.now();                    // 兜底复位用（见 loadStats）
      statsBuildAt=0; loadStats();                    // 立刻接上进度，底栏开始报数（V3）
      /* 立即取一次真值（小库可能已经建完），2.5s 后再接上进度，
         8s 那次是宽限期到点后的兜底解锁 —— 否则 panel 一直开着时没人再调 syncSysinfo，
         按钮会灰到下次重新打开设置页。 */
      syncSysinfo();
      setTimeout(syncSysinfo, 2500);
      setTimeout(syncSysinfo, 8000);
    }, {okText:"开始重建", okClass:""});   /* B1：不动原图的操作不该是红色「删除」 */
  });
  /* F3-P2-4（2026-10-19）：两个「清除」按钮原来既没有防连点、也只报成功数。
     ① 连点两次：第一次还在删，第二次把期间重新生成的又删一遍，最后弹
        「已清除缩略图缓存（0 个文件）」—— 读起来像「成功了但什么都没清」；
     ② 有文件被预览占用/只读时，后端会把它们静默跳过，界面照样报「已清除 N 个」，
        而设置页的占用字节数不降反升，用户没有任何解释。
     现在：单飞（按钮 disabled）+ 如实报「N 个已清除，M 个被占用未能删除」。 */
  let _cacheBusy=false;
  const runCacheClear=(what, label, btnSel)=>async()=>{
    const b=btnSel?document.getElementById(btnSel):null;
    if(_cacheBusy){ toast("正在清理，请稍候…","warn"); return; }
    _cacheBusy=true;
    if(b) b.disabled=true;
    try{
      const j=await api("/api/cache_clear",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({what:what})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      const n=j.removed||0;
      const bad=Array.isArray(j.failed)?j.failed:[];
      if(bad.length){
        toast(label+"："+n+" 个已清除，"+bad.length+" 个被占用未能删除（关掉大图/预览后重试）",
          "warn", 6500);
        setBotState("warn", label+"："+bad.length+" 个文件未能删除", null, false);
      }else{
        toast(label+"（"+n+" 个文件）","ok");
      }
      syncSysinfo();
    }finally{
      _cacheBusy=false;
      if(b) b.disabled=false;
    }
  };
  mk("setClearThumb", ()=>{
    confirmDialog("清除缩略图缓存",
      "会删除本地缩略图缓存文件（不动原图），下次浏览时按需重新生成。",
      runCacheClear("thumbs","已清除缩略图缓存","setClearThumb"),
      {okText:"清除缓存", okClass:""});
  });
  mk("setClearSolo", ()=>{
    confirmDialog("清除单图暂存",
      "会删除「打开图片」复制进来的临时副本（不动原图、不动库内文件）。"
      +"已打开的那张如果还开着，界面不受影响；下次打开会重新复制一份。",
      runCacheClear("solo","已清除单图暂存","setClearSolo"),
      {okText:"清除暂存", okClass:""});
  });
  /* F26 第三个出口：强制重扫当前目录。
     与「重建索引」的分工 —— 重建丢索引表（贵、期间搜不到），重扫只丢指纹与节流（便宜、
     不动索引表），专治「目录变了界面没动 / 新图搜不到」。
     V2.10.7：实现已抽到 extras.js 的 mvForceRefresh（顶栏「刷新」按钮与 R 键同一份）——
     早先这里自己写过一遍，两处逻辑迟早漂移。 */
  mk("setRescan", ()=>{ if(typeof mvForceRefresh==="function") mvForceRefresh(); });
  /* ---- F28 数据目录位置：两档切换（必须问「要不要把现有数据搬过去」）----
     静默切换的后果是用户以为收藏 / 标签 / 索引全丢了，所以这里**不给静默路径**：
     每次切换都弹三选一（取消 / 不复制 / 复制并切换）。 */
  const ddSegEl=$("#ddMode");
  /* P1-16：切数据目录是「长时间 + 有写副作用」的操作（可能搬几个 G 的文件），
     此前唯一的守卫是 ddState.want —— 它要等**服务端返回后**才被改写，
     所以用户在等待期间连点两次分段按钮，就会并发发两次 /api/datadir：
     两边同时往目标目录写 favorites.json / tags.json，后写的那份会截断前一份。
     用一个 in-flight 标志把窗口期堵死：进行中直接拒绝并提示，等收尾才放开。 */
  let ddBusy=false;
  /* X3：用户是否已在三选一里作出决定（#ddYes / #ddNo）。
     守卫的正常归还是 ddGo 的 finally；但弹窗还有三条「什么也没决定就关掉」的旁路
     —— Esc / 标题栏 ✕ / 点遮罩，它们只调 bigDialog 的 close。此前守卫只挂在
     取消按钮与 ddGo 上 ⇒ 走旁路关掉之后 ddBusy 永远是 true，此后每次点分段按钮都
     只弹「上一次数据目录操作还在进行中」，只有刷新页面才能恢复。
     现在交给 bigDialog 的 onClose 统一收尾：没作出决定就当成「取消」，当场放开。 */
  let ddDecided=false;
  function ddRelease(){
    if(!ddBusy) return;
    ddBusy=false;
    ddDecided=false;
    ddSegEl && ddSegEl.classList.remove("busy");
  }
  /* 审计 F3-P1-2：守卫的时间窗必须覆盖「用户作出决定之后」的整条链路。
     此前 `try{ await ddSwitchInner(mode) } finally{ ddBusy=false }` —— 而 ddSwitchInner
     的函数体里一个 await 都没有（只读状态、建弹窗、绑三个 onclick），所以 await 立刻
     resolve、finally 立刻放行；真正的耗时全在 #ddYes/#ddNo 打开的 ddGo 里（另一个调用栈，
     不在这个 await 链上）。结果是：弹窗还开着、迁移还没开始，守卫已经没了。
     现在释放权交给 ddGo 的 finally 与「取消」按钮；ddSwitchInner 用返回值声明
     「守卫是移交了（true）还是压根没弹窗（false）」。 */
  async function ddSwitch(mode){
    if(ddBusy){ toast("上一次数据目录操作还在进行中，请稍候","err"); return; }
    if(!mode || mode===ddState.want) return;
    ddBusy=true;
    ddDecided=false;                                 // 新一次尝试：还没作出任何决定
    ddSegEl && ddSegEl.classList.add("busy");
    let handedOff=false;
    try{ handedOff = (await ddSwitchInner(mode)) !== false; }
    catch(_){ handedOff=false; }
    if(!handedOff) ddRelease();                      // 前置校验失败 / 异常 ⇒ 当场放开
  }
  async function ddSwitchInner(mode){
    /* ε-13（§6.6）：/api/sysinfo 还没回来（ddState.dir 为空）时不许切换 ——
       此前弹窗里目标路径显示成空串、提交的 mode 却照发后端，用户在
       「读取失败」状态下作出的目录决策是盲的。先等真值到手。 */
    if(!ddState.dir){
      toast("数据目录信息还没读到，稍候再试","err");
      syncSysinfo();
      return false;                                  // 没弹窗 ⇒ 守卫当场还回去
    }
    if(!mode || mode===ddState.want) return false;
    const tgt=(ddState.targets||{})[mode]||"";
    /* X3：onClose 覆盖 Esc / ✕ / 点遮罩 / 被新弹窗顶掉四条旁路。
       已作出决定时守卫归 ddGo 的 finally，这里不许提前放开（否则迁移期间又能再点一次）。 */
    const dlg=bigDialog("切换数据目录",{onClose:()=>{ if(!ddDecided) ddRelease(); }});
    dlg.body.innerHTML=
      '<div style="font-size:calc(13px*var(--fsx));line-height:var(--lh-body)">'
      +'数据目录将改为：<br><code style="font-family:var(--mono);font-size:calc(12px*var(--fsx));'
      +'overflow-wrap:anywhere">'+esc(tgt)+'</code></div>'
      +'<div style="margin-top:calc(10px*var(--fsx));font-size:calc(12.5px*var(--fsx));'
      +'color:var(--tx3);line-height:var(--lh-loose)">'
      +'设置 / 收藏 / 标签 / 最近使用 / 标记 / 索引 / 缩略图缓存 / 回收站都在数据目录里。'
      +'要不要把现在的这一份<b>复制</b>过去？（目标里同名且内容相同的文件会跳过；'
      +'内容不同的，旧文件先备份为 .cmv-conflict-* 再写入，不覆盖）</div>'
      +'<div style="display:flex;justify-content:flex-end;flex-wrap:wrap;'
      +'gap:calc(8px*var(--fsx));margin-top:calc(16px*var(--fsx))">'
      +'<button id="ddCancel" type="button">取消</button>'
      +'<button id="ddNo" type="button">不复制，直接切换</button>'
      +'<button class="primary" id="ddYes" type="button">复制并切换</button></div>';
    dlg.body.querySelector("#ddCancel").onclick=()=>{ ddRelease(); dlg.close(); };
    dlg.body.querySelector("#ddNo").onclick=()=>ddGo(mode,false,dlg);
    dlg.body.querySelector("#ddYes").onclick=()=>ddGo(mode,true,dlg);
    return true;                                     // 守卫移交给了弹窗链路
  }
  /* 迁移进度轮询（A17，2026-11-05 审计复核）。
     原先这段循环内联在 ddGo 里，且「单次请求失败」就 break —— 搬运线程还在后台跑，
     一次瞬时抖动（请求超时 / 端口忙 / 服务短暂重启）就被判成「数据目录切换失败」，
     用户以为数据搬坏了，也不再等它完成。
     现在容忍连续 3 次（≈1.5s）；仍拿不到状态才认定后端真的没了。
     抽成独立函数是为了能被门禁直接驱动（`window.mvDdPollStatus`）—— 内联在 ddGo
     里就只能靠点完整个弹窗链路才能覆盖「前两次失败、第三次成功」这一态。 */
  async function ddPollStatus(){
    const t0=Date.now();
    let pollFail=0;
    for(;;){
      await new Promise(r=>setTimeout(r,500));
      const st=await api("/api/datadir_status").catch(e=>({error:e.message}));
      if(st.error){
        if(++pollFail < 3) continue;          // 瞬时失败：再等一轮，不惊动用户
        return st;
      }
      pollFail=0;
      if(st.state==="done"||st.state==="error") return st.result||{error:"迁移失败"};
      if(Date.now()-t0>1800000) return {error:"迁移超过 30 分钟仍未完成，请查看服务日志"};
    }
  }
  window.mvDdPollStatus = ddPollStatus;       // 供门禁/其它模块调用（内联循环无法单测）

  async function ddGo(mode, migrate, dlg){
    ddDecided=true;                   // 已决定：本次关窗不再释放守卫，归下面的 finally
    if(dlg) dlg.close();
    try{
    setBotState("busy", migrate?"正在迁移数据目录…":"正在切换数据目录…", null, true);
    let j=await api("/api/datadir",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mode:mode, migrate:!!migrate})})
      .catch(e=>({error:e.message}));
    /* ε-12（§6.3）：迁移已挪后台线程 —— 拿到 migrating 就轮询
       /api/datadir_status 到 done/error，再走统一的收尾分支。
       好处：迁移几个 G 时请求线程不再被独占，浏览器 fetch 也不会
       挂到代理/服务器超时；UI 侧 busy 状态由本循环维持，语义不变。 */
    if(j.migrating){
      /* 轮询细节见 ddPollStatus（A17：容忍连续 3 次瞬时失败） */
      j = await ddPollStatus();
    }
    if(j.error){
      setBotState("err","数据目录切换失败",null);
      toastErr("数据目录切换失败", j.error);
      syncSysinfo();
      return;
    }
    const mv=j.migrated||{};
    if(j.same){
      toast("数据目录未变化");
    }else if(j.partial){
      /* P0-2：有文件没搬过去 —— 绝不能报「已切换…复制 N 个文件」让用户以为搬完了。
         走警告色 + 明确的「数据还在原目录」，避免用户重启后以为收藏/设置丢了。 */
      const where=(mode==="portable")?"程序目录":"用户目录";
      setBotState("warn","数据目录已切换，但有文件未复制",null,true);
      toastErr("已切换为"+where+"，但 "+mv.failed+" 个文件未能复制",
               j.warning||"这些文件仍在原目录，请手工处理后再重启");
    }else{
      const where=(mode==="portable")?"程序目录":"用户目录";
      const how=migrate
        ? ("，复制 "+mv.files+" 个文件"+(mv.skipped?("（跳过 "+mv.skipped+" 个已存在）"):"")
           +(mv.conflicts?("，"+mv.conflicts+" 个同名旧文件已备份为 .cmv-conflict-*"):""))   /* ε-11 §6.4 */
        : "，未复制数据";
      toast("已切换为"+where+how,"ok");
    }
    if(j.restart_required){
      /* 那 8 个路径常量在进程启动时算好，运行中改不了 —— 如实告知并给一键重启。
         ε-6 E6-6：文案**必须同时说清「重启前的新改动仍写进原目录」**。
         后端 `datadir_switch` 只写标记、不改内存里的 DATA_DIR（这是**有意**的：
         半个进程用旧目录、半个用新目录比「重启后统一生效」糟得多），
         但此前只说「重启后生效」⇒ 用户很容易以为**此刻起就已经写新目录**了。
         于是这段时间打的标签/收藏/标记其实落在旧目录，重启后「看起来丢了」，
         而它们其实好好躺在原目录里 —— 「以为是新目录、其实是旧目录」这类误解
         正是最该避免的（P0-2 同款）。 */
      const go=()=>{ if(window.mvRestart) window.mvRestart(); };
      setBotState("warn",
                  j.partial?"有文件未复制，仍重启到新目录？"
                           :"数据目录已切换，重启前的新改动仍写入原目录",
                  {label:"立即重启",fn:go},true);
      toastAction("重启前的新改动仍写入原目录，建议立即重启","立即重启",go);
    }else{
      setBotState("idle","已就绪",null,true);
    }
    syncSysinfo();
    }finally{
      /* 成功 / 失败 / 30 分钟上限 / 轮询异常，四类出口一律在这里放开守卫。
         这是唯一覆盖「用户已经作出决定之后」整条链路的时机。 */
      ddRelease();
    }
  }
  if(ddSegEl) ddSegEl.addEventListener("click",e=>{
    const b=e.target.closest(".segbtn");
    if(b) ddSwitch(b.dataset.m);
  });
  mk("setKeys", ()=>{ close(); if(window.mvOpenKeys) window.mvOpenKeys(); });

  setBotState("idle","已就绪",null,true);             // 初始态归位（V3）

  /* ---- V2 回收站角标：把「里面有 N 件」摆在顶栏按钮上（省掉「点开才知道」）----
     每 30 秒轮询一次（本地 JSON 列表，开销可忽略）；删除/还原后由各操作点主动调一次。 */
  window.mvSetTrashBadge=function(n){
    const b=$("#trashBadge"), btn=$("#btnTrash");
    if(!b) return;
    n=n|0;
    if(n>0){
      b.hidden=false; b.textContent = n>99 ? "99+" : String(n);
      if(btn) btn.classList.add("has-badge");
    }else{
      b.hidden=true; b.textContent="";
      if(btn) btn.classList.remove("has-badge");
    }
  };
  let badgeTok=0;
  window.mvUpdateTrashBadge=async function(){
    const tok=++badgeTok;
    let n;
    try{ const j=await api("/api/trash"); n=((j&&j.items)||[]).length; }
    catch(e){ return; }
    if(tok!==badgeTok) return;
    window.mvSetTrashBadge(n);
  };
  window.mvUpdateTrashBadge();
  // 常驻轮询（故意不 clear）：回收站角标每 30s 对一次数，页面隐藏时跳过。生命期=整个页面。
  setInterval(function(){ if(!document.hidden) window.mvUpdateTrashBadge(); },30000);
})();

/* ==================================================================
   6. 快捷键速查面板（V17）
   原先底栏 ⌨ 弹的是一行 `white-space:nowrap` 的提示条，条目早就排到超宽
   （`/` `←→` `Esc` `F` `T` `[ ]` `Del` `P` `空格` …），再加一条就溢出。
   改成分组卡片：内容写在下面的 KEY_GROUPS 里 —— 加 / 改一条快捷键只动这一处，
   不碰 index.html。`?` 随时唤出，Esc / 点遮罩 / 点 ✕ 关闭。

   本 IIFE 刻意放在 settings.js 末尾：Esc 与 Tab 走**捕获阶段**，而设置抽屉自己也有一套
   捕获监听（Esc 关闭 + 焦点陷阱）。同阶段同节点按注册顺序触发，注册在前的先跑；
   放最后注册才能让「速查面板开着时」的按键由本面板说了算。
   ================================================================== */
(function setupKeysPanel(){
  const mask=$("#hintMask"), panel=$("#hintPanel"), body=$("#hintBody");
  if(!mask || !panel || !body) return;
  mask.hidden=false;                 // hidden 只作初始态；开合一律走 .on 才能过渡

  /* 分组只列**真的绑了键**的项 —— 面板里写一条没实现的快捷键，比不写更糟。
     键名以「显示给人看」为准（←/→ 而不是 ArrowLeft），说明里带上生效前提。 */
  const KEY_GROUPS=[
    {g:"浏览", items:[
      [["/"],          "聚焦搜索框"],
      [["R"],          "刷新当前目录（立即重扫，等同顶栏刷新按钮）"],
      [["Esc"],        "清空搜索（光标在搜索框内时）"],
      [["?"],          "唤出 / 关闭本面板"],
    ]},
    {g:"视图", items:[
      [["T"],          "时间轴 ⇄ 网格"],
      [["N"],          "内容显示方式：隐藏 ⇄ 模糊 ⇄ 显示"],
      [["[","]"],      "时间轴：上一组 / 下一组"],
      [["↑","↓"],      "下拉菜单里移动选择"],
      [["Home","End"], "跳到第一项 / 最后一项"],
    ]},
    {g:"大图", items:[
      [["←","→"],      "上一张 / 下一张"],
      [["Esc"],        "关闭大图"],
      [["P"],          "详情面板"],
      [["F"],          "底部预览栏"],
      [["T"],          "聚焦标签输入"],
      [["Del"],        "删除当前图"],
      /* 2026-11-09：像素快查。手势没有任何可见痕迹，这条是最后的兜底说明 ——
         左下角还有一颗常驻按钮（点一下即可，不必按键）。 */
      [["Ctrl"],       "像素快查：按住 + 指针移到大图上，放大看像素细节"],
      [["中键"],       "钉住 / 取消钉住快查镜片"],
    ]},
    {g:"视频", items:[
      [["空格"],       "播放 / 暂停"],
      [[",","."],      "上一帧 / 下一帧"],
      [["L"],          "循环播放"],
      [["M"],          "静音"],
    ]},
    {g:"返回与关闭", items:[
      [["Esc"],        "逐层关闭：大图 → 对比 → 批量选择"],
      [["Esc"],        "关闭弹窗 / 设置抽屉 / 本面板"],
      [["Enter"],      "确认（各类弹窗与内联重命名）"],
    ]},
  ];

  body.innerHTML=KEY_GROUPS.map(function(grp){
    const rows=grp.items.map(function(it){
      const keys=it[0].map(function(k){ return "<kbd>"+esc(k)+"</kbd>"; }).join("");
      return '<div class="hint-row"><span class="hint-keys">'+keys+"</span>"
             +"<span>"+esc(it[1])+"</span></div>";
    }).join("");
    return '<div class="hint-grp"><div class="hint-gl">'+esc(grp.g)+"</div>"+rows+"</div>";
  }).join("");

  let lastFocus=null;
  const isOpen=()=>mask.classList.contains("on");
  function open(){
    if(isOpen()) return;
    lastFocus=document.activeElement;
    mask.classList.add("on");
    setTimeout(()=>{ const b=$("#hintClose"); if(b) b.focus(); },60);
  }
  function close(){
    if(!isOpen()) return;
    mask.classList.remove("on");
    if(window.mvSyncInert) window.mvSyncInert();          // G2·V16③：先解冻（同上）
    if(lastFocus && lastFocus.focus) lastFocus.focus();   // 焦点归还触发器
    lastFocus=null;
  }
  window.mvOpenKeys=open;
  window.mvCloseKeys=close;
  window.mvToggleKeys=()=>{ isOpen()?close():open(); };

  $("#hintClose").onclick=close;
  const trig=$("#hintTrig");                            // 底栏 ⌨ 仍然是入口之一
  if(trig){
    trig.onclick=()=>window.mvToggleKeys();
    /* span 挂了 role=button + tabindex=0 就要自己接 Enter/Space —— 只给 onclick 的话
       键盘用户按下去没反应（原生 button 才有这个默认行为）。 */
    trig.addEventListener("keydown",e=>{
      if(e.key==="Enter" || e.key===" "){ e.preventDefault(); window.mvToggleKeys(); }
    });
  }
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });

  /* 捕获阶段：大图那套按键是同在 document 上的冒泡监听，不拦住的话
     关掉本面板的同时还会顺手把大图一起关掉。 */
  document.addEventListener("keydown",e=>{
    if(isOpen()){
      if(e.key==="Escape" || e.key==="?"){
        e.stopPropagation(); e.preventDefault(); close(); return;
      }
      if(e.key==="Tab"){                                 // 焦点陷阱：面板只有一个焦点位
        e.stopPropagation(); e.preventDefault();
        const b=$("#hintClose"); if(b) b.focus();
      }
      return;
    }
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    const el=e.target, tg=(el.tagName||"").toUpperCase();
    if(tg==="INPUT" || tg==="TEXTAREA" || tg==="SELECT" || el.isContentEditable) return;
    if(e.key==="?"){ e.preventDefault(); open(); }        // Shift+/ —— 与 `/` 搜索不冲突
  },true);
})();


/* ============================================================
   内容安全（V2.11.0）
   ============================================================
   前端只负责三件事：显示方式、把用户操作发给服务端、把进度画出来。
   判定、阈值、缓存、模型全在服务端 —— 前端不做任何推理，也不自己算分。
   ============================================================ */
(function setupNsfw(){
  const segBar = $("#nsfwSeg");          // 工具条上的三态控件（红框那个位置）
  const segSet = $("#nsfwModeSeg");      // 设置页里同一套 —— 两个入口，一个真相
  const ctl = $("#nsfwCtl");             // 标签 + 三档的整组容器（off 状态挂这里）
  let pollTimer = null;
  let scanPulsed = false;                // 「开始检测」脉冲只放一次（首次从未跑过翻到跑过）
  /* 左侧标签的红眼睛：和被遮挡卡片左上角那只同色，扫一眼就知道这排按钮管什么 */
  { const li = $("#nsfwLblIco"); if(li) li.innerHTML = ico("eye"); }
  const NSFW_TITLES = {
    hide:"隐藏：敏感内容从列表里剔除（快捷键 N 循环切换）",
    blur:"模糊：敏感内容显示为毛玻璃，点卡片左上角的眼睛可单独查看（快捷键 N 循环切换）",
    show:"显示：不做任何处理（快捷键 N 循环切换）"};

  /* ---- 显示方式：工具条与设置页两处控件同步 ----
     三种状态，判据各不同（V2.12.6 加的后两态）：

       ready      引擎可用 + 跑过检测  → 正常，三档可点
       undetected 引擎可用 + 没跑过    → **琥珀**提醒「未检测」，点击给去开启的出路
       noengine   引擎不可用           → 整组置灰，红眼也一起**去色变灰**，提示说清原因

     ⚠️ 两个 class 挂在 #nsfwCtl（整组：标签 + 三档）上，不是挂在 seg 上 ——
        只挂 seg 会出现「字是亮的、按钮是灰的」。
     ⚠️ undetected 与 noengine **互斥**，且 ready 优先 —— 三态由同一次调用算出来，
        分两个 if 写容易出现「两个 class 同时挂上」的自相矛盾态。 */
  function nsfwSyncUI(){
    for(const s of [segBar, segSet]){
      if(!s) continue;
      for(const b of s.querySelectorAll(".segbtn")){
        const on = b.dataset.n === NSFW_MODE;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", String(on));
      }
    }
    if(!ctl) return;
    const ready = nsfwReady();
    const noEng = nsfwNoEngine();
    const undet = !noEng && !ready;        // 引擎在、但一次都没跑过
    ctl.classList.toggle("undetected", undet);
    ctl.classList.toggle("noengine", noEng);
    /* 提示语三态各一套，走 data-tip（D2 起全站唯一的提示出口），不写 title。
       回退值也读 data-tip —— 读 b.title 会拿到 undefined，
       等于「引擎不可用 ⇄ 可用」切换一次之后提示就空了。 */
    for(const b of segBar.querySelectorAll(".segbtn"))
      b.setAttribute("data-tip", ready
        ? (NSFW_TITLES[b.dataset.n] || b.getAttribute("data-tip") || "")
        : undet
          ? "尚未检测：先在设置页跑一次内容检测，这三档才会生效"
          : "内容检测不可用：" + ((NSFW_INFO && NSFW_INFO.reason) || "未知原因"));
    /* 标签自身的提示也换 —— 用户最先悬停到的就是那块「敏感内容」 */
    const lbl = segBar.querySelector(".nsfw-lbl");
    if(lbl) lbl.setAttribute("data-tip", ready
      ? "敏感内容显示方式（NSFW）"
      : undet ? "敏感内容：尚未检测（点此去开启）"
              : "敏感内容：检测不可用 —— " + ((NSFW_INFO && NSFW_INFO.reason) || "未知原因"));

    /* 设置页那行琥珀警示：未检测时说明「为什么不生效 + 怎么办」，就绪后收起来。
       hidden 属性而不是清空文本 —— 清空会留一个空的 .set-gh（它有 margin）。 */
    const warn = $("#nsfwModeWarn");
    if(warn){
      warn.hidden = !undet;
      if(undet) warn.textContent = "尚未检测 —— 这三档现在切换不会真正生效："
        + "库里的图还没被判定过（都算「未知」）。先点上面的「开始检测」跑一次全库扫描，"
        + "扫完这三档自动生效。";
    }
    /* 「开始检测」脉冲高亮：只从「未跑过」首次翻到「跑过」时闪一下，
       像「保存成功」那样给个收束信号。⚠️ 与 nsfwNudge 的 .flash 是同一个 class，
       用完都摘掉，不会互相顶掉（nudge 是 1.4s 定时摘，这里是首次翻转触发）。 */
    if(ready && !scanPulsed){
      scanPulsed = true;
      const b = $("#nsfwScan");
      if(b){
        b.classList.add("flash");
        setTimeout(()=>b.classList.remove("flash"), 1400);
      }
    }
  }
  window.nsfwSyncUI = nsfwSyncUI;

  /* ---- 未就绪时的统一出路（工具条点击、快捷键 N 都走这里）----
     两种处境给两条不同的路：
       undetected → 「去开启」直接开设置页的内容安全页签，并把「开始检测」按钮高亮一下
       noengine   → 没有可点的动作，只能把原因说清（缺库还是缺模型，设置页里分得清）

     ⚠️ 用 toastAction（app.js:365 的现成原语）而不是 toast：这个提示是要**点**的，
        文案里写「请去设置页」而没有任何可点入口，等于把用户扔在原地。
     ⚠️ 同文案的 toast 会被 _toastPush 合并成「×N」并续命 —— 连点三档不会刷屏，
        这正好是想要的（按钮本身是 <button>，不是 a，不会有跳转副作用）。 */
  window.nsfwNudge = function nsfwNudge(){
    if(nsfwNoEngine()){
      toast("内容检测不可用：" + ((NSFW_INFO && NSFW_INFO.reason) || "未安装检测引擎"), "err");
      return;
    }
    toastAction("尚未检测：敏感内容三档要跑过一次检测才生效", "去开启", ()=>{
      if(typeof mvOpenSettings === "function") mvOpenSettings();
      /* showTab 在 settings 的闭包里，这里够不着 —— 直接点页签（等价且不新增出口）。
         点击后 showTab 会自己 nsfwLoadStatus() 取一次真值。 */
      const t = document.querySelector('.set-tab[data-tab="nsfw"]');
      if(t) t.click();
      /* 把话说到底：停在设置页不代表检测跑了，得点「开始检测」。
         高亮那枚按钮 + 落到提示行，用户不用在整页里找。 */
      const b = $("#nsfwScan");
      if(b){
        b.classList.add("flash");          // 样式在 nsfw.css，1.4s 后自己摘掉
        setTimeout(()=>b.classList.remove("flash"), 1400);
        try{ b.focus({preventScroll:false}); }catch(_){}
      }
      toast("点「开始检测」跑一次全库扫描，完成后这三档自动生效", "info", 6000);
    }, "warn");
  };

  for(const s of [segBar, segSet]){
    if(!s) continue;
    s.addEventListener("click", e=>{
      const b = e.target.closest ? e.target.closest(".segbtn") : null;
      if(!b || !b.dataset.n) return;
      /* 未就绪（没装引擎 / 装了没跑过）都在 setNsfwMode 里统一拦 ——
         拦的时候它会调 nsfwNudge() 给出路。
         ⚠️ 这里**不再**自己判断 nsfwOn() 后 return：早期版本在这里 early-return，
            漏掉了「引擎可用但没跑过」那种状态，于是点下去弹「已隐藏」而什么都藏不住。 */
      setNsfwMode(b.dataset.n);
    });
  }

  /* ---- 状态行 / 档位 / 阈值 ---- */
  function syncStatusUI(){
    const I = NSFW_INFO || {};
    const on = nsfwOn();
    const st = $("#nsfwStateRow");
    if(st) st.textContent = !I.available ? "不可用"
                          : (I.enabled === false ? "已关闭" : "可用");
    const cnt = $("#nsfwCountRow");
    if(cnt){
      const c = I.counts || {};
      cnt.textContent = I.available
        ? ((c.nsfw||0) + " 张 NSFW · " + (c.sfw||0) + " 张安全"
           + ((c.unknown|0) ? " · " + c.unknown + " 未识别" : ""))
        : "—";
    }
    const mn = $("#nsfwManualRow");
    if(mn) mn.textContent = String(I.manual || 0);
    /* B9（2026-10-07 文案梳理）：本地推理这句与本区副标题「全本地推理，图片不出这台机器」
       同义，删说明段；但**不可用原因**（I.reason）tooltip 覆盖不到，必须留着。
       用 hidden 而非清空 —— 空的 .set-gh 仍占 margin。 */
    const hint = $("#nsfwHint");
    if(hint){
      hint.hidden = !!I.available;
      hint.textContent = I.available ? "" : (I.reason || "");
    }

    /* F3-P2-5（2026-10-19）：这一整段在**扫描期间每 800ms** 被调一次（轮询 → syncStatusUI）。
       ① `sel.innerHTML` 整片重建 ⇒ 用户展开着的下拉会被合上/重排，选中态被服务端旧值刷回；
       ② `thr.value` 直接回写 ⇒ 正在拖的阈值滑块每 0.8 秒**弹回一次**，等到 change 触发时
          读到的已是弹回后的旧值，界面还会说一句「阈值 X 已生效」，而 X 是旧值。
       现在：选项列表只在**内容真的变了**时重建（签名比对），并且焦点在控件上时一律不回写
       （照 `#trashKeepDays` 已有的写法）。 */
    const sel = $("#nsfwModelSel");
    if(sel && (I.tiers || []).length){
      const sig = JSON.stringify((I.tiers || []).map(t =>
        [t.id, t.label, t.desc, !!t.ready, t.id === I.model]));
      if(sel._mvSig !== sig){
        sel._mvSig = sig;
        sel.innerHTML = I.tiers.map(t =>
          '<option value="' + esc(t.id) + '"'
          + (t.id === I.model ? " selected" : "")
          + (t.ready ? "" : " disabled") + '>'
          + esc(t.label + " · " + t.desc) + (t.ready ? "" : "（未安装）") + "</option>").join("");
      }
      sel.disabled = !on;
    }
    const thr = $("#nsfwThr"), tv = $("#nsfwThrVal");
    const t = (I.threshold != null ? I.threshold : 0.60);
    const _nsfwEditing = (document.activeElement === thr) || (document.activeElement === sel);
    if(thr){ if(!_nsfwEditing) thr.value = String(Math.round(t * 100)); thr.disabled = !on; }
    if(tv && !_nsfwEditing) tv.textContent = t.toFixed(2);
    const cm = $("#nsfwClearManual");
    if(cm) cm.disabled = !on || !(I.manual || 0);
    nsfwSyncUI();
  }

  function syncProgUI(){
    const I = NSFW_INFO || {};
    const fill = $("#nsfwFill"), txt = $("#nsfwProgTxt"), num = $("#nsfwProgNum");
    const tot = I.total || 0, done = I.done || 0;
    const pc = tot ? Math.round(done / tot * 100) : 0;
    if(fill) fill.style.width = (I.building ? Math.max(4, pc) : (tot ? 100 : 0)) + "%";
    if(txt) txt.textContent = I.building ? "正在检测…" : (tot ? "检测完成" : "尚未检测");
    if(num) num.textContent = done + " / " + tot;
    for(const b of [$("#nsfwScan"), $("#nsfwRescan")])
      if(b) b.disabled = !!(I.building) || !(I.available);
  }

  /* ---- 取状态。quiet=true 时不弹提示（轮询用）----

     失败处理是这里的关键，V2.11.0 在这上面栽过跟头：
     扫描刚开始时的第一个状态请求偶尔会拿到异常响应（HTML 而不是 JSON），
     api() 解析失败抛异常 —— 老代码在 catch 里把 NSFW_INFO 清成 null 且不续轮询，
     于是「正在检测」永远不出现，设置页一直停在 0 / 0（用户实报的 bug）。

     现在：已知有扫描在进行（nsfwScanning）时，失败只保留旧状态继续重试；
     连续失败很多次且确实没有扫描，才按「不可用」处理。 */
  let nsfwFails = 0;
  let nsfwScanning = false;
  let nsfwWasBuilding = false;
  async function nsfwLoadStatus(quiet){
    try{
      const j = await api("/api/nsfw/status");
      nsfwFails = 0;
      const justFinished = nsfwWasBuilding && !j.building;   // 扫描刚结束的那一拍
      nsfwScanning = !!j.building;
      nsfwWasBuilding = nsfwScanning;
      NSFW_INFO = j;
      syncStatusUI(); syncProgUI();
      if(j.building && !pollTimer)
        pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
      if(!j.building && pollTimer){ clearInterval(pollTimer); pollTimer = null; }
      if(justFinished) nsfwReloadAfterScan();
      return j;
    }catch(e){
      nsfwFails++;
      if(nsfwScanning && nsfwFails < 12){
        if(!pollTimer) pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
        return null;                      // 扫描还在跑：这次没拿到就下一拍再拿
      }
      /* F3-P2-1（2026-10-19）：失败计数越过上限后，`pollTimer` 还在跑 —— 上面那两处
         `if(!pollTimer) pollTimer=setInterval(...)` 已经把它挂上，而本分支原来**没有
         clearInterval**，且每个 tick 都带 quiet=true（不再弹 toast）⇒ 这条 800ms 轮询
         再无任何出口，直到某次请求成功或页面重载。表现为「扫描早结束了，界面还在
         每 0.8 秒发一次请求 + 重绘」。 */
      if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
      NSFW_INFO = null;
      syncStatusUI(); syncProgUI();
      if(!quiet) toast("内容安全状态读取失败：" + (e.message || e), "err");
      return null;
    }
  }
  window.nsfwLoadStatus = nsfwLoadStatus;

  /* 扫描结束 → 必须重拉一次列表。
     这是「换根目录后重扫也不生效」的根因：nsfwRefreshList 在点击后 400ms 就跑了
     （那时扫描刚开始），而扫描结束时**文件本身没变** → 目录签名不变 →
     自动刷新永远不触发 → 网格里的 n/ns 还是扫描前拉回来的旧值（全 unknown）。
     服务端算得再对，界面不知道，用户看到的就是「功能失效」。 */
  function nsfwReloadAfterScan(){
    setTimeout(()=>{
      try{
        if(typeof silentReload === "function") silentReload("检测完成，列表已更新");
        else load(S.path || "");
      }catch(e){ try{ applyFilter(); }catch(_){} }
    }, 250);
  }

  const scan = $("#nsfwScan"), rescan = $("#nsfwRescan");
  if(scan) scan.onclick = ()=>nsfwRunScan(false);
  if(rescan) rescan.onclick = ()=>nsfwRunScan(true);
  async function nsfwRunScan(force){
    try{
      const r = await api("/api/nsfw/scan" + (force ? "?force=1" : ""),
        {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      if(r && r.ok !== false){
        nsfwScanning = true;              // 先立标记：首个状态请求失败也要继续轮询
        if(!pollTimer) pollTimer = setInterval(()=>{ nsfwLoadStatus(true); }, 800);
        toast(force ? "已丢弃旧结果，重新检测中…" : "开始检测…");
        nsfwLoadStatus(true);
        /* 不在这里刷新列表：+400ms 时扫描还没跑完，拉回来的是旧数据。
           列表刷新由 nsfwLoadStatus 检测到「扫描刚结束」的跳变来做。 */
      }else{
        toast((r && r.error) || "检测失败", "err");
      }
    }catch(e){ toast("检测失败：" + (e.message || e), "err"); }
  }
  /* 检测完成后列表里每张的 n/ns 都变了，得让网格重取一次。
     用现有的 load() 而不是 applyFilter()：后者只重排内存里那批旧数据，拿不到新字段。 */
  function nsfwRefreshList(){
    setTimeout(()=>{
      try{ load(S.path || ""); }catch(e){ try{ applyFilter(); }catch(_){} }
    }, 400);
  }

  /* ---- 阈值：改完立刻生效，不用重新检测 ----
     分数已经躺在每条数据里（ns 字段），前端按新阈值重算一遍即可。 */
  const thr = $("#nsfwThr");
  if(thr){
    thr.addEventListener("input", ()=>{
      const v = +thr.value / 100;
      const tv = $("#nsfwThrVal"); if(tv) tv.textContent = v.toFixed(2);
    });
    thr.addEventListener("change", async ()=>{
      const v = +thr.value / 100;
      try{
        const r = await api("/api/prefs", {method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({nsfw_threshold:v})});
        if(r && r.ok){
          NSFW_INFO = Object.assign({}, NSFW_INFO || {}, {threshold:r.nsfw_threshold});
          const tv = $("#nsfwThrVal"); if(tv) tv.textContent = r.nsfw_threshold.toFixed(2);
          toast("阈值 " + r.nsfw_threshold.toFixed(2) + " 已生效（无需重新检测）");
          applyFilter();                    // 同一批分数，换个结论
        }else toast((r && r.error) || "阈值写入失败", "err");
      }catch(e){ toast("阈值写入失败：" + (e.message || e), "err"); }
    });
  }

  /* ---- 档位切换：换了模型，旧分数全部作废，必须重扫 ---- */
  const sel = $("#nsfwModelSel");
  if(sel) sel.addEventListener("change", async ()=>{
    const id = sel.value;
    try{
      const r = await api("/api/prefs", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({nsfw_model:id})});
      if(r && r.ok){
        NSFW_INFO = Object.assign({}, NSFW_INFO || {}, {model:id});
        toast("已切换到 " + id + " 档，需要重新检测");
        nsfwRunScan(true);
      }else toast((r && r.error) || "切换失败", "err");
    }catch(e){ toast("切换失败：" + (e.message || e), "err"); }
  });

  /* ---- 清空人工纠正 ---- */
  const cm = $("#nsfwClearManual");
  if(cm) cm.onclick = async ()=>{
    if(!confirm("清空全部人工纠正？\n\n这些图会回到模型判定，下次检测后可能被重新标成 NSFW。")) return;
    try{
      const r = await api("/api/nsfw/label", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({clear:true})});
      if(r && r.ok){
        toast("已清空人工纠正");
        nsfwLoadStatus(true); nsfwRefreshList();
      }else toast((r && r.error) || "清空失败", "err");
    }catch(e){ toast("清空失败：" + (e.message || e), "err"); }
  };

  /* ---- 找回旧目录数据（换根迁移手动兜底）----
     自动迁移靠指纹命中率判断「是不是搬家」，目录结构变过的场景它不敢动；
     这里用户显式指定旧根，后端跳过门槛但保留逐条验证。 */
  const mb = $("#migRun");
  if(mb) mb.onclick = async ()=>{
    const inp = $("#migOldRoot");
    const old = (inp && inp.value || "").trim();
    if(!old){ toast("请先填旧的根目录完整路径", "err"); return; }
    if(!confirm("把旧目录「" + old + "」的检测结果、人工纠正、收藏、标签、标记迁移到当前根目录？\n\n迁移只改路径不动内容，原数据不会被删除。")) return;
    mb.disabled = true;
    try{
      const r = await api("/api/root/migrate", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({old_root:old})});
      if(r && r.ok && r.migrated){
        mvToastMigrate(r);
        if(inp) inp.value = "";
        nsfwLoadStatus(true); nsfwRefreshList();
      }else if(r && r.ok){
        toast((r.reason || "没有可迁移的条目"), "err");
      }else toast((r && r.error) || "恢复失败", "err");
    }catch(e){ toast("恢复失败：" + (e.message || e), "err"); }
    mb.disabled = false;
  };

  /* ---- 快捷键 N：循环三态（与 T 切时间轴同款）----
     捕获阶段注册，避免和输入框抢键。 */
  document.addEventListener("keydown", e=>{
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    const el = e.target, tg = (el.tagName || "").toUpperCase();
    if(tg === "INPUT" || tg === "TEXTAREA" || tg === "SELECT" || el.isContentEditable) return;
    if(e.key !== "n" && e.key !== "N") return;
    if(document.body.classList.contains("modal-on")) return;
    /* 未就绪时**也要拦下这次按键**（preventDefault）再给提示：
       不拦的话 N 会一路落到别处（比如被其他模块吃掉当别的快捷键），
       而用户看到的只是「按了 N 什么都没发生」—— 和这次要修的毛病一模一样。
       setNsfwMode 内部已拦并调 nsfwNudge()，所以这里只需防默认 + 交出去。 */
    if(!nsfwReady()){
      e.preventDefault();
      setNsfwMode(NSFW_MODE);        // 状态不变，只借它走到 nsfwNudge()
      return;
    }
    e.preventDefault();
    setNsfwMode(NSFW_MODES[(NSFW_MODES.indexOf(NSFW_MODE) + 1) % NSFW_MODES.length]);
  }, true);
})();

/* ================= 局域网访问（手机 / 另一台电脑） =================
   设计取舍：
   · 入口是 **6 位一次性验证码**，不是二维码 —— 另一台电脑没摄像头，扫不了；
     12 位随机令牌人眼又敲不动。验证码 5 分钟有效、用一次即换。
   · 配对后由服务端设置 Cookie，复制的地址不含凭证；
   · 本机地址永远是 127.0.0.1（见后端 _lan_status 的 local 分支），
     不然在电脑上点「开启」会把自己也降级成只读；
   · 远程只读不是靠前端藏按钮，而是后端按来源地址拒绝 —— 前端只负责说清楚。 */
(function bindLan(){
  const btn=document.getElementById("lanTgl");
  if(!btn) return;
  const $=id=>document.getElementById(id);
  let busy=false, codeLeft=0, codeTimer=null;

  function stopTimer(){ if(codeTimer){ clearInterval(codeTimer); codeTimer=null; } }

  function paintLeft(){
    const el=$("lanCodeLeft");
    const s=Math.max(0, codeLeft|0);
    if(el) el.textContent = Math.floor(s/60)+":"+String(s%60).padStart(2,"0");
    if(s<=0){ stopTimer(); loadCode(false); }
  }

  async function loadCode(rotate){
    if(!$("lanCode")) return;
    const j = rotate
      ? await api("/api/lan/code",{method:"POST"}).catch(()=>null)
      : await api("/api/lan/code").catch(()=>null);
    if(!j || j.error || !j.code) return;          // 远程访问等情况下静默（面板已隐藏）
    $("lanCode").textContent = j.code;
    codeLeft = Number(j.left)||0;
    stopTimer();
    paintLeft();
    codeTimer = setInterval(()=>{ codeLeft--; paintLeft(); }, 1000);
  }

  function chip(ro){
    const c=$("roChip");
    if(c) c.hidden=!ro;
    document.body.classList.toggle("ro", !!ro);
  }

  function render(j){
    const on=!!j.on;
    if($("lanState")) $("lanState").textContent = on ? "已开启" : "未开启";
    btn.textContent = on ? "关闭" : "开启";
    btn.disabled = !!j.readonly;
    // 远程设备自己看不到验证码（后端 403），索性别把这块亮出来
    const box=$("lanBox"); if(box) box.hidden = !on || !!j.readonly;
    if(on && !j.readonly){
      if($("lanUrl")) $("lanUrl").textContent = j.lan_url || "未检测到可用的局域网 IPv4 地址";
      if($("lanCopy")) $("lanCopy").disabled = !j.lan_url;
      if(!codeTimer) loadCode(false);
    }
    if(!on) stopTimer();
    const hint=$("lanHint");
    if(hint){
      hint.textContent = j.readonly
        ? "当前是手机 / 其它设备访问：只读。可以浏览、搜索、收藏、打标签、下载；删除、换目录、重建索引等需要回到电脑上操作。"
        : (on ? "若弹出 Windows 防火墙提示请选「允许」（专用网络即可）；本功能不会暴露到公网。"
              : "开启后，手机或另一台电脑与这台电脑连同一个 Wi-Fi 即可访问。远程一律只读，收藏与标签照常。");
    }
    chip(!!j.readonly);
  }

  async function sync(){
    const j=await api("/api/lan").catch(()=>null);
    if(j && !j.error) render(j);
  }

  btn.onclick=async()=>{
    if(busy) return;
    busy=true;
    const wasOn = btn.textContent === "关闭";
    btn.disabled=true;
    const j=await api("/api/lan/toggle",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({on:!wasOn})}).catch(e=>({error:e.message}));
    busy=false; btn.disabled=false;
    if(j.error){ toast("切换失败："+j.error,"err"); sync(); return; }
    toast(wasOn ? "局域网访问已关闭" : "局域网访问已开启", "ok");
    render(j);
  };

  if($("lanCodeNew")) $("lanCodeNew").onclick=()=>{
    toast("已换成新的验证码，旧码随即作废","ok");
    loadCode(true);
  };

  if($("lanCopy")) $("lanCopy").onclick=async()=>{
    const u=($("lanUrl")&&$("lanUrl").textContent)||"";
    /* 2026-11-15：改走 `mvCopyText`（app.js）。此前是裸调 clipboard API ——
       而 `navigator.clipboard` 只在**安全上下文**（https / localhost）存在，局域网
       `http://192.168.x.x` 下是 `undefined` ⇒ 这个按钮恰好在它唯一被需要的地方失灵。
       `mvCopyText` 内建 textarea + `execCommand("copy")` 兜底，正是为这条路径写的。 */
    try{ await window.mvCopyText(u); toast("地址已复制","ok"); }
    catch(e){ toast("复制失败，请手动选中地址","err"); }
  };

  if($("lanRotate")) $("lanRotate").onclick=async()=>{
    const j=await api("/api/lan/rotate",{method:"POST"}).catch(e=>({error:e.message}));
    if(j.error){ toast("换令牌失败："+j.error,"err"); return; }
    toast("令牌已更新：已连过的设备需要重新输验证码","ok");
    stopTimer();
    render(j);
  };

  const tab=document.getElementById("tabData");
  if(tab) tab.addEventListener("click", sync);   // 切到「数据」页时取一次真值
  sync();
})();
