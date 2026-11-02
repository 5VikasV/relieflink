import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

if (!existsSync('.venv/bin/python')) {
  console.error('Run ./setup.sh first.');
  process.exit(1);
}

const children = [
  spawn('.venv/bin/python', ['-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8000', '--reload', '--reload-dir', 'backend'], { stdio: 'inherit' }),
  spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5173'], { stdio: 'inherit' }),
];
let closing = false;
function stop(code = 0) {
  if (closing) return;
  closing = true;
  for (const child of children) child.kill('SIGTERM');
  setTimeout(() => process.exit(code), 250);
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => stop());
for (const child of children) {
  child.on('error', error => { console.error(error.message); stop(1); });
  child.on('exit', code => { if (!closing) stop(code || 0); });
}
