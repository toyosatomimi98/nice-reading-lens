/* 手机端：持续回传低分辨率预览帧，由电脑那边决定什么时候要高清图。 */

const $ = (id) => document.getElementById(id);

const state = {
  stream: null,
  running: false,
  busy: false,
  intervalMs: 800,
  probeWidth: 480,
  baseConstraints: null,
  sent: 0,
  wakeLock: null,
  probeCount: 0,
  probeWindow: 0,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function log(text) {
  const li = document.createElement('li');
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  li.textContent = `${t}  ${text}`;
  const list = $('log');
  list.prepend(li);
  while (list.children.length > 40) list.lastChild.remove();
}

function setStatus(text, kind = '') {
  $('status').textContent = text;
  $('dot').className = 'dot ' + kind;
}

async function keepAwake() {
  try {
    if ('wakeLock' in navigator) {
      state.wakeLock = await navigator.wakeLock.request('screen');
    }
  } catch (_) {
    log('屏幕常亮没申请到，记得把自动锁屏关掉');
  }
}

async function start() {
  if (state.running) return;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 15 },
      },
    });
  } catch (err) {
    setStatus('打不开摄像头：' + err.message, 'bad');
    log('getUserMedia 失败：' + err.message);
    return;
  }

  const video = $('video');
  video.srcObject = state.stream;
  await video.play().catch(() => {});
  await sleep(400);

  const track = state.stream.getVideoTracks()[0];
  state.baseConstraints = track.getConstraints();
  state.running = true;
  $('overlay').classList.add('hidden');
  $('shot').disabled = false;
  $('m-res').textContent = `${video.videoWidth}×${video.videoHeight}`;
  setStatus('取景中', 'live');
  log('摄像头已启动，开始按间隔回传预览帧');
  await keepAwake();
  probeLoop();
}

function stop() {
  state.running = false;
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  state.stream = null;
  $('overlay').classList.remove('hidden');
  $('shot').disabled = true;
  setStatus('已停止');
}

/* ---------- 预览帧 ---------- */

function probeCanvas(maxWidth) {
  const video = $('video');
  const vw = video.videoWidth || 640;
  const vh = video.videoHeight || 480;
  const scale = Math.min(1, maxWidth / vw);
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(vw * scale);
  canvas.height = Math.round(vh * scale);
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas;
}

async function probeLoop() {
  if (!state.running) return;
  if (!state.busy) {
    try {
      const canvas = probeCanvas(state.probeWidth);
      const blob = await new Promise((r) => canvas.toBlob(r, 'image/jpeg', 0.55));
      const form = new FormData();
      form.append('frame', blob, 'probe.jpg');
      const res = await fetch('/api/probe', { method: 'POST', body: form });
      const data = await res.json();
      if (typeof data.probe_interval_ms === 'number') state.intervalMs = data.probe_interval_ms;
      if (typeof data.probe_width === 'number') state.probeWidth = data.probe_width;
      $('m-verdict').textContent = data.verdict || '—';
      state.probeCount += 1;
      state.probeWindow += 1;
      if (data.action === 'capture') {
        log('判定：翻页 → 抓一张高清图');
        await captureStill();
      }
    } catch (err) {
      log('预览帧发送失败：' + err.message);
      setStatus('连接断了，正在重试', 'bad');
    }
  }
  setTimeout(probeLoop, state.intervalMs);
}

/* ---------- 高清采集 ---------- */

async function captureStill() {
  if (state.busy) return;
  state.busy = true;
  setStatus('正在采集高清图', 'busy');
  const track = state.stream && state.stream.getVideoTracks()[0];
  const video = $('video');

  try {
    // iOS Safari 平时给的是预览分辨率，拍照前临时把约束抬上去
    if (track) {
      try {
        await track.applyConstraints({
          width: { ideal: 3840 },
          height: { ideal: 2160 },
        });
        await sleep(420);
      } catch (_) {
        log('这台设备不接受更高分辨率，用当前分辨率采集');
      }
    }

    const w = video.videoWidth;
    const h = video.videoHeight;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(video, 0, 0, w, h);
    const blob = await new Promise((r) => canvas.toBlob(r, 'image/jpeg', 0.92));

    const form = new FormData();
    form.append('image', blob, 'page.jpg');
    await fetch('/api/page', { method: 'POST', body: form });
    state.sent += 1;
    $('m-pages').textContent = state.sent;
    $('m-res').textContent = `${w}×${h}`;
    log(`已提交书页 ${w}×${h}，${(blob.size / 1024).toFixed(0)} KB`);
    setStatus('已提交，等电脑那边识别', 'busy');

    if (track && state.baseConstraints) {
      try {
        await track.applyConstraints(state.baseConstraints);
      } catch (_) {}
    }
    await sleep(2500);
    if (state.running) setStatus('取景中', 'live');
  } catch (err) {
    log('采集失败：' + err.message);
    setStatus('采集失败', 'bad');
  } finally {
    state.busy = false;
  }
}

/* ---------- 事件 ---------- */

$('start').addEventListener('click', start);
$('shot').addEventListener('click', () => {
  if (state.running) captureStill();
});

document.addEventListener('visibilitychange', async () => {
  if (document.hidden) {
    log('页面切到后台，采集暂停');
    setStatus('已切到后台');
  } else if (state.running) {
    await keepAwake();
    setStatus('取景中', 'live');
  }
});

setInterval(() => {
  if (!state.running) return;
  $('m-fps').textContent = state.probeWindow + ' 帧/10s';
  state.probeWindow = 0;
}, 10000);

window.addEventListener('error', (e) => log('脚本报错：' + e.message));
