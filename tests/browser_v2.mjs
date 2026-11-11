// Disposable browser exercise of the actual Desktop plugin module. No live Hermes profile.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { execFileSync, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createInterface } from 'node:readline'
const token = randomBytes(32).toString('hex')
const hermes = process.env.HERMES_REPO || '/Users/djm/.hermes/hermes-agent'
const require = createRequire(path.join(hermes, 'package.json'))
const { build } = require('esbuild')
const { chromium } = require('playwright')

const root = path.resolve(import.meta.dirname, '..')
const temp = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'receipt-browser-'))
const sdk = path.join(temp, 'sdk.js')
const server = spawn(process.env.RECEIPT_PYTHON || path.join(hermes, '.venv/bin/python'),
  [path.join(root,'tests/browser_api.py')], {env:{...process.env,HERMES_HOME:path.join(temp,'home'),RECEIPT_TEST_TOKEN:token},stdio:['ignore','pipe','inherit']})
process.on('exit',()=>server.kill())
const port = await new Promise((resolve,reject)=>{
  const lines=createInterface({input:server.stdout});
  lines.once('line',line=>resolve(JSON.parse(line).port));server.once('exit',code=>reject(Error('API exited '+code)))
})
const api='http://127.0.0.1:'+port
let ready=false
for(let n=0;n<60;n++){try{await fetch(api+'/current?session_id=s1');ready=true;break}catch{await new Promise(r=>setTimeout(r,100))}}
if(!ready)throw Error('Disposable API failed readiness')
const auth={Authorization:'Bearer '+token}
const statuses={unauth:(await fetch(api+'/current?session_id=s1')).status,
  missing:(await fetch(api+'/current',{headers:auth})).status,
  crossProfile:(await fetch(api+'/current?session_id=other',{headers:auth})).status}
if(JSON.stringify(statuses)!==JSON.stringify({unauth:401,missing:422,crossProfile:404}))throw Error(JSON.stringify(statuses))
fs.writeFileSync(sdk, `import {useEffect,useState} from 'react';
const state=window.__receiptState ||= {current:null,listeners:[]};
export const host={state:{activeSessionId:state}};
export function useValue(state){const [value,setValue]=useState(state.current);useEffect(()=>{state.listeners.push(setValue);return ()=>{state.listeners=state.listeners.filter(x=>x!==setValue)}},[state]);return value}`)
const react = path.join(temp, 'react.js')
await build({stdin:{contents:`import React from 'react';import{createRoot}from'react-dom/client';
import plugin from ${JSON.stringify(path.join(root, 'desktop/plugin.js'))};import{host}from ${JSON.stringify(sdk)};
window.switchSession=(id)=>{const s=host.state.activeSessionId;s.current=id;s.listeners.forEach(x=>x(id))};
const pending=[];window.resolvePending=()=>pending.shift()?.();
const ctx={register:x=>window.pane=x,rest:async url=>{
 window.requests.push(url);
 const id=new URL('https://local'+url).searchParams.get('session_id');
 if(id==='s1')await new Promise(done=>pending.push(done));
 const response=await fetch(window.apiBase+url,{headers:{Authorization:'Bearer '+window.apiToken}});
 if(!response.ok)throw Error('API HTTP '+response.status);
 const result=await response.json();window.responses.push(id);return {...result,warning:result.warning+' Active '+id}
}};window.requests=[];window.responses=[];plugin.register(ctx);createRoot(document.getElementById('root')).render(window.pane.render());`,
  resolveDir:temp, loader:'js'}, outfile:react,bundle:true,format:'iife',platform:'browser',
  nodePaths:[path.join(hermes, 'node_modules')],alias:{'@hermes/plugin-sdk':sdk}})
const png = process.env.RECEIPT_PNG
if (!png || !fs.existsSync(png)) throw Error('RECEIPT_PNG required')
if (!execFileSync('tesseract',[png,'stdout'],{encoding:'utf8'}).includes('COMPLETION: UNVERIFIED'))
  throw Error('Browser fixture must agree with the no-manifest unverified warning')
fs.writeFileSync(path.join(temp,'index.html'),`<!doctype html><html><body><div id="root"></div><script>window.apiBase=${JSON.stringify(api)};window.apiToken=${JSON.stringify(token)}</script><script src="react.js"></script></body></html>`)
const browser = await chromium.launch({headless:true, executablePath:process.env.RECEIPT_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'})
try {
  const page = await browser.newPage({viewport:{width:740,height:900},deviceScaleFactor:1})
  page.on('pageerror', err => console.error('browser pageerror:',err.message))
  page.on('console', msg => console.error('browser console:',msg.text()))
  await page.goto('file://'+path.join(temp,'index.html'))
  const button = page.getByRole('button',{name:/Get the receipt/})
  await button.waitFor()
  if (await button.isEnabled()) throw Error('Missing active-session guard')
  await page.evaluate(()=>window.switchSession('s1'))
  await button.click()
  await page.waitForFunction(()=>window.requests.length===1)
  await page.evaluate(()=>window.switchSession('s2'))
  await button.click()
  await page.waitForFunction(()=>window.requests.length===2)
  await page.waitForFunction(()=>document.body.innerText.includes('Active s2'))
  await page.evaluate(()=>window.resolvePending())
  await page.waitForFunction(()=>window.responses.includes('s1') && window.responses.includes('s2'))
  await page.waitForTimeout(50)
  await page.getByRole('img',{name:/Public scoped receipt/}).waitFor()
  const state = await page.evaluate(()=>({requests:window.requests, warning:document.body.innerText,
    imgLoaded:document.querySelector('img')?.naturalWidth===652}))
  if (!state.imgLoaded || !state.warning.includes('Active s2') || state.warning.includes('Active s1') ||
      state.requests[0]!=='/current?session_id=s1' || state.requests[1]!=='/current?session_id=s2') throw Error(JSON.stringify(state))
  const screenshot = process.env.RECEIPT_SCREENSHOT || path.join(temp,'browser.png')
  await page.screenshot({path:screenshot,fullPage:true})
  console.log(JSON.stringify({state,statuses,screenshot,scope:'actual router and pane in disposable authenticated harness; not full Desktop host'}))
} finally { await browser.close();server.kill();fs.rmSync(temp,{recursive:true,force:true}) }
