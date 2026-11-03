/* Dedicated NIGHTLINE 2.5.3 installer. All progress is reported by the server on the same origin. */
import { initializePanelState } from './storage.js';
import { PAYLOAD, SHA256 } from './generated/installer-payload.js';
import { INSTALLER_HTML } from './generated/installer-ui.js';

const API='https://api.cloudflare.com/client/v4';
const permissions=[['account_analytics','read'],['account_settings','read'],['workers_scripts','edit'],['workers_kv_storage','edit']].map(([key,type])=>({key,type}));
export const TOKEN_TEMPLATE='https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys='+encodeURIComponent(JSON.stringify(permissions))+'&accountId=*&name=NIGHTLINE%20Free%20Installer';
const locks=new Set();
const escapeHTML=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reply=(data,status=200)=>Response.json(data,{status,headers:{'cache-control':'no-store','x-content-type-options':'nosniff'}});

async function readInput(request){
  if(Number(request.headers.get('content-length'))>2048)throw Error('درخواست بیش‌ازحد بزرگ است.');
  const reader=request.body?.getReader();if(!reader)throw Error('توکن ارسال نشده است.');
  const decoder=new TextDecoder();let body='',size=0;
  try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.length;if(size>2048){await reader.cancel();throw Error('درخواست بیش‌ازحد بزرگ است.');}body+=decoder.decode(part.value,{stream:true});}body+=decoder.decode();}
  finally{reader.releaseLock();}
  let data;try{data=JSON.parse(body);}catch{throw Error('قالب درخواست نصب معتبر نیست.');}
  const token=String(data.token||'').trim();
  if(/^(?:gh[pousr]_|github_pat_)/.test(token))throw Error('این توکن GitHub است. نصب Worker به توکن Cloudflare نیاز دارد.');
  if(!/^[A-Za-z0-9_-]{20,512}$/.test(token))throw Error('توکن Cloudflare نامعتبر یا ناقص است.');
  return token;
}
async function unpack(){
  const bytes=Uint8Array.from(atob(PAYLOAD),c=>c.charCodeAt(0));
  const source=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
  const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',source))].map(b=>b.toString(16).padStart(2,'0')).join('');
  if(hash!==SHA256)throw Error('یکپارچگی بستهٔ نصب تأیید نشد؛ فایل نصب‌کننده را دوباره دریافت کنید.');
  return source;
}
export async function runInstallation(initialToken,emit,apiBase=API){
  let token=initialToken,account,namespace,script,uploaded=false,uploadAttempted=false,ownsLock=false,current='account';
  const stage=(step,state,message,percent)=>{current=step;emit({type:'stage',step,state,message,percent});};
  async function cf(path,method='GET',body,allowMissing=false){
    const headers={authorization:'Bearer '+token};
    if(body!==undefined&&!(body instanceof FormData)&&typeof body!=='string'){headers['content-type']='application/json';body=JSON.stringify(body);}
    let res;try{res=await fetch(apiBase+path,{method,headers,body,signal:AbortSignal.timeout(20000)});}
    catch(e){throw Error(e.name==='TimeoutError'||e.name==='AbortError'?'مهلت پاسخ Cloudflare تمام شد؛ ارتباط یا وضعیت سرویس را بررسی کنید.':'ارتباط نصب‌کننده با API کلادفلر برقرار نشد.');}
    const data=await res.json().catch(()=>null);
    if(allowMissing&&res.status===404)return null;
    if(!res.ok||!data||data.success!==true){
      const hints={401:'توکن رد شد؛ توکن Cloudflare معتبر و فعال وارد کنید.',403:'دسترسی کافی نیست؛ چهار مجوز تمپلیت و منابع اکانت را کنترل کنید.',429:'سهمیه یا نرخ درخواست Cloudflare پر شده؛ کمی بعد تلاش کنید.'};
      const detail=(data?.errors||[]).slice(0,2).map(e=>String(e.code||'')+' '+String(e.message||'')).join(' · ').replaceAll(token,'[REDACTED]').slice(0,220);
      throw Error('Cloudflare HTTP '+res.status+' — '+(hints[res.status]||'مجوزها، سهمیه و وضعیت اکانت را بررسی کنید.')+(detail?' ('+detail+')':''));
    }
    return data.result;
  }
  try{
    stage('account','running','در حال بررسی توکن و شناسایی حساب مقصد…',3);
    const accounts=await cf('/accounts?per_page=2');
    if(!Array.isArray(accounts)||accounts.length!==1)throw Error('توکن باید فقط به یک اکانت دسترسی داشته باشد؛ Account Resources را در توکن محدود کنید.');
    account=accounts[0].id;
    if(locks.has(account))throw Error('نصب دیگری برای این اکانت در جریان است؛ منتظر پایان آن بمانید.');
    locks.add(account);ownsLock=true;
    let subdomain=await cf(`/accounts/${account}/workers/subdomain`,'GET',undefined,true);
    if(!subdomain?.subdomain)subdomain=await cf(`/accounts/${account}/workers/subdomain`,'PUT',{subdomain:'nightline-free-'+crypto.randomUUID().slice(0,8)});
    if(!/^[a-z0-9-]+$/.test(subdomain?.subdomain||''))throw Error('workers.dev در این اکانت فعال نیست؛ وضعیت اکانت را در داشبورد بررسی کنید.');
    stage('account','done','اکانت مقصد و زیردامنه شناسایی شدند؛ نام موجود تغییر نمی‌کند.',14);
    stage('package','running','آماده‌سازی بسته و بررسی SHA-256…',16);
    const source=await unpack();stage('package','done','بستهٔ تعبیه‌شدهٔ نسخهٔ ۲.۵.۳ سالم است.',25);
    script='nightline-free-'+crypto.randomUUID().replaceAll('-','').slice(0,12);
    stage('storage','running','در حال ساخت KV اختصاصی پنل…',28);
    namespace=await cf(`/accounts/${account}/storage/kv/namespaces`,'POST',{title:script+'-kv'});
    stage('storage','done','فضای ذخیره‌سازی جدید ساخته شد.',38);
    stage('security','running','تولید گذرواژه و ثبت وضعیت مدیریت پیش از انتشار…',40);
    const password=[...crypto.getRandomValues(new Uint8Array(24))].map(b=>b.toString(16).padStart(2,'0')).join('');
    const kv={get:async()=>null,put:async(key,value)=>{await cf(`/accounts/${account}/storage/kv/namespaces/${namespace.id}/values/${encodeURIComponent(key)}`,'PUT',value);}};
    await initializePanelState({KV:kv},{password,firstUserName:'مدیر NIGHTLINE رایگان'});
    stage('security','done','مدیریت ایجاد شد؛ راه‌اندازی عمومی در پنل نصب‌شده قفل می‌شود.',50);
    stage('worker','running','در حال ارسال کد کامل پنل و اتصال KV…',53);
    const form=new FormData();
    form.append('metadata',new Blob([JSON.stringify({main_module:'worker.js',compatibility_date:'2026-04-01',bindings:[{type:'kv_namespace',name:'KV',namespace_id:namespace.id},{type:'plain_text',name:'SETUP_LOCKED',text:'true'}]})],{type:'application/json'}));
    form.append('worker.js',new Blob([source],{type:'application/javascript+module'}),'worker.js');
    uploadAttempted=true;await cf(`/accounts/${account}/workers/scripts/${script}`,'PUT',form);uploaded=true;
    stage('worker','done','Worker نصب شد: '+script,72);
    stage('publish','running','فعال‌سازی آدرس عمومی workers.dev…',75);
    await cf(`/accounts/${account}/workers/scripts/${script}/subdomain`,'POST',{enabled:true,previews_enabled:false});
    stage('publish','done','آدرس عمومی پنل فعال شد.',87);
    const origin=`https://${script}.${subdomain.subdomain}.workers.dev`;
    stage('verify','running','بررسی پاسخ پنل؛ انتشار KV یا DNS ممکن است زمان ببرد…',90);
    let verified=false;
    for(let attempt=0;attempt<3;attempt++){
      try{const res=await fetch((apiBase===API?origin+'/health':apiBase+'/health')+'?_install='+crypto.randomUUID(),{cache:'no-store',signal:AbortSignal.timeout(3500)});const health=await res.json();if(res.ok&&health.ok===true&&health.version==='2.5.3'&&health.initialized===true){verified=true;break;}}catch{}
      if(attempt<2)await new Promise(r=>setTimeout(r,700));
    }
    const notice=verified?'پاسخ پنل و راه‌اندازی مدیریت تأیید شد. اطلاعات ورود را ذخیره و پس از ورود گذرواژه را تغییر دهید.':'نصب و فعال‌سازی در Cloudflare انجام شد؛ پاسخ نهایی هنوز تأیید نشده است. برای انتشار KV و DNS کمی صبر کنید و سپس آدرس پنل را باز کنید. نصب را دوباره اجرا نکنید.';
    stage('verify','done',verified?'پاسخ نسخهٔ ۲.۵.۳ و مدیریت تأیید شد.':'نصب ثبت شد؛ انتشار نهایی هنوز در انتظار تأیید است.',100);
    return {ok:true,url:origin+'/panel',password,verified,notice};
  }catch(e){
    emit({type:'stage',step:current,state:'error',message:'این مرحله کامل نشد؛ بررسی و پاک‌سازی منابع جدید…'});
    let cleanup='';
    if(uploadAttempted){try{await cf(`/accounts/${account}/workers/scripts/${script}`,'DELETE',undefined,true);uploaded=false;}catch{uploaded=true;}}
    if(namespace&&account&&!uploaded){try{await cf(`/accounts/${account}/storage/kv/namespaces/${namespace.id}`,'DELETE',undefined,true);}catch{cleanup=` فضای KV ${script}-kv را از داشبورد بررسی کنید.`;}}
    if(uploaded)cleanup=` پاک‌سازی Worker ${script} تأیید نشد؛ Worker و KV همان نصب را قبل از تکرار، در داشبورد بررسی کنید.`;
    const error=String(e instanceof Error?e.message:'خطای نصب').replaceAll(token,'[REDACTED]')+cleanup;
    return {ok:false,error};
  }finally{if(ownsLock)locks.delete(account);token='';initialToken='';}
}
export default {
  async fetch(request,env={},ctx={}){
    const url=new URL(request.url);
    if(request.method==='GET'&&url.pathname==='/'){
      const nonce=crypto.randomUUID().replaceAll('-','');
      const html=INSTALLER_HTML.replaceAll('{{NONCE}}',nonce).replaceAll('{{TOKEN_TEMPLATE}}',escapeHTML(TOKEN_TEMPLATE));
      return new Response(html,{headers:{'content-type':'text/html; charset=utf-8','cache-control':'no-store','content-security-policy':`default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; font-src data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,'referrer-policy':'no-referrer','x-content-type-options':'nosniff'}});
    }
    if(request.method!=='POST'||url.pathname!=='/install')return reply({ok:false,error:'مسیر نصب پیدا نشد.'},404);
    if(request.headers.get('origin')!==url.origin||!/^application\/json(?:;|$)/i.test(request.headers.get('content-type')||''))return reply({ok:false,error:'درخواست باید از صفحهٔ همین نصب‌کننده ارسال شود.'},403);
    let token;try{token=await readInput(request);}catch(e){return reply({ok:false,error:e.message},400);}
    const apiBase=/^http:\/\/127\.0\.0\.1:\d+\/cf$/.test(env.NL_INSTALLER_TEST_CF_BASE||'')?env.NL_INSTALLER_TEST_CF_BASE:API;
    if(!request.headers.get('accept')?.includes('application/x-ndjson'))return reply(await runInstallation(token,()=>{},apiBase));
    const encoder=new TextEncoder();let connected=true;
    const stream=new ReadableStream({start(controller){
      const emit=event=>{if(connected){try{controller.enqueue(encoder.encode(JSON.stringify(event)+'\n'));}catch{connected=false;}}};
      const task=(async()=>{
        const heartbeat=setInterval(()=>emit({type:'heartbeat'}),5000);
        try{const result=await runInstallation(token,emit,apiBase);emit(result.ok?{type:'done',...result}:{type:'error',...result});}
        catch{emit({type:'error',ok:false,error:'خطای پیش‌بینی‌نشده؛ وضعیت منابع جدید را در داشبورد بررسی کنید.'});}
        finally{token='';clearInterval(heartbeat);if(connected){try{controller.close();}catch{}}}
      })();
      if(ctx.waitUntil)ctx.waitUntil(task);
    },cancel(){connected=false;}});
    return new Response(stream,{headers:{'content-type':'application/x-ndjson; charset=utf-8','cache-control':'no-store, no-transform','content-encoding':'identity','x-content-type-options':'nosniff'}});
  }
};
