(function(){
'use strict';
var $=function(id){return document.getElementById(id);},active=false,credentials=null;
try{document.documentElement.dataset.theme=localStorage.getItem('nl.installer.theme')||'dark';}catch(e){}
$('theme').onclick=function(){var theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('nl.installer.theme',theme);}catch(e){}};
$('reveal').onclick=function(){var visible=$('token').type==='password';$('token').type=visible?'text':'password';this.textContent=visible?'پنهان':'نمایش';};
function status(text,error){$('form-status').textContent=text;$('form-status').classList.toggle('error',!!error);}
function log(text){var li=document.createElement('li');li.textContent=new Date().toLocaleTimeString('fa-IR')+' · '+text;$('event-log').appendChild(li);if($('event-log').children.length>30)$('event-log').firstElementChild.remove();}
function stage(event){
 var row=document.querySelector('[data-step="'+event.step+'"]');
 if(row){row.dataset.state=event.state;row.querySelector('.step-state').textContent=event.state==='done'?'انجام شد':event.state==='error'?'خطا':'در حال انجام';if(event.message)row.querySelector('small').textContent=event.message;}
 if(Number.isFinite(event.percent)){$('progress-fill').style.width=event.percent+'%';$('progress').setAttribute('aria-valuenow',String(event.percent));}
 if(event.message){status(event.message);log(event.message);}
}
function showResult(data){credentials={url:data.url,password:data.password};$('result').hidden=false;$('panel-url').textContent=data.url;$('panel-url').href=data.url;$('open-panel').href=data.url;$('password').value=data.password;$('result-notice').textContent=data.notice;status('نصب انجام شد؛ اطلاعات ورود را پیش از بستن صفحه ذخیره کنید.');$('go-label').textContent='نصب با موفقیت تمام شد';$('result').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'nearest'});}
function receive(event){
 if(event.type==='stage')stage(event);
 if(event.type==='error'){var current=document.querySelector('.steps [data-state="running"]');if(current){current.dataset.state='error';current.querySelector('.step-state').textContent='خطا';}status(event.error||'نصب کامل نشد.',true);log(event.error||'خطای نصب');$('log-panel').open=true;}
 if(event.type==='done'&&event.ok)showResult(event);
}
$('install').addEventListener('submit',async function(event){
 event.preventDefault();if(active||credentials)return;
 var token=$('token').value.trim();
 if(/^gh[pousr]_/.test(token)||/^github_pat_/.test(token)){status('این توکن GitHub است؛ برای ساخت Worker باید توکن Cloudflare وارد کنید.',true);return;}
 if(!/^[A-Za-z0-9_-]{20,512}$/.test(token)){status('توکن Cloudflare را کامل وارد کنید؛ از لینک ساخت توکن آماده استفاده کنید.',true);$('token').focus();return;}
 active=true;$('go').disabled=true;$('go').classList.add('busy');$('go-label').textContent='در حال نصب؛ این صفحه را نبندید';$('token').disabled=true;$('token').value='';$('progress-section').hidden=false;$('event-log').innerHTML='';$('result').hidden=true;
 document.querySelectorAll('[data-step]').forEach(function(row){delete row.dataset.state;row.querySelector('.step-state').textContent='در انتظار';});$('progress-fill').style.width='0%';
 status('اتصال به نصب‌کننده برقرار می‌شود…');log('درخواست نصب از همین صفحه ارسال شد.');
 var start=Date.now(),timer=setInterval(function(){var seconds=Math.floor((Date.now()-start)/1000);$('elapsed').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');},1000);
 var controller=new AbortController(),deadline=setTimeout(function(){controller.abort();},240000),terminal=false;
 try{
  var response=await fetch('/install',{method:'POST',headers:{'content-type':'application/json','accept':'application/x-ndjson'},body:JSON.stringify({token:token}),signal:controller.signal});token='';
  if(!response.ok){var error=await response.json().catch(function(){return {};});throw Error(error.error||'خطای ارتباط با نصب‌کننده: HTTP '+response.status);}
  function line(text){if(!text.trim())return;var data=JSON.parse(text);if(data.type==='error'||data.type==='done')terminal=true;receive(data);}
  if(response.body&&response.body.getReader){
   var reader=response.body.getReader(),decoder=new TextDecoder(),buffer='';
   while(true){var part=await reader.read();if(part.done)break;buffer+=decoder.decode(part.value,{stream:true});var lines=buffer.split('\n');buffer=lines.pop();lines.forEach(line);}
   buffer+=decoder.decode();if(buffer.trim())line(buffer);
  }else (await response.text()).split('\n').forEach(line);
  if(!terminal)throw Error('ارتباط پیش از دریافت نتیجه قطع شد. منابع جدید اکانت را بررسی کنید و نصب را بی‌دلیل تکرار نکنید.');
 }catch(error){receive({type:'error',error:error.name==='AbortError'?'مهلت ارتباط تمام شد. ممکن است نصب روی سرور ادامه یافته باشد؛ پیش از تکرار، منابع اکانت را بررسی کنید.':error.message});}
 finally{token='';clearInterval(timer);clearTimeout(deadline);active=false;$('go').classList.remove('busy');$('go').disabled=!!credentials;$('token').disabled=!!credentials;if(!credentials)$('go-label').textContent='بررسی و تلاش دوباره';}
});
async function copy(value){try{await navigator.clipboard.writeText(value);status('کپی شد.');}catch(e){status('دسترسی کلیپ‌بورد بسته است؛ مقدار را انتخاب و دستی کپی کنید.',true);}}
$('copy-url').onclick=function(){if(credentials)copy(credentials.url);};$('copy-password').onclick=function(){if(credentials)copy(credentials.password);};$('show-password').onclick=function(){$('password').type=$('password').type==='password'?'text':'password';};
$('download').onclick=function(){if(!credentials)return;var blob=new Blob(['NIGHTLINE 2.5.3\nPanel: '+credentials.url+'\nAdmin password: '+credentials.password+'\nKeep this file private.\n'],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='nightline-private-login.txt';a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
window.addEventListener('beforeunload',function(e){if(active){e.preventDefault();e.returnValue='';}});
})();