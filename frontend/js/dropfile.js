/* ComfyMediaViewer · js/dropfile.js（2026-11-04 新建）
   外部原图拖拽打开：从资源管理器 / 浏览器 / 聊天软件把图片拖进窗口，直接看原图与配方。

   ## 为什么要这一层
   「打开图片」按钮（V2.10.7）已经把「库外图片 -> 临时副本 -> 单图模式」跑通了，
   拖拽只是换了个入口：把 dataTransfer.files 交给 uploadSoloMany 即可。
   后端零改动 —— /api/solo 会按内容哈希落进数据目录的 solo/，
   inside_root() 本来就放行这个目录，/api/file、/api/meta 照常服务。

   ## 两条实测出来的硬约束
   1. **dragover 必须 preventDefault**。不写这一行，浏览器认为这里不是合法放置目标，
      根本不会派发 drop（实测 drop 计数恒为 0）。这是规范行为，不是兼容性问题。
   2. **drop 也必须 preventDefault**，且要无条件打 —— 拖进来的若不是图片，
      浏览器默认会把它当作导航目标，整个应用页面会被替换掉，只能重启。

   ## 桌面版（Tauri）为什么能收到
   wry 在有 drag_drop_handler 时会 `SetAllowExternalDrop(false)`，把 WebView2 的
   HTML5 拖放整个关掉。所以 tauri.conf.json 里把 windows[0].dragDropEnabled
   设为 false —— 官方注释原话：「Disabling it is required to use HTML5 drag and
   drop on the frontend on Windows」。配置项编译进壳，改完要重新构建桌面壳。
*/
(function(){
"use strict";

/* 与 viewer.js 的 SOLO_BATCH_MAX 同源：这里是提示文案用的，真正的截断在 uploadSoloMany。 */
const BATCH_MAX = 20;

/* ---------- 页内图片来源标记（2026-11-09 · B 方案） ----------
   网格卡片 / 列表行里的缩略图是裸 <img>，浏览器默认可拖。拖起时浏览器会造一个
   「虚拟 File」（实测：size=0、名字 thumb、无扩展名），而旧逻辑只判 types 含 Files，
   于是把软件自己的图当成「从资源管理器拖进来的原图」—— 亮起「松手打开原图」，
   松手后读不到数据（0 字节发不出去 / 后端 411）。

   做法：页内图片 dragstart 时打一个私有 MIME 当来源标记，drop 端见到它就按
   **内部操作**处理：不亮遮罩、不读取、不发请求。

   ★ 为什么不直接禁掉 draggable：用户要保留「把图拖到 QQ / 桌面」的能力。
     外部程序只认 Files，多一个私有类型不影响它收图。
   ★ 类型名取 application/x- 前缀（Chromium / WebView2 都放行自定义类型）。 */
const INTERNAL_MIME = "application/x-cmv-item";

/* 本次拖拽是否始于页内图片 —— 给「私有标记被吞掉」留的兜底判据：
   只有在页内发起过的拖拽，才可能把那个虚拟 File 拖回来。 */
let internalSession = false;

/* dragenter / dragleave 是成对但**不对称**的事件流：拖过子元素时浏览器会补发
   leave/enter，直接用布尔量会让遮罩疯狂闪烁。用深度计数，归零才收起。 */
let depth = 0;
/* 看门狗：拖拽期间浏览器会持续（约每 300ms）派发 dragover，每次续期；
   一旦事件流断了（拖到窗口外松手、源窗口被关、Esc 取消），1.5s 后自动收起。
   ★ 为什么需要它：实测 CDP 派发的 dragleave 里 dataTransfer.types 是空的，
     只靠 leave 计数会让遮罩永久挂在屏幕上 —— 真实使用里「拖到一半切走窗口」
     同样会丢 leave。兜底比精确计数更可靠。 */
let watchdog = 0;

function veil(){ return document.getElementById("dropVeil"); }

/* types 判定的统一入口：DOMStringList 走 contains，新式 types 走 includes。
   两个都试，免得在某个 webview 里因为 API 形状不同而永远判 false。 */
function dtHas(dt, type){
  if(!dt || !dt.types) return false;
  const t = dt.types;
  try{
    if(typeof t.includes === "function") return t.includes(type);
    if(typeof t.contains === "function") return t.contains(type);
  }catch(_){}
  return false;
}

function hasFiles(e){
  const dt = e && e.dataTransfer; if(!dt) return false;
  /* 页内图片：虽然也带 Files（浏览器造的虚拟文件），但它不是外部原图。 */
  if(dtHas(dt, INTERNAL_MIME)) return false;
  return dtHas(dt, "Files");
}

/* 只读调试钩子：门禁靠它区分「事件没来」和「来了但没认出来」，不参与任何行为。 */
window.__dropState = function(){
  const v = veil();
  return { depth: depth, hidden: v ? v.hidden : null, types: window.__dropTypes || null,
           internal: internalSession };
};

/* 页内图片拖起 —— 打来源标记（值用卡片路径，取不到就退回元素 src）。
   ★ 只认 IMG / VIDEO：拖选中文字、拖链接不算内部拖拽，别把人家正常的拖放管掉。 */
document.addEventListener("dragstart", e=>{
  const t = e.target;
  if(!t || t.nodeType !== 1) return;                  // 1 = Element
  if(t.tagName !== "IMG" && t.tagName !== "VIDEO") return;
  internalSession = true;
  const card = t.closest ? t.closest(".card[data-p]") : null;
  let val = "";
  try{ val = (card && card.dataset.p) || t.currentSrc || t.src || ""; }catch(_){ }
  /* setData 失败（极少数 webview 禁自定义类型）也不影响兜底：internalSession 已置位。 */
  try{ e.dataTransfer.setData(INTERNAL_MIME, val || "1"); }catch(_){ }
}, true);

document.addEventListener("dragend", ()=>{ internalSession = false; }, true);

function arm(){ clearTimeout(watchdog); watchdog = setTimeout(hide, 1500); }
function show(){ const v=veil(); if(v && v.hidden) v.hidden=false; }
function hide(){
  clearTimeout(watchdog); watchdog = 0;
  const v=veil(); if(v && !v.hidden) v.hidden=true;
  depth = 0;
}

document.addEventListener("dragenter", e=>{
  try{ window.__dropTypes = [].slice.call((e.dataTransfer && e.dataTransfer.types) || []); }catch(_){}
  if(!hasFiles(e)) return;
  e.preventDefault();                 // 不拦的话后续 dragover 会按「不可放置」处理
  depth++;
  show(); arm();
}, true);

document.addEventListener("dragover", e=>{
  try{ window.__dropTypes = [].slice.call((e.dataTransfer && e.dataTransfer.types) || []); }catch(_){}
  if(!hasFiles(e)) return;
  e.preventDefault();                 // ★ 没有这一行就没有 drop 事件（实测）
  if(e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  show(); arm();                      // 拖拽还在继续 -> 续期
}, true);

document.addEventListener("dragleave", e=>{
  /* ← 这里刻意**不判** hasFiles：dragleave 的 dataTransfer 往往不带 types
     （实测 CDP 直接给空），判了就会「进得去、出不来」。 */
  if(depth <= 0) return;
  depth = Math.max(0, depth - 1);
  if(depth === 0) hide();
}, true);

document.addEventListener("drop", e=>{
  const dt = e.dataTransfer;
  const files = Array.from((dt && dt.files) || []);
  /* 软件自己的图被拖回窗口 —— 不处理。
     判据两条：
       ① 私有 MIME（正常情况，dragstart 打的）；
       ② 兜底：本次拖拽始于页内图片，且 files 全是「size=0 + 无扩展名」的虚拟文件
          （标记被某个 webview 吞掉时走这条）。
     ★ ② 必须限定 internalSession：从资源管理器**拖文件夹**过来也长这样（伪 File），
       那种要照旧交给 uploadSoloMany 提示「不是可打开的图片」，不能静默吞掉。 */
  const allVirtual = files.length > 0 && files.every(
    f => !f.size && !/\.[A-Za-z0-9]{2,5}$/.test(f.name || ""));
  const internal = dtHas(dt, INTERNAL_MIME) || (internalSession && allVirtual);
  internalSession = false;
  if(internal){ e.preventDefault(); hide(); return; }

  /* 只有「真的拖了文件」才拦。
     ★ 为什么不是无条件拦：drop 的默认行为不止「导航」，还包括「把选中的文字/
       链接放进输入框」。全拦会把搜索框拖字这种正常操作用坏。
     但也必须拦住文件 —— 否则 webview 会拿文件去导航，整个应用被替换成一张图，
     只能重启。所以判据取「types 含 Files」或「files 非空」，两者任一即拦。 */
  const isFile = hasFiles(e) || files.length > 0;
  if(!isFile){ hide(); return; }
  e.preventDefault();
  hide();
  if(!dt) return;
  /* 拖的是文件夹时 files 里会有伪 File（size=0、无扩展名），uploadSoloMany
     会把它归到 skipped 并明确提示，不在这里静默丢弃。 */
  if(!files.length) return;
  if(typeof uploadSoloMany !== "function"){
    if(typeof toast === "function") toast("当前版本不支持拖拽打开","err");
    return;
  }
  uploadSoloMany(files);
}, true);

/* 拖到窗口外松手：drop 不会落在文档上，dragleave 也未必补发 —— 遮罩会一直挂着。
   拖拽被取消（Esc / 源窗口关闭）时补一次收尾。 */
window.addEventListener("blur", hide);
document.addEventListener("dragend", hide);
window.addEventListener("keydown", e=>{ if(e.key === "Escape") hide(); }, true);

/* 设计稿里提示过「最多 20 张」，这里把上限带到 UI 上，避免收下再丢。 */
window.__dropBatchMax = BATCH_MAX;
})();
