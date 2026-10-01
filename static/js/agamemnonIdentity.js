/* Canonical model identity for the Agamemnon soldier artwork. */
const MODEL_FAMILIES = [
  ['anthropic', /claude|anthropic/, '#d97757'],
  ['openai', /gpt|openai|o[134](?:-|\b)|codex/, '#74c69d'],
  ['google', /gemini|google/, '#7aa2f7'],
  ['mistral', /mistral|mixtral/, '#ffb86c'],
  ['local', /qwen|deepseek|llama|ollama|local|mlx/, '#b794f4'],
];
function matchIdentity(value) {
  const normalized = String(value || '').toLowerCase();
  const match = MODEL_FAMILIES.find(([, pattern]) => pattern.test(normalized));
  return match ? { family: match[0], color: match[2] } : null;
}
export function resolveAgamemnonModelIdentity(model, source = '') {
  return matchIdentity(model) || matchIdentity(source) || { family: 'default', color: '#d7b35a' };
}
export function applyAgamemnonModelIdentity(element, model, source = '') {
  const identity = resolveAgamemnonModelIdentity(model, source);
  if (element) {
    element.dataset.modelFamily = identity.family;
    element.style.setProperty('--agent-model-color', identity.color);
  }
  return identity;
}
