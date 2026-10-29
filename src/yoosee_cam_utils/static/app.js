const grid = document.querySelector('#camera-grid');
const status = document.querySelector('#status');
const autostart = document.querySelector('#autostart');
const cameras = new Map();
let editingUser = null;

const AUTO_KEY = 'yoosee.autostart-full-hd';
const RECORD_LIMIT_KEY = 'yoosee.recording-limit-minutes';

async function api(path, data) {
  const options = data === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Camera-Client': '1'},
    body: JSON.stringify(data),
  };
  const response = await fetch(path, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = Error(result.error || `Request failed (${response.status}). Check the camera and try again.`);
    error.status = response.status;
    throw error;
  }
  return result;
}

function show(message) { status.textContent = message; }
function cameraPath(id, action) { return `/api/cameras/${encodeURIComponent(id)}/${action}`; }
function profileResolution(profile) {
  const video = profile.VideoEncoderConfiguration || {};
  return {width: Number(video.Width) || 0, height: Number(video.Height) || 0};
}
function labelProfile(profile) {
  const {width, height} = profileResolution(profile);
  const type = width >= 1920 ? 'Full HD' : width >= 1280 ? 'HD' : 'Low bandwidth';
  return `${width || '?'} × ${height || '?'} · ${type}`;
}
function detach(camera) {
  if (camera.timer) clearInterval(camera.timer);
  camera.timer = null;
  if (camera.player) camera.player.destroy();
  camera.player = null;
  camera.video.pause();
  camera.video.removeAttribute('src');
  camera.video.load();
  camera.latency.textContent = 'Player delay: measuring…';
}
function attach(camera, playlist) {
  detach(camera);
  const generation = ++camera.generation;
  if (Hls.isSupported()) {
    camera.player = new Hls({enableWorker: false, liveSyncDurationCount: 1, liveMaxLatencyDurationCount: 3, maxLiveSyncPlaybackRate: 1.1, maxBufferLength: 15});
    camera.player.loadSource(playlist);
    camera.player.attachMedia(camera.video);
    camera.timer = setInterval(() => {
      if (generation !== camera.generation || !camera.player) return;
      if (camera.video.paused) {
        camera.latency.textContent = 'Playback paused; delay updates when playing';
        return;
      }
      const delay = camera.player.latency;
      camera.latency.textContent = Number.isFinite(delay) && delay > 0 ? `Player delay: ${delay.toFixed(1)} s behind HLS live edge` : 'Player delay: measuring…';
    }, 1000);
    camera.player.on(Hls.Events.ERROR, (_, event) => {
      if (event.fatal && generation === camera.generation) camera.state.textContent = 'Video failed. Start the stream to retry.';
    });
  } else if (camera.video.canPlayType('application/vnd.apple.mpegurl')) {
    camera.latency.textContent = 'Player delay is unavailable in this browser';
    camera.video.src = playlist;
  } else {
    throw Error('This browser does not support HLS.');
  }
  camera.video.play().catch(() => {});
}

function durationOptions(select) {
  for (const [seconds, label] of [[900, '15 minutes'], [1800, '30 minutes'], [3600, '1 hour'], ['custom', 'Custom for this tab…']]) {
    const option = document.createElement('option');
    option.value = seconds;
    option.textContent = label;
    select.append(option);
  }
  select.value = sessionStorage.getItem(RECORD_LIMIT_KEY) || '3600';
}

function renderCamera(item) {
  const card = document.createElement('article');
  card.className = 'camera-card';
  card.dataset.id = item.id;

  const heading = document.createElement('div');
  heading.className = 'camera-heading';
  const identity = document.createElement('div');
  identity.className = 'camera-identity';
  const title = document.createElement('h2');
  title.textContent = item.name || item.id;
  const state = document.createElement('output');
  state.className = 'tile-state';
  state.textContent = item.active ? 'Stream active' : 'Stream stopped';
  identity.append(title, state);
  heading.append(identity);

  const video = document.createElement('video');
  video.controls = true;
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  const source = document.createElement('p');
  source.className = 'source-info';
  source.textContent = 'Source: measuring…';
  const latency = document.createElement('p');
  latency.className = 'latency';
  latency.textContent = 'Player delay: measuring…';

  const controls = document.createElement('div');
  controls.className = 'stream-controls';
  const qualityLabel = document.createElement('label');
  qualityLabel.textContent = 'Quality';
  const quality = document.createElement('select');
  quality.setAttribute('aria-label', `${item.name || item.id} stream quality`);
  quality.disabled = !item.can_control;
  qualityLabel.append(quality);
  const streamToggle = document.createElement('button');
  streamToggle.type = 'button';
  streamToggle.disabled = !item.can_control;
  const cameraStreamToggle = streamToggle;
  controls.append(qualityLabel, cameraStreamToggle);

  const ptz = document.createElement('details');
  ptz.className = 'ptz-inline';
  ptz.hidden = !item.can_control;
  const ptzSummary = document.createElement('summary');
  ptzSummary.textContent = 'PTZ controls';
  const ptzContent = document.createElement('div');
  ptzContent.className = 'ptz-inline-content';
  const ptzPad = document.createElement('div');
  ptzPad.className = 'ptz-mini-pad';
  ptzPad.setAttribute('aria-label', `${item.name || item.id} pan and tilt controls`);
  const ptzDirections = [
    null, ['0', '0.25', 'Tilt up'], null,
    ['-0.25', '0', 'Pan left'], ['stop', 'stop', 'Stop'], ['0.25', '0', 'Pan right'],
    null, ['0', '-0.25', 'Tilt down'], null,
  ];
  for (const direction of ptzDirections) {
    if (!direction) {
      const spacer = document.createElement('span');
      spacer.setAttribute('aria-hidden', 'true');
      ptzPad.append(spacer);
      continue;
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = direction[2] === 'Stop' ? '■' : ({'Tilt up': '↑', 'Tilt down': '↓', 'Pan left': '←', 'Pan right': '→'}[direction[2]]);
    button.setAttribute('aria-label', direction[2]);
    button.title = direction[2];
    if (direction[2] === 'Stop') button.className = 'ptz-stop';
    button.onclick = () => direction[0] === 'stop' ? sendPtz(camera, 0, 0, 0, true) : sendPtz(camera, Number(direction[0]), Number(direction[1]), Number(ptzDuration.value));
    ptzPad.append(button);
  }
  const ptzDurationLabel = document.createElement('label');
  ptzDurationLabel.className = 'ptz-inline-duration';
  ptzDurationLabel.append(document.createTextNode('Movement per click'));
  const ptzDuration = document.createElement('input');
  ptzDuration.type = 'range';
  ptzDuration.min = '0.2';
  ptzDuration.max = '1';
  ptzDuration.step = '0.1';
  ptzDuration.value = localStorage.getItem(`yoosee.ptz-duration:${item.id}`) || '0.8';
  const ptzDurationValue = document.createElement('output');
  ptzDurationValue.textContent = `${Number(ptzDuration.value).toFixed(1)} s`;
  ptzDurationLabel.append(ptzDuration, ptzDurationValue);
  ptzDuration.oninput = () => {
    ptzDurationValue.textContent = `${Number(ptzDuration.value).toFixed(1)} s`;
    localStorage.setItem(`yoosee.ptz-duration:${item.id}`, ptzDuration.value);
  };
  ptzContent.append(ptzPad, ptzDurationLabel);
  ptz.append(ptzSummary, ptzContent);

  const recording = document.createElement('section');
  recording.className = 'recording-panel';
  recording.hidden = !item.can_control;
  const recordingHeading = document.createElement('div');
  recordingHeading.className = 'recording-heading';
  const recordingTitle = document.createElement('strong');
  recordingTitle.textContent = 'FHD recording';
  const recordingState = document.createElement('output');
  recordingState.className = 'recording-state';
  recordingState.textContent = 'Not recording';
  recordingHeading.append(recordingTitle, recordingState);
  const recordingActions = document.createElement('div');
  recordingActions.className = 'recording-actions';
  const limitLabel = document.createElement('label');
  limitLabel.textContent = 'Auto-stop';
  const limit = document.createElement('select');
  limit.setAttribute('aria-label', `${item.name || item.id} recording auto-stop limit`);
  durationOptions(limit);
  limitLabel.append(limit);
  const customLabel = document.createElement('label');
  customLabel.className = 'custom-limit';
  customLabel.hidden = limit.value !== 'custom';
  customLabel.append(document.createTextNode('Minutes '));
  const customMinutes = document.createElement('input');
  customMinutes.type = 'number';
  customMinutes.min = '1';
  customMinutes.max = '720';
  customMinutes.step = '1';
  customMinutes.value = sessionStorage.getItem(`${RECORD_LIMIT_KEY}:${item.id}`) || '60';
  customMinutes.setAttribute('aria-label', 'Custom recording limit in minutes, maximum 720');
  customLabel.append(customMinutes);
  const record = document.createElement('button');
  record.type = 'button';
  record.textContent = 'Start recording';
  record.className = 'button-record';
  record.disabled = !item.can_control;
  recordingActions.append(limitLabel, customLabel, record);
  const recordingInfo = document.createElement('p');
  recordingInfo.className = 'recording-info';
  recordingInfo.textContent = 'Saved as separate 15-minute clips. The selected limit applies to this recording session.';
  recording.append(recordingHeading, recordingActions, recordingInfo);

  const saved = document.createElement('details');
  saved.className = 'saved-clips';
  const savedHeading = document.createElement('summary');
  savedHeading.textContent = 'Saved clips';
  const clips = document.createElement('div');
  clips.className = 'clip-list';
  saved.append(savedHeading, clips);

  card.append(heading, video, source, latency, controls, ptz, recording, saved);
  grid.append(card);
  const camera = { ...item, card, quality, streamToggle: cameraStreamToggle, state, video, source, latency, ptz, recordingState, recordingInfo, recording, limit, customLabel, customMinutes, record, savedHeading, clips, profiles: [], player: null, timer: null, generation: 0, recordingSession: null, sessionId: sessionStorage.getItem(`yoosee.recording-session:${item.id}`), lastSegmentCount: -1, ptzBusy: false };
  cameras.set(item.id, camera);
  quality.onchange = () => startCamera(camera, quality.value);
  streamToggle.onclick = () => camera.active ? stopCamera(camera) : startCamera(camera, quality.value || preferredProfile(camera, 'fhd'));
  limit.onchange = () => {
    sessionStorage.setItem(RECORD_LIMIT_KEY, limit.value);
    customLabel.hidden = limit.value !== 'custom';
  };
  customMinutes.onchange = () => sessionStorage.setItem(`${RECORD_LIMIT_KEY}:${item.id}`, customMinutes.value);
  record.onclick = () => camera.recordingSession ? stopRecording(camera) : startRecording(camera);
  updateStreamToggle(camera);
}

function updateStreamToggle(camera) {
  const busy = camera.streamBusy;
  camera.streamToggle.textContent = busy ? (camera.active ? 'Stopping…' : 'Starting…') : camera.active ? 'Stop stream' : 'Start stream';
  camera.streamToggle.className = camera.active ? 'button-danger' : 'button-primary';
  camera.streamToggle.disabled = !camera.can_control || busy;
  camera.streamToggle.setAttribute('aria-pressed', String(Boolean(camera.active)));
  updateAllToggle();
}

function updateAllToggle() {
  const button = document.querySelector('#toggle-all');
  const controlled = [...cameras.values()].filter(camera => camera.can_control);
  const activeCount = controlled.filter(camera => camera.active).length;
  button.disabled = controlled.length === 0;
  button.textContent = activeCount === 0 ? 'Start all' : activeCount === controlled.length ? 'Stop all' : 'Stop active';
  button.className = activeCount === 0 ? 'button-primary' : 'button-danger';
  button.disabled = controlled.length === 0 || controlled.some(camera => camera.streamBusy);
}

async function sendPtz(camera, pan, tilt, seconds, stop = false) {
  if (camera.ptzBusy) return;
  camera.ptzBusy = true;
  show(stop ? `Stopping ${camera.name || camera.id} movement…` : `Moving ${camera.name || camera.id}…`);
  for (const control of camera.ptz.querySelectorAll('button')) control.disabled = true;
  try {
    await api(cameraPath(camera.id, 'ptz'), stop ? {stop: true} : {pan, tilt, seconds});
    show(stop ? `${camera.name || camera.id}: PTZ stopped.` : `${camera.name || camera.id}: PTZ movement complete.`);
  } catch (error) {
    show(`${camera.name || camera.id}: ${error.message}`);
  } finally {
    camera.ptzBusy = false;
    for (const control of camera.ptz.querySelectorAll('button')) control.disabled = false;
  }
}

function showSource(camera, source) {
  camera.source.textContent = source && source.fps ? `Source: ${source.width} × ${source.height} at ${source.fps} fps` : 'Source FPS is not available yet';
}

async function startCamera(camera, profile) {
  if (!profile) { camera.state.textContent = 'No stream profile available'; return false; }
  if (camera.streamBusy) return false;
  camera.streamBusy = true;
  updateStreamToggle(camera);
  camera.state.textContent = 'Starting stream…';
  try {
    const result = await api(cameraPath(camera.id, 'start'), {profile});
    camera.quality.value = result.profile;
    camera.state.textContent = 'Stream active';
    camera.active = true;
    attach(camera, result.playlist);
    showSource(camera, result.source);
    return true;
  } catch (error) {
    detach(camera);
    camera.active = false;
    camera.state.textContent = error.message;
    return false;
  } finally {
    camera.streamBusy = false;
    updateStreamToggle(camera);
  }
}

async function stopCamera(camera) {
  if (camera.streamBusy) return false;
  camera.streamBusy = true;
  updateStreamToggle(camera);
  try {
    await api(cameraPath(camera.id, 'stop'), {});
    detach(camera);
    camera.state.textContent = 'Stream stopped';
    camera.active = false;
    camera.source.textContent = 'Source: stopped';
    return true;
  } catch (error) {
    camera.state.textContent = error.message;
    return false;
  } finally {
    camera.streamBusy = false;
    updateStreamToggle(camera);
  }
}

function preferredProfile(camera, mode) {
  if (!camera.profiles.length) return '';
  const sorted = [...camera.profiles].sort((a, b) => profileResolution(a).width - profileResolution(b).width);
  return (mode === 'fhd' ? [...sorted].reverse().find(profile => profileResolution(profile).width >= 1920) : sorted[0])?.token || sorted.at(-1).token;
}

function chosenLimit(camera) {
  if (camera.limit.value !== 'custom') return Number(camera.limit.value);
  const minutes = Number(camera.customMinutes.value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 720) throw Error('Choose a custom limit from 1 to 720 minutes.');
  sessionStorage.setItem(`${RECORD_LIMIT_KEY}:${camera.id}`, String(minutes));
  return minutes * 60;
}

function renderRecordingState(camera, session) {
  camera.recordingSession = session?.state === 'recording' ? session : null;
  const active = Boolean(camera.recordingSession);
  updateRecordingToggle(camera);
  camera.limit.disabled = active;
  camera.customMinutes.disabled = active;
  if (active) {
    const elapsed = Number(session.elapsed_seconds) || 0;
    const remaining = Math.max(0, Number(session.max_seconds) - elapsed);
    const segments = Number(session.segment_count) || 0;
    camera.recordingState.textContent = `Recording · ${formatTime(elapsed)} elapsed`;
    camera.recordingInfo.textContent = `Auto-stops in ${formatTime(remaining)} · ${segments} saved ${segments === 1 ? 'clip' : 'clips'} · 15-minute segments`;
  } else if (session?.state === 'idle') {
    camera.recordingState.textContent = 'Not recording';
  } else {
    camera.recordingState.textContent = session?.state || 'Not recording';
    camera.recordingInfo.textContent = session?.stop_reason || session?.error || 'Saved clips remain available below.';
  }
}

function updateRecordingToggle(camera) {
  const active = Boolean(camera.recordingSession);
  camera.record.disabled = !camera.can_control || camera.recordBusy;
  camera.record.textContent = camera.recordBusy ? (camera.recordAction === 'stop' ? 'Stopping…' : 'Starting…') : active ? 'Stop recording' : 'Start recording';
  camera.record.className = active ? 'button-stop-record' : 'button-record';
  camera.record.setAttribute('aria-pressed', String(active));
}

function formatTime(total) {
  const seconds = Math.max(0, Math.floor(total));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `${minutes}:${String(rest).padStart(2, '0')}`;
}

async function syncRecording(camera) {
  if (!camera.can_control) return;
  try {
    const suffix = camera.sessionId ? `?session_id=${encodeURIComponent(camera.sessionId)}` : '';
    const session = await api(cameraPath(camera.id, 'recording') + suffix);
    const hadActiveSession = Boolean(camera.recordingSession);
    const previousCount = camera.recordingSession?.segment_count;
    if (session.state !== 'idle' && session.id) {
      camera.sessionId = session.id;
      sessionStorage.setItem(`yoosee.recording-session:${camera.id}`, session.id);
    } else if (session.state === 'idle') {
      camera.sessionId = null;
      sessionStorage.removeItem(`yoosee.recording-session:${camera.id}`);
    }
    renderRecordingState(camera, session);
    if ((session.state === 'recording' || hadActiveSession) && session.segment_count !== previousCount) await loadClips(camera);
    if (session.state === 'idle' && hadActiveSession) await loadClips(camera);
  } catch (error) {
    camera.recordingState.textContent = error.message;
  }
}

async function startRecording(camera) {
  const profile = preferredProfile(camera, 'fhd');
  if (!profile) { show(`${camera.name || camera.id}: no Full HD profile is available.`); return; }
  let maxSeconds;
  try { maxSeconds = chosenLimit(camera); } catch (error) { show(error.message); return; }
  if (camera.recordBusy) return;
  camera.recordBusy = true;
  camera.recordAction = 'start';
  updateRecordingToggle(camera);
  show(`${camera.name || camera.id}: starting FHD recording…`);
  try {
    const session = await api(cameraPath(camera.id, 'recording/start'), {profile, max_seconds: maxSeconds});
    camera.sessionId = session.id;
    sessionStorage.setItem(`yoosee.recording-session:${camera.id}`, session.id);
    renderRecordingState(camera, session);
    show(`${camera.name || camera.id}: recording started. Clips are saved every 15 minutes.`);
  } catch (error) {
    camera.record.disabled = false;
    show(`${camera.name || camera.id}: ${error.message}`);
  } finally {
    camera.recordBusy = false;
    updateRecordingToggle(camera);
  }
}

async function stopRecording(camera) {
  const session = camera.recordingSession;
  if (!session) return;
  if (camera.recordBusy) return;
  camera.recordBusy = true;
  camera.recordAction = 'stop';
  updateRecordingToggle(camera);
  try {
    const result = await api(cameraPath(camera.id, 'recording/stop'), {session_id: session.id});
    renderRecordingState(camera, result);
    show(`${camera.name || camera.id}: stopping recording and finalizing the current clip…`);
    window.setTimeout(() => syncRecording(camera), 1000);
  } catch (error) {
    show(`${camera.name || camera.id}: ${error.message}`);
  } finally {
    camera.recordBusy = false;
    updateRecordingToggle(camera);
  }
}

async function loadClips(camera) {
  const clips = await api(cameraPath(camera.id, 'clips'));
  camera.clips.replaceChildren();
  camera.savedHeading.textContent = `Saved clips · ${clips.length}`;
  for (const clip of clips) {
    const row = document.createElement('div');
    row.className = 'clip-row';
    const info = document.createElement('div');
    const link = document.createElement('a');
    link.href = `/recordings/${encodeURIComponent(camera.id)}/${encodeURIComponent(clip.id)}`;
    link.textContent = `${clip.width} × ${clip.height} · ${clip.fps} fps · ${new Date(clip.started_at).toLocaleString()}`;
    link.download = `${clip.id}.mkv`;
    const detail = document.createElement('small');
    detail.textContent = `${formatTime(clip.duration_seconds)} · ${(clip.bytes / 1024 / 1024).toFixed(1)} MiB · segment ${clip.segment_index || 1}`;
    info.append(link, detail);
    row.append(info);
    if (camera.can_control) {
      const remove = document.createElement('button');
      remove.textContent = 'Delete';
      remove.className = 'button-quiet';
      remove.disabled = camera.recordingSession?.id === clip.session_id;
      if (remove.disabled) remove.title = 'This clip belongs to the active recording session.';
      remove.onclick = async () => {
        if (!window.confirm('Delete this saved clip?')) return;
        remove.disabled = true;
        try {
          await api(`${cameraPath(camera.id, 'clips')}/${encodeURIComponent(clip.id)}/delete`, {});
          await loadClips(camera);
          show(`${camera.name || camera.id}: clip deleted.`);
        } catch (error) {
          show(error.message);
          remove.disabled = false;
        }
      };
      row.append(remove);
    }
    camera.clips.append(row);
  }
}

async function loadCamera(item) {
  const camera = cameras.get(item.id);
  try {
    camera.profiles = await api(cameraPath(item.id, 'profiles'));
    for (const profile of camera.profiles) {
      const option = document.createElement('option');
      option.value = profile.token;
      option.textContent = labelProfile(profile);
      camera.quality.append(option);
    }
    let stream = await api(cameraPath(item.id, 'status'));
    if (autostart.checked && camera.can_control) stream = await api(cameraPath(item.id, 'start'), {profile: preferredProfile(camera, 'fhd')});
    if (stream.active) {
      camera.quality.value = stream.profile;
      camera.state.textContent = 'Stream active';
      camera.active = true;
      attach(camera, stream.playlist);
      showSource(camera, stream.source);
    } else {
      camera.active = false;
      camera.state.textContent = item.error || 'Stream stopped';
      showSource(camera, stream.source);
    }
    updateStreamToggle(camera);
    await loadClips(camera);
    await syncRecording(camera);
  } catch (error) {
    camera.state.textContent = error.message;
    camera.source.textContent = 'Source unavailable';
  }
}

async function toggleAllStreams() {
  const controllable = [...cameras.values()].filter(camera => camera.can_control);
  if (!controllable.length) { show('You do not have camera control permission.'); return; }
  const toStop = controllable.filter(camera => camera.active);
  const action = toStop.length ? 'stop' : 'start';
  const targets = toStop.length ? toStop : controllable;
  show(action === 'start' ? 'Starting all streams…' : `Stopping ${targets.length === controllable.length ? 'all' : 'active'} streams…`);
  const results = await Promise.all(targets.map(camera => action === 'start' ? startCamera(camera, camera.quality.value || preferredProfile(camera, 'fhd')) : stopCamera(camera)));
  show(`${results.filter(Boolean).length} of ${targets.length} streams ${action === 'start' ? 'started' : 'stopped'}. Check each camera card for errors.`);
}

async function applyQualityToAll(mode) {
  const controllable = [...cameras.values()].filter(camera => camera.can_control);
  if (!controllable.length) { show('You do not have camera control permission.'); return; }
  show(`Changing all streams to ${mode === 'fhd' ? 'Full HD' : 'the lightest available quality'}…`);
  const results = await Promise.all(controllable.map(camera => {
    const profile = preferredProfile(camera, mode);
    camera.quality.value = profile;
    return startCamera(camera, profile);
  }));
  show(`${results.filter(Boolean).length} of ${results.length} streams changed. Check each camera card for errors.`);
}

document.querySelector('#toggle-all').onclick = toggleAllStreams;
document.querySelector('#all-fhd').onclick = () => applyQualityToAll('fhd');
document.querySelector('#all-light').onclick = () => applyQualityToAll('light');
autostart.checked = localStorage.getItem(AUTO_KEY) === 'true';
autostart.onchange = () => localStorage.setItem(AUTO_KEY, String(autostart.checked));

async function setupAdmin() {
  const panel = document.querySelector('#admin-panel');
  try {
    const [users, networks] = await Promise.all([api('/api/admin/users'), api('/api/admin/networks')]);
    panel.hidden = false;
    const list = document.querySelector('#user-list');
    list.replaceChildren();
    for (const user of users) {
      const item = document.createElement('div');
      item.className = 'admin-item';
      const label = document.createElement('span');
      label.textContent = `${user.username} · ${user.role} · ${user.enabled ? 'enabled' : 'disabled'}`;
      item.append(label);
      const edit = document.createElement('button');
      edit.textContent = 'Edit access';
      edit.onclick = () => {
        editingUser = user.username;
        document.querySelector('#new-username').value = user.username;
        document.querySelector('#new-username').disabled = true;
        document.querySelector('#new-role').value = user.role;
        document.querySelector('#new-cameras').value = Object.keys(user.cameras).join(',');
        document.querySelector('#new-networks').value = user.networks.join(',');
        document.querySelector('#new-password').disabled = false;
        document.querySelector('#create-user').textContent = 'Save access';
        document.querySelector('#cancel-edit').hidden = false;
      };
      item.append(edit);
      const toggle = document.createElement('button');
      toggle.textContent = user.enabled ? 'Disable' : 'Enable';
      toggle.onclick = async () => {
        try {
          await api(`/api/admin/users/${user.enabled ? 'disable' : 'enable'}`, {username: user.username});
          await setupAdmin();
          show(`${user.enabled ? 'Disabled' : 'Enabled'} ${user.username}.`);
        } catch (error) { show(error.message); }
      };
      item.append(toggle);
      list.append(item);
    }
    const networkList = document.querySelector('#network-list');
    networkList.replaceChildren();
    networkList.className = 'admin-list';
    for (const cidr of networks) {
      const item = document.createElement('div');
      item.className = 'admin-item';
      const label = document.createElement('span');
      label.textContent = cidr;
      const button = document.createElement('button');
      button.textContent = 'Remove';
      button.onclick = async () => {
        try { await api('/api/admin/networks/remove', {cidr}); await setupAdmin(); show(`Removed ${cidr} from the viewer allowlist.`); }
        catch (error) { show(error.message); }
      };
      item.append(label, button);
      networkList.append(item);
    }
  } catch (error) {
    if (error.status !== 403) show(error.message);
  }
}

document.querySelector('#create-user').onclick = async () => {
  const role = document.querySelector('#new-role').value;
  const camerasInput = document.querySelector('#new-cameras').value.split(',').map(value => value.trim()).filter(Boolean);
  const networks = document.querySelector('#new-networks').value.split(',').map(value => value.trim()).filter(Boolean);
  const permission = role === 'camera_admin' ? 'camera_admin' : 'viewer';
  const camera_permissions = role === 'web_admin' ? {} : Object.fromEntries(camerasInput.map(id => [id, permission]));
  const username = editingUser || document.querySelector('#new-username').value.trim();
  const editing = Boolean(editingUser);
  try {
    await api(editing ? '/api/admin/users/access' : '/api/admin/users', {username, password: document.querySelector('#new-password').value, role, camera_permissions, networks});
    clearUserForm();
    await setupAdmin();
    show(editing ? 'Account access updated.' : 'Account created.');
  } catch (error) { show(error.message); }
};

function clearUserForm() {
  editingUser = null;
  document.querySelector('#new-username').value = '';
  document.querySelector('#new-username').disabled = false;
  document.querySelector('#new-password').value = '';
  document.querySelector('#new-password').disabled = false;
  document.querySelector('#new-role').value = 'viewer';
  document.querySelector('#new-cameras').value = '';
  document.querySelector('#new-networks').value = '';
  document.querySelector('#create-user').textContent = 'Create account';
  document.querySelector('#cancel-edit').hidden = true;
}

document.querySelector('#cancel-edit').onclick = clearUserForm;
document.querySelector('#add-network').onclick = async () => {
  const cidr = document.querySelector('#new-network').value.trim();
  try { await api('/api/admin/networks/add', {cidr}); document.querySelector('#new-network').value = ''; await setupAdmin(); show(`Added ${cidr} to the viewer allowlist.`); }
  catch (error) { show(error.message); }
};

(async function initialize() {
  try {
    const inventory = await api('/api/cameras');
    if (!inventory.length) { show('No cameras are configured.'); return; }
    const controllable = inventory.some(camera => camera.can_control);
    for (const id of ['all-fhd', 'all-light']) document.getElementById(id).disabled = !controllable;
    document.querySelector('.autostart').hidden = !controllable;
    for (const item of inventory) renderCamera(item);
    updateAllToggle();
    await Promise.all(inventory.map(loadCamera));
    show(`${inventory.length} configured camera${inventory.length === 1 ? '' : 's'} loaded. FPS is measured from the live source.`);
    window.setInterval(() => { for (const camera of cameras.values()) syncRecording(camera); }, 2000);
  } catch (error) { show(error.message); }
})();

setupAdmin();
