// Disposable browser exercise of the actual Desktop plugin module. No live Hermes profile.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
const require = createRequire('/Users/djm/.hermes/hermes-agent/package.json')
const { build } = require('esbuild')
const { chromium } = require('playwright')

const root = path.resolve(import.meta.dirname, '..')
const temp = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'receipt-browser-'))
const sdk = path.join(temp, 'sdk.js')
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
 return {png:window.receiptPng,completion:'unverified',warning:'No explicit task manifest; completion unverified. Active '+id}
}};window.requests=[];plugin.register(ctx);createRoot(document.getElementById('root')).render(window.pane.render());`,
  resolveDir:temp, loader:'js'}, outfile:react,bundle:true,format:'iife',platform:'browser',
  nodePaths:['/Users/djm/.hermes/hermes-agent/node_modules'],alias:{'@hermes/plugin-sdk':sdk}})
const png = process.env.RECEIPT_PNG
if (!png || !fs.existsSync(png)) throw Error('RECEIPT_PNG required')
if (!execFileSync('tesseract',[png,'stdout'],{encoding:'utf8'}).includes('COMPLETION: UNVERIFIED'))
  throw Error('Browser fixture must agree with the no-manifest unverified warning')
fs.writeFileSync(path.join(temp,'index.html'),`<!doctype html><html><body><div id="root"></div><script>window.receiptPng='data:image/png;base64,${fs.readFileSync(png).toString('base64')}'</script><script src="react.js"></script></body></html>`)
const browser = await chromium.launch({headless:true, executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'})
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
  await page.waitForTimeout(50)
  await page.getByRole('img',{name:/Public scoped receipt/}).waitFor()
  const state = await page.evaluate(()=>({requests:window.requests, warning:document.body.innerText,
    imgLoaded:document.querySelector('img')?.naturalWidth===652}))
  if (!state.imgLoaded || !state.warning.includes('Active s2') || state.warning.includes('Active s1') ||
      state.requests[0]!=='/current?session_id=s1' || state.requests[1]!=='/current?session_id=s2') throw Error(JSON.stringify(state))
  const screenshot = process.env.RECEIPT_SCREENSHOT || path.join(temp,'browser.png')
  await page.screenshot({path:screenshot,fullPage:true})
  console.log(JSON.stringify({state,screenshot}))
} finally { await browser.close() }
