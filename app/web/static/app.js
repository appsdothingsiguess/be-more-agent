(function(){
"use strict";
var $=function(id){return document.getElementById(id)};
var es=null, backoff=1000, settings={}, live=null, offline=false;
var srvPhase=null, srvMsg="", sending=false, recState=null, playingHere=false;
var sessionMsgs=null;
var indKey="", indSince=0, pending=null, sheetTimer=null;

// ---- helpers ----
function rid(n){
  var a="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", s="";
  var b=new Uint8Array(n);
  if(window.crypto&&crypto.getRandomValues)crypto.getRandomValues(b);else for(var i=0;i<n;i++)b[i]=Math.floor(Math.random()*256);
  for(var j=0;j<n;j++)s+=a.charAt(b[j]%a.length);
  return s;
}
function store(kind,key,val){
  try{
    var st=kind==="s"?window.sessionStorage:window.localStorage;
    if(val===undefined)return st.getItem(key);
    st.setItem(key,val);
  }catch(e){}
  return null;
}
var clientId=store("s","bmo.client");
if(!clientId||!/^[A-Za-z0-9]{8,32}$/.test(clientId)){clientId=rid(16);store("s","bmo.client",clientId)}

function api(method,path,body){
  var o={method:method,credentials:"same-origin",headers:{}};
  if(body!==undefined){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body)}
  return fetch(path,o).then(function(r){
    return r.json().catch(function(){return {}}).then(function(j){
      if(r.status===401&&path!=="/api/login"){showLogin()}
      return {ok:r.ok,status:r.status,data:j};
    });
  });
}
function has(k){return Object.prototype.hasOwnProperty.call(settings,k)}
function showBanner(t){$("bannerText").textContent=t;$("banner").classList.remove("hidden")}

// ---- output choice ----
var OUT={
  device:{speak:true,pi:false,local:true},
  bmo:{speak:true,pi:true,local:false},
  both:{speak:true,pi:true,local:true},
  text:{speak:false,pi:false,local:false}
};
var output=null;
// "Text only" is the Pi's saved mute (ui.text_only), so it also covers voice turns and the
// startup greeting; the other choices only pick where this device's replies play.
function initOutput(s){
  var v=store("l","bmo.output");
  if(v==="text"&&!s["ui.text_only"]&&!store("l","bmo.output.synced")){
    store("l","bmo.output.synced","1");post("ui.text_only",true);   // older page: browser-only mute
    output="text";renderOutput();return;
  }
  store("l","bmo.output.synced","1");
  if(!v||!OUT[v])v="bmo";
  output=v;syncOutput(s);
}
function setOutput(v){
  output=v;store("l","bmo.output",v);renderOutput();
  if(has("ui.text_only")&&!!settings["ui.text_only"]!==(v==="text"))post("ui.text_only",v==="text");
}
function syncOutput(s){
  if(!output||!("ui.text_only" in s))return;
  if(s["ui.text_only"]&&output!=="text"){output="text";store("l","bmo.output","text")}
  else if(!s["ui.text_only"]&&output==="text"){output="bmo";store("l","bmo.output","bmo")}
  renderOutput();
}
function renderOutput(){
  var bs=$("outSeg").querySelectorAll("button");
  for(var i=0;i<bs.length;i++)bs[i].setAttribute("aria-checked",bs[i].getAttribute("data-out")===output?"true":"false");
}

// ---- login / init ----
function showLogin(){
  if(es){es.close();es=null}
  closeSheet();
  $("app").classList.add("hidden");$("login").classList.remove("hidden");$("pin").focus();
}
function showApp(){$("login").classList.add("hidden");$("app").classList.remove("hidden")}
function init(){
  return api("GET","/api/state").then(function(r){
    if(!r.ok)return;
    showApp();applySettings(r.data.settings||{});
    if(!output)initOutput(settings);
    initMic();connect();
  });
}

// ---- settings ----
function num(v){var n=parseFloat(String(v));return isNaN(n)?0:n}
var ROWS={"speaker.volume":"row-vol","sounds.enabled":"row-snd","ui.text_only":"row-mute","wake_word.enabled":"row-wake",
  "listen.followup":"row-follow","listen.followup_seconds":"row-followsec","microphone.gain_db":"row-gain","camera.vision_mode":"row-cam","memory.session_idle_minutes":"row-idle"};
function applySettings(s){
  settings=s||{};
  Object.keys(ROWS).forEach(function(k){$(ROWS[k]).classList.toggle("hidden",!has(k))});
  $("sec-cam").classList.toggle("hidden",!has("camera.vision_mode"));
  $("sec-listen").classList.toggle("hidden",!(has("wake_word.enabled")||has("listen.followup")||has("listen.followup_seconds")||has("microphone.gain_db")));
  if(has("speaker.volume")){$("s-vol").value=num(settings["speaker.volume"]);$("s-vol-v").textContent=num(settings["speaker.volume"])+"%"}
  if(has("ui.text_only"))$("s-mute").checked=!!settings["ui.text_only"];
  if(has("camera.vision_mode"))$("s-cam").value=settings["camera.vision_mode"];
  if(has("sounds.enabled"))$("s-snd").checked=!!settings["sounds.enabled"];
  if(has("microphone.gain_db")){$("s-gain").value=settings["microphone.gain_db"];$("s-gain-v").textContent=settings["microphone.gain_db"]}
  if(has("wake_word.enabled"))$("s-wake").checked=!!settings["wake_word.enabled"];
  if(has("listen.followup"))$("s-follow").checked=!!settings["listen.followup"];
  if(has("listen.followup_seconds")){$("s-followsec").value=num(settings["listen.followup_seconds"]);$("s-followsec-v").textContent=num(settings["listen.followup_seconds"])}
  $("memToggleRow").classList.toggle("hidden",!has("memory.enabled"));
  if(has("memory.enabled"))$("s-mem").checked=!!settings["memory.enabled"];
  if(has("memory.session_idle_minutes")){$("s-idle").value=num(settings["memory.session_idle_minutes"]);$("s-idle-v").textContent=num(settings["memory.session_idle_minutes"])}
  renderSessInfo();
  syncOutput(settings);
}
function post(key,val){
  var b={};b[key]=val;
  api("POST","/api/settings",b).then(function(r){
    if(r.ok){$("setErr").textContent="";applySettings(r.data)}
    else{$("setErr").textContent=(r.data.key?r.data.key+": ":"")+(r.data.error||"Rejected");applySettings(settings)}
  });
}
function bindRange(id,vid,key,conv,suffix){
  $(id).addEventListener("input",function(){$(vid).textContent=this.value+(suffix||"")});
  $(id).addEventListener("change",function(){post(key,conv(this.value))});
}
bindRange("s-vol","s-vol-v","speaker.volume",function(v){return parseInt(v,10)},"%");
bindRange("s-gain","s-gain-v","microphone.gain_db",parseFloat);
bindRange("s-followsec","s-followsec-v","listen.followup_seconds",parseFloat);
function bindCheck(id,key){$(id).addEventListener("change",function(){post(key,this.checked)})}
bindCheck("s-mute","ui.text_only");bindCheck("s-snd","sounds.enabled");bindCheck("s-wake","wake_word.enabled");
bindCheck("s-follow","listen.followup");bindCheck("s-mem","memory.enabled");
$("s-idle").addEventListener("change",function(){post("memory.session_idle_minutes",num(this.value))});
$("s-cam").addEventListener("change",function(){post("camera.vision_mode",this.value)});
$("outSeg").addEventListener("click",function(e){
  var b=e.target.closest?e.target.closest("button"):null;
  if(b)setOutput(b.getAttribute("data-out"));
});

// ---- live chip ----
function modelName(m){
  if(!m)return "";
  return String(m).replace(/\.(onnx|tflite)$/,"").replace(/_v?\d+(\.\d+)*$/,"").replace(/[_-]+/g," ").trim();
}
function renderChip(){
  var chip=$("liveChip"), t="";
  if(offline)t="Server offline";
  else if(live){
    if(live.error)t="Wake word unavailable";
    else if(live.armed)t="Listening for '"+(modelName(live.model)||"wake word")+"'";
    else t="Wake word off";
  }
  chip.textContent=t;chip.classList.toggle("hidden",!t);
  var wm=live&&live.model?"("+modelName(live.model)+")":"";
  $("wakeModel").textContent=wm;
}

// ---- chat ----
function addMsg(who,text,cls){
  var d=document.createElement("div");d.className="msg "+(who==="user"?"user":"bmo")+(cls?" "+cls:"");
  var l=document.createElement("small");l.textContent=who==="user"?"You":"BMO";
  var t=document.createElement("span");t.textContent=text;
  d.appendChild(l);d.appendChild(t);
  var c=$("chat");var near=c.scrollHeight-c.scrollTop-c.clientHeight<80;
  c.insertBefore(d,indicatorEl());
  if(near||who==="user")c.scrollTop=c.scrollHeight;
  return d;
}
var indEl=null;
function indicatorEl(){
  if(!indEl){indEl=document.createElement("div");indEl.className="indicator hidden";indEl.setAttribute("role","status");indEl.setAttribute("aria-live","polite")}
  if(indEl.parentNode!==$("chat"))$("chat").appendChild(indEl);
  return indEl;
}
function resetChat(){
  var c=$("chat");c.textContent="";pending=null;c.appendChild(indicatorEl());
}
function sysLine(text){
  var d=document.createElement("div");d.className="sysline";d.textContent=text;
  $("chat").insertBefore(d,indicatorEl());
}
function onSession(ev){
  resetChat();
  sessionMsgs=0;
  var t="New session started";
  if(ev.consolidated==="ok")t+=" - Saved to long-term memory";
  else if(ev.consolidated==="pending")t+=" - Will save when the server is back";
  sysLine(t);
  renderSessInfo();
}
function onText(ev){
  if(ev.who==="user"&&pending&&(pending.turn==null||pending.turn===ev.turn)){
    var p=pending;pending=null;
    p.el.classList.remove("pending");p.el.lastChild.textContent=ev.text;return;
  }
  addMsg(ev.who,ev.text);
}

// ---- progress indicator ----
function isBusy(){return !!(srvPhase||sending||recState||playingHere)}
function curPhase(){
  if(recState==="recording")return "recording";
  if(recState==="uploading")return "uploading";
  if(playingHere)return "playing_here";
  if(srvPhase&&srvPhase!=="idle"&&srvPhase!=="starting"&&srvPhase!=="error")return srvPhase;
  if(sending)return "thinking";
  return "";
}
var PTEXT={recording:"Listening to you",uploading:"Sending",listening:"BMO is listening",looking:"Looking with the camera",
  thinking:"BMO is thinking",waiting_gpu:"Getting BMO's brain ready (can take a minute)",speaking:"BMO is talking",playing_here:"Playing on this device"};
function renderIndicator(){
  var p=curPhase(), el=indicatorEl();
  var key=p+"|"+(srvMsg||"");
  if(key!==indKey){indKey=key;indSince=Date.now()}
  var stop=$("stop");
  stop.classList.toggle("hidden",!isBusy());
  if(!p){el.classList.add("hidden");el.textContent="";return}
  el.textContent="";
  var t=PTEXT[p]||p;
  if(p==="listening"&&srvMsg&&/follow/i.test(srvMsg))t="BMO is listening for a follow-up";
  var span=document.createElement("span");span.textContent=t;
  el.appendChild(span);
  if(p==="recording"){
    el.appendChild(document.createTextNode("..."));
    var bars=document.createElement("span");bars.className="bars";bars.setAttribute("aria-hidden","true");
    for(var i=0;i<5;i++)bars.appendChild(document.createElement("i"));
    el.appendChild(bars);levelEls=bars.children;
  }else{
    levelEls=null;
    if(p==="thinking"){
      var d=document.createElement("span");d.className="dots";d.setAttribute("aria-hidden","true");
      d.innerHTML="<span>.</span><span>.</span><span>.</span>";el.appendChild(d);
    }else if(p!=="speaking"&&p!=="playing_here")el.appendChild(document.createTextNode("..."));
  }
  if((p!=="recording")&&Date.now()-indSince>90000)el.appendChild(document.createTextNode(" (still working)"));
  el.classList.remove("hidden");
  var c=$("chat");if(c.scrollHeight-c.scrollTop-c.clientHeight<120)c.scrollTop=c.scrollHeight;
}
setInterval(function(){if(curPhase())renderIndicator()},5000);
var levelEls=null;
function setLevel(rms){
  var v=Math.min(1,rms*8);
  var els=levelEls, mb=$("levelBars").children;
  [els,mb].forEach(function(list){
    if(!list)return;
    for(var i=0;i<list.length;i++){
      var h=4+Math.round(18*v*(0.5+0.5*Math.sin(i*1.7+Date.now()/120)));
      list[i].style.height=Math.max(4,h)+"px";
    }
  });
}

// ---- events ----
function onEvent(ev){
  switch(ev.type){
  case "text":onText(ev);break;
  case "session":onSession(ev);break;
  case "error":
    showBanner((ev.message||"Error")+(ev.hint?" - "+ev.hint:""));
    srvPhase=null;sending=false;renderIndicator();break;
  case "notice":addMsg("bmo",(ev.message||"")+(ev.hint?" "+ev.hint:""));break;
  case "phase":
    sending=false;
    if(ev.phase==="idle"||ev.phase==="error"||ev.phase==="starting")srvPhase=null;else srvPhase=ev.phase;
    srvMsg=ev.message||"";renderIndicator();break;
  case "turn_done":
    sending=false;srvPhase=null;renderIndicator();break;
  case "audio":
    if(ev.client_id===clientId&&output&&OUT[output].local)playReply(ev.url);
    break;
  case "live":live=ev;renderChip();break;
  case "setting":
    if(ev.key){var s={};Object.keys(settings).forEach(function(k){s[k]=settings[k]});s[ev.key]=ev.value;applySettings(s)}
    break;
  }
}
function retry(){
  var wait=backoff;backoff=Math.min(backoff*2,15000);
  setTimeout(function(){
    if(es)return;
    api("GET","/api/state").then(function(r){
      if(r.status===401)return;
      if(r.ok)connect();else retry();
    }).catch(retry);
  },wait);
}
function connect(){
  if(es)es.close();
  resetChat();srvPhase=null;srvMsg="";sending=false;renderIndicator();
  es=new EventSource("/api/events");
  es.onopen=function(){backoff=1000;offline=false;$("recon").classList.add("hidden");renderChip()};
  es.onmessage=function(m){try{onEvent(JSON.parse(m.data))}catch(e){}};
  es.onerror=function(){
    offline=true;renderChip();
    $("recon").classList.remove("hidden");
    es.close();es=null;
    retry();
  };
}

// ---- reply playback ----
var actx=null, curSrc=null, playSeq=0;
function ctx(){
  if(!actx){var C=window.AudioContext||window.webkitAudioContext;if(C)actx=new C()}
  return actx;
}
function unlock(){var c=ctx();if(c&&c.state==="suspended")c.resume()}
["pointerdown","keydown","touchend"].forEach(function(n){document.addEventListener(n,unlock,{passive:true})});
function stopLocal(){
  playSeq++;
  if(curSrc){try{curSrc.onended=null;curSrc.stop()}catch(e){}curSrc=null}
  if(playingHere){playingHere=false;renderIndicator()}
}
function playReply(url){
  var c=ctx();if(!c)return;
  stopLocal();
  var seq=playSeq;
  playingHere=true;renderIndicator();
  fetch(url,{credentials:"same-origin"}).then(function(r){
    if(!r.ok)throw new Error("audio "+r.status);return r.arrayBuffer();
  }).then(function(buf){
    return new Promise(function(res,rej){c.decodeAudioData(buf,res,rej)});
  }).then(function(ab){
    if(seq!==playSeq)return;
    var src=c.createBufferSource();src.buffer=ab;src.connect(c.destination);
    src.onended=function(){if(curSrc===src){curSrc=null;playingHere=false;renderIndicator()}};
    curSrc=src;
    if(c.state==="suspended")c.resume();
    src.start();
  }).catch(function(){
    if(seq===playSeq){playingHere=false;renderIndicator()}
  });
}

// ---- sending text ----
function send(){
  var t=$("text").value.trim();if(!t)return;
  var o=OUT[output]||OUT.bmo;
  unlock();
  sending=true;renderIndicator();
  api("POST","/api/message",{text:t,speak:o.speak,play_on_pi:o.pi,client_id:clientId}).then(function(r){
    if(r.ok){$("text").value="";$("sendErr").textContent="";autosize()}
    else{sending=false;renderIndicator();$("sendErr").textContent=r.data.error||"Could not send"}
  });
}
function autosize(){var t=$("text");t.style.height="auto";t.style.height=Math.min(t.scrollHeight,128)+"px"}
$("send").addEventListener("click",send);
$("text").addEventListener("input",autosize);
$("text").addEventListener("keydown",function(e){
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}
});
$("stop").addEventListener("click",function(){
  cancelRec();stopLocal();sending=false;srvPhase=null;renderIndicator();
  api("POST","/api/interrupt",{});
});

// ---- microphone ----
var MAXSEC=30, MINSEC=0.3, SILENCE_MS=1200;
var rec=null;
function micSupported(){
  return window.isSecureContext&&navigator.mediaDevices&&navigator.mediaDevices.getUserMedia;
}
function initMic(){
  var ok=!!micSupported();
  $("mic").disabled=!ok;
  var h=$("micHint");
  if(!ok){
    h.textContent="Open "+"https:"+"//"+location.hostname+":8443 to use the mic";
    h.classList.remove("hidden");
  }else h.classList.add("hidden");
}
function setRecUi(on){
  $("mic").classList.toggle("rec",on);
  $("mic").setAttribute("aria-label",on?"Stop recording":"Start recording");
  $("micIcon").classList.toggle("hidden",on);
  $("levelBars").classList.toggle("hidden",!on);
}
function startRec(){
  if(rec||recState)return;
  unlock();
  $("sendErr").textContent="";
  navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}}).then(function(stream){
    var c=ctx();
    if(!c){stream.getTracks().forEach(function(t){t.stop()});showBanner("Audio is not supported in this browser");return}
    return c.resume().then(function(){return beginCapture(c,stream)});
  }).catch(function(e){
    var name=e&&e.name;
    showBanner(name==="NotAllowedError"?"Microphone permission was denied":"Could not start the microphone"+(e&&e.message?": "+e.message:""));
    rec=null;recState=null;setRecUi(false);renderIndicator();
  });
}
var workletLoaded=false;
function beginCapture(c,stream){
  var r={stream:stream,src:c.createMediaStreamSource(stream),chunks:[],n:0,rate:c.sampleRate,node:null,sink:null,
    floor:0.005,speech:false,loud:0,lastSpeech:0,t0:Date.now(),done:false};
  rec=r;
  r.sink=c.createGain();r.sink.gain.value=0;r.sink.connect(c.destination);
  var onFrame=function(f){
    if(r.done)return;
    r.chunks.push(f);r.n+=f.length;
    var s=0;for(var i=0;i<f.length;i++)s+=f[i]*f[i];
    var rms=Math.sqrt(s/f.length);
    setLevel(rms);
    var now=Date.now();
    var thr=Math.max(0.015,r.floor*3);
    if(rms>thr){
      r.loud++;if(r.loud>=2){r.speech=true}
      if(r.speech)r.lastSpeech=now;
    }else{
      r.loud=0;
      r.floor=r.floor*0.95+Math.min(rms,0.05)*0.05;
      if(r.speech&&now-r.lastSpeech>SILENCE_MS)finishRec();
    }
    if(r.n/r.rate>=MAXSEC)finishRec();
  };
  var p;
  if(c.audioWorklet&&window.AudioWorkletNode){
    p=(workletLoaded?Promise.resolve():c.audioWorklet.addModule("/static/mic-worklet.js").then(function(){workletLoaded=true}))
      .then(function(){
        var n=new AudioWorkletNode(c,"bmo-mic",{numberOfInputs:1,numberOfOutputs:1,channelCount:1});
        n.port.onmessage=function(e){onFrame(e.data)};
        r.src.connect(n);n.connect(r.sink);r.node=n;
      });
  }else p=Promise.reject(new Error("no worklet"));
  return p.catch(function(){
    var n=c.createScriptProcessor(4096,1,1);
    n.onaudioprocess=function(e){onFrame(new Float32Array(e.inputBuffer.getChannelData(0)))};
    r.src.connect(n);n.connect(r.sink);r.node=n;
  }).then(function(){
    recState="recording";setRecUi(true);renderIndicator();
  });
}
function releaseRec(r){
  r.done=true;
  try{if(r.node){if(r.node.port)r.node.port.onmessage=null;r.node.onaudioprocess=null;r.node.disconnect()}}catch(e){}
  try{r.src.disconnect()}catch(e){}
  try{r.sink.disconnect()}catch(e){}
  r.stream.getTracks().forEach(function(t){t.stop()});
  rec=null;setRecUi(false);
}
function cancelRec(){
  if(rec){releaseRec(rec);recState=null;renderIndicator()}
}
function finishRec(){
  var r=rec;if(!r||r.done)return;
  releaseRec(r);
  var secs=r.n/r.rate;
  if(secs<MINSEC||!r.speech){recState=null;renderIndicator();return}
  var all=new Float32Array(r.n), off=0;
  r.chunks.forEach(function(f){all.set(f,off);off+=f.length});
  var wav=encodeWav(resample(all,r.rate,16000),16000);
  upload(wav);
}
function resample(x,from,to){
  if(from===to)return x;
  var ratio=from/to, n=Math.floor(x.length/ratio), out=new Float32Array(n);
  for(var i=0;i<n;i++){
    var a=i*ratio, b=(i+1)*ratio;
    if(ratio>1){ // average the source samples covered by this output sample
      var s=0,k=0;for(var j=Math.floor(a);j<Math.min(Math.ceil(b),x.length);j++){s+=x[j];k++}
      out[i]=k?s/k:0;
    }else{
      var i0=Math.floor(a), fr=a-i0, y0=x[i0], y1=x[Math.min(i0+1,x.length-1)];
      out[i]=y0+(y1-y0)*fr;
    }
  }
  return out;
}
function encodeWav(x,rate){
  var buf=new ArrayBuffer(44+x.length*2), v=new DataView(buf);
  function str(o,s){for(var i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))}
  str(0,"RIFF");v.setUint32(4,36+x.length*2,true);str(8,"WAVE");str(12,"fmt ");
  v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);
  str(36,"data");v.setUint32(40,x.length*2,true);
  for(var i=0;i<x.length;i++){
    var s=Math.max(-1,Math.min(1,x[i]));
    v.setInt16(44+i*2,s<0?s*0x8000:s*0x7fff,true);
  }
  return new Blob([buf],{type:"audio/wav"});
}
function upload(blob){
  var o=OUT[output]||OUT.bmo;
  recState="uploading";renderIndicator();
  var pend={el:addMsg("user","Voice message…","pending"),turn:null};
  pending=pend;
  var q="?speak="+(o.speak?1:0)+"&pi="+(o.pi?1:0)+"&client="+encodeURIComponent(clientId);
  fetch("/api/voice"+q,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"audio/wav"},body:blob}).then(function(r){
    if(r.status===401){showLogin()}
    return r.json().catch(function(){return {}}).then(function(j){return {ok:r.ok,data:j}});
  }).then(function(r){
    recState=null;
    if(r.ok){
      if(pending===pend)pend.turn=r.data.turn;
      sending=true; // until the server's phase event arrives
    }else{
      if(pend.el.parentNode)pend.el.parentNode.removeChild(pend.el);
      if(pending===pend)pending=null;
      showBanner(r.data.error||"Could not send voice message");
    }
    renderIndicator();
  }).catch(function(){
    recState=null;
    if(pend.el.parentNode)pend.el.parentNode.removeChild(pend.el);
    if(pending===pend)pending=null;
    showBanner("Could not send voice message");renderIndicator();
  });
}
$("mic").addEventListener("click",function(){
  if(rec)finishRec();else if(!recState)startRec();
});

// ---- banner / login / logout ----
$("bannerX").addEventListener("click",function(){$("banner").classList.add("hidden")});
$("loginForm").addEventListener("submit",function(e){
  e.preventDefault();
  api("POST","/api/login",{pin:$("pin").value}).then(function(r){
    if(r.ok){$("pin").value="";$("loginErr").textContent="";init()}
    else $("loginErr").textContent=r.data.error||"Login failed";
  });
});
$("logout").addEventListener("click",function(){cancelRec();stopLocal();api("POST","/api/logout",{}).then(showLogin)});

// ---- memory ----
function loadMemories(){
  return api("GET","/api/memories").then(function(r){
    if(!r.ok){$("memErr").textContent=r.data.error||"Could not load memory";return}
    $("memErr").textContent="";
    var conv=r.data.conversation||[], lt=r.data.long_term||{}, list=$("memList");
    var n=conv.length;
    $("convCount").textContent="This conversation: "+n+" message"+(n===1?"":"s");
    if(r.data.session&&typeof r.data.session.messages==="number")sessionMsgs=r.data.session.messages;
    renderSessInfo();
    list.textContent="";
    var mems=lt.available?(lt.memories||[]):[];
    mems.forEach(function(m){
      var li=document.createElement("li");
      var sp=document.createElement("span"),nm=document.createElement("strong");
      nm.textContent=m.name?m.name.replace(/-/g," ")+(m.type?" ("+m.type+")":""):"";
      if(m.name)sp.appendChild(nm);sp.appendChild(document.createTextNode((m.name?" ":"")+(m.content||"")));
      var b=document.createElement("button");b.type="button";b.className="ghost";
      b.textContent="Delete";b.setAttribute("aria-label","Delete memory: "+(m.name||m.content));
      b.addEventListener("click",function(){
        api("POST","/api/memories/delete",m.name?{name:m.name}:{id:m.id}).then(function(d){
          $("memErr").textContent=d.ok?"":(d.data.error||"Could not delete");
          loadMemories();
        });
      });
      li.appendChild(sp);li.appendChild(b);list.appendChild(li);
    });
    $("memEmpty").textContent=!lt.available?"Long-term memory isn't set up on the server yet.":
      (mems.length?"":"BMO doesn't remember anything yet.");
  });
}
function renderSessInfo(){
  var m=has("memory.session_idle_minutes")?num(settings["memory.session_idle_minutes"]):null;
  var t="";
  if(sessionMsgs!==null)t="Session: "+sessionMsgs+" message"+(sessionMsgs===1?"":"s");
  if(m!==null)t+=(t?" \u00b7 ":"")+(m>0?"saves to long-term memory after "+m+" min idle":"idle saving is off");
  $("sessInfo").textContent=t;
}
$("newSession").addEventListener("click",function(){
  var b=this;b.disabled=true;
  api("POST","/api/session/new",{}).then(function(r){
    if(!r.ok)showBanner((r.data&&r.data.error)||"Could not start a new session");
  }).catch(function(){showBanner("Could not start a new session")}).then(function(){b.disabled=false});
});
$("forget").addEventListener("click",function(){
  if(!confirm("Forget everything? BMO will lose this conversation and its long-term memories."))return;
  api("POST","/api/memories/forget",{}).then(function(r){
    var d=r.data||{};
    if(!r.ok||d.server==="error"){
      $("memMsg").textContent="";
      $("memErr").textContent=r.ok?"Forgot this conversation, but the server could not clear long-term memory.":(d.error||"Could not forget");
    }else{
      $("memErr").textContent="";
      $("memMsg").textContent=d.server==="ok"?"Forgot everything.":"Forgot this conversation; long-term memory isn't available yet.";
    }
    loadMemories();
  });
});

// ---- status ----
function refreshServer(){
  api("GET","/api/status").then(function(r){
    if(!r.ok)return;
    var dl=$("serverInfo");dl.textContent="";
    Object.keys(r.data).forEach(function(k){
      var v=r.data[k];if(v===null||v===undefined)v="-";
      if(typeof v==="object")v=JSON.stringify(v);
      var dt=document.createElement("dt");dt.textContent=k;
      var dd=document.createElement("dd");dd.textContent=String(v);
      dl.appendChild(dt);dl.appendChild(dd);
    });
  });
}

// ---- settings sheet ----
function sheetOpen(){return !$("sheet").classList.contains("hidden")}
function openSheet(){
  $("sheet").classList.remove("hidden");$("gear").setAttribute("aria-expanded","true");
  $("sheet").focus();
  loadMemories();refreshServer();
  if(!sheetTimer)sheetTimer=setInterval(refreshServer,10000);
}
function closeSheet(){
  if(!sheetOpen())return;
  $("sheet").classList.add("hidden");$("gear").setAttribute("aria-expanded","false");
  if(sheetTimer){clearInterval(sheetTimer);sheetTimer=null}
  if(!$("app").classList.contains("hidden"))$("gear").focus();
}
$("gear").addEventListener("click",function(){if(sheetOpen())closeSheet();else openSheet()});
$("sheetX").addEventListener("click",closeSheet);
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&sheetOpen())closeSheet()});

init().then(function(){if($("app").classList.contains("hidden"))showLogin()});
})();
