"""Named media references, pending input previews, and explicit file assignment."""
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from one_job.errors import StepError
from one_job.paths import assert_unlinked, safe_filename
from one_job.slots import collect_sets, extensions_for, workflow_input_names, _exact_assignment, _assign_series, _slot_index


def pending_files(root):
    folder = root / 'input'
    assert_unlinked(root, folder, 'input')
    if not folder.is_dir(): return []
    files = []
    for path in sorted(folder.iterdir(), key=lambda path: path.name.casefold()):
        assert_unlinked(root, path, 'input')
        if path.is_file() and not path.name.startswith('.'): files.append(path)
    return files


def overview(root, manifest):
    from one_job.job import read_status
    slots = manifest['inputs']
    originals = workflow_input_names(root, slots)
    files = pending_files(root)
    sets, missing = collect_sets(root, manifest, lambda name: read_status(root, name))
    ready = [item for item in sets if item.get('files') and all(path.parent == root / 'input' for path in item['files'].values())]
    assigned, _ = _exact_assignment(files, slots)
    batch = ''
    if len(slots) == 1 and ready:
        assigned = ready[0]['files']
    if not assigned:
        groups = {}
        for path in files:
            match = re.fullmatch(r'([A-Za-z0-9_-]{1,100})_(\d+)', path.stem)
            if match: groups.setdefault(match[1], []).append(path)
        for prefix, entries in groups.items():
            chosen = _assign_series(entries, slots)
            if chosen:
                batch, assigned = prefix, chosen
                break
    if not assigned:
        for slot in slots:
            match = next((path for path in files if path.name.casefold() == originals.get(slot['name'], '').casefold()
                          and path.suffix.lower() in extensions_for(slot)), None)
            if match: assigned[slot['name']] = match
        batch = '__original__' if assigned else 'inputs-' + uuid.uuid4().hex[:12]
    rows = []
    for index, slot in enumerate(slots):
        original = originals.get(slot['name'], '')
        kind = slot['name'].rsplit('_', 1)[0].capitalize()
        rows.append({**slot, 'label': f'{kind} {_slot_index(slot) + 1}', 'original': original,
                     'assigned': assigned[slot['name']].name if slot['name'] in assigned else None})
    return {'slots': rows, 'batch': batch, 'files': [path.name for path in files],
            'ready': len(ready), 'missing': missing}


def upload_name(root, manifest, slot_name, batch, filename):
    slot = next((slot for slot in manifest['inputs'] if slot['name'] == slot_name), None)
    suffix = Path(filename).suffix.lower()
    if not slot or suffix not in extensions_for(slot):
        raise StepError('bad_input', 'This file does not match that input slot')
    if batch == '__original__':
        original = workflow_input_names(root, manifest['inputs']).get(slot_name)
        if not original or Path(original).suffix.lower() != suffix:
            raise StepError('bad_input', 'Use the expected file type for this reference, or assign a new complete input set')
        return safe_filename(original, 'input filename')
    if batch and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', batch):
        raise StepError('bad_input', 'Invalid input set name')
    if len(manifest['inputs']) == 1:
        return safe_filename(filename, 'input filename')
    stem = f'{batch}_{_slot_index(slot)}' if batch else slot_name
    return safe_filename(stem + suffix, 'input filename')


def assign_files(root, manifest, batch, bindings):
    """Publish every checked destination before removing the original names."""
    folder = root / 'input'
    assert_unlinked(root, folder, 'input')
    moves, sources, targets = [], set(), set()
    for slot_name, filename in bindings.items():
        if not filename: continue
        filename = safe_filename(filename, 'input filename')
        source = folder / filename
        assert_unlinked(root, source, 'input')
        if not source.is_file(): raise StepError('bad_input', 'The selected input changed; reload before assigning it')
        target = folder / upload_name(root, manifest, slot_name, batch, filename)
        assert_unlinked(root, target, 'input')
        if source in sources or target in targets:
            raise StepError('bad_input', 'Choose a different file for each reference')
        sources.add(source); targets.add(target)
        if source == target: continue
        if target.exists(): raise StepError('bad_input', 'That reference is already filled; choose a new input set')
        moves.append((source, target))
    if moves:
        from one_job.job import read_status
        sets, _ = collect_sets(root, manifest, lambda name: read_status(root, name))
        for item in sets:
            prompt = item.get('prompt')
            if set(item.get('files', {}).values()) == sources and prompt is not None and prompt.parent == folder:
                stem = batch if batch and batch != '__original__' else manifest['inputs'][0]['name']
                destination = folder / safe_filename(stem + '.txt', 'prompt filename')
                assert_unlinked(root, destination, 'prompt input')
                if destination != prompt:
                    if destination.exists(): raise StepError('bad_input', 'The new input set already has a prompt file')
                    moves.append((prompt, destination))
    published = []
    try:
        for source, target in moves:
            os.link(source, target)
            published.append(target)
    except OSError:
        for target in published: target.unlink(missing_ok=True)
        raise
    for source, target in moves: source.unlink()


def require_inputs(root, manifest):
    if not manifest['inputs']: return
    from one_job.job import read_status
    sets, missing = collect_sets(root, manifest, lambda name: read_status(root, name))
    if not sets:
        required = ', '.join(missing or [slot['name'] for slot in manifest['inputs']])
        raise StepError('bad_input', 'No complete input set is ready. Add or assign the required references: ' + required)


def markup(root, manifest, token):
    from one_job.web import _esc
    from one_job.media import TYPES
    info = overview(root, manifest)
    slots = info['slots']
    if not slots: return '', info
    parts = ['<section class="panel" id="required-inputs"><h2>Workflow inputs</h2>',
             f'<p><strong>{len(slots)} required file(s)</strong> per run. {info["ready"]} complete input set(s) ready.</p>',
             '<p class="subtle">Each card maps to a workflow loader. Upload directly to a card, or assign files you already uploaded. Reference order follows the workflow.</p>',
             '<form method="post" action="/assign_inputs"><input type="hidden" name="token" value="' + _esc(token) + '">',
             '<input type="hidden" name="batch" value="inputs-' + uuid.uuid4().hex[:12] + '"><div class="grid input-cards">']
    for slot in slots:
        name = slot['name']; assigned = slot['assigned']
        label = slot['label'] + (' · ' + slot['original'] if slot['original'] else '')
        parts.append('<article class="card"><h3>' + _esc(label) + '</h3><p class="subtle">' + _esc(name) + ' · Node ' + _esc(slot['node_id']) + '</p>')
        if assigned:
            url = '/input_media?name=' + quote(assigned, safe='')
            kind = TYPES.get(Path(assigned).suffix.lower(), '').split('/')[0]
            if kind == 'image': parts.append('<img loading="lazy" src="' + url + '" alt="' + _esc(label) + '">')
            elif kind in {'video', 'audio'}: parts.append(f'<{kind} controls preload="metadata" src="{url}"></{kind}>')
            parts.append('<p class="success">Ready: ' + _esc(assigned) + '</p>')
        else:
            parts.append('<p class="hint">Missing reference</p><label>Upload ' + _esc(slot['label']) + '<input class="slot-upload" type="file" data-slot="' + _esc(name) + '" accept="' + _esc(','.join(slot['extensions'])) + '"></label>')
        parts.append('<label>Assign an uploaded file<select name="bind:' + _esc(name) + '"><option value="' + _esc(assigned or '') + '">Keep current assignment</option>')
        for filename in info['files']:
            if Path(filename).suffix.lower() in extensions_for(slot):
                parts.append('<option value="' + _esc(filename) + '">' + _esc(filename) + '</option>')
        parts.append('</select></label></article>')
    parts.append('</div><button class="secondary">Apply input assignments</button></form><p id="slot-upload-message" role="status"></p>')
    assigned_files = {slot['assigned'] for slot in slots}
    unassigned = [name for name in info['files'] if name not in assigned_files and not name.lower().endswith('.txt')]
    if unassigned:
        parts.append('<details><summary>Other uploaded files (' + str(len(unassigned)) + ')</summary><div class="grid">')
        for filename in unassigned[:40]:
            parts.append('<article class="card"><p>' + _esc(filename) + '</p>')
            if TYPES.get(Path(filename).suffix.lower(), '').startswith('image/'):
                parts.append('<img loading="lazy" src="/input_media?name=' + quote(filename, safe='') + '" alt="' + _esc(filename) + '">')
            parts.append('</article>')
        parts.append('</div><p>These files are outside the displayed set. Other complete named sets also run. Assign unmatched files to the cards above.</p></details>')
    parts.append('</section>')
    return ''.join(parts), info


UPLOAD_JS = r'''
const inputInfo=INPUT_INFO;
const uploadToken=UPLOAD_TOKEN;
async function sendInput(file,slot,message){
  message.textContent='Uploading '+file.name;
  let url=inputBase+'/upload?name='+encodeURIComponent(file.name);
  if(slot)url+='&slot='+encodeURIComponent(slot)+'&batch='+encodeURIComponent(inputInfo.batch);
  const r=await fetch(url,{method:'POST',headers:{'X-Block-Party-Token':uploadToken},body:file});
  const d=await r.json();if(!r.ok)throw new Error(d.message||'Upload failed');
}
let uploading=false;
async function addInputs(files,explicit){
  if(uploading)return;uploading=true;
  const message=document.getElementById(explicit?'slot-upload-message':'upload-message');
  const free=inputInfo.slots.filter(s=>!s.assigned);
  try{
    const entries=Array.from(files), planned=[], used=new Set();
    // Exact author filenames take precedence over selection order.
    for(const file of entries){
      const ext='.'+file.name.split('.').pop().toLowerCase();
      let slot=explicit?inputInfo.slots.find(s=>s.name===explicit):free.find(s=>!used.has(s.name)&&s.original.toLowerCase()===file.name.toLowerCase()&&s.extensions.includes(ext));
      if(slot)used.add(slot.name);planned.push({file,slot,ext});
    }
    for(const item of planned){
      if(!item.slot&&!explicit&&item.ext!=='.txt'){
        item.slot=free.find(s=>!used.has(s.name)&&s.extensions.includes(item.ext));
        if(item.slot)used.add(item.slot.name);
      }
      await sendInput(item.file,item.slot?item.slot.name:null,message);
    }
    location.reload();
  }catch(e){message.textContent=e.message+' Reload the page to review any files already added.';uploading=false;}
}
document.querySelectorAll('.slot-upload').forEach(input=>input.onchange=e=>addInputs(e.target.files,input.dataset.slot));
document.getElementById('input-files').onchange=e=>addInputs(e.target.files);
const drop=document.getElementById('input-drop');
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};
drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');addInputs(e.dataTransfer.files)};
'''
