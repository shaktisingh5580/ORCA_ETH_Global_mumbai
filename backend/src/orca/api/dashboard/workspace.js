/* Purpose: connect the monochrome workspace to real ORCA research and Supabase auth APIs.
Behavior: keep tokens in memory, render untrusted data as text, poll actual events and require approvals.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A05; Phase: A05 dashboard increment. */
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => JSON.stringify(value, null, 2);
const detail = (title, value) => `<details><summary>${esc(title)}</summary><pre>${esc(pretty(value))}</pre></details>`;
const badge = value => `<span class="badge">${esc(String(value).replaceAll('_', ' '))}</span>`;
const state = {config:null, token:null, refresh:null, expires:0, project:null, datasets:[], selected:new Set(), run:null, reviews:[], anchors:[], timer:null, generation:0, polling:false};
let refreshPromise = null, noticeTimer, previewMapping = null, lastEvents = '';
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true, 12000); }
function empty(id, message) { $(id).classList.add('empty'); $(id).textContent = message; }
function fill(id, html) { $(id).classList.remove('empty'); $(id).innerHTML = html; }
async function responseBody(response) { let body; try { body = await response.json(); } catch { body = {}; } if (!response.ok) { const message = body.detail || body.error_description || body.msg || body.message || `Request failed (${response.status})`; throw new Error(typeof message === 'string' ? message : pretty(message)); } return body; }
async function authRequest(grant, body) {
  if (!state.config?.auth_url || !state.config?.anon_key) throw new Error('Supabase login is not configured on this server.');
  const response = await fetch(`${state.config.auth_url}/token?grant_type=${grant}`, {method:'POST', headers:{'Content-Type':'application/json',apikey:state.config.anon_key}, body:JSON.stringify(body), signal:AbortSignal.timeout(30000)});
  const session = await responseBody(response); state.token = session.access_token; state.refresh = session.refresh_token; state.expires = Date.now() + session.expires_in * 1000;
  return session;
}
async function token() {
  if (!state.token) throw new Error('Sign in to your workspace first.');
  if (Date.now() > state.expires - 60000) {
    if (!refreshPromise) refreshPromise = authRequest('refresh_token', {refresh_token:state.refresh}).finally(() => refreshPromise = null);
    await refreshPromise;
  }
  return state.token;
}
async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  const access = await token();
  const response = await fetch(path, {method, headers:{Authorization:`Bearer ${access}`, ...(body !== undefined ? {'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()} : {})}, ...(body !== undefined ? {body:JSON.stringify(body)} : {}), signal:AbortSignal.timeout(45000)});
  return responseBody(response);
}
function base() { if (!state.project) throw new Error('Choose a project first.'); return `/v1/projects/${encodeURIComponent(state.project)}`; }
function runBase() { if (!state.run) throw new Error('Choose an investigation first.'); return `${base()}/reasoning/${state.run.id}`; }
async function action(button, work) { if (button.disabled) return; button.disabled = true; try { await work(); } catch(error) { notice(error.message); } finally { button.disabled = false; updateActions(); } }
function updateActions() {
  $('plan-button').disabled = !state.project || !state.selected.size || !state.config?.reasoning_configured;
  $('upload-open').disabled = !state.project;
  $('publish-button').disabled = state.run?.state !== 'completed' || !$('publish-consent').checked || !state.config?.arkiv_configured;
}
function resetProject() {
  state.generation++; state.run = null; state.datasets = []; state.selected.clear(); state.reviews = []; state.anchors = []; lastEvents = ''; clearTimeout(state.timer);
  $('run-select').innerHTML = '<option value="">Choose an investigation</option>'; $('candidate-section').hidden = true; $('mapping-section').hidden = true;
  $('selected-count').textContent = 'No evidence selected'; $('event-count').textContent = '0 events';
  empty('dataset-list','Loading evidence…'); empty('run-content','Select evidence and propose a research plan.'); empty('timeline','Choose an investigation to follow its activity.'); empty('reviews','Complete an approved calculation to request a review.'); empty('anchors','No publication loaded.'); updateActions();
}
async function loadOrganizations() {
  const user = await api('/v1/me');
  $('organization').innerHTML = user.orgs.map(o => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join('') || '<option value="">No authorized organizations</option>';
  $('organization').disabled = false; await loadProjects();
}
async function loadProjects() {
  state.project = null; resetProject(); const organization = $('organization').value;
  if (!organization) { empty('dataset-list','Your account has no active organization membership.'); return; }
  const projects = await api(`/v1/research/projects?organization_id=${encodeURIComponent(organization)}&limit=100`);
  $('project').innerHTML = projects.items.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('') || '<option value="">No accessible projects</option>';
  $('project').disabled = false; await loadProject();
}
async function loadProject() {
  state.project = $('project').value; resetProject(); $('project-title').textContent = $('project').selectedOptions[0]?.textContent || 'Overview';
  if (!state.project) { empty('dataset-list','Create a project in your existing ORCA workspace, then sign in again.'); return; }
  const version = state.generation;
  const [datasets,runs] = await Promise.all([api(`${base()}/datasets`),api(`${base()}/reasoning`)]);
  if (version !== state.generation) return;
  state.datasets = datasets; renderDatasets(); renderHistory(runs); updateActions();
  if (runs.length) await chooseRun(runs[0].id);
}
function renderHistory(runs) {
  $('run-select').innerHTML = '<option value="">Choose an investigation</option>' + runs.map(r => `<option value="${esc(r.id)}">${esc(r.request.question.slice(0,55))} · ${esc(r.state)}</option>`).join('');
  if (state.run) $('run-select').value = state.run.id;
}
function renderDatasets() {
  if (!state.datasets.length) { empty('dataset-list','No datasets yet. Add a CSV or JSON file to begin.'); return; }
  fill('dataset-list', state.datasets.map(d => `<label class="dataset"><input type="checkbox" data-dataset="${esc(d.id)}" ${state.selected.has(d.id)?'checked':''}><span class="file-icon">DATA</span><span class="dataset-info"><span class="dataset-name">${esc(d.name)}</span><br><small>${esc(d.inspection.row_count)} rows · ${esc(d.inspection.columns.length)} columns</small></span>${badge(d.source_mode)}</label>`).join(''));
  $('mapping-section').hidden = false;
  const prior = $('mapping-dataset').value;
  $('mapping-dataset').innerHTML = state.datasets.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  if (state.datasets.some(d => d.id === prior)) $('mapping-dataset').value = prior;
  mappingColumns();
}
function mappingColumns() {
  const d = state.datasets.find(d => d.id === $('mapping-dataset').value); if (!d) return;
  const options = '<option value="">Choose column</option>' + d.inspection.columns.map(c => `<option>${esc(c)}</option>`).join('');
  $('timestamp-column').innerHTML = options; $('value-column').innerHTML = options; previewMapping = null; $('save-mapping').disabled = true;
  $('mapping-preview').innerHTML = detail('Original rows and missing values',d.inspection);
}
function mapping() { return {timestamp_column:$('timestamp-column').value,value_column:$('value-column').value,unit:$('unit').value,naive_timezone:$('timezone').value||null,duplicate_policy:$('duplicates').value}; }
async function chooseRun(id) {
  if (!id) return; const version = ++state.generation; clearTimeout(state.timer);
  const run = await api(`${base()}/reasoning/${encodeURIComponent(id)}`); if (version !== state.generation) return;
  state.run = run; $('run-select').value = id; renderRun(true); await refreshRun();
}
function renderRun(resetCandidate = false) {
  const r = state.run; if (!r) return;
  const option = [...$('run-select').options].find(o=>o.value===r.id);
  if (option) option.textContent = `${r.request.question.slice(0,55)} · ${r.state}`;
  let html = `<div class="run-state">${badge(r.state)}<span class="hash">${esc(r.id)}</span></div><h3>${esc(r.request.question)}</h3>`;
  if (['queued','planning'].includes(r.state)) html += '<p class="muted">The planner is inspecting the selected evidence. Actual tool activity appears in the timeline.</p>';
  if (r.error_code) html += `<p>Could not complete this run: <strong>${esc(r.error_code)}</strong>. You can submit a new investigation to retry.</p>`;
  if (r.plan) {
    html += `<p>${esc(r.plan.summary)}</p>` + (r.plan.missing_evidence.length ? `<h3>Evidence needed</h3><ul>${r.plan.missing_evidence.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : '');
    html += detail('Proposed mappings and limitations',r.plan);
    if (r.state === 'planned') html += '<p class="small muted">Approval uses these exact mappings and units to run deterministic calculations.</p><button id="approve-plan">Approve plan & calculate</button>';
  }
  if (r.result) {
    html += '<div class="result-grid">' + r.result.artifacts.map(a => `<div class="metric"><span>${esc(state.datasets.find(d=>d.id===a.dataset_id)?.name||a.role)}</span><strong>${Number(a.summary.mean_celsius).toFixed(3)} <small>°C</small></strong><span>${esc(a.summary.count ?? a.summary.observation_count ?? '')} observations · ${esc(a.source_mode)}</span></div>`).join('') + (r.result.difference_celsius !== undefined ? `<div class="metric"><span>Difference of means</span><strong>${Number(r.result.difference_celsius).toFixed(3)} °C</strong><span>Current − reference</span></div>` : '') + '</div>';
    html += '<p class="small muted">Observation means, not an area-weighted climate estimate. Prototype results require scientific validation.</p>' + detail('Calculation evidence and digests',r.result);
  }
  fill('run-content',html);
  $('approve-plan')?.addEventListener('click',e=>action(e.currentTarget,async()=>{state.run=await api(`${runBase()}/approve`,{approve:true,plan_digest:state.run.plan_digest});renderRun(true);await refreshRun();}));
  if (resetCandidate || (!$('candidate-means').children.length && r.result)) renderCandidate(); updateActions();
}
function renderCandidate() {
  const result = state.run?.result; $('candidate-section').hidden = !result; if (!result) return;
  $('candidate-label').value = 'Computed result for review'; $('candidate-mode').value = result.artifacts.some(a=>a.source_mode==='synthetic_demo')?'synthetic_demo':'computed';
  $('candidate-means').innerHTML = result.artifacts.map(a=>`<label>${esc(state.datasets.find(d=>d.id===a.dataset_id)?.name||a.role)} · mean °C<input type="number" step="any" required data-mean="${esc(a.dataset_id)}" value="${esc(a.summary.mean_celsius)}"></label>`).join('');
}
function renderReviews() {
  if (!state.reviews.length) { empty('reviews',state.run?.result?'No reviews submitted yet.':'Complete an approved calculation to request a review.'); return; }
  fill('reviews',state.reviews.map(r=>`<article class="review-card"><div>${badge(r.decision?.reviewer?.verdict||r.state)} ${badge(r.candidate.source_mode)}</div><h3>${esc(r.candidate.label)}</h3>${r.parent_review_id?`<p class="hash">Correction of ${esc(r.parent_review_id)}</p>`:''}${r.error_code?`<p>Review failed: ${esc(r.error_code)}</p>`:''}<p>${esc(r.decision?.reviewer?.summary||'Review queued or in progress…')}</p>${(r.findings||[]).map(f=>`<div class="finding"><strong>${esc(f.code||f.kind||f.id)}</strong><br>${esc(pretty(f))}</div>`).join('')}${r.decision?.coordinator?`<h3>Coordinator proposal</h3><p>${esc(r.decision.coordinator.summary)}</p>`:''}${r.state==='completed' && r.decision?.coordinator?.action==='recompute_approved_plan'?`<button class="secondary" data-correct="${esc(r.id)}">Approve correction & review again</button>`:''}${detail('Candidate, findings and decision',r)}</article>`).join(''));
}
function renderAnchors() {
  if (!state.anchors.length) { empty('anchors','Nothing published for this investigation.'); return; }
  fill('anchors',state.anchors.map(a=>`<article class="anchor-card">${badge(a.state)}<h3>Evidence attestation</h3><p class="hash">Manifest ${esc(a.manifest_digest)}</p>${a.receipt?`<p class="hash">Entity ${esc(a.receipt.entity_key)}<br>Transaction ${esc(a.receipt.tx_hash||'See retained receipt')}</p><button class="secondary" data-verify="${esc(a.id)}">Verify with a fresh Arkiv read</button>`:''}${a.error_code?`<p>${esc(a.error_code)}. The retained record needs operator attention; it is not marked published.</p>`:''}${detail('Signed record, manifest and receipt',a)}</article>`).join(''));
}
function renderEvents(events) {
  const encoded = pretty(events); if (lastEvents === encoded) return; lastEvents = encoded;
  $('event-count').textContent = `${events.length} events`;
  if (!events.length) { empty('timeline','Waiting for the first recorded activity…'); return; }
  fill('timeline',events.map(e=>`<article class="event"><time>${esc(new Date(e.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}))}</time><div class="event-title">${esc(e.event_type.replaceAll('_',' '))}</div><p>${esc(e.data.role||e.data.tool||e.data.name||e.data.model||'')}</p>${detail('Inspect event',e.data)}</article>`).join(''));
}
async function refreshRun() {
  if (!state.run || !state.token) return;
  if (state.polling) { clearTimeout(state.timer); state.timer=setTimeout(refreshRun,1000); return; }
  state.polling=true; const version=state.generation, path=runBase();
  try {
    const [run,first,reviews,anchors] = await Promise.all([api(path),api(`${path}/events`),api(`${path}/reviews`),api(`${path}/anchors`)]);
    if (version !== state.generation) return;
    const events=[...first.events];
    while (events.length && events.length%200===0 && events.length<2000) { const next=await api(`${path}/events?after=${events.at(-1).id}`); events.push(...next.events); if(next.events.length<200) break; }
    if (version !== state.generation) return;
    const changed=pretty(state.run)!==pretty(run), reviewsChanged=pretty(state.reviews)!==pretty(reviews), anchorsChanged=pretty(state.anchors)!==pretty(anchors);
    state.run=run;state.reviews=reviews;state.anchors=anchors;
    if(changed) renderRun(); renderEvents(events); if(reviewsChanged) renderReviews(); if(anchorsChanged) renderAnchors();
  } catch(error) { if(version===state.generation) notice(error.message); }
  finally { state.polling=false; if(state.run&&state.token){clearTimeout(state.timer);state.timer=setTimeout(refreshRun,5000);} }
}
function bind() {
  $('account-button').onclick=()=>{if(state.token){state.token=null;state.refresh=null;state.project=null;resetProject();$('account-button').textContent='Sign in';$('account-state').textContent='Signed out';$('organization').disabled=true;$('project').disabled=true;empty('dataset-list','Sign in to load your evidence.');}else $('login-dialog').showModal();};
  $('login-close').onclick=()=>$('login-dialog').close();
  $('login-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('login-error').textContent='';try{const session=await authRequest('password',{email:$('email').value,password:$('password').value});$('password').value='';$('account-state').textContent=session.user.email;$('account-button').textContent='Sign out';$('login-dialog').close();await loadOrganizations();}catch(error){$('login-error').textContent=error.message;notice(error.message);}finally{button.disabled=false;}};
  $('organization').onchange=()=>loadProjects().catch(e=>notice(e.message));$('project').onchange=()=>loadProject().catch(e=>notice(e.message));
  $('upload-open').onclick=()=>$('upload-dialog').showModal();$('upload-close').onclick=()=>$('upload-dialog').close();
  $('dataset-file').onchange=()=>{if(!$('dataset-name').value)$('dataset-name').value=$('dataset-file').files[0]?.name||'';};
  $('upload-form').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{const f=$('dataset-file').files[0];if(!f||f.size>5242880)throw new Error('Choose a file no larger than 5 MB.');if(!/\.(csv|json)$/i.test(f.name))throw new Error('Choose a CSV or JSON file.');const data=await api(`${base()}/datasets`,{name:$('dataset-name').value,format:f.name.toLowerCase().endsWith('.json')?'json':'csv',content:await f.text(),source_mode:$('dataset-mode').value});state.datasets=await api(`${base()}/datasets`);if(state.selected.size<4)state.selected.add(data.id);renderDatasets();selectedCount();$('upload-dialog').close();$('upload-form').reset();notice('Evidence uploaded. Original bytes are preserved.');});};
  $('dataset-list').onchange=e=>{const id=e.target.dataset.dataset;if(!id)return;if(e.target.checked){if(state.selected.size>=4){e.target.checked=false;notice('Choose at most four datasets.');return;}state.selected.add(id);}else state.selected.delete(id);selectedCount();};
  $('mapping-dataset').onchange=mappingColumns;
  $('mapping-form').onchange=()=>{previewMapping=null;$('save-mapping').disabled=true;};
  $('mapping-form').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{const value=mapping();const result=await api(`${base()}/datasets/${$('mapping-dataset').value}/preview`,value);$('mapping-preview').innerHTML=detail('Normalization preview',result);$('mapping-preview').querySelector('details').open=true;previewMapping={dataset:$('mapping-dataset').value,mapping:value};$('save-mapping').disabled=false;});};
  $('save-mapping').onclick=e=>action(e.currentTarget,async()=>{if(!previewMapping)throw new Error('Preview the mapping first.');const result=await api(`${base()}/datasets/${previewMapping.dataset}/normalizations`,{mapping:previewMapping.mapping,approve:true});$('mapping-preview').innerHTML=detail('Approved immutable normalization',result);notice('Normalization saved. Include the same mapping details in your research question.');});
  $('question-form').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{const run=await api(`${base()}/reasoning`,{question:$('question').value,dataset_ids:[...state.selected]});renderHistory(await api(`${base()}/reasoning`));await chooseRun(run.id);});};
  $('run-select').onchange=()=>chooseRun($('run-select').value).catch(e=>notice(e.message));
  $('submit-review').onclick=e=>action(e.currentTarget,async()=>{const means={};for(const input of document.querySelectorAll('[data-mean]')){if(input.value===''||!Number.isFinite(Number(input.value)))throw new Error('Every candidate mean must be a finite number.');means[input.dataset.mean]=Number(input.value);}await api(`${runBase()}/reviews`,{label:$('candidate-label').value,source_mode:$('candidate-mode').value,plan_digest:state.run.plan_digest,means_celsius:means,evidence_digests:Object.fromEntries(state.run.result.artifacts.map(a=>[a.dataset_id,a.artifact_digest]))});notice('Candidate submitted for an evidence-based review.');await refreshRun();});
  $('reviews').onclick=e=>{const b=e.target.closest('[data-correct]');if(!b)return;action(b,async()=>{const r=state.reviews.find(r=>r.id===b.dataset.correct);await api(`${runBase()}/reviews/${r.id}/correct`,{approve:true,candidate_digest:r.candidate_digest});notice('Correction approved as a new candidate. Original evidence remains unchanged.');await refreshRun();});};
  $('publish-consent').onchange=updateActions;
  $('publish-button').onclick=e=>action(e.currentTarget,async()=>{await api(`${runBase()}/anchors`,{approve_public_reference:true});$('publish-consent').checked=false;notice('Publication queued. A receipt will appear only after a verified Arkiv read-back.');await refreshRun();});
  $('anchors').onclick=e=>{const b=e.target.closest('[data-verify]');if(!b)return;action(b,async()=>{const result=await api(`${runBase()}/anchors/${b.dataset.verify}/verify`);notice(`Fresh chain read: ${result.verification}.`);});};
}
function selectedCount(){ $('selected-count').textContent=`${state.selected.size} dataset${state.selected.size===1?'':'s'} selected`;updateActions(); }
bind();updateActions();
try {const [config,health]=await Promise.all([fetch('/research/config').then(responseBody),fetch('/livez').then(responseBody)]);state.config=config;$('connection-state').textContent=health?'Backend connected':'Backend unavailable';if(!config.reasoning_configured)notice('Gemini is not configured on this server. Existing evidence remains available.');updateActions();}catch(error){$('connection-state').textContent='Backend unavailable';notice(error.message);}
