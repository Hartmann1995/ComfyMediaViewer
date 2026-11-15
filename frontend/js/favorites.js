/* ComfyMediaViewer · js/favorites.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ---------------- 收藏分类 ---------------- */
async function refreshFavCats(){
  /* F3 P2-2：原来 `.catch(()=>({cats:[]}))` 把失败**伪装成空结果** ——
     S.favCats 被写成 []，两个下拉只剩「全部分类 / 新建分类…」，没有任何提示，
     用户会以为自己的分类丢了，或者顺手新建一个同名的空分类（而数据还在服务端）。
     改为：失败时**保留上一次的分类**（不动内存态）并明确报错。 */
  try{
    const j = await api("/api/favs");
    S.favCats = j.cats || [];
  }catch(e){
    toastErr("收藏分类读取失败",
      "列表可能不是最新的（已保留上一次读取到的分类）：" + String((e&&e.message)||e));
  }
  /* H2（2026-10-07）：原先还要 `fillCatSelect($("#favcat"), true)` 维护一个隐藏下拉的选项，
     好让 `.value` 能承载「当前收藏分类」。该幽灵控件已删除，分类状态改由
     `S.filters.favcat` 承载 ⇒ 这行不再需要（左栏 `#favNav` 由 buildFavNav 自建）。
     下面 `#lbCat` 是**可见**的「收藏到分类」下拉，与筛选无关，保留。 */
  fillCatSelect($("#lbCat"), false);
}
/* 合并真实分类与本地新建的未使用分类（后者 count=0） */
function catList(){
  const map={};
  for(const c of S.favCats) map[c.name]=c.count;
  for(const c of S.extraCats) if(!(c in map)) map[c]=0;
  return Object.entries(map).map(([name,count])=>({name,count}))
    .sort((a,b)=>b.count-a.count);
}
function fillCatSelect(sel, withAll){
  const cur = sel.value;
  const cats = catList();
  sel.innerHTML = "";
  if(withAll){
    sel.appendChild(new Option("全部分类", "*"));
    for(const c of cats) sel.appendChild(new Option(c.count?`${c.name} (${c.count})`:c.name, c.name));
  }else{
    if(!cats.length) sel.appendChild(new Option("默认", "默认"));   // 空分类兜底：否则 "__new__" 是唯一选项、会被自动选中，再点它不触发 change（新建分类失灵）
    for(const c of cats) sel.appendChild(new Option(c.name, c.name));
    sel.appendChild(new Option("新建分类…", "__new__"));
  }
  // 恢复原选择（选项仍在时）
  if([...sel.options].some(o=>o.value===cur)) sel.value = cur;
  mvFitCatSelect(sel);
}
/* ---------------- 顶栏「收藏分类」下拉的宽度 ----------------
   2026-10-31（用户实测）：`#lbCat` 框宽 91px，而选中「默认」时文字只占 24px ——
   整整 67px 是空白。原因不是 padding，而是原生 <select> 的**固有宽度取最长 option**
   （这里永远是「新建分类…」）撑出来的；显示哪一项它都那么宽。
   修法：按当前选中项的文字实测宽度现算，`文字 + 24px（内边距 + 下拉箭头）`，
   并按字号档位（--fsx）上下夹紧：下限 44px / 上限 140px。
   上限是必要的：分类名最长 20 字，不夹会把顶栏撑爆（那正是本批要修的病）。
   ⚠️ 只在 fillCatSelect 末尾与 change 之后调用；拖动字号滑块时由
      settings.js 的 applyFs 统一重算（--fsx 变了，量出来的宽就过期了）。 */
function mvFitCatSelect(sel){
  sel = sel || document.getElementById("lbCat");
  if(!sel) return;
  const fsx=parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--fsx"))||1;
  const txt=(sel.selectedOptions && sel.selectedOptions[0])
    ? sel.selectedOptions[0].textContent : "";
  let tw=0;
  if(txt){
    /* 用离屏 span + 同一个 font 量：canvas measureText 要拼字体串，而 select 的
       font 由一堆令牌组合而来（字号走 --fsx），拼错一个字符量出来就是错的。 */
    const sp=document.createElement("span");
    sp.style.cssText="position:absolute;left:-9999px;top:0;visibility:hidden;white-space:pre";
    sp.style.font=getComputedStyle(sel).font;
    sp.textContent=txt;
    document.body.appendChild(sp);
    tw=sp.getBoundingClientRect().width;
    sp.remove();
  }
  const CHROME=16 + 18*fsx;   // 下拉箭头 ≈16px（固定，不随字号缩放）+ 左右内边距 ≈18px×fsx
  const w=Math.min(140*fsx, Math.max(56*fsx, tw+CHROME));
  sel.style.width=Math.round(w)+"px";
}
window.mvFitCatSelect=mvFitCatSelect;
/* 新建分类弹窗（替代浏览器 prompt，部分浏览器会拦截 prompt）
   2026-11-08：加 opts（可选）—— 分类重命名复用它。
     {title:"…", value:"初始值", maxlength:20}
   初始值走 inp.value 赋值而不是拼进 innerHTML（分类名是用户输入，不能进 HTML）。 */
function catInputDialog(cb, opts){
  const o=opts||{};
  /* F3 P2-4：两个弹窗共用 #catInputMask，后开的会把先开的 `old.remove()` 掉。
     而 close() 只 remove、**不调 cb** ⇒ 调用方 await 的那条 Promise 永不 resolve，
     调用方那串 await 整条静默挂起（无提示、无报错）。
     修法：每个实例挂 `_dismiss`（语义 = 按「取消」了结），顶替方负责先调用它。
     沿用同一个 id 是有意的 —— inert 基建与 mvTopOverlay 都按 #catInputMask 认这一族浮层。 */
  const old=document.getElementById("catInputMask");
  if(old){ if(typeof old._dismiss==="function") old._dismiss(); else old.remove(); }
  const mask=document.createElement("div"); mask.id="catInputMask";
  mask.innerHTML='<div id="catInputBox"><div class="cit">新建收藏分类</div>'+
    '<input placeholder="输入分类名称">'+
    '<div class="cbtns"><button class="cancel">取消</button><button class="ok">确定</button></div></div>';
  mask.querySelector(".cit").textContent=o.title||"新建收藏分类";
  const inp=mask.querySelector("input");
  inp.maxLength=o.maxlength||20;
  document.body.appendChild(mask);
  const close=()=>mask.remove();
  /* 取消 / 点遮罩 / Esc / 空输入点确定 —— 一律以 null 了结 Promise。
     原先「取消」也不 resolve（只是调用方恰好在 await 它，才没暴露成 bug）。 */
  const settle=v=>{ close(); cb(v); };
  const ok=()=>{ const v=inp.value.trim(); settle(v?v:null); };
  mask._dismiss=()=>settle(null);            // 被别的弹窗顶掉 ⇒ 按取消了结
  mask.addEventListener("click",e=>{ if(e.target===mask) settle(null); });
  mask.querySelector(".cancel").onclick=()=>settle(null);
  mask.querySelector(".ok").onclick=ok;
  inp.onkeydown=e=>{
    if(e.isComposing || e.keyCode===229) return;   // 中文输入法确认候选词的回车不当「确定」
    if(e.key==="Enter") ok(); if(e.key==="Escape") settle(null); };
  if(o.value) inp.value=o.value;
  setTimeout(()=>{ inp.focus(); if(o.value) inp.select(); },0);
}

/* 通用确认弹窗（复用分类弹窗样式）
   opts（可选）：{okText:"确定文案", okClass:"额外的按钮类"}
   2026-10-27（代码梳理 B1）：原先确认按钮**写死**红色「删除」，却被用在
   「重建提示词索引 / 清除缩略图缓存 / 清除单图暂存 / 全部还原」这些不动数据的
   操作上（前两处文案还明写着「不动原图」）—— 按钮说的和做的不是一回事，
   红色 danger 语义也被稀释。现在默认仍是红色「删除」（8 处删除类调用一字不改），
   非破坏性调用传 {okText:"…", okClass:""} 换主色按钮。 */
function confirmDialog(title, msg, cb, opts){
  const o=opts||{};
  const okText=o.okText||"删除";
  const okCls=(o.okClass===undefined)?"danger-solid":o.okClass;
  const old=document.getElementById("catInputMask");
  if(old){ if(typeof old._dismiss==="function") old._dismiss(); else old.remove(); }
  const mask=document.createElement("div"); mask.id="catInputMask";
  mask.innerHTML='<div id="catInputBox"><div class="cit"></div>'+
    '<div style="font-size:calc(13px*var(--fsx));color:var(--tx2)"></div>'+
    '<div class="cbtns"><button class="cancel">取消</button><button class="ok"></button></div></div>';
  mask.querySelector(".cit").textContent=title;
  mask.querySelector("#catInputBox > div:nth-child(2)").textContent=msg;
  /* 用 textContent 而不是拼进 innerHTML：文案是开发者写的字面量，但别为将来
     接入变量时留一个 HTML 注入的口子。 */
  const okBtn=mask.querySelector(".ok");
  okBtn.className="ok"+(okCls?" "+okCls:"");
  okBtn.textContent=okText;
  document.body.appendChild(mask);
  const close=()=>mask.remove();
  const ok=()=>{ close(); cb(); };
  // 被顶掉 ⇒ 等价于「取消」：只关掉自己，**不**执行 cb（否则等于用户没点也删了）
  mask._dismiss=()=>close();
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });
  mask.querySelector(".cancel").onclick=close;
  mask.querySelector(".ok").onclick=ok;
  mask.tabIndex=0; mask.focus();
  const openedAt=Date.now();
  mask.onkeydown=e=>{ if(e.key==="Escape") close();
    // Enter 有 300ms 保护窗：按 Delete 弹窗后连按 Enter 不会立即误删
    /* X5（2026-10-28）：焦点落在按钮上时，Enter 必须交给浏览器**原生激活**那个按钮。
       默认焦点是「取消」（app.js dlgPick 在同权按钮里取第一个非 .bhd 的）—— 此前
       这里不看 target 就叫 ok()，于是「Tab 到取消 / 默认就停在取消 + 回车」= 删图。
       用户在取消键上按回车，得到的却是删除，这是本族弹窗最危险的一条路径。 */
    if(e.key==="Enter" && Date.now()-openedAt>300){
      const t=e.target;
      if(t && t.closest && t.closest("#catInputBox .cbtns button")) return;
      ok();
    } };
}

/* ---------------- 分类管理（右键重命名 / 删除）· 2026-11-08 ----------------
   背景：分类此前只有「新建」入口 —— 左栏的分类既改不了名也删不掉，界面里唯一
   能做的是一张一张把图挪到别的分类，而服务端的 `del_preset_cat` 只允许删空
   分类且前端从未调用过它。存量分类（尤其 0 条的预建名）于是永久堆在左栏。

   数据模型提醒：分类不是独立表，是 favorites.json 每条 `cat` 的聚合视图。
   所以「改名」= 批量改这些条目的 cat（撞名即合并）；「删除」必须先决定这些
   条目去哪（移到默认 / 一并取消收藏）。两件事都由服务端在一次请求里完成，
   前端只在成功回执后刷新左栏与统计。 */

function favCatCount(name){
  const c = catList().find(x=>x.name===name);
  return c ? (c.count||0) : 0;
}

/* 左栏分类项的右键菜单。x/y 为视口坐标。 */
function favCatMenu(x, y, name){
  if(name==="默认"){
    /* 「默认」是未分类条目的兜底容器：删了会让它们无处可去，改名等于把
       所有未分类的图塞进某个具名分类 —— 都不是用户在这里能表达的意图。 */
    popupMenu(x, y, [{label:"「默认」是兜底分类，不能改名或删除", icon:"info", fn:()=>{}}]);
    return;
  }
  popupMenu(x, y, [
    {label:"重命名…", icon:"write", fn:()=>renameFavCatDialog(name)},
    {label:"删除…", icon:"trash", cls:"danger", fn:()=>deleteFavCatDialog(name)},
  ]);
}

/* 分类改名。撞名不在这里拦死 —— 服务端按「合并到已有分类」处理；
   但**合并前必须二次确认**：合并后无法自动拆回（没有「撤销合并」的数据依据）。 */
function renameFavCatDialog(oldName){
  catInputDialog(async v=>{
    const nm=(v||"").trim();
    if(!nm || nm===oldName) return;
    const doRename=async()=>{
      let r;
      try{
        r = await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({op:"rename_cat", cat:oldName, to:nm})});
      }catch(e){ r={error:String((e&&e.message)||e)}; }
      if(!r || r.error || r.ok===false){
        toastErr("重命名失败", (r&&(r.reason||r.error)) || "服务端未返回原因");
        return;
      }
      if(Array.isArray(S.extraCats)) S.extraCats=S.extraCats.filter(x=>x!==oldName);
      /* 正在看这个分类 ⇒ 把筛选状态跟到新名下，否则当前视图会空掉 */
      if((S.filters.favcat||"*")===oldName){
        if(favMode) setFavCatFilter(nm, true);       // setter 内部会重新拉 fav_view
        else S.filters.favcat=nm;
      }
      await refreshFavCats();
      if(typeof buildFavNav==="function") buildFavNav();
      const n=r.moved|0;
      if(r.merged) toast("已合并到「"+nm+"」"+(n?"（"+n+" 张）":""),"ok");
      else toast("已重命名为「"+nm+"」"+(n?"（"+n+" 张）":""),"ok");
    };
    if(catList().some(c=>c.name===nm)){
      confirmDialog("合并收藏分类",
        "「"+nm+"」已存在。继续会把「"+oldName+"」下的 "+favCatCount(oldName)
        +" 张收藏并入「"+nm+"」，且无法自动拆回。", doRename,
        {okText:"合并", okClass:""});
      return;
    }
    await doRename();
  }, {title:"重命名收藏分类", value:oldName, maxlength:20});
}

/* 删除分类的确认弹窗：非空分类必须选「这些收藏去哪」。
   cb(mode) 的 mode 走服务端同一套枚举：move（移到默认）/ unfav（一并取消收藏）。
   空分类只给「取消 / 删除」，不多问一步。 */
function catDeleteDialog(name, count, cb){
  const old=document.getElementById("catInputMask");
  if(old){ if(typeof old._dismiss==="function") old._dismiss(); else old.remove(); }
  const mask=document.createElement("div"); mask.id="catInputMask";
  mask.innerHTML='<div id="catInputBox"><div class="cit">删除收藏分类</div>'+
    '<div class="cmsg"></div><div class="cbtns"></div></div>';
  const msg=mask.querySelector(".cmsg");
  msg.textContent = count
    ? "「"+name+"」下有 "+count+" 张收藏，请先选择这些收藏的去向："
    : "「"+name+"」是空分类，删除后不可恢复。";
  msg.style.cssText="font-size:calc(13px*var(--fsx));color:var(--tx2);line-height:1.6";
  const btns=mask.querySelector(".cbtns");
  btns.style.flexWrap="wrap";        // 窄窗 / 大字号下三个按钮换行，不许出界
  const close=()=>mask.remove();
  const mk=(label,cls,fn)=>{
    const b=document.createElement("button"); b.type="button";
    if(cls) b.className=cls; b.textContent=label;
    b.onclick=()=>{ close(); fn(); };
    btns.appendChild(b); return b;
  };
  mk("取消","cancel",()=>{});
  if(count) mk("取消收藏","",()=>cb("unfav"));
  mk(count?"移到「默认」":"删除", count?"ok":"ok danger-solid",
     ()=>cb(count?"move":"reject"));
  document.body.appendChild(mask);
  /* 被别的浮层顶掉 = 取消：只关自己，**不**执行 cb（否则等于用户没点也删了） */
  mask._dismiss=()=>close();
  mask.addEventListener("click",e=>{ if(e.target===mask) close(); });
  /* 焦点落在「取消」上：这是危险操作，回车必须是最安全的那个分支
     （与 confirmDialog 的 X5 修复同款 —— 默认焦点绝不落在删除按钮上）。 */
  const cancelBtn=btns.querySelector(".cancel");
  cancelBtn.focus();
  mask.onkeydown=e=>{ if(e.key==="Escape") close(); };
}

/* 删除分类（服务端确认后刷新左栏 / 统计）。
   本地 count 来自启动时的分类快照，可能已经过期（别的窗口 / 桥接脚本改过）。
   服务端说「还有 N 张」时不要只报错把人卡住 —— 按真实条数重开一次三去向弹窗。 */
function deleteFavCatDialog(name){
  const ask=(count)=>{
    catDeleteDialog(name, count, async mode=>{
      let r;
      try{
        r = await api("/api/fav",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({op:"del_cat", cat:name, mode})});
      }catch(e){ r={error:String((e&&e.message)||e)}; }
      if(!r || r.error || r.ok===false){
        if(r && r.ok===false && mode==="reject" && (r.count|0)>0){
          toast("分类下其实还有 "+r.count+" 张收藏，请重新选择去向","warn");
          ask(r.count|0);
          return;
        }
        toastErr("删除分类失败", (r&&(r.reason||r.error)) || "服务端未返回原因");
        return;
      }
      if(Array.isArray(S.extraCats)) S.extraCats=S.extraCats.filter(x=>x!==name);
      /* 正在看这个分类 ⇒ 退回「全部收藏」，不要停在一条已被删掉的分类上 */
      if(favMode && (S.filters.favcat||"*")===name) setFavCatFilter("*", true);
      await refreshFavCats();
      if(typeof buildFavNav==="function") buildFavNav();
      if(typeof loadStats==="function") loadStats();   // 收藏总数与分类计数同步
      const extra = r.removed ? "，"+r.removed+" 张已取消收藏"
                  : r.moved   ? "，"+r.moved+" 张已移到「默认」" : "";
      toast("已删除分类「"+name+"」"+extra,"ok");
    });
  };
  ask(favCatCount(name));
}

