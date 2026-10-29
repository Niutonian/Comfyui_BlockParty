(() => {
    const root = document.currentScript.previousElementSibling;
    const canvas = root.querySelector('canvas'), context = canvas.getContext('2d');
    const data = root.querySelector('textarea'), controls = root.querySelector('.bp-box-controls');
    const status = root.querySelector('.bp-box-status'), form = root.closest('form');
    let boxes = JSON.parse(data.value), selected = boxes.length ? 0 : -1, gesture = null;
    const dimension = key => {
        const field = form?.elements.namedItem('v_' + root.dataset[key + 'Field']);
        return Math.max(1, Math.min(16384, Number(field?.value || root.dataset[key]) || 1024));
    };
    const source = JSON.parse(root.dataset.source || '{}');
    const size = () => {
        if (source.node_id) {
            const value = key => form?.elements.namedItem('v_' + source.fields[key])?.value || source[key];
            const ratio = /^(\d+):(\d+)/.exec(String(value('aspect_ratio')));
            const mp = Number(value('megapixels')), multiple = Number(value('multiple'));
            if (ratio && Number(ratio[1]) > 0 && Number(ratio[2]) > 0 && mp > 0 && mp <= 16 && multiple >= 1 && multiple <= 128) {
                const scale = Math.sqrt(mp * 1024 * 1024 / (Number(ratio[1]) * Number(ratio[2])));
                return [Number(ratio[1]), Number(ratio[2])].map(r => Math.max(1, Math.min(16384, Math.round(r * scale / multiple) * multiple)));
            }
        }
        return [dimension('width'), dimension('height')];
    };
    function draw() {
        const [width, height] = size(), scale = Math.min(720 / width, 480 / height, 1);
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        canvas.style.maxWidth = canvas.width + 'px';
        context.clearRect(0, 0, canvas.width, canvas.height);
        const sx = canvas.width / width, sy = canvas.height / height;
        boxes.forEach((box, index) => {
            const x = box.x * sx, y = box.y * sy, w = box.width * sx, h = box.height * sy;
            context.strokeStyle = index === selected ? '#ffb75e' : '#58d4d5';
            context.fillStyle = index === selected ? '#ffb75e24' : '#58d4d518';
            context.lineWidth = index === selected ? 3 : 1;
            context.fillRect(x, y, w, h); context.strokeRect(x, y, w, h);
            context.fillStyle = '#fff'; context.font = '12px sans-serif';
            context.fillText(String(index + 1) + ': ' + (box.metadata?.desc || box.metadata?.text || 'box').slice(0, 50), x + 5, y + 16, Math.max(30, Math.abs(w) - 10));
        });
    }
    function store() {
        data.value = JSON.stringify(boxes, null, 2);
        data.setCustomValidity(''); status.textContent = '';
        draw();
    }
    function button(text, action) {
        const element = document.createElement('button'); element.type = 'button';
        element.textContent = text; element.onclick = action; controls.append(element);
    }
    function editor() {
        controls.replaceChildren();
        const select = document.createElement('select'); select.setAttribute('aria-label', 'Selected bounding box');
        boxes.forEach((box, index) => select.add(new Option(`${index + 1}: ${box.metadata?.desc || box.metadata?.text || 'box'}`, String(index))));
        select.value = String(selected); select.onchange = () => { selected = Number(select.value); editor(); draw(); };
        controls.append(select);
        button('Add box', () => {
            if (boxes.length >= 256) { status.textContent = 'Keep at most 256 boxes.'; return; }
            const [width, height] = size();
            boxes.push({x: Math.round(width * .1), y: Math.round(height * .1), width: Math.round(width * .3), height: Math.round(height * .3), metadata: {type: 'obj', desc: '', text: '', palette: []}});
            selected = boxes.length - 1; store(); editor();
        });
        if (selected < 0 || !boxes[selected]) return;
        button('Delete selected box', () => { boxes.splice(selected, 1); selected = Math.min(selected, boxes.length - 1); store(); editor(); });
        const box = boxes[selected];
        function field(title, value, type, change) {
            const label = document.createElement('label'); label.textContent = title;
            const input = document.createElement(type === 'multiline' ? 'textarea' : 'input');
            if (type !== 'multiline') input.type = type;
            else input.rows = 3;
            input.value = value;
            if (type === 'number') input.step = 'any';
            input.onchange = () => {
                if (type === 'number' && (!input.value.trim() || !Number.isFinite(Number(input.value)))) return;
                change(type === 'number' ? Number(input.value) : input.value); store();
            };
            label.append(input); controls.append(label);
        }
        for (const key of ['x', 'y', 'width', 'height']) field(key + ' (pixels)', box[key], 'number', value => {box[key] = value;});
        const meta = () => (box.metadata ||= {});
        const typeLabel = document.createElement('label'); typeLabel.textContent = 'Box type';
        const type = document.createElement('select'); type.add(new Option('Object', 'obj')); type.add(new Option('Text', 'text'));
        type.value = box.metadata?.type === 'text' ? 'text' : 'obj';
        type.onchange = () => {meta().type = type.value; store();};
        typeLabel.append(type); controls.append(typeLabel);
        field('Description', box.metadata?.desc || '', 'multiline', value => {meta().desc = value;});
        field('Text to render (text boxes)', box.metadata?.text || '', 'multiline', value => {meta().text = value;});
        field('Palette (comma-separated colors)', (box.metadata?.palette || []).join(', '), 'text', value => {meta().palette = value.split(',').map(s => s.trim()).filter(Boolean);});
    }
    const position = event => {
        const rect = canvas.getBoundingClientRect(), [width, height] = size();
        return [Math.round(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * width), Math.round(Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) * height)];
    };
    canvas.onpointerdown = event => {
        if (event.button !== 0) return;
        const [x, y] = position(event);
        selected = event.altKey ? -1 : boxes.findLastIndex(box => x >= Math.min(box.x, box.x + box.width) && x <= Math.max(box.x, box.x + box.width) && y >= Math.min(box.y, box.y + box.height) && y <= Math.max(box.y, box.y + box.height));
        const fresh = selected < 0;
        if (fresh) {
            if (boxes.length >= 256) return;
            selected = boxes.length;
            boxes.push({x, y, width: 0, height: 0, metadata: {type: 'obj', desc: '', text: '', palette: []}});
        }
        gesture = {x, y, fresh, original: {...boxes[selected]}};
        canvas.setPointerCapture(event.pointerId); editor(); draw();
    };
    canvas.onpointermove = event => {
        if (!gesture) return;
        const [x, y] = position(event), box = boxes[selected];
        if (gesture.fresh) {
            box.x = Math.min(gesture.x, x); box.y = Math.min(gesture.y, y);
            box.width = Math.abs(x - gesture.x); box.height = Math.abs(y - gesture.y);
        } else {
            box.x = gesture.original.x + x - gesture.x; box.y = gesture.original.y + y - gesture.y;
        }
        store();
    };
    function end() {
        if (!gesture) return;
        if (gesture.fresh && (!boxes[selected].width || !boxes[selected].height)) { boxes.splice(selected, 1); selected = boxes.length - 1; }
        gesture = null; store(); editor();
    }
    canvas.onpointerup = end; canvas.onpointercancel = end;
    data.oninput = () => {
        try {
            const parsed = JSON.parse(data.value);
            if (!Array.isArray(parsed) || parsed.length > 256 || parsed.some(box => !box || typeof box !== 'object' || ['x', 'y', 'width', 'height'].some(k => !Number.isFinite(box[k])) || (box.metadata != null && (typeof box.metadata !== 'object' || Array.isArray(box.metadata) || ['type', 'desc', 'text'].some(k => k in box.metadata && typeof box.metadata[k] !== 'string') || ('palette' in box.metadata && (!Array.isArray(box.metadata.palette) || box.metadata.palette.some(c => typeof c !== 'string'))))))) throw new Error('Invalid box data');
            boxes = parsed; selected = Math.min(Math.max(selected, 0), boxes.length - 1);
            data.setCustomValidity(''); status.textContent = ''; editor(); draw();
        } catch (error) { status.textContent = 'Invalid box JSON. Each box needs numeric x, y, width, height and optional metadata.'; data.setCustomValidity('Correct the bounding box JSON before saving.'); }
    };
    const sizeFields = [root.dataset.widthField, root.dataset.heightField, ...Object.values(source.fields || {})].map(name => 'v_' + name);
    form?.addEventListener('input', event => { if (sizeFields.includes(event.target.name)) draw(); });
    document.addEventListener('DOMContentLoaded', draw, {once: true});
    editor(); draw();
})();
