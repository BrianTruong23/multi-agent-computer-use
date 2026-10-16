const $ = (id) => document.getElementById(id);
const BUILTIN_DEFAULT_TASK_ID = '0326d92d-d218-48a8-9ca1-981cd6d064c7';
const state = { tasks: [], selected: new Set(), selectionInitialized: false, domain: 'all', job: null, results: [], liveAgents: [], checks: [], models: {}, estimate: null, budget: null, vmView: 'graph', graphScale: 1, graphSignature: '', imageScale: 1, liveStream: null, liveConnected: false };
const fields = ['osworld-root','result-root','vm-provider','manager-provider','manager-model','cua-provider','cua-model','max-replans','max-steps','max-parallelism','max-cost-usd'];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}
function value(id) { return $(id).value.trim(); }
function runMode() { return document.querySelector('input[name="run-mode"]:checked')?.value || 'macu'; }
function currentRunMode() {
  if (state.job?.run_mode) return state.job.run_mode;
  if (state.job?.command?.length) return state.job.command.includes('--no-manager') ? 'single' : 'macu';
  return runMode();
}
function saveSettings() {
  const saved = {};
  fields.forEach(id => saved[id] = $(id).value);
  localStorage.setItem('macu-dashboard-settings', JSON.stringify(saved));
}
function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem('macu-dashboard-settings') || '{}');
    fields.forEach(id => { if (saved[id] !== undefined) $(id).value = saved[id]; });
  } catch (_) {}
}
async function jsonFetch(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function refreshSetup() {
  saveSettings();
  $('checks').innerHTML = '<div class="empty">Checking environment…</div>';
  const params = new URLSearchParams({provider: value('vm-provider')});
  if (value('osworld-root')) params.set('osworld_root', value('osworld-root'));
  try {
    const data = await jsonFetch(`/api/tasks?${params}`);
    if (!value('osworld-root')) $('osworld-root').value = data.osworld_root;
    state.tasks = data.tasks;
    state.checks = data.checks;
    state.models = data.models || {};
    const singleInput = document.querySelector('input[name="run-mode"][value="single"]');
    const modesReady = (data.run_modes || []).includes('single');
    singleInput.disabled = !modesReady;
    singleInput.closest('.mode-option').classList.toggle('locked', !modesReady);
    if (!modesReady && runMode() === 'single') {
      document.querySelector('input[name="run-mode"][value="macu"]').checked = true;
      updateRunMode();
    }
    if (data.live_stream) startLiveStream();
    populateModels();
    const known = new Set(state.tasks.map(t => t.key));
    if (!state.selectionInitialized) {
      let savedDefaults = [];
      try { savedDefaults = JSON.parse(localStorage.getItem('macu-dashboard-default-tasks') || '[]'); } catch (_) {}
      const requestedDefaults = Array.isArray(savedDefaults) && savedDefaults.length
        ? savedDefaults
        : state.tasks.filter(task => task.id === BUILTIN_DEFAULT_TASK_ID).map(task => task.key);
      state.selected = new Set(requestedDefaults.filter(key => known.has(key)));
      state.selectionInitialized = true;
    } else {
      state.selected = new Set([...state.selected].filter(k => known.has(k)));
    }
    renderChecks(); renderTasks(); renderModelCatalog(); updateEstimate(); updateLaunchButton();
  } catch (error) {
    $('checks').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}
function renderChecks() {
  $('checks').innerHTML = state.checks.map(check => `
    <div class="check ${check.ok ? 'ok' : 'bad'}"><i></i><div><strong>${escapeHtml(check.id.replace('_',' '))}</strong><span>${escapeHtml(check.detail)}</span></div></div>`).join('');
  const failed = state.checks.filter(c => !c.ok);
  const callout = $('readiness-callout');
  if (failed.length) {
    callout.classList.add('show');
    callout.textContent = `${failed.length} readiness check${failed.length > 1 ? 's' : ''} failed. You can browse tasks, but fix these before launching a real run.`;
  } else {
    callout.classList.remove('show');
  }
}
function visibleTasks() {
  const q = value('task-search').toLowerCase();
  return state.tasks.filter(task =>
    (state.domain === 'all' || task.domain === state.domain || (state.domain === 'shared_canvas' && task.group === 'shared_canvas')) &&
    (!q || `${task.id} ${task.instruction}`.toLowerCase().includes(q))
  );
}
function renderTasks() {
  const tasks = visibleTasks();
  $('selected-count').textContent = state.selected.size;
  if (!state.tasks.length) {
    $('tasks').innerHTML = '<div class="empty">No GIMP or LibreOffice Calc examples found. Verify the OSWorld root and its <code>evaluation_examples/examples/</code> directory.</div>';
    return;
  }
  if (!tasks.length) {
    $('tasks').innerHTML = '<div class="empty">No tasks match this filter.</div>';
    return;
  }
  $('tasks').innerHTML = tasks.map(task => `
    <label class="task-row ${state.selected.has(task.key) ? 'selected' : ''}" data-key="${escapeHtml(task.key)}">
      <input type="checkbox" ${state.selected.has(task.key) ? 'checked' : ''}>
      <div><span class="task-domain">${escapeHtml(task.domain.replace('libreoffice_','LibreOffice '))}</span><span class="task-id">${escapeHtml(task.id)}</span>${task.group === 'shared_canvas' ? `<span class="shared-tag">Shared canvas · ${task.confidence} · ${task.suggested_agents} agents</span>` : ''}</div>
      <div class="task-instruction">${escapeHtml(task.instruction)}</div>
    </label>`).join('');
  document.querySelectorAll('.task-row').forEach(row => {
    row.querySelector('input').addEventListener('change', event => {
      event.target.checked ? state.selected.add(row.dataset.key) : state.selected.delete(row.dataset.key);
      renderTasks(); updateEstimate(); updateLaunchButton();
    });
  });
}
function updateLaunchButton() {
  const running = !!state.job?.running;
  const blockers = state.checks.filter(check => !check.ok);
  const ready = state.checks.length > 0 && blockers.length === 0;
  const overBudget = state.estimate && !state.estimate.within_cap;
  $('launch').disabled = running || !ready || state.selected.size === 0 || overBudget;
  $('stop').disabled = !running;
  const reasons = [];
  if (blockers.length) reasons.push(`setup: ${blockers.map(check => check.id.replace('_',' ')).join(', ')}`);
  if (!state.selected.size) reasons.push('no task selected');
  if (overBudget) reasons.push('estimate exceeds cap');
  $('launch').title = reasons.length ? `Launch blocked — ${reasons.join('; ')}` : 'Launch selected tasks';
  if (!running && reasons.length) $('launch-message').textContent = `Blocked: ${reasons.join('; ')}`;
  else if ($('launch-message').textContent.startsWith('Blocked:')) $('launch-message').textContent = '';
}

async function launch() {
  saveSettings();
  $('launch-message').textContent = 'Starting…';
  const payload = {
    osworld_root: value('osworld-root'), result_root: value('result-root'), vm_provider: value('vm-provider'),
    manager_provider: value('manager-provider'), manager_model: value('manager-model'),
    cua_provider: value('cua-provider'), cua_model: value('cua-model'),
    max_replans: Number(value('max-replans')), max_steps: Number(value('max-steps')),
    max_parallelism: Number(value('max-parallelism')), max_cost_usd: Number(value('max-cost-usd')),
    run_mode: runMode(), task_keys: [...state.selected]
  };
  try {
    const data = await jsonFetch('/api/launch', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    state.job = data.job;
    $('launch-message').textContent = `Started job ${data.job.id}`;
    renderJob();
  } catch (error) {
    $('launch-message').textContent = error.message;
  }
}
async function stop() {
  await jsonFetch('/api/stop', {method:'POST'});
  $('launch-message').textContent = 'Stop signal sent.';
}
async function refreshStatus() {
  const params = new URLSearchParams();
  if (value('result-root')) params.set('result_root', value('result-root'));
  try {
    const data = await jsonFetch(`/api/status?${params}`);
    state.job = data.job; state.results = data.results; state.budget = data.budget;
    renderJob(); renderResults();
  } catch (_) {}
}
function applyLiveAgents(agents) {
  state.liveAgents = agents || [];
  if (currentRunMode() === 'macu' && state.job?.running && Number(state.job.elapsed_seconds || 0) < 120 && state.liveAgents.length === 1 && !(state.liveAgents[0].dependencies || []).length && String(state.liveAgents[0].description || '').length > 140) state.liveAgents = [];
  renderLiveAgents();
  if (state.job) renderJob();
}
function startLiveStream() {
  if (state.liveStream) state.liveStream.close();
  const params = new URLSearchParams();
  if (value('result-root')) params.set('result_root', value('result-root'));
  const stream = new EventSource(`/api/live-stream?${params}`);
  state.liveStream = stream;
  stream.addEventListener('workers', event => {
    const data = JSON.parse(event.data); state.liveConnected = true; applyLiveAgents(data.agents);
  });
  stream.onerror = () => { state.liveConnected = false; renderLiveAgents(); };
}
async function refreshLiveAgents() {
  const params = new URLSearchParams();
  if (value('result-root')) params.set('result_root', value('result-root'));
  try {
    const data = await jsonFetch(`/api/live-agents?${params}`);
    applyLiveAgents(data.agents);
  } catch (_) {
    // A run may already be active while the dashboard is being upgraded. In
    // that case, consume the temporary static live feed until the next restart.
    const feeds = await Promise.all((state.results || []).map(async result => {
      try {
        const response = await fetch(`/view/${encodeURIComponent(result.task_id)}/live_agents.json?v=${Date.now()}`);
        return response.ok ? (await response.json()).agents || [] : [];
      } catch (_) { return []; }
    }));
    state.liveAgents = feeds.flat();
    renderLiveAgents();
  }
}
function agentConditions(agent = {}) {
  const model = agent.model || value('cua-model') || 'Unknown model';
  const receivesVision = agent.receives_vision ?? !!state.models[model]?.image_input;
  return {
    model, receivesVision,
    observation: agent.observation || 'screenshot',
    actionSpace: agent.action_space || 'pyautogui',
    coordinates: agent.coordinate_mode || (state.job?.running ? 'runtime config' : 'absolute'),
    resolution: agent.screen_resolution || '1920x1080',
    maxSteps: agent.max_steps || Number(value('max-steps')) || 60,
    provider: agent.provider || value('cua-provider') || 'tinker',
  };
}
function cursorMarkup(agent) {
  if (!agent.cursor) return '';
  const [width,height] = String(agent.screen_resolution || '1920x1080').split('x').map(Number);
  const x = Number(agent.cursor.x), y = Number(agent.cursor.y);
  if (!width || !height || x < 0 || y < 0 || x > width || y > height) return '';
  return `<span class="live-cursor" style="left:${(x/width)*100}%;top:${(y/height)*100}%" title="Cursor at ${Math.round(x)}, ${Math.round(y)}"><i></i></span>`;
}
function actionHistoryMarkup(agent) {
  const history = agent.action_history || [];
  if (!history.length) return '';
  return `<details class="action-history"><summary>Action timeline · ${history.length} recent</summary><ol>${history.slice().reverse().map(item => `<li><b>Step ${escapeHtml(item.step)}</b><code>${escapeHtml(item.action || item.activity || 'Observe')}</code></li>`).join('')}</ol></details>`;
}
function liveAgentCards() {
  const root = value('result-root');
  return state.liveAgents.map(agent => {
    const frameParams = new URLSearchParams({result_root: root, v: String(agent.frame_version || 0)});
    const frameUrl = agent.frame_url || `/api/live-frame/${encodeURIComponent(agent.task_id)}/${encodeURIComponent(agent.agent_id)}?${frameParams}`;
    const screen = agent.frame_version
      ? `<img src="${escapeHtml(frameUrl)}" alt="Live screen for ${escapeHtml(agent.agent_id)}">${cursorMarkup(agent)}`
      : '<span>Waiting for first screenshot…</span>';
    const condition = agentConditions(agent);
    return `<article class="vm-card">
      <div class="vm-screen${agent.frame_version ? ' vm-zoom-target' : ''}"${agent.frame_version ? ` data-src="${escapeHtml(frameUrl)}" data-title="${escapeHtml(agent.agent_id)}"` : ''}>${screen}${agent.step != null ? `<span class="vm-step">Step ${escapeHtml(agent.step)}</span>` : ''}</div>
      <div class="vm-body">
        <div class="vm-title"><strong>${escapeHtml(agent.agent_id)}</strong><span class="vm-status ${escapeHtml(agent.status)}">${escapeHtml(agent.status)}</span></div>
        <div class="vm-meta">${escapeHtml(agent.vm)} · ${escapeHtml(condition.model)}</div>
        <div class="vm-conditions"><span class="condition-chip ${condition.receivesVision ? 'vision-on' : 'vision-off'}">${condition.receivesVision ? 'VISION INPUT ON' : 'VISION INPUT OFF'}</span><span class="condition-chip">${escapeHtml(condition.observation)}</span><span class="condition-chip">${escapeHtml(condition.coordinates)} coords</span></div>
        <p class="vm-description">${escapeHtml(agent.description || agent.instruction)}</p>
        <p class="vm-activity"><span>Latest activity</span>${escapeHtml(agent.activity || (agent.status === 'waiting' ? 'Waiting for dependencies.' : 'Starting the computer-use environment…'))}</p>
        ${actionHistoryMarkup(agent)}
      </div>
    </article>`;
  }).join('');
}
function renderLiveAgents() {
  $('live-vm-count').textContent = state.liveAgents.length;
  const condition = agentConditions(state.liveAgents.find(agent => agent.status === 'active') || state.liveAgents[0] || {});
  const mode = currentRunMode();
  const topology = mode === 'macu' ? `Multiple isolated VMs · up to ${state.job?.max_parallelism || Number(value('max-parallelism')) || 1} parallel` : 'Single Agent · one VM';
  $('agent-test-conditions').innerHTML = `<span>TEST CONDITIONS</span><i class="condition-chip ${state.liveConnected ? 'vision-on' : ''}">${state.liveConnected ? '● LIVE WORKER SIGNAL' : '○ POLLING'}</i><strong>${escapeHtml(condition.model)}</strong><i class="condition-chip ${mode === 'macu' ? 'vision-on' : ''}">${mode === 'macu' ? 'MACU' : 'SINGLE AGENT'}</i><i class="condition-chip">${escapeHtml(topology)}</i><i class="condition-chip ${condition.receivesVision ? 'vision-on' : 'vision-off'}">${condition.receivesVision ? 'Vision screenshots ON' : 'Vision input OFF'}</i><i class="condition-chip">${escapeHtml(condition.provider)}</i><i class="condition-chip">${escapeHtml(condition.resolution)}</i><i class="condition-chip">${escapeHtml(condition.actionSpace)}</i><i class="condition-chip">${escapeHtml(condition.coordinates)} coordinates</i><i class="condition-chip">max ${escapeHtml(condition.maxSteps)} steps</i>`;
  const cards = liveAgentCards();
  const empty = '<div class="empty">VMs will appear after the manager creates the task graph.</div>';
  $('live-vms').innerHTML = cards || empty;
  if ($('vms-dialog').open) $('expanded-live-vms').innerHTML = cards || empty;
  $('expand-vms').disabled = state.liveAgents.length === 0;
  renderDependencyGraph(); bindVmZoomTargets();
}
function agentFrameUrl(agent) {
  if (!agent.frame_version) return '';
  const params = new URLSearchParams({result_root:value('result-root'),v:String(agent.frame_version)});
  return agent.frame_url || `/api/live-frame/${encodeURIComponent(agent.task_id)}/${encodeURIComponent(agent.agent_id)}?${params}`;
}
function renderDependencyGraph() {
  const viewport = $('graph-viewport');
  if (!state.liveAgents.length) { viewport.innerHTML = `<div class="empty"><strong>${currentRunMode() === 'macu' && state.job?.running ? 'Manager is generating the current MACU dependency graph…' : 'The dependency map appears when the manager creates the graph.'}</strong>${currentRunMode() === 'macu' && state.job?.running ? '<br>Multiple isolated VM branches will appear here as soon as the DAG is ready.' : ''}</div>`; return; }
  const allNodes = []; const edges = [];
  const taskGroups = Object.groupBy ? Object.groupBy(state.liveAgents, a => a.task_id) : state.liveAgents.reduce((out,a) => ((out[a.task_id] ||= []).push(a),out),{});
  let yOffset = 20; let canvasWidth = 960;
  Object.entries(taskGroups).forEach(([taskId, agents]) => {
    const byId = new Map(agents.map(agent => [agent.agent_id, agent]));
    const memo = new Map();
    const depthOf = (agent, visiting = new Set()) => {
      if (memo.has(agent.agent_id)) return memo.get(agent.agent_id);
      if (visiting.has(agent.agent_id)) return 0;
      visiting.add(agent.agent_id);
      const deps = (agent.dependencies || []).map(id => byId.get(id)).filter(Boolean);
      const depth = deps.length ? Math.max(...deps.map(dep => depthOf(dep, visiting))) + 1 : 0;
      memo.set(agent.agent_id, depth); visiting.delete(agent.agent_id); return depth;
    };
    const levels = new Map();
    agents.forEach(agent => { const depth = depthOf(agent); (levels.get(depth) || levels.set(depth, []).get(depth)).push(agent); });
    const maxDepth = Math.max(...levels.keys());
    const widest = Math.max(...[...levels.values()].map(level => level.length), 1);
    const taskWidth = Math.max(900, widest * 280); canvasWidth = Math.max(canvasWidth, taskWidth);
    const positions = new Map();
    [...levels.entries()].sort((a,b) => a[0]-b[0]).forEach(([depth, level]) => {
      level.forEach((agent, index) => {
        const x = ((taskWidth / level.length) * (index + .5)) - 120;
        const y = yOffset + depth * 245;
        positions.set(agent.agent_id, {x,y,status:agent.status}); allNodes.push({agent,x,y});
      });
    });
    const managerId = `${taskId}::manager`; const managerX = taskWidth / 2 - 120; const managerY = yOffset + (maxDepth + 1) * 245;
    const dependedOn = new Set(agents.flatMap(agent => agent.dependencies || []));
    const terminalAgents = agents.filter(agent => !dependedOn.has(agent.agent_id));
    const managerStatus = agents.some(a => a.status === 'failed') ? 'failed' : (agents.every(a => a.status === 'complete') ? 'complete' : 'waiting');
    const manager = {task_id:taskId,agent_id:managerId,description:'Aggregate & verify final result',status:managerStatus,manager:true,dependencies:terminalAgents.map(a => a.agent_id)};
    positions.set(managerId,{x:managerX,y:managerY,status:manager.status}); allNodes.push({agent:manager,x:managerX,y:managerY});
    [...agents,manager].forEach(agent => (agent.dependencies || []).forEach(dep => {
      const from = positions.get(dep), to = positions.get(agent.agent_id); if (from && to) edges.push({from,to,pending:agent.status === 'waiting',active:from.status === 'active' || to.status === 'active'});
    }));
    yOffset = managerY + 235;
  });
  const canvasHeight = Math.max(620,yOffset); const paths = edges.map(edge => {
    const x1=edge.from.x+120,y1=edge.from.y+205,x2=edge.to.x+120,y2=edge.to.y;
    const mid=(y1+y2)/2; return `<path class="graph-edge${edge.pending?' pending':''}${edge.active?' flowing':''}" d="M ${x1} ${y1} L ${x1} ${mid} L ${x2} ${mid} L ${x2} ${y2}" marker-end="url(#arrow)"></path>`;
  }).join('');
  const nodes = allNodes.map(({agent,x,y}) => {
    const src=agentFrameUrl(agent); const visual=agent.manager ? '<span>MANAGER<br>aggregation</span>' : (src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(agent.agent_id)} screen">${cursorMarkup(agent)}` : '<span>PENDING</span>');
    return `<article class="dag-node ${escapeHtml(agent.status)}${agent.manager?' manager':''}" style="left:${x}px;top:${y}px"><div class="dag-node-head"><div><span class="dag-node-kind">${agent.manager?'Manager':'CUA subagent'}</span><strong title="${escapeHtml(agent.description)}">${escapeHtml(agent.description)}</strong></div><span class="vm-status ${escapeHtml(agent.status)}">${escapeHtml(agent.status)}</span></div><div class="dag-node-screen${src?' vm-zoom-target':''}"${src?` data-src="${escapeHtml(src)}" data-title="${escapeHtml(agent.agent_id)}"`:''}>${visual}</div><div class="dag-node-vm">${agent.manager?'Receives agent results + screenshots':escapeHtml(agent.vm || 'VM pending')}</div><p class="dag-node-activity">${escapeHtml(agent.activity || (agent.status==='waiting'?'Waiting for dependencies.':'Preparing…'))}</p></article>`;
  }).join('');
  viewport.innerHTML=`<div class="graph-canvas" id="graph-canvas" style="width:${canvasWidth}px;height:${canvasHeight}px"><svg class="graph-edges" viewBox="0 0 ${canvasWidth} ${canvasHeight}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path class="graph-edge-arrow" d="M0,0 L8,4 L0,8 z"></path></marker></defs>${paths}</svg>${nodes}</div>`;
  const signature = JSON.stringify(state.liveAgents.map(agent => [agent.task_id,agent.agent_id,agent.dependencies || []]));
  if (signature !== state.graphSignature) {
    state.graphSignature = signature;
    requestAnimationFrame(fitDependencyGraph);
  } else applyGraphZoom();
}
function applyGraphZoom() { const canvas=$('graph-canvas'); if(canvas) { canvas.style.transform='none'; canvas.style.zoom=state.graphScale; } $('graph-zoom-label').textContent=`${Math.round(state.graphScale*100)}%`; }
function fitDependencyGraph() {
  const canvas=$('graph-canvas'), viewport=$('graph-viewport'); if(!canvas)return;
  canvas.style.zoom=1;
  const widthScale=(viewport.clientWidth-28)/canvas.offsetWidth;
  const heightScale=(viewport.clientHeight-28)/canvas.offsetHeight;
  state.graphScale=Math.max(.4,Math.min(1,widthScale,heightScale));
  applyGraphZoom(); viewport.scrollTop=0; viewport.scrollLeft=Math.max(0,(canvas.offsetWidth-viewport.clientWidth)/2);
}
function bindVmZoomTargets() { document.querySelectorAll('.vm-zoom-target').forEach(node => { if(node.dataset.zoomBound) return; node.dataset.zoomBound='1'; node.addEventListener('click',()=>openVmImage(node.dataset.src,node.dataset.title)); }); }
function openVmImage(src,title) { if(!src)return; state.imageScale=1; $('vm-image-title').textContent=title || 'Agent screen'; $('vm-image-full').src=src; applyImageZoom(); $('vm-image-dialog').showModal(); }
function applyImageZoom() { $('vm-image-full').style.width=`${state.imageScale*100}%`; $('image-zoom-reset').textContent=`${Math.round(state.imageScale*100)}%`; }
function renderJob() {
  const job = state.job || {};
  const pill = $('job-pill');
  pill.className = `job-pill ${job.running ? 'running' : (job.returncode && job.returncode !== 0 ? 'failed' : 'idle')}`;
  pill.querySelector('span').textContent = job.running ? `Run ${job.id} active` : (job.id ? `Run ${job.id} ${job.returncode === 0 ? 'finished' : 'stopped'}` : 'No active run');
  const status = job.running ? 'Running' : (job.id ? (job.returncode === 0 ? 'Completed' : 'Stopped / failed') : 'Idle');
  $('job-metrics').innerHTML = `<div><span>Status</span><strong>${status}</strong></div><div><span>Elapsed</span><strong>${job.elapsed_seconds != null ? `${job.elapsed_seconds}s` : '—'}</strong></div><div><span>Exit code</span><strong>${job.returncode ?? '—'}</strong></div>`;
  const context = $('run-context');
  const batch = job.batch || {}; const tasks = batch.tasks || [];
  if (tasks.length) {
    const current = batch.current || tasks.find(task => task.status === 'failed') || tasks[tasks.length - 1];
    const task = state.tasks.find(item => item.id === current?.task_id);
    const completed = tasks.filter(item => item.status === 'complete').length;
    const activeAgents = state.liveAgents.filter(agent => agent.task_id === current?.task_id && agent.status === 'active');
    const stepText = activeAgents.length ? activeAgents.map(agent => `${agent.agent_id}: step ${agent.step ?? 'starting'}/${agent.max_steps || value('max-steps')}`).join(' · ') : 'No CUA step reported yet';
    context.hidden = false;
    context.innerHTML = `<div class="run-context-head"><div><span class="eyebrow">CURRENT BATCH TASK</span><strong>Task ${current?.position || '—'} of ${batch.total || tasks.length} · ${escapeHtml(current?.domain || '')}</strong></div><span class="phase-badge">${escapeHtml(batch.phase || 'Preparing')}</span></div><p class="current-task-text">${escapeHtml(task?.instruction || current?.task_id || '')}</p><div class="batch-progress"><i style="width:${Math.round((completed / tasks.length) * 100)}%"></i></div><div class="run-context-foot"><span>${completed}/${tasks.length} tasks complete</span><span>${escapeHtml(stepText)}</span><code>${escapeHtml(current?.task_id || '')}</code></div><div class="batch-task-strip">${tasks.map(item => `<span class="batch-dot ${escapeHtml(item.status)}" title="Task ${item.position}: ${escapeHtml(item.task_id)}">${item.position}</span>`).join('')}</div>`;
  } else { context.hidden = true; context.innerHTML = ''; }
  const log = $('log');
  log.textContent = job.log?.length ? job.log.join('\n') : 'Waiting for a run…';
  if (job.running) log.scrollTop = log.scrollHeight;
  updateLaunchButton();
}
function formatNumber(number, digits = 2) { return number == null ? '—' : Number(number).toFixed(digits); }
function renderResults() {
  const complete = state.results.filter(r => r.status === 'completed');
  const scored = complete.filter(r => r.score != null);
  const avg = scored.length ? scored.reduce((sum,r) => sum + Number(r.score), 0) / scored.length : null;
  $('score-summary').innerHTML = state.results.length ? `
    <div class="summary-chip"><strong>${complete.length}</strong> completed</div>
    <div class="summary-chip"><strong>${scored.length}</strong> evaluated</div>
    <div class="summary-chip"><strong>${avg == null ? '—' : formatNumber(avg,3)}</strong> mean score</div>
    <div class="summary-chip"><strong>$${formatNumber(state.budget?.spent_usd || 0,4)}</strong> Tinker spent of $${formatNumber(state.budget?.limit_usd || 15,2)}</div>` : (state.budget ? `<div class="summary-chip"><strong>$${formatNumber(state.budget.spent_usd || 0,4)}</strong> Tinker spent of $${formatNumber(state.budget.limit_usd || 15,2)}</div>` : '');
  if (!state.results.length) {
    $('results').innerHTML = '<div class="empty">Completed and in-progress task results will appear here.</div>'; return;
  }
  $('results').innerHTML = state.results.map(result => `
    <article class="result-row">
      <div class="result-main">
        <div class="result-title"><strong>${escapeHtml(result.task_id)}</strong><span class="badge ${result.status === 'completed' ? 'complete' : (result.status === 'failed' ? 'failed' : 'running')}">${escapeHtml(result.status)}</span><span class="badge">${escapeHtml(result.domain || 'unknown domain')}</span></div>
        <p class="result-instruction">${escapeHtml(result.instruction || 'Graph/result data is being prepared.')}</p>
        <div class="result-meta"><span>${result.subtask_count} planned agents</span><span>${result.success_count} successful nodes</span><span>${result.error_count} errors</span><span>${result.replans} replans</span><span>${result.duration_seconds == null ? '—' : `${formatNumber(result.duration_seconds,1)}s`}</span><span>${result.cost_usd == null ? '—' : `$${formatNumber(result.cost_usd,4)}`}</span></div>
      </div>
      <div><div class="score"><span>OSWorld score</span>${result.score == null ? '—' : escapeHtml(result.score)}</div><div class="result-actions"><button class="button secondary small inspect" data-id="${escapeHtml(result.task_id)}">${result.status === 'failed' ? 'Error' : 'Response'}</button>${result.status === 'completed' ? `<button class="button primary small visualize" data-id="${escapeHtml(result.task_id)}">${result.has_visualization ? 'Open replay' : 'Build replay + video'}</button>${result.has_video ? `<button class="button secondary small video" data-id="${escapeHtml(result.task_id)}">Watch video</button>` : ''}` : ''}</div></div>
    </article>`).join('');
  document.querySelectorAll('.inspect').forEach(button => button.addEventListener('click', () => showResult(button.dataset.id)));
  document.querySelectorAll('.visualize').forEach(button => button.addEventListener('click', () => visualize(button.dataset.id, button)));
  document.querySelectorAll('.video').forEach(button => button.addEventListener('click', () => window.open(`/view/${encodeURIComponent(button.dataset.id)}/replay.mp4`, '_blank')));
}
function showResult(id) {
  const result = state.results.find(r => r.task_id === id); if (!result) return;
  $('dialog-title').textContent = result.task_id;
  $('dialog-instruction').textContent = result.instruction;
  $('dialog-response').textContent = result.final_response || 'No final response yet.';
  $('result-dialog').showModal();
}
async function visualize(id, button) {
  if (button.textContent === 'Open replay') { window.open(`/view/${encodeURIComponent(id)}/`, '_blank'); return; }
  button.disabled = true; button.textContent = 'Building…';
  try {
    const data = await jsonFetch(`/api/visualize/${encodeURIComponent(id)}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({result_root:value('result-root')})});
    window.open(data.url, '_blank'); await refreshStatus();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
}

$('refresh-setup').addEventListener('click', async () => {
  const button = $('refresh-setup');
  button.disabled = true; button.textContent = 'Stopping runs…';
  try {
    await jsonFetch('/api/cleanup', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({result_root:value('result-root')})});
    button.textContent = 'VMs released';
  } catch (error) {
    button.textContent = 'Cleanup failed';
    $('readiness-callout').classList.add('show'); $('readiness-callout').textContent = error.message;
  }
  await refreshSetup(); await refreshStatus(); await refreshLiveAgents();
  setTimeout(() => { button.textContent = 'Stop & recheck'; button.disabled = false; }, 1000);
});
$('refresh-results').addEventListener('click', refreshStatus);
$('vm-provider').addEventListener('change', refreshSetup);
$('osworld-root').addEventListener('change', refreshSetup);
$('result-root').addEventListener('change', () => {saveSettings(); refreshStatus(); refreshLiveAgents(); if(state.liveStream) startLiveStream();});
$('task-search').addEventListener('input', renderTasks);
document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); tab.classList.add('active'); state.domain = tab.dataset.domain; renderTasks();
}));
$('select-visible').addEventListener('click', () => {visibleTasks().forEach(t => state.selected.add(t.key)); renderTasks(); updateEstimate(); updateLaunchButton();});
$('clear-selection').addEventListener('click', () => {state.selected.clear(); renderTasks(); updateEstimate(); updateLaunchButton();});
$('save-default').addEventListener('click', () => {
  localStorage.setItem('macu-dashboard-default-tasks', JSON.stringify([...state.selected]));
  const button = $('save-default');
  const original = button.textContent;
  button.textContent = state.selected.size ? 'Default saved' : 'Empty default saved';
  setTimeout(() => { button.textContent = original; }, 1400);
});
$('launch').addEventListener('click', launch); $('stop').addEventListener('click', stop);
$('copy-command').addEventListener('click', async () => {
  const button = $('copy-command');
  if (!state.job?.command?.length) return;
  const command = state.job.command.join(' ');
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(command);
      copied = true;
    }
  } catch (_) {}
  if (!copied) {
    const textarea = document.createElement('textarea');
    textarea.value = command;
    textarea.setAttribute('readonly', '');
    textarea.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(textarea);
    textarea.select();
    copied = document.execCommand('copy');
    textarea.remove();
  }
  const original = button.textContent;
  button.textContent = copied ? 'Copied!' : 'Copy failed';
  setTimeout(() => { button.textContent = original; }, 1400);
});
document.querySelectorAll('.monitor-tab').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('.monitor-tab').forEach(item => { item.classList.remove('active'); item.setAttribute('aria-selected', 'false'); });
  document.querySelectorAll('.monitor-pane').forEach(pane => pane.classList.remove('active'));
  tab.classList.add('active'); tab.setAttribute('aria-selected', 'true'); $(tab.dataset.monitor).classList.add('active');
  if (tab.dataset.monitor === 'vms-pane') refreshLiveAgents();
}));
function setVmView(view) {
  state.vmView = view;
  $('vm-grid-view').classList.toggle('active', view === 'grid');
  $('vm-graph-view').classList.toggle('active', view === 'graph');
  $('live-vms').classList.toggle('view-hidden', view === 'graph');
  $('live-graph').classList.toggle('active', view === 'graph');
  if (view === 'graph') renderDependencyGraph();
}
$('vm-grid-view').addEventListener('click', () => setVmView('grid'));
$('vm-graph-view').addEventListener('click', () => setVmView('graph'));
$('graph-zoom-in').addEventListener('click', () => { state.graphScale=Math.min(2,state.graphScale+.15); applyGraphZoom(); });
$('graph-zoom-out').addEventListener('click', () => { state.graphScale=Math.max(.4,state.graphScale-.15); applyGraphZoom(); });
$('graph-fit').addEventListener('click', fitDependencyGraph);
$('graph-fullscreen').addEventListener('click', async () => {
  if (document.fullscreenElement) await document.exitFullscreen();
  else await $('live-graph').requestFullscreen();
});
document.addEventListener('fullscreenchange', () => {
  $('graph-fullscreen').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen';
  requestAnimationFrame(fitDependencyGraph);
});
$('expand-vms').addEventListener('click', () => {
  $('expanded-live-vms').innerHTML = liveAgentCards();
  $('vms-dialog').showModal(); bindVmZoomTargets();
});
$('close-vms-dialog').addEventListener('click', () => $('vms-dialog').close());
$('vms-dialog').addEventListener('click', event => { if(event.target === $('vms-dialog')) $('vms-dialog').close(); });
$('close-vm-image').addEventListener('click', () => $('vm-image-dialog').close());
$('vm-image-dialog').addEventListener('click', event => { if(event.target === $('vm-image-dialog')) $('vm-image-dialog').close(); });
$('image-zoom-in').addEventListener('click', () => {state.imageScale=Math.min(4,state.imageScale+.25);applyImageZoom();});
$('image-zoom-out').addEventListener('click', () => {state.imageScale=Math.max(.5,state.imageScale-.25);applyImageZoom();});
$('image-zoom-reset').addEventListener('click', () => {state.imageScale=1;applyImageZoom();});
$('close-dialog').addEventListener('click', () => $('result-dialog').close());
$('result-dialog').addEventListener('click', event => { if(event.target === $('result-dialog')) $('result-dialog').close(); });
['manager-model','cua-model','max-parallelism','max-cost-usd'].forEach(id => $(id).addEventListener('change', () => { saveSettings(); updateEstimate(); }));
$('manager-provider').addEventListener('change', () => { saveSettings(); populateModels(); updateEstimate(); });
$('cua-provider').addEventListener('change', () => { saveSettings(); populateModels(); updateEstimate(); });
document.querySelectorAll('input[name="run-mode"]').forEach(input => input.addEventListener('change', () => {
  localStorage.setItem('macu-dashboard-run-mode', runMode());
  updateRunMode(); updateEstimate(); updateLaunchButton();
}));
function updateRunMode() {
  const mode = runMode();
  document.querySelectorAll('.mode-option').forEach(option => option.classList.toggle('selected', option.querySelector('input')?.checked));
  $('manager-config').classList.toggle('mode-disabled', mode === 'single');
  $('max-parallelism').disabled = mode === 'single';
  document.querySelector('.explain p').textContent = mode === 'single'
    ? 'Each selected OSWorld task is completed by one CUA using the original instruction. No manager DAG, replanning, or parallel branches are used.'
    : 'For each selected OSWorld task, the manager creates a DAG and runs up to this many ready CUA branches at once. Selected benchmark tasks themselves are processed one after another.';
}

function populateModels() {
  const managerCurrent = value('manager-model') || 'thinkingmachines/Inkling-Small';
  const cuaCurrent = value('cua-model') || 'thinkingmachines/Inkling-Small';
  const managerProvider = value('manager-provider') || 'tinker';
  const options = Object.entries(state.models).filter(([,m]) => (m.provider || 'tinker') === managerProvider).map(([id,m]) => `<option value="${escapeHtml(id)}">${escapeHtml(m.family)} · ${escapeHtml(m.name)}</option>`).join('');
  const cuaProvider = value('cua-provider') || 'tinker';
  const visionOptions = Object.entries(state.models).filter(([,m]) => m.image_input && m.cua_available !== false && (m.provider || 'tinker') === cuaProvider).map(([id,m]) => `<option value="${escapeHtml(id)}">${escapeHtml(m.family)} · ${escapeHtml(m.name)}</option>`).join('');
  $('manager-model').innerHTML = options; $('cua-model').innerHTML = visionOptions;
  if (state.models[managerCurrent]?.provider === managerProvider || (managerProvider === 'tinker' && state.models[managerCurrent] && !state.models[managerCurrent].provider)) $('manager-model').value = managerCurrent;
  if (state.models[cuaCurrent]?.image_input && state.models[cuaCurrent]?.cua_available !== false) $('cua-model').value = cuaCurrent;
}
function renderModelCatalog() {
  $('model-catalog').innerHTML = Object.entries(state.models).map(([id,m]) => `<div class="model-card"><div><strong>${escapeHtml(m.name)}</strong><span>${escapeHtml(m.family)} · ${escapeHtml(m.type)} · ${escapeHtml(m.context)}</span></div><div class="model-price"><span>Input <b>$${m.prefill}/M</b></span><span>Output <b>$${m.sample}/M</b></span></div><small>${m.image_input ? 'Image API verified' : 'Text manager only on current endpoint'}${m.note ? ` · ${escapeHtml(m.note)}` : ''}</small></div>`).join('');
}
let estimateTimer;
function updateEstimate() {
  clearTimeout(estimateTimer);
  if (!state.selected.size || !value('manager-model') || !value('cua-model')) {
    state.estimate = null; $('cost-preview').innerHTML = '<strong>Select tasks to calculate estimated Tinker cost.</strong>'; updateLaunchButton(); return;
  }
  estimateTimer = setTimeout(async () => {
    try {
      const estimate = await jsonFetch('/api/estimate', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_keys:[...state.selected],manager_provider:value('manager-provider'),manager_model:value('manager-model'),cua_provider:value('cua-provider'),cua_model:value('cua-model'),max_parallelism:Number(value('max-parallelism')),max_cost_usd:Number(value('max-cost-usd') || 15),run_mode:runMode()})});
      state.estimate = estimate;
      $('cost-preview').className = `cost-preview ${estimate.within_cap ? 'within' : 'over'}`;
      $('cost-preview').innerHTML = `<div><span>Pre-run estimate</span><strong>$${estimate.estimated_cost_usd.toFixed(2)}</strong> of $${Number(value('max-cost-usd') || 15).toFixed(2)} cap</div><p>Manager $${estimate.manager_cost_usd.toFixed(2)} + CUA $${estimate.cua_cost_usd.toFixed(2)} · ${escapeHtml(estimate.assumptions)} Runtime uses a shared reservation guard and stops before the configured cap.</p>`;
    } catch (error) { state.estimate = null; $('cost-preview').textContent = error.message; }
    updateLaunchButton();
  }, 150);
}

loadSettings();
const savedRunMode = localStorage.getItem('macu-dashboard-run-mode');
if (savedRunMode === 'single' || savedRunMode === 'macu') document.querySelector(`input[name="run-mode"][value="${savedRunMode}"]`).checked = true;
updateRunMode(); setVmView('graph');
refreshSetup(); refreshStatus(); refreshLiveAgents();
setInterval(() => { refreshStatus(); refreshLiveAgents(); }, 2500);
