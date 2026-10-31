import { posix } from 'node:path';

const pathKeys = new Set(['path', 'file_path', 'source_path', 'destination_path', 'new_path', 'from_path', 'to_path', 'prefix']);
const opaqueKeys = new Set(['command', 'content', 'text', 'old_text', 'new_text', 'stdin']);

function relativeAppPath(value, prefix = false) {
  if (typeof value !== 'string' || !(value === '/app' || value.startsWith('/app/'))) return value;
  const normalized = posix.normalize(value);
  if (normalized !== '/app' && !normalized.startsWith('/app/')) return value;
  return normalized.slice(5) || (prefix ? '' : '.');
}

export function mapContainerToolArguments(value) {
  if (Array.isArray(value)) return value.map(mapContainerToolArguments);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    if (opaqueKeys.has(key)) return [key, item];
    if (pathKeys.has(key)) return [key, relativeAppPath(item, key === 'prefix')];
    if (key === 'paths' && Array.isArray(item)) return [key, item.map((path) => relativeAppPath(path))];
    return [key, mapContainerToolArguments(item)];
  }));
}

export function wrapContainerToolExecution(executeTool, record) {
  return async (call) => {
    let args;
    try { args = JSON.parse(call.arguments); } catch { return executeTool(call); }
    const mapped = mapContainerToolArguments(args);
    if (JSON.stringify(mapped) === JSON.stringify(args)) return executeTool(call);
    await record?.({ tool: call.name, before: args, after: mapped });
    return executeTool({ ...call, arguments: JSON.stringify(mapped) });
  };
}
