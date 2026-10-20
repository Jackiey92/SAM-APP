# M0 memory PoC

Only the dedicated PoC directory and architecture 08 are changed by this task.
No OpenViking source or imports are used. Python: repository `.venv/bin/python`.

Run from repository root: `poc/memory_m0/reproduce.sh`. This installs only the
pinned sqlite-vec dependency, downloads the dedicated BGE cache if absent,
verifies the frozen fixture hash, runs online A–C, then repeats A–C in
`unshare -Urn` with HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1.
The namespace has only loopback and no routes. `offline.py` additionally logs
all Python socket.connect audit events and rejects INET connections.
Network namespace isolation applies to native libraries too; the Python audit
is not claimed to be a native syscall trace. No system network is disconnected.

`make_fixture.py` authors synthetic acc-m0 company facts and manual goldens;
it was run before retrieval. Do NOT regenerate or relabel fixture from results.
48 facts, 17 single-golden queries; all queries are tagged tricky (period,
reporting scope, different numeric meaning, negation, units or paraphrases).
Therefore tricky and overall groups coincide. This intentionally small,
templated dataset is NOT independent real-world recall validation.
Recall@k = macro mean of |golden intersection top-k| / |golden|.
MRR@10 = mean reciprocal first golden rank (0 when absent).
SQL uses pure cosine KNN; single company scope is an explicit interface/assertion,
not a production multi-company filter implementation. M1 must add real prefilters.

A upsert is delete+insert within one transaction (vec0 API), not ON CONFLICT.
Benchmark: seeded 10,000 vectors, 512 dimensions, 30 top-20 SQL timings;
known first three distances correspond to [1,0], [1,.1], [1,.2].
Temporary benchmark database is deleted after each run. Real app.db is untouched.
B single embedding is first-inference latency (not warmed steady state);
batch=32 follows it. RSS is process high-water mark, not model-only memory.
Model revision is pinned to 7999e1d3359715c523056ef9478215996d62a620.
Dedicated download timing is in download.json (initial global cache download
preceded this and its pure download time was not separately instrumented).

Remote is a non-network skeleton. Configuration points for a future working
implementation: SAM_EMBEDDING_PROVIDER=remote, endpoint, api_key, model;
require authorization and explicit destination log, choose correct dimension,
and rebuild all projections. M0 never makes a remote embedding request.
`test_protocol.py` verifies default refusal, incomplete config refusal and
NotImplementedError even for explicit remote configuration.

Artifacts: online/offline.json hold all query top-10 IDs/scores/golden/text;
corresponding .log files hold console output. environment.json, download.json,
network-audit.jsonl and fixture-before.sha256 preserve evidence.
first-failure.log preserves the initial transformers config private-attribute
error; fix reads revision from the snapshot directory, without changing model.
The model-cache and Python runtime caches are local, ignored by Git.
