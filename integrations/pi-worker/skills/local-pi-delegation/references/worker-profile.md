# Windows Pi/Qwen worker profile

## Runtime

- Endpoint: OpenAI-compatible TurboQuant llama.cpp server on the Windows host, port `8086`.
- Coding harness: Pi in JSONL RPC mode, launched per project through SSH.
- Model: `byteshape/Qwen3.6-35B-A3B-MTP-GGUF`, IQ3 3.06-bit variant.
- Host: RTX 4070 Ti Super 16 GB, Ryzen 7 7800X3D, 64 GB DDR5.
- Working root: the directory `ODYSSEUS_PI_WORKER_ROOT` names; the owner's setup uses `D:/Development`. Nothing outside it is reachable.
- Context ceiling: 32K tokens.
- KV cache: TurboQuant `q8_K` keys and `turbo3` values.
- CPU offload: disabled in the current performance profile.
- MTP: disabled by default because it regressed measured generation on this system. Enable it only after benchmarking the exact workload.

The installed profile measured about 5,083 tokens/s prompt processing and 156 tokens/s generation at a 4,200-token benchmark prompt. These are benchmark observations, not guaranteed application throughput.

## Assignment shape

Let Pi inspect the named files. Strong assignments involve a handful of related files and a deterministic check:
- reproduce and repair one localized bug;
- add or adjust targeted unit tests;
- implement a small handler, helper, configuration option or CLI flag;
- perform a mechanical refactor with clear boundaries;
- update project-local documentation tied to a code change;
- inspect a failing command and propose a focused patch.

Escalate to the primary harness when the work needs long-range architectural consistency, several repositories, unclear requirements, high-risk security decisions, irreversible changes, extensive browsing or more than two repair passes.

## Tool inputs

`run_pi_task` accepts an absolute `project_path`, a self-contained `task`, an optional thinking level, a timeout and a session-persistence choice. Start at `medium` thinking and use `high` only for a bounded task that needs deeper reasoning, since extra thinking costs context and time.

Pi runs on Windows, which is where the worker's filesystem access comes from. The SSH/MCP bridge exposes no arbitrary host commands and rejects projects outside the configured working root.
