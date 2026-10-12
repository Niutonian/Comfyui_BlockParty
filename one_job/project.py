"""Portable ordered job chains and fixed video assembly; no shared shell scripts."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from one_job.errors import StepError
from one_job.paths import assert_unlinked, job_relative, safe_filename, single_segment
from one_job.job import load_manifest, workflow_file, sha256_file, prepare_job, run_pending


def load_project(root):
    path = root / 'project.json'
    assert_unlinked(root, path, 'project manifest')
    if not path.is_file() or path.stat().st_size > 1024**2:
        raise StepError('bad_input', 'project.json is missing or too large')
    try: data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError: raise StepError('bad_input', 'project.json is invalid') from None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('steps'), list) or not 1 <= len(data['steps']) <= 100:
        raise StepError('bad_input', 'project needs version 1 and 1..100 ordered steps')
    known, jobs = {}, set()
    for step in data['steps']:
        if not isinstance(step, dict): raise StepError('bad_input', 'invalid project step')
        identifier = single_segment(step.get('id'), 'step ID')
        if identifier in known: raise StepError('bad_input', 'duplicate project step ID')
        kind = step.get('type')
        if kind == 'job':
            name = single_segment(step.get('job'), 'project job')
            if name in jobs: raise StepError('bad_input', 'use a distinct job folder for each step')
            jobs.add(name)
            job = root / 'jobs' / name
            assert_unlinked(root, job, 'project job')
            manifest = load_manifest(job)
            workflow_file(job, manifest)
            if step.get('count') is not None and (type(step['count']) is not int or not 1 <= step['count'] <= 999 or manifest['inputs']):
                raise StepError('bad_input', 'count is only for text-only jobs')
            if not isinstance(step.get('bindings', []), list): raise StepError('bad_input', 'bindings must be a list')
            slots = {s['name']: s for s in manifest['inputs']}
            bound = set()
            for binding in step.get('bindings', []):
                if not isinstance(binding, dict) or binding.get('from') not in known or known[binding['from']]['type'] != 'job':
                    raise StepError('bad_input', 'bindings must reference an earlier job step')
                if binding.get('slot') not in slots or binding['slot'] in bound:
                    raise StepError('bad_input', 'binding needs a distinct declared input slot')
                bound.add(binding['slot'])
                if type(binding.get('output', 0)) is not int or not 0 <= binding.get('output', 0) < 100:
                    raise StepError('bad_input', 'invalid output index')
            if step.get('prompts'):
                folder = job_relative(root, step['prompts'], 'project prompts')
                assert_unlinked(root, folder, 'project prompts')
                if not folder.is_dir(): raise StepError('bad_input', 'project prompt folder is missing')
        elif kind == 'join_video':
            if step.get('from') not in known or known[step['from']]['type'] != 'job':
                raise StepError('bad_input', 'assembly must reference an earlier job')
            safe_filename(step.get('filename', 'project.mp4'), 'project result')
            if not step.get('filename', 'project.mp4').endswith('.mp4'): raise StepError('bad_input', 'assembly output must be MP4')
            for field, default, maximum in (('seconds_per_clip', 9, 600), ('duration', 90, 36000)):
                value = step.get(field, default)
                if type(value) not in (int, float) or not 0 < value <= maximum:
                    raise StepError('bad_input', 'invalid assembly duration')
            if type(step.get('output', 0)) is not int or not 0 <= step.get('output', 0) < 100:
                raise StepError('bad_input', 'invalid assembly output index')
            if step.get('captions'):
                folder = job_relative(root, step['captions'], 'project captions')
                assert_unlinked(root, folder, 'project captions')
                if not folder.is_dir(): raise StepError('bad_input', 'caption folder is missing')
            if not isinstance(step.get('footer', ''), str) or len(step.get('footer', '')) > 300:
                raise StepError('bad_input', 'invalid assembly footer')
        else: raise StepError('bad_input', 'only job and join_video steps are supported')
        known[identifier] = step
    return data


def project_plan(root, link, isolated=False):
    from one_job.provision import plan_job
    from one_job.environments import create_environment, environment_link
    project = load_project(root)
    plans, pins, assets, model_pins = [], {}, {}, {}
    for step in project['steps']:
        if step['type'] != 'job': continue
        job = root / 'jobs' / step['job']
        selected = create_environment(job, link) if isolated else environment_link(job, link)
        manifest = load_manifest(job)
        if selected.comfy_root == link.comfy_root:
            for model in manifest['models']:
                pair = (model['folder'], model['filename'])
                if pair in model_pins and model_pins[pair] != model['sha256'].lower():
                    raise StepError('hash_mismatch', 'project stages require different models at the same path; use isolated environments')
                model_pins[pair] = model['sha256'].lower()
            for node in manifest.get('nodes', []):
                if node['name'] in pins and pins[node['name']] != node['commit']:
                    raise StepError('node_version_conflict', 'project stages require different node revisions; use isolated environments')
                pins[node['name']] = node['commit']
        plans.append({'step': step['id'], 'job': step['job'], 'link': selected.to_json(), 'plan': plan_job(job, selected)})
    for directory in (root / 'assets',):
        assert_unlinked(root, directory, 'project assets')
        if directory.is_dir():
            for path in sorted(directory.rglob('*')):
                assert_unlinked(root, path, 'project asset')
                if path.is_file(): assets[path.relative_to(root).as_posix()] = sha256_file(path)
    # Input files and asset prompts are part of the approved project, not scripts.
    for item in plans:
        folder = root / 'jobs' / item['job'] / 'input'
        assert_unlinked(root, folder, 'project input')
        if folder.exists():
            for path in sorted(folder.iterdir()):
                assert_unlinked(root, path, 'project input')
                if path.is_file(): assets[path.relative_to(root).as_posix()] = sha256_file(path)
    contract = [sha256_file(root / 'project.json'), plans, assets]
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    return {'digest': digest, 'jobs': plans, 'assets': assets,
            'blocked': [f"{item['step']}: {problem}" for item in plans for problem in item['plan']['blocked']]}


def selected_outputs(job, output=0):
    results = []
    state = job / 'state/files'
    assert_unlinked(job, state, 'project results')
    if state.is_dir():
        for record in state.glob('*.json'):
            assert_unlinked(job, record, 'project results')
            item = json.loads(record.read_text(encoding='utf-8'))
            if item.get('status') == 'done' and len(item.get('files', [])) > output:
                name = safe_filename(item['files'][output], 'stage output')
                path = job / 'output' / name
                assert_unlinked(job, path, 'stage output')
                if not path.is_file(): raise StepError('bad_input', 'completed stage output is missing')
                results.append(path)
    return sorted(set(results), key=lambda path: path.name)


def bind_inputs(root, step, steps):
    job = root / 'jobs' / step['job']
    manifest = load_manifest(job)
    slots = {s['name']: s for s in manifest['inputs']}
    folder = job / 'input'
    assert_unlinked(root, folder, 'project inputs'); folder.mkdir(exist_ok=True)
    for binding in step.get('bindings', []):
        previous = root / 'jobs' / steps[binding['from']]['job']
        paths = selected_outputs(previous, binding.get('output', 0))
        if not paths: raise StepError('bad_input', 'upstream job has no completed outputs')
        slot = slots[binding['slot']]
        for path in paths:
            if path.suffix.lower() not in slot['extensions']:
                raise StepError('bad_input', 'upstream output does not match downstream input type')
            name = path.name if len(slots) == 1 else path.stem + '_' + binding['slot'].rsplit('_', 1)[1] + path.suffix
            destination = folder / safe_filename(name)
            assert_unlinked(root, destination, 'project input')
            claimed = job / 'state/claimed' / destination.name
            assert_unlinked(root, claimed, 'claimed project input')
            if claimed.is_file() and sha256_file(claimed) == sha256_file(path):
                continue
            if destination.exists() and sha256_file(destination) != sha256_file(path):
                raise StepError('bad_input', 'downstream input differs from upstream output; use a fresh project')
            if not destination.exists(): shutil.copyfile(path, destination)
    if step.get('prompts'):
        source = job_relative(root, step['prompts'], 'project prompts')
        for path in sorted(source.glob('*.txt')):
            assert_unlinked(root, path, 'project prompt')
            destination = folder / safe_filename(path.name)
            assert_unlinked(root, destination, 'project prompt')
            if destination.exists() and sha256_file(destination) != sha256_file(path):
                raise StepError('bad_input', 'downstream prompt differs from project asset')
            if not destination.exists(): shutil.copyfile(path, destination)


def assemble_video(root, step, steps, cancel=None):
    source = root / 'jobs' / steps[step['from']]['job']
    clips = selected_outputs(source, step.get('output', 0))
    duration, seconds = step.get('duration', 90), step.get('seconds_per_clip', 9)
    if not clips or abs(len(clips) * seconds - duration) > 1e-6:
        raise StepError('bad_input', 'clip count and clip durations do not match the final duration')
    output = root / 'output'; edit = root / 'state/edit'
    for folder in (output, edit):
        assert_unlinked(root, folder, 'project assembly'); folder.mkdir(parents=True, exist_ok=True)
    # Textfile filters keep shared captions out of filter expressions.
    footer = edit / 'footer.txt'; assert_unlinked(root, footer, 'assembly footer')
    footer.write_text(step.get('footer', 'AI-generated visuals'), encoding='utf-8')
    font = 'C\\:/Windows/Fonts/segoeui.ttf' if os.name == 'nt' else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    font_filter = f"fontfile='{font}'" if os.name == 'nt' or Path(font).is_file() else "font='sans'"
    for i, clip in enumerate(clips):
        if cancel and cancel.is_set(): raise StepError('cancelled', 'project assembly cancelled')
        filters = 'scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1,fps=24'
        if step.get('captions'):
            caption = job_relative(root, step['captions'], 'project captions') / (clip.stem + '.txt')
            assert_unlinked(root, caption, 'project caption')
            if not caption.is_file(): raise StepError('bad_input', 'caption missing for ' + clip.stem)
            target = edit / 'caption.txt'; assert_unlinked(root, target, 'assembly caption')
            target.write_bytes(caption.read_bytes())
            filters += f",drawtext={font_filter}:textfile='caption.txt':fontsize=34:fontcolor=white:x=(w-text_w)/2:y=h-155:box=1:boxcolor=black@0.65:boxborderw=16"
        filters += f",drawtext={font_filter}:textfile='footer.txt':fontsize=19:fontcolor=white:x=(w-text_w)/2:y=h-38:box=1:boxcolor=black@0.65:boxborderw=6"
        destination = edit / f'clip_{i:04d}.mp4'; assert_unlinked(root, destination, 'assembly clip')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(clip.resolve()), '-t', str(seconds), '-vf', filters,
                        '-af', f'apad,atrim=duration={seconds}', '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
                        '-c:a', 'aac', '-ar', '48000', '-ac', '2', destination.name], cwd=edit, check=True)
    concat = edit / 'concat.txt'; assert_unlinked(root, concat, 'assembly list')
    concat.write_text(''.join(f"file 'clip_{i:04d}.mp4'\n" for i in range(len(clips))), encoding='utf-8')
    final = output / step.get('filename', 'project.mp4'); assert_unlinked(root, final, 'project result')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', 'concat.txt',
      '-vf', f'fps=24,tpad=stop_mode=clone:stop_duration=1,trim=duration={duration},setpts=PTS-STARTPTS',
      '-af', f'apad,atrim=duration={duration},asetpts=PTS-STARTPTS', '-t', str(duration), '-frames:v', str(round(duration * 24)),
      '-c:v', 'libx264', '-crf', '18', '-preset', 'fast', '-c:a', 'aac', '-movflags', '+faststart', str(final.resolve())], cwd=edit, check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(final), '-f', 'null', '-'], check=True)
    metadata = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,duration,nb_frames', '-of', 'json', str(final)]))
    video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    if abs(float(video['duration']) - duration) > .05 or int(video['nb_frames']) != round(duration * 24):
        raise StepError('bad_input', 'assembled film failed timing validation')
    print('Project result:', final, flush=True)
    return final


def run_project(root, link, digest, isolated=False, restart=False, tokens=None, cancel=None, prepare_only=False, retry_failed=False):
    from one_job.provision import provision_job, plan_job
    from one_job.host import HostLink
    from one_job.lifecycle import start_server, stop_server
    project = load_project(root)
    lease = root / 'state/project.lock'; assert_unlinked(root, lease, 'project lock')
    lease.parent.mkdir(parents=True, exist_ok=True)
    try: descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError: raise StepError('bad_input', 'this project is already running') from None
    os.close(descriptor)
    try:
        plan = project_plan(root, link, isolated)
        if plan['digest'] != digest: raise StepError('stale_contract', 'project changed; review its plan again')
        if plan['blocked']: raise StepError('missing_source', '; '.join(plan['blocked']))
        steps = {s['id']: s for s in project['steps']}
        items = {item['step']: item for item in plan['jobs']}
        for step_index, step in enumerate(project['steps']):
            from one_job.progress import emit
            emit('project', message='Project stage: ' + step['id'], current=step_index, total=len(project['steps']))
            if cancel and cancel.is_set(): raise StepError('cancelled', 'project cancelled between steps')
            print('Project step:', step['id'], flush=True)
            for relative, expected in plan['assets'].items():
                path = job_relative(root, relative, 'approved project asset')
                if not path.exists() and '/input/' in relative:
                    path = job_relative(root, relative.replace('/input/', '/state/claimed/', 1), 'claimed project asset')
                assert_unlinked(root, path, 'approved project asset')
                if not path.is_file() or sha256_file(path) != expected:
                    raise StepError('stale_contract', 'project asset changed during execution')
            if step['type'] == 'job':
                item = items[step['id']]; selected = HostLink.from_json(item['link'])
                job = root / 'jobs' / step['job']
                owned = selected.comfy_root != link.comfy_root
                try:
                    # Earlier stages can satisfy shared dependencies; replan but
                    # never authorize actions outside the original approved plan.
                    current = plan_job(job, selected)
                    if current['contract'] != item['plan']['contract']:
                        raise StepError('stale_contract', 'job contract changed during project execution')
                    approved = {json.dumps(a, sort_keys=True) for a in item['plan']['actions']}
                    if any(json.dumps(a, sort_keys=True) not in approved for a in current['actions']):
                        raise StepError('stale_contract', 'new dependency action appeared during project execution')
                    provision_job(job, selected, current['digest'], tokens, cancel, restart)
                    if prepare_only: continue
                    bind_inputs(root, step, steps)
                    if retry_failed and run_pending(job, selected, cancel=cancel, retry_failed=True):
                        raise StepError('unclassified', 'Some retried items failed; downstream stages were not run')
                    count = step.get('count')
                    if count is not None:
                        count = max(0, count - len(selected_outputs(job)))
                    if count != 0 and run_pending(job, selected, count=count, cancel=cancel):
                        raise StepError('unclassified', 'job stage failed; downstream stages were not run')
                    if not selected_outputs(job): raise StepError('bad_input', 'job produced no completed outputs')
                    if not owned:
                        from one_job.comfy import _request
                        queue = json.loads(_request(selected.url.rstrip('/') + '/queue')[1])
                        if not queue.get('queue_running') and not queue.get('queue_pending'):
                            _request(selected.url.rstrip('/') + '/free', json.dumps({'unload_models': True, 'free_memory': True}).encode(), {'Content-Type': 'application/json'})
                finally:
                    if owned and restart: stop_server(selected)
            elif not prepare_only:
                assemble_video(root, step, steps, cancel)
    finally:
        assert_unlinked(root, lease, 'project lock'); lease.unlink()


def export_project(root, share):
    from one_job.security import reject_embedded_secrets
    from one_job.provision import validate_sources
    from one_job.job import load_workflow
    project = load_project(root)
    name = single_segment(project.get('name', root.name), 'project name')
    assert_unlinked(share.parent, share, 'project share folder'); share.mkdir(parents=True, exist_ok=True)
    destination = share / (name + '.zip'); assert_unlinked(share, destination, 'project ZIP')
    files = [root / 'project.json']
    for step in project['steps']:
        if step['type'] != 'job': continue
        job = root / 'jobs' / step['job']; manifest = load_manifest(job)
        validate_sources(manifest['models'])
        reject_embedded_secrets(load_workflow(workflow_file(job, manifest)))
        files.extend([job / 'manifest.json', workflow_file(job, manifest)])
        allowed = {'.txt'} | {extension for slot in manifest['inputs'] for extension in slot['extensions']}
        assert_unlinked(root, job / 'input', 'project inputs')
        if (job / 'input').is_dir(): files.extend(p for p in (job / 'input').iterdir() if p.is_file() and p.suffix.lower() in allowed)
    assert_unlinked(root, root / 'assets', 'project assets')
    if (root / 'assets').is_dir():
        files.extend(p for p in (root / 'assets').rglob('*') if p.is_file() and p.suffix.lower() in {'.txt', '.png', '.jpg', '.jpeg', '.webp', '.wav', '.mp3', '.mp4'})
    descriptor, temporary = tempfile.mkstemp(dir=share, prefix='.project-', suffix='.zip'); os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in set(files):
                assert_unlinked(root, path, 'project package file')
                archive.write(path, name + '/' + path.relative_to(root).as_posix())
            for step in project['steps']:
                if step['type'] == 'job':
                    for folder in ('input', 'output'): archive.writestr(f"{name}/jobs/{step['job']}/{folder}/", '')
            archive.writestr(name + '/README.md', f'# {name}\n\nExtract into Block Party projects/. Start and link ComfyUI. Open:\n\n'
                f'```\npython -m one_job runweb projects/{name}\n```\n\nReview the project dependencies and click Start project. '
                'CLI: `python -m one_job project run projects/' + name + ' --restart`. '
                'Use --isolated for separate Python/node environments. FFmpeg and FFprobe are required for video assembly. '
                'Models, node code, credentials and run state are not bundled.\n')
        assert_unlinked(share, destination, 'project ZIP'); os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return destination


def create_video_project(root, jobs_root, image_name, video_name, duration=90, seconds=9, captions=None, footer='AI-generated visuals'):
    from one_job.export_job import clean_name
    if root.exists(): raise StepError('bad_input', 'project folder already exists')
    assert_unlinked(root.parent, root, 'new project'); root.mkdir(parents=True)
    for label, name in (('images', image_name), ('video', video_name)):
        source = jobs_root / clean_name(name); assert_unlinked(jobs_root, source, 'source job')
        manifest = load_manifest(source)
        destination = root / 'jobs' / label; destination.mkdir(parents=True)
        manifest['name'] = label
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        workflow = workflow_file(source, manifest)
        target = job_relative(destination, manifest['workflows'][0]['file'], 'project workflow'); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workflow, target)
        for folder in ('input', 'output'): (destination / folder).mkdir()
        text_target = destination / 'input' if label == 'images' else root / 'assets/video_prompts'
        text_target.mkdir(parents=True, exist_ok=True)
        # Explicit project creation opts into pending/claimed prompt examples.
        copied = set()
        for folder in (source / 'input', source / 'state/claimed'):
            assert_unlinked(source, folder, 'source prompts')
            if folder.is_dir():
                for path in sorted(folder.glob('*.txt')):
                    assert_unlinked(source, path, 'source prompt')
                    if path.name not in copied:
                        shutil.copyfile(path, text_target / safe_filename(path.name)); copied.add(path.name)
    data = {'version': 1, 'name': clean_name(root.name), 'steps': [
        {'id': 'images', 'type': 'job', 'job': 'images'},
        {'id': 'video', 'type': 'job', 'job': 'video', 'bindings': [{'from': 'images', 'output': 0, 'slot': 'image_0'}], 'prompts': 'assets/video_prompts'},
        {'id': 'assembly', 'type': 'join_video', 'from': 'video', 'output': 0, 'seconds_per_clip': seconds,
         'duration': duration, 'filename': root.name + '.mp4', 'footer': footer}]}
    if captions:
        target = root / 'assets/captions'; target.mkdir(parents=True)
        assert_unlinked(captions.parent, captions, 'caption folder')
        for path in captions.glob('*.txt'):
            assert_unlinked(captions, path, 'caption file'); shutil.copyfile(path, target / safe_filename(path.name))
        data['steps'][-1]['captions'] = 'assets/captions'
    (root / 'project.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    load_project(root)
    return root
