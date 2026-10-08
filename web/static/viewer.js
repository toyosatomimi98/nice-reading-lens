/* 电脑端：所有阅读模式共用同一份识别结果，只是换一种摆法。 */

const $ = (id) => document.getElementById(id);
const main = $('main');

const LABELS = {
  mt_backend: ['翻译后端', 'select', ['ollama', 'openai']],
  model: ['翻译模型', 'text'],
  ollama_url: ['Ollama 地址', 'text'],
  api_base: ['云端接口地址', 'text'],
  api_model: ['云端模型名', 'text'],
  api_key: ['云端 API Key', 'text'],
  temperature: ['采样温度', 'number'],
  num_ctx: ['上下文窗口', 'number'],
  context_chars: ['带入前文长度', 'number'],
  batch_chars: ['单次翻译字符上限', 'number'],
  probe_interval_ms: ['预览帧间隔 ms', 'number'],
  probe_width: ['预览帧宽度 px', 'number'],
  change_jaccard: ['翻页判定阈值（越小越灵）', 'number'],
  stable_jaccard: ['画面稳定阈值', 'number'],
  stable_frames: ['需要连续稳定帧数', 'number'],
  cooldown_ms: ['翻页后静默 ms', 'number'],
  split_mode: ['双页分割', 'select', ['auto', 'on', 'off']],
  split_order: ['左右页顺序', 'select', ['lr', 'rl']],
  rectify: ['自动摆正页面', 'bool'],
  auto_rotate: ['自动转正横屏拍歪的书页', 'bool'],
  reading_mode: ['默认阅读模式', 'select', ['light', 'standard', 'immersive']],
  auto_switch: ['新页自动切换', 'bool'],
  keep_anchor: ['保留阅读位置', 'bool'],
  show_original: ['用未摆正的原图核对', 'bool'],
  immersive_source: ['沉浸模式叠加原文', 'bool'],
  min_confidence: ['识别置信度下限', 'number'],
};

const app = {
  pages: [],
  status: { stage: 'idle', text: '连接中…' },
  settings: {},
  info: {},
  glossary: {},
  currentId: null,
  mode: 'standard',
  ws: null,
  follow: true,
  camera: {},
};

const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])
  );

/* ---------- 数据 ---------- */

async function boot() {
  const [doc, settings, info, glossary] = await Promise.all([
    fetch('/api/doc').then((r) => r.json()),
    fetch('/api/config').then((r) => r.json()),
    fetch('/api/info').then((r) => r.json()),
    fetch('/api/glossary').then((r) => r.json()).catch(() => ({})),
  ]);
  app.pages = doc.pages || [];
  app.status = doc.status || app.status;
  app.camera = doc.camera || {};
  app.settings = settings;
  app.info = info;
  app.glossary = glossary;
  app.mode = settings.reading_mode || 'standard';
  app.currentId = app.pages.length ? app.pages[app.pages.length - 1].id : null;
  syncChrome();
  renderAll();
  connect();
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  app.ws = ws;

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'hello') {
      app.pages = msg.pages || [];
      app.status = msg.status || app.status;
      app.camera = msg.camera || app.camera;
      app.settings = msg.settings || app.settings;
      if (!app.currentId && app.pages.length) app.currentId = lastId();
      renderAll();
    } else if (msg.type === 'page.add') {
      app.pages.push(msg.page);
      const wasLast = app.currentId === (app.pages[app.pages.length - 2] || {}).id;
      if (app.settings.auto_switch && (wasLast || !app.currentId)) {
        app.currentId = msg.page.id;
        app.follow = true;
        renderAll();
      } else {
        renderRail();
        toast(`第 ${msg.page.index} 页已就绪`, () => {
          app.currentId = msg.page.id;
          app.follow = true;
          renderAll();
        });
      }
    } else if (msg.type === 'page.focus') {
      app.currentId = msg.id;
      renderAll();
    } else if (msg.type === 'status') {
      app.status = msg.status;
      renderStatus();
    } else if (msg.type === 'settings') {
      app.settings = msg.settings;
      syncChrome();
    } else if (msg.type === 'camera') {
      app.camera = msg.camera || {};
      renderCamera();
    } else if (msg.type === 'reset') {
      app.pages = [];
      app.currentId = null;
      renderAll();
    }
  };

  ws.onclose = () => setTimeout(connect, 1500);
}

const lastId = () => (app.pages.length ? app.pages[app.pages.length - 1].id : null);
const pageById = (id) => app.pages.find((p) => p.id === id) || null;
const currentPage = () => pageById(app.currentId) || app.pages[app.pages.length - 1] || null;

/* ---------- 外壳 ---------- */

function syncChrome() {
  document.querySelectorAll('#seg button').forEach((b) => {
    b.classList.toggle('on', b.dataset.mode === app.mode);
  });
  $('toggle-source').classList.toggle('on', !!app.settings.show_original);
  renderStatus();
}

function renderStatus() {
  const busy = ['prepare', 'ocr', 'translate', 'capturing', 'load'].includes(app.status.stage);
  const bad = app.status.stage === 'error';
  const pill = $('status');
  pill.className = 'pill' + (busy ? ' busy' : '') + (bad ? ' bad' : app.status.stage === 'ready' ? ' ok' : '');
  $('status-text').textContent = app.status.text || '等待翻页';
}

function renderAll() {
  renderRail();
  renderMain(false);
  syncChrome();
  renderCamera();
}

function renderRail() {
  const thumbs = $('thumbs');
  if (!app.pages.length) {
    thumbs.innerHTML = '<div class="empty">还没有页面</div>';
    return;
  }
  thumbs.innerHTML = app.pages
    .map(
      (p) => `<button class="thumb${p.id === app.currentId ? ' on' : ''}" data-id="${p.id}">
        <img src="/api/page/${p.id}/thumb" alt="第 ${p.index} 页">
      </button>`
    )
    .join('');
  thumbs.querySelectorAll('.thumb').forEach((el) =>
    el.addEventListener('click', () => {
      app.currentId = el.dataset.id;
      app.follow = el.dataset.id === lastId();
      renderAll();
    })
  );
}

/* ---------- 取景面板 ---------- */

function renderCamera() {
  const dot = $('cam-dot');
  if (!dot) return;
  const cam = app.camera || {};
  const online = !!cam.online;
  const img = $('cam-img');

  dot.className = 'dot' + (online ? ' live' : '');
  let label = online ? '取景中' : '未连接';
  if (online && cam.video && cam.video[0]) label += ` ${cam.video[0]}×${cam.video[1]}`;
  if (cam.requested_at) label = '已请求采集…';
  $('cam-text').textContent = label;
  $('cam-shot').disabled = !online;

  if (!online) {
    img.hidden = true;
    img.removeAttribute('src');
  }
}

/* 预览图直接当普通图片轮询，比走 WebSocket 推 base64 省事得多 */
setInterval(() => {
  const img = $('cam-img');
  if (!img || document.hidden) return;
  if (!app.camera || !app.camera.online) return;
  img.hidden = false;
  img.src = `/api/camera/frame?t=${Date.now()}`;
}, 1200);

/* ---------- 正文 ---------- */

function renderMain(keepAnchor) {
  const page = currentPage();
  const anchor = keepAnchor ? captureAnchor() : null;

  if (!page) {
    main.innerHTML = `<div class="empty-state">
      <h2>还没有收到书页</h2>
      <p>手机打开取景页，翻页之后这里会自动出现内容。</p>
      <p>手机端地址：<code>${esc(app.info.capture_url || '')}</code></p>
      <img src="/api/qr" alt="二维码">
    </div>`;
    return;
  }

  const render = { light: lightHtml, standard: standardHtml, immersive: immersiveHtml }[app.mode];
  main.innerHTML = render(page);
  if (app.mode === 'immersive') {
    fitBoxes();
    bindBoxes();
  }
  if (app.mode === 'light') bindLight(page);
  if (anchor) restoreAnchor(anchor);
}

function blockHtml(b, showEnglish) {
  const zh = b.zh ? esc(b.zh) : '<span style="color:var(--muted)">…</span>';
  const en = showEnglish ? `<p class="en">${esc(b.text)}</p>` : '';
  return `<div class="blk ${b.type}" data-block="${b.id}">${en}<p class="zh">${zh}</p></div>`;
}

/* 轻量模式侧栏只放译文：原图就在旁边，不用重复排一遍英文 */
function lightHtml(page) {
  const src = app.settings.show_original ? 'raw' : 'view';
  return `<div class="split">
    <div class="image"><div class="imgwrap">
      <img id="pageimg" src="/api/page/${page.id}/${src}" alt="第 ${page.index} 页">
      <div class="halo" id="halo"></div>
    </div></div>
    <div class="text">${page.blocks.map((b) => blockHtml(b, false)).join('')}</div>
  </div>`;
}

/* 标准模式：按文档顺序把英文和译文重排在一起 */
function standardHtml(page) {
  const meta = page.elapsed_ms
    ? `识别 ${(page.elapsed_ms.ocr / 1000).toFixed(1)}s · 翻译 ${(page.elapsed_ms.translate / 1000).toFixed(1)}s`
    : '';
  return `<div class="scroll"><article class="reader">
    <div class="pagehead"><span>第 ${page.index} 页</span><span>${esc(meta)}</span>
      <span class="grow" style="flex:1"></span>
      ${page.rectified ? '<span>已摆正</span>' : ''}
      ${page.rotated ? '<span>已转正</span>' : ''}
      ${page.split.length === 2 ? `<span>${page.split[0] === 'left' ? '左页在前' : '右页在前'}</span>` : ''}
    </div>
    ${page.blocks.map((b) => blockHtml(b, true)).join('')}
  </article></div>`;
}

function immersiveHtml(page) {
  const w = page.width || 1;
  const h = page.height || 1;
  const src = app.settings.show_original ? 'raw' : 'view';
  const boxes = page.blocks
    .map((b) => {
      if (b.type === 'heading' && !b.zh) return '';
      const [x0, y0, x1, y1] = b.rect;
      const style = [
        `left:${((x0 / w) * 100).toFixed(3)}%`,
        `top:${((y0 / h) * 100).toFixed(3)}%`,
        `width:${(((x1 - x0) / w) * 100).toFixed(3)}%`,
        `height:${(((y1 - y0) / h) * 100).toFixed(3)}%`,
      ].join(';');
      return `<div class="zhbox" data-block="${b.id}" style="${style}" data-en="${esc(b.text)}"><span>${esc(b.zh || '')}</span></div>`;
    })
    .join('');
  return `<div class="scroll"><div class="immersive">
    <div class="imgwrap"><img src="/api/page/${page.id}/${src}" alt="第 ${page.index} 页">${boxes}</div>
  </div></div>`;
}

/* ---------- 沉浸模式：把中文缩到框里 ---------- */

function fitBoxes() {
  const FLOOR = 10; // 再小就读不动了
  main.querySelectorAll('.zhbox').forEach((box) => {
    const span = box.firstElementChild;
    const maxH = box.clientHeight;
    const maxW = box.clientWidth;
    if (!maxH || !maxW || !span.textContent.trim()) {
      box.style.display = 'none';
      return;
    }
    let lo = 5;
    let hi = Math.min(maxH * 1.1, 44);
    for (let i = 0; i < 8; i += 1) {
      const mid = (lo + hi) / 2;
      span.style.fontSize = `${mid}px`;
      if (span.scrollHeight <= maxH + 1 && span.scrollWidth <= maxW + 1) lo = mid;
      else hi = mid;
    }
    if (lo < FLOOR) {
      // 字号顶到下限还是塞不下，就把框往下撑一点，宁可覆盖也别缩成蚂蚁字
      span.style.fontSize = `${FLOOR}px`;
      const needed = Math.min(span.scrollHeight, maxH * 1.5);
      box.style.height = `${Math.max(maxH, needed)}px`;
      lo = FLOOR;
    }
    span.style.fontSize = `${lo.toFixed(1)}px`;
    box.title = '点击查看原文';
  });

}

/* 只在渲染之后绑一次，反复 fitBoxes 不会叠上重复的监听 */
function bindBoxes() {
  main.querySelectorAll('.zhbox').forEach((box) => {
    if (box.dataset.bound) return;
    box.dataset.bound = '1';
    box.addEventListener('click', (event) => {
      event.stopPropagation();
      const previous = main.querySelector('.enbox');
      const same = previous && previous.dataset.for === box.dataset.block;
      if (previous) previous.remove();
      if (same) return;
      const tip = document.createElement('div');
      tip.className = 'enbox';
      tip.dataset.for = box.dataset.block;
      tip.textContent = box.dataset.en;
      tip.style.left = box.style.left;
      tip.style.top = `calc(${box.style.top} + ${box.style.height} + 4px)`;
      box.parentElement.appendChild(tip);
    });
  });
}

/* ---------- 轻量模式：点段落对到图上 ---------- */

function bindLight(page) {
  const img = $('pageimg');
  const halo = $('halo');
  const imagePane = main.querySelector('.image');
  if (!img || !halo) return;

  const place = (block) => {
    const rect = { width: img.clientWidth, height: img.clientHeight };
    const [x0, y0, x1, y1] = block.rect;
    halo.style.left = `${(x0 / page.width) * rect.width}px`;
    halo.style.top = `${(y0 / page.height) * rect.height}px`;
    halo.style.width = `${((x1 - x0) / page.width) * rect.width}px`;
    halo.style.height = `${((y1 - y0) / page.height) * rect.height}px`;
    halo.classList.add('on');
    const target = (y0 / page.height) * rect.height - imagePane.clientHeight * 0.3;
    imagePane.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  };

  main.querySelectorAll('.text .blk').forEach((el) => {
    const block = page.blocks.find((b) => b.id === el.dataset.block);
    if (!block) return;
    el.addEventListener('mouseenter', () => place(block));
    el.addEventListener('click', () => place(block));
    el.addEventListener('mouseleave', () => halo.classList.remove('on'));
  });
}

/* ---------- 阅读位置 ---------- */

function scrollRoot() {
  if (app.mode === 'light') return main.querySelector('.text');
  if (app.mode === 'standard') return main.querySelector('.scroll');
  if (app.mode === 'immersive') return main.querySelector('.scroll');
  return null;
}

function captureAnchor() {
  if (!app.settings.keep_anchor) return null;
  const root = scrollRoot();
  if (!root) return null;
  const rootTop = root.getBoundingClientRect().top;
  for (const el of root.querySelectorAll('[data-block]')) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom > rootTop + 2) {
      return { id: el.dataset.block, offset: rect.top - rootTop };
    }
  }
  return null;
}

function restoreAnchor(anchor) {
  const root = scrollRoot();
  if (!root || !anchor) return;
  const el = root.querySelector(`[data-block="${anchor.id}"]`);
  if (!el) return;
  const delta = el.getBoundingClientRect().top - root.getBoundingClientRect().top - anchor.offset;
  root.scrollTop += delta;
}

/* ---------- 设置抽屉 ---------- */

function renderDrawer() {
  document.querySelectorAll('#seg button').forEach((b) => {
    if (b.dataset.bound) return;
    b.dataset.bound = '1';
    b.addEventListener('click', () => setMode(b.dataset.mode));
  });

  const rows = Object.entries(LABELS)
    .map(([key, [label, kind, options]]) => {
      const value = app.settings[key];
      if (value === undefined) return '';
      if (kind === 'bool') {
        return `<label class="field"><span>${label}</span>
          <input type="checkbox" data-key="${key}" ${value ? 'checked' : ''}></label>`;
      }
      if (kind === 'select') {
        const opts = options
          .map((o) => `<option value="${o}" ${o === value ? 'selected' : ''}>${o}</option>`)
          .join('');
        return `<label class="field"><span>${label}</span>
          <select data-key="${key}">${opts}</select></label>`;
      }
      const inputType = key === 'api_key' ? 'password' : kind;
      return `<label class="field"><span>${label}</span>
        <input type="${inputType}" step="any" data-key="${key}" value="${esc(value)}"></label>`;
    })
    .join('');

  $('drawer').innerHTML = `
    <h3>连接手机</h3>
    <p style="font-size:13px;color:var(--muted);margin:0 0 10px">
      手机和电脑连同一个 Wi-Fi，用 Safari 打开下面这个地址：<br>
      <code style="display:inline-block;margin-top:6px">${esc(app.info.capture_url || '')}</code>
    </p>
    <img src="/api/qr" alt="二维码" style="width:170px;border:1px solid var(--line);border-radius:8px;background:#fff;padding:8px">

    <h3>阅读</h3>
    ${rows}

    <h3>词表（JSON）</h3>
    <p style="font-size:12.5px;color:var(--muted);margin:0 0 6px">固定术语译法，翻译时会优先照这个来。</p>
    <textarea id="glossary" rows="7" style="width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12.5px">${esc(JSON.stringify(app.glossary, null, 2))}</textarea>

    <div class="actions">
      <button id="save-glossary">保存词表</button>
      <button id="upload">手动上传书页</button>
      <button id="reset" style="color:var(--err)">清空会话</button>
    </div>
    <input type="file" id="file" accept="image/*" hidden>
  `;

  $('drawer').querySelectorAll('[data-key]').forEach((el) =>
    el.addEventListener('change', () => {
      const key = el.dataset.key;
      const value = el.type === 'checkbox' ? el.checked : el.value;
      app.settings[key] = el.type === 'checkbox' ? value : el.value;
      if (key === 'reading_mode') setMode(el.value, true);
      send({ type: 'config', patch: { [key]: value } });
      if (['show_original', 'keep_anchor'].includes(key)) renderAll();
    })
  );

  $('save-glossary').addEventListener('click', async () => {
    try {
      const parsed = JSON.parse($('glossary').value || '{}');
      const saved = await fetch('/api/glossary', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      }).then((r) => r.json());
      app.glossary = saved;
      $('glossary').value = JSON.stringify(saved, null, 2);
      toast('词表已保存');
    } catch (err) {
      toast('JSON 写错了：' + err.message);
    }
  });

  $('upload').addEventListener('click', () => $('file').click());
  $('file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('image', file, file.name);
    await fetch('/api/ingest', { method: 'POST', body: form });
    toast('已提交，稍等一下');
    e.target.value = '';
  });

  $('reset').addEventListener('click', () => {
    if (confirm('清空所有已处理的页面？')) send({ type: 'reset' });
  });
}

function send(message) {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) app.ws.send(JSON.stringify(message));
}

function setMode(mode, fromDrawer) {
  app.mode = mode;
  syncChrome();
  renderMain(true);
  if (!fromDrawer) send({ type: 'config', patch: { reading_mode: mode } });
}

/* ---------- 小东西 ---------- */

let toastTimer = null;
function toast(text, action) {
  document.querySelectorAll('.toast').forEach((t) => t.remove());
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text + (action ? '（点这里跳过去）' : '');
  if (action) {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      action();
      el.remove();
    });
  }
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), action ? 9000 : 2600);
}

$('open-drawer').addEventListener('click', () => {
  $('drawer').classList.add('open');
  $('scrim').classList.add('open');
});
$('scrim').addEventListener('click', () => {
  $('drawer').classList.remove('open');
  $('scrim').classList.remove('open');
});
$('toggle-source').addEventListener('click', () => {
  app.settings.show_original = !app.settings.show_original;
  send({ type: 'config', patch: { show_original: app.settings.show_original } });
  syncChrome();
  renderMain(true);
});

$('cam-shot').addEventListener('click', async () => {
  try {
    await fetch('/api/capture', { method: 'POST' });
    toast('已通知手机采集这一页');
  } catch (err) {
    toast('发送失败：' + err.message);
  }
});

main.addEventListener('click', () => {
  const tip = main.querySelector('.enbox');
  if (tip) tip.remove();
});

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    const i = app.pages.findIndex((p) => p.id === app.currentId);
    const next = app.pages[i + (e.key === 'ArrowRight' ? 1 : -1)];
    if (next) {
      app.currentId = next.id;
      app.follow = next.id === lastId();
      renderAll();
    }
  }
});

window.addEventListener('resize', () => {
  if (app.mode === 'immersive') fitBoxes();
});

boot().then(renderDrawer).catch((err) => {
  main.innerHTML = `<div class="empty-state"><h2>连不上后端</h2><p>${esc(err.message)}</p></div>`;
});
