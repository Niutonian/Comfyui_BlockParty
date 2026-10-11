# Security review — 9 October 2026

Status update, 10 October: see [fixes and validation](security_review/RESOLUTION.md). Findings below describe the original reviewed state.

## Assessment and scope

Reviewed all three pages of `one-job-concept.pdf`, the updated `CONCEPT.md`, the Python launcher, ComfyUI export endpoint, browser UI, installer, supplied job JSON, and existing tests. This assesses the current working tree, including existing uncommitted changes.

The implementation has useful path checks, local-only queue URLs, redirect rejection for queue/upload/result calls, parameter validation, and a repair menu that does not execute arbitrary commands. However, it does **not yet enforce the concept's security contract for jobs received from other people**. Five findings below should be addressed before treating imported jobs as constrained, verified tasks.

No application code was modified. No real workflow was queued, model downloaded, or service attacked. The offline probes use mocked network calls and disposable fixture files. This is a source review with targeted reproductions, not a penetration test of the installed ComfyUI and its custom-node dependencies.

## 1. High — Manifest omissions bypass dependency verification

**Locations:** `one_job/job.py:90`, `:147`, `:209`, `:228`; `one_job/export_job.py:86`; `__init__.py:82`.

`check_contract()` verifies that each *declared* model appears somewhere in the serialized graph. It never checks that every model or custom node actually used by the graph has a declaration. Both `models: []` and `nodes: []` pass preparation for a workflow containing model loaders and custom-node classes. The model-reference check is also a substring search, not a comparison with the loader's actual model input. `verify_nodes()` explicitly accepts a missing commit, printing `present, not pinned`.

**Prerequisite and impact:** Someone supplies a modified job which the user prepares and runs. Installed custom nodes and existing models can execute without the version/hash checks the user expects. A graph can invoke whatever capabilities those installed nodes expose, including filesystem or network operations where such nodes exist. This does not by itself prove universal remote code execution; impact depends on the installed node implementations. A hash supplied by the same author also establishes identity, not author trust or harmlessness.

**Evidence:** The offline probe successfully calls `prepare_job()` with a model loader, a custom class, and empty dependency lists. A separate probe verifies an unpinned directory successfully. Network availability alone is mocked; contract and dependency checks run normally.

**Fix:** Derive a complete dependency inventory from authoritative node/input metadata and reconcile it with the manifest. Reject undeclared dependencies, unresolved custom classes, and missing commit pins. Resolve custom-node directory links correctly during export. Check model references by exact loader/input association. Define trusted node capabilities separately from dependency integrity; unknown node types should require an explicit trust decision or isolated execution.

## 2. High — Directory links bypass job containment

**Locations:** `one_job/job.py:294`, `:313`, `:492`; `one_job/export_job.py:132`; `one_job/parameters.py:493`; `__init__.py:106`.

`_save_outputs()` checks the destination against `job/output`, but resolves both paths. If `output` itself is a junction/symlink to another directory, the check passes because both resolve there. The same trust-anchor problem affects `state/files` and `state/claimed`. Export performs no containment validation before writing `workflow.api.json` and `manifest.json`; it follows an existing linked job directory or linked destination files. Parameter and preparation writes also follow links.

**Prerequisite and impact:** The job folder contains attacker-controlled filesystem links, or a separate actor can modify its directory structure. Ordinary JSON alone cannot create these links. On supported filesystems or archive extraction paths that preserve links, processing/exporting the job can create or overwrite files outside its directory using the user's permissions. Input discovery also follows links, potentially reading files outside the intended drop folder.

**Evidence:** A Windows junction at a fixture job's `output` caused `_save_outputs()` to write into a sibling directory. A junction at `jobs_root/linked` caused `write_job()` to write its workflow and manifest into that sibling directory. All targets were harmless disposable fixtures.

**Fix:** Establish the resolved job root as the trust anchor, then validate every input, state, output, and export destination against it. Reject linked/reparse-point descendants on imported jobs, including destination files. Validate before creating directories or moving files. Where concurrent untrusted writers are in scope, use filesystem operations that avoid following links and enforce suitable directory permissions; a separate `resolve()` check alone has a race window.

## 3. Medium — Preparation remains valid after dependencies change

**Locations:** `one_job/job.py:254`, `:269`, `:547`.

After preparation, `require_prepared()` checks the workflow hash, manifest hash, and root path only. The stored model hashes are not compared with current files, and node commits are not rechecked when running. A changed or upgraded model/node installation therefore leaves an old job marked prepared. Checking Git HEAD alone would also miss modified working-tree code.

**Prerequisite and impact:** A dependency changes after preparation, whether through a model replacement, custom-node update, or tampering. The launcher runs different dependencies from those it approved and can miss the hash mismatch that the concept promises will stop execution.

**Evidence:** The probe prepares a job with a real hashed fixture model, replaces the model's bytes, and successfully calls `require_prepared()` again. Source inspection confirms `run_pending()` performs no subsequent model/node verification.

**Fix:** Revalidate dependency identities before execution or maintain a trusted cache with reliable invalidation when files, locations, node revisions, or working-tree contents change. Keep approval state outside the shared job package, or explicitly discard packaged `state/` on import. The existing `prepared.json` hashes are not an authentication mechanism because the package can contain that file too.

## 4. Medium — Unknown or wrong server identity can pass validation

**Locations:** `one_job/job.py:176`; `one_job/host.py:170`, `:292`.

`ensure_server()` accepts a server when its `/system_stats` response has a `comfyui_version` field. It attempts process identification but silently accepts `None`. It also calls `link_from_running_server()` without the linked URL's port, so identification always checks port 8188 even when the actual target is another port. Discovery relies on Windows process tools and can fail on other supported platforms. The separate `server_is_comfy()` probe uses ordinary `urlopen`, which follows redirects despite the main client's redirect guard.

**Prerequisite and impact:** A different server occupies the target port, or host identification is unavailable. The launcher may send private images, prompts, and workflow data to a process other than the linked installation while verifying dependencies in the saved installation. A local malicious process is one possible cause; multiple legitimate ComfyUI installations are another.

**Evidence:** With a successful stats response and failed identity discovery, `ensure_server()` accepts a fixture link on port 9191; its discovery call receives no port argument. These network/process results are mocked, not a claim that the current live server is spoofed.

**Fix:** Identify the listener at the exact configured host/port, provide platform-appropriate discovery, and fail closed or explicitly re-establish trust when identity cannot be verified. Reuse the redirect-refusing HTTP client for probes. For stronger identity assurance, use an authenticated handshake with the intended server.

## 5. Medium — Uploads overwrite shared ComfyUI input files

**Location:** `one_job/comfy.py:45`.

Every upload sends the original basename and `overwrite=true`, with no job-specific subfolder. A dropped `portrait.png` can replace an unrelated `portrait.png` already in ComfyUI's input directory. Concurrent jobs using the same basename can also replace each other's input between upload and execution.

**Prerequisite and impact:** Two jobs or an existing ComfyUI workflow use the same filename. An actor able to supply dropped files can cause data loss or substitution of another workflow's input. The launcher need not share a job directory with that other workflow.

**Evidence:** The offline probe captures the actual multipart body and confirms the original filename and forced overwrite. No real file was uploaded.

**Fix:** Upload into a unique job/run namespace and disable overwrite, then use the server-returned filename. Retain uploaded files until queued work has completed; clean up only files owned by that run.

## Additional concerns and deployment-dependent risks

- **Export endpoint trust:** `__init__.py:129` checks the immediate peer address only. A local reverse proxy makes remote requests appear local, and the route has no independent authorization or origin check. Export can overwrite an existing job and automatically refresh its prepared record. Browser CSRF exposure depends on the host: current upstream ComfyUI installs origin-checking middleware by default, but replaces it when CORS is enabled. Therefore this review does **not** claim default ComfyUI is universally vulnerable to a cross-site export attack. Verify the deployed version/proxy configuration and enforce explicit endpoint authorization and origin policy. [Upstream ComfyUI middleware](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py).
- **Blocking export work:** `export_job()` performs model hashing and repeated synchronous Git subprocess calls directly inside the async request handler. Large models or a deliberately large graph can block ComfyUI's HTTP event loop. Offload work, deduplicate pack resolution, and enforce graph/work limits.
- **Unbounded media buffering:** Uploads and responses are read completely into memory (`one_job/comfy.py:25`, `:45`). One large dropped video can exhaust launcher memory; processing one file at a time does not bound its size. Stream with byte limits. `_read_prompt_file()` also checks its 1 MB limit after reading the full file.
- **Secret-bearing workflows:** Export writes the supplied graph verbatim and treats most primitive inputs as potential parameters. A custom node with an embedded token/password can therefore put credentials into a shareable workflow/manifest. No actual embedded credential was identified in the supplied jobs. Use node-aware secret metadata and local-only secret references.

## Validation

- Existing suite: **47 tests passed**, using `python -m unittest discover -s tests` semantics. Sandbox execution initially produced temporary-directory permission errors; the same suite passed outside that restriction with fixture storage inside the workspace.
- Offline probes: **7 confirmations across the five findings**, in `security_review/reproduce.py`. Run from the repository root with `python security_review/reproduce.py`. On Windows it uses disposable directory junctions; on other platforms, symlinks.
- No live exploit, browser-origin attack, GPU execution, dependency CVE audit, or third-party custom-node code audit was performed. The upstream middleware check is contextual and does not establish the version/configuration of the user's running server.

Recommended order: fix complete dependency enforcement and filesystem containment first, then dependency invalidation, server identity, and upload isolation. Until then, restrict use to personally reviewed jobs and trusted folder contents.
