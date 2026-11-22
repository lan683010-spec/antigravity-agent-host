/**
 * Antigravity Agent Host sidecar backend (Codex-Host style multi-agent visual monitor).
 *
 * Uses Antigravity's built-in `sidecar_sdk` to serve the live Aux Pane UI and
 * read/control `.agent-runs/` across all registered workspaces.
 */
import { execFile } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SidecarApp, Response } from 'sidecar_sdk';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = resolve(__dirname, '..', '..');
const ROOTS_INDEX = join(PLUGIN_ROOT, 'runs_roots.json');
const DEFAULT_WS_RUNS = 'G:\\antigravity workspace\\.agent-runs';

function getKnownRunsRoots() {
  const seen = new Set();
  const out = [];
  const add = (p) => {
    if (!p || typeof p !== 'string') return;
    const norm = p.trim();
    const key = norm.toLowerCase();
    if (!seen.has(key) && existsSync(norm)) {
      seen.add(key);
      out.push(norm);
    }
  };
  add(DEFAULT_WS_RUNS);
  add(process.env.AGENTCTL_RUNS_DIR);
  add(join(PLUGIN_ROOT, '.agent-runs'));
  if (existsSync(ROOTS_INDEX)) {
    try {
      const arr = JSON.parse(readFileSync(ROOTS_INDEX, 'utf-8'));
      if (Array.isArray(arr)) arr.forEach(add);
    } catch {}
  }
  return out;
}

function listAllTasks(limit = 40) {
  const roots = getKnownRunsRoots();
  const seenIds = new Set();
  const tasks = [];
  for (const r of roots) {
    let entries = [];
    try {
      entries = readdirSync(r, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const ent of entries) {
      if (!ent.isDirectory()) continue;
      const metaPath = join(r, ent.name, 'meta.json');
      if (!existsSync(metaPath)) continue;
      try {
        const meta = JSON.parse(readFileSync(metaPath, 'utf-8'));
        if (meta && meta.task_id && !seenIds.has(meta.task_id)) {
          seenIds.add(meta.task_id);
          meta._root = r;
          tasks.push(meta);
        }
      } catch {}
    }
  }
  tasks.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
  return tasks.slice(0, limit);
}

function findTaskDir(taskId) {
  if (!taskId || /[/\\]/.test(taskId)) return null;
  for (const r of getKnownRunsRoots()) {
    const dir = join(r, taskId);
    if (existsSync(join(dir, 'meta.json'))) return dir;
  }
  return null;
}

function readTaskDetail(taskId) {
  const dir = findTaskDir(taskId);
  if (!dir) return null;
  let meta = null;
  try {
    meta = JSON.parse(readFileSync(join(dir, 'meta.json'), 'utf-8'));
  } catch {
    return null;
  }
  const eventsPath = join(dir, 'events.jsonl');
  const events = [];
  if (existsSync(eventsPath)) {
    try {
      const lines = readFileSync(eventsPath, 'utf-8').split('\n');
      for (const line of lines.slice(-300)) {
        if (!line.trim()) continue;
        try {
          events.push(JSON.parse(line));
        } catch {}
      }
    } catch {}
  }
  const diffPath = join(dir, 'diff.patch');
  let diff = '';
  if (existsSync(diffPath)) {
    try {
      diff = readFileSync(diffPath, 'utf-8');
    } catch {}
  }
  const stderrPath = join(dir, 'stderr.log');
  let stderr = '';
  if (existsSync(stderrPath)) {
    try {
      stderr = readFileSync(stderrPath, 'utf-8').slice(-4000);
    } catch {}
  }
  return { meta, events, diff, stderr };
}

let cachedHealth = { at: 0, data: [] };
function getHealthStatus(force = false) {
  const now = Date.now();
  if (!force && cachedHealth.data.length && now - cachedHealth.at < 30000) {
    return Promise.resolve(cachedHealth.data);
  }
  return new Promise((resolvePromise) => {
    execFile(
      'python',
      ['-m', 'agentctl', 'doctor', '--include-mock', '--json'],
      { cwd: PLUGIN_ROOT, encoding: 'utf-8', timeout: 12000 },
      (err, stdout) => {
        try {
          const parsed = JSON.parse(stdout || '[]');
          if (Array.isArray(parsed)) {
            cachedHealth = { at: Date.now(), data: parsed };
            return resolvePromise(parsed);
          }
        } catch {}
        resolvePromise(cachedHealth.data);
      }
    );
  });
}

function runAgentctl(args) {
  return new Promise((resolvePromise, rejectPromise) => {
    execFile(
      'python',
      ['-m', 'agentctl', ...args],
      { cwd: PLUGIN_ROOT, encoding: 'utf-8', timeout: 25000 },
      (err, stdout, stderr) => {
        if (err) return rejectPromise(new Error(stderr || err.message));
        try {
          const parsed = JSON.parse(stdout || '{}');
          return resolvePromise(parsed);
        } catch {
          if (err) return rejectPromise(new Error(stderr || err.message));
          resolvePromise({ output: stdout });
        }
      }
    );
  });
}

const app = new SidecarApp();

app.page('/', () => readFileSync(join(__dirname, 'index.html'), 'utf-8'));

app.api('/api/state', async (data) => {
  const tasks = listAllTasks(40);
  const selectedId = data.taskId || (tasks[0]?.task_id ?? null);
  const detail = selectedId ? readTaskDetail(selectedId) : null;
  const health = await getHealthStatus(data.refreshHealth === '1');
  return {
    selectedTaskId: selectedId,
    selectedTask: detail?.meta || null,
    events: detail?.events || [],
    diff: detail?.diff || '',
    stderr: detail?.stderr || '',
    tasks,
    health,
  };
}, 'GET');

app.api('/api/dispatch', async (data) => {
  const agent = String(data.agent || 'codex');
  const prompt = String(data.prompt || '').trim();
  const cwd = String(data.cwd || 'G:\\antigravity workspace');
  const sandbox = String(data.sandbox || 'workspace-write');
  const title = String(data.title || '').trim();
  if (!prompt) throw new Error('Prompt is required');
  const args = [
    '--runs-dir',
    join(cwd, '.agent-runs'),
    'run',
    '--agent',
    agent,
    '--cwd',
    cwd,
    '--sandbox',
    sandbox,
    '--prompt',
    prompt,
    '--dispatcher',
    'Agent Host UI Console',
    '--async',
    '--wait',
    '1.0',
    '--json',
  ];
  if (title) args.push('--title', title);
  if (data.worktree) args.push('--worktree');
  return await runAgentctl(args);
}, 'POST');

app.api('/api/resume', async (data) => {
  const taskId = String(data.taskId || '').trim();
  const prompt = String(data.prompt || '').trim();
  if (!taskId || !prompt) throw new Error('taskId and prompt are required');
  return await runAgentctl([
    'resume',
    taskId,
    '--prompt',
    prompt,
    '--async',
    '--wait',
    '1.0',
    '--json',
  ]);
}, 'POST');

app.api('/api/stop', async (data) => {
  const taskId = String(data.taskId || '').trim();
  if (!taskId) throw new Error('taskId is required');
  return await runAgentctl(['stop', taskId, '--json']);
}, 'POST');

app.api('/api/delete', async (data) => {
  const clean = data.cleanFinished === true;
  const taskId = typeof data.taskId === 'string' ? data.taskId.trim() : '';
  if (clean === Boolean(taskId)) throw new Error('Specify either taskId or cleanFinished');
  const args = clean ? ['clean', '--json'] : ['delete', '--json', '--', taskId];
  if (clean && data.agent && data.agent !== 'all') {
    if (typeof data.agent !== 'string') throw new Error('Invalid agent');
    args.push(`--agent=${data.agent}`);
  }
  // Include the legacy default workspace even before it has been indexed.
  if (existsSync(DEFAULT_WS_RUNS)) args.unshift('--runs-dir', DEFAULT_WS_RUNS);
  const result = await runAgentctl(args);
  return { deleted: result.deleted || [], tasks: listAllTasks(40) };
}, 'POST');

app.run();
