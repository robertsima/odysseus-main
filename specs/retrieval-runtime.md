# Retrieval runtime contract

The supported retrieval stack is local **FastEmbed** for vector generation and **ChromaDB** for persistence and search. It is intentionally not a fallback chain.

- `build_embedding_lanes()` creates exactly one `fastembed` lane.
- HTTP/custom embedding endpoints are not probed or selected.
- Legacy unsuffixed Chroma collections are not queried or migrated. Active lane collections use the configured Chroma client and the `*_fastembed` name.
- If FastEmbed or Chroma is unavailable, retrieval reports degraded/unavailable state rather than silently switching implementations.
- Existing unrelated LLM/research fallback chains are outside this contract.
