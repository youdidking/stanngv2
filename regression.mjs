import assert from 'node:assert/strict';
import fs from 'node:fs';
import { build } from 'esbuild';
import { sanitizeSettings, DEFAULT_SETTINGS } from './src/storage.js';
import installer, { TOKEN_TEMPLATE } from './src/installer.js';

assert.deepEqual(Object.keys(await import('./nightline-installer.js')), ['default'], 'Cloudflare bundle must export only its default handler');
assert.equal(sanitizeSettings({showSubInfoNodes:false},{...DEFAULT_SETTINGS,showSubInfoNodes:false}).showSubInfoNodes,true);
for (const v of ['0','300-100','999999','-1','10-0']) assert.equal(sanitizeSettings({fragmentLength:v}).fragmentLength,DEFAULT_SETTINGS.fragmentLength);
assert.equal(sanitizeSettings({fragmentLength:'100-200',fragmentInterval:'1-3'}).fragmentLength,'100-200');
assert.equal(sanitizeSettings({fragmentPackets:'20-1'}).fragmentPackets,'tlshello');
console.log('PASS mandatory notices and bounded fragment ranges');
fs.mkdirSync('.arena',{recursive:true});
await build({entryPoints:['src/sub.js'],outfile:'.arena/sub.mjs',bundle:true,platform:'node',format:'esm',plugins:[{name:'sockets-test-stub',setup(b){b.onResolve({filter:/^cloudflare:sockets$/},()=>({path:'sockets',namespace:'mock'}));b.onLoad({filter:/.*/,namespace:'mock'},()=>({contents:'export function connect(){throw Error("No sockets in unit tests")}' }));}}]});
const sub = await import('./.arena/sub.mjs');
const user={id:'test',name:'کاربر',uuid:'b9ac52fc-ec0f-4f3b-9ee6-c967ed402153',trojanPassword:'secret',subToken:'testtoken',usedBytes:1234,usedUpBytes:0,usedDownBytes:1234,limitBytes:0,effectiveExpiresAt:0};
const settings={...DEFAULT_SETTINGS,showSubInfoNodes:false,cleanIPs:['104.16.1.1'],tlsPorts:[443],earlyData:0,alpn:'http/1.1'};
const lines=sub.buildUriLines(user,settings,'panel.example.com');
assert(decodeURIComponent(lines[0]).includes('رایگان'));
assert(decodeURIComponent(lines[1]).includes('مصرف'));
assert(decodeURIComponent(lines[2]).includes('اعتبار'));
const configs=sub.buildXrayConfigs(user,settings,'panel.example.com');
assert(configs.length>0);
for(const config of configs){
 assert.equal(config.outbounds[0].streamSettings.tlsSettings.allowInsecure,false);
 assert.equal(config.outbounds[0].streamSettings.sockopt.dialerProxy,'fragment');
 assert.equal(config.outbounds.find(o=>o.tag==='fragment').settings.fragment.packets,'tlshello');
 assert.equal(config.outbounds.find(o=>o.tag==='fragment').settings.domainStrategy,'UseIP');
 assert(!config.outbounds[0].streamSettings.wsSettings.path.includes('ed='));
}
const sing=sub.buildSingboxConfig(user,settings,'panel.example.com');
assert(sing.outbounds.filter(o=>o.type==='vless'||o.type==='trojan').every(o=>!o.transport.max_early_data));
const yaml=sub.buildClashYaml(user,settings,'panel.example.com');assert(!yaml.includes('max-early-data: 2048'));
assert(sub.buildSubscriptionHeaders(user,settings,'panel.example.com')['subscription-userinfo']);
console.log('PASS URI notices, Xray fragment chain, TLS checks and shared Early Data across formats');

const page=await installer.fetch(new Request('https://installer.example/'));
assert.equal(page.status,200);assert(page.headers.get('content-security-policy').includes("frame-ancestors 'none'"));
const perms=JSON.parse(new URL(TOKEN_TEMPLATE).searchParams.get('permissionGroupKeys'));
assert.deepEqual(perms.map(p=>p.key),['account_analytics','account_settings','workers_scripts','workers_kv_storage']);
const bad=await installer.fetch(new Request('https://installer.example/install',{method:'POST',headers:{origin:'https://evil.example','content-type':'application/json'},body:'{}'}));assert.equal(bad.status,403);
const originalFetch=globalThis.fetch;
const token='test_cloudflare_token_01234567890123456789';
const account='0123456789abcdef0123456789abcdef';
const calls=[];let persisted,metadata,failPublish=false,multiple=false;
globalThis.fetch=async(url,opts={})=>{
 const path=new URL(url).pathname.replace('/client/v4','');calls.push([opts.method||'GET',path]);
 assert.equal(opts.headers.authorization,'Bearer '+token);
 let result={};
 if(path==='/accounts')result=multiple?[{id:account},{id:'another'}]:[{id:account}];
 else if(path.endsWith('/workers/subdomain'))result={subdomain:'existing-account'};
 else if(path.endsWith('/storage/kv/namespaces')&&opts.method==='POST')result={id:'abcdef0123456789abcdef0123456789ab'};
 else if(path.includes('/values/'))persisted=JSON.parse(opts.body);
 else if(opts.body instanceof FormData){metadata=JSON.parse(await opts.body.get('metadata').text());const code=await opts.body.get('worker.js').text();assert(code.length>100000);}
 else if(path.endsWith('/subdomain')&&opts.method==='POST'&&failPublish)return Response.json({success:false},{status:500});
 return Response.json({success:true,result});
};
const installRequest=()=>new Request('https://installer.example/install',{method:'POST',headers:{origin:'https://installer.example','content-type':'application/json'},body:JSON.stringify({token})});
try{
 const result=await (await installer.fetch(installRequest())).json();
 assert.equal(result.ok,true,result.error);
 assert.match(result.url,/^https:\/\/nightline-free-[a-f0-9]{12}\.existing-account\.workers.dev\/panel$/);
 assert.equal(result.password.length,48);
 assert(!JSON.stringify(persisted).includes(token));assert(persisted.auth.hash||persisted.auth.passwordHash);
 assert(metadata.bindings.some(b=>b.name==='KV'));
 assert(metadata.bindings.some(b=>b.name==='SETUP_LOCKED'&&b.text==='true'));
 const seed=calls.findIndex(c=>c[1].includes('/values/'));
 const upload=calls.findIndex(c=>c[0]==='PUT'&&c[1].includes('/workers/scripts/'));
 assert(seed<upload,'Seed credentials before deployment');
 assert(!calls.some(c=>c[0]==='PUT'&&c[1].endsWith('/workers/subdomain')),'Do not rename account subdomain');
 console.log('PASS installer deployment, embedded payload checksum, initialized KV, naming and credential isolation (mock API)');
 calls.length=0;failPublish=true;
 const fail=await(await installer.fetch(installRequest())).json();assert.equal(fail.ok,false);
 assert(calls.some(c=>c[0]==='DELETE'&&c[1].includes('/workers/scripts/')));
 assert(calls.some(c=>c[0]==='DELETE'&&c[1].includes('/storage/kv/namespaces/')));
 console.log('PASS installer rollback after publish failure (mock API)');
 calls.length=0;multiple=true;failPublish=false;
 const amb=await(await installer.fetch(installRequest())).json();assert.equal(amb.ok,false);assert.equal(calls.length,1);
 console.log('PASS reject ambiguous multi-account tokens before creating resources');
}finally{globalThis.fetch=originalFetch;}
console.log('All regression groups passed. Live Cloudflare installation and Iranian ISP Fragment performance are not certified by these tests.');
