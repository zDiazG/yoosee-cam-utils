const grid=document.querySelector('#camera-grid'), status=document.querySelector('#status');
const selectedCamera=document.querySelector('#selected-camera'), autostart=document.querySelector('#autostart');
const ptzDuration=document.querySelector('#ptz-duration'), ptzDurationValue=document.querySelector('#ptz-duration-value');
const cameras=new Map();
let ptzBusy=false;
const AUTO_KEY='yoosee.autostart-full-hd';
async function api(path,data){const options=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Camera-Client':'1'},body:JSON.stringify(data)};const r=await fetch(path,options);if(!r.ok)throw Error(`Request failed (${r.status}). Check the camera and try again.`);return r.json();}
function show(text){status.textContent=text;}
function cameraPath(id,action){return `/api/cameras/${encodeURIComponent(id)}/${action}`;}
function profileResolution(profile){const v=profile.VideoEncoderConfiguration||{};return {width:Number(v.Width)||0,height:Number(v.Height)||0};}
function labelProfile(profile){const {width,height}=profileResolution(profile);const kind=width>=1920?'Full HD':width>=1280?'HD':'Low bandwidth';return `${width||'?'}×${height||'?'} · ${kind}`;}
function detach(camera){if(camera.timer){clearInterval(camera.timer);camera.timer=null;}if(camera.player){camera.player.destroy();camera.player=null;}camera.video.pause();camera.video.removeAttribute('src');camera.video.load();camera.latency.textContent='Player delay from HLS live edge: measuring...';}
function attach(camera,playlist){detach(camera);const generation=++camera.generation;if(Hls.isSupported()){camera.player=new Hls({enableWorker:false,liveSyncDurationCount:1,liveMaxLatencyDurationCount:3,maxLiveSyncPlaybackRate:1.1,maxBufferLength:15});camera.player.loadSource(playlist);camera.player.attachMedia(camera.video);camera.timer=setInterval(()=>{if(generation!==camera.generation||!camera.player)return;const delay=camera.player.latency;camera.latency.textContent=Number.isFinite(delay)&&delay>0?`Player delay from HLS live edge: ${delay.toFixed(1)} s`:'Player delay from HLS live edge: measuring...';},500);camera.player.on(Hls.Events.ERROR,(_,d)=>{if(d.fatal&&generation===camera.generation)camera.state.textContent='Video failed. Use Start to retry.';});}else if(camera.video.canPlayType('application/vnd.apple.mpegurl')){camera.latency.textContent='Player delay from HLS live edge: unavailable in this browser';camera.video.src=playlist;}else throw Error('This browser does not support HLS.');camera.video.play().catch(()=>{});}
function renderCamera(item){
  const card=document.createElement('article');card.className='camera-card';card.dataset.id=item.id;
  const heading=document.createElement('div');heading.className='camera-heading';
  const title=document.createElement('h2');title.textContent=item.name||item.id;
  const focus=document.createElement('button');focus.type='button';focus.className='focus-camera';focus.textContent='Use PTZ';focus.onclick=()=>{selectedCamera.value=item.id;};
  heading.append(title,focus);
  const qualityLabel=document.createElement('label');qualityLabel.textContent='Quality ';
  const quality=document.createElement('select');quality.setAttribute('aria-label',`${item.name||item.id} quality`);qualityLabel.append(quality);
  const controls=document.createElement('div');controls.className='tile-controls';
  const start=document.createElement('button');start.textContent='Start / retry';
  const stop=document.createElement('button');stop.textContent='Stop';
  controls.append(qualityLabel,start,stop);
  const state=document.createElement('output');state.className='tile-state';state.textContent=item.active?'Stream active':'Stopped';
  const video=document.createElement('video');video.controls=true;video.autoplay=true;video.muted=true;video.playsInline=true;
  const source=document.createElement('p');source.className='source-info';source.textContent='Source: measuring FPS...';
  const latency=document.createElement('p');latency.className='latency';latency.textContent='Player delay from HLS live edge: measuring...';
  card.append(heading,controls,state,video,source,latency);grid.append(card);
  const camera={...item,card,quality,state,video,source,latency,profiles:[],player:null,timer:null,generation:0};cameras.set(item.id,camera);
  const option=document.createElement('option');option.value=item.id;option.textContent=item.name||item.id;selectedCamera.append(option);
  quality.onchange=()=>startCamera(camera,quality.value);
  start.onclick=()=>startCamera(camera,quality.value);
  stop.onclick=()=>stopCamera(camera);
}
function showSource(camera,source){if(source&&source.fps){camera.source.textContent=`Source: ${source.width}×${source.height} at ${source.fps} fps (measured)`;}else{camera.source.textContent='Source: FPS not available yet';}}
async function startCamera(camera,profile){
  if(!profile){camera.state.textContent='No stream profile is available.';return false;}
  camera.state.textContent='Starting stream...';
  try{const result=await api(cameraPath(camera.id,'start'),{profile});camera.quality.value=result.profile;camera.state.textContent='Stream active';attach(camera,result.playlist);showSource(camera,result.source);return true;}
  catch(error){detach(camera);camera.state.textContent=error.message;return false;}
}
async function stopCamera(camera){
  try{await api(cameraPath(camera.id,'stop'),{});detach(camera);camera.state.textContent='Stopped';camera.source.textContent='Source: stopped';return true;}
  catch(error){camera.state.textContent=error.message;return false;}
}
function preferredProfile(camera,mode){if(!camera.profiles.length)return '';const sorted=[...camera.profiles].sort((a,b)=>profileResolution(a).width-profileResolution(b).width);return (mode==='fhd'?[...sorted].reverse().find(p=>profileResolution(p).width>=1920):sorted[0])?.token||sorted.at(-1).token;}
async function loadCamera(item){
  try{
    const profiles=await api(cameraPath(item.id,'profiles'));const camera=cameras.get(item.id);camera.profiles=profiles;
    for(const profile of profiles){const option=document.createElement('option');option.value=profile.token;option.textContent=labelProfile(profile);camera.quality.append(option);}
    let state=await api(cameraPath(item.id,'status'));
    if(autostart.checked){const preferred=preferredProfile(camera,'fhd');state=await api(cameraPath(item.id,'start'),{profile:preferred});}
    if(state.active){camera.quality.value=state.profile;camera.state.textContent='Stream active';attach(camera,state.playlist);showSource(camera,state.source);}
    else{camera.state.textContent=item.error||'Stopped';showSource(camera,state.source);}
  }catch(error){const camera=cameras.get(item.id);camera.state.textContent=error.message;camera.source.textContent='Source: unavailable';}
}
document.querySelector('#apply-action').onclick=async()=>{
  const action=document.querySelector('#bulk-action').value;if(!action)return;
  show(`${action==='start'?'Starting':'Stopping'} configured cameras...`);
  const results=await Promise.all([...cameras.values()].map(camera=>action==='start'?startCamera(camera,camera.quality.value||preferredProfile(camera,'fhd')):stopCamera(camera)));
  show(`${results.filter(Boolean).length}/${results.length} cameras ${action==='start'?'started':'stopped'}. Check each tile for errors.`);
};
document.querySelector('#apply-quality').onclick=async()=>{
  const mode=document.querySelector('#bulk-quality').value;show('Applying quality to configured cameras...');
  const results=await Promise.all([...cameras.values()].map(camera=>{const profile=preferredProfile(camera,mode);camera.quality.value=profile;return startCamera(camera,profile);}));
  show(`${results.filter(Boolean).length}/${results.length} cameras changed quality. Check each tile for errors.`);
};
autostart.checked=localStorage.getItem(AUTO_KEY)==='true';
autostart.onchange=()=>localStorage.setItem(AUTO_KEY,String(autostart.checked));
ptzDuration.value=localStorage.getItem('yoosee.ptz-duration')||'0.8';ptzDurationValue.textContent=`${Number(ptzDuration.value).toFixed(1)} s`;
ptzDuration.oninput=()=>{ptzDurationValue.textContent=`${Number(ptzDuration.value).toFixed(1)} s`;localStorage.setItem('yoosee.ptz-duration',ptzDuration.value);};
document.querySelectorAll('[data-pan]').forEach(button=>button.onclick=async()=>{if(ptzBusy)return;ptzBusy=true;const id=selectedCamera.value;show(`Sending PTZ command to ${cameras.get(id)?.name||id}...`);try{await api(cameraPath(id,'ptz'),{pan:Number(button.dataset.pan),tilt:Number(button.dataset.tilt),seconds:Number(ptzDuration.value)});show('PTZ movement command sent and stopped.');}catch(error){show(error.message);}finally{ptzBusy=false;}});
document.querySelector('#ptz-stop').onclick=async()=>{if(ptzBusy)return;ptzBusy=true;const id=selectedCamera.value;try{await api(cameraPath(id,'ptz'),{stop:true});show('PTZ stop request accepted.');}catch(error){show(error.message);}finally{ptzBusy=false;}};
(async()=>{try{const inventory=await api('/api/cameras');if(!inventory.length){show('No cameras are configured.');return;}for(const item of inventory)renderCamera(item);await Promise.all(inventory.map(loadCamera));show(`${inventory.length} configured camera${inventory.length===1?'':'s'} loaded. Source FPS is measured per active stream.`);}catch(error){show(error.message);}})();
