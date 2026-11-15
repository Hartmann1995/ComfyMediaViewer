/* ComfyMediaViewer · js/batch.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 批量选择模式（顶栏「☑ 批量」或右键网格空白处） ---------------- */
let batchOn=false;
let lastBatchIdx=-1;                                   // Shift 连选的锚点（旧下标口径，仅作兜底）
let lastBatchPath="";                                  // 锚点路径：视图重建后仍能定位（P1 修复）
const batchSel=new Set();
/* P1-1：与 server.py 的 DELETE_MAX_PATHS 保持一致。服务端超限时会把被丢弃的路径
   计入 dropped，前端据此不再把它们当成已删除。两边同时改才算改完。 */
const DELETE_MAX_PATHS=20000;
/* F7-P1-2（2026-10-17）：批量删除执行期的防重入标志。
   此前确认框一关就彻底静默，而 `/api/delete` 在几千张量级要跑好几秒（系统回收站 COM
   调用），用户会再点一次 —— 同一批路径被并发提交两遍，服务端重跑一轮 trash_move，
   最后同屏出现两条互相矛盾的 toast，其中「已删除 0 个」那条带的撤销点了必然失败。 */
let BATCH_DEL_BUSY=false;
/* 2026-11-15：**当前筛选条件下的文件全集**，由 filters.js 的 applyFilter / promptSearch
   在每次重建视图时发布（`window.mvPublishVisible`，未分页）。
   为什么需要它：`batchSel` 跨筛选保留是刻意的（换排序、翻页不该丢选择），但此前
   没有任何地方区分「这张在别的页」与「这张已经被筛掉了」——
     勾 20 张 → 左栏点「PNG」→ 屏上只剩 6 张 → 批量条仍写「已选 20」→ 点删除，
     确认框也写「删除选中的 20 个文件」⇒ 14 张此刻屏幕上从没出现过的图被一起删掉。
   现在按这个全集算差值，批量条与删除确认框都会如实说出「其中 N 张已被筛选隐藏」。
   不直接裁剪 batchSel：裁剪会让用户「先筛选再切回来」时莫名其妙丢失选择，
   而选择本身无害，有害的是**不告知就删**。 */
let VISIBLE_PATHS=null;
window.mvPublishVisible=function(arr){
  const s=new Set();
  for(const it of (arr||[])) if(it && (it.type==="image"||it.type==="video")) s.add(it.path);
  VISIBLE_PATHS=s;
  if(batchOn) updateBatchBar();          // 只重算文案/可用态，不动选择集
};
function batchHiddenCount(){
  if(!VISIBLE_PATHS || !batchSel.size) return 0;
  let n=0;
  for(const p of batchSel) if(!VISIBLE_PATHS.has(p)) n++;
  return n;
}

function setBatch(on){
  batchOn=on;
  if(!on){ lastBatchIdx=-1; lastBatchPath=""; }
  document.body.classList.toggle("batch",on);
  if(!on) batchSel.clear();
  syncBatchUI(); updateBatchBar();
  $("#batchBar").classList.toggle("on",on);
  const bb=$("#btnBatch"); if(bb) bb.classList.toggle("on",on);
  /* 批量条与提示条同为「底部居中」⇒ 开关后必须重算避让高度，否则提示条压住批量条 */
  if(window.mvSyncToastLift) window.mvSyncToastLift();
  if(on) toast("已开启批量选择：单击勾选 · Shift 连选 · Esc 退出","ok");
}
function syncBatchUI(){
  grid.querySelectorAll(".card[data-p]").forEach(el=>
    el.classList.toggle("sel", batchSel.has(el.dataset.p)));
}
function updateBatchBar(){
  /* 2026-11-15：「已选 N」必须把「不在当前筛选内」的那部分说出来。
     只写总数会让用户以为点删除只会删屏上这些，实际会把筛选外的也一起删。 */
  const hidden=batchHiddenCount();
  $("#batchN").textContent = hidden
    ? `已选 ${batchSel.size}（${hidden} 张不在当前筛选内）`
    : `已选 ${batchSel.size}`;
  /* F7-P1-2：执行期这三个入口一律锁死。`busy` 必须并进 disabled 判据（而不是在
     onclick 里 `disabled=true`）—— 删除过程中 `batchSel` / `updateBatchBar` 还会被
     别的路径调到，手写一次 disabled 会被下一次 updateBatchBar 直接解掉。 */
  const n=batchSel.size, busy=BATCH_DEL_BUSY;
  $("#bDel").disabled=busy||!n;
  $("#bExp").disabled=busy||!n;
  $("#bTag").disabled=busy;      // 只跟执行期走：空闲时「没勾选也能点进去看提示」是原行为
  $("#bCmp").disabled=busy||!(n>=2&&n<=4);   // 对比仅 2-4 张可用
}
grid.addEventListener("contextmenu",e=>{
  if(e.target.closest(".card")) return;                // 卡片上的右键留给收藏菜单
  if(lb.classList.contains("on")) return;
  e.preventDefault(); e.stopPropagation();   // 阻止冒泡：否则本处理器末尾注册的 once contextmenu 关闭监听会在冒泡阶段立刻移除菜单
  closeCatMenu();
  const m=document.createElement("div"); m.id="catMenu";
  /* 与卡片右键菜单同一个 #catMenu：补一个空图标槽，保持同栅格的行首对齐。
     这里的条目（批量选择 / 全选 / 清除选择）本批不图标化。 */
  const mk=(txt,cls,fn,icon)=>{ const d=document.createElement("div");
    d.className="cmi"+(cls?" "+cls:"");
    d.appendChild(menuCellIcon(icon)); d.appendChild(menuCellText(txt));
    d.onclick=()=>{ m.remove(); fn(); }; m.appendChild(d); };
  mk(batchOn?"退出批量选择":"批量选择","",()=>setBatch(!batchOn), batchOn?null:"batch");
  if(batchOn){
    mk("全选当前视图","",()=>{
      for(const it of S.view)
        if(it.type==="image"||it.type==="video") batchSel.add(it.path);
      syncBatchUI(); updateBatchBar();
    });
    mk("清除选择","",()=>{ batchSel.clear(); syncBatchUI(); updateBatchBar(); });
  }
  document.body.appendChild(m);
  /* 贴边上限与 CSS 同源（V2.10.0 · E4）：#catMenu{min-width:calc(190px*var(--fsx))}。
     字号放大后菜单更宽更高，这里也要跟着放大，否则菜单会被推出视口。 */
  const _fsx=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fsx"))||1;
  m.style.left=Math.min(e.clientX, innerWidth-190*_fsx)+"px";
  m.style.top=Math.min(e.clientY, innerHeight-130*_fsx)+"px";
  document.addEventListener("click",closeCatMenu,{once:true});
  document.addEventListener("contextmenu",closeCatMenu,{once:true});
});
$("#bAll").onclick=()=>{ for(const it of S.view)
  if(it.type==="image"||it.type==="video") batchSel.add(it.path);
  syncBatchUI(); updateBatchBar(); };
$("#bNone").onclick=()=>{ batchSel.clear(); syncBatchUI(); updateBatchBar(); };
$("#bCmp").onclick=()=>openCmp();
$("#cmpExit").onclick=closeCmp;
$("#bExit").onclick=()=>setBatch(false);
$("#btnBatch").onclick=()=>setBatch(!batchOn);
/* H5（2026-10-06）：顶栏「⋯ 工具▾」下拉已拆掉 —— 它只剩「查重 / 出图统计」两项，
   两个都是要跑全库扫描的低频操作，为它们在顶栏常驻一个下拉触发器不划算。
   两项已收进设置面板「数据」页的「分析工具」区（绑定见 js/settings.js 的 setupSettingsPanel）。
   ⚠ 别在这里重新加回 #btnMore 的绑定 —— 那个 id 已从 index.html 移除。 */
/* 「导出…」：把所选文件复制到目标文件夹（图片剥离生成元数据，其余原样复制）。
   注：此按钮此前只有 disabled 状态、没有任何点击处理，属于历史遗留的死按钮，本次补上。 */
$("#bExp").onclick=()=>{
  const paths=[...batchSel];
  if(!paths.length){ toast("先勾选要导出的文件"); return; }
  const dlg=bigDialog("导出到文件夹");
  const hidden=batchHiddenCount();
  dlg.body.innerHTML=
    '<div class="row" style="color:var(--tx2);margin-bottom:10px">已选 '+paths.length
    +' 个文件'+(hidden?`（其中 ${hidden} 个不在当前筛选内）`:"")
    +'；图片会剥离生成元数据后复制，其余格式原样复制。目标目录不存在会自动创建。</div>'
    +'<div style="display:flex;gap:8px">'
    +'<input id="expPath" style="flex:1" placeholder="目标文件夹绝对路径，如 D:\\export" spellcheck="false">'
    +'<button id="expGo" class="primary">导出</button></div>';
  const inp=dlg.body.querySelector("#expPath");
  inp.value=S.lastExportDir||"";
  /* F7-P2-2（2026-10-20）：`btn.disabled=true` 只挡得住鼠标，挡不住 Enter ——
     `inp.onkeydown` 直接调 go()。实测连按 3 次回车发出 **3 个 /api/batch_export**，
     同一批文件被复制成 `_1/_2/_3` 三份。这里用单飞闸（onclick 与 Enter 共用）。
     另外整段原来没有任何进行中反馈：几百张时界面只有一个置灰按钮，分不清「在跑」还是「卡死」。 */
  let _expBusy=false;
  const go=async()=>{
    if(_expBusy) return;
    const dest=inp.value.trim();
    if(!dest){ toast("请输入目标文件夹路径","err"); return; }
    _expBusy=true;
    const btn=dlg.body.querySelector("#expGo"); btn.disabled=true;
    setBotState("busy", `正在导出 ${paths.length} 个文件…`, null, true);
    let j;
    try{
      j=await api("/api/batch_export",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths, dest})}).catch(e2=>({error:e2.message}));
    }finally{
      _expBusy=false; btn.disabled=false;
    }
    if(j.error){ setBotState("err","导出失败："+j.error,null,false); toast(j.error,"err"); return; }
    setBotState("done", `已导出 ${j.done||0} 个文件`, null, false);
    S.lastExportDir=j.dest||dest;
    dlg.close();
    /* B7-P2-6（2026-10-17）：列表里同一路径出现两次只会导出一份 —— 不说的话
       用户按「勾了 10 个」数目录里的文件，会以为少了。 */
    const _dupTxt=j.dup ? `（列表内重复 ${j.dup} 个，未重复导出）` : "";
    if(j.failed && j.failed.length)          // V12：一行只放摘要，逐条原因进「详情」
      toastErr(`已导出 ${j.done} 个，${j.failed.length} 个失败`,
        (j.dup?`另有 ${j.dup} 个重复项已跳过\n\n`:"")
        +j.failed.map(f=>`${f.path||f.name||"(未命名)"}\n  → ${f.error||"未知原因"}`).join("\n\n"));
    else if(j.unclean && j.unclean.length)   // B5：导出了，但有些文件的元数据没能抹干净
      toastErr(`已导出 ${j.done} 个，其中 ${j.unclean.length} 个未能抹除干净`,
        (j.dup?`另有 ${j.dup} 个重复项已跳过\n\n`:"")
        +j.unclean.map(u=>`${u.name}\n  → ${u.error||"未知原因"}`).join("\n\n"));
    else toast(`已导出 ${j.done} 个到 ${j.dest}${_dupTxt}`,"ok");
  };
  dlg.body.querySelector("#expGo").onclick=go;
  inp.onkeydown=e2=>{ if(e2.key==="Enter") go(); };
  inp.focus();
};
$("#bDel").onclick=()=>{
  const paths=[...batchSel]; if(!paths.length) return;
  const hasMore=paths.length>DELETE_MAX_PATHS;
  const hidden=batchHiddenCount();
  confirmDialog("批量删除",
    `确定删除选中的 ${paths.length} 个文件吗？文件将移入${delTipWhere()}。`
    + (hidden?`\n\n⚠️ 其中 ${hidden} 个已被当前筛选隐藏（屏幕上现在看不到），也会一起删除。`
               + `\n如果只想删眼前这些，请先取消勾选或清除筛选后重新选择。`:"")
    + (hasMore?`\n\n⚠️ 一次最多删除 ${DELETE_MAX_PATHS} 个，超出的部分不会被删除，请删完这批再继续。`:""),
    async()=>{
    /* F7-P1-2（2026-10-17）：防重入。执行期 `#bDel` 一直被 updateBatchBar 解回可点，
       第二次确认会把同一批路径再提交一遍；这里既拦回调、也锁住入口。 */
    if(BATCH_DEL_BUSY){ toast("正在删除，请等待这一批完成","err"); return; }
    BATCH_DEL_BUSY=true; updateBatchBar();
    try{
    /* ε-14（§6.8）：删除期间给进行中反馈 —— 此前弹窗一关界面就静默，大批量
       （几百张，含系统回收站 COM 调用）好几秒没有任何迹象，用户会再点一次。 */
    setBotState("busy", `正在删除 ${paths.length} 个文件…`, null, true);
    let ok=0, fb=0; const failed=new Set(); let dropped=0;
    try{
      const j=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths})});          // 单请求批量，避免 N 次往返
      ok=j.deleted||0;
      fb=j.fallback||0;                          // 系统回收站不可用、退回应用内回收站的个数
      dropped=j.dropped||0;                      // P1-1：服务端因超上限而未处理的条数
      for(const p of (j.failed||[])) failed.add(normPath(p));   // 归一化后再比对
      noteDeleteMeta(j);                       // §6.1：元数据没写回要说出来
    }catch(e){ for(const p of paths) failed.add(normPath(p)); }
    /* ⚠️ P1-1：删除成功的集合必须以**服务端回报的 deleted 数量**为界，不能按
       「全部路径 − failed」推断 —— 服务端若因超限丢弃了尾部路径，它们不在 failed 里，
       推断法会把还在磁盘上的文件从列表里抹掉（界面显示删了、文件其实还在）。 */
    let dead;
    if(failed.size || dropped){
      // 有失败或截断：只把「按服务端计数确定已删」的部分当作已删，其余保持原样
      const candidates=[...paths].filter(p=>!failed.has(normPath(p)));
      dead=new Set(candidates.slice(0, ok));
    }else{
      dead=new Set(paths);
    }
    /* F7-P1-1（2026-10-17）：删除后的清理必须走 purgePaths —— 它同时清 S.items /
       S.tagView / S.metaMap / batchSel，并 ++viewGen 作废在途加载。此前这里只手写
       清 S.items：标签筛选生效时 applyFilter 的源是 S.tagView，于是提示「已删除 N 个」，
       卡片却仍留在网格里（可点开一张已不在磁盘的图、可再勾选、再删必然全失败）。 */
    purgePaths([...dead]);
    if(window.mvUpdateTrashBadge) window.mvUpdateTrashBadge();   // V2：删完同步顶栏角标
    const donePaths=[...dead];
    const left = paths.length - ok;                    // 没删掉的（含失败 + 被截断）
    const msg = left ? `已删除 ${ok} 个，未删除 ${left} 个`
                     : `已删除 ${ok} 个（${delDoneMsg()}）`;
    if(dropped && !failed.size){
      setBotState("err","有文件未删除",null,false);
      toastErr(`已删除 ${ok} 个`, `超出单次上限 ${DELETE_MAX_PATHS} 个，剩余 ${left} 个未处理，请再删一次`);
    }
    // 进了系统回收站的文件没法在应用内还原 → 不给「撤销」按钮，免得点了必然失败
    else if(delIsSystem() && !fb){ setBotState(left?"err":"ok", left?"部分未删除":"删除完成", null, false); toast(msg, left?"err":"ok"); }
    else if(donePaths.length){ setBotState(left?"err":"ok", left?"部分未删除":"删除完成", null, false); toastAction(msg, "撤销", ()=>undoDelete(donePaths), left?"err":"ok"); }
    /* 一个都没删掉（全部失败）：没有可撤销的对象。挂一个点了必然报「回收站里找不到」
       的撤销按钮，等于把用户往第二个错误上引 —— 同 P0-17 那条纪律。 */
    else { setBotState("err","删除失败",null,false); toast(msg,"err"); }
    } finally {
      BATCH_DEL_BUSY=false;
      updateBatchBar();        // 按当前剩余选中重新算 disabled（失败项仍保留勾选，可直接重试）
    }
  });
};
document.addEventListener("keydown",e=>{               // Esc：菜单 → 对比 → 批量 → 大图
  /* F7-P2-1（2026-10-20）：确认框 / 输入框开着时，Esc 的语义只是「关掉这个框」——
     而本监听在 document 冒泡阶段，`confirmDialog` 的 onkeydown 没有 stopPropagation
     ⇒ 事件继续冒泡到这里，命中下面的批量分支，连勾选一起清空（实测
     `{dlgGone:true, batchOn:false, sel:0}`）。这里统一挡在入口。 */
  if(e.key==="Escape" && document.querySelector(".bigMask, #catInputMask")) return;
  if(e.key==="Escape" && document.getElementById("catMenu")){ closeCatMenu(); return; }
  if(e.key==="Escape" && $("#cmp").classList.contains("on")){ closeCmp(); return; }
  if(e.key==="Escape" && batchOn && !lb.classList.contains("on")
     && e.target.tagName!=="INPUT") setBatch(false);
});
/* 全局快捷键：/ 聚焦搜索框 · Esc（在搜索框里）清空搜索 */
document.addEventListener("keydown",e=>{
  const el=e.target, tg=(el.tagName||"").toUpperCase();
  const typing = tg==="INPUT"||tg==="TEXTAREA"||tg==="SELECT"||el.isContentEditable;
  if(e.key==="/" && !typing && !lb.classList.contains("on")
     && !document.querySelector(".bigMask")){
    e.preventDefault(); const q=$("#q"); if(q){ q.focus(); q.select(); }
    return;
  }
  if(e.key==="Escape" && el===$("#q") && $("#q").value){
    $("#q").value=""; viewGen++;   // ε-6 E6-4：统一令牌，作废在途的搜索/视图加载
    applyFilter(); toast("已清空搜索");
  }
});
/* 网格回到顶部按钮：滚动超过一屏才出现 */
(function(){
  const gw=document.getElementById("gridwrap"), btn=document.getElementById("toTop");
  if(!gw||!btn) return;
  const sync=()=>{ btn.hidden = gw.scrollTop < 600; };
  gw.addEventListener("scroll",sync,{passive:true});
  /* UI-03：平滑回顶会连续发几十个 scroll 事件 —— 全程压住文件名显形（hold 900ms），
     HUD 照常跟着走（看着数字归零，比什么都不显示更清楚）。 */
  btn.onclick=()=>{
    if(typeof mvScrollProg==="function") mvScrollProg(900);
    gw.scrollTo({top:0,behavior:"smooth"});
  };
  sync();
})();
const sent=document.createElement("div"); sent.id="sent";
sent.style.cssText="height:1px";
grid.insertAdjacentElement("afterend", sent);
new IntersectionObserver(es=>{ if(es[0].isIntersecting) renderMore(); },
  {rootMargin:"1600px"}).observe(sent);

/* 网格视频懒加载：进入视口附近才挂 src（数据存在 data-src 上），
   重新渲染时由 applyFilter/promptSearch 先 disconnect 清掉旧观察 */
const vObs=new IntersectionObserver(ens=>{
  for(const en of ens){ if(!en.isIntersecting) continue;
    const v=en.target;
    if(!v.src && v.dataset.src) v.src=v.dataset.src;
    vObs.unobserve(v);
  }
},{rootMargin:"800px"});

/* ================================================================
   扩展功能：收藏夹视图 / 查重 / 元数据筛选 / 回收站 / 统计 / 自动刷新 / 视频控制
   ================================================================ */

/* 从内存列表移除已删除文件并重渲染（查重/批量删除共用） */
let PURGE_T=null;
let PURGE_PEND=null;   // 已经加了 .removing、但淡出动画还没落地的那一批（E6-5）
function purgePaths(paths, onCommit){
  /* E6-5：`commit` 原先闭包捕获**当次**的 `dead`，而「上一次还没落地就再来一次」
     的分支写的是 `clearTimeout(...); commit();` —— 它取消的是**上一次**的定时器，
     执行的却是**这一次**的 commit ⇒ **上一次那一批永远不会被移除**：
     数据还在 S.items 里、卡片还挂着 `.removing`（半透明残留在网格里），
     点它还能打开一张磁盘上已经没有的图；计数与统计也跟着偏大。
     原注释写的「立即补 commit（不叠加）」本意是对的，实现漏了「把两批并起来」。
     现在 `commit(deadSet)` 收一个集合，补提交时把上一批并入 —— 两批都不丢。 */
  const dead=new Set(paths);
  const commit=(deadSet)=>{
    S.items=S.items.filter(it=>!deadSet.has(it.path));
    if(S.tagView) S.tagView=S.tagView.filter(it=>!deadSet.has(it.path));  // 标签筛选基准列表同步清理
    if(S.metaMap) for(const p of deadSet) delete S.metaMap[p];
    for(const p of deadSet) batchSel.delete(p);
    if(batchOn) updateBatchBar();
    /* A9（10-15 全代码审计）：purgePaths 只改内存 + 重算视图，却**不 ++viewGen** ——
       删除期间若有在途的异步加载回来（自动刷新的 silentReload、或提示词搜索），
       它会拿「删除前」的目录快照整片覆盖 S.items ⇒ 刚删掉的卡片复活、计数回跳，
       点开还是空的。本函数改变了**权威视图**，按 state.js「权威视图世代」的约定
       必须 ++viewGen 作废在途加载（与 load / loadFavView / setTagFilter 同款）。 */
    viewGen++;
    applyFilter();
    refreshStatsSoon();                            // 删除会改变目录数量口径
    /* 2026-10-16：唯一的「视图已重建」挂点 —— 大图删除接替必须在这里兑现，
       而不是调用 purgePaths 的当下（PURGE_T 分支会把 applyFilter 推到淡出动画之后，
       那时列表才真正变短，提前按路径回找会落空）。 */
    if(onCommit){ try{ onCommit(); }catch(e){} }
  };
  /* V14：被删的卡片先淡出并让位，之后再重建网格 —— 「我这一下点到了」的确认感。
     数据仍立即从内存移除（commit 里做），只是 DOM 延后一个动画时长；
     reduced-motion 下跳过动画；上一次还没落地就再来一次时立即补 commit（不叠加）。 */
  const cards=[...(typeof grid!=="undefined"&&grid?grid.querySelectorAll("[data-p]"):[])]
    .filter(c=>dead.has(c.dataset.p));
  const reduce=window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if(PURGE_T){
    /* 上一次的淡出还没落地 ⇒ 立刻补提交，且**必须把上一批并进来**（E6-5）。 */
    clearTimeout(PURGE_T); PURGE_T=null;
    const merged=new Set(PURGE_PEND||[]);
    for(const p of paths) merged.add(p);
    PURGE_PEND=null;
    commit(merged);
    return;
  }
  if(!cards.length || reduce){ commit(dead); return; }
  cards.forEach(c=>c.classList.add("removing"));
  PURGE_PEND=[...paths];
  PURGE_T=setTimeout(()=>{ PURGE_T=null; PURGE_PEND=null; commit(dead); }, 200);
}

/* -------- 撤销删除后回到大图（2026-10-16 · 自 v3.0.0 回植）--------
   删除后接替打开了邻居；用户接着按「撤销」，若大图停在邻居上，等于撤销只做了一半。
   ⚠️ `undoDelete` 是 batch / gallery / nav **三处共用**的全局函数，所以这里两条纪律：
     ① 来源标记**读后即清** —— 否则「批量删除 → 稍后撤销」会莫名其妙打开大图；
     ② 标记带时间戳、60s 过期 —— toast 上的撤销本来就有时效，过期残留不该再生效。 */
let _lastLbDel={path:"", at:0};
function mvNoteLbDelete(path){ _lastLbDel={path:path||"", at:Date.now()}; }
/* 还原会让 `undoDelete` 末尾的 load() 重新扫目录，文件要等异步加载回来才重新进 S.view，
   ⇒ 不能在还原成功的当下定位。这里轮询等它出现（≤6s）—— 比往 applyFilter 里塞钩子侵入面小。
   ⚠️ 让位判据必须是「大图被换到了**别的地方**」：撤销场景下大图本来**就**开着
      （停在删除后自动接替的那张），若拿「大图开着且当前不是目标」当「用户自己换了图」，
      条件恒真 ⇒ 永不兑现。这里拿调用瞬间那张做基线比对。 */
async function _reopenAfterUndo(path){
  const atPath=((S.view[S.idx]||{}).path)||"";
  for(let i=0;i<24;i++){
    const idx=(S.view||[]).findIndex(x=>x && x.path===path);
    if(idx>=0){ openLb(idx); return; }
    if(lb.classList.contains("on")){
      const cur=((S.view[S.idx]||{}).path)||"";
      if(cur && cur!==atPath) return;     // 用户自己翻到别处了 ⇒ 让位
    }
    await new Promise(r=>setTimeout(r,250));
  }
}

/* 撤销删除：/api/delete 只返回删除数量，所以按原始路径去回收站里找回 key 再还原 */
async function undoDelete(paths){
  const _note=_lastLbDel; _lastLbDel={path:"", at:0};      // 读后即清（见上方①）
  const backTo=(_note.path && paths.includes(_note.path) && Date.now()-_note.at<60000)
    ? _note.path : "";
  try{
    const t=await api("/api/trash");
    const keys=[];
    for(const p of paths){
      const want=normPath(p);
      const hit=(t.items||[]).filter(x=>normPath(x.orig)===want).sort((a,b)=>b.t-a.t)[0];
      if(hit) keys.push(hit.key);
    }
    if(!keys.length){ toast("撤销失败：回收站里找不到这些文件","err"); return; }
    let ok=0; const partial=[]; const bad=[];
    for(const k of keys){
      try{
        const r=await api("/api/trash_restore",{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify({key:k})});
        ok++;
        if(r.partial) partial.push((r.path||"").split(/[\\/]/).pop()||"");   // P1-2 同上
      /* P0-17：原先这里是 `catch(_){}` —— 还原**全部失败**时会一路走到下面那句
         「已撤销删除，还原 0 个文件」（成功样式），用户以为撤销生效了，实际一个都没回来。
         失败必须计数、必须如实说，且不能混进「成功」文案。 */
      }catch(e){ bad.push((e&&e.message)||"未知错误"); }
    }
    if(bad.length && !ok)
      toastErr("撤销失败", `${bad.length} 个文件都没能还原：${bad[0]}`);
    else if(bad.length)
      toastErr(`已撤销 ${ok} 个，${bad.length} 个失败`,
               `${bad.length} 个未还原：${bad[0]}`);
    else if(partial.length)
      toastErr(`已撤销删除，还原 ${ok} 个文件`,
               `${partial.length} 个的收藏 / 标签 / 标记未能写回，需要手工恢复`);
    else
      toast(`已撤销删除，还原 ${ok} 个文件`,"ok");
    refreshStatsSoon();
    if(window.mvUpdateTrashBadge) window.mvUpdateTrashBadge();   // V2：还原后同步顶栏角标
    load(S.path||S.root,{silent:true});
    /* 2026-10-16：这次撤销本来就是从大图里点的删除 ⇒ 等文件回到列表后把它重新打开 */
    if(backTo) _reopenAfterUndo(backTo).catch(()=>{});
  }catch(e){ toast("撤销失败："+e.message,"err"); }
}

/* 通用大弹窗：Esc/点遮罩关闭（Esc 用捕获阶段，优先于 lightbox 等快捷键）

   opts.onClose（X3）：**任何**关闭路径（Esc / 标题栏 ✕ / 点遮罩 / 被新弹窗顶掉）
   都会走一次且只走一次。业务侧若有「操作进行中不许再来一次」这类 in-flight 守卫，
   复位必须挂在这里 —— 早先只有调用方自己接的那些按钮会复位，旁路一关守卫就永久锁死
   （settings.js 切数据目录就是这样：Esc 关掉三选一之后，再点分段按钮一直提示
   「上一次数据目录操作还在进行中」，只能刷新页面）。 */
function bigDialog(title, opts={}){
  const old=document.querySelector(".bigMask");
  if(old){ if(old._close) old._close(); else old.remove(); }   // 走 close 才能摘掉上一个弹窗的 keydown
  const mask=document.createElement("div"); mask.className="bigMask";
  const box=document.createElement("div"); box.className="bigBox";
  if(opts.wide) box.style.width="min(1060px,96vw)";
  box.innerHTML='<div class="bhd"><b></b><button><span class="ic">'+ico("x")+'</span></button></div><div class="bbd"></div>';
  box.querySelector("b").textContent=title;
  mask.appendChild(box); document.body.appendChild(mask);
  // close 必须移除监听器：此前每次开弹窗都在 document 上留一个永不摘除的 keydown，
  // 关掉的弹窗仍会被后续 Esc 触发（且 mask.isConnected 为 false 后变成纯空转的常驻句柄）
  const onKey=e=>{
    if(e.key!=="Escape" || !mask.isConnected) return;
    e.stopPropagation(); close();
  };
  /* 幂等：close 既是「✕ 的 onclick」又是「Esc / 遮罩 / 被新弹窗顶掉」的落点，
     同一拍里可能被叫两次（例如 onClose 里又 close 了自己）。`mask._closed` 保证
     onClose 只跑一次 —— 业务回调不一定幂等，重复调用会重复弹提示 / 重复复位状态。 */
  const close=()=>{
    if(mask._closed) return;
    mask._closed=true;
    document.removeEventListener("keydown", onKey, true);
    mask.remove();
    /* 关闭回调是「旁路收尾」的唯一出口。回调抛错不许把关闭流程打断（弹窗已经摘了，
       界面停在半关状态比丢一行日志糟得多）—— 只记账。 */
    if(typeof opts.onClose==="function"){
      try{ opts.onClose(); }
      catch(e){ console.warn("[bigDialog] onClose", e); }
    }
  };
  box.querySelector(".bhd button").onclick=close;
  if(!opts.noDismiss) mask.addEventListener("click",e=>{ if(e.target===mask) close(); });
  document.addEventListener("keydown", onKey, true);
  mask._close=close;
  return {mask, box, body:box.querySelector(".bbd"), close};
}

