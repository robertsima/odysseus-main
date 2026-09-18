import assert from 'node:assert/strict';
import test from 'node:test';
import { installPlan } from '../../scripts/add-project-skill.mjs';

test('project installer targets only PromptScript without global or all-agent flags', () => {
  const plan = installPlan(['vercel-labs/skills', 'find-skills', '--yes', '--dry-run']);
  assert.deepEqual(plan.args, ['add', 'vercel-labs/skills', '--skill', 'find-skills', '--agent', 'promptscript', '--copy', '--yes']);
  assert.equal(plan.dryRun, true);
});

test('reject global installs, wildcard agents, extra flags and shell/source injection', () => {
  for (const args of [
    ['owner/repo', 'skill', '-g'], ['owner/repo', 'skill', '--global'],
    ['owner/repo', 'skill', '--agent', '*'], ['owner/repo', '*'],
    ['https://example.com/repo', 'skill'], ['owner/repo;echo', 'skill'],
    ['owner/repo', '../skill'], [],
  ]) assert.throws(() => installPlan(args));
});
