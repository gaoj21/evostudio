async function req(path, { method = 'GET', body, formData, signal } = {}) {
  const res = await fetch(path, {
    method,
    signal,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: formData !== undefined ? formData : body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = new Error(`${method} ${path} failed: HTTP ${res.status}`);
    err.status = res.status;
    try {
      err.body = await res.json();
    } catch {
      err.body = null;
    }
    throw err;
  }
  return res.json();
}

const assistantRequest = (graphId, context, body, options = {}) => req(`/api/graphs/${encodeURIComponent(graphId)}/assistant`, { method: 'POST', body: { ...body, context }, signal: options.signal });

const mem0Path = (graph, space) => `/api/graphs/${encodeURIComponent(graph)}/mem0/spaces${space ? '/' + encodeURIComponent(space) : ''}`;

const agentPath = (g, a = '') => `/api/graphs/${encodeURIComponent(g)}/agents${a ? '/' + encodeURIComponent(a) : ''}`;
export const api = {
  listResultRuns: g => req(`/api/graphs/${encodeURIComponent(g)}/results`),
  chatResults: (g, body, options) => assistantRequest(g, 'results', body, options),
  stopAssistant: (g, id) => req(`/api/graphs/${encodeURIComponent(g)}/assistant/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  stopChatJob: (g, id) => req(`/api/graphs/${encodeURIComponent(g)}/chat/jobs/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  chatMemoryResources: g => req(`${agentPath(g)}/memory-resources`),
  canvasAgents: g => req(agentPath(g)),
  createCanvasAgent: (g, body) => req(agentPath(g), { method: 'POST', body }),
  removeCanvasAgent: (g, a) => req(agentPath(g, a), { method: 'DELETE' }),
  updateCanvasAgent: (g, a, body) => req(agentPath(g, a), { method: 'PUT', body }),
  agentSessions: (g, a) => req(`${agentPath(g, a)}/sessions`),
  deleteAgentSession: (g, a, s) => req(`${agentPath(g, a)}/sessions/${encodeURIComponent(s)}`, { method: 'DELETE' }),
  createAgentSession: (g, a) => req(`${agentPath(g, a)}/sessions`, { method: 'POST' }),
  sendAgentMessage: (g, a, s, message) => req(`${agentPath(g, a)}/sessions/${encodeURIComponent(s)}/messages`, { method: 'POST', body: { message } }),
  stopAgentSession: (g, a, s) => req(`${agentPath(g, a)}/sessions/${encodeURIComponent(s)}/stop`, { method: 'POST' }),
  mem0Spaces: (graph) => req(mem0Path(graph)),
  deleteMem0Space: (graph, space) => req(mem0Path(graph, space), { method: 'DELETE' }),
  createMem0Space: (graph, name) => req(mem0Path(graph), { method: 'POST', body: { name } }),
  mem0Entries: (graph, space, q = '') => req(`${mem0Path(graph, space)}/entries?q=${encodeURIComponent(q)}`),
  addMem0Entry: (graph, space, content) => req(`${mem0Path(graph, space)}/entries`, { method: 'POST', body: { content } }),
  updateMem0Entry: (graph, space, id, content) => req(`${mem0Path(graph, space)}/entries/${encodeURIComponent(id)}`, { method: 'PUT', body: { content } }),
  deleteMem0Entry: (graph, space, id) => req(`${mem0Path(graph, space)}/entries/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  listProjects: () => req('/api/projects'),
  updateProject: (id, body) => req(`/api/projects/${encodeURIComponent(id)}`, { method: 'PUT', body }),
  deleteProject: id => req(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  createProject: (body) => req('/api/projects', { method: 'POST', body }),
  projectTasks: (id) => req(`/api/projects/${encodeURIComponent(id)}/tasks`),
  createTask: (id, body) => req(`/api/projects/${encodeURIComponent(id)}/tasks`, { method: 'POST', body }),
  assignTask: (id, graph) => req(`/api/projects/${encodeURIComponent(id)}/tasks/${encodeURIComponent(graph)}`, { method: 'PUT' }),
  health: () => req('/api/health'),
  palette: () => req('/api/palette'),
  listGraphs: () => req('/api/graphs'),
  createGraph: (name, goal) => req('/api/graphs', { method: 'POST', body: { name, goal } }),
  getGraph: (id) => req(`/api/graphs/${encodeURIComponent(id)}`),
  renameGraph: (id, name) =>
    req(`/api/graphs/${encodeURIComponent(id)}/rename`, { method: 'POST', body: { name } }),
  saveGraph: (id, graph) => req(`/api/graphs/${encodeURIComponent(id)}`, { method: 'PUT', body: graph }),
  deleteGraph: (id) => req(`/api/graphs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // What a run would do, from one revision of the graph: schema, start
  // points, warnings, prefill, and the plan_id a run request must present.
  runPlan: (id, { startAt = [], mode = 'single', includeRecords = false } = {}) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-plan`, {
      method: 'POST',
      body: { start_at: startAt, mode, ...(includeRecords ? { include_records: true } : {}) },
    }),
  runGraph: (id, inputs, startAt, session, planId, record) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run`, {
      method: 'POST',
      body: {
        inputs,
        ...(startAt?.length ? { start_at: startAt } : {}),
        ...(session ? { session } : {}),
        ...(planId ? { plan_id: planId } : {}),
        // Which of the source's records; absent means the source yields one.
        ...(record !== undefined && record !== null ? { record } : {}),
      },
    }),
  graphInputs: (id, startAt = []) =>
    req(`/api/graphs/${encodeURIComponent(id)}/inputs${
      startAt.length ? `?start_at=${encodeURIComponent(startAt.join(','))}` : ''
    }`),
  getRun: (runId) => req(`/api/runs/${encodeURIComponent(runId)}`),
  listRuns: (graphId) =>
    req(`/api/runs${graphId ? `?graph_id=${encodeURIComponent(graphId)}` : ''}`),
  runBatchUpload: (id, file, params = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    Object.entries(params).forEach(([k, v]) => formData.append(k, v));
    return req(`/api/graphs/${encodeURIComponent(id)}/run-batch`, { method: 'POST', formData });
  },
  runBatchSource: (id, params) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch`, { method: 'POST', body: { source: 'credit_risk', ...params } }),
  runBatchCanvas: (id, params = {}) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch`, { method: 'POST', body: { source: 'canvas', ...params } }),
  // Same three shapes as run-batch, but nothing is started: it answers how
  // many runs you would get, so stepping cannot multiply a batch unseen.
  previewBatchUpload: (id, file, params = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    Object.entries(params).forEach(([k, v]) => formData.append(k, v));
    return req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', formData });
  },
  previewBatchSource: (id, params) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', body: { source: 'credit_risk', ...params } }),
  previewBatchCanvas: (id, params = {}) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', body: { source: 'canvas', ...params } }),
  collectSource: (id, body = {}) => req(`/api/graphs/${encodeURIComponent(id)}/source-collections`, { method: 'POST', body }),
  stopSourceCollection: (id, collectionId) => req(`/api/graphs/${encodeURIComponent(id)}/source-collections/${encodeURIComponent(collectionId)}/stop`, { method: 'POST' }),
  sourceCollection: (id, collectionId) => req(`/api/graphs/${encodeURIComponent(id)}/source-collections/${encodeURIComponent(collectionId)}`),
  listBatches: (graphId) =>
    req(`/api/batches${graphId ? `?graph_id=${encodeURIComponent(graphId)}` : ''}`),
  getBatch: (batchId) => req(`/api/batches/${encodeURIComponent(batchId)}`),
  // A URL rather than a fetch: the browser saves the file itself, so results
  // of any size never pass through JS.
  batchExportUrl: (batchId, format = 'csv') =>
    `/api/batches/${encodeURIComponent(batchId)}/export?format=${encodeURIComponent(format)}`,
  compareBatches: (batchId, baselineId) =>
    req(`/api/batches/${encodeURIComponent(batchId)}/compare`
      + `?baseline=${encodeURIComponent(baselineId)}`),
  cancelBatch: (batchId) =>
    req(`/api/batches/${encodeURIComponent(batchId)}/cancel`, { method: 'POST' }),
  // Score a batch from what it holds. Credit-risk batches need no body; any
  // other batch sends {metric, label_key}.
  evaluateBatch: (batchId, body) =>
    req(`/api/batches/${encodeURIComponent(batchId)}/evaluate`, { method: 'POST', body: body || {} }),
  getBatchEvaluation: (batchId) =>
    req(`/api/batches/${encodeURIComponent(batchId)}/evaluation`),
  // Run the records of a stopped batch that did not finish; the rest are
  // kept as they are.
  resumeBatch: (batchId) =>
    req(`/api/batches/${encodeURIComponent(batchId)}/resume`, { method: 'POST' }),
  // Stops the run for real: the model call it is inside is abandoned.
  cancelRun: (runId) =>
    req(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
  abandonRun: (runId) =>
    req(`/api/runs/${encodeURIComponent(runId)}/abandon`, { method: 'POST' }),
  creditRiskSource: (dataset) => req(`/api/sources/credit-risk${dataset ? `?dataset=${encodeURIComponent(dataset)}` : ''}`),
  // Snapshot memory to a timestamped backup, then empty it. Per workflow
  // when an id is given; refused (409) while anything is running.
  resetMemory: (graphId) =>
    req('/api/memory/reset', { method: 'POST', body: graphId ? { graph_id: graphId } : {} }),
  listMemoryAgents: (id) => req(`/api/graphs/${encodeURIComponent(id)}/memory/agents`),
  searchMemory: (id, agent, q) =>
    req(`/api/graphs/${encodeURIComponent(id)}/memory?agent=${encodeURIComponent(agent)}${q ? `&q=${encodeURIComponent(q)}` : ''}`),
  evolveMetrics: () => req('/api/evolve/metrics'),
  listEvolveTasks: (graphId) => req(`/api/evolve${graphId ? `?graph_id=${encodeURIComponent(graphId)}` : ''}`),
  getEvolveTask: (taskId) => req(`/api/evolve/${encodeURIComponent(taskId)}`),
  startEvolveUpload: (id, file, params) => {
    const formData = new FormData();
    formData.append('file', file);
    Object.entries(params || {}).forEach(([k, v]) => formData.append(k, v));
    return req(`/api/graphs/${encodeURIComponent(id)}/evolve`, { method: 'POST', formData });
  },
  previewEvolveResults: (id, params) => req(`/api/graphs/${encodeURIComponent(id)}/evolve/preview`, { method: 'POST', body: params }),
  startEvolveResults: (id, params) => req(`/api/graphs/${encodeURIComponent(id)}/evolve`, { method: 'POST', body: params }),
  startEvolveSource: (id, params) =>
    req(`/api/graphs/${encodeURIComponent(id)}/evolve`, { method: 'POST', body: { source: 'credit_risk', ...params } }),
  evolvePresets: () => req('/api/evolve/presets'),
  // mode 'new' saves the optimized prompts as a new workflow; 'replace'
  // writes them into the optimized one after saving a copy of it.
  applyEvolve: (taskId, mode = 'new') =>
    req(`/api/evolve/${encodeURIComponent(taskId)}/apply?mode=${encodeURIComponent(mode)}`, { method: 'POST' }),
  chatGraph: (id, body, options) => assistantRequest(id, 'canvas', body, options),
  chatJob: (id, jobId) =>
    req(`/api/graphs/${encodeURIComponent(id)}/chat/jobs/${encodeURIComponent(jobId)}`),
  runningChatJob: (id) => req(`/api/graphs/${encodeURIComponent(id)}/chat/jobs`),
  importGraph: (file) => {
    const formData = new FormData();
    formData.append('file', file);
    return req('/api/graphs/import', { method: 'POST', formData });
  },
  listTemplates: () => req('/api/templates'),
  getTemplate: (id) => req(`/api/templates/${encodeURIComponent(id)}`),
  listReviews: (status) => req(`/api/review${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  resolveReview: (id, decision, note) =>
    req(`/api/review/${encodeURIComponent(id)}`, { method: 'POST', body: { decision, note } }),
  listMetrics: () => req('/api/metrics'),
  listTools: () => req('/api/tools'),
  listSkills: () => req('/api/skills'),
  saveSkill: (spec) => req('/api/skills', { method: 'POST', body: spec }),
  deleteSkill: (name) => req(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  listSourceTypes: () => req('/api/sources'),
  // Run a source config once and learn the fields of its first record.
  probeSource: (source) => req('/api/sources/probe', { method: 'POST', body: { source } }),
  listCustomTools: () => req('/api/tools/custom'),
  saveCustomTool: (spec) => req('/api/tools/custom', { method: 'POST', body: spec }),
  deleteCustomTool: (name) => req(`/api/tools/custom/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  // A toolkit as a folder: a zip of a library or a project. The entry file's
  // public functions are the tools; the rest travels with them.
  uploadCustomTool: (file, { name = '', entry = '', sources = [] } = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    if (name) formData.append('name', name);
    if (entry) formData.append('entry', entry);
    formData.append('sources', JSON.stringify(sources));
    return req('/api/tools/custom/upload', { method: 'POST', formData });
  },
  // Deliberate and separate from upload: pip runs only when asked.
  installCustomToolRequirements: (name) =>
    req(`/api/tools/custom/${encodeURIComponent(name)}/install`, { method: 'POST' }),
  getSchedule: (id) => req(`/api/graphs/${encodeURIComponent(id)}/schedule`),
  setSchedule: (id, schedule) =>
    req(`/api/graphs/${encodeURIComponent(id)}/schedule`, { method: 'PUT', body: schedule }),
  clearSchedule: (id) =>
    req(`/api/graphs/${encodeURIComponent(id)}/schedule`, { method: 'DELETE' }),
  getWatch: (id) => req(`/api/graphs/${encodeURIComponent(id)}/watch`),
  startWatch: (id) => req(`/api/graphs/${encodeURIComponent(id)}/watch`, { method: 'POST' }),
  stopWatch: (id) => req(`/api/graphs/${encodeURIComponent(id)}/watch`, { method: 'DELETE' }),
  getWorkspace: (id) => req(`/api/graphs/${encodeURIComponent(id)}/workspace`),
  getWorkspaceFile: (id, path) =>
    req(`/api/graphs/${encodeURIComponent(id)}/workspace/file?path=${encodeURIComponent(path)}`),
  saveWorkspaceFile: (id, path, content) =>
    req(`/api/graphs/${encodeURIComponent(id)}/workspace/file`, { method: 'PUT', body: { path, content } }),
  uploadWorkspaceFile: (id, file, path = '') => {
    const formData = new FormData();
    formData.append('file', file);
    return req(`/api/graphs/${encodeURIComponent(id)}/workspace/upload${path ? `?path=${encodeURIComponent(path)}` : ''}`, { method: 'POST', formData });
  },
  deleteWorkspaceFile: (id, path, recursive = false) =>
    req(`/api/graphs/${encodeURIComponent(id)}/workspace/file`
      + `?path=${encodeURIComponent(path)}${recursive ? '&recursive=true' : ''}`,
    { method: 'DELETE' }),
  // A URL, not a fetch: the browser saves it, so a whole run or the whole
  // project never passes through JS.
  workspaceDownloadUrl: (id, path = '') =>
    `/api/graphs/${encodeURIComponent(id)}/workspace/download`
      + `?path=${encodeURIComponent(path)}`,
  mkdirWorkspace: (id, path) =>
    req(`/api/graphs/${encodeURIComponent(id)}/workspace/mkdir`, { method: 'POST', body: { path } }),
};
