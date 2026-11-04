// Exercise the actual generated viewer's adaptation functions with deterministic session/viewport events.
// Run after prepareReceiver/buildWeb; no browser, live phone or user settings are changed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../build/receiver/web/node_modules/typescript/lib/typescript.js';
const root = new URL('../build/receiver/web/', import.meta.url);
const source = readFileSync(process.argv[2] || new URL('src/main.ts', root), 'utf8');
const parsed = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true);
const names = ['adapt', 'autoTarget', 'wantHevc', 'judgeDecoder'];
const functions = names.map(name => {
  const node = parsed.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, `missing ${name}`);
  return node.getText(parsed);
}).join('\n');
const transpile = text => ts.transpileModule(text, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
} }).outputText;
const mathSource = transpile(readFileSync(new URL('src/display-fit.ts', root), 'utf8'));
const math = await import(`data:text/javascript;base64,${Buffer.from(mathSource).toString('base64')}`);
const fixture = `
const { windowResolution, suggestedResolution, aspectChanged, sameStream } = math;
let display={width:840,height:1492,fps:60,uiScale:100,hevc:false};
let active=true, painted=true, displayNegotiating=false, displayBusy=false;
let expectRestart=false, holding=false, wantFullscreen=false, urgentChange='', lastAutoTarget='';
let fastReports=0, slowReports=0, pageFastDecoder=false, hevcDecodes=true;
let calls=[], scheduled=0, flags=new Map();
const window={devicePixelRatio:2}, document={hidden:false,fullscreenElement:null};
const screen={clientWidth:1912,clientHeight:850}, performanceMode={checked:false};
const readFlag=k=>flags.get(k)||'', saveFlag=(k,v)=>flags.set(k,v), fastDecoder=()=>readFlag('f50-fast-decoder')==='1';
let codec='auto';
const el=()=>({value:codec}), performanceOn=()=>false, autoFitOn=()=>true, showDisplay=()=>{}, fit=()=>{};
const error=()=>{throw new Error('unexpected save failure')}, t=s=>s;
const fetchDisplay=async()=>({...display}), scheduleAuto=()=>{scheduled++};
const saveDisplay=async(target,how)=>{calls.push({...target,reason:how.reason});display={...target};displayNegotiating=true;return true};
${functions}
assert.equal(wantHevc(),true,'a fresh browser with HEVC support must not require past fast-decoder history');
hevcDecodes=false; assert.equal(wantHevc(),false,'unsupported HEVC uses H264');
hevcDecodes=true; flags.set('f50-hevc-failed','1'); assert.equal(wantHevc(),false,'real HEVC failure remains respected');
codec='h265'; assert.equal(wantHevc(),true,'explicit H265 can retry after a prior failure');
codec='h264'; assert.equal(wantHevc(),false,'explicit H264 remains authoritative');
codec='auto'; flags.clear();
await adapt();
assert.equal(calls.length,1,'initial shape starts one negotiation');
active=false;painted=false;
for (const height of [932,850,932,850]) { screen.clientHeight=height; await adapt(); }
assert.equal(calls.length,1,'viewport events must not abort the in-flight session');
active=true; // Session active notification is earlier than the first decoded frame.
await adapt();
assert.equal(calls.length,1,'wait for actual video before another automatic negotiation');
painted=true;displayNegotiating=false;
await adapt();
assert.equal(calls.length,1,'latest viewport returned to the requested size: no redundant reconnect');
screen.clientHeight=932;
await adapt();
assert.equal(calls.length,2,'a stable genuinely different shape still applies');
assert.equal(calls[1].height,932);
painted=true;displayNegotiating=false;
const oldTarget=autoTarget();
for (let i=0;i<3;i++) judgeDecoder(3,30);
assert.equal(readFlag('f50-fast-decoder'),'1','remember fast decoding for the next visit');
assert.deepEqual(autoTarget(),oldTarget,'learning fast decoding must not double live resolution');
assert.equal(wantHevc(),true,'HEVC was selected from support before speed learning');
await adapt();
assert.equal(calls.length,2,'fast-decoder learning must not interrupt the picture');
// An explicit codec failure is allowed to recover even without a video frame.
active=false;painted=false;displayNegotiating=true;urgentChange='codec';
screen.clientHeight=850;
await adapt();
assert.equal(calls.length,3,'codec recovery must not deadlock behind the video gate');
// New origin, no fast-decoder hint, but the existing HEVC portrait stream is already painted.
active=true;painted=true;displayNegotiating=false;urgentChange='';lastAutoTarget='';
flags.clear();pageFastDecoder=false;
screen.clientWidth=789;screen.clientHeight=904;
display={width:1578,height:1808,fps:60,uiScale:100,hevc:true};
await adapt();
assert.equal(calls.length,3,'fresh origin must keep the already decoded same-shape stream');
await adapt();
assert.equal(calls.length,3,'later automatic checks must not undo initial adoption');
urgentChange='manual';lastAutoTarget='';
await adapt();
assert.equal(calls.length,4,'explicit quality selection still applies');
console.log('DISPLAY_NEGOTIATION_OK: in-flight resize coalescing, first-frame gate, stable resize, quality learning, codec recovery');
`;
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
await new AsyncFunction('math','assert',transpile(fixture))(math,assert);
