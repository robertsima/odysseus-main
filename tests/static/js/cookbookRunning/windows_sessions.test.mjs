// Commands the Running tab sends for Cookbook tasks on Windows hosts
// (static/js/cookbookRunning.js). A Windows task has no tmux: its output is a
// log file and its liveness a pid file under %TEMP%, and Stop has to end the
// whole process tree the runner started, or llama-server keeps the GPU.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const noop2d = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
HTMLCanvasElement.prototype.getContext = () => noop2d;

const { _envState } = await import('../../../../static/js/cookbook.js');
const { _tmuxCmd, _tmuxGracefulKill } = await import('../../../../static/js/cookbookRunning.js');

beforeEach(() => {
  _envState.hostPlatform = 'windows';
  _envState.remoteHost = '';
  _envState.servers = [];
});

const LOCAL_TASK = { sessionId: 'serve_abc', remoteHost: '', platform: '' };
const REMOTE_TASK = { sessionId: 'serve_abc', remoteHost: 'winbox', sshPort: '2222', platform: 'windows' };

// Split a command line the way a POSIX shell does (quotes and backslashes; no
// expansion, so a `$` that survives is one the shell would have expanded only
// if it sat outside single quotes).
function shellWords(line) {
  const out = [];
  let word = null;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === "'") {
      const end = line.indexOf("'", i + 1);
      assert.ok(end > i, 'unterminated single quote');
      word = (word ?? '') + line.slice(i + 1, end);
      i = end;
    } else if (c === '"') {
      word = word ?? '';
      for (i++; line[i] !== '"'; i++) {
        assert.ok(i < line.length, 'unterminated double quote');
        if (line[i] === '\\' && '"\\$`'.includes(line[i + 1])) i++;
        else assert.notEqual(line[i], '$', `unquoted expansion in "${line}"`);
        word += line[i];
      }
    } else if (c === '\\') {
      word = (word ?? '') + line[++i];
    } else if (/\s/.test(c)) {
      if (word !== null) out.push(word);
      word = null;
    } else {
      word = (word ?? '') + c;
    }
  }
  if (word !== null) out.push(word);
  return out;
}

// `powershell -Command "<script>"`: the script must hold no double quote, or
// the Windows command line ends the -Command argument early.
function powershellScript(command) {
  const m = command.match(/^powershell -Command "(.*)"$/s);
  assert.ok(m, `expected a powershell -Command wrapper: ${command.slice(0, 80)}`);
  assert.doesNotMatch(m[1], /"/, 'the PowerShell script carries a double quote');
  return m[1];
}

function assertStopsTheProcessTree(script, pidFile) {
  // A Stop-Tree function that recurses into each child process before it
  // stops the parent, applied to the pid recorded for the session.
  const fn = script.match(/function Stop-Tree\(\[int\]\$Id\) \{(.*)\}; \$p = Get-Content /);
  assert.ok(fn, 'defines Stop-Tree');
  assert.match(fn[1], /Get-CimInstance Win32_Process -Filter \('ParentProcessId = ' \+ \$Id\)/);
  assert.match(fn[1], /ForEach-Object \{ Stop-Tree \(\[int\]\$_\.ProcessId\) \}/);
  assert.match(fn[1], /Stop-Process -Id \$Id -Force/);
  assert.ok(script.includes(pidFile), `reads the pid from ${pidFile}`);
  assert.match(script, /if \(\$p -match '\^\\d\+\$'\) \{ Stop-Tree \(\[int\]\$p\) \}/);
  assert.doesNotMatch(script, /Stop-Process -Id \$p\b/, 'never stops only the parent pid');
}

test('stopping a local Windows task ends its whole process tree', () => {
  const cmd = _tmuxGracefulKill(LOCAL_TASK);
  assert.doesNotMatch(cmd, /^ssh /);
  assertStopsTheProcessTree(powershellScript(cmd), "odysseus-tmux\\serve_abc.pid");
  assert.equal(_tmuxCmd(LOCAL_TASK, 'kill-session -t serve_abc'), cmd, 'kill-session runs the same stop');
});

test('stopping a remote Windows task sends the tree stop over ssh intact', () => {
  const cmd = _tmuxGracefulKill(REMOTE_TASK);
  const argv = shellWords(cmd);
  assert.deepEqual(argv.slice(0, 4), ['ssh', '-p', '2222', 'winbox']);
  assert.equal(argv.length, 5, 'the remote command is a single argument');
  assertStopsTheProcessTree(powershellScript(argv[4]), "$env:TEMP\\odysseus-sessions\\serve_abc.pid");
});

test('a local Windows task reads its log from %TEMP%\\odysseus-tmux without ssh', () => {
  const cmd = _tmuxCmd(LOCAL_TASK, 'capture-pane -t serve_abc -p -S -300');
  const script = powershellScript(cmd);
  assert.equal(script, "Get-Content (Join-Path $env:TEMP 'odysseus-tmux\\serve_abc.log') -Tail 300 -ErrorAction SilentlyContinue");
});

test('a remote Windows task reads its log from %TEMP%\\odysseus-sessions over ssh', () => {
  const argv = shellWords(_tmuxCmd(REMOTE_TASK, 'capture-pane -t serve_abc -p -S -300'));
  assert.deepEqual(argv.slice(0, 4), ['ssh', '-p', '2222', 'winbox']);
  assert.equal(argv.length, 5);
  assert.equal(
    powershellScript(argv[4]),
    "Get-Content '$env:TEMP\\odysseus-sessions\\serve_abc.log' -Tail 300 -ErrorAction SilentlyContinue",
  );
});

test('a Windows liveness check reads the pid file on the right host', () => {
  const local = powershellScript(_tmuxCmd(LOCAL_TASK, 'has-session -t serve_abc'));
  assert.ok(local.includes("Join-Path $env:TEMP 'odysseus-tmux\\serve_abc.pid'"));
  const argv = shellWords(_tmuxCmd(REMOTE_TASK, 'has-session -t serve_abc'));
  assert.equal(argv[3], 'winbox');
  assert.ok(powershellScript(argv[4]).includes("'$env:TEMP\\odysseus-sessions\\serve_abc.pid'"));
});
