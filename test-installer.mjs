import assert from 'node:assert/strict';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright';
import fs from 'node:fs';
const PORT=20100+Math.floor(Math.random()*300),MOCK=20500+Math.floor(Math.random()*300);
const BASE='http://127.0.0.1:'+PORT,TOKEN='cloudflare_valid_token_01234567890123456789';
let uploads=0,stateDoc=null,metadata='',requests=[];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');requests.push([req.method,url.pathname]);
 const chunks=[];for await(const chunk of req)chunks.push(chunk);const body=Buffer.concat(chunks).toString();
 res.setHeader('content-type','application/json');
 if(url.pathname==='/cf/health'){res.end(JSON.stringify({ok:true,version:'2.5.3',initialized:true}));return;}
 if(req.headers.authorization!=='Bearer '+TOKEN){res.statusCode=403;res.end(JSON.stringify({success:false,errors:[{code:10000,message:'Authentication error: missing permissions'}]}));return;}
 // Intentional pause verifies that a real intermediate stage appears before completion.
 if(url.pathname==='/cf/accounts')await new Promise(r=>setTimeout(r,500));
 let result={};
 if(url.pathname==='/cf/accounts')result=[{id:'a'.repeat(32)}];
 else if(url.pathname.endsWith('/workers/subdomain'))result={subdomain:'community-account'};
 else if(url.pathname.endsWith('/storage/kv/namespaces')&&req.method==='POST')result={id:'b'.repeat(32)};
 else if(url.pathname.includes('/values/'))stateDoc=JSON.parse(body);
 else if(url.pathname.includes('/workers/scripts/')&&req.method==='PUT'){uploads++;metadata=body;}
 res.end(JSON.stringify({success:true,result}));
});
await new Promise(r=>server.listen(MOCK,'127.0.0.1',r));
const proc=spawn('node_modules/.bin/wrangler',['dev','--config','wrangler.installer.toml','--local','--port',String(PORT),'--var','NL_INSTALLER_TEST_CF_BASE:http://127.0.0.1:'+MOCK+'/cf'],{stdio:['ignore','pipe','pipe'],detached:process.platform!=='win32'});
let logs='';proc.stdout.on('data',d=>logs+=d);proc.stderr.on('data',d=>logs+=d);
let browser;
try{
 let ready=false;for(let i=0;i<50;i++){try{if((await fetch(BASE,{signal:AbortSignal.timeout(1200)})).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,200));}
 assert(ready,'Installer workerd did not start: '+logs);
 browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1280,height:980}});const page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));await page.goto(BASE);
 assert.equal(await page.locator('h1').innerText(),'NIGHTLINE');assert.equal(await page.locator('#go').isEnabled(),true);
 await page.fill('#token','ghp_012345678901234567890123456789012345');await page.click('#go');assert((await page.locator('#form-status').innerText()).includes('GitHub'));
 assert.equal(uploads,0);
 await page.fill('#token',TOKEN);await page.click('#go');
 await page.waitForSelector('[data-step="account"][data-state="running"]');
 assert.equal(await page.locator('#result').isVisible(),false,'Progress must arrive before terminal result');
 assert.equal(await page.locator('#go').isDisabled(),true);
 await page.waitForSelector('#result:not([hidden])',{timeout:30000});
 assert.equal(new URL(page.url()).origin,BASE,'All installation stages stay on installer origin');
 assert.equal(await page.locator('.steps [data-state="done"]').count(),7);
 assert.equal(await page.locator('#progress').getAttribute('aria-valuenow'),'100');
 assert.match(await page.locator('#panel-url').innerText(),/^https:\/\/nightline-free-[a-f0-9]{12}\.community-account\.workers.dev\/panel$/);
 assert.equal((await page.locator('#password').inputValue()).length,48);
 assert.equal(uploads,1);assert(metadata.includes('SETUP_LOCKED'));assert(!JSON.stringify(stateDoc).includes(TOKEN));
 assert.equal(await page.locator('#token').inputValue(),'');assert(!(await page.locator('#event-log').innerText()).includes(TOKEN));
 assert.equal(await page.evaluate(()=>Object.keys(localStorage).some(k=>/token|password/i.test(k))),false);
 fs.mkdirSync('.arena',{recursive:true});await page.screenshot({path:'.arena/installer-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile installer must not overflow');
 await page.screenshot({path:'.arena/installer-mobile.png',fullPage:true});
 const fail=await context.newPage();await fail.goto(BASE);await fail.fill('#token','cloudflare_wrong_token_01234567890123456789');await fail.click('#go');
 await fail.waitForFunction(()=>document.getElementById('form-status').classList.contains('error'));
 assert((await fail.locator('#form-status').innerText()).includes('403'));assert.equal(await fail.locator('#go').isEnabled(),true);
 const streamed=await fetch(BASE+'/install',{method:'POST',headers:{origin:BASE,'content-type':'application/json',accept:'application/x-ndjson'},body:JSON.stringify({token:TOKEN})});
 assert(streamed.headers.get('content-type').startsWith('application/x-ndjson'));
 const events=(await streamed.text()).trim().split('\n').map(JSON.parse);
 assert(events.some(e=>e.type==='stage'&&e.state==='running'));assert(events.at(-1).type==='done');assert.equal(events.at(-1).verified,true);
 assert.equal(errors.length,0,errors.join('\n'));
 console.log('PASS installer real workerd + Chromium: no JS errors, token validation, live NDJSON progress, seven stages, same-site completion, errors, naming, KV security, mobile layout and no persisted credentials.');
 console.log('Cloudflare account operations were served by a local mock; this is not a live account deployment certification.');
}catch(e){console.error(logs.slice(-2500));throw e;}
finally{if(browser)await browser.close();try{if(process.platform!=='win32')process.kill(-proc.pid,'SIGTERM');else proc.kill('SIGTERM');}catch{}server.closeAllConnections();server.close();}
