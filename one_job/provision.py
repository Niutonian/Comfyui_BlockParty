"""Reviewed, hash-verified dependency provisioning for a linked ComfyUI."""
from __future__ import annotations
import errno
import hashlib
import ipaddress
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from one_job.errors import StepError
from one_job.host import config_dir, find_model_file, model_dirs_for
from one_job.paths import assert_unlinked, commit_text, relative_parts, single_segment

PROVIDERS = {'huggingface': ('huggingface.co', 'HF_TOKEN'), 'civitai': ('civitai.com', 'CIVITAI_API_KEY')}
MAX_MODEL = 200 * 1024**3


def source_url(value, *, network=False, redirect=False):
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise StepError('bad_input', 'download source must be a public HTTPS URL')
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.fragment or parts.port not in (None, 443)):
            raise ValueError()
        if not redirect:
            keys = {k.lower().replace('_', '') for k, _ in urllib.parse.parse_qsl(parts.query)}
            if keys & {'token', 'apikey', 'key', 'authorization', 'access_token', 'accesstoken', 'signature', 'x-amz-signature'}:
                raise StepError('bad_input', 'keep download credentials out of URLs; use local credentials')
        host = parts.hostname
        if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
            raise ValueError()
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = []
        if network:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
        if any(not address.is_global for address in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise StepError('bad_input', 'download source must resolve to a public HTTPS host') from None
    return value


def validate_sources(models):
    if not isinstance(models, list):
        raise StepError('bad_input', 'model sources must be a list')
    result = {}
    for model in models:
        if not isinstance(model, dict):
            raise StepError('bad_input', 'invalid model source')
        folder, filename = model.get('folder'), model.get('filename')
        single_segment(folder, 'model folder')
        relative_parts(filename, 'model file')
        url = model.get('url')
        auth = model.get('auth') or None
        if auth not in (None, *PROVIDERS):
            raise StepError('bad_input', 'model authentication must be huggingface or civitai')
        if url:
            source_url(url)
            if auth and urllib.parse.urlsplit(url).hostname != PROVIDERS[auth][0]:
                raise StepError('bad_input', 'credential provider does not match the download host')
        size = model.get('size_bytes')
        if size is not None and (type(size) is not int or size < 1 or size > MAX_MODEL):
            raise StepError('bad_input', 'invalid model download size')
        pair = (folder, filename)
        if pair in result:
            raise StepError('bad_input', 'duplicate model source')
        result[pair] = {'url': url or None, 'auth': auth, 'size_bytes': size}
    return result


def repository_url(value):
    source_url(value)
    parts = urllib.parse.urlsplit(value)
    if parts.hostname not in {'github.com', 'gitlab.com', 'codeberg.org'} or parts.query:
        raise StepError('bad_input', 'node repositories must use credential-free HTTPS on GitHub, GitLab or Codeberg')
    return value


def credentials(one_shot=None):
    """Only machine-local dotenv; never load a received job's .env."""
    values = {}
    path = config_dir() / 'downloads.env'
    assert_unlinked(config_dir(), path, 'download credentials')
    if path.is_file():
        if path.stat().st_size > 16384:
            raise StepError('bad_input', 'local downloads.env is too large')
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'): continue
            key, sep, value = line.partition('=')
            if sep: values[key.strip()] = value.strip().strip('"\'')
    supplied = one_shot or {}
    result = {}
    for provider, (_, variable) in PROVIDERS.items():
        value = supplied.get(provider) or os.environ.get(variable) or values.get(variable) or ''
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise StepError('bad_input', 'invalid download credential')
        if value: result[provider] = value
    return result


def plan_job(job_dir, link):
    from one_job.progress import emit
    emit('checking', message='Checking workflow contracts and installed dependencies')
    from one_job.job import load_manifest, workflow_file, load_workflow, check_contract, _verified_hash, _git_head, sha256_file
    manifest = load_manifest(job_dir)
    path = workflow_file(job_dir, manifest)
    check_contract(load_workflow(path), manifest)
    validate_sources(manifest['models'])
    actions, blocked = [], []
    for model in manifest['models']:
        folder, filename = model['folder'], model['filename']
        expected = model.get('sha256', '')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected):
            raise StepError('hash_mismatch', f'{filename} needs a full SHA256')
        existing = find_model_file(Path(link.comfy_root), folder, filename)
        if existing:
            if _verified_hash(existing) != expected.lower():
                raise StepError('hash_mismatch', f'{filename} differs from the shared model; it will not be replaced')
            continue
        if not model.get('url'):
            blocked.append(f'{folder}/{filename}: author must supply a download URL')
            continue
        destination = model_dirs_for(Path(link.comfy_root), folder)[0] / filename
        assert_unlinked(Path(link.comfy_root), destination, 'model download')
        actions.append({'kind': 'model', **model, 'destination': str(destination)})
    names = set()
    for node in manifest.get('nodes') or []:
        name = single_segment(node.get('name'), 'node pack')
        if name in names: raise StepError('bad_input', 'duplicate node pack')
        names.add(name)
        commit_text(node.get('commit'), name)
        if len(node['commit']) not in (40, 64):
            raise StepError('bad_input', f'{name} requires a full Git commit hash')
        pack = Path(link.custom_nodes) / name
        if pack.exists():
            if not pack.is_dir() or _git_head(pack) != node['commit']:
                raise StepError('node_version_conflict', f'{name} has another revision; existing nodes will not be changed')
            continue
        assert_unlinked(Path(link.custom_nodes), pack, 'node install')
        url = node.get('git_url')
        if not url:
            blocked.append(f'{name}: author must supply a Git repository URL')
            continue
        repository_url(url)
        actions.append({'kind': 'node', **node, 'destination': str(pack)})
    # Bind confirmation to the actual contract and host, not the submitted action list.
    identity = [sha256_file(job_dir / 'manifest.json'), sha256_file(path), str(Path(link.comfy_root).resolve()), link.python]
    digest = hashlib.sha256(json.dumps([identity, actions, blocked], sort_keys=True).encode()).hexdigest()
    return {'digest': digest, 'contract': identity[:2], 'actions': actions, 'blocked': blocked}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Connect to the addresses we validate, rather than resolving a second time.
        answers = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
            raise StepError('bad_input', 'download host resolved to a private address')
        last_error = None
        for family, kind, protocol, _, address in answers:
            sock = socket.socket(family, kind, protocol)
            sock.settimeout(self.timeout)
            try:
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as error:
                sock.close()
                last_error = error
        raise last_error or OSError('connection failed')


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context, check_hostname=self._check_hostname)


def open_download(url, tokens, offset=0, etag=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), PublicHTTPSHandler())
    for _ in range(8):
        source_url(url, network=True, redirect=True)
        host = urllib.parse.urlsplit(url).hostname
        headers = {'User-Agent': 'BlockParty/1', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
            if etag: headers['If-Range'] = etag
        for provider, (domain, _) in PROVIDERS.items():
            if host == domain and tokens.get(provider):
                headers['Authorization'] = 'Bearer ' + tokens[provider]
        try:
            return opener.open(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                location = error.headers.get('Location')
                error.close()
                if not location: break
                url = urllib.parse.urljoin(url, location)
                continue
            code = 'auth_required' if error.code in (401, 403) else 'download_failed'
            status = error.code
            error.close()
            raise StepError(code, f'download returned HTTP {status}; check access and local credentials') from None
        except (OSError, urllib.error.URLError):
            raise StepError('download_failed', 'download connection failed; retry preparation') from None
    raise StepError('download_failed', 'download redirected too many times')


def download_model(action, tokens, cancel=None):
    from one_job.progress import emit
    destination = Path(action['destination'])
    anchor = destination.parent
    # Caller has already checked ancestors against the linked ComfyUI root.
    anchor.mkdir(parents=True, exist_ok=True)
    key = action['sha256'].lower()
    temporary = anchor / ('.block-party-' + key + '.partial')
    metadata = temporary.with_suffix('.json')
    lease = temporary.with_suffix('.lock')
    for path in (temporary, metadata, lease): assert_unlinked(anchor, path, 'partial download')
    try: descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StepError('bad_input', 'another download owns this model; wait before retrying') from None
    os.close(descriptor)
    try:
        for attempt in range(3):
            try:
                if cancel and cancel.is_set(): raise StepError('cancelled', 'download cancelled; partial file retained')
                identity = hashlib.sha256(action['url'].encode()).hexdigest()
                saved = {}
                if metadata.exists():
                    try: saved = json.loads(metadata.read_text(encoding='utf-8'))
                    except (ValueError, OSError): pass
                offset = temporary.stat().st_size if temporary.exists() and saved.get('source') == identity else 0
                if offset > MAX_MODEL: raise StepError('bad_input', 'partial model exceeds the supported size')
                remaining = max(0, (action.get('size_bytes') or 0) - offset)
                if shutil.disk_usage(anchor).free < remaining + 16 * 1024**2:
                    raise StepError('disk_full', 'not enough space for the remaining model download')
                digest, total, last = hashlib.sha256(), offset, time.monotonic()
                started, started_bytes = last, offset
                emit('downloading', message='Downloading ' + action['filename'], bytes_done=offset, bytes_total=action.get('size_bytes'), current=offset, total=action.get('size_bytes') or 0)
                if offset:
                    print(f"resuming {action['filename']} at {offset // 1024**2} MiB", flush=True)
                    with temporary.open('rb') as prefix:
                        while chunk := prefix.read(1024 * 1024):
                            if cancel and cancel.is_set(): raise StepError('cancelled', 'download cancelled; partial file retained')
                            digest.update(chunk)
                if offset and action.get('size_bytes') == offset and digest.hexdigest() == key:
                    assert_unlinked(anchor, destination, 'model download')
                    if destination.exists():
                        raise StepError('stale_contract', 'model destination appeared; prepare again')
                    os.link(temporary, destination)
                    temporary.unlink(); metadata.unlink(missing_ok=True)
                    return
                with open_download(action['url'], tokens, offset, saved.get('etag')) as response:
                    status = getattr(response, 'status', 200)
                    if offset and status == 200:
                        offset = total = 0
                        started, started_bytes = time.monotonic(), 0
                        digest = hashlib.sha256()
                    elif status == 206:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[3]) > MAX_MODEL:
                            raise StepError('download_failed', 'server returned an invalid resume range')
                        if action.get('size_bytes') and int(match[3]) != action['size_bytes']:
                            raise StepError('download_failed', 'resume size differs from the manifest')
                    elif status != 200:
                        raise StepError('download_failed', 'server did not return a model file')
                    announced = response.headers.get('Content-Length')
                    if announced and (not announced.isdigit() or int(announced) > MAX_MODEL):
                        raise StepError('bad_input', 'model download exceeds the supported size')
                    metadata.write_text(json.dumps({'source': identity, 'etag': response.headers.get('ETag')}), encoding='utf-8')
                    received = 0
                    with temporary.open('ab' if offset else 'wb') as output:
                        while chunk := response.read(1024 * 1024):
                            if cancel and cancel.is_set(): raise StepError('cancelled', 'download cancelled; partial file retained')
                            total += len(chunk)
                            received += len(chunk)
                            if total > MAX_MODEL or (action.get('size_bytes') and total > action['size_bytes']):
                                raise StepError('bad_input', 'model download exceeds its expected size')
                            if shutil.disk_usage(anchor).free < len(chunk) + 16 * 1024**2:
                                raise StepError('disk_full', 'not enough space for the model download')
                            output.write(chunk)
                            digest.update(chunk)
                            if time.monotonic() - last > 1:
                                speed = (total - started_bytes) / max(.01, time.monotonic() - started)
                                expected = action.get('size_bytes') or (int(announced) + offset if announced else 0)
                                emit('downloading', message='Downloading ' + action['filename'], bytes_done=total, bytes_total=expected,
                                     bytes_per_second=speed, eta_seconds=max(0, expected-total)/speed if expected and speed else None,
                                     current=total, total=expected)
                                print(f"downloading {action['filename']}: {total // 1024**2} MiB", flush=True)
                                last = time.monotonic()
                        output.flush()
                        os.fsync(output.fileno())
                    if announced and received != int(announced):
                        raise StepError('download_failed', 'incomplete model download; retry preparation')
                    if action.get('size_bytes') and total != action['size_bytes']:
                        raise StepError('download_failed', 'download size differs from the manifest')
                    if digest.hexdigest() != action['sha256'].lower():
                        raise StepError('hash_mismatch', 'download SHA256 differs from the manifest; file discarded')
                assert_unlinked(anchor, destination, 'model download')
                # A hard link publishes atomically without overwriting a concurrent file.
                try: os.link(temporary, destination)
                except FileExistsError:
                    raise StepError('stale_contract', 'model destination appeared during download; prepare again') from None
                print(f"downloaded and verified {action['filename']}", flush=True)
                temporary.unlink(); metadata.unlink(missing_ok=True)
                return
            except StepError as error:
                if error.code in {'hash_mismatch', 'bad_input'}:
                    temporary.unlink(missing_ok=True); metadata.unlink(missing_ok=True)
                if error.code != 'download_failed' or attempt == 2: raise
            except (http.client.IncompleteRead, TimeoutError, urllib.error.URLError, ValueError):
                if attempt == 2: raise StepError('download_failed', 'download was interrupted; partial file retained') from None
            if cancel:
                if cancel.wait(2 ** attempt): raise StepError('cancelled', 'download cancelled; partial file retained')
            else: time.sleep(2 ** attempt)
    finally:
        lease.unlink()


def run_install(command, cwd, label):
    env = dict(os.environ)
    for variable in ('HF_TOKEN', 'CIVITAI_API_KEY', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
        env.pop(variable, None)
    env['GIT_TERMINAL_PROMPT'] = '0'
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=1800, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise StepError('install_failed', f'{label} could not finish; check Git, Python and network access') from None
    # Third-party installer output can contain URLs and credentials. Never forward it.
    if result.returncode:
        raise StepError('install_failed', f'{label} failed (exit {result.returncode}); no node pack was published')


def install_node(action, link):
    from one_job.progress import emit
    emit('installing', message=f"Installing {action['name']} at {action['commit'][:12]}")
    from one_job.job import _git_head
    repository_url(action['git_url'])
    source_url(action['git_url'], network=True)
    stage_root = Path(link.comfy_root) / '.block-party-staging'
    assert_unlinked(Path(link.comfy_root), stage_root, 'node staging')
    stage_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=stage_root, prefix='pack-') as temporary:
        pack = Path(temporary) / 'repo'
        print(f"installing {action['name']} at {action['commit'][:12]}", flush=True)
        git = ['git', '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
               '-c', 'core.hooksPath=/dev/null', '-c', 'http.followRedirects=false', '-c', 'credential.helper=']
        run_install(git + ['clone', '--no-checkout', '--', action['git_url'], str(pack)], stage_root, 'node clone')
        run_install(git + ['-C', str(pack), 'checkout', '--detach', action['commit']], stage_root, 'pinned checkout')
        if _git_head(pack) != action['commit']:
            raise StepError('node_version_conflict', 'checkout does not match the pinned commit')
        for directory, subdirs, filenames in os.walk(pack):
            subdirs[:] = [p for p in subdirs if p != '.git']
            for name in subdirs + filenames:
                assert_unlinked(pack, Path(directory) / name, 'node repository file')
        requirements = pack / 'requirements.txt'
        if requirements.is_file():
            emit('installing', message='Installing Python requirements for ' + action['name'])
            print(f"installing Python requirements for {action['name']}", flush=True)
            run_install([link.python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r', str(requirements)], pack, 'Python requirements')
        elif (pack / 'pyproject.toml').is_file() or (pack / 'setup.py').is_file():
            run_install([link.python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', str(pack)], pack, 'Python package')
        destination = Path(action['destination'])
        assert_unlinked(Path(link.custom_nodes), destination, 'node install')
        if destination.exists():
            raise StepError('stale_contract', 'node destination appeared during installation')
        destination.parent.mkdir(parents=True, exist_ok=True)
        pack.rename(destination)


def provision_job(job_dir, link, confirmed_digest, one_shot=None, cancel=None, restart=False):
    """Replan under an exclusive host lock, then execute the confirmed fixed actions."""
    from one_job.job import prepare_job, server_is_comfy, ensure_server, sha256_file, load_manifest, workflow_file
    root = Path(link.comfy_root)
    lock = root / '.block-party-provision.lock'
    assert_unlinked(root, lock, 'dependency lock')
    try: descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StepError('bad_input', 'another dependency preparation is active; wait for it to finish') from None
    try:
        os.close(descriptor)
        plan = plan_job(job_dir, link)
        if plan['digest'] != confirmed_digest:
            raise StepError('stale_contract', 'dependency plan changed; review it again')
        if plan['blocked']:
            raise StepError('missing_source', '; '.join(plan['blocked']))
        tokens = credentials(one_shot)
        workflow_path = workflow_file(job_dir, load_manifest(job_dir))
        contract_hashes = tuple(plan['contract'])
        required_auth = {a.get('auth') for a in plan['actions'] if a.get('auth')}
        for provider in required_auth:
            if not tokens.get(provider):
                raise StepError('auth_required', f'provide a local {provider} token')
        if server_is_comfy(link.url):
            ensure_server(link)
            from one_job.comfy import _request
            queue = json.loads(_request(link.url.rstrip('/') + '/queue')[1])
            if plan['actions'] and (queue.get('queue_running') or queue.get('queue_pending')):
                raise StepError('bad_input', 'ComfyUI queue must be idle before dependency changes')
        nodes = False
        restart_record = root / '.block-party-restart.json'
        assert_unlinked(root, restart_record, 'node restart record')
        for action in plan['actions']:
            if cancel and cancel.is_set(): raise StepError('cancelled', 'preparation cancelled')
            if action['kind'] == 'model':
                if action.get('auth') and not tokens.get(action['auth']):
                    raise StepError('auth_required', f"provide a local {action['auth']} token")
                assert_unlinked(root, Path(action['destination']), 'model download')
                if cancel is None: download_model(action, tokens)
                else: download_model(action, tokens, cancel)
            else:
                install_node(action, link)
                nodes = True
                from one_job.job import load_workflow
                required = sorted({node['class_type'] for node in load_workflow(workflow_path).values()})
                if restart_record.exists():
                    try: required = sorted(set(required) | set(json.loads(restart_record.read_text(encoding='utf-8'))))
                    except (ValueError, TypeError):
                        raise StepError('restart_required', 'node restart record is invalid; restart ComfyUI and review preparation') from None
                assert_unlinked(root, restart_record, 'node restart record')
                restart_record.write_text(json.dumps(required), encoding='utf-8')
        if contract_hashes != (sha256_file(job_dir / 'manifest.json'), sha256_file(workflow_path)):
            raise StepError('stale_contract', 'job changed during preparation; review the dependency plan again')
        if nodes:
            if restart:
                from one_job.lifecycle import restart_server
                restart_server(link, cancel=cancel)
            else:
                raise StepError('restart_required', 'dependencies installed; restart the linked ComfyUI, then prepare again')
        if restart_record.exists():
            if restart and not nodes:
                from one_job.lifecycle import restart_server
                restart_server(link, cancel=cancel)
            ensure_server(link)
            from one_job.comfy import _request
            try:
                required = json.loads(restart_record.read_text(encoding='utf-8'))
                registered = json.loads(_request(link.url.rstrip('/') + '/object_info')[1])
                if not set(required).issubset(registered):
                    raise StepError('restart_required', 'new nodes are not loaded; restart ComfyUI and check its startup errors')
            except (ValueError, TypeError):
                raise StepError('restart_required', 'cannot confirm new nodes loaded; restart ComfyUI and review preparation') from None
            assert_unlinked(root, restart_record, 'node restart record')
            restart_record.unlink()
        if plan['actions']:
            print('Dependency changes complete. Verifying the local workflow contract.', flush=True)
        if restart and not server_is_comfy(link.url):
            from one_job.lifecycle import start_server
            start_server(link, cancel=cancel)
        from one_job.progress import emit
        emit('verifying', message='Verifying the installed workflow and model hashes')
        return prepare_job(job_dir, link)
    except OSError as error:
        code = 'disk_full' if error.errno == errno.ENOSPC else 'install_failed'
        raise StepError(code, 'dependency write failed; check permissions and free space') from None
    finally:
        assert_unlinked(root, lock, 'dependency lock')
        lock.unlink()


def init_credentials():
    path = config_dir() / 'downloads.env'
    assert_unlinked(config_dir(), path, 'download credentials')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write('# Local download credentials. Never copy this file into a shared job.\nHF_TOKEN=\nCIVITAI_API_KEY=\n')
    return path


def format_plan(plan):
    lines = []
    known_bytes = 0
    unknown_sizes = 0
    for action in plan['actions']:
        if action['kind'] == 'model':
            known_bytes += action.get('size_bytes') or 0
            unknown_sizes += not bool(action.get('size_bytes'))
            size = f"{action['size_bytes'] / 1024**3:.2f} GiB" if action.get('size_bytes') else 'size unknown'
            lines.append(f"Download {action['folder']}/{action['filename']} ({size}) from {action['url']}")
        else:
            lines.append(f"Install {action['name']} at {action['commit']} from {action['git_url']} (including Python requirements)")
    if known_bytes or unknown_sizes:
        lines.append(f'Model disk estimate: {known_bytes / 1024**3:.2f} GiB' + (f' plus {unknown_sizes} files of unknown size' if unknown_sizes else '') + '. Partial files are reused; additional space is needed for node/Python installations.')
    return '\n'.join(lines + plan['blocked']) or 'All declared dependencies are already present.'
