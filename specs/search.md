# Search

Last updated: dev@f486d2b5 | 2026-09-28

## Scope

This spec covers web search, URL fetching, and search-derived context in:

- canonical `routes/search/search_routes.py`, with `routes/search_routes.py` as a compatibility shim;
- reusable outbound transport primitives in `src/outbound_fetch.py`;
- `services/search/*` and exported `services.search.SearchService`;
- `src/search/*` compatibility aliases around canonical service modules;
- search call sites in `src/chat_processor.py`, `src/tool_execution.py`, `src/session_search.py`, `src/research_handler.py`, `src/deep_research.py`, and `services/research/research_handler.py`;
- search settings in `src/settings.py`, `static/js/settings.js`, and compare/research frontend search callers;
- YouTube context paths in `src/youtube_handler.py` and `services/youtube/youtube_handler.py`;
- research visual/report consumers in `src/visual_report.py` and `routes/research/research_routes.py`;
- tests under `tests/test_search_*`, `tests/test_service_search_*`, `tests/test_services_search_*`, `tests/test_security_regressions.py`, `tests/test_agent_loop.py`, `tests/test_deep_research_*`, `tests/test_research_handler_*`, `tests/test_youtube_*`, and `tests/test_og_image_extraction.py`.

`routes/chat_routes.py` also exposes `GET /api/search`, but that route searches chat messages and belongs to chat history behavior, not web search.

## Route Flows

`routes/search/search_routes.py` owns the browser/API web-search routes:

- `GET /api/search/config` returns search configuration with provider key presence, not secret values;
- `POST /api/search` calls `comprehensive_web_search(..., return_sources=True)` and returns `{context, sources, error?}`;
- `GET /api/search/providers` returns provider metadata and availability;
- `POST /api/search/query` calls one provider directly and returns `{results, provider, time, error?}` without ranking, fallback chains, cache formatting, or content fetch.

Compare mode uses both route shapes: shared presearch uses `/api/search`, while provider/search comparison panes use `/api/search/query`. Research panels can pass provider override settings through research routes into the deep-research search path.

Research provider naming is not fully normalized in the UI: some frontend selectors still use `google`, while provider dispatch expects `google_pse`.

## Search Pipeline

`services/search/core.py` owns `comprehensive_web_search()`. It coordinates provider selection, fallback chains, ranking, optional fetch/content extraction, formatted prompt context, cache invalidation, and analytics.

`services/search/service.py` owns `SearchService`, the async facade exported by `services.search` and `services`. It wraps the synchronous comprehensive search path off the event loop and maps route-style output into service result rows.

`services/search/providers.py` owns provider-specific calls for SearXNG, Brave, DuckDuckGo, Google PSE, Tavily, and Serper. `PROVIDER_INFO`, provider availability, missing-key behavior, and provider dispatch live there.

`services/search/query.py` owns query enhancement and sanitization, including stripping markdown/code-fence noise from model- or user-supplied queries before provider calls and extracting Unicode/non-ASCII capitalized entity names. `services/search/ranking.py` owns result ranking, including word-boundary title/snippet/subject matching so short query terms do not match unrelated substrings.

## Provider Settings And Fallback

`src/settings.py` owns default provider settings. The default provider is SearXNG, with DuckDuckGo as the default fallback chain. `static/js/settings.js` owns the admin search settings UI, provider key presence display, provider selection, and fallback ordering. SafeSearch is a backend/provider setting today, not a visible Settings control.

Provider API keys come from settings or environment at call time. Web config routes expose availability/presence only, non-admin settings reads are scrubbed, and chat settings tools cannot set provider credentials.

Runtime behavior:

- disabled search returns disabled/unavailable text in the comprehensive path;
- missing keyed-provider secrets return empty provider results instead of exposing secrets;
- SearXNG general searches send `engines=<pin>` and **no** `categories`. SearXNG adds every enabled engine of a requested category to an explicit engine list, so `engines=...&categories=general` also queried all the rate-limited defaults. The pin is `SEARXNG_GENERAL_ENGINES` (default `yahoo,brave,wikipedia`, read per call); an empty value sends `categories=general` with no pin. Title-lookup engines (`wikipedia`, `wikidata`) are asked only when the query could be an article title: at most three words, no operators, no `site:` scope. They do not count as a working pin on their own, so when every other pinned engine is cooling the request goes to SearXNG's defaults. Their hits arrive in SearXNG's JSON `infoboxes` list (wikipedia's default `display_type` is `infobox`), and `searxng_search_api` turns infoboxes into rows ahead of `results`. Bing was dropped from the default: in the 2026-09-27 22:55 bundle it answered, but the relevance gate rejected most of its rows. Qwant was dropped on 2026-09-28: its first reply from the NAS was a CAPTCHA. Brave (keyless scraper of search.brave.com) was added: it returned 20 rows, 14 of them relevant, when the 2026-09-28 pin fell back to SearXNG's defaults, and the cooldown covers its rate limiting. Names that are `inactive` or removed on searxng 2026.9.25 (startpage, mojeek, presearch, dogpile, marginalia) are left out of the pin, with one warning per name. The reasons for each engine choice are in the comment above `_DEFAULT_GENERAL_ENGINES` in `providers.py`. News lookups keep `categories=news`. Retries go news -> pinned general, then without `language`, then SearXNG's defaults (`categories=general`, no pin), then the HTML fallback;
- SearXNG's `unresponsive_engines` is fed back into the pin. A pinned engine reported as suspended, too many requests, CAPTCHA, access denied, 403 or 429 is left out with exponential backoff (`resilience.cooldowns.backoff`): `SEARXNG_ENGINE_COOLDOWN_SECONDS` (default 300) on the first report, doubling on each consecutive one up to `SEARXNG_ENGINE_COOLDOWN_MAX_SECONDS` (default 3600). Reports that arrive while the engine is already cooling (parallel searches seeing the same episode) are not new strikes. Rows from the engine, or three hours without a report, reset the doubling. A `Suspended: ...` report is SearXNG's own verdict (an hour for too-many-requests by default), so it cools for at least `SEARXNG_ENGINE_SUSPENDED_SECONDS` (default 3600). On 2026-09-28 the flat 300 s let brave back in three times, only to fail again, while SearXNG already listed it as suspended. Plain timeouts do not count. When every pinned engine is cooling, the request goes to SearXNG's defaults. Every JSON answer logs `SearXNG answered N row(s) for ... (engines: yahoo=12, brave=7; asked: ...)`, and parsed rows carry an `engine` field;
- the comprehensive chain asks SearXNG for 20 rows, which costs no extra upstream requests. It applies the `site:` filter and relevance gate to all of them, then trims to the requested count;
- `site:` scoping keeps on-domain rows and puts rows under the scope's path first (on code hosts, the `/<owner>/<repo>` part). SearXNG never receives the operator: its engines ignore it or, like yahoo on a `site:github.com/<owner>/<repo>` query, answer nothing. `searxng_search_api` sends `query.site_keyword_query()` instead: the other words, then `owner/repo` for a code-host repository path, then the host (`stale vectors weaviate/weaviate github.com`). The query still counts as site-scoped, so it never goes to the news category and never asks wikipedia. SearXNG is not asked again with a different query when no on-domain row comes back. When fewer than three on-domain rows survive page 1 (`core._SITE_MIN_ROWS`), it is asked once for result page 2 of the same query (`pageno=2`, one request, no retry ladder), if at least 3 s of the deadline are left; new on-domain rows are merged and any failure keeps page 1. A `site:` value with a wildcard anywhere but a leading `*.` (`site:status.*`, `site:docs.*.com`) cannot be honoured by any engine and matched no host in the domain filter, so `query.normalize_site_wildcard()` drops the operator, keeps its concrete labels as keywords (`status`) and the answer carries a `Note:` line saying so. Other providers get the operator. When none of their rows is on the domain, they are retried once with the same keyword form;
- GitHub-issue-scoped queries first try GitHub's public issue search API (`providers.github_issue_search`). These are `site:github.com/<owner>/<repo>[/issues|/pulls]`, `site:github.com/issues ...`, `site:github.com ... issue`, a bare `github.com/<owner>/<repo>` token, or `GitHub ... issue ...` (see `query.github_issue_scope`). The request carries at most five subject terms plus `repo:` and `is:issue`/`is:pr`, and is retried once with three terms. `GITHUB_PERSONAL_ACCESS_TOKEN` is sent only to public github.com. Requests go one at a time, about 6 s apart, and a rate-limit answer starts a cooldown until GitHub's reset. GitHub rows skip the relevance gate because GitHub already ANDs every term. The provider asks for 20-50 items and re-ranks them itself (`providers.rank_github_issues`). Title matches count most (terms after light stemming, plus a bonus when they appear in query order), then body matches. Open issues rank above closed ones, and closed-as-completed above other closed ones. Recency decays with a 180-day half-life, and reactions plus comments add a log-scaled bonus. GitHub's own position only breaks near-ties. Searches without a repository (`GitHub issue ...`, `site:github.com/issues ...`) also weigh quality, because tiny zero-engagement repositories ranked high on 2026-09-28 and one became a headline finding of a research report. The search response carries no star count, and looking stars up would cost one more API call per repository. So engagement weighs more, and a repository with several matching issues gets a small bonus. An issue the repository owner filed that nobody answered, or one a bot opened, loses a little. Issues with no reactions and no comments (below `_GH_MIN_ENGAGEMENT`) always sort after engaged ones, and those from single-hit repositories come last. These rows show `(open, N comments[, M reactions])` in their title and carry `low_signal: True`. When every GitHub row is low-signal, the chain does not stop there. It asks the web providers as well and merges their rows with GitHub's (`core._merge_with_github`): GitHub gets up to half the slots, the mixed list gets the generic ranking, and the attempt reads `ok (n, no engagement)`. When the web finds nothing, the GitHub rows are returned alone. Repository-scoped searches keep the plain ranking and titles. The chain and `comprehensive_web_search` keep this order, because the generic ranker is skipped when every row is a GitHub row. The simplified retry pass does not ask GitHub again. `ODYSSEUS_GITHUB_ISSUE_SEARCH=0` turns the step off;
- comprehensive search walks the fallback chain once per provider; when every provider comes back empty (or only with rows the relevance gate rejects) and `simplify_query()` yields a shorter query (operators, quotes, years and research scaffolding dropped, first six terms, `site:` kept), the chain runs once more with it;
- the relevance gate needs one significant query token per row, two once the query has five or more. Stopwords, research scaffolding (`github`, `issue(s)`, `official`, `documentation`, ...) and bare years are not significant. Tokens match as whole words after light suffix folding (`embeddings`/`embedding`, `benchmarking`/`benchmark`), and `llama_index`/`llama-index` also match `LlamaIndex`. On a long query, one hit is enough in two cases: the hit is an identifier-like token (CamelCase, snake_case or letters+digits, such as `LongMemEval`, `refresh_ref_docs`, `BM25`), or the row sits inside the `site:` scope (under the scoped path, or on a specific domain; `github.com` alone is not specific). Each gate event logs `dropped host[engine]:hits` for the rejected rows. A provider whose rows were all rejected is reported as `irrelevant (0/n)` and cached like an empty answer. The failure message then tells the model to retry with 3-6 specific terms;
- `services/search/resilience.py` holds process-wide load guards: a per-provider concurrency gate with jittered start spacing (`ODYSSEUS_SEARXNG_CONCURRENCY`, default 4; `ODYSSEUS_DDG_CONCURRENCY`, default 2; GitHub issue search 1), a circuit breaker that skips DuckDuckGo for 90 s after three consecutive transport failures (one INFO line on trip and on recovery), and a single-flight in-memory cache of chain outcomes (5 min for results, 60 s for honest empties, errors never cached). It also holds named cooldowns (`resilience.cooldowns`) for SearXNG engines and the GitHub issue search;
- one deadline covers a whole search call: `ODYSSEUS_SEARCH_DEADLINE_SECONDS`, default 30 s (`resilience.deadline_scope`, a context variable). `comprehensive_web_search(deadline_seconds=...)`, `searxng_search_results`, the deep-research `_search` loop and `/api/search/query` each open one. Every layer below reads what is left of it. Provider HTTP timeouts are cut to the remaining time (`resilience.request_timeout`), and SearXNG's retry ladder and HTML fallback stop when it runs out. The `site:` keyword retry, the fallback providers and the simplified re-query are not started once less than 1 s is left; those steps report `out of time`. Waits for a gate slot, for paced start spacing and for an identical in-flight search are bounded by the deadline too. Comprehensive search gives the provider chain all but a fetch reserve (30%, at most 8 s) and runs the chain in a helper thread, so it stops waiting even if a request blocks past its timeout. Page fetches stop at the deadline. The answer keeps the rows found so far and the pages fetched so far, and adds a note saying how many pages were not fetched. The agent `web_search` tool passes `deadline_seconds=limit-2` and keeps its `asyncio.wait_for(limit)` only as a backstop;
- `/api/search/query` is a direct provider test/query path and does not use the comprehensive fallback chain. Direct provider result limits can be controlled dynamically by the caller.

## Content Fetching

`src.outbound_fetch.py` owns reusable synchronous public-URL classification, one-resolution-per-hop DNS pinning, redirect handling, and response-body budgets without search/content-extraction dependencies. `services/search/content.py` adapts those primitives and owns webpage extraction/cache/result shaping for the services path:

- public HTTP/HTTPS URL checks;
- DNS fail-closed behavior;
- rejection of localhost, metadata, private, reserved, multicast, and link-local targets;
- redirect revalidation on each hop;
- one-time public DNS resolution per hop plus an `httpcore`/`httpx` pinned
  transport that connects to the validated public IP while preserving the
  original URL, Host header, and TLS SNI, closing DNS-rebinding time-of-check
  drift;
- metadata, Open Graph image, list, table, code block, PDF, and text extraction;
- readable text extraction for `text/*`, Markdown, `.txt`, `.json`, `.jsonl`, and JSON content types;
- central User-Agent behavior through `WEB_FETCH_USER_AGENT`;
- soft and hard download byte caps through `WEB_FETCH_SOFT_MAX_BYTES` and `WEB_FETCH_HARD_MAX_BYTES`, with declared-length and streaming-budget checks; requests prefer identity transfer encoding so compressed bodies cannot bypass the effective body cap;
- JS-heavy empty result hints;
- cache writes;
- a negative cache (`resilience.failed_fetches`) that answers HTTP 403/404/410/451, or a URL that timed out twice, from memory for 10 minutes with the original error plus a `[cached failure ...]` note and `cached_failure: true`;
- empty/error result shape, including explicit HTTP-status failures instead of raising through callers.

`src/search/content.py` is now a compatibility alias to `services.search.content`; chat URL auto-fetch, agent `web_fetch`, and deep research keep the `src.search` import path but share the services implementation.

Agent `web_fetch` raises the per-call budget only within the global hard cap, leads tool output with a partial-content notice when the download budget truncated the page, and then applies normal tool-output truncation so the notice survives.

Content failures are caller-shaped:

- comprehensive search drops failed page fetches and keeps usable search context;
- `web_fetch` returns tool errors, including bot-protection and HTTP-status failures;
- direct URL chat prefetch turns failures into compact untrusted unavailable-page context without exposing raw URL/exception/response diagnostics;
- deep research records search/provider failures separately from extraction failures.

## Result Shapes

Search does not have one canonical result shape yet. Current shapes include:

- `/api/search`: `{context, sources, error?}`;
- `/api/search/query`: `{results, provider, time, error?}`;
- `comprehensive_web_search(return_sources=True)`: formatted context plus `{url, title}` sources;
- `SearchService.search()`: service result rows;
- agent `web_search`: tool output text plus a hidden sources marker stripped by the agent loop;
- agent `web_fetch`: fetched page text or tool error;
- deep research: findings, cited sources, optional source images, and `_last_search_error` state.

Chat/session transcript search is separate from web search but now uses `chat_messages_fts` when available, sanitizes FTS queries, and batches message lookup after FTS hits to avoid per-hit database reads.

Search owns Open Graph image extraction for fetched pages. Research owns promotion of those images into research sources and visual reports. This is not a standalone web image-search provider or gallery image proxy.

## YouTube

`services/youtube/youtube_handler.py` owns YouTube URL detection, id extraction, transcript, comment, and formatting behavior. `src/youtube_handler.py` is a compatibility alias to the canonical services module so startup `init_youtube()` state and chat imports share one implementation.

YouTube transcript and comment content is search-like external context. URL parsing covers common watch, mobile/music, embed, `/v/`, shorts, live, and `youtu.be` forms and must tolerate non-string input.

## Compatibility State

`src/search/core.py`, `src/search/providers.py`, `src/search/ranking.py`, `src/search/cache.py`, `src/search/content.py`, `src/search/query.py`, and `src/search/analytics.py` are compatibility shims or module aliases around `services.search`. Ranking helpers exposed through `src.search.ranking` include recency scoring, result ranking, naive-UTC handling, `_SPORTS_HINT_RE`, and age formats.

`src.youtube_handler` remains a compatibility import path, but it should resolve to the same module object as `services.youtube.youtube_handler`.

## Context Policy

Search results, fetched pages, Open Graph metadata, and YouTube transcript/comment content are untrusted context.

Chat search, chat URL prefetch, compare presearch, and YouTube context wrap inserted content through the shared untrusted-context message helpers. Agent `web_search`/`web_fetch` results are read-only tool outputs and must not be treated as instructions.

Deep research wraps fetched webpage content through `untrusted_context_message("webpage", content)` before extractor calls, though search result/failure shapes still differ from chat and agent tools.

## Optional And Platform Behavior

`ddgs` is optional; provider code has an HTML fallback, skipped when `ddgs` itself failed on the network (it already queried the same endpoint). Search cache and analytics state live under the shared data dir and mkdir failures in read-only image layers are tolerated where possible. PDF extraction uses `pdfminer.six` when installed and otherwise the core `pypdf` dependency. Native SearXNG defaults to `http://localhost:8080`; Docker uses the compose `searxng` service URL and pins the SearXNG image with a healthcheck.

Compose preserves retained SearXNG settings but runs `scripts/migrate_searxng_settings.py` before startup to add missing `use_default_settings: true` inheritance. The migration accepts only a regular single-document YAML mapping, preserves BOM/newline/style/ownership/mode, writes and directory-fsyncs atomically, and no-ops when the key exists. Compose treats migration failure as non-fatal so SearXNG health reports the retained-file problem instead of the wrapper command preventing startup.

`httpx` and BeautifulSoup are required runtime dependencies for the active search/fetch path.

## Operator Notes

These notes cover the 2026-09-27 changes. They come from the bundle `odysseus-diagnostics-20260927-225514`, recorded while the server already ran SearXNG `latest` (= 2026.9.25-12f8b6515).

- 174 web searches from about 5 parallel research workers made 366 SearXNG requests.
- SearXNG listed brave ("too many requests") and duckduckgo (timeout) as unresponsive on 365 of them, and google cse on 346. Odysseus pinned `bing,mojeek,presearch`, but sent the pin together with `categories=general`, which makes SearXNG query every default general engine as well. On this image mojeek is `inactive` and presearch no longer exists.
- The relevance gate kept 0 of 5 rows on 132 of 189 checks. Eight of those queries were replayed through a working search engine. On titles and URLs alone, the old gate kept 35 of 42 of its rows and the new gate keeps 42 of 42. So the rows SearXNG returned were mostly off-topic engine output, not good rows rejected by a strict gate.
- 52 searches used `site:github.com...`, which SearXNG's engines ignore.

The 2026-09-28 changes (default pin `yahoo,brave,wikipedia`, `site:` sent to SearXNG as keywords, quality ranking and web merge for GitHub searches without a repository) come from the 02:42-03:31 bundle, with the pin `yahoo,qwant,wikipedia`:

- 21 of 21 searches succeeded, all in under 10 s. The GitHub issue search went first on 19 and answered 18 (23 API calls, all HTTP 200).
- qwant's first reply was a CAPTCHA, so it was cooling down for the rest of the run. wikipedia showed 0 rows on all 3 asks, because its hit is an infobox, which Odysseus did not read then. yahoo returned 7 rows twice, and 0 on a `site:github.com/weaviate/...` query. That query then fell back to SearXNG's defaults: brave returned 20 rows and the gate kept 14, duckduckgo hit a CAPTCHA, and google cse reported "too many requests".
- GitHub searches without a repository ranked tiny zero-engagement repositories high (DGAFP/assistant-rh#471, fpt/rs-gallium#289, Early-Bird-Solutions-LLC/PinballWizard#588). One of them became a headline finding of a research report.

Server-side steps. The ZimaOS compose file is a separate copy of the repo's, so apply these by hand:

1. In the server's compose file, pin the SearXNG image to the tag the server already runs, instead of `latest`:

   ```yaml
   searxng:
     image: docker.io/searxng/searxng:2026.9.25-12f8b6515
   ```

   Then run `docker compose pull searxng && docker compose up -d searxng`, or change the image in the ZimaOS app settings and restart the app. Check the version with `docker logs <searxng-container> 2>&1 | grep -m1 '^SearXNG '`. Keep the existing entrypoint, volumes, cap set and healthcheck unchanged.
2. Optional: set these in the Odysseus service environment, only if you want to change the defaults:
   - `SEARXNG_GENERAL_ENGINES=yahoo,brave,wikipedia` (the default since the second 2026-09-28 change; before that it was `yahoo,qwant,wikipedia`, and before that `bing,yahoo`). If `SEARXNG_GENERAL_ENGINES` is set on the server, the default does not apply: remove it or set it to this value. `wikipedia` is only asked on queries of up to three words. Add `qwant` back only if it answers from your IP without a CAPTCHA. Engines marked `disabled: true` in SearXNG's defaults still answer when named explicitly. Engines marked `inactive: true` (mojeek and startpage on this image) do not. An empty value sends SearXNG's own general set.
   - `ODYSSEUS_SEARCH_DEADLINE_SECONDS=30` sets the overall time limit of one search call.
   - `SEARXNG_ENGINE_COOLDOWN_SECONDS=300` (first cooldown), `SEARXNG_ENGINE_COOLDOWN_MAX_SECONDS=3600` (backoff cap), `SEARXNG_ENGINE_SUSPENDED_SECONDS=3600` (minimum after a `Suspended` report).
   - `ODYSSEUS_GITHUB_ISSUE_SEARCH=0` turns off the GitHub issue step. `GITHUB_PERSONAL_ACCESS_TOKEN`, which the GitHub integration already uses, raises GitHub search from 10 to 30 requests a minute.
3. Optional: take pressure off the rate-limited engines for the times Odysseus falls back to SearXNG's defaults. Edit the retained settings file on the host, the one mounted at `/etc/searxng/settings.yml`. Compose never overwrites it once it exists. Add this under the existing `use_default_settings: true`:

   ```yaml
   engines:
     - name: yahoo
       disabled: false
     - name: duckduckgo
       disabled: true
     - name: google cse
       disabled: true
   ```

   If this block is already there from the 2026-09-27 notes, remove its `qwant` (`disabled: false`) and `brave` (`disabled: true`) entries. brave is now part of the pin. A pinned engine answers even when it is disabled, but brave should stay enabled so that SearXNG's defaults, the last-resort retry, still include it. With `use_default_settings: true`, SearXNG merges these entries into its defaults by name. Restart the searxng container afterwards. Leave the rest of the file alone, including `server.secret_key` and `search.formats`, which must keep `json`.
4. Verify from the Odysseus container: `python -c "import httpx;r=httpx.get('http://searxng:8080/search',params={'q':'pgvector hybrid search','format':'json','engines':'yahoo,brave'},timeout=20).json();print(len(r['results']),r.get('unresponsive_engines'),sorted({e for x in r['results'] for e in x.get('engines',[])}))"`. Expect a non-zero row count with rows from `yahoo` and/or `brave`, and neither engine listed as unresponsive. For wikipedia, ask `'q':'pgvector','engines':'wikipedia'` and print `len(r['infoboxes'])`: expect 1. If an engine never appears, drop it from the pin (step 2).
5. In the next diagnostics bundle, check these log lines:
   - `SearXNG answered N row(s) ... (engines: ...; asked: yahoo,brave[,wikipedia])`. If an engine never contributes rows, remove it from `SEARXNG_GENERAL_ENGINES`. If brave is cooling down on most searches (`searxng engine brave: SearXNG reported ...`), yahoo is carrying the load alone; consider a keyed provider as a fallback.
   - `SearXNG: sending 'site:...' as '...' (site: operator as keywords)`, followed by `searxng: no result on <domain>` when none of the rows was on the domain.
   - `Relevance gate ... dropped host[engine]:hits`. Dropped rows with 0-1 hits from one engine mean that engine returns junk. Dropped rows from good hosts mean the gate is too strict.
   - `GitHub issue search '...' returned N row(s) (K with no comments or reactions); ranked top: owner/repo #N (engagement E)`, and `GitHub issue search found only issues without comments or reactions for ...; adding web results` followed by `Merged W web row(s) with G of H low-engagement GitHub row(s) (top GitHub: ...)`.
   - `searxng engine brave: SearXNG reported '...' (strike N, backoff 300s doubling to 3600s); leaving it out for Ns` shows the backoff at work. `searxng: only N row(s) on <domain> from page 1 ...; page 2 added M` shows the second-page fetch.
   - The cooldown lines `github issue search: rate limit reached` and `searxng engine X: SearXNG reported ...`.

## Current Gaps

- Search route handlers need direct tests for request body formats, provider validation, provider availability, and route error/empty-result shapes.
- Agent search, chat search prefetch, and research search do not yet share a single result/failure shape.
- `src/search` and `services/search` are mostly consolidated through shims, but import-path parity tests remain important.
- Deep-research webpage-content extraction uses the shared untrusted wrapper, but synthesis/reuse boundaries still need route/tool tests.
- Search-sourced `og_image` URLs need an explicit privacy/security decision: documented direct browser loads, public-URL validation, or a same-origin proxy.
- Route and integration tests do not fully pin chat/compare/YouTube untrusted-context insertion.
