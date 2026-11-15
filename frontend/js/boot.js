/* ComfyMediaViewer · js/boot.js（2026-09-09 从 index.html 拆出，加载顺序与原文一致） */
"use strict";
/* 10. 启动 */
refreshTagStats();
/* F1-P2-1（2026-10-19）：这两个都是**不 await 的**异步调用，必须自己挂 catch。
   `loadStats()` 的 try 只包住 `await api()`，它后面的 buildLibNav / buildFavNav /
   JSON.stringify 任一抛错就是一个无人处理的 rejection（旧代码里这个 promise 直接
   被丢掉）⇒ 控制台一行红字，界面静默停更，用户看不出「左栏数字不再更新」是故障。 */
loadStats().catch(e => {
  try{ setBotState("warn", "库统计加载失败：" + String((e && e.message) || e), null, false); }catch(_){}
});
/* 内容安全状态：异步拉一次即可。拿到之前 NSFW_INFO 是 null，界面按「原样显示」处理 ——
   顺序很关键：不能反过来先假设「要保护」再把图藏了，那样一旦接口失败
   用户看到的就是一片空白。拉失败也只记 log，不影响其它任何功能。
   ⚠️ F1-P2-5（2026-10-19）：原来是 `try{ nsfwLoadStatus(true) }catch(e){}` ——
   async 函数的异常走 rejected promise，**这个 catch 永远抓不到**，是死代码，
   只给读者「这里做过错误处理」的假象。改成直接挂 .catch。 */
if(typeof nsfwLoadStatus === "function") nsfwLoadStatus(true).catch(() => {});
$("#tagManage").onclick=openTagManager;
/* 标签匹配方式切换（AND=同时满足 / OR=任一满足），立即按新语义重算当前筛选 */
(function(){
  const b=$("#tagMode"); if(!b) return;
  const sync=()=>{
    b.textContent = TAG_MODE==="or" ? "OR" : TAG_MODE==="not" ? "NOT" : "AND";
    b.classList.toggle("on", TAG_MODE!=="and");
    b.dataset.tip = TAG_MODE==="and" ? "多标签：同时满足（点一下切到 OR）"
            : TAG_MODE==="or"  ? "多标签：任一满足（点一下切到 NOT = 都不含）"
            : "多标签：都不包含 / 排除（点一下回到 AND）";
  };
  sync();
  b.onclick=()=>{
    TAG_MODE = TAG_MODE==="and" ? "or" : TAG_MODE==="or" ? "not" : "and";
    savePref("mv.tagmode", TAG_MODE);
    sync();
    toast(TAG_MODE==="and" ? "标签筛选：同时满足（AND）"
        : TAG_MODE==="or"  ? "标签筛选：任一满足（OR）"
        : "标签筛选：都不包含（NOT）——选中的标签会被排除");
    if(TAG_FILTER.length || TAG_EXCLUDE.length) setTagFilter([...TAG_FILTER]);
  };
})();
$("#bTag").onclick=openBatchTag;
/* 2026-11-15：恢复上次浏览的目录，而不是无条件回到根。
   此前这里是写死的 `load("")` ⇒ 在 output/2026-11-14/batch3 翻到第 600 张时界面卡一下
   按 F5，直接掉回根目录第一屏，「刚才看到哪」全靠记忆重找。
   只在普通浏览时写入（app.js 的 load 成功分支）。目录已不存在 / 盘没挂载时，
   load 的 boot 分支会自己回落到根目录并走正常失败提示，不会卡在报错页。
   刻意**不**恢复搜索词与筛选：筛选条件是按目录隔离的（见 load 里 dirChanged 那段），
   开机自动套上一组看不见来源的条件，比回到干净列表更容易让人困惑。 */
(function(){
  let last="";
  try{ last=localStorage.getItem("mv.lastPath")||""; }catch(_){ last=""; }
  load(last, last ? {boot:true} : undefined);
})();
/* 换根迁移报告：自动迁移在后台跑（stats 轮询触发），启动几秒后大概率已完成。
   只在启动时和切根后各查一次 —— /api/migrate_report 取走即清，这里只是补上
   「迁移完成时用户可能不在场」的提示时机。 */
setTimeout(()=>{ try{ mvMigrateReport(); }catch(e){} }, 4000);
