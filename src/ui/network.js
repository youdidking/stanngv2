/* Bounded browser-native diagnostics. No server-side IP scan or rejection-as-ping heuristic. */
(function (root) {
  'use strict';
  function median(values) {
    var a = values.filter(Number.isFinite).slice().sort(function (x,y) { return x-y; });
    return a.length ? Math.round(a.length % 2 ? a[(a.length-1)/2] : (a[a.length/2-1]+a[a.length/2])/2) : null;
  }
  function summary(samples) {
    var good = samples.filter(function(s) { return s.ok; });
    var diffs = [];
    for (var i=1;i<good.length;i++) diffs.push(Math.abs(good[i].ms-good[i-1].ms));
    return { ok: good.length > samples.length/2, ms: median(good.map(function(s){return s.ms;})),
      attemptMs: median(samples.map(function(s){return s.ms;})), jitter: median(diffs),
      replies:good.length, attempts:samples.length, noReply: samples.length ? Math.round((samples.length-good.length)*100/samples.length) : 100,
      samples:samples };
  }
  async function timed(url, options) {
    options = options || {};
    var local = new AbortController(), parent = options.signal, expired = false;
    function abort() { local.abort(); }
    if (parent) { if(parent.aborted) abort(); else parent.addEventListener('abort',abort,{once:true}); }
    var timer = setTimeout(function(){expired=true;local.abort();}, options.timeout || 2500);
    var start = performance.now();
    try {
      var sep = url.indexOf('?')<0?'?':'&';
      var res = await fetch(url + sep + '_nl=' + Date.now() + '-' + Math.random().toString(36).slice(2,8), {
        method:options.method || 'GET',mode:options.mode || 'cors',credentials: options.credentials || 'omit',
        cache:'no-store',redirect:options.redirect || 'follow',referrerPolicy:'no-referrer',
        headers:options.headers,body:options.body,signal:local.signal
      });
      var extra={};
      if(options.check === 'health') {
        if(!res.ok) throw Error('http');
        var h=await res.json(); if(h.ok!==true || h.version!=='2.5.3')throw Error('health');
      } else if(options.check === 'dns') {
        if(!res.ok)throw Error('http');
        var d=await res.json();if(d.Status!==0 || !Array.isArray(d.Answer) || !d.Answer.some(function(a){return a.type===1;}))throw Error('dns');
      } else if(options.check === 'dns-wire') {
        if(!res.ok)throw Error('http');
        var wire=new Uint8Array(await res.arrayBuffer());
        if(wire.length<12 || !(wire[2]&128) || (wire[3]&15)!==0 || (wire[6]*256+wire[7])<1)throw Error('dns');
      } else if(options.check === 'download') {
        if(!res.ok)throw Error('http');extra.bytes=(await res.arrayBuffer()).byteLength;
        if(extra.bytes!==options.bytes)throw Error('size');
      } else if(options.check === 'upload') {
        if(!res.ok)throw Error('http');extra.bytes=Number((await res.json()).receivedBytes);
        if(extra.bytes!==options.bytes)throw Error('size');
      } else if(res.type!=='opaque' && !res.ok)throw Error('http');
      return Object.assign({ok:true,ms:Math.max(1,Math.round(performance.now()-start))},extra);
    } catch(e) {
      return {ok:false,ms:Math.max(1,Math.round(performance.now()-start)),reason: parent&&parent.aborted?'cancelled':expired?'timeout':'browser-or-network'};
    } finally { clearTimeout(timer);if(parent)parent.removeEventListener('abort',abort); }
  }
  async function sample(url, options, rounds) {
    var out=[];
    for(var i=0;i<(rounds||3);i++) {if(options.signal&&options.signal.aborted)break;out.push(await timed(url,options));}
    return summary(out);
  }
  async function bounded(items, count, fn, signal) {
    var output=new Array(items.length), next=0;
    async function work(){while(next<items.length && !(signal&&signal.aborted)){var i=next++;output[i]=await fn(items[i],i);}}
    await Promise.all(Array.from({length:Math.min(count,items.length)},work));
    return output.filter(Boolean);
  }
  function wsProbe(url, signal) {
    return new Promise(function(resolve){
      var ws, samples=[], started, expected='', done=false, timer;
      function finish(){if(done)return;done=true;clearTimeout(timer);if(signal)signal.removeEventListener('abort',finish);try{ws.close();}catch(e){}resolve(summary(samples));}
      function send(){if(done)return;if(samples.length>=5){finish();return;}expected='nl-'+samples.length+'-'+Math.random().toString(36).slice(2,10);started=performance.now();ws.send(JSON.stringify({type:'nl-ping',id:expected}));clearTimeout(timer);timer=setTimeout(function(){samples.push({ok:false,ms:2500});finish();},2500);}
      if(signal&&signal.aborted){finish();return;}
      try {ws=new WebSocket(url);}catch(e){finish();return;}
      timer=setTimeout(finish,3500);
      if(signal)signal.addEventListener('abort',finish,{once:true});
      ws.onopen=send;
      ws.onmessage=function(event){try{var d=JSON.parse(event.data);if(d.type==='nl-pong'&&d.id===expected){samples.push({ok:true,ms:Math.max(1,Math.round(performance.now()-started))});send();}}catch(e){}};
      ws.onerror=finish;ws.onclose=finish;
    });
  }
  root.NLNetwork={median:median,summary:summary,timed:timed,sample:sample,bounded:bounded,wsProbe:wsProbe};
})(globalThis);
