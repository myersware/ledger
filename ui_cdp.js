// tiny headless-chromium driver: node ui_cdp.js <debug-port> <base-url> <steps.json>; steps = [[name, hash|null, js-expression]]; prints JSON results + page errors
const [port, base, stepsFile] = process.argv.slice(2), steps = JSON.parse(require('fs').readFileSync(stepsFile, 'utf8'));
(async () => {
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {method: 'PUT'})).json();
  const ws = new WebSocket(tabs.webSocketDebuggerUrl); await new Promise(r => ws.onopen = r);
  let id = 0, pend = {}, errors = []; ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && pend[d.id]) pend[d.id](d); else if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text); };
  const send = (method, params = {}) => new Promise(r => { pend[++id] = r; ws.send(JSON.stringify({id, method, params})); });
  await send('Runtime.enable'); await send('Page.enable'); await send('Page.navigate', {url: base + '/'}); await new Promise(r => setTimeout(r, 800));
  const out = [];
  for (const [name, hash, js] of steps) {
    if (hash) { await send('Runtime.evaluate', {expression: `location.hash=${JSON.stringify(hash)};route()`, awaitPromise: true}); await new Promise(r => setTimeout(r, 500)); }
    const r = await send('Runtime.evaluate', {expression: `(async()=>{${js}})()`, awaitPromise: true, returnByValue: true});
    out.push([name, r.result.exceptionDetails ? 'EXC ' + (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text) : r.result.result.value]);
    await new Promise(r => setTimeout(r, 300));
  }
  console.log(JSON.stringify({out, errors})); ws.close(); process.exit(0);
})();
