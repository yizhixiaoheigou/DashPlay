import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from '../build/receiver/web/node_modules/typescript/lib/typescript.js';
const main = readFileSync(new URL('../build/receiver/web/src/main.ts', import.meta.url), 'utf8');
const source = main.slice(main.indexOf('interface BoxPhone'), main.indexOf('function fit()'));
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.hidden = false; this.attributes = {}; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
}
const phone = { address: '02:00:00:00:00:01', name: 'Test iPhone', active: true, last: true };
function setup() {
  const nodes = {}, requests = [], timers = new Map();
  let timerId = 0, now = 0;
  const context = vm.createContext({
    el: id => nodes[id] ??= new Element(), document: { createElement: tag => new Element(tag) },
    t: key => key, token: 'synthetic-token', panel: { open: true }, toast() {},
    window: { setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; }, clearTimeout: id => timers.delete(id) },
    AbortController, Date: { now: () => now },
    fetch: async (url, init) => {
      requests.push({ url, init });
      return { ok: true, json: async () => init?.method === 'POST' ? { ok: true } : { supported: true, phones: [phone, {...phone,address:'02:00:00:00:00:02',active:false}] } };
    },
  });
  vm.runInContext(code, context);
  return { context, nodes, requests, timers, advance: ms => { now += ms; }, run: text => vm.runInContext(text, context) };
}
{
  const { context, nodes, requests, run } = setup();
  context.fetch = async (url, init) => {
    requests.push({url, init});
    return {ok:true, json:async()=>init?.method === 'POST' ? {ok:true}
      : {supported:true, phones:[{...phone,active:false},{...phone,address:'02:00:00:00:00:02',active:false}]}};
  };
  await run('loadDevices()');
  await run('switchTo(boxPhones[0])');
  assert.equal(run('boxPhones[0].active'),false,'an accepted invitation is not an active session');
  assert.equal(nodes['device-list'].children[0].children[1].textContent,'deviceStarting');
  assert.equal(nodes['device-list'].children[0].children.some(e=>e.className==='device-disconnect'),false);
  assert.equal(nodes['device-list'].children[0].disabled,true,'do not repeat a pending invitation');
  const posts = requests.filter(r=>r.init?.method==='POST').length;
  await run('switchTo(boxPhones[0])');
  assert.equal(requests.filter(r=>r.init?.method==='POST').length,posts);
  context.fetch=async()=>({ok:true,json:async()=>({supported:true,phones:[phone]})});
  await run('loadDevices()');
  // If the post-action read was still finishing, consume its queued follow-up.
  for(let i=0;i<8;i++) await Promise.resolve();
  await run('loadDevices()');
  assert.equal(nodes['device-list'].children[0].children[1].textContent,'deviceInUse');
}
{
  const {context,nodes,advance,run}=setup();
  context.fetch=async(_url,init)=>({ok:true,json:async()=>init?.method==='POST'?{ok:true}:{supported:true,phones:[{...phone,active:false},{...phone,address:'02:00:00:00:00:02',active:false}]}});
  await run('loadDevices()'); await run('switchTo(boxPhones[0])');
  for(let i=0;i<8;i++) await Promise.resolve();
  advance(91_000); await run('loadDevices()');
  assert.equal(nodes['devices-status'].textContent,'deviceConnectTimeout');
  assert.equal(nodes['device-list'].children[0].disabled,false,'timeout allows another request');
  assert.equal(nodes['device-list'].children[0].children.at(-1).textContent,'deviceSwitch');
}
{
  const {context,nodes,run}=setup();
  await run('loadDevices()');
  let finishPost,finishGet;
  context.fetch=(_url,init)=>new Promise(resolve=>{if(init?.method==='POST')finishPost=resolve;else finishGet=resolve;});
  const switching=run('switchTo({...boxPhones[0], active:false})');
  const stale=run('loadDevices()'); // This GET started DURING the POST, not before it.
  finishPost({ok:true,json:async()=>({ok:true})}); await switching;
  finishGet({ok:true,json:async()=>({supported:true,phones:[phone]})}); await stale;
  assert.equal(run('boxPhones[0].active'),false,'a GET from during POST cannot falsely complete the connection');
  assert.equal(nodes['device-list'].children[0].children[1].textContent,'deviceStarting');
}
{
  const {context,nodes,run}=setup();
  context.fetch=async(_url,init)=>({ok:!init?.method,json:async()=>init?.method?{error:'not-paired'}:{supported:true,phones:[{...phone,active:false},{...phone,address:'02:00:00:00:00:02',active:false}]}});
  await run('loadDevices()'); await run('switchTo(boxPhones[0])');
  assert.equal(nodes['devices-status'].textContent,'deviceNotPaired');
  assert.equal(nodes['device-list'].children[0].disabled,false);
  assert.equal(run('boxPhones[0].active'),false);
}
console.log('Connection confirmation: accepted vs live, bounded wait, rejected request and GET during POST passed.');
{
  const {context,nodes,timers,advance,run}=setup();
  context.fetch=async(_url,init)=>({ok:true,json:async()=>init?.method==='POST'?{ok:true}:{supported:true,phones:[{...phone,active:false},{...phone,address:'02:00:00:00:00:02',active:false}]}});
  await run('loadDevices()'); await run('switchTo(boxPhones[0])');
  for(let i=0;i<8;i++) await Promise.resolve();
  context.panel.open=false;
  context.fetch=async()=>({ok:true,json:async()=>({supported:false})});
  await run('loadDevices()');
  assert.equal(nodes['devices-block'].hidden,false,'BlueZ busy must not hide an accepted pending action');
  assert.equal(run('connectingTo'),phone.address);
  context.fetch=async()=>{throw new Error('temporary network loss');};
  const poll=[...timers.values()].find(t=>t.delay===1500);
  assert.ok(poll,'confirmation schedules a follow-up without needing panel interaction');
  poll.callback(); for(let i=0;i<8;i++) await Promise.resolve();
  assert.equal(nodes['devices-block'].hidden,false,'temporary lookup failures keep the pending action visible');
  advance(91_000);
  [...timers.values()].find(t=>t.delay===1500).callback();
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.equal(nodes['devices-status'].textContent,'deviceConnectTimeout');
  assert.equal(nodes['device-list'].children[0].disabled,false);
}
{
  const {context,nodes,run}=setup();
  let wired='available';
  context.fetch=async(_url,init)=>({ok:true,json:async()=>init?.method==='POST'?{ok:true}:{supported:true,phones:[],wired,wiredName:'Synthetic cable'}});
  await run('loadDevices()'); await run('switchTo(null)');
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.equal(nodes['device-list'].children[0].children[1].textContent,'deviceStarting');
  wired='live'; await run('loadDevices()');
  assert.equal(nodes['device-list'].children[0].children[1].textContent,'deviceInUse');
  assert.equal(run('connectingTo'),null);
}
console.log('Connection polling: closed panel, temporary fetch failure, timeout recovery and wired confirmation passed.');
