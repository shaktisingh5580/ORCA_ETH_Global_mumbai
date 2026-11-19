/* Purpose: exercise dashboard account continuity through real UI handlers and isolated HTTP responses.
Behavior: test tabs, reload restoration, refresh races and sign-out without real credentials or services.
Refs: main_implementation plan.md P10.4; ORCA_F02_AGENT_ONBOARDING_HANDOFF.md. */
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
    this.isConnected = true;
    this.classList = {add(){},remove(){},toggle(){}};
  }
  set innerHTML(value) {
    for (const child of this.children) child.isConnected = false;
    this.children = [];
    this._html = value;
    const options = this.options;
    if (options.length) this.value = options[0].value;
  }
  get innerHTML() { return this._html; }
  append(child) { this.children.push(child); }
  querySelector(query) {
    this.fields ??= new Map([...this._html.matchAll(/\bid="([^"]+)"([^>]*)>/g)]
      .map(match => [match[1], Object.assign(new Element(match[1]), {disabled:/\bdisabled\b/.test(match[2])})]));
    return this.fields.get(query.slice(1));
  }
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
    document:{createElement:tag=>new Element(tag),getElementById:id=>elements.get(id),querySelectorAll:query=>query === '[data-view]' ? tabs : query === '[data-view-panel]' ? panels : []},
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
      if (url.startsWith('/v1/network/agents?')) return response({items:[]});
      if (/\/(datasets|reasoning|agents|institutional-agents)(\?|$)/.test(url)) return response([]);
      throw new Error(`Unhandled isolated request: ${url}`);
    },
  };
  const api = await vm.runInNewContext(`(async()=>{${script}\nreturn {state,selectView,authRequest,token,api,clearSession,loadProjects,loadProject,loadNetwork,renderEnsObservation, mountIdentity(agent) { selectedAgent = agent; renderAgent(); mountAgentIdentity(agent); }};})()`,context);
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

test('network directory renders paginated service eligibility when coordination is unavailable', async () => {
  const page = await boot({override:url=>url === '/v1/network/config' ? response({detail:'Coordination is not configured'},503) : url.startsWith('/v1/network/agents?') && response({items:[{name:'Ocean provider',capability:'temperature_summary_v1',provider_mode:'self_declared_external',service_eligible:true,ens_verified:false,research_dispatch_enabled:false}]})});
  await login(page); await page.loadNetwork();
  const listing = page.elements.get('network-agents').innerHTML;
  assert.match(listing,/Ocean provider/);
  assert.match(listing,/Service qualified · ENS not verified · Research dispatch disabled/);
  assert.doesNotMatch(listing,/Network unavailable/);
  assert.match(page.elements.get('network-profile').textContent,/Coordination profile unavailable/);
  const observation = page.renderEnsObservation({status:'NAME_OBSERVED_NOT_AUTHORIZED',hierarchy:[{owner:'0x123'}],block_number:'42',registration_matches:{endpoint:false,public_key:false,card_digest:false},authority_verified:false,research_dispatch_enabled:false});
  assert.match(observation,/Name active on finalized Sepolia state/);
  assert.match(observation,/Agent authority not verified/);
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

test('identity panel reads permission roles and submits only signatures for its issued challenge', async () => {
  const agent = {id:'agent-one', state:'active', version:2, registration:{name:'Ocean'}};
  const challengeId = randomUUID();
  const evidence = {agent_id:agent.id, observed_block:123, status:'SIGNATURES_VALID'};
  const delegate = '0x' + '1'.repeat(40);
  const page = await boot({override:url => {
    if (url.endsWith('/endpoint-permissions')) return response({
      status:'DISTINCT_RESOLVERS', delegate, observed_block:123,
      names:[{name:'analyst.example.eth',resolver:delegate,endpoint_text_role:true},
        {name:'<img>',resolver:delegate,endpoint_text_role:false}],
    });
    if (url.endsWith('/agent-bindings/challenges')) return response({
      id:challengeId, manifest:{expiresAt:Math.floor(Date.now()/1000)+300},
    }, 201);
    if (url.endsWith('/agent-bindings') || url.includes(`/agent-bindings/${challengeId}`)) return response(evidence);
  }});
  await login(page);
  page.mountIdentity(agent);
  const panel = page.elements.get('agent-detail').children[0];
  const field = id => panel.querySelector(`#agent-${id}`);
  field('ens-name').value = 'analyst.example.eth';
  field('ens-peer').value = 'reviewer.example.eth';
  field('ens-delegate').value = delegate;
  await field('ens-permissions').onclick();
  assert.deepEqual(JSON.parse(page.calls.at(-1).options.body), {
    organization_id:'org-one', ens_chain_id:11155111,
    first_name:'analyst.example.eth', second_name:'reviewer.example.eth', delegate,
  });
  assert.match(field('ens-permissions-result').innerHTML, /Endpoint text role: present/);
  assert.match(field('ens-permissions-result').innerHTML, /&lt;img&gt;/);
  assert.doesNotMatch(field('ens-permissions-result').innerHTML, /<img>/);

  field('binding-wallet').value = delegate;
  await field('binding-create').onclick();
  assert.deepEqual(JSON.parse(page.calls.at(-1).options.body), {
    organization_id:'org-one', agent_id:agent.id, expected_version:2, ens_chain_id:11155111,
    name:'analyst.example.eth', arkiv_wallet:delegate,
  });
  assert.equal(field('binding-id').value, challengeId);
  const signatures = {controller:'0x'+'a'.repeat(130), wallet:'0x'+'b'.repeat(130), service:'c'.repeat(86)};
  field('binding-signatures').value = JSON.stringify({...signatures, private_key:'invalid'});
  await field('binding-submit').onclick();
  assert.match(field('binding-result').textContent, /Expected controller and wallet signatures/);
  assert.match(page.calls.at(-1).url, /\/challenges$/);
  field('binding-signatures').value = JSON.stringify(signatures);
  await field('binding-submit').onclick();
  assert.deepEqual(JSON.parse(page.calls.at(-1).options.body), {
    organization_id:'org-one', challenge_id:challengeId, ...signatures,
  });
  assert.equal(field('binding-submit').disabled, true);
  assert.equal(field('binding-signatures').value, '');
  await field('binding-read').onclick();
  assert.equal(page.calls.at(-1).url, `/v1/identity/agent-bindings/${challengeId}?organization_id=org-one`);
  assert.match(field('binding-result').innerHTML, /Binding signatures verified.*Research dispatch disabled/);
});

test('switching agents during a permission check ignores the detached response', async () => {
  let released, started;
  const ready = new Promise(resolve => started = resolve);
  const page = await boot({override:url => {
    if (url.endsWith('/endpoint-permissions')) {
      started();
      return new Promise(resolve => released = resolve);
    }
  }});
  await login(page);
  page.mountIdentity({id:'first', state:'active', version:1, registration:{name:'First'}});
  const container = page.elements.get('agent-detail');
  const panel = container.children[0];
  panel.querySelector('#agent-ens-name').value = 'analyst.example.eth';
  const pending = panel.querySelector('#agent-ens-permissions').onclick();
  await ready;
  page.mountIdentity({id:'second', state:'draft', version:1, registration:{name:'Second'}});
  released(response({names:[], observed_block:123}));
  await pending;
  const nextPanel = container.children[0];
  assert.equal(nextPanel.querySelector('#agent-ens-permissions-result').innerHTML, '');
  assert.equal(nextPanel.querySelector('#agent-binding-create').disabled, true);
  assert.equal(panel.querySelector('#agent-ens-permissions-result').innerHTML, '');
});
