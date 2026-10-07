# Pi worker operating profile

The worker is operator-provisioned. Do not assume a particular model, GPU,
context limit, quantization, operating system performance or benchmark score.

- Configure the SSH host, identity, worker script and approved checkout root.
- `AGAMEMNON_PI_WORKER_ROOT` (legacy alias `ODYSSEUS_PI_WORKER_ROOT`) restricts
  the accessible repositories. No path outside that root is authorized.
- Read returned errors and command/test evidence to judge each bounded task.
- Start with a small task and explicit acceptance checks. Keep briefs short
  enough for the deployed model's actual context window.
- A successful worker report is a claim: inspect its diff and tests before
  accepting it. Record only accepted results in an explicitly configured log.
- If the deployed worker lacks the needed capability, keep the task in the
  primary harness rather than assuming another operator's hardware profile.
