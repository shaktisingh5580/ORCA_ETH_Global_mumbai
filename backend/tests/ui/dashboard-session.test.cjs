/* Purpose: exercise dashboard account continuity through real UI handlers and isolated HTTP responses.
Behavior: test tabs, reload restoration, refresh races and sign-out without real credentials or services.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A05; ORCA_F02_AGENT_ONBOARDING_HANDOFF.md; Phase: dashboard session repair. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

const folder = path.resolve(__dirname, '../../src/orca/api/dashboard');
const html = fs.readFileSync(path.join(folder, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(folder, 'workspace.js'), 'utf8');
const names = ['research', 'agents', 'review', 'provenance'];
const sessionKey = 'orca.research.session.v1';
const session = (suffix = 'one') => ({access_token:`test-access-${suffix}`, refresh_token:`test-refresh-${suffix}`, expires_in:3600, user:{id:'test-subject',email:'researcher@example.test'}});
const response = (body, status = 200) => ({ok:status < 400, status, json:async () => body});

class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false;
    this.children = []; this.dataset = {}; this._html = '';
    this.classList = {add(){},remove(){},toggle(){}};
  }
  set innerHTML(value) {
    this._html = value;
    const options = this.options;
    if (options.length) this.value = options[0].value;
  }
  get innerHTML() { return this._html; }
  get options() { return [...this._html.matchAll(/<option(?: value="([^"]*)")?[^>]*>([^<]*)<\/option>/g)].map(x=>({value:x[1] ?? x[2],textContent:x[2]})); }
  get selectedOptions() { return this.options.filter(x=>x.value === this.value); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}

async function boot({storage = new Map(), override} = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(x=>[x[1],new Element(x[1])]));
  const tabs = names.map(name=>Object.assign(elements.get(`tab-${name}`),{dataset:{view:name}}));
  const panels = names.map(name=>Object.assign(elements.get(`view-${name}`),{dataset:{viewPanel:name}}));
  const calls = [];
  const context = {
    document:{getElementById:id=>elements.get(id),querySelectorAll:query=>query === '[data-view]' ? tabs : query === '[data-view-panel]' ? panels : []},
    sessionStorage:{getItem:key=>storage.get(key) ?? null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    location:{hash:''}, history:{replaceState(_state,_title,value){context.location.hash=value;}},
    window:{addEventListener(){}}, crypto:{randomUUID}, AbortSignal,
    setTimeout:()=>1, clearTimeout(){}, confirm:()=>false,
    fetch:async (url, options = {}) => {
      calls.push({url,options});
      const intercepted = override && await override(url,options);
      if (intercepted) return intercepted;
      if (url === '/research/config') return response({auth_url:'https://project.example.test/auth/v1',anon_key:'test-public-key',reasoning_configured:true,arkiv_configured:true});
      if (url === '/livez') return response({status:'alive'});
      if (url.includes('/token?')) return response(session(url.includes('refresh_token') ? 'two' : 'one'));
      if (url === '/v1/me') return response({orgs:[{id:'org-one',name:'Institution one'},{id:'org-two',name:'Institution two'}]});
      if (url.startsWith('/v1/research/projects?')) return response({items:[{id:'project-one',name:'Project one'},{id:'project-two',name:'Project two'}]});
      if (url === '/v1/network/config') return response({});
      if (/\/(datasets|reasoning|agents|institutional-agents)(\?|$)/.test(url)) return response([]);
      throw new Error(`Unhandled isolated request: ${url}`);
    },
  };
  const api = await vm.runInNewContext(`(async()=>{${script}\nreturn {state,selectView,authRequest,token,api,clearSession,loadProjects,loadProject};})()`,context);
  return {...api,elements,tabs,panels,calls,storage};
}

async function login(page) {
  page.elements.get('email').value = 'researcher@example.test';
  page.elements.get('password').value = 'test-only-password';
  await page.elements.get('login-form').onsubmit({preventDefault(){},submitter:{disabled:false}});
  assert.equal(page.state.token,'test-access-one');
}

test('all four tab handlers retain the signed-in account and selected project', async () => {
  const page = await boot(); await login(page);
  const calls = page.calls.length;
  for (const [index,tab] of page.tabs.entries()) {
    let prevented = false;
    tab.onclick({preventDefault(){prevented=true;}});
    assert.equal(prevented,true);
    assert.equal(page.state.token,'test-access-one');
    assert.equal(page.state.project,'project-one');
    assert.equal(page.elements.get('account-button').textContent,'Sign out');
    assert.equal(page.elements.get('account-state').textContent,'researcher@example.test');
    assert.equal(page.elements.get('session-state').textContent,'Signed in');
    assert.deepEqual(page.panels.filter(p=>!p.hidden).map(p=>p.id),[`view-${names[index]}`]);
  }
  assert.equal(page.calls.length,calls,'View changes must not restart authentication');
  assert.equal(page.elements.get('password').value,'');
  assert.ok(!page.storage.get(sessionKey).includes('test-only-password'));
});

test('same-tab reload restores and authorizes the account and remembered workspace', async () => {
  const first = await boot(); await login(first);
  first.elements.get('organization').value = 'org-two'; await first.loadProjects();
  first.elements.get('project').value = 'project-two'; await first.loadProject();
  const restored = await boot({storage:first.storage});
  assert.equal(restored.state.token,'test-access-one');
  assert.equal(restored.state.project,'project-two');
  assert.equal(restored.elements.get('organization').value,'org-two');
  assert.equal(restored.elements.get('session-state').textContent,'Signed in');
  assert.equal(restored.calls.filter(c=>c.url.includes('/token?')).length,0);
  assert.ok(restored.calls.some(c=>c.url === '/v1/me'));
});

test('concurrent expired-token requests share one refresh and persist the rotated tokens', async () => {
  const page = await boot(); await login(page); page.state.expires = 0;
  await Promise.all([page.api('/v1/me'),page.api('/v1/me')]);
  assert.equal(page.calls.filter(c=>c.url.includes('grant_type=refresh_token')).length,1);
  assert.equal(page.state.token,'test-access-two');
  assert.equal(JSON.parse(page.storage.get(sessionKey)).refresh_token,'test-refresh-two');
});

test('rejected refresh clears the account and saved session on every view', async () => {
  const page = await boot({override:url=>url.includes('grant_type=refresh_token') && response({msg:'Refresh token expired'},401)});
  await login(page); page.state.expires = 0;
  await assert.rejects(page.token(),/Refresh token expired/);
  assert.equal(page.storage.has(sessionKey),false);
  for (const name of names) {
    page.selectView(name);
    assert.equal(page.state.token,null);
    assert.equal(page.elements.get('session-state').textContent,'Signed out');
    assert.equal(page.elements.get('account-button').textContent,'Sign in');
  }
});

test('temporary refresh network failure preserves the session for a retry', async () => {
  const page = await boot({override:url=>{if(url.includes('grant_type=refresh_token'))throw new TypeError('Offline');}});
  await login(page); page.state.expires = 0;
  await assert.rejects(page.token(),/Offline/);
  assert.equal(page.state.token,'test-access-one');
  assert.equal(page.storage.has(sessionKey),true);
});

test('an API authentication failure clears account state while a permission denial keeps it', async () => {
  let rejectStatus;
  const page = await boot({override:url=>url === '/v1/me' && rejectStatus && response({detail:'Request denied'},rejectStatus)});
  await login(page);
  rejectStatus = 403;
  await assert.rejects(page.api('/v1/me'),/Request denied/);
  assert.equal(page.state.token,'test-access-one');
  assert.equal(page.elements.get('session-state').textContent,'Signed in');
  rejectStatus = 401;
  await assert.rejects(page.api('/v1/me'),/Request denied/);
  assert.equal(page.state.token,null);
  assert.equal(page.state.project,null);
  assert.equal(page.elements.get('organization').disabled,true);
  assert.equal(page.elements.get('session-state').textContent,'Signed out');
  assert.equal(page.storage.has(sessionKey),false);
});

test('a late refresh response cannot resurrect a session after sign-out', async () => {
  let finish, started;
  const ready = new Promise(resolve=>started=resolve);
  const page = await boot({override:url=>{if(url.includes('grant_type=refresh_token')){started();return new Promise(resolve=>finish=resolve);}}});
  await login(page); page.state.expires = 0;
  const refresh = page.token(); const rejected = assert.rejects(refresh,/Sign-in was cancelled/);
  await ready; page.elements.get('account-button').onclick(); finish(response(session('late')));
  await rejected;
  assert.equal(page.state.token,null);
  assert.equal(page.state.project,null);
  assert.equal(page.storage.has(sessionKey),false);
  assert.equal(page.elements.get('session-state').textContent,'Signed out');
});

test('malformed storage and sessions for another auth issuer are discarded', async () => {
  for (const stored of ['invalid-json',JSON.stringify({auth_url:'https://other.example.test',access_token:'test',refresh_token:'test',expires:Date.now()+100000})]) {
    const page = await boot({storage:new Map([[sessionKey,stored]])});
    assert.equal(page.state.token,null);
    assert.equal(page.storage.has(sessionKey),false);
    assert.equal(page.calls.some(c=>c.url === '/v1/me'),false);
  }
});
