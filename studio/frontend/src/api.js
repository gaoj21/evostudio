async function req(path, { method = 'GET', body, formData } = {}) {
  const res = await fetch(path, {
    method,
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

export const api = {
  health: () => req('/api/health'),
  palette: () => req('/api/palette'),
  listGraphs: () => req('/api/graphs'),
  createGraph: (name, goal) => req('/api/graphs', { method: 'POST', body: { name, goal } }),
  getGraph: (id) => req(`/api/graphs/${encodeURIComponent(id)}`),
  renameGraph: (id, name) =>
    req(`/api/graphs/${encodeURIComponent(id)}/rename`, { method: 'POST', body: { name } }),
  saveGraph: (id, graph) => req(`/api/graphs/${encodeURIComponent(id)}`, { method: 'PUT', body: graph }),
  deleteGraph: (id) => req(`/api/graphs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runGraph: (id, inputs, startAt, session) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run`, {
      method: 'POST',
      body: {
        inputs,
        ...(startAt?.length ? { start_at: startAt } : {}),
        ...(session ? { session } : {}),
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
  previewBatchUpload: (id, file) => {
    const formData = new FormData();
    formData.append('file', file);
    return req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', formData });
  },
  previewBatchSource: (id, params) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', body: { source: 'credit_risk', ...params } }),
  previewBatchCanvas: (id, params = {}) =>
    req(`/api/graphs/${encodeURIComponent(id)}/run-batch/preview`, { method: 'POST', body: { source: 'canvas', ...params } }),
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
  abandonRun: (runId) =>
    req(`/api/runs/${encodeURIComponent(runId)}/abandon`, { method: 'POST' }),
  creditRiskSource: () => req('/api/sources/credit-risk'),
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
  startEvolveSource: (id, params) =>
    req(`/api/graphs/${encodeURIComponent(id)}/evolve`, { method: 'POST', body: { source: 'credit_risk', ...params } }),
  applyEvolve: (taskId) => req(`/api/evolve/${encodeURIComponent(taskId)}/apply`, { method: 'POST' }),
  chatGraph: (id, body) => req(`/api/graphs/${encodeURIComponent(id)}/chat`, { method: 'POST', body }),
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
  listCustomTools: () => req('/api/tools/custom'),
  saveCustomTool: (spec) => req('/api/tools/custom', { method: 'POST', body: spec }),
  deleteCustomTool: (name) => req(`/api/tools/custom/${encodeURIComponent(name)}`, { method: 'DELETE' }),
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
