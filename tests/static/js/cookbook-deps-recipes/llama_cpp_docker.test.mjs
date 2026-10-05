// The install recipes the Cookbook's Dependencies panel shows
// (static/js/cookbook-deps-recipes.js). llama.cpp moved from
// github.com/ggerganov to github.com/ggml-org, and the old GHCR namespace no
// longer publishes images, so a copied `docker pull` there fails (#4457).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { pickRecipe, recipeCommands } from '../../../../static/js/cookbook-deps-recipes.js';

test('the llama.cpp Docker recipe pulls the server image from ghcr.io/ggml-org', () => {
  const commands = recipeCommands(pickRecipe('llama_cpp', 'org/any-GGUF'), 'docker');
  const pulls = commands.filter((c) => /^docker pull /.test(c));
  assert.ok(pulls.length, `a docker pull command: ${commands}`);
  for (const pull of pulls) {
    const image = pull.split(/\s+/)[2];
    assert.match(image, /^ghcr\.io\/ggml-org\/llama\.cpp:server/);
  }
});
