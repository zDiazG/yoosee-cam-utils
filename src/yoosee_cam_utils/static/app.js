const grid=document.querySelector('#camera-grid'), status=document.querySelector('#status');
const selectedCamera=document.querySelector('#selected-camera'), autostart=document.querySelector('#autostart');
const ptzDuration=document.querySelector('#ptz-duration'), ptzDurationValue=document.querySelector('#ptz-duration-value');
const cameras=new Map();
let ptzBusy=false;
let editingUser=null;
const AUTO_KEY='yoosee.autostart-full-hd';
async function api(path,data){const options=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Camera-Client':'1'},body:JSON.stringify(data)};const r=await fetch(path,options);const result=await r.json().catch(()=>({}));if(!r.ok){const error=Error(result.error||`Request failed (${r.status}). Check the camera and try again.`);error.status=r.status;throw error;}return result;}
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
  start.disabled=!item.can_control;stop.disabled=!item.can_control;
  controls.append(qualityLabel,start,stop);
  const state=document.createElement('output');state.className='tile-state';state.textContent=item.active?'Stream active':'Stopped';
  const video=document.createElement('video');video.controls=true;video.autoplay=true;video.muted=true;video.playsInline=true;
  const source=document.createElement('p');source.className='source-info';source.textContent='Source: measuring FPS...';
  const latency=document.createElement('p');latency.className='latency';latency.textContent='Player delay from HLS live edge: measuring...';
  const clips=document.createElement('div');clips.className='clip-list';
  if(item.can_control){
    const recordControls=document.createElement('div');recordControls.className='clip-controls';
    const durationLabel=document.createElement('label');durationLabel.textContent='Clip length';
    const duration=document.createElement('select');duration.setAttribute('aria-label',`${item.name||item.id} clip length`);
    for(const seconds of [10,30,60,120,300]){const option=document.createElement('option');option.value=seconds;option.textContent=`${seconds} s`;if(seconds===30)option.selected=true;duration.append(option);}
    durationLabel.append(duration);
    const record=document.createElement('button');record.textContent='Record FHD clip';
    record.onclick=()=>recordClip(camera,duration,record);
    recordControls.append(durationLabel,record);card.append(heading,controls,state,video,source,latency,recordControls,clips);
  }else card.append(heading,controls,state,video,source,latency,clips);
  grid.append(card);
  const camera={...item,card,quality,state,video,source,latency,clips,profiles:[],player:null,timer:null,generation:0};cameras.set(item.id,camera);
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
    if(autostart.checked&&camera.can_control){const preferred=preferredProfile(camera,'fhd');state=await api(cameraPath(item.id,'start'),{profile:preferred});}
    if(state.active){camera.quality.value=state.profile;camera.state.textContent='Stream active';attach(camera,state.playlist);showSource(camera,state.source);}
    else{camera.state.textContent=item.error||'Stopped';showSource(camera,state.source);}
    await loadClips(camera);
  }catch(error){const camera=cameras.get(item.id);camera.state.textContent=error.message;camera.source.textContent='Source: unavailable';}
}
async function loadClips(camera){
  const clips=await api(cameraPath(camera.id,'clips'));camera.clips.replaceChildren();
  for(const clip of clips){const row=document.createElement('div');row.className='clip-row';const link=document.createElement('a');link.href=`/recordings/${encodeURIComponent(camera.id)}/${encodeURIComponent(clip.id)}`;link.textContent=`FHD · ${clip.fps} fps · ${new Date(clip.started_at).toLocaleString()}`;link.download=`${clip.id}.mkv`;row.append(link);if(camera.can_control){const remove=document.createElement('button');remove.textContent='Delete';remove.onclick=async()=>{if(!window.confirm('Delete this saved clip?'))return;remove.disabled=true;try{await api(`${cameraPath(camera.id,'clips')}/${encodeURIComponent(clip.id)}/delete`,{});await loadClips(camera);show(`${camera.name||camera.id}: saved clip deleted.`);}catch(error){show(error.message);remove.disabled=false;}};row.append(remove);}camera.clips.append(row);}
}
async function recordClip(camera,duration,button){
  const profile=preferredProfile(camera,'fhd');if(!profile){show(`${camera.name||camera.id}: no Full HD profile is available.`);return;}
  button.disabled=true;show(`${camera.name||camera.id}: recording an FHD clip...`);
  try{
    const job=await api(cameraPath(camera.id,'clips'),{profile,seconds:Number(duration.value)});
    const deadline=Date.now()+Number(duration.value)*1000+45000;let result=job;
    while(result.state==='recording'&&Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,1000));result=await api(`${cameraPath(camera.id,'clips')}/${encodeURIComponent(job.id)}`);}
    if(result.state!=='complete')throw Error(result.error||'Clip capture did not finish.');
    await loadClips(camera);show(`${camera.name||camera.id}: FHD clip saved locally (${result.result.bytes} bytes, SHA-256 recorded).`);
  }catch(error){show(`${camera.name||camera.id}: ${error.message}`);}
  finally{button.disabled=false;}
}
document.querySelector('#apply-action').onclick=async()=>{
  const action=document.querySelector('#bulk-action').value;if(!action)return;
  const controllable=[...cameras.values()].filter(camera=>camera.can_control);if(!controllable.length){show('No camera control permission is available.');return;}
  show(`${action==='start'?'Starting':'Stopping'} configured cameras...`);
  const results=await Promise.all(controllable.map(camera=>action==='start'?startCamera(camera,camera.quality.value||preferredProfile(camera,'fhd')):stopCamera(camera)));
  show(`${results.filter(Boolean).length}/${results.length} cameras ${action==='start'?'started':'stopped'}. Check each tile for errors.`);
};
document.querySelector('#apply-quality').onclick=async()=>{
  const mode=document.querySelector('#bulk-quality').value;const controllable=[...cameras.values()].filter(camera=>camera.can_control);if(!controllable.length){show('No camera control permission is available.');return;}show('Applying quality to configured cameras...');
  const results=await Promise.all(controllable.map(camera=>{const profile=preferredProfile(camera,mode);camera.quality.value=profile;return startCamera(camera,profile);}));
  show(`${results.filter(Boolean).length}/${results.length} cameras changed quality. Check each tile for errors.`);
};
autostart.checked=localStorage.getItem(AUTO_KEY)==='true';
autostart.onchange=()=>localStorage.setItem(AUTO_KEY,String(autostart.checked));
ptzDuration.value=localStorage.getItem('yoosee.ptz-duration')||'0.8';ptzDurationValue.textContent=`${Number(ptzDuration.value).toFixed(1)} s`;
ptzDuration.oninput=()=>{ptzDurationValue.textContent=`${Number(ptzDuration.value).toFixed(1)} s`;localStorage.setItem('yoosee.ptz-duration',ptzDuration.value);};
document.querySelectorAll('[data-pan]').forEach(button=>button.onclick=async()=>{if(ptzBusy)return;ptzBusy=true;const id=selectedCamera.value;show(`Sending PTZ command to ${cameras.get(id)?.name||id}...`);try{await api(cameraPath(id,'ptz'),{pan:Number(button.dataset.pan),tilt:Number(button.dataset.tilt),seconds:Number(ptzDuration.value)});show('PTZ movement command sent and stopped.');}catch(error){show(error.message);}finally{ptzBusy=false;}});
document.querySelector('#ptz-stop').onclick=async()=>{if(ptzBusy)return;ptzBusy=true;const id=selectedCamera.value;try{await api(cameraPath(id,'ptz'),{stop:true});show('PTZ stop request accepted.');}catch(error){show(error.message);}finally{ptzBusy=false;}};
function updatePtzAccess(){const camera=cameras.get(selectedCamera.value);for(const control of document.querySelectorAll('[data-pan],#ptz-stop'))control.disabled=!camera?.can_control;}
selectedCamera.onchange=updatePtzAccess;
(async function setupAdmin(){
  const panel=document.querySelector('#admin-panel');
  try{
    const users=await api('/api/admin/users'),networks=await api('/api/admin/networks');panel.hidden=false;
    const list=document.querySelector('#user-list');list.replaceChildren();
    for(const user of users){
      const item=document.createElement('div');item.className='admin-item';
      const label=document.createElement('span');label.textContent=`${user.username} · ${user.role} · ${user.enabled?'enabled':'disabled'}`;
      item.append(label);
      const edit=document.createElement('button');edit.textContent='Edit access';edit.onclick=()=>{editingUser=user.username;document.querySelector('#new-username').value=user.username;document.querySelector('#new-username').disabled=true;document.querySelector('#new-role').value=user.role;document.querySelector('#new-cameras').value=Object.keys(user.cameras).join(',');document.querySelector('#new-networks').value=user.networks.join(',');document.querySelector('#new-password').disabled=false;document.querySelector('#create-user').textContent='Save access';document.querySelector('#cancel-edit').hidden=false;};item.append(edit);
      const toggle=document.createElement('button');toggle.textContent=user.enabled?'Disable':'Enable';toggle.onclick=async()=>{try{await api(`/api/admin/users/${user.enabled?'disable':'enable'}`,{username:user.username});await setupAdmin();show(`${user.enabled?'Disabled':'Enabled'} ${user.username}.`);}catch(error){show(error.message);}};item.append(toggle);
      list.append(item);
    }
    const networkList=document.querySelector('#network-list');networkList.replaceChildren();networkList.className='admin-list';
    for(const cidr of networks){const item=document.createElement('div');item.className='admin-item';const label=document.createElement('span');label.textContent=cidr;const button=document.createElement('button');button.textContent='Remove';button.onclick=async()=>{try{await api('/api/admin/networks/remove',{cidr});await setupAdmin();show(`Removed ${cidr} from the viewer allowlist.`);}catch(error){show(error.message);}};item.append(label,button);networkList.append(item);}
  }catch(error){if(error.status!==403)show(error.message);}
})();
document.querySelector('#create-user').onclick=async()=>{
  const role=document.querySelector('#new-role').value;
  const cameras=document.querySelector('#new-cameras').value.split(',').map(value=>value.trim()).filter(Boolean);
  const networks=document.querySelector('#new-networks').value.split(',').map(value=>value.trim()).filter(Boolean);
  const permission=role==='camera_admin'?'camera_admin':'viewer';
  const camera_permissions=role==='web_admin'?{}:Object.fromEntries(cameras.map(id=>[id,permission]));
  const username=editingUser||document.querySelector('#new-username').value.trim();
  const editing=Boolean(editingUser);
  try{await api(editing?'/api/admin/users/access':'/api/admin/users',{username,password:document.querySelector('#new-password').value,role,camera_permissions,networks});clearUserForm();await setupAdmin();show(editing?'Account access updated.':'Account created.');}
  catch(error){show(error.message);}
};
function clearUserForm(){editingUser=null;document.querySelector('#new-username').value='';document.querySelector('#new-username').disabled=false;document.querySelector('#new-password').value='';document.querySelector('#new-password').disabled=false;document.querySelector('#new-role').value='viewer';document.querySelector('#new-cameras').value='';document.querySelector('#new-networks').value='';document.querySelector('#create-user').textContent='Create account';document.querySelector('#cancel-edit').hidden=true;}
document.querySelector('#cancel-edit').onclick=clearUserForm;
document.querySelector('#add-network').onclick=async()=>{const cidr=document.querySelector('#new-network').value.trim();try{await api('/api/admin/networks/add',{cidr});document.querySelector('#new-network').value='';await setupAdmin();show(`Added ${cidr} to the viewer allowlist.`);}catch(error){show(error.message);}};
(async()=>{try{const inventory=await api('/api/cameras');if(!inventory.length){show('No cameras are configured.');return;}const canControl=inventory.some(item=>item.can_control);for(const id of ['apply-action','apply-quality'])document.getElementById(id).disabled=!canControl;document.querySelector('.check').hidden=!canControl;for(const item of inventory)renderCamera(item);updatePtzAccess();await Promise.all(inventory.map(loadCamera));show(`${inventory.length} configured camera${inventory.length===1?'':'s'} loaded. Source FPS is measured per active stream.`);}catch(error){show(error.message);}})();
