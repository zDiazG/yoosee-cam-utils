// Unattended display: starts every visible camera, then keeps it playing.
// Each camera retries on its own with a growing delay, so a camera or server
// restart recovers without anyone touching the screen.
const grid = document.querySelector('#watch-grid');
const statusLine = document.querySelector('#watch-status');
let csrfToken = '';

async function api(path, data) {
  const options = data === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Camera-Client': '1', 'X-CSRF-Token': csrfToken},
    body: JSON.stringify(data),
  };
  const response = await fetch(path, options);
  const result = await response.json().catch(() => ({}));
  if (response.status === 401) window.location.assign('/login');
  if (!response.ok) throw Object.assign(Error(result.error || `Request failed (${response.status})`), {status: response.status});
  return result;
}

function pixels(profile) {
  const video = profile.VideoEncoderConfiguration || {};
  return (Number(video.Width) || 0) * (Number(video.Height) || 0);
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function watchCamera(item) {
  const tile = document.createElement('figure');
  tile.className = 'watch-tile';
  const video = document.createElement('video');
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  const caption = document.createElement('figcaption');
  caption.textContent = item.name || item.id;
  tile.append(video, caption);
  grid.append(tile);
  let delay = 2000;
  for (;;) {
    let player = null;
    try {
      caption.textContent = `${item.name || item.id} · connecting`;
      const profiles = await api(`/api/cameras/${encodeURIComponent(item.id)}/profiles`);
      const best = profiles.slice().sort((a, b) => pixels(b) - pixels(a))[0];
      const stream = await api(`/api/cameras/${encodeURIComponent(item.id)}/start`, {profile: best && best.token});
      if (!stream.playlist && !stream.live) throw Error('Stream did not start');
      player = attach(video, stream);
      caption.textContent = item.name || item.id;
      delay = 2000;
      await monitor(video);
      throw Error('Playback stalled');
    } catch (error) {
      caption.textContent = `${item.name || item.id} · ${error.message}. Retrying`;
    } finally {
      if (player) player.destroy();
      video.removeAttribute('src');
      video.load();
    }
    await sleep(delay);
    delay = Math.min(delay * 2, 30000);
  }
}

// Same order as the main page: fragmented MP4 over MediaSource first, HLS second.
// hls.js runs without a worker because the page's CSP does not allow blob: workers.
function attach(video, stream) {
  if (stream.live && window.MediaSource && MediaSource.isTypeSupported(stream.mime)) return attachLive(video, stream);
  if (window.Hls && window.Hls.isSupported()) {
    const player = new window.Hls({enableWorker: false, liveSyncDurationCount: 1, liveMaxLatencyDurationCount: 2, maxBufferLength: 4, maxMaxBufferLength: 8, backBufferLength: 4});
    player.on(window.Hls.Events.ERROR, (_event, data) => { if (data.fatal) video.dispatchEvent(new Event('watch-fatal')); });
    player.loadSource(stream.playlist);
    player.attachMedia(video);
    video.play().catch(() => {});
    return player;
  }
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = stream.playlist;
    video.play().catch(() => {});
    return {destroy() {}};
  }
  throw Error('This browser cannot play the stream');
}

function appendTo(buffer, data) {
  return new Promise((resolve, reject) => {
    buffer.addEventListener('updateend', resolve, {once: true});
    buffer.addEventListener('error', () => reject(Error('Video decode failed')), {once: true});
    buffer.appendBuffer(data);
  });
}

function removeFrom(buffer, end) {
  return new Promise(resolve => {
    buffer.addEventListener('updateend', resolve, {once: true});
    buffer.remove(0, end);
  });
}

function attachLive(video, stream) {
  const controller = new AbortController();
  const source = new MediaSource();
  const url = URL.createObjectURL(source);
  video.src = url;
  (async () => {
    await new Promise(resolve => source.addEventListener('sourceopen', resolve, {once: true}));
    source.duration = Infinity;
    const buffer = source.addSourceBuffer(stream.mime);
    const response = await fetch(stream.live, {signal: controller.signal});
    if (!response.ok || !response.body) throw Error(`Live video request failed (${response.status})`);
    const reader = response.body.getReader();
    video.play().catch(() => {});
    for (;;) {
      const {done, value} = await reader.read();
      if (done) throw Error('Live video disconnected');
      await appendTo(buffer, value);
      if (!buffer.buffered.length) continue;
      const end = buffer.buffered.end(buffer.buffered.length - 1);
      // Stay near the live edge; drop old data so memory stays bounded.
      if (end - video.currentTime > 1.5) video.currentTime = end - 0.3;
      if (buffer.buffered.start(0) < end - 10) await removeFrom(buffer, end - 6);
    }
  })().catch(error => {
    if (error.name !== 'AbortError') video.dispatchEvent(new Event('watch-fatal'));
  });
  return {destroy() { controller.abort(); URL.revokeObjectURL(url); }};
}

// Resolves when playback has not advanced for 20 s or hls.js reports a fatal error.
function monitor(video) {
  return new Promise(resolve => {
    let last = -1;
    let still = 0;
    const timer = setInterval(() => {
      if (video.currentTime === last) still += 1; else { still = 0; last = video.currentTime; }
      if (still >= 4) { clearInterval(timer); resolve(); }
    }, 5000);
    video.addEventListener('watch-fatal', () => { clearInterval(timer); resolve(); }, {once: true});
  });
}

(async function main() {
  for (;;) {
    try {
      const session = await api('/api/auth/status');
      csrfToken = session.csrf_token;
      const cameras = await api('/api/cameras');
      statusLine.textContent = cameras.length ? '' : 'No cameras are available for this account.';
      await Promise.all(cameras.map(watchCamera));
      return;
    } catch (error) {
      statusLine.textContent = `${error.message}. Retrying`;
      await sleep(5000);
    }
  }
})();

// Keep the CSRF token fresh; ignore failures, the camera loops retry on their own.
setInterval(() => api('/api/auth/status').then(session => { csrfToken = session.csrf_token; }).catch(() => {}), 600000);
