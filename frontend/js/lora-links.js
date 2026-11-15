/* Read-only Civitai links. A name remains copyable; only verified matches say C站.
   V2.13.3（2026-11-11）两次调整：
   ① 文案恒为站点字母（`R` / `C`），不再有「搜」前缀 —— 匹配在本地库十几毫秒就命中，
      文案从「搜RED」变「RED」只活 34ms，用户只看到「搜」闪一下没了。
   ② 排版收成一个分段胶囊 `[R│C] ↗`（箭头由 CSS 装饰）：原来两个文字链接常驻
      73.6px、占面板内容宽 23%，现在约 44px。站点全称与状态只写在 title / aria-label。 */
function loraSearchUrl(name, host = 'civitai.com') {
  const base = name.replace(/\\/g, '/').split('/').pop().replace(/\.(safetensors|ckpt|pt|bin)$/i, '');
  return 'https://' + (host === 'civitai.red' ? host : 'civitai.com') + '/search/models?query=' + encodeURIComponent(base).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}

function bindLoraLinks(container, parsed) {
  const links = Array.from(container.querySelectorAll('.lora-civitai'));
  if (!links.length) return;
  const hashes = new Map();
  const key = name => String(name).replace(/\\/g, '/').split('/').pop()
    .replace(/\.(safetensors|ckpt|pt|bin)$/i, '').toLowerCase();
  const raw = parsed && parsed['Lora hashes'];
  if (typeof raw === 'string') {
    for (const part of raw.replace(/^"|"$/g, '').split(',')) {
      const m = /^\s*(.+?)\s*:\s*([a-f\d]{8,64})\s*$/i.exec(part);
      if (m) hashes.set(key(m[1]), m[2]);
    }
  }
  links.forEach(link => {
    link.href = loraSearchUrl(link.dataset.name, link.dataset.host);
    link.onclick = async event => {
      if (window.self === window.top) return;
      event.preventDefault();
      try {
        await api('/api/lora-links/open', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url: link.href})});
      } catch (error) {
        // Preserve a usable fallback when the OS cannot launch its browser.
        copyText(link.href, link);
        link.title = '打开浏览器失败，链接已复制：' + error.message;
      }
    };
  });
  const items = [...new Set(links.map(link => link.dataset.name))].slice(0, 64)
    .map(name => ({name, hash: hashes.get(key(name)) || ''}));
  const labels = {missing: '本机未找到模型，按名称搜索 C 站', ambiguous: '存在同名模型，需在 C 站确认',
    not_found: '未找到对应版本，按名称搜索 C 站', unavailable: 'C 站查询暂不可用，点击搜索',
    changed: '模型文件正在变化，点击搜索', busy: '查询繁忙，点击搜索'};
  let attempts = 0;
  async function update() {
    if (!links[0].isConnected || ++attempts > 120) return;
    try {
      const result = await api('/api/lora-links', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items}), timeoutMs: 15000});
      if (!links[0].isConnected) return;
      let pending = false;
      const results = new Map((result.items || []).map(item => [item.name, item]));
      links.forEach(link => {
        const item = results.get(link.dataset.name);
        if (!item) return;
        const host = link.dataset.host === 'civitai.red' ? 'civitai.red' : 'civitai.com';
        const site = host === 'civitai.red' ? 'RED' : 'COM';
        // 文案与状态解耦：无论匹配成败都写同一个字母，绝不让文字在打开瞬间变短。
        // 值相同时**不写**：每 1.5s 的轮询会重复回填，即使内容一致也会重建文本节点，
        // 既打断选中/朗读焦点，也让"零位移"变成靠肉眼判断的事。
        const text = host === 'civitai.red' ? 'R' : 'C';
        if (link.textContent !== text) link.textContent = text;
        if (item.status === 'matched' && /^https:\/\/civitai\.(com|red)\/(models|model-versions)\/\d+(?:\?modelVersionId=\d+)?$/.test(item.url)) {
          link.href = item.url.replace(/^https:\/\/civitai\.(com|red)/, 'https://' + host);
          link.dataset.state = 'match';
          link.title = 'C 站 ' + site + ' · 打开对应模型版本'
            + (item.source ? '（' + item.source + '）' : '');
        } else {
          link.href = loraSearchUrl(link.dataset.name, host);
          link.dataset.state = 'search';
          link.title = 'C 站 ' + site + ' · '
            + (item.status === 'pending' ? '正在匹配模型，可先按名称搜索'
                                         : (labels[item.status] || '按名称搜索'));
          pending = pending || item.status === 'pending';
        }
        // 字母格只有 1 个字，读屏必须靠 aria-label 才说得清去哪
        if (link.getAttribute('aria-label') !== link.title) {
          link.setAttribute('aria-label', link.title);
        }
      });
      if (pending) setTimeout(update, 1500);
    } catch (_) {
      links.forEach(link => { link.title = '匹配暂不可用，点击按名称搜索 C 站'; });
    }
  }
  update();
}
