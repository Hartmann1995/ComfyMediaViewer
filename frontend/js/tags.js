/* ComfyMediaViewer · js/tags.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* ================= 标签系统(P1:大图 chip 编辑 + 卡片 hover) ================= */
let SUGGEST_TAGS = [];              // 全部标签缓存(联想/建议用)
/* 第 16 级（2026-10-17）：标签计数的**口径状态**，与 SUGGEST_TAGS 同一次响应一起更新。
   TAG_STATS_DANGLING = 当前根下有标签、但在图库里看不到的条目数（文件已删/换成了非媒体文件）；
   TAG_STATS_UNKNOWN  = 根目录不可读（外接盘掉线等），无法判定谁还在 ⇒ 计数不可信，显示「—」。
   这两个值只影响**显示**，不参与筛选 —— 筛选永远按列表里的真实文件走。 */
let TAG_STATS_DANGLING = 0, TAG_STATS_UNKNOWN = false;
function refreshTagStats(){
  /* 2026-11-05 P2：与 `refreshTagStatsNow` 共用世代号 —— 这条路径（大图 chip 摘除后
     刷新）没有令牌，慢的旧响应会覆盖 SUGGEST_TAGS / TAG_STATS_*，左栏与联想里
     重新出现刚摘掉的标签。语义上「最后发起的那次赢」。 */
  const tok = ++_tagStatsTok;
  api("/api/tags_list").then(j=>{
    if(tok !== _tagStatsTok) return;
    SUGGEST_TAGS = (j.tags||[]);
    TAG_STATS_DANGLING = j.dangling||0;
    TAG_STATS_UNKNOWN = !!j.unknown;
    tagStatsStore(j.tags);            /* O12：同一份响应顺手填左栏缓存，别再让 renderTagNav 重拉一次 */
  }).catch(()=>{});
}
/* 当前大图 item 的标签 chip 面板 */
function renderTagPanel(){
  const panel=$("#tagPanel"); if(!panel) return;
  if(S.solo){ panel.innerHTML=""; return; }   // 单图模式：临时副本没有标签概念
  const it = (S.idx>=0) ? S.view[S.idx] : null;
  if(!it || lb.classList.contains("on")===false){ panel.innerHTML=""; return; }
  const tags = it.tags || [];
  const seg = t => { const i=t.indexOf("/"); return i>=0 ? '<span class="seg">'+esc(t.slice(0,i+1))+'</span>' : ""; };
  let h='<div class="tph">标签<span class="cnt">'+tags.length+'</span></div><div class="tagwrap">';
  for(const t of tags)
    h+='<span class="tagchip '+tagClass(t)+'" data-t="'+esc(t)+'">'+seg(t)+'<span class="leaf">'+esc(t.split("/").pop())+'</span>'
      +'<span class="x" data-rm="'+esc(t)+'" data-tip="摘除">'+ico("x")+'</span></span>';
  h+='<div id="tagInputWrap"><input id="tagInput" placeholder="+ 加标签…" maxlength="40" autocomplete="off">'
    +'<div id="tagSuggest"></div></div></div>';
  panel.innerHTML=h;
  // 摘除
  panel.querySelectorAll(".tagchip .x").forEach(x=>x.onclick=async()=>{
    const t=x.dataset.rm;
    const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({op:"remove",path:it.path,tag:t})}).catch(e=>({error:e.message}));
    if(j.error){ toast(j.error,"err"); return; }
    it.tags=j.tags||[];
    renderTagPanel(); refreshTagStats();
  });
  // chip 点击可改名(管理弹窗放 P2,这里点击=无操作占位)
  // 输入联想
  const inp=panel.querySelector("#tagInput");
  const sug=panel.querySelector("#tagSuggest");
  let _sel=0, _items=[];
  function closeSug(){ sug.style.display="none"; _items=[]; _sel=0; }
  function showSug(list){
    /* P1-17：列表换了就必须把高亮下标收回 0。
       此前 showSug 只改 _items，_sel 却留着上一轮的值 —— 上一轮有 5 条、这一轮只有 2 条时，
       _sel=3 越界，回车取到 `_items[3]`（undefined）后回退成输入框文本，于是「明明高亮的是
       第 1 条、回车上屏的却是别的词」。越界一律夹回合法区间。 */
    _items=list;
    if(_sel >= list.length) _sel = 0;
    if(_sel < 0) _sel = 0;
    if(!list.length){ closeSug(); return; }
    sug.innerHTML=list.map((t,i)=>
      '<div class="tsi'+(i===_sel?" sel":"")+'" data-i="'+i+'"><span>'+esc(t.name)+'</span>'
      +'<span class="c">'+t.count+'</span></div>').join("");
    sug.style.display="block";
  }
  inp.oninput=()=>{
    const q=inp.value.trim().toLowerCase();
    if(!q){ closeSug(); return; }
    const has = (it.tags||[]).map(x=>x.toLowerCase());
    const list = SUGGEST_TAGS.filter(t=>!has.includes(t.name.toLowerCase())
      && t.name.toLowerCase().includes(q)).slice(0,8);
    const exact=list.some(t=>t.name.toLowerCase()===q);
    if(!exact && q.length>=1)
      list.push({name:inp.value.trim(), count:0, _new:true});   /* ε-11 §6.4：保留用户输入的大小写（q 只用于匹配比较，后端 _norm_tag 不做小写归一） */
    showSug(list);
  };
  inp.onkeydown=async e=>{
    if(e.isComposing || e.keyCode===229) return;   // 中文输入法选词的回车不是提交
    if(e.key==="ArrowDown"){ e.preventDefault(); moveSel(1); }
    else if(e.key==="ArrowUp"){ e.preventDefault(); moveSel(-1); }
    else if(e.key==="Escape"){ closeSug(); inp.value=""; }
    else if(e.key==="Enter"){
      e.preventDefault(); closeSug();
      const q=inp.value.trim();
      /* P1-17 兜底：只认下标合法的项。越界/为空一律回退到输入框原文，
         绝不拿 `_items[_sel]` 里一个 undefined 去猜名字。 */
      const cur = (_sel>=0 && _sel<_items.length) ? _items[_sel] : null;
      const tag = (cur && cur.name) ? cur.name : q;
      if(!tag) return;
      await tagAdd(it, tag);
      inp.value=""; _sel=0;
      /* 2026-11-15：提交后**收焦点**，别再 `focus()` 回输入框。
         原写法（清空后继续聚焦，方便连打下一个标签）把焦点留在 INPUT 里，而 nav.js
         的全局快捷键对 INPUT 一律让路（键盘事件得先给输入框用）⇒ 用户按 T 打完标签、
         回车确认后，按 ←/→ 翻页、按 Esc 关大图**全部失效**，只能鼠标点一下别处或
         按 Tab 移开。这是官方快捷键路径，不是边角操作。
         代价只是「想连删第二个标签要再按一次 T」—— 比「打完标签后键盘整体失灵」划算。 */
      inp.blur();
    } else if(e.key==="Tab"){ closeSug(); }
  };
  function moveSel(d){
    _sel=Math.max(0, Math.min(_items.length-1, _sel+d));
    sug.querySelectorAll(".tsi").forEach((el,i)=>el.classList.toggle("sel",i===_sel));
  }
  sug.onmousedown=e=>e.preventDefault();      // 防失焦
  sug.onclick=async e=>{
    const el=e.target.closest(".tsi"); if(!el) return;
    const item=_items[+el.dataset.i];
    const tag=item?item.name:"";
    closeSug(); inp.value="";
    if(tag) await tagAdd(it, tag);
    /* 2026-11-15：与上面的 Enter 分支同源 —— `sug.onmousedown` 有 preventDefault
       （防建议列表抢走焦点），所以点选完焦点仍在输入框里，键盘快捷键同样会被让路。 */
    inp.blur();
  };
}
/* F5-P2-3（2026-10-20）：标签名的规则在后端 `_norm_tag`（server.py）里，前端此前**零校验** ——
   用户输错只得到一句「无效的标签名」，分不清是字符非法、太长、还是单层太长。
   这里把同一套规则复刻出来，返回**具体原因**（空串 = 合法）。
   ⚠️ 改后端规则时必须同步改这里 —— 两处都是判据，不是文案。 */
const TAG_SPECIAL_JS = "`~!@#$%^&*()+=[]{}|\\:;\"'<>?,";
function _tagInvalid(name){
  const t = String(name == null ? "" : name).split("/")
    .map(x => x.trim()).filter(Boolean).join("/");
  if(!t) return "标签不能为空";
  if(t.length > 40) return "总长度不能超过 40 个字符（当前 " + t.length + " 个）";
  for(const ch of t){
    if(ch !== "/" && TAG_SPECIAL_JS.includes(ch)) return "不能包含字符「" + ch + "」";
  }
  for(const seg of t.split("/")){
    if(seg.length > 16) return "每一层不能超过 16 个字符（「" + seg + "」有 " + seg.length + " 个）";
  }
  return "";
}

/* F5-P2-1（2026-10-20）：单图打标签没有互斥 —— 连按两次回车，第二个请求在第一次响应回来前
   就发出去了（去重判据读的是**尚未回写**的 `it.tags`）⇒ 重复请求 + 重复 toast。
   这里用 in-flight 集合挡；成功后 `it.tags` 已回写，后续再点会落到「已存在」。
   F5-P2-4（2026-10-20）：去重口径与后端对齐成**精确比对**（`_norm_tag` 不做小写归一，
   `tags_add` 用 `tag not in cur`）—— 原来前端按小写比对，`Cat` 存在时 `cat` 会被前端
   拦下、而后端其实允许共存，三处（本处 / 左栏高亮 / 搜索）各一套口径。 */
const _tagBusy = new Map();          // path -> Set(tag)（在途请求）
async function tagAdd(it, tag){
  if(!tag || !it || !it.path) return;
  tag = String(tag).trim();
  if(!tag) return;
  const _bad = _tagInvalid(tag);
  if(_bad){ toast("标签名不可用：" + _bad, "err"); return; }
  const busy = _tagBusy.get(it.path) || new Set();
  if(busy.has(tag)){ toast("正在贴这个标签，请稍候…", "warn"); return; }
  if((it.tags||[]).includes(tag)){
    toast("已存在该标签"); return;
  }
  busy.add(tag); _tagBusy.set(it.path, busy);
  try{
  const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({op:"add",path:it.path,tag:tag})}).catch(e=>({error:e.message}));
  if(j.error){ toast(j.error,"err"); return; }
  it.tags=j.tags||[];
  toast("已贴「"+tag+"」","ok");
  renderTagPanel(); refreshTagStats(); refreshStatsSoon();
  syncCardTags(it);
  }finally{
    busy.delete(tag);
    if(!busy.size) _tagBusy.delete(it.path);
  }
}
/* 同步网格中该卡的 hover 标签行 */
function syncCardTags(it){
  const tags=it.tags||[];
  const _ce = (typeof S !== "undefined" && S.cardEl) ? S.cardEl.get(it) : null;
  let cardEl = (_ce && _ce.isConnected) ? _ce : null;
  // 列表视图的标签是纯文本列，走这里同步（网格视图才有 hover 行）

  if(!cardEl){                                  // 网格重渲染后旧引用失效：按路径回查
    const g=document.getElementById("grid");
    if(g) cardEl = [...g.querySelectorAll("[data-p]")].find(c=>c.dataset.p===it.path) || null;
  }
  if(!cardEl) return;
  syncCardTagsInto(cardEl, it);
}
/* §6.3：命中卡片后的写入段（列表列 / hover 行）。单卡与批量路径共用，
   单卡回查是 O(n)、一次点击无感；批量（prepCardTags）必须走映射表。 */
function syncCardTagsInto(cardEl, it){
  const tags=it.tags||[];
  if(LIST_VIEW){
    const cell=cardEl.querySelector(".lv-tags");
    if(cell) fillLvTags(cell, it);           // V4 批次 1：这一列含标记图标，统一走组装器
    return;
  }
  let row=cardEl.querySelector(".crdtags .tgs") || cardEl.querySelector(".crdtags");
  if(!row) return;
  const show=tags.slice(0,2);
  row.innerHTML = show.map(t=>'<span class="ct">'+tagDot(t)+esc(t.split("/").pop())+'</span>').join("")
    + (tags.length>2?'<span class="ct more">+'+(tags.length-2)+'</span>':"");
}
/* 卡片构建时若带标签,hover 行预埋 */
function prepCardTags(){
  if(!S.view) return;
  /* §6.3：原来逐项调 syncCardTags —— WeakMap miss/失效（网格刚重渲染）时
     每一项都全量 querySelectorAll 按路径回查，带标签项一多就是 O(n²)
     （实测几千项全带标签时冻结数百 ms~秒级）。改为单次遍历建 path→el
     映射（O(n)），逐项 Map 命中；单卡路径（贴/删标签后的刷新）保持原样。 */
  const g=document.getElementById("grid");
  let map=null;
  if(g){
    map=new Map();
    for(const c of g.querySelectorAll("[data-p]")) map.set(c.dataset.p, c);
  }
  for(let i=0;i<S.view.length;i++){
    const it=S.view[i];
    if(!it||!it.tags||!it.tags.length) continue;
    const _ce=(typeof S!=="undefined"&&S.cardEl)?S.cardEl.get(it):null;
    const cardEl=(_ce&&_ce.isConnected)?_ce:(map?(map.get(it.path)||null):null);
    if(cardEl) syncCardTagsInto(cardEl, it);
  }
}
/* 打开大图时渲染标签面板 */
window._renderTagPanelOnOpen=function(){
  if(S.solo) return;   // 单图模式：S.idx 指的是库里上一张，别拿它去算建议
  const it = (S.idx>=0) ? S.view[S.idx] : null;
  if(it){
    renderTagPanel(); setTimeout(prepCardTags,0);
    // meta 就绪后(轮询检测 curMeta 变化)渲染 AI 建议
    const _i=S.idx, _p=it.path;                 // 轮询期间可能已切图：用索引+路径双重校验
    const _t=setInterval(()=>{
      if(!lb.classList.contains("on")){ clearInterval(_t); return; }
      if(S.curMeta){ clearInterval(_t);
        const cur=S.view[S.idx];
        if(S.idx===_i && cur && cur.path===_p) renderTagSuggestions(it); }
    },300);
  }
};

/* ================= 标签筛选 P2:左栏 AND 累加 ================= */
let TAG_FILTER = [];            // 当前激活的标签
let TAG_EXCLUDE = [];           // 单独排除的标签（与 TAG_FILTER 互斥，右键标签行切换）
let TAG_MODE = "and";           // 多标签匹配：and=同时满足 / or=任一满足 / not=都不包含（排除）
try{ const m=localStorage.getItem("mv.tagmode"); if(m==="and"||m==="or"||m==="not") TAG_MODE=m; }catch(_){}
let tagListCache = [];          // 完整标签统计(渲染左栏)
let tagFilterActive = false;    // 当前视图是否处于标签筛选结果

/* ---------- O12（2026-10-28）：tagListCache 此前**只写不读** ----------
   `renderTagNav()` 每次（点筛选、展开标签组、切视图、标签签名变化）都重拉一次
   `/api/tags_list`；而这些都是**纯 UI 状态变化**，服务端计数根本没变
   （7484 张规模下这个接口要扫全索引）。
   改成缓存优先，身份判据三件套：
     ① `_tagCacheSrc === SUGGEST_TAGS`：任何拿到新统计的路径都会**换掉这个数组引用**
        （refreshTagStats / 改名 / 删除都走 `SUGGEST_TAGS = j.tags…`）⇒ 引用变了自动失效；
     ② `_tagCacheRoot`：标签统计是**按根目录**算的，换目录必须重拉；
     ③ 30s TTL 兜底：别的窗口/ComfyUI 侧改了标签时，最多滞后半分钟自愈。
   ⚠️ 别用「计数比一比」来判断要不要重拉 —— 那要求先拉到才能比，等于没省。 */
let _tagCacheSrc = null, _tagCacheRoot = null, _tagCacheAt = 0;
const TAG_CACHE_TTL = 30000;
function tagStatsStore(tags){
  tagListCache = tags || [];
  _tagCacheSrc = SUGGEST_TAGS;
  _tagCacheRoot = S.root || "";
  /* ⚠️ **空列表不许当有效缓存**：新根目录第一次拉回来的就是 `[]`，
     若照常标记「有效」，30s TTL 内的任何 renderTagNav 都会渲染出空左栏
     （实测撞过：门禁建好标签后左栏仍然 0 行）。空库时每条 renderTagNav
     都会重拉一次 —— 空库的 tags_list 本来就极便宜，代价可以忽略。 */
  _tagCacheAt = tagListCache.length ? Date.now() : 0;
  return tagListCache;
}
function tagStatsCacheOk(){
  return _tagCacheAt > 0
    && _tagCacheSrc === SUGGEST_TAGS
    && _tagCacheRoot === (S.root || "")
    && (Date.now() - _tagCacheAt) < TAG_CACHE_TTL;
}

/* 渲染左栏标签导航(带层级折叠,Top14 + 「全部」入口)
   ⚠️ E5-3（2026-09-23）订正：这行注释此前写的是「Top12 + 全部入口」—— 与实现**两处都不符**
   （实为 Top14，且**根本没有**「全部」入口）。实测的后果比报告写的更差：
     · 分组溢出那行（`… 还有 N 个子标签`）是 cursor:default 的**死文本**，点不动；
     · **扁平标签超过 14 个是「静默丢弃」** —— 连「还有 N 个」都不显示，用户不知道自己漏看了。
   现在两处都补可点的「展开全部 / 收起」，展开状态记在 TAG_NAV_MORE（跨重渲染保持）。 */
const TAG_NAV_TOP=14, TAG_NAV_KIDS=8;
let TAG_NAV_MORE=new Set();                     // '#flat' 或组名
function tagMoreRow(n, lvl, key, expanded){
  const pad = 9 + lvl*14;
  return '<div class="tlvl more" data-t="__more__" data-k="'+esc(key)+'" role="button" tabindex="0"'
    +' data-tip="'+(expanded?"收起这一组":"展开其余 "+n+" 个")+'"'
    +' style="padding-left:calc('+pad+'px*var(--fsx))">'
    +'<span class="ic">'+ico(expanded?"caretUp":"caretDown")+'</span>'
    +'<span class="nm">'+(expanded?"收起":"全部标签 · 还有 "+n+" 个")+'</span></div>';
}
function renderTagNav(){
  const nav=$("#tagNav"); if(!nav) return;
  refreshTagStatsNow(stats=>{
    tagListCache = stats||[];
    /* 层级标签的展示规则（此前会重复）：
       后端 tag_stats() 已把子标签计入父级链（人物/黑发 → 人物 也 +1），所以
         ① 有子标签的父级只在「分组头」渲染一次，不再同时出现在平铺标签列表里；
         ② 父级计数直接取统计值，不再把子项相加（那会重复计数）。 */
    const active=new Set(TAG_FILTER);
    const byName=new Map(stats.map(t=>[t.name,t.count]));
    const groups={};                                  // 父级名 → 子标签列表
    for(const t of stats){
      const i=t.name.indexOf("/");
      if(i<0) continue;
      const p=t.name.slice(0,i);                      // 只按第一段分组
      (groups[p]=groups[p]||[]).push(t);
    }
    const rows=[];                                    // {count, html}：顶层按计数降序排列
    const flats=stats.filter(t=>!t.name.includes("/") && !groups[t.name]);
    const flatOpen=TAG_NAV_MORE.has("#flat");
    for(const t of (flatOpen?flats:flats.slice(0,TAG_NAV_TOP)))
      rows.push({count:t.count,
        html:tagRow(t.name, t.count, 0, active.has(t.name), false)});
    if(flats.length>TAG_NAV_TOP){
      /* ⚠️ 这条独立成行、count=-1 ⇒ 排到最后。**不能**把它并进上面那 14 行做成一个整体行 ——
         那会把「扁平标签与分组行按计数交错」的既有版面改掉（每行独立参与排序是既有行为）。 */
      rows.push({count:-1,
        html:tagMoreRow(flats.length-TAG_NAV_TOP, 0, "#flat", flatOpen)});
    }
    for(const [p, kids] of Object.entries(groups)){
      const gOpen=TAG_NAV_MORE.has(p);
      let html=tagRow(p, byName.get(p)||0, 0, active.has(p), true);
      for(const k of (gOpen?kids:kids.slice(0,TAG_NAV_KIDS)))
        html+=tagRow(k.name, k.count, 1, active.has(k.name), false);
      if(kids.length>TAG_NAV_KIDS)
        html+=tagMoreRow(kids.length-TAG_NAV_KIDS, 1, p, gOpen);
      rows.push({count:byName.get(p)||0, html});      // 展开行在组内 ⇒ 排序后仍紧随其子项
    }
    rows.sort((a,b)=>b.count-a.count);                // 父级行插到它该在的位置，子项紧随其后
    nav.innerHTML=rows.map(r=>r.html).join("") + tagClearRow() + tagStatsNoteRow();
    bindTagNav();
  });
}
/* 标签配色（设计规范 2.5「语义化标签色」）：
   先按标签名里的关键词归类到 8 组柔和半透明色（风景→青、人物→粉、动物→紫…），
   命中不到关键词时按名字哈希兜底，保证同名标签在左栏、卡片、大图面板里始终同色。
   CSS 侧只认类名 .tg-*（见 css/tags.css），改色不用改这里。 */
const TAG_CLASSES = ["teal","pink","purple","yellow","orange","green","blue","red"];
const TAG_GROUPS = [
  ["teal",   ["风景","景","海","湖","山","天空","云","自然","户外","landscape","scenery","sea","ocean","sky","nature","mountain"]],
  ["pink",   ["人物","人像","肖像","女孩","男孩","美食","食物","portrait","person","girl","boy","face","food","woman","man"]],
  ["purple", ["动物","猫","狗","鸟","兽","动漫","二次元","插画","animal","cat","dog","bird","anime","manga","illustration"]],
  ["yellow", ["城市","街","都市","科幻","赛博","夜景","city","street","urban","scifi","sci-fi","cyber","neon","night"]],
  ["orange", ["建筑","室内","机械","科技","机甲","architecture","building","indoor","tech","mecha","robot"]],
  ["green",  ["植物","花","草","树","森林","plant","flower","tree","forest","leaf"]],
  ["blue",   ["空间","太空","宇宙","抽象","其他","space","cosmos","abstract","other"]],
  ["red",    ["最近","最新","收藏","重要","recent","favorite","fav","important"]],
];
function tagClass(name){
  const full=String(name||"").toLowerCase();
  const leaf=full.split("/").pop();
  for(const [cls,keys] of TAG_GROUPS)
    for(const k of keys)
      if(full.includes(k) || leaf.includes(k)) return "tg-"+cls;
  let h=0; const s=String(name||"");
  for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))>>>0;
  return "tg-"+TAG_CLASSES[h%TAG_CLASSES.length];
}
/* 圆点：类名驱动配色，样式全在 css/tags.css 的 .tg-* 里 */
function tagDot(name){
  return '<i class="tdot '+tagClass(name)+'"></i>';
}
function tagRow(name, count, lvl, on, isHeader){
  const ex = TAG_EXCLUDE.includes(name) || (TAG_MODE==="not" && on);   // 被排除：⊘ 红色
  const ic = ex ? '<span class="ic">'+ico("ban")+'</span>'
    : (isHeader ? '<span class="ic">'+ico("caretDown")+'</span>'
    : (lvl ? '<span class="ic">'+ico("dot")+'</span>' : '<span class="ic tdotwrap">'+tagDot(name)+'</span>'));
  const pad = 9 + lvl*14;
  const tip = ex ? "已排除：点击取消排除 · 右键切换" : "点击筛选 · 右键排除该标签";
  /* 第 16 级：目录不可读时后端给的是**未过滤**的旧口径，数字必然偏大 ⇒ 一律显示「—」。
     写一个我们验证不了的数字，比写「不知道」更容易把人带沟里。 */
  const cntTxt = TAG_STATS_UNKNOWN ? "—" : count;
  return '<div class="tlvl'+(on?" on":"")+(ex?" ex":"")+'" data-t="'+esc(name)+'" data-tip="'+tip+'"'
    +' style="padding-left:calc('+pad+'px*var(--fsx))">'
    +ic+'<span class="nm">'+esc(name)+'</span>'
    +'<span class="c">'+cntTxt+'</span></div>';
}
/* 第 16 级：计数口径的**出口**。数字比昨天小了（标签指向的文件没了）、或者这次
   根本判不出来（盘掉线），界面上都得有一句话说明；否则用户看到的就是「标签凭空少了」。
   刻意**不带 data-t** —— 它不是一个筛选入口，bindTagNav 只绑 [data-t]，点了不该有反应。 */
function tagStatsNoteRow(){
  if(TAG_STATS_UNKNOWN)
    return '<div class="tlvl more" data-tip="媒体目录当前读不到（外接盘掉线或被移走），'
      +'无法确认哪些文件还在，因此标签数字暂时以「—」显示；目录恢复后会自动更新">'
      +'<span class="ic">'+ico("dot")+'</span>'
      +'<span class="nm">目录暂时读不到 · 数字未核实</span></div>';
  if(TAG_STATS_DANGLING>0)
    return '<div class="tlvl more" data-tip="这些标签指向的文件已不在图库中（或已不是图片/视频），'
      +'所以不计入上面的数字">'
      +'<span class="ic">'+ico("dot")+'</span>'
      +'<span class="nm">还有 '+TAG_STATS_DANGLING+' 个标签已失效</span>'
      /* R13（2026-11-15）：此前这行是一句**没有出口**的说明 —— 数字就摆在那儿，
         用户只能等换根或手工改 tags.json。现在右侧给一个「清理」：
         只动标签记录，不动任何图片；点之前还会弹确认（见 pruneDanglingTags）。
         刻意不带 data-t：它不是筛选入口，点它不该改 TAG_FILTER。 */
      +'<span class="c act" data-act="pruneTags" data-tip="删掉这些指向已不存在文件的标签记录（不动任何图片）">清理</span></div>';
  return "";
}
function tagFilterDesc(){
  let d="";
  if(TAG_FILTER.length)
    d = TAG_MODE==="not" ? TAG_FILTER.map(t=>"非 "+t).join(" ")
                         : TAG_FILTER.join(TAG_MODE==="or"?" 或 ":" + ");
  if(TAG_EXCLUDE.length)
    d = (d?d+" ":"") + TAG_EXCLUDE.map(t=>"非 "+t).join(" ");
  return d;
}
function tagClearRow(){
  const none = !TAG_FILTER.length && !TAG_EXCLUDE.length;
  const d = tagFilterDesc();
  return '<div class="litem'+(none?" on":"")+'" data-t="__clear__"><span class="ic">'+ico("gridTable")+'</span><span class="nm">全部图片</span>'
    +(d?'<span class="cnt">'+esc(d)+'</span>':"")+'</div>';
}
/* R13（2026-11-15）：「N 个标签已失效」的清理动作。
   与 refreshTagStats / refreshTagStatsNow 的区别只有一个 —— **可 await**。
   清理完必须等新统计到手再 renderTagNav，否则 tagListCache 还认为旧数组有效
   （`_tagCacheSrc === SUGGEST_TAGS` 仍成立），左栏会原样重画旧数字。 */
async function refreshTagStatsAwait(){
  const tok = ++_tagStatsTok;
  const j = await api("/api/tags_list").catch(()=>null);
  if(!j || tok !== _tagStatsTok) return false;
  SUGGEST_TAGS = (j.tags||[]);
  TAG_STATS_DANGLING = j.dangling||0;
  TAG_STATS_UNKNOWN = !!j.unknown;
  tagStatsStore(j.tags);
  renderTagNav();
  return true;
}
function pruneDanglingTags(){
  const n = TAG_STATS_DANGLING;
  confirmDialog("清理失效标签",
    "把 "+n+" 条指向已不存在文件的标签记录从标签表里删掉。只删记录，不动任何图片；"
    +"清理前会自动备份标签表。",
    async()=>{
      const j = await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"prune"})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err",6000); return; }
      await refreshTagStatsAwait();
      if(j.removed) toast("已清理 "+j.removed+" 条失效标签","ok",4200);
      else toast("没有可清理的失效标签","ok",3000);
    },
    {okText:"清理", okClass:""});    // 不动文件 ⇒ 主色按钮，别用红色「删除」语义
}
function bindTagNav(){
  const nav=$("#tagNav");
  nav.querySelectorAll("[data-t]").forEach(el=>{
    el.onclick=()=>{
      const t=el.dataset.t;
      if(t==="__clear__"){ TAG_EXCLUDE=[]; setTagFilter([]); return; }
      if(t==="__more__"){                              // E5-3：展开/收起（**不是**筛选，别落进下面）
        const k=el.dataset.k||"";
        if(TAG_NAV_MORE.has(k)) TAG_NAV_MORE.delete(k); else TAG_NAV_MORE.add(k);
        renderTagNav(); return;
      }
      if(TAG_EXCLUDE.includes(t)){                     // 已排除的：点击=取消排除
        TAG_EXCLUDE=TAG_EXCLUDE.filter(x=>x!==t);
        setTagFilter([...TAG_FILTER]);
        return;
      }
      const i=TAG_FILTER.indexOf(t);
      const next = i>=0 ? TAG_FILTER.filter(x=>x!==t) : [...TAG_FILTER, t];
      setTagFilter(next);
    };
    /* E5-3：展开行带了 tabindex=0 ⇒ 必须配键盘触发，否则是「可聚焦但无响应」
       （WCAG 2.1.1 键盘可达性反例，比不给它 tabindex 更糟）。其余标签行维持既有鼠标语义。 */
    if(el.dataset.t==="__more__")
      el.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); el.click(); } };
    el.oncontextmenu=e=>{                              // 右键：切换「排除该标签」
      const t=el.dataset.t;
      if(t==="__clear__") return;
      if(t==="__more__") return;                       // E5-3：展开行没有「排除」语义
      e.preventDefault(); e.stopPropagation();
      if(TAG_EXCLUDE.includes(t)) TAG_EXCLUDE=TAG_EXCLUDE.filter(x=>x!==t);
      else { TAG_EXCLUDE=[...TAG_EXCLUDE, t]; TAG_FILTER=TAG_FILTER.filter(x=>x!==t); }
      if(TAG_FILTER.length || TAG_EXCLUDE.length) setTagFilter([...TAG_FILTER]);
      else setTagFilter([]);
    };
  });
  /* R13（2026-11-15）：「清理失效标签」按钮单独绑，**不能**混进上面的 [data-t] ——
     那条路径会把 data-t 的值当标签名去 toggle（`data-act` 不在选择器里，天然分开）。 */
  nav.querySelectorAll("[data-act]").forEach(el=>{
    el.onclick=ev=>{
      ev.stopPropagation();
      if(el.dataset.act==="pruneTags") pruneDanglingTags();
    };
  });
}
/* 应用标签筛选:走 /api/tags_filter 全量换 S.view(保留 S.path 供退出恢复) */
/* ε-6 E6-4：这里原来是本文件**自造**的 `tagTok`。它与 app.js 的 `loadTok`、
   filters.js 的 `pTok` 是同一个病的三份药 —— 各自只能作废「自己这一类」的在途请求，
   于是「切目录」作废不了在途的标签筛选（慢的 /api/tags_filter 后到 ⇒ 刚切到的目录
   又被换成上一次的标签结果，而路径框/面包屑仍显示目录态）。
   统一到 state.js 的 `viewGen`：**凡决定整个列表内容**的加载（load / loadFavView /
   setTagFilter / promptSearch）都是权威视图加载，一律 `++viewGen`。 */
async function setTagFilter(tags){
  /* P1（2026-10-09）：进入先快照。下面 TAG_FILTER 是**同步**改的，而
     tagFilterActive / S.tagView 只在请求成功后才更新 —— 失败时若不回滚，
     界面会停在「chip 说筛了狗、网格还是猫（或干脆是目录列表）」的自相矛盾态，
     而且只有一句 3.2 秒就消失的 toast。回滚需要调用前的原值。 */
  const _prevF=TAG_FILTER, _prevE=TAG_EXCLUDE, _prevActive=tagFilterActive;
  TAG_FILTER=tags;
  const tok=++viewGen;
  renderTagNav();                     // 立即刷新高亮(含"全部图片"清除行)
  if(!TAG_FILTER.length && !TAG_EXCLUDE.length){
    S.tagView=null;
    if(tagFilterActive){
      tagFilterActive=false;
      S.view=[]; S.shown=0;
      vObs.disconnect(); grid.innerHTML="";
      if(S.metaMap) await loadMetaMap();      // 元数据面板开着：基准列表切回目录列表
      if(tok!==viewGen) return;
      applyFilter();                          // 用内存里的目录列表重建视图；不动元数据筛选
      toast("已退出标签筛选");
    }
    return;
  }
  toast("按标签筛选: "+tagFilterDesc());
  try{
    const j=await api("/api/tags_filter?tags="+encodeURIComponent(TAG_FILTER.join("|"))
      +"&mode="+TAG_MODE
      +"&not="+encodeURIComponent(TAG_EXCLUDE.join("|"))
      +"&path="+encodeURIComponent(S.path||S.root));
    if(tok!==viewGen) return;
    tagFilterActive=true;
    S.tagView=j.items||[];              // 标签筛选的基准列表：后续本地筛选都基于它
    S.shown=0;
    vObs.disconnect(); grid.innerHTML="";
    if(S.metaMap) await loadMetaMap();       // 先让元数据映射覆盖标签结果，再叠加筛选
    if(tok!==viewGen) return;
    applyFilter();                           // 叠加类型/时间/排序/元数据筛选（不再直接 renderMore）
    $("#count").textContent=S.view.filter(x=>!TL_SYNTH[x.type]).length+" 项 · 标签";
  }catch(e){
    /* P1（2026-10-09）：失败必须回滚到调用前 —— 否则 chip 与结果长期不符。
       仅当本次仍是最新一代才回滚：tok!==viewGen 说明已被更新的一次调用接管，
       那种情况由新调用决定最终状态，老的乱回滚反而会覆盖新的。 */
    if(tok===viewGen){
      TAG_FILTER=_prevF; TAG_EXCLUDE=_prevE; tagFilterActive=_prevActive;
      renderTagNav();
      if(typeof renderChips==="function") renderChips();
      applyFilter();                     // 用回滚后的条件把视图拉回一致
    }
    toast("标签筛选失败:"+e.message,"err");
  }
}

/* 标签管理弹窗:改名/删除 */
function openTagManager(){
  const dlg=bigDialog("标签管理");
  refreshTagStatsNow(stats=>{
    if(!dlg.mask.isConnected) return;
    if(!stats || !stats.length){
      dlg.body.innerHTML='<div style="color:var(--tx3)">还没有标签。在图片详情面板或批量操作里打标签后,这里可全局改名/删除。</div>';
      return;
    }
    let h='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
    for(const t of stats)
      h+='<div class="tagchip '+tagClass(t.name)+'" data-t="'+esc(t.name)+'" style="cursor:default"><span class="leaf">'+esc(t.name)+'</span>'
        +'<span style="color:var(--tx3);font-size:calc(10px*var(--fsx))">'+t.count+'</span>'
        +'<span class="x" data-del="'+esc(t.name)+'" data-tip="删除(从所有图摘除)">'+ico("x")+'</span></div>';
    h+='</div><div style="display:flex;gap:8px;align-items:center;margin-top:4px">'
      +'<input id="mgRenameFrom" placeholder="要改名的标签(可从上面点击填入)" style="flex:2;min-width:140px">'
      +'<span style="color:var(--tx3)">→</span>'
      +'<input id="mgRenameTo" placeholder="新名字" style="flex:1;min-width:100px">'
      +'<button id="mgRenameGo" class="primary">改名合并</button></div>';
    dlg.body.innerHTML=h;
    // 点 chip 填入 renameFrom
    dlg.body.querySelectorAll(".tagchip").forEach(ch=>{
      ch.addEventListener("click",e=>{
        if(e.target.dataset.del) return;
        dlg.body.querySelector("#mgRenameFrom").value=ch.dataset.t;
      });
      const x=ch.querySelector(".x");
      if(x) x.onclick=async e=>{
        e.stopPropagation();
        const t=x.dataset.del;
        confirmDialog("删除标签","从所有图片上摘除「"+t+"」?该操作不可撤销(可重新打标)。",async()=>{
          const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({op:"delete",tag:t})}).catch(e=>({error:e.message}));
          if(j.error){ toast(j.error,"err"); return; }
          toast("已删除标签「"+t+"」("+j.removed+" 张图)","ok");
          SUGGEST_TAGS=(j.tags_all||[]);
          applyTagRenameLocal(new Map([[t,null]]));      // P1-13：内存标签同步摘除
          dlg.close();
          /* 2026-11-05 P2：顺序必须是「先重绘左栏、后开弹窗」。旧顺序
             openTagManager → renderTagNav：前者向 refreshTagStatsNow 领了令牌 N，
             后者紧接着 ++ 成 N+1 ⇒ N 的回调被 `!fresh()` 丢弃 ⇒ 弹窗永久空白。
             另外把刚拿到的 tags_all 灌进统计缓存：非空时 openTagManager 命中缓存
             同步渲染，一次请求都不发（空列表仍会重拉，但那时弹窗本来也是空态）。 */
          tagStatsStore(SUGGEST_TAGS);
          renderTagNav();
          openTagManager();
        });
      };
    });
    const go=dlg.body.querySelector("#mgRenameGo");
    go.onclick=async()=>{
      const f=dlg.body.querySelector("#mgRenameFrom").value.trim();
      const t=dlg.body.querySelector("#mgRenameTo").value.trim();
      if(!f||!t){ toast("请填写两个名字","err"); return; }
      const j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"rename",from:f,to:t})}).catch(e=>({error:e.message}));
      if(j.error){ toast(j.error,"err"); return; }
      toast("已改名:「"+f+"」→「"+t+"」("+j.moved+" 张图)","ok");
      SUGGEST_TAGS=(j.tags_all||[]);
      applyTagRenameLocal(tagRenameMap(f, t));       // P1-13：内存标签同步改名（含子标签）
      dlg.close();
      /* 2026-11-05 P2：同删除分支 —— 先重绘左栏、后开弹窗（否则弹窗的统计令牌
         会被紧接的 renderTagNav 顶掉，弹窗永久空白）。 */
      tagStatsStore(SUGGEST_TAGS);
      renderTagNav();
      openTagManager();
      renderTagNav();
    };
  });
}

/* 同步拉取标签统计(带回调) */
let _tsBusy=false;
/* P1-13：标签**全局改名 / 删除**是在服务端批量改所有图，前端内存里的
   `S.items[].tags` / `S.tagView[].tags` / 大图当前项的 tags 都还留着旧名字 ——
   此前只调 renderTagNav()，于是左栏显示新名、卡片与大图 chip 仍是旧名，
   界面自相矛盾。这里就地重写内存标签并回写已渲染的卡片，不重新拉整列表
   （重拉会丢滚动位置与翻页进度，且改名这种纯标签操作没必要重扫目录）。
   新名用一个 Map 表达：{旧名: 新名|null}，null = 删除。 */
function applyTagRenameLocal(map){
  /* ⚠️ map 是 **Map** 不是普通对象：`Object.keys(map)` 恒为空数组，
     早先写成 `!Object.keys(map).length` 会让本函数永远提前 return 0（静默失效）。
     判空要走 map.size。 */
  if(!map || !map.size) return 0;
  /* 子标签的改名规则**不依赖 SUGGEST_TAGS**（它是异步拉的，改名时可能还没到位，
     真页面实测就出现过「只改了父标签、风格/汉服 原样不动」）。
     这里按前缀就地推导：任何以 `旧名/` 开头的标签都改成 `新名/` + 余下部分。 */
  const rules=[];                              // [{from, to}]  to=null 表示删除
  for(const [k,v] of map) rules.push({from:k, to:v});
  /* 最长前缀优先：先处理 `风格/汉服`，再处理 `风格`，避免短规则把长标签先切掉 */
  rules.sort((x,y)=>y.from.length-x.from.length);
  const fix = (arr)=>{
    if(!Array.isArray(arr)) return;
    for(const r of rules){
      for(let i=arr.length-1;i>=0;i--){
        const t=arr[i];
        if(t===r.from){
          if(r.to===null) arr.splice(i,1);
          else arr[i]=r.to;
          continue;
        }
        /* P1（2026-10-09）：删除（to===null）也要按前缀连带子标签。
           后端 tags 的删除是 `x != n and not x.startswith(n+"/")` —— 删「风格」
           会连带删掉「风格/汉服」；前端原来只在 to!==null（改名）时走前缀规则，
           于是左栏已经没有子标签、卡片 hover 行与大图 chip 仍写着「风格/汉服」，
           一直挂到下次整列表刷新。这里与后端对齐。 */
        if(t.startsWith(r.from+"/")){
          if(r.to===null) arr.splice(i,1);
          else arr[i]=r.to+t.slice(r.from.length);
        }
      }
    }
    // 去重（改名后可能与已有标签撞名），保持首次出现顺序
    const seen=new Set();
    for(let i=0;i<arr.length;i++){
      if(seen.has(arr[i])){ arr.splice(i,1); i--; }
      else seen.add(arr[i]);
    }
  };
  let touched=0;
  const seen=new Set();
  const visit=(arr)=>{
    for(const it of (arr||[])){
      if(!it || typeof it!=="object" || !Array.isArray(it.tags)) continue;
      if(seen.has(it)) continue;
      seen.add(it);
      const before=it.tags.join("\u0000");
      fix(it.tags);
      if(it.tags.join("\u0000")!==before){
        touched++;
        if(typeof syncCardTags==="function") syncCardTags(it);   // 卡片 hover 行 / 列表「标签」列
      }
    }
  };
  visit(S.view); visit(S.tagView); visit(S.items);
  /* 注：S.dirs 只是子文件夹卡片 {name,path,n}，不带 items，无需遍历 */
  /* 大图正开着 → 重新渲染元数据面板（标签 chip 在其中；用当前已缓存的 meta，不重发请求） */
  if(typeof curItem==="function" && S.curMeta){
    const cur=curItem();
    if(cur && typeof renderMeta==="function") renderMeta(S.curMeta, cur);
  }
  return touched;
}
/* 把「旧名→新名」的清单整理成 Map（含父标签：改名「风格」时「风格/汉服」也要变） */
function tagRenameMap(from,to){
  const m=new Map(); m.set(from, to);
  for(const t of (SUGGEST_TAGS||[])){
    const n=t && t.name;
    if(n && n!==from && n.startsWith(from+"/")) m.set(n, to+n.slice(from.length));
  }
  return m;
}

function refreshTagStatsNow(cb){
  /* F5-P2-2（2026-10-20）：没有令牌 —— 连点标签时每次都调 renderTagNav → 这里，
     而 `/api/tags_list` 的响应顺序不保证 ⇒ **慢的旧回调后到**，把左栏整片重写成旧统计
     与旧勾选态（TAG_FILTER 已是新值，二者短暂不一致）。
     加一个自增令牌：只有最新一次发出的请求有权回调（与 extras 的 viewGen 同款思路，
     但这里只关心「这几次统计请求」的先后，不该被切目录抢走）。 */
  const tok = ++_tagStatsTok;
  const fresh = () => tok === _tagStatsTok;
  /* O12：命中缓存就**同步**回调，一个请求都不发。调用方只关心「拿到一份可用的统计」，
     不关心它是刚拉的还是缓存里的（它们都渲染同一份结构）。 */
  if(tagStatsCacheOk()){
    if(cb) cb(tagListCache);
    return;
  }
  api("/api/tags_list").then(j=>{
    if(!fresh()) return;
    /* 第 16 级：口径状态必须和列表**同一份响应**一起更新 —— 分两次拉会出现
       「数字已按新口径、提示还停在旧口径」的中间态。 */
    TAG_STATS_DANGLING = j.dangling||0;
    TAG_STATS_UNKNOWN = !!j.unknown;
    const list = tagStatsStore(j.tags);
    if(cb) cb(list);
  }).catch(()=>{ if(fresh() && cb) cb([]); });
}
let _tagStatsTok = 0;          // F5-P2-2：统计请求的世代号（定义在函数前，hoisting 不影响 let 使用顺序）

/* ================= P3:批量标签 / 右键子菜单 / AI 建议 ================= */

/* --- 批量给 batchSel 贴标签 --- */
function openBatchTag(){
  const n=batchSel.size;
  if(!n){ toast("请先选择图片","err"); return; }
  const dlg=bigDialog("批量贴标签（"+n+" 张）");
  dlg.body.innerHTML='<div style="margin-bottom:10px;font-size:calc(12px*var(--fsx));color:var(--tx3)">'
    +'勾选要贴到全部 '+n+' 张上的标签；已存在的不会重复。也可输入新标签回车添加。</div>'
    +'<div id="btChips" class="tagwrap" style="margin-bottom:10px"></div>'
    +'<div style="display:flex;gap:6px;align-items:center;margin-bottom:14px">'
    +'<input id="btNew" placeholder="新标签名…" style="flex:1;min-width:120px" maxlength="40">'
    +'<button id="btAddNew" class="primary">'+ico("plus")+' 添加</button></div>'
    +'<div class="cbtns" style="display:flex;justify-content:flex-end;gap:8px">'
    +'<button id="btCancel">取消</button><button id="btGo" class="primary" disabled>贴到 '+n+' 张</button></div>';
  const chipBox=dlg.body.querySelector("#btChips");
  const picked=new Set();
  let pendingNew="";
  function renderChips(){
    chipBox.innerHTML="";
    for(const t of SUGGEST_TAGS){
      if(t.count===0) continue;
      chipBox.insertAdjacentHTML("beforeend",
        '<span class="tagchip '+tagClass(t.name)+'" data-t="'+esc(t.name)+'" style="'+(picked.has(t.name)
          ?'background:var(--ac-soft);border-color:var(--ac)':"")+'">'
        +'<span class="leaf">'+esc(t.name)+'</span><span class="c" style="color:var(--tx3);font-size:calc(10px*var(--fsx))">'+t.count+'</span></span>');
    }
    for(const t of picked){ if(!SUGGEST_TAGS.some(x=>x.name===t)){
      chipBox.insertAdjacentHTML("beforeend",
        '<span class="tagchip" data-t="'+esc(t)+'" style="background:var(--ac-soft);border-color:var(--ac)">'
        +'<span class="leaf">'+esc(t)+'</span></span>'); } }
    chipBox.querySelectorAll(".tagchip").forEach(ch=>ch.onclick=()=>{
      const t=ch.dataset.t;
      if(picked.has(t)) picked.delete(t); else picked.add(t);
      renderChips();
      dlg.body.querySelector("#btGo").disabled=!picked.size;
    });
  }
  renderChips();
  /* 4.2-10（2026-11-05 审计）：输入名要与服务端 `_norm_tag` 同一口径。
     服务端会 `"/".join(s.strip() for s in t.split("/") if s.strip())`，
     前端却把原文塞进 picked ⇒ 输入「风格 / 汉服」时卡片显示带空格的名字、
     落盘却是「风格/汉服」，刷新后像被改过。这里先归一，再进 picked / 建议列表。 */
  const _normTagInput = s => String(s == null ? "" : s)
    .split("/").map(x => x.trim()).filter(Boolean).join("/");
  const addPending=()=>{
    const v=dlg.body.querySelector("#btNew").value.trim();
    if(!v) return;
    /* F5-P2-3：与单图入口同一套校验 —— 别让可预知的 400 留到服务端去拒绝。 */
    const _bad= _tagInvalid(v);
    if(_bad){ toast("标签名不可用："+_bad, "err"); return; }
    const vn = _normTagInput(v);
    if(!vn){ toast("标签名不可用：不能只有分隔符", "err"); return; }
    pendingNew=vn;
    picked.add(vn);
    SUGGEST_TAGS=[{name:vn,count:0},...SUGGEST_TAGS.filter(x=>x.name!==vn)];
    dlg.body.querySelector("#btNew").value="";
    renderChips();
    dlg.body.querySelector("#btGo").disabled=false;
  };
  dlg.body.querySelector("#btNew").onkeydown=e=>{ if(e.isComposing || e.keyCode===229) return;   // 同上
    if(e.key==="Enter"){ e.preventDefault(); addPending(); } };
  dlg.body.querySelector("#btAddNew").onclick=addPending;
  dlg.body.querySelector("#btCancel").onclick=dlg.close;
  dlg.body.querySelector("#btGo").onclick=async()=>{
    if(!picked.size) return;
    const paths=[...batchSel];
    const tags=[...picked];
    /* P1-18：原来是 N×M 次串行 await，且 `ok++` 写在内层循环外 —— 成功计数恒等于
       选中张数，全失败也照样报「已给 N 张图贴上 M 个标签」。现在改走服务端单请求
       （op:add_many），一次锁一次落盘，并按**服务端返回的真实对数**汇报。
       失败也逐条说清是哪一张、为什么，而不是含糊一句「失败 X 次」。 */
    const go=dlg.body.querySelector("#btGo");
    go.disabled=true;
    const oldTxt=go.textContent;
    setBotState("busy",`正在为 ${paths.length} 张图贴上 ${tags.length} 个标签…`,null,true);
    let j;
    try{
      j=await api("/api/tags",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({op:"add_many",paths:paths,tags:tags})});
    }catch(e){ j={error:e.message}; }
    go.disabled=false; go.textContent=oldTxt;
    if(j.error){
      dlg.close();
      setBotState("err","贴标签失败",null,false);
      toastErr("贴标签失败", j.error);
      return;
    }
    /* P1（2026-10-09）两条口径修正：
       ① `j.ok` 是**成功标志**（后端返回前会置 True），拿它当计数 ⇒ `pairs`
          恒为布尔，底栏「共 true 处新增」、toast 里「部分图本来已有该标签」
          的归因全是假的。计数改读 `j.pairs`，并对后端再次漂移兜底。
       ② 落盘失败的记录 `path` 是**空串**（后端目前只有整表级失败这一支），
          原来 `filter(x=>x.path)` 把真正的写盘失败全滤掉 ⇒ failedN 恒 0，
          界面把「磁盘写不进去」说成「这几张图早就贴过了」，用户不去查数据
          目录只会反复重试。改按**条数**判，并把原始异常摆到详情里。 */
    const added=j.files||0;
    const pairs=Number.isFinite(j.pairs)?j.pairs:added;
    const fails=(j.failed||[]);
    const failedN=fails.length;
    dlg.close();
    if(added===0){
      setBotState(failedN?"err":"warn", failedN?"贴标签失败":"没有新增标签", null, false);
      toastErr("没有贴上任何标签",
               failedN ? ("写入标签失败，请检查数据目录是否可写"
                          + ((fails[0]&&fails[0].error) ? "（"+fails[0].error+"）" : ""))
                       : "所选的标签这几张图上都已经有了");
    }else{
      setBotState("done",`已给 ${added} 张图贴上标签（共 ${pairs} 处新增）`,null,false);
      toast(`已给 ${added} 张图贴上标签`+(pairs!==added?`（共 ${pairs} 处新增，部分图本来已有该标签）`:"")
            +(failedN?`；${failedN} 处未成功`:""));
    }
    /* 内存同步：把新增的标签就地并进这些图，避免等下次整列表刷新才看到 */
    if(added){
      /* F5-P2-5（2026-10-20）：原来按**选中路径**就地补标签 —— 而服务端会把已被删除 /
         移出 root 的路径从 clean_p 里剔掉（不进 failed）⇒ 这些文件在卡片上也会显示新标签，
         实际一个都没落盘。现在优先用服务端回传的真实成功清单（`j.paths`），
         没有该字段时退回旧口径（兼容旧后端）。
         顺带把 `paths.includes` 换成 Set：原来选 500 张 × 列表 1 万条是 O(n·m) 的线性扫。 */
      const doneSet = new Set(Array.isArray(j.paths) ? j.paths : paths);
      const seen=new Set();
      for(const it of ((S.view||[]).concat(S.tagView||[], S.items||[]))){
        if(!doneSet.has(it.path) || seen.has(it)) continue;
        seen.add(it);
        it.tags=Array.from(new Set([...(it.tags||[]), ...tags]));
        if(typeof syncCardTags==="function") syncCardTags(it);
      }
    }
    refreshTagStats(); refreshStatsSoon();
  };
}

/* --- AI 建议词表：从正向提示词里提「人物 / 穿搭 / 场景 / 风格 / 构图」标签 ---
   ⚠️ 2026-11-27 A1+A2（主人要求「预设标签太少，能拓展吗」）：
     旧表 51 条，全库 12,141 张实测下来 —— 「蕾丝」3,058 次命中里有 1,386 次其实是
     necklace / bracelet（45%）；「Cosplay」380 次里 267 次是 cosmic / costume（70%）；
     「城市夜景」557 次里 342 次是白天的 street（61%）—— 全是裸子串匹配惹的祸。
     本次两件事：
       ① **收紧**：拉丁词一律带词边界（`\b`）；`cos` 只认 cosplay；夜景必须同时出现
          night / neon；`shota`（男性）从「少女」挪到「男性」；snow white 不算雪景；
          项链/手链单独给了标签，不再冒充蕾丝。
       ② **扩容**：51 → 180 条，补性别/发长/瞳色/兽耳、景别构图、画风流派、天气与室内场景。
     每条可带 `w`（权重，默认 10）：具体标签 14~20、中等 10~13、泛化 4~8。
     排序 = 权重 → 命中位置（提示词越靠前越可能是主体）→ 词表顺序（稳定可复现）。
     ⚠️ 加词只往本数组追加即可；`tag` 里不要写空格（层级按 `/` 切，带空格会让分组错位）。 */
const SUGGEST_LEXICON=[
  // ---------- 人物 · 性别 / 年龄感（w 低 = 泛化，不该挤掉具体标签） ----------
  {re:/(?:^|[^a-z])1boy|male focus|solo male|\bmale\b|\bman\b|男性|男生/i, tag:"人物/男性", w:7},
  {re:/(?:^|[^a-z])1girl|female focus|\bwoman\b|\bfemale\b|少女|女孩|女生/i, tag:"人物/少女", w:6},
  {re:/loli|萝莉|幼女/i, tag:"人物/萝莉", w:9},
  {re:/mature female|office lady|御姐|熟女/i, tag:"人物/御姐", w:12},
  {re:/old man|elderly|大叔|老人/i, tag:"人物/长者", w:12},
  {re:/muscular|\babs\b|肌肉/i, tag:"人物/肌肉", w:12},
  {re:/chubby|plump|curvy|丰满/i, tag:"人物/丰满", w:12},
  // ---------- 人物 · 发色 ----------
  {re:/black hair|dark hair|黑发/i, tag:"人物/黑发", w:13},
  {re:/white hair|silver hair|白发|银发/i, tag:"人物/白发", w:13},
  {re:/blonde|golden hair|金发/i, tag:"人物/金发", w:13},
  {re:/brown hair|棕发|栗发/i, tag:"人物/棕发", w:13},
  {re:/pink hair|sakura hair|粉发/i, tag:"人物/粉发", w:13},
  {re:/blue hair|蓝发/i, tag:"人物/蓝发", w:13},
  {re:/purple hair|violet hair|紫发/i, tag:"人物/紫发", w:13},
  {re:/red hair|ginger hair|scarlet hair|红发/i, tag:"人物/红发", w:13},
  {re:/green hair|绿发/i, tag:"人物/绿发", w:13},
  {re:/gr[ae]y hair|灰发|银灰发/i, tag:"人物/灰发", w:13},
  {re:/multicolou?red hair|two-?tone hair|gradient hair|挑染/i, tag:"人物/挑染", w:14},
  // ---------- 人物 · 发长 / 发型 ----------
  {re:/long hair|长发/i, tag:"人物/长发", w:12},
  {re:/short hair|短发/i, tag:"人物/短发", w:11},
  {re:/medium hair|中长发/i, tag:"人物/中长发", w:12},
  {re:/twin ?tails|twintails|双马尾/i, tag:"人物/双马尾", w:12},
  {re:/ponytail|马尾/i, tag:"人物/马尾", w:12},
  {re:/braid|辫子/i, tag:"人物/辫子", w:12},
  {re:/bob cut|波波头/i, tag:"人物/波波头", w:13},
  {re:/\bbangs\b|刘海/i, tag:"人物/刘海", w:8},
  {re:/hime cut|姬发式/i, tag:"人物/姬发式", w:14},
  {re:/ahoge|呆毛/i, tag:"人物/呆毛", w:13},
  {re:/\bbun\b|hair bun|丸子头/i, tag:"人物/丸子头", w:13},
  // ---------- 人物 · 瞳色 ----------
  {re:/red eyes?|赤瞳|红瞳/i, tag:"人物/红瞳", w:12},
  {re:/blue eyes?|碧眼|蓝瞳/i, tag:"人物/蓝瞳", w:12},
  {re:/green eyes?|绿瞳/i, tag:"人物/绿瞳", w:12},
  {re:/yellow eyes?|golden eyes?|amber eyes?|金瞳/i, tag:"人物/金瞳", w:12},
  {re:/purple eyes?|violet eyes?|紫瞳/i, tag:"人物/紫瞳", w:12},
  {re:/heterochromia|异色瞳/i, tag:"人物/异色瞳", w:15},
  // ---------- 人物 · 表情 ----------
  {re:/\bsmile\b|微笑|笑容/i, tag:"人物/笑容", w:7},
  {re:/\bblush\b|脸红|害羞/i, tag:"人物/脸红", w:8},
  {re:/crying|tears|流泪|眼泪/i, tag:"人物/眼泪", w:9},
  {re:/closed eyes|闭眼/i, tag:"人物/闭眼", w:8},
  {re:/looking at viewer|看向镜头/i, tag:"人物/看向镜头", w:8},
  {re:/open mouth|张嘴/i, tag:"人物/张嘴", w:7},
  // ---------- 人物 · 身体特征 ----------
  {re:/cat ears|nekomimi|猫耳/i, tag:"人物/猫耳", w:14, g:"ears"},
  {re:/fox ears|狐耳/i, tag:"人物/狐耳", w:14, g:"ears"},
  {re:/wolf ears|狼耳/i, tag:"人物/狼耳", w:14, g:"ears"},
  {re:/bunny ears|rabbit ears|usagi|兔耳/i, tag:"人物/兔耳", w:14, g:"ears"},
  {re:/animal ears|兽耳/i, tag:"人物/兽耳", w:12, g:"ears"},
  {re:/\bhorns?\b|犄角/i, tag:"人物/角", w:13},
  {re:/\bwings?\b|翅膀/i, tag:"人物/翅膀", w:13},
  {re:/\btail\b|尾巴/i, tag:"人物/尾巴", w:11},
  {re:/elf ears|pointy ears|精灵耳/i, tag:"人物/精灵耳", w:15},
  {re:/freckles|雀斑/i, tag:"人物/雀斑", w:13},
  {re:/tattoo|纹身/i, tag:"人物/纹身", w:13},
  {re:/glasses|meganekko|眼镜/i, tag:"人物/眼镜", w:11},
  // ---------- 穿搭 · 服装 ----------
  {re:/hanfu|chinese clothes|汉服|古风|中国风|华服/i, tag:"穿搭/汉服", w:16},
  {re:/qipao|cheongsam|旗袍/i, tag:"穿搭/旗袍", w:16},
  {re:/kimono|yukata|和服|浴衣/i, tag:"穿搭/和服", w:15},
  {re:/cosplay|コスプレ/i, tag:"穿搭/Cosplay", w:15},
  {re:/lolita|洛丽塔/i, tag:"穿搭/洛丽塔", w:15},
  {re:/\bmaid\b|女仆/i, tag:"穿搭/女仆装", w:13},
  {re:/school uniform|校服/i, tag:"穿搭/校服", w:13, g:"uniform"},
  {re:/\buniform\b|seifuku|制服/i, tag:"穿搭/制服", w:9, g:"uniform"},
  {re:/\bsuit\b|business suit|西装|西服/i, tag:"穿搭/西装", w:11},
  {re:/wedding dress|婚纱/i, tag:"穿搭/婚纱", w:16},
  {re:/evening gown|long dress|长裙/i, tag:"穿搭/长裙", w:12, g:"skirt"},
  {re:/mini ?skirt|短裙/i, tag:"穿搭/短裙", w:12, g:"skirt"},
  {re:/\bdress\b|\bgown\b|\bskirt\b|连衣裙|裙装/i, tag:"穿搭/裙装", w:7, g:"skirt"},
  {re:/\blace\b|蕾丝/i, tag:"穿搭/蕾丝", w:11},
  {re:/pantyhose|thigh ?highs?|black stocking|黑丝|丝袜/i, tag:"穿搭/黑丝", w:11},
  {re:/white stocking|白丝/i, tag:"穿搭/白丝", w:12},
  {re:/swimsuit|bikini|泳衣|泳装/i, tag:"穿搭/泳装", w:12, g:"swim"},
  {re:/school swimsuit|死库水/i, tag:"穿搭/死库水", w:15, g:"swim"},
  {re:/sportswear|yoga|gym|jersey|运动服/i, tag:"穿搭/运动装", w:10},
  {re:/hoodie|卫衣/i, tag:"穿搭/卫衣", w:10},
  {re:/sweater|knitwear|毛衣/i, tag:"穿搭/毛衣", w:10},
  {re:/\bshirt\b|衬衫/i, tag:"穿搭/衬衫", w:9},
  {re:/\bjacket\b|blazer|外套/i, tag:"穿搭/外套", w:9},
  {re:/\bcoat\b|大衣/i, tag:"穿搭/大衣", w:9},
  {re:/jeans|denim|牛仔/i, tag:"穿搭/牛仔", w:9},
  {re:/\bshorts\b|短裤/i, tag:"穿搭/短裤", w:9},
  {re:/lingerie|underwear|内衣/i, tag:"穿搭/内衣", w:10},
  {re:/\bbra\b|胸罩/i, tag:"穿搭/内衣", w:10},
  {re:/panties|内裤/i, tag:"穿搭/内裤", w:10},
  {re:/pajamas|睡衣/i, tag:"穿搭/睡衣", w:10},
  {re:/\bapron\b|围裙/i, tag:"穿搭/围裙", w:10},
  {re:/\bnurse\b|护士/i, tag:"穿搭/护士装", w:13},
  {re:/\bveil\b|头纱/i, tag:"穿搭/头纱", w:12},
  {re:/\bcrown\b|tiara|头冠|王冠/i, tag:"穿搭/头冠", w:12},
  {re:/\bhat\b|\bcap\b|帽子/i, tag:"穿搭/帽子", w:8},
  {re:/\bribbon\b|headband|发带|发饰/i, tag:"穿搭/发饰", w:9},
  {re:/choker|项圈/i, tag:"穿搭/项圈", w:11},
  {re:/necklace|项链/i, tag:"穿搭/项链", w:11},
  {re:/earrings?|耳环|耳饰/i, tag:"穿搭/耳饰", w:11},
  {re:/\bgloves?\b|手套/i, tag:"穿搭/手套", w:9},
  {re:/\bscarf\b|围巾/i, tag:"穿搭/围巾", w:10},
  {re:/necktie|\btie\b|领带/i, tag:"穿搭/领带", w:10},
  {re:/\bboots?\b|靴子/i, tag:"穿搭/靴子", w:9},
  {re:/high heels|\bheels\b|高跟鞋/i, tag:"穿搭/高跟鞋", w:11},
  {re:/barefoot|赤足/i, tag:"穿搭/赤足", w:9},
  {re:/wet clothes|wet shirt|湿衣/i, tag:"穿搭/湿衣", w:11},
  // ---------- 场景 · 室内 ----------
  {re:/classroom|教室/i, tag:"场景/教室", w:13},
  {re:/\bschool\b(?! ?uniform)|校园/i, tag:"场景/校园", w:6},
  {re:/bedroom|卧室/i, tag:"场景/卧室", w:13},
  {re:/\bbed\b|床上/i, tag:"场景/床", w:8},
  {re:/bathroom|shower|浴室|淋浴/i, tag:"场景/浴室", w:13},
  {re:/bathtub|浴缸/i, tag:"场景/浴缸", w:14},
  {re:/kitchen|厨房/i, tag:"场景/厨房", w:14},
  {re:/library|bookshelf|图书馆|书架/i, tag:"场景/图书馆", w:14},
  {re:/cafe|coffee shop|咖啡馆/i, tag:"场景/咖啡馆", w:15},
  {re:/\boffice\b|workspace|办公室/i, tag:"场景/办公室", w:12},
  {re:/shrine|temple|神社|寺庙/i, tag:"场景/神社寺庙", w:15},
  {re:/castle|城堡/i, tag:"场景/城堡", w:15},
  {re:/rooftop|天台|屋顶/i, tag:"场景/天台", w:13},
  {re:/\btrain\b|subway|车厢|地铁/i, tag:"场景/车厢", w:12},
  {re:/car interior|车内/i, tag:"场景/车内", w:15},
  {re:/\bwindow\b|窗边/i, tag:"场景/窗边", w:9},
  {re:/\bindoor\b|室内/i, tag:"场景/室内", w:5},
  // ---------- 场景 · 室外 / 天气 / 时间 ----------
  {re:/night city|neon city|city at night|night street|street at night|夜景|霓虹街/i, tag:"场景/城市夜景", w:14, g:"night"},
  {re:/\bstreet\b|alley|街道|小巷/i, tag:"场景/街道", w:8},
  {re:/\boutdoor\b|室外|户外/i, tag:"场景/室外", w:5},
  {re:/forest|woods|森林/i, tag:"场景/森林", w:13},
  {re:/garden|flower field|花田|花园/i, tag:"场景/花园", w:11},
  {re:/beach|海边|沙滩/i, tag:"场景/海边", w:12, g:"sea"},
  {re:/\bpool\b|泳池/i, tag:"场景/泳池", w:12},
  {re:/\bsnow(?![- ]?\s*white)|雪景|下雪|雪地/i, tag:"场景/雪景", w:12},
  {re:/\brain\b|rainy|雨天|下雨/i, tag:"场景/雨天", w:12},
  {re:/sunset|dusk|twilight|golden hour|日落|黄昏/i, tag:"场景/日落", w:13},
  {re:/\bnight\b|夜晚|夜里/i, tag:"场景/夜晚", w:6, g:"night"},
  {re:/autumn|maple|秋景|红叶/i, tag:"场景/秋景", w:13},
  {re:/winter|冬景|冬天/i, tag:"场景/冬景", w:12},
  {re:/cherry blossoms?|sakura|樱花/i, tag:"场景/樱花", w:13},
  {re:/underwater|aquarium|水下/i, tag:"场景/水下", w:15},
  {re:/ruins|wasteland|废墟/i, tag:"场景/废墟", w:15},
  {re:/desert|沙漠/i, tag:"场景/沙漠", w:14},
  {re:/\bspace\b|cosmos|nebula|星空|宇宙|太空/i, tag:"场景/星空", w:13},
  {re:/festival|fireworks|祭典|烟花/i, tag:"场景/祭典", w:13},
  {re:/\bmarket\b|市集|集市/i, tag:"场景/市集", w:12},
  {re:/\bbridge\b|大桥|桥/i, tag:"场景/桥", w:13},
  {re:/hallway|corridor|走廊|过道/i, tag:"场景/走廊", w:13},
  {re:/\bsky\b|\bclouds?\b|天空|云/i, tag:"场景/天空", w:6},
  {re:/mountain|山脉|山景/i, tag:"场景/山", w:9},
  {re:/ocean|\bsea\b|大海|海洋/i, tag:"场景/海", w:9, g:"sea"},
  {re:/\blake\b|river|湖泊|湖面|河/i, tag:"场景/湖河", w:9},
  // ---------- 风格 · 画风 / 题材 ----------
  {re:/photorealistic|photo-?realistic|\bdslr\b|raw photo|35mm|摄影写实/i, tag:"风格/摄影写实", w:16},
  {re:/watercolou?r|水彩|水墨淡彩/i, tag:"风格/水彩", w:18},
  {re:/oil painting|油画/i, tag:"风格/油画", w:18},
  {re:/ink wash|sumi-?e|chinese ink|水墨/i, tag:"风格/水墨", w:18},
  {re:/monochrome|gr[ae]yscale|黑白/i, tag:"风格/黑白", w:16},
  {re:/pixel art|像素风|像素/i, tag:"风格/像素", w:18},
  {re:/3d render|\bblender\b|\bcgi\b|octane|3d渲染/i, tag:"风格/3D渲染", w:16},
  {re:/impasto|thick paint|厚涂/i, tag:"风格/厚涂", w:18},
  {re:/sketch|line ?art|pencil|线稿|素描/i, tag:"风格/线稿", w:14},
  {re:/\bcomic\b|\bmanga\b|漫画/i, tag:"风格/漫画", w:12},
  {re:/poster|graphic design|平面设计/i, tag:"风格/平面设计", w:15},
  {re:/cyberpunk|赛博/i, tag:"风格/赛博", w:15},
  {re:/steampunk|蒸汽朋克/i, tag:"风格/蒸汽朋克", w:18},
  {re:/gothic|哥特/i, tag:"风格/哥特", w:14},
  {re:/fantasy|奇幻/i, tag:"风格/奇幻", w:12},
  {re:/sci-?fi|科幻|futuris|未来感/i, tag:"风格/科幻", w:13},
  {re:/traditional chinese|国风|国潮/i, tag:"风格/国风", w:15},
  {re:/ukiyo-?e|浮世绘/i, tag:"风格/浮世绘", w:18},
  {re:/film grain|35mm film|胶片/i, tag:"风格/胶片", w:15},
  {re:/vintage|retro|复古/i, tag:"风格/复古", w:14},
  {re:/minimalis|极简/i, tag:"风格/极简", w:16},
  {re:/anime style|二次元|动漫/i, tag:"风格/二次元", w:5},
  // ---------- 构图 / 视角 ----------
  {re:/from above|overhead|bird'?s eye|俯视|鸟瞰/i, tag:"视角/俯视", w:13},
  {re:/from below|low angle|仰视/i, tag:"视角/仰视", w:13},
  {re:/from behind|back view|背影/i, tag:"视角/背面", w:12},
  {re:/side view|\bprofile\b|侧脸|侧面/i, tag:"视角/侧面", w:11},
  {re:/close-?up|特写/i, tag:"视角/特写", w:12},
  {re:/full body|全身/i, tag:"视角/全身", w:12},
  {re:/upper body|半身以上/i, tag:"视角/半身", w:10},
  {re:/portrait|肖像/i, tag:"视角/肖像", w:7},
  {re:/cowboy shot|七分身/i, tag:"视角/七分身", w:13},
  {re:/feet focus|legs? focus|\bthighs?\b|腿脚|腿部/i, tag:"视角/腿脚", w:12},
  {re:/wide shot|long shot|远景/i, tag:"视角/远景", w:13},
  {re:/dutch angle|倾斜构图/i, tag:"视角/倾斜构图", w:18},
  {re:/\bpov\b|第一人称/i, tag:"视角/第一人称", w:14},
  {re:/looking back|回眸/i, tag:"视角/回眸", w:13},
  {re:/depth of field|\bbokeh\b|景深|虚化/i, tag:"视角/景深", w:9},
];
const SUGGEST_MAX=24;              // 排序后最多留几条（再多没人看，还拖面板）
function suggestTagsFromMeta(it){
  const m=S.curMeta && (S.curMeta.parsed||{});
  const pos = m && m.positive ? m.positive : "";
  if(!pos) return [];
  /* ⚠️ 用 exec 而不是 test：要拿命中位置参与排序（提示词开头的词更可能是主体）。
     排序三档：权重 → 命中位置 → 词表顺序。全部确定，不随浏览器实现抖动。 */
  const hits=new Map();            // tag -> {w, at, i, g}
  for(let i=0;i<SUGGEST_LEXICON.length;i++){
    const lx=SUGGEST_LEXICON[i];
    const mm=lx.re.exec(pos);
    if(!mm) continue;
    const at=mm.index, w=(lx.w==null?10:lx.w), old=hits.get(lx.tag);
    /* 同一标签可有多条规则（「短裙」既认 mini skirt 也认短裙）：
       留权重更高、命中更靠前的那次，别让后面的弱规则盖掉强规则。 */
    if(!old || w>old.w || (w===old.w && at<old.at))
      hits.set(lx.tag, {w, at, i, g:lx.g||""});
  }
  /* 同族只留**最具体**的一条（g 相同者比权重、再比命中位置）：
     穿 short skirt 时不再同时推「裙装」，猫耳不再附带「兽耳」，
     死库水不再附带「泳装」，校服不再附带「制服」，城市夜景不再附带「夜晚」，海边不再附带「海」。
     —— 这一条是「条数变多但不吵」的关键，去掉它平均每条图要多 1.3 个冗余胶囊。 */
  /* ⚠️ best 里存**标签名**，不能存 `for...of` 拿到的 [k,v] 数组：
     那个数组是临时对象，后面 `Array.from(hits.entries())` 又会造一批新数组，
     用 `===` 比引用永远不成立 ⇒ 整族标签会被全部过滤掉（实测：裙装/猫耳/泳装
     在全库一次都没出现）。比名字才是稳定的。 */
  const best=new Map();
  for(const e of hits){
    const g=e[1].g; if(!g) continue;
    const cur=best.get(g), cw=cur?hits.get(cur):null;
    if(!cur || e[1].w>cw.w || (e[1].w===cw.w && e[1].at<cw.at)) best.set(g,e[0]);
  }
  return Array.from(hits.entries())
    .filter(e=>!e[1].g || best.get(e[1].g)===e[0])
    .sort((a,b)=> (b[1].w-a[1].w) || (a[1].at-b[1].at) || (a[1].i-b[1].i))
    .slice(0, SUGGEST_MAX)
    .map(e=>e[0]);
}

/* --- 大图详情面板里加"AI 建议"行(在 renderTagPanel 渲染后调用) --- */
/* 收起时先给 10 条（旧版固定 8 条、无出口），其余折叠成「还有 N 条」。
   展开状态按**图片路径**记忆：翻页即收起，同一张图点掉几个建议不会又缩回去。 */
const TAG_SUG_SHOW=10;
let _tagSugOpen=false, _tagSugPath="";
function renderTagSuggestions(it){
  const panel=$("#tagPanel"); if(!panel) return;
  const sug=$("#tagSugRow"); if(sug) sug.remove();
  if(!it) return;
  if(_tagSugPath!==it.path){ _tagSugPath=it.path; _tagSugOpen=false; }
  const tags=(it.tags||[]).map(x=>x.toLowerCase());
  const cand=suggestTagsFromMeta(it).filter(t=>!tags.includes(t.toLowerCase()));
  if(!cand.length) return;
  const show=_tagSugOpen ? cand : cand.slice(0,TAG_SUG_SHOW);
  const row=document.createElement("div"); row.id="tagSugRow";
  row.style.cssText="margin-top:7px;display:flex;flex-wrap:wrap;gap:4px;align-items:center";
  row.innerHTML='<span style="font-size:calc(10px*var(--fsx));color:var(--tx3);margin-right:2px;display:inline-flex;align-items:center;gap:calc(2px*var(--fsx))">'+ico("sparkle")+' 建议</span>';
  for(const t of show){
    const s=document.createElement("span"); s.className="tagchip sug"; s.style.cssText="border-style:dashed;font-size:calc(10.5px*var(--fsx))";
    s.textContent=t;
    s.onclick=async()=>{ await tagAdd(it, t); renderTagSuggestions(it); };
    row.appendChild(s);
  }
  if(cand.length>TAG_SUG_SHOW){
    const b=document.createElement("span");
    b.className="tagchip sug more"; b.style.cssText="border-style:dashed;font-size:calc(10.5px*var(--fsx));color:var(--tx3);cursor:pointer";
    b.textContent=_tagSugOpen ? "收起" : ("还有 "+(cand.length-TAG_SUG_SHOW)+" 条");
    b.onclick=()=>{ _tagSugOpen=!_tagSugOpen; renderTagSuggestions(it); };
    row.appendChild(b);
  }
  panel.appendChild(row);
}

