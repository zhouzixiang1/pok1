# National TCP Evolution Cross-Layer Alignment Matrix

This matrix is an implementation and verification index. A green test is
evidence only for the behavior it actually exercises. A row is complete only
when its authority, producer, consumer, dynamic gate, rendered prompt,
positive/negative regression, and fail-closed outcome agree.

Status values:

- **aligned** — current code and focused evidence cover the row;
- **repairing** — a concrete inconsistency or missing negative proof exists;
- **planned** — implementation evidence is not yet sufficient.

The two pinned official oracle documents are immutable inputs. This matrix
does not reinterpret them and has zero rating or strategy authority.

| Rule | Authority | Production owner | Dynamic gate | Rendered prompt contract | Data producer → consumer | Positive / negative regression | Fail-closed behavior | Status |
|---|---|---|---|---|---|---|---|---|
| platform is the TCP server, each AI is a client, default port 10001 | `AGENTS.md`; competition platform launcher | `sever/main.py`, `sever/server/tcp_server.py`; strict client CLI; managed endpoint lease | server/client launch probe, managed-socket destination guard, official harness command projection | implementation prompts preserve server/client ownership and never ask policy code to listen/connect | operator endpoint config → system client runtime → server accept loop | default-10001 and ephemeral managed-port positives; remote/unleased candidate connect and role-reversal negatives | invalid/unleased endpoint never reaches candidate code or an official run | aligned |
| 70 independent hands, fresh 20000 stacks, 50/100 blinds, national position order | `AGENTS.md`; competition platform documents | `sever/engine/game.py`; system `national_bot.py` template | native decision/runtime probe; 70-hand precommit | Master/Worker/Reviewer national profile | server hand state → typed `decision_context.hand`/`betting` | platform-alignment and native transcript fixtures; reject wrong count/position | incomplete/wrong-position sample is not admitted | aligned |
| delimiter-free raw TCP; recv is not a message boundary | `AGENTS.md`; official harness evidence | `sever/server/protocol.py`, `transport.py`; embedded strict decoder | fragmented/coalesced transcript probe; official wire recorder | all implementation/review prompts prohibit line framing | raw chunks → incremental tokens → reducer | arbitrary chunking, sticky tokens, idle/EOF; reject newline/framing APIs | malformed/incomplete token cannot mutate authoritative state | aligned |
| one canonical action and one socket owner | `AGENTS.md` strict ABI and delimiter-free official wire contract | system `national_bot.py`; managed executor/socket; runtime-probe producer and `runtime_architecture_policy.py` dynamic consumer | typed-intent probe, sandbox, send-path checks; dynamic evidence accepts only exact `fold`, `call`, `check`, `allin`, or `raise [0-9]+` with no delimiter | policy returns only pass/fold/allin/raise; system prompts never authorize raw wire | policy intent → socket validation → canonical raw wire → independently recomputed capability evidence | valid one-action transcript and canonical wire positives; strings, integers, direct call/check intents, duplicate send, malformed raise, garbage, trailing space and newline/CRLF negatives; whole runtime-probe shard `18 passed`, including strict match-control lead→fold and equality/malformed→non-fold | precomputed fallback or fold; candidate never owns socket; any non-canonical evidence wire yields no dynamic capability credit | aligned |
| raise is street-total; inclusive exact 2x is legal | official raise oracle; pinned hash | `validator.py`; strict reducer/legality; official replay | decision fixtures, replay validation, formal oracle hash | prompts state `raise_to=400` emits `raise 400`; 2x+1 optional only | legal bounds → policy context → socket token | exact 200→400 positive; below-400 and stack-consuming raise negative | illegal target sanitized to fallback; evidence issue blocks formal pass | aligned |
| postflop call/check and limp-pass semantics | platform illegal-action rules | validator, game reducer, strict `pass_wire_kind` | transcript/decision tests | Master/Worker/Reviewer exact pass mapping | street/open state → legal kinds/pass mapping | first postflop pass=check; second pass=call; reject call-open/check-after-action and BB limp-call | illegal intent cannot reach wire | aligned |
| called all-in enters runout with no later action | platform state machine | game all-in runout; strict `_in_allin_runout` reducer | candidate socket-sequence probe; formal wire pending-action check | prompts prohibit action during runout | all-in/call → runout/showdown/settlement | complete runout positive; injected extra decision/send negative | any pending/extra action is compliance failure | aligned |
| omitted terminal closer is inferred once, contribution first | official terminal behavior; `AGENTS.md` | game mirror; strict reducer; official wire replay | runtime boundary probe and formal replay | prompts describe unique proof and update order | next street/showdown/settlement → inferred action → stack/pot/tracker | paid call/check boundary positive; duplicate/ambiguous inference negative | ambiguous or duplicated inference is a wire/state issue | aligned |
| card encoding, board/blind cross-binding and showdown-only `oppo_hands` | platform wire semantics | protocol decoder, strict tracker, official wire replay | showdown/card/board/blind cross-seat evidence checks | prompts label range as reached-showdown-only | peer hole/public tokens → cross-seat proof → bounded tracker posterior | legal board/hole/blind alternation positive; early/duplicate/conflicting/colliding cards or same blind negative | invalid/unbound showdown or public-state evidence fails formal replay | aligned |
| `earnChips` is signed per-seat net; natural hand 70 has no wire pair | official terminal-settlement oracle; pinned hash | game settlement/THP; formal harness/certifier | official-full-v5 deterministic receipt | prompts forbid synthetic hand-70 settlement | hands 1..69 wire + THP 0..69/footer → certificate | exact 70 starts/69 pairs/strict THP positive; 69-only, fabricated pair, mismatched prefix/footer negative | missing identity/THP/cross-binding cannot certify | aligned |
| official timing: 60s; 250ms baseline, 54s refinement, 55s hard return, safe send delay | `AGENTS.md`; official profile | strict worker/socket owner; `official_execution_profile.json` | timing/refinement/process-tree probes | Worker/Reviewer runtime contract | monotonic deadlines → trusted worker telemetry → send | fast baseline and bounded scaling positive; late result/tree leak negative | latest legal typed intent returned; late work killed and ignored | aligned |
| five-file strict candidate ABI and system/candidate ownership | `AGENTS.md` | artifact/manifest/receipt loaders; Worker boundary | static ABI, exact-byte, import/sandbox gates | prompts allow only `policy.py` writes | prepared artifact → Worker atomic materialization → publication | exact five files/one candidate diff positive; helper/assets/system-byte change negative | candidate rejected, source bytes restored atomically | aligned |
| the content-bound bytes checked by gates are the only bot bytes executable by native, rating, precommit, Arena, official wire diagnostics and official certification | `AGENTS.md` strict ABI, candidate I/O prohibition and publication identity | strict layout; `managed_bot_executor.py`; native/runtime-probe/official/Arena/decision/wire-probe launch adapters; formal execution profile v7 | strict cache/control rejection, expected-hash source snapshot, sealed-memfd projection, owner start barrier and dynamic import/startup probes | implementation/review prompts authorize exactly the five-file ABI and no helper, bytecode, import hook or startup customization | frozen five-file manifest/hash → no-follow descriptor reads → post-read hash/layout verification → sealed memfds → optional host-owner `--block-fd` verification → `/bot` five-file read-only projection → Python `-I -B` | benign five-source launch and exact owner release positives; owner mismatch terminates/reaps and no-owner argv/FD/env remains unchanged; malicious unchecked-hash `policy` and `precompute` pyc demonstrably override ordinary source, then fail before launch; post-snapshot pyc/`sitecustomize.py` injection remains absent; wire probe rejects arbitrary script/symlink paths and host bot Popen | any owner mismatch/timeout, cache, extra/control entry, source race, hash drift, unsealable descriptor or projection mismatch fails before the endpoint is consumed; the host artifact directory is never mounted and the owner marker never enters the sandbox | aligned |
| compact system precompute is bounded, calibrated, content-bound and measurably consumed | `AGENTS.md` space-for-time contract; official evaluator/deck semantics | system `precompute.py` schema 4, generator `national-precompute-v3`, `scripts/build_national_preflop_equity_table.py` SHA-256 `5aa6808974f9af67ac7bb5189c431791d9aed9e791869f9428b1ab8e04cf62d3`, runtime manifest and policy consumer | gate pins generator/environment/evaluator/deck/random identities, fixed seed, schema/generator name, exact generated content/hash/size and consumer reachability; runtime manifest binds precompute SHA-256 `8adeab7e8122465e1a76231a32fa34d1c08c30f77e70ef978bb8093920f00627` | all five roles describe the table as system-owned calibrated heads-up evidence, not a candidate-editable ranking heuristic or strength result | official evaluator + uniform opponent/board completion sampler, 65,536 samples for each of 169 canonical starting classes → generated 169-float table plus 1,326-combo/8,192-mask/21-selection facts → bounded policy lookup | exact deterministic anchors, pair ordering, suited-over-offsuit and `A2o > K2o > 76o` positives; old heuristic values, seed/generator/environment/hash drift, candidate edit, unreachable table, truncated/giant table and evaluator mismatch negatives | identity or content drift fails before artifact use; candidate cannot replace the table; runtime retains an empty/legal system fallback and grants no capability for an unbound/non-influential asset | source-verified; final Web `2901 passed, 20 skipped` |
| the first strict system blueprint may learn bounded strategy semantics from the pinned LLL reference, but strength mechanisms must be independently system-owned, dynamically reachable and fail closed | user-authorized clean-room reference `/home/zzx/project/pok/lll/lll/bot/国赛平台代码.py` at SHA-256 `a7aef0b3b8b1a0096164631e87f9f1dd0c57b1a95c2738762c9f6301bc434dfb`; `AGENTS.md` strict ABI/evidence boundary | repository `bootstrap_assets/strict_v1/policy.py` SHA-256 `f7c6a14a0b6fdceb6f47016ba9f8048d3ce82d4baa9dfa1b88c3a74e2b24f956`, system runtime 10, evaluation contract 32, manifest schema 3, runtime-probe identity schema/orchestrator/worker/scenario `15/15/16/7`; exact system bytes are national bot `0115c5844961011d920d012edbba30eb23171de0f5649f5b46e75a0e6bd94bef` (2493/2500 lines) and precompute `8adeab7e8122465e1a76231a32fa34d1c08c30f77e70ef978bb8093920f00627` | manifest/provenance gate plus precompute content/generator/environment/hash gate, runtime-10 exact-source/manifest/control-identity and 2500-line hard-cap gates, strength-control regression gate, runtime-probe consumer-reachability gate and five-role prompt-contract gate; identities bind prepared `0ad1dd758ebc0b62f86f19bdc645abaeb5b7d48fee7513aa8a5c0c65a2721a17`, output `db439a8b92e737663951814d918ab16dfabef454c5559f87fee60ca76061d327` and first-control `b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2` | Master, Worker, Reviewer, Critic and Orchestrator all require calibrated 169-class equity, four actionable preflop spots with spot-specific raise-to-total sizing/exact typed all-in, mathematically proven match lock, position realization only for nonclosing future-street calls and opponent weighting on the current board; they prohibit inherited LLL strength, heuristic pseudo-equity, text-only controls and trusting stored capability flags | pinned descriptive provenance → generated system facts + strict runtime `decision_context` (`preflop_spot`, `match_control`, position/closing facts, current board) → repository policy → typed intent; four actionable spots are `sb_open`, `bb_vs_limp`, `bb_vs_raise`, `sb_vs_reraise`; schema-4 equity uses 65,536 official-evaluator completions per canonical class; shallow contexts cover raise-plus-allin and allin-only legality; match control binds initial chips/blinds/position/future forced blinds/hero net; current-board range weights feed flop/turn rollout without future-runout leakage | regressions cover: replacing the old hand-written 169 ordering with deterministic calibrated anchors; distinct spot bands `225–300`, `325–450`, `650–900`, `900–1200`, exact shallow-stack `allin`, and ultrashort allin-only AA jams with no weak-hand jam leak; strict `hero_net_earned > forced_fold_loss_bound` lock with equality/malformed non-fold negatives; position influence only on marginal flop/turn nonclosing calls and neutrality on river/all-in closure/missing-inconsistent facts; raise-conditioned current-board range cannot improve hero equity and malformed-board/preflop controls remain neutral; generated runtime line-count/crossover publication stays at 2493/2500 after semantics-preserving separator normalization. The whole runtime probe is `18 passed` and final-wire match-control consumer behavior is strict lead→fold, equality/malformed→non-fold, eliminating the baseline consumer `candidate_contract` failure. Existing causal wire/mixing/profile positives and negatives remain dynamically recomputed, not trusted from flags. | any LLL byte/runtime/history import, identity/hash drift, runtime over 2500 lines, old heuristic table, invalid spot/control/board/position evidence, equality lock, future-runout weighting, action-text inference, unreachable consumer, prompt drift or noncanonical wire rejects the gate or neutralizes only the unsafe strategy adjustment; system legality/fallback/socket path remains authoritative. These identities are unmerged source inputs, not publication, certification, rating or strength evidence. | source-verified; whole runtime probe 18 passed; full Web `2901 passed, 20 skipped`; unmerged and uncertified |
| candidate cannot use FS/network/subprocess/full-history/unbounded tables | `AGENTS.md` | capability AST, managed sandbox, worker process limits | static negative checks plus runtime isolation/instrumentation | Worker/Reviewer prohibit these capabilities | candidate AST/process behavior → gate ledger | alias/dynamic-I/O/history/table bypass negatives; legal bounded compute positive | violation kills worker and blocks quality/publication | aligned |
| connection-lived opponent tracker with bounded, confidence-capped influence | `AGENTS.md`; runtime architecture policy | system reducer/tracker; typed opponent snapshot | persistent-memory and selected-primary counterfactual probes | prompts require real consumer only when claimed | actions/inferred closers/settlement/showdown → bounded snapshot → policy | tracker update positive; sparse/poisoned/unreachable influence negative | untrusted metadata cannot claim capability or change wire | aligned |
| one strength sample is one compliant complete 70-hand native TCP match; W/L/D primary | `AGENTS.md` evidence authority | native runner, Elo daemon, rating snapshot/replay analysis, evaluation bundle/snapshot | admission/rebuild/chip readers require exact epoch/mode/evaluation identity, immutable rating `NativeMatchTimingPlan` snapshot+digest, safe raw replay path, exact raw-byte SHA-256 and replay/header revalidation; cycle publication validates every append row before binding | analyst/Master/Critic use only the immutable current-cycle W/L/D projection, with chips secondary; timing/heartbeat are infrastructure, never strength | content-bound replay + plan digest → staged exact-field summary + replay SHA-256 → admitted current-identity match row → immutable cycle/snapshot/history injection | complete current-identity stage→commit and exact raw-replay reopen positives; incomplete, missing/old identity, wrong epoch/mode/artifact, missing/drifted plan, typed abort, missing/replaced replay, SHA/header drift and re-signed payload negatives | missing/drifted raw or timing authority yields no history rebuild, chip signal, replay analysis or cycle; retained-cycle cleanup may not delete referenced raw bytes; a foreign/invalid append row rejects publication before ratings/selection freeze | source-aligned; live pending |
| official EXE and Arena have zero strength authority | `AGENTS.md`; official policy | official evidence/certifier; Arena diagnostic projection | evaluation identity weight=0; exact native-mode history admission and append-log publication checks | every role labels compliance/diagnostic only | official/Arena evidence → compliance/UI only | poison winner/chips sentinels carrying `official_exe` or `national_arena` mode are excluded from H2H/chips/selection and rejected at cycle publication | any strength contamination or mutable status issue is excluded; there is no live-history fallback | aligned |
| paired publication namespace, strict publication eligibility and one-time epoch initialization are separate immutable authorities | `AGENTS.md` trust boundary and one-time reset contract | canonical namespace resolver shared by runtime/epoch/scheduler/reset/reconcile; strict publication resolver; schema-2 reset receipt | both exact annotated completion/high-water tags must peel to the same commit; v143+ high-water must also equal the highest eligible artifact/tag-tree/signed-certificate publication; stopped-checkout/PID/schema-2 execute-receipt validation | bootstrap prompts describe v142 as numeric identity only and expose no source/history bytes | paired tag refs → numeric namespace; exact strict artifact/certificate → executable publication; stopped-runtime execute receipt → epoch initialization | real paired v142 positive; unpaired, lightweight and wrong-commit refs, higher completion-only artifact, transient second-read failure, directories, bare commits, counters, dry-run/pre-binding/second-reset and v143+ debris negatives | incomplete or unavailable namespace authority does not advance the namespace and exposes no active Bot to scheduler/prompt/API/UI; numeric strict ref without exact eligible publication projects recovery and blocks launch/allocation | repairing |
| current-epoch version allocation may reserve a label only through a schema-2 active checkpoint or its unique digest-chain-head abandon receipt | `AGENTS.md` canonical checkpoint and identity-continuity contract | epoch authority projection, checkpoint schema, scheduler, atomic `abandoned_versions.jsonl`, shared publication lock, schema-2 abandon transaction and stopped-runtime reconciliation | checkpoint binds published high-water, abandon floor/head digest and allocation floor; durable claim binds checkpoint/reason/HEAD/candidate preimage before mutation; receipt binds exact envelope/workflow/revision and previous digest; idempotence re-fsyncs the unique chain head | roles receive published high-water and exact checkpoint target only; legacy ledger/history and transaction quarantine have zero prompt or allocation authority | paired namespace + validated abandon floor/head → next label; exact checkpoint → in-flight target; dual durable claim → exact receipt → atomic content-addressed quarantine → checkpoint CAS → terminal receipt | valid schema-2 checkpoint/two-receipt chain/clear-retry/CLI dry+execute resume/exact prepared retry positives; older receipt, non-successor, orphan target, claim path/count, source+quarantine, drift, Git error, symlink/hardlink, malformed/partial/reordered ledger negatives | scheduler/writers fail closed; live claim blocks launch; ambiguous or failed effects retain exact recovery evidence; target names are never deletion/adoption authority | repairing |
| v143 is empty-pool, no-strength, two-step operator-only first strict publication | `AGENTS.md`; reset/bootstrap receipts | scheduler, strict authority workflow, fixed system blueprint, repository-owned first-strict control, official bootstrap/finalize CLI | parked receipt + valid unused control artifact/ledger state + green doctor + 5/3×70 certificate + completed authorization + PID-scoped finalize guard | proposal Scouts expose prepared v143 only; proposal/Worker text says capability-audit lens and fixed bytes, never causal or strength improvement; final prompt never exposes v142 or operator commands | reset receipt + prepared fixed v143 → typed capability gates → parked job → signed certificate → operator finalize | control hash/unused `0/1`, rendered prompt/receipt/read-scope/fixed-blueprint wording and four-state transition positive; v142/history/automatic bootstrap/LLM commit/strength claim negative; live tag/certificate evidence still required | present control bytes do not unlock bootstrap before `official_bootstrap_required`; pipeline parks, invalid/ambiguous jobs fail closed, and only exact `ready_to_finalize` can publish | source-aligned; live pending |
| singleton v144 is the first strategy generation and establishes normal full-v5 plus the first rating-ready two-bot pool | `AGENTS.md` normal certification and evidence authority; delivery runbook | checkpoint-bound singleton Master/precommit resolver, normal official job/certifier, publication transaction, native Elo daemon and immutable cycle publisher | exact published v143 parent/active-pool/receipt authority; parent precommit regression; exact 5 self-play + 3 eligible-v143 rounds × 70; signed certificate/tag and content-addressed native cycle | no strength snapshot is invented; roles read exact published v143 plus prepared v144 and label the measurement a hypothesis; official compliance/chips never become strength | published v143 + inherited prepared v144 → proposal-v4 reachable mechanism → typed probe + parent precommit → normal full-v5 → published v144 → native W/L/D cycle | receipt/live-parent/pool drift, missing snapshot, first-control fallback, candidate-as-opponent, incomplete rounds and official-strength contamination negative; singleton positive source tests green; live v144/cycle proof still required | any authority drift yields no opponent/plan; v144 remains unpublished and rating-ready false until certificate/tag/native cycle bind exact identities | source-aligned; live pending |
| every active LLM role has an explicit system-rendered prompt, exact read/write/tool/model scope and producer-derived evidence authority | generation order; role prompt/evidence contracts | 19-role `ACTIVE_LLM_ROLE_CONTRACTS`; semantic producers; typed renderer/evidence/MCP/dispatch receipts; strict per-invocation logs | producer replay, source/template/MCP hashes, frozen capabilities, accepted-effect and exact-log reproof | callers supply typed semantic material only; proposal Scouts receive compact frozen facts rather than the final-Master tutorial; strict descriptors own semantic inputs; v143 Critic has an explicit no-strength prompt and empty read scope | frozen input → semantic producer → sealed prompt → guarded provider → accepted effect → generation-bound `strict_invocations/<id>` log/evidence | valid 19-role dispatch, Scout capability/read-denial, accept-before-bind recovery, final projection and opaque-log positives; forged prompt/provenance, root/version/path, duplicate marker, poisoned history, unknown role/tool/model and truncation negatives | any scope, prompt, effect or evidence mismatch canonically abandons with zero provider-retry debt; a denied read returns no bytes and creates no evidence; stale real/replay dispatch cannot cross an abandoned child tombstone | repairing |
| planning evidence is immutable, same-identity, generation-scoped | `AGENTS.md`; evidence snapshot contract | evaluation bundle/snapshot, master context, replay spotlight | manifest/hash/cutoff/citation validation | roles read only exact frozen paths | committed cycle → generation snapshot → planning/audit | frozen/current identity positive; live/copied/tampered/stale/symlink snapshot negative | planning blocks; no live fallback | aligned |
| retired history, archive, experience and free-form lessons have zero authority | `AGENTS.md` trust boundary | epoch authority, resolved-path role guards, active-scope tests | poisoned-sentinel/render/read-capability guards | prompts whitelist current strict evidence only | strict completion tags/current snapshots only → roles | current strict completion positive; `.git`/v1..v142/archive/failure/lesson/delivery-doc sentinel negative | input absent or role blocked; never summarized as strategy | aligned |
| provider conversation history cannot cross a process, epoch, workflow or checkpoint boundary | role history contract; checkpoint evidence authority | `orchestrator_session.py`, Orchestrator SDK options and owned-stream watchdog | legacy-sidecar deletion plus pre-dispatch `resume is None` assertion | Orchestrator receives a freshly rendered sealed prompt and typed MCP projection only | validated checkpoint → new rendered prompt/MCP identity → fresh provider stream; opaque session IDs are discarded | fresh stream positive; bare legacy sidecar and forced stale loader negative; dynamic SDK options capture | any attempted `resume` fails before provider dispatch; recovery keeps checkpoint but imports no server-side history | aligned |
| Master produces three distinct proposals, two anonymous ballots, one selected executable mechanism | generation order; strict workflow contract | strict authority workflow, LLM query, proposal-packet-v4 parser/compiler and quality consumer; first durable effect freezes one revision for all six bootstrap Master slots | system-verified ABI-reachable decision-first anchors; exact strength-node projection/digest; six-field 70-hand measurement; two-ballot veto recomputation; prepared-symbol AST digests; full binding/cache/typed-probe reproof | fresh target-only and fixed-blueprint audit; singleton exact published parent+target with no invented strength; normal exact source+target+frozen strength nodes; future edges only in proposed diff; final prompt cannot synthesize a fourth proposal | frozen facts/source AST → three proposals → two votes/veto → exact selected contract → preserved Worker block → reachable AST delta + candidate typed check → later native strength samples | three-mode positive, snapshot metadata/candidate-target/empty-or-text delta, veto tamper/all-reject, utility-anchor crowdout, retry-feedback loss, prompt truncation, binding/cache drift, unchanged reachable chain and missing/failed typed check negatives | invalid packet or all-veto blocks Master; invalid binding gets deterministic next-prompt repair; strategy quality cannot pass on unrelated file change or inherited generic capability alone; typed evidence is explicitly not a full counterfactual/strength claim | source-aligned; live pending |
| Critic is mandatory execution evidence but its strategic verdict is advisory | `AGENTS.md` generation order; native precommit authority | `tool_gates.py`, `pipeline_state.py`, `tool_planning.py` | schema/content-bound Critic receipt followed by native TCP precommit | Critic and Orchestrator prompts say score cannot authorize Worker rework | Critic role result → checkpoint advisory receipt → precommit context only | schema-valid low score proceeds; incomplete receipt retries Critic; retired `critic_repair` task and direct `critic_checked`→Worker transition negative | invalid receipt reruns Critic; legacy repair contract requires controlled abandon/re-prepare; candidate bytes remain unchanged | aligned |
| quality/precommit/official gates cannot be environment-soft-passed | `AGENTS.md` generation order | `tool_gates.py`, `tool_eval.py`, `national_native.py`, official certifier | production profile, attempt-local monotonic native cancellation token, frozen first-strict execution scope and publication transaction | prompts cannot request bypasses and describe infra retry as full artifact/gate/baseline reproof, not directory existence | gate receipts + frozen execution scope → complete 70-hand units → checkpoint → staged blobs/tag | required executed/conclusive 70-hand pass and same-scope journal recovery positives; skipped/pending/inconclusive/env-disable/passed-with-issues, late cancelled admission, revived token, duplicate first-strict match and fingerprint/gate/baseline drift negatives | cancellation admits no late sample or terminal gate; infra overlay remains blocked on any reproof mismatch; publication remains blocked | repairing |
| a complete local-strength 70-hand match has one immutable timing identity; legal candidate policy choices are never mistaken for a protocol failure | `AGENTS.md` local-strength/refinement/raw-TCP rules; fixed-stack/min-raise authority in `sever/engine/validator.py`; generic engine guard in `sever/engine/game.py` | schema-5 `NativeMatchTimingPlan` in `national_native.py`; runtime-heartbeat schema 4; native-progress schema 4; `NationalTCPGameEngine`; checkpoint-bound quality/precommit reporters; first-strict journal; rating/replay consumers; bounded orchestrator sidecar | exact plan snapshot+digest (fixed 0/2.0/1.8/0.2 profile), active-validator tight 34-request/hand bound for **every** normalized hand count, and one fixed 5,960 s operation/lease: 300 s capacity + 2×30 s read-only preparation + 120 s startup + 5,415 s engine + 35 s cleanup + 30 s post-execution durable completion. Fixed phase ceilings are launching 480 s, engine-running 5,415 s and finalizing 65 s; launching emits every 30 s and authority re-proves every 5 s. The separate generic 100-request/street abort remains fail-closed. | Master/Worker/Reviewer/Critic/Orchestrator state that plan, cap, fixed phase deadlines, heartbeat/reproof and extension are system infrastructure; only the system reducer owns `pass → call/check`; no role may renew a deadline or treat a heartbeat as strength | runtime-only prelaunch identity → capacity queue → bounded artifact binding → startup watchdog → engine watchdog → annotation + rehash + terminal validation → bounded durable journal seal → quality/precommit/rating/replay/snapshot consumers; launch/engine/finalizing event → checkpoint-plan/PID/provider-nonce-bound runtime heartbeat → five-second authority reproof → at-most-once absolute stream extension → successful `runner_returned` only after durable seal, while raised/cancelled cleanup terminals remain non-authorizing | 34 legal-request full hand and 35th fail-closed request; one-hand projection parity; plan/environment/digest tamper; launch-before-queue, queue/preparation/startup timeout; fixed-deadline/non-rolling launch, engine and finalizing events; 30 s launch heartbeat and 5 s reproof; `runner_returned`-after-seal plus raised/cancelled cleanup ordering; whole-match vs handshake; typed abort quality/journal/rating; foreign/checkpoint-plan/terminal/stale/wrong-stage/old-stream heartbeat and exact cap; legal `raise 208`, host-owned pass→check/call. The server bind/create await is now inside the same absolute 120 s startup watchdog; its focused native suite is `17 passed`. The flock/SQLite durable-completion path now obeys the remaining 30 s, preserves the running effect on timeout, creates no false seal and recovers with the same ticket; the completion-recovery aggregate is `60 passed`. | missing/drifted plan, unbound/over-time queue/preparation/startup/engine/finalization/completion, typed abort, timeout/incomplete hand count, stale/foreign/terminal heartbeat, failed five-second reproof, rejected terminal clear without retry or a second extension cannot pass/admit/replay/rate; `runner_returned` authority is withheld until annotation, rehash, terminal validation and durable seal all succeed; raised/cancelled cleanup never authorizes completion; timing/evaluation-contract drift requires controlled abandon/re-prepare | source-aligned; unmerged |
| a completed native runner bridges to its provider result through one bounded process-local terminal authority, and durable first-control completion cannot depend on one cross-thread wakeup | `AGENTS.md` fixed timing/owner/fail-closed recovery contract | schema-1 terminal handoff in `pipeline_state.py`; outer terminal publication and `_await_first_strict_control_completion` in `national_native.py`; bounded consumer in `orchestrator.py` | dispatch lock precedes heartbeat lock; exact one-shot receipt binds checkpoint/owner/nonce/match/timing/operation/event/outcome; the durable control writer is an `asyncio.to_thread` task polled every 50 ms with `asyncio.wait`, and only durable `COMMIT` is authoritative | prompts expose no handoff/journal controls; Orchestrator treats liveness as system-only infrastructure, never strength or provider-authored completion | runner terminal after release → receipt → bounded consumer; control writer → periodic task poll → durable commit; cancellation drains the writer before re-raising | live/receipt success, rollback, one-shot/expiry/tamper and return/raise/cancel regressions; completed-future-with-lost-callback wakeup, cancellation-drain and durable-COMMIT positives/negatives; current complete Web regression is `2901 passed, 20 skipped` | unlink failure revokes authority; wrong identity/outcome/expiry rejects result; a missed callback cannot stall forever; cancellation never detaches the writer; absence of exact durable `COMMIT` cannot complete first control | source-verified; unmerged |
| publication cross-binds working bytes, staged blobs, signed certificate, annotated completion/high-water refs and remote tree | `AGENTS.md` publication transaction | `tool_commit.py`, strict authority workflow, epoch registry and certificate validators | pre/post-commit artifact digests, index/blob checks, annotated-tag type/tree and remote-ref verification | Orchestrator may invoke only the canonical publication tool after every hard gate; roles cannot synthesize completion | immutable candidate + gate/certificate receipts → staged tree → commit/tag/high-water → remote completion identity | exact five-file tree/certificate/tag publication positive; dirty-byte, index drift, lightweight/wrong-tree tag, partial push and directory-only negative | rollback/repair leaves generation unpublished; no `.completed`, tag or version advancement is inferred | aligned |
| signed publication is followed by one exact eight-step durable handoff before the next generation | `AGENTS.md` generation order and post-publication contract | `post_publication_handoff.py`, `tool_commit.py`, `cycle_archivist.py`, `evolution_infra.py`, Elo signal consumers | schema-2 active pointer/journal; first authority-directory child+parent fsync/inode proof; exact plan/output schemas; operational and external effect reproof | A provider that sees pending/running/blocked handoff must `end_stream`; only outer deterministic recovery may invoke `run_archivist`, and Archivist input is a zero-memory allowlisted exact-publication projection | publication identity → provider end-stream → outer-owned stability row → locked daemon refresh/priority signals → high-level append-log rotation freeze → non-destructive strict-log archives → schema-2 frozen multi-reap → annotation → no-commit housekeeping | provider/PreCompact pending+blocked projection, crash/reclaim/idempotent replay and all eight exact effects positive; provider Archivist attempt, forged-skip/re-signed output, missing/vacuous receipt set, first-mkdir fsync failure, rotation prefix/predecessor drift, live-log deletion, target recompute, unlocked signal read/unlink, annotation/worktree drift negatives | active pointer remains pending/running/blocked; provider/checkpoint/prepare/post-cleanup cannot outrun the obligation; completed-looking receipts do not bypass live reproof | repairing |
| backend is the authority; frontend renders typed projections without recomputation | architecture docs; canonical recovery/launch contracts | epoch/control/pipeline/rating/official/log routes; AppState and process LLM-manager owner fences; TS API/types/pages/controllers | epoch/handoff double sample, checkpoint before/read/after tri-state, stability TTL/digest fence, signed validators, paired checkpoint revision, launch-fence double sample, typed scheduler boundary, bounded handoff owner scope, results-root descriptor log read and dynamic frontend tests | UI uses `advisory_approved`, renders handoff/blocked/operator states, enables Start only for one exact boundary, and labels `daemon_pairs` as a 1..8 complete-70-hand sampling/throughput budget rather than strength proof | canonical epoch/checkpoint/recovery/handoff/owner/stability/jobs/cycle/log and persisted daemon config identity → typed API/SSE → UI/daemon CLI; initialized clean absence → `next_v` + `source_v=null` scheduler boundary → UI | exact current/dead/foreign owner, three launch boundaries, route/profile/5/3/70/handoff/no-daemon and daemon-pairs 1/8 positives; 0/9, process-argv/config/stability/frontend drift, terminal-looking/disappearing checkpoint, unowned lifespan, stale manager clear, negative Critic, unrecoverable route/start, forged scheduler, old revision, poll/stream loss and symlink-swap negatives | API withholds route and returns 409 before stability/task ownership; invalid daemon budget is rejected consistently; live foreign owner blocks a second runtime; failed/unowned launch cannot alter live running/UI/managers; UI clears stale state and disables invalid controls | source-aligned; live pending |
| continuous delivery and crash-safe generation recovery | generation scheduler/workflow contracts; canonical schema-2 abandon transaction | `orchestrator.py`, `generation_scheduler.py`, `tool_runtime_guard.py`, `tool_bot_management.py`, `tool_eval.py`, both workflow journals, publication/handoff/daemon | tri-state checkpoint observation; exact pending ToolUse/result/owner binding; active timeout leases and exact CAS; deterministic recovery and post-publication cleanup; startup recovery before resume-ACK consumption; typed continuous/one-gen exits | Orchestrator follows only the exact checkpoint route: selected first-materializes; preparing crash-recovers or system-abandons an unbound preimage; timed_out canonically abandons; infra_timed_out re-proves artifact/gates/baseline before precommit; no checkpoint means provider end-stream and outer-only prepare_generation | validated checkpoint + uniquely bound authorized-owner result + complete journals → abandon/publication terminal action → outer scheduler; cancellation token + frozen first-strict scope → same execution journal; publication → provider end-stream → proven outer cleanup | selected/preparing/timeout, same-scope cancellation recovery, multi-stage driver and exact abandon positives; restart/identity overwrite, unknown/reused/swapped/unsettled result, EOF pending, malformed/disappearing checkpoint, ACK consumption before blocked recovery, continuous false-zero exit, journal drift and cleanup failure negatives | invalid checkpoint/proof/journal/cleanup is recovery_blocked without consuming operator ACK; timeout and foreign owner remain active fences; abandon/operator/recovery/accounting map to non-success; live v143+ proof remains pending | repairing |
| ten consecutive generations after last repair/restart | user acceptance contract; delivery runbook | system stability projection plus operator ledger | boot/branch/HEAD/contract/runtime-config/daemon/tag/cert/cycle verifier with coalesced TTL snapshot; only the exact proven publication commit may advance HEAD | UI displays N/10 only for unexpired backend `verification.state=fresh` | process boot + config + repository identity + published identities → background verification → observation projection | uninterrupted single-commit publication increments; intervening HEAD/branch drift, config/repair/restart/PID reuse/remote drift and pending/stale/failed verification negatives | persisted reset to 0 on detected drift before the next admitted row; unverified cache immediately suppresses count without inventing a pass | planned |

## Current dynamic evidence

### 2026-07-16 — workflow-v30 quality evidence invalidated the old native liveness budget

Fresh `generation:143:workflow-v30` reached Worker completion after three
accepted Scouts, two anonymous ballots and a zero-tool final Master. Its first
first-strict self-play quality attempt did **not** prove a candidate or TCP
failure: the outer 600 s match timer interrupted a healthy match at hand 46/70.
The captured result had zero illegal actions, zero action timeouts and zero
process failures; both sides used the configured 2.0 s hard/1.8 s refinement
envelope. A concurrent retry was deliberately stopped through the authenticated
local control route rather than allowed to manufacture two more identical
inconclusive receipts. The checkpoint remains durable at `workers_done`; no
candidate, rating, tag or certificate was accepted.

The same audit found two linked producer/consumer defects. `420` s precommit
and the rating runner's implicit `280` s default could truncate the same real
70-hand local-strength path. Separately, the candidate fixture named
`native_postflop_facing_check_passes_with_call` required a preflop `call` even
though `raise 208` is legal under the authoritative server validator; that was
a false candidate failure, not a permission to weaken validation. The pending
repair centralizes the timing-derived liveness budget and moves `pass→check` /
`pass→call` assertions to host-owned reducer fixtures. It now derives the
complete-match envelope from the active four-street × 100-action engine cap,
emits an explicit cap failure instead of silently closing a street, places the
same budget inside the first-strict seal/journal before the outer idempotent
completion, and records a distinct whole-match timeout phase. It retains raw
TCP parser+validator checks for every candidate wire action. The preliminary
70/70 run on the superseded eight-slot floor remains diagnostic only; no final
dynamic run is active, and a fresh corrected-code 70-hand self-play plus full
regression suite are required before this row can move out of `repairing`.

Runtime consumed merged `8d623ca7`, rotated the changed rating authority to
empty instance `771bfaeb48b64b248ce3fd3be6c4a906`, and bound manifest
`f8ef8c2aa6ab28b13c9b5bcec947d4e980d1ddc98f0de1dfdbe53f469da45de1`
when the canonical native daemon started. The prior identity archive is
`web/core/results/archive/evaluation_identity/20260716_141841`. Official doctor
remained green.

Workflow-v28 dynamically closed the prior Scout blocker: exactly three valid
content-bound proposals and two anonymous critic ballots completed. The final
Master then exposed a new, narrower efficiency mismatch. Its already compiled
97,478-character prompt still granted directory-wide Read, so all three
infrastructure attempts redundantly loaded `policy.py`, `precompute.py`, and a
25k-token partial `national_bot.py`, then hit the same 132-second trusted-output
stall while thinking telemetry continued. The checkpoint/candidate remained
fenced and v28 canonically abandoned with receipt
`7953e317aecc28ce1ef3659837fad4c02c8491615cec7bc6f10a5cd04a3fd6eb`,
transaction
`63ace03409fd33d351021cdb6bac693f24c5a8a896a64065fe62340de6ace462`,
and finalize receipt
`fdc59860bea80740dc01133bde8fcc86c8702a01220a7ef2dfbff6eec2d019d8`.

The automatically prepared workflow-v29 was stopped before repeating that cost
and canonically abandoned on the unchanged contract HEAD with receipt
`0e9ea4843761e42a3ecf410aac6b4f92718e3b53c643a96812e57690ec24f1f3`,
transaction
`db52589abcf72d42d6b356299568cfc1fc45fa3761267a23be958e9b558176d1`,
and finalize receipt
`41ae0d93611eb7ef900c8d188007630bd1e021b72ec7d76f354e5310ffaa4695`.
Runtime is now stopped and clean at `8d623ca7`, with no checkpoint/candidate.

The current repair removes all final-Master tools/read scope, keeps the frozen
evidence tripwire, binds an empty strict `master:final` tool set, and gives only
the exact final compiler role 240-second first-substantive-output and
post-substantive silence ceilings under an isolated `strict-authority-v2`
journal and unchanged 900-second total. Fresh workflow-v30 is the first
allocation allowed to use v2. Scouts retain code exploration; proposal symbol AST
digests and selection binding remain deterministic; Workers still read the
leased target. Thinking telemetry remains non-authoritative. Web verification is
`2757 passed, 20 skipped`; the next live proof is fresh workflow-v30 after the
stopped runtime fast-forwards to the exact merged commit.

The stopped runtime has now fast-forwarded only through `origin/main` to the
exact merged repair `f6c1c86aeffce9f98970744237a242bda161eb30`. Post-sync
reproof remains fail-closed and clean: no evolution process, checkpoint,
candidate or reconciliation claim; epoch state `fresh_bootstrap_ready` with
`current_v=142`, `next_v=143` and zero active strict bots; checkpoint recovery
`active=false`, `recoverable=true`, `issues=[]`. The existing evaluation
instance `771bfaeb48b64b248ce3fd3be6c4a906`, base identity
`0f3094ac881e0873f8776d6a12e96ea5ca74d8994a1e7bedfc26a03a85f2f996`
and manifest
`f8ef8c2aa6ab28b13c9b5bcec947d4e980d1ddc98f0de1dfdbe53f469da45de1`
all revalidate byte-for-byte against the repair, so no rating identity was
rotated. Runtime official doctor is still `ok=true`. A fresh focused rerun of
the final-Master/strict-journal/proposal/prompt/abandon causal chain is
`232 passed`; workflow-v30 has deliberately not been allocated or started at
this handoff fence.

Official doctor is green and `first_strict_control_v1` hash
`2a0d58ed7126e46a04107903633ae7667e8196ae4d6a26b8aca60c8e18245c33`
remains valid, unused, and `0/1`. No v143 official dependency is missing; the
operator action stays locked until the exact fresh checkpoint reaches
`official_bootstrap_required`.

Codex-only helper commit
`81b75070d550e9000aced1d79f909ccf843011e2` remains outside this matrix's poker
authority graph. Its exact wheel was manually installed and passed persistent
cold-start/deep/STDIO canaries, but that user-side operation neither changed nor
restarted poker runtime and supplies no prompt/checkpoint/rating evidence.

## Completion rule

Merged `8d623ca7` retained the frozen Web `2753 passed, 20 skipped`, sever
`31 passed`, frontend `18/18`, ESLint/TypeScript/Vite build, active Python
compilation, `bash -n`, diff check and official-doctor evidence. The current
final-Master repair adds Web `2757 passed, 20 skipped`, sever `31 passed`,
frontend `18/18`, ESLint/TypeScript/Vite production build (165 modules), active
Python/shell checks, diff check and official doctor `ok=true`, plus focused
zero-tool, strict-journal, prompt, timeout and proposal-governance coverage.
The stopped runtime must fast-forward to its exact merged commit. Source gates
do not replace merged-commit or live runtime evidence. The separately verified
Codex helper does not change any row's status or count as live national-protocol
evidence.

Before this matrix may be marked complete, each **repairing** or **planned** row
must cite the exact test command/result and the merged commit containing its
producer, consumer, and fail-closed path. v143/v144 and the ten-generation
rows additionally require runtime evidence; source tests alone cannot close
them.

## 2026-07-16 post-audit source evidence (historical unmerged checkpoint)

This records the schema-4/schema-3 checkpoint that preceded the current
schema-5/schema-4 contract; it is retained as audit history and is not the
current execution instruction. At that checkpoint, the timing and
strength-history rows had source-level causal proof:
the first-strict lease includes fixed capacity, preparation and execution;
runtime-heartbeat schema 4/native-progress schema 3 are checkpoint-plan-bound
and acknowledged-terminal-cleared; and every admitted history summary
reopens exact SHA-bound raw replay bytes before it can influence H2H, chips,
cycle publication or prompt history. Regression evidence includes a legal
34-action hand, a 35th-action typed abort, one-hand projection parity,
queue/lease calculation, forged/old/terminal heartbeat rejection, exact raw
replay acceptance, and missing/hash/header replay rejection. Full Web
verification is `2773 passed, 20 skipped`; `sever/tests` is `33 passed`; the
frontend TypeScript/Vite build passes with 165 modules. These are source facts
only: there is no merged commit, runtime recovery, v143 certificate, v144,
immutable rating cycle or stability observation credit yet.

## 2026-07-16 presentation-numbering decision

The user narrowed “Bot 1..N” to a Web-only sequence. It does not rename
`national_v143+`, replace paired annotated tags, or enter any producer/consumer
in the evaluation, evidence, prompt, checkpoint, rating or certification graph.
The Bot page orders the exact backend-admitted published pool by physical
version for its display ordinal, keeps that ordinal independent of strength
sorting, and shows the real completion tag beside it. Missing publication/tag
authority renders `tag identity unavailable`; candidates and directory debris
receive no ordinal. Regression guards verify that the ordinal block does not
read rank/selection score or mutate DTO state. The current live acceptance
remains unchanged: no published Bot, no rating cycle, and `0/10`.

## 2026-07-17 current unmerged timing and bootstrap authority

The current source contract supersedes the historical 5,910-second plan.
`NativeMatchTimingPlan` is schema 5, runtime heartbeat is schema 4 and nested
native progress is schema 4. One operation and first-strict lease are exactly
5,960 seconds: 300 capacity + 2×30 preparation + 120 startup + 5,415 engine
+ 35 cleanup + 30 post-execution durable completion. Phase deadlines are
fixed at launch 480, engine 5,415 and finalizing 65 seconds; launch publishes
at most the same fixed interval state every 30 seconds, and the authority is
re-proved every 5 seconds without rolling or extending any phase deadline.
Only a `runner_returned` terminal handoff may authorize completion, and it is
published after the applicable annotation, replay/output rehash, terminal
validation and durable journal seal. `runner_raised` and `runner_cancelled`
also publish from outer `finally` after resource release, but they are cleanup
signals and the consumer rejects them as completion authority.

Both timing P1s are now repaired and covered. The server bind/create await is
inside the same absolute 120-second startup watchdog, with typed timeout and
safe cleanup before any client launch (`17 passed` in the focused native
suite). The first-strict flock/SQLite completion uses the remaining absolute
30-second budget without a detached writer; timeout preserves the running
effect, emits neither inbox/event nor false completed seal, and the same ticket
can recover. Its workflow/native/hidden recovery aggregate is `60 passed`.
The superseding completion bridge is a schema-1, process-local, one-shot
terminal handoff, not a requirement that the final live heartbeat remain on
disk. Lock order is dispatch then heartbeat. It binds exact checkpoint, owner,
nonce, match, timing, operation, event and terminal-outcome fields. Live state
converts to a receipt and then unlinks; unlink failure rolls the receipt back,
the next live match clears an old receipt, and consumption immediately revokes
the nonce. Its expiry is fixed at no more than 30 seconds and never rolls. The
runner publishes only from its outer return/raise/cancel boundary after all
resource release. Orchestrator periodic and done paths try exact live authority
first, then the receipt; current checkpoint proof permits only the same
workflow/version, a non-regressing revision and the explicit stage set for that
owner. Focused evidence is terminal handoff `11 passed`, timeout file
`76 passed`, native `17 passed`, nonworker `13 passed`, cleanup `9 passed`,
recovery `7 passed`, and services `32 passed`.
These are source proofs only. A later runtime/precompute/evaluation-contract
change invalidated the earlier `2853 passed, 20 skipped, 1 warning` full-Web
result as a current freeze proof; the replacement final Web rerun is `2901
passed, 20 skipped, 1 warning` in 156.14 seconds. The tree is
still unmerged, and no runtime recovery, v143, certificate, rating or N/10 claim
is implied.

The same journal audit found a remote app-server wakeup failure: a completed
`concurrent.futures` writer could exist without its cross-thread callback waking
the event loop. `_await_first_strict_control_completion` now creates the
`asyncio.to_thread(complete_control_execution)` task and polls it every 50 ms
with `asyncio.wait`, rather than relying on that callback alone. Cancellation
does not detach or cancel the durable writer; it drains the writer and then
re-raises cancellation. A successful durable `COMMIT` remains the only
authoritative completion. Positive completion, lost-wakeup simulation and
cancellation-drain regressions bind this behavior; no role prompt or candidate
may manufacture journal completion.

The current unmerged strict-v1 blueprint retains manifest-schema-3
semantic-reference-only provenance for
`/home/zzx/project/pok/lll/lll/bot/国赛平台代码.py` at SHA-256
`a7aef0b3b8b1a0096164631e87f9f1dd0c57b1a95c2738762c9f6301bc434dfb`.
The production identity is now runtime 10, evaluation contract 32,
precompute schema 4 / generator `national-precompute-v3`, and runtime-probe
schema/orchestrator/worker/scenario `15/15/16/7`. TCP parsing, legality,
fallback, tracker, socket send and evidence remain system-owned; LLL bytes,
runtime, history, ratings and strength remain unavailable.

Five strength P1s are repaired with producer-to-consumer regressions:

1. The old 169-value hand-written ordering heuristic is replaced by a generated
   calibrated heads-up table. The pinned generator SHA-256 is
   `5aa6808974f9af67ac7bb5189c431791d9aed9e791869f9428b1ab8e04cf62d3`;
   a fixed seed drives 65,536 uniformly sampled official-evaluator opponent and
   board completions for each canonical class, with evaluator/deck/random/
   environment identities bound. Anchors prove `A2o > K2o > 76o`, suited over
   offsuit and ordered pairs; schema/content/generator drift fails closed.
2. The four actionable preflop spots are `sb_open`, `bb_vs_limp`,
   `bb_vs_raise` and `sb_vs_reraise`. Their tested raise-to-total bands are
   respectively `225–300`, `325–450`, `650–900` and `900–1200`. When the
   desired strong shallow-stack target reaches the exact hero total, the policy
   returns typed `allin` even though `legal.max_raise_to` excludes that total.
   The final allin-only repair also covers ultrashort contexts where `allin` is
   legal but `raise` is absent and both raise bounds are null: AA jams in all
   four spots, the weak control never jams, and postflop remains neutral.
3. Runtime 10 emits schema-1 `hand.match_control`, binding initial chips,
   blinds, current position/exposure, future forced blinds, forced-fold loss
   bound and hero net. Only the strict inequality
   `hero_net_earned > forced_fold_loss_bound` sets `fold_locks_win`; equality or
   malformed evidence is neutral. A valid lock folds even AA and suppresses
   refinement.
4. Runtime emits consistent `hero_in_position_postflop` and
   `acts_first_postflop`. Position/EQR affects only marginal flop/turn calls
   that leave a future street; river, `betting.call_closes_allin_runout=true`,
   action-text-only all-in hints and missing/inconsistent facts are neutral.
5. Opponent tilt now weights holdings against the current public board, rather
   than reusing preflop class ordering postflop. A raise-conditioned range may
   not improve hero equity; flop/turn weights never use sampled future-runout
   cards. Malformed boards are neutral and preflop keeps the calibrated table.

The dynamic freeze gates now bind the system precompute generator/content/
environment/hash contract; runtime-10 exact system bytes, manifest and first-
control identity; all five strength positive/negative regressions; capability
and runtime-probe consumer reachability; and the five-role prompt contract.
Master, Worker, Reviewer, Critic and Orchestrator each state calibrated
169-class equity, spot-specific raise-to-total/exact all-in, mathematical match
lock, nonclosing-only position realization and current-board range weighting.
Stored flags or keyword presence alone cannot pass.

The candidate runtime probe independently enforces match-control causality on
the final system wire: strict proved lead → `fold`; equality boundary and
malformed proof → non-fold. The current whole runtime-probe shard is
`18 passed`; the final baseline no longer reports the former consumer
`candidate_contract` failure.

Current source identities are policy
`f7c6a14a0b6fdceb6f47016ba9f8048d3ce82d4baa9dfa1b88c3a74e2b24f956`,
prepared artifact
`0ad1dd758ebc0b62f86f19bdc645abaeb5b7d48fee7513aa8a5c0c65a2721a17`,
output artifact
`db439a8b92e737663951814d918ab16dfabef454c5559f87fee60ca76061d327`,
first-control artifact
`b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2`,
system national bot
`0115c5844961011d920d012edbba30eb23171de0f5649f5b46e75a0e6bd94bef`
and system precompute
`8adeab7e8122465e1a76231a32fa34d1c08c30f77e70ef978bb8093920f00627`.
They are review inputs only, not a published Bot, certificate or strength
sample. Final source evidence is `2901 passed, 20 skipped, 1 warning` in 156.14
seconds; merge and runtime publication remain separate unclaimed steps.

The Codex-only Worker MCP remains outside evolution. Its raw-secret P1 now has
follow-up `c7a254ce14863926c5da31a9387288170d7fb05d` (parent exactly
`7bd7c78ce72924c4899fd5403c188c14ea98deec`) with `118 passed`; it recursively
scans raw strings before serialization. The complete series awaits ordered
main-tree review and inclusion as `4a458dc8` → `7bd7c78c` → `c7a254ce`; no
subset or installed service supplies poker runtime/evidence credit.
