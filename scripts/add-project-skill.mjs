#!/usr/bin/env node
// Operator-side installer. Never invoked by the model's manage_skills tool.
import { existsSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export function installPlan(args, cwd = process.cwd()) {
  const options = args.filter(value => value.startsWith('-'));
  if (options.some(value => !['--yes', '--dry-run'].includes(value))) {
    throw new Error('Only --yes and --dry-run are supported. PromptScript skills are project-local; do not use --global.');
  }
  const values = args.filter(value => !value.startsWith('-'));
  if (values.length !== 2 || !/^[\w.-]+\/[\w.-]+$/.test(values[0])
      || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/.test(values[1])) {
    throw new Error('Usage: npm run skills:add -- owner/repo skill-name [--yes] [--dry-run]');
  }
  const [source, skill] = values;
  for (const base of ['.promptscript/skills', '.agents/skills']) {
    if (existsSync(resolve(cwd, base, skill))) {
      throw new Error(`Skill already exists at ${base}/${skill}. Review changes before updating with the skills CLI.`);
    }
  }
  return {
    cwd,
    args: ['add', source, '--skill', skill, '--agent', 'promptscript', '--copy',
      ...(options.includes('--yes') ? ['--yes'] : [])],
    dryRun: options.includes('--dry-run'),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const plan = installPlan(process.argv.slice(2));
    console.log(`Project: ${plan.cwd}\nskills ${plan.args.join(' ')}`);
    console.log('Review third-party instructions and resources. Installation grants no Odysseus permissions.');
    if (!plan.dryRun) {
      // Image dependencies live in an immutable /opt layer; native npm ci
      // installs resolve relative to this project. Neither path downloads npx
      // packages at invocation time.
      const require = createRequire(process.env.ODYSSEUS_SKILL_TOOLS_ROOT
        ? join(resolve(process.env.ODYSSEUS_SKILL_TOOLS_ROOT), 'package.json')
        : import.meta.url);
      const cli = join(dirname(require.resolve('skills/package.json')), 'bin/cli.mjs');
      const result = spawnSync(process.execPath, [cli, ...plan.args], {
        cwd: plan.cwd, stdio: 'inherit', shell: false,
        env: { ...process.env, DISABLE_TELEMETRY: '1', PROMPTSCRIPT_TELEMETRY: 'false' },
      });
      if (result.error) throw result.error;
      process.exitCode = result.status ?? 1;
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
