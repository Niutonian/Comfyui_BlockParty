const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

const MEDIA_KEY = {
	LoadImage: "image",
	LoadImageMask: "image",
	LoadVideo: "file",
	VHS_LoadVideo: "video",
	VHS_LoadVideoPath: "video",
};
const SAVERS = new Set(["SaveImage", "SaveVideo", "VHS_VideoCombine"]);
const MODEL_EXT = [".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft"];
const EXPOSE = new Set(["text", "prompt", "negative", "width", "height", "seed", "noise_seed"]);
const SEED_KEYS = new Set(["seed", "noise_seed"]);
const JOB_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const PARAM_NAME = /^[a-z][a-z0-9_]{0,40}$/;

function notify(severity, detail) {
	const toast = app.extensionManager && app.extensionManager.toast;
	if (toast) {
		toast.add({ severity, summary: "Block Party", detail, life: 8000 });
		return;
	}
	window.alert(detail);
}

function nodeRank(id) {
	const text = String(id);
	return /^\d+$/.test(text) ? [0, Number(text)] : [1, text];
}

function byNodeId(left, right) {
	const a = nodeRank(left);
	const b = nodeRank(right);
	if (a[0] !== b[0]) {
		return a[0] - b[0];
	}
	if (typeof a[1] === "number") {
		return a[1] - b[1];
	}
	return String(a[1]).localeCompare(String(b[1]));
}

function isModelName(value) {
	if (typeof value !== "string") {
		return false;
	}
	const lower = value.toLowerCase();
	return MODEL_EXT.some((ext) => lower.endsWith(ext));
}

function isPrimitive(value) {
	const kind = typeof value;
	return kind === "string" || kind === "number" || kind === "boolean";
}

function valueType(key, value) {
	if (typeof value === "boolean") {
		return "boolean";
	}
	if (typeof value === "string") {
		return "string";
	}
	if (Number.isInteger(value) && SEED_KEYS.has(key)) {
		return "seed";
	}
	if (Number.isInteger(value)) {
		return "integer";
	}
	return "number";
}

function baseName(key) {
	let text = String(key).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
	if (!text || !/^[a-z]/.test(text)) {
		text = text ? `p_${text}` : "param";
	}
	text = text.slice(0, 41);
	return PARAM_NAME.test(text) ? text : "param";
}

function takeName(base, used) {
	if (!used.has(base)) {
		used.add(base);
		return base;
	}
	for (let number = 2; number < 10000; number += 1) {
		const suffix = `_${number}`;
		const trimmed = base.slice(0, 41 - suffix.length) + suffix;
		if (!used.has(trimmed) && PARAM_NAME.test(trimmed)) {
			used.add(trimmed);
			return trimmed;
		}
	}
	throw new Error("too many parameters");
}

function boundsFor(key, type, value) {
	if (type === "integer" && (key === "width" || key === "height")) {
		let step = 1;
		if (value % 64 === 0) {
			step = 64;
		} else if (value % 8 === 0) {
			step = 8;
		}
		return { min: value >= 64 ? 64 : value, max: value, step };
	}
	if (type === "integer") {
		return { min: value, max: value, step: 1 };
	}
	if (type === "number") {
		return { min: value, max: value, step: 0.01 };
	}
	return null;
}

function candidates(workflow) {
	const rows = [];
	const used = new Set();
	const ids = Object.keys(workflow).sort(byNodeId);
	ids.forEach((id) => {
		const node = workflow[id];
		if (!node || typeof node.class_type !== "string" || !node.inputs) {
			return;
		}
		Object.keys(node.inputs).forEach((key) => {
			const value = node.inputs[key];
			if (!isPrimitive(value) || Array.isArray(value)) {
				return;
			}
			if (MEDIA_KEY[node.class_type] === key) {
				return;
			}
			if (SAVERS.has(node.class_type) && key === "filename_prefix") {
				return;
			}
			if (isModelName(value)) {
				return;
			}
			const type = valueType(key, value);
			const row = {
				name: takeName(baseName(key), used),
				nodeId: String(id),
				classType: node.class_type,
				key,
				type,
				value,
				checked: EXPOSE.has(key),
				seedMode: "random",
			};
			const bounds = boundsFor(key, type, value);
			if (bounds) {
				row.min = bounds.min;
				row.max = bounds.max;
				row.step = bounds.step;
			}
			rows.push(row);
		});
	});
	return rows;
}

function ensureStyle() {
	if (document.getElementById("block-party-export-style")) {
		return;
	}
	const style = document.createElement("style");
	style.id = "block-party-export-style";
	style.textContent = `
		.bp-back { position: fixed; inset: 0; z-index: 100000; background: rgba(0,0,0,.55);
			display: flex; align-items: center; justify-content: center; }
		.bp-panel { width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 32px);
			overflow: auto; background: #1b1b1b; color: #eee; border: 1px solid #3a3a3a;
			border-radius: 10px; padding: 16px 18px 14px; font: 13px/1.4 sans-serif; }
		.bp-panel h2 { margin: 0 0 4px; font-size: 16px; }
		.bp-note { margin: 0 0 12px; color: #aaa; }
		.bp-panel label { display: block; margin: 8px 0 4px; color: #ccc; }
		.bp-panel input[type="text"], .bp-panel input[type="number"] {
			width: 100%; box-sizing: border-box; background: #111; color: #eee;
			border: 1px solid #444; border-radius: 6px; padding: 6px 8px; }
		.bp-list { margin: 8px 0 12px; }
		.bp-row { display: grid; grid-template-columns: 22px 1fr; gap: 8px;
			padding: 8px 0; border-top: 1px solid #333; }
		.bp-title { font-weight: 600; }
		.bp-meta { color: #999; font-size: 12px; }
		.bp-value { color: #bbb; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		.bp-range { display: flex; gap: 8px; margin-top: 6px; }
		.bp-range label { flex: 1; margin: 0; font-size: 12px; }
		.bp-seed { margin-top: 6px; color: #ccc; }
		.bp-seed label { display: inline-flex; align-items: center; gap: 4px; margin: 0 12px 0 0; }
		.bp-actions { display: flex; justify-content: flex-end; gap: 8px; }
		.bp-actions button { background: #2d2d2d; color: #eee; border: 1px solid #555;
			border-radius: 6px; padding: 6px 12px; cursor: pointer; }
		.bp-actions button[type="submit"] { background: #2f6fed; border-color: #2f6fed; }
		.bp-status { min-height: 1.2em; margin: 8px 0; color: #f0c674; }
	`;
	document.head.appendChild(style);
}

function preview(value) {
	if (typeof value === "string") {
		return value;
	}
	if (typeof value === "boolean") {
		return value ? "true" : "false";
	}
	return String(value);
}

function addRange(parent, label, value) {
	const field = document.createElement("label");
	field.textContent = label;
	const input = document.createElement("input");
	input.type = "text";
	input.value = String(value);
	field.appendChild(input);
	parent.appendChild(field);
	return input;
}

function parseInteger(text, label) {
	if (!/^[+-]?\d+$/.test(String(text).trim())) {
		throw new Error(`${label} must be an integer`);
	}
	return Number(String(text).trim());
}

function parseDecimal(text, label) {
	const value = Number(String(text).trim());
	if (!Number.isFinite(value)) {
		throw new Error(`${label} must be a number`);
	}
	return value;
}

function aligned(value, low, step) {
	if (!(step > 0)) {
		return false;
	}
	const span = (value - low) / step;
	return Math.abs(span - Math.round(span)) <= 1e-6;
}

function openDialog(workflow, rows) {
	ensureStyle();
	const existing = document.getElementById("block-party-export");
	if (existing) {
		existing.remove();
	}
	const back = document.createElement("div");
	back.className = "bp-back";
	back.id = "block-party-export";
	const panel = document.createElement("form");
	panel.className = "bp-panel";
	const title = document.createElement("h2");
	title.textContent = "Export job";
	const note = document.createElement("p");
	note.className = "bp-note";
	note.textContent = rows.length
		? "Check the values a run may change. The ceiling starts at the current value."
		: "This graph has no primitive values to expose.";
	const nameLabel = document.createElement("label");
	nameLabel.textContent = "Job name";
	const nameInput = document.createElement("input");
	nameInput.type = "text";
	nameInput.required = true;
	nameInput.autocomplete = "off";
	nameLabel.appendChild(nameInput);
	const list = document.createElement("div");
	list.className = "bp-list";
	const fields = rows.map((row) => {
		const wrap = document.createElement("div");
		wrap.className = "bp-row";
		const box = document.createElement("input");
		box.type = "checkbox";
		box.checked = row.checked;
		const body = document.createElement("div");
		const heading = document.createElement("div");
		heading.className = "bp-title";
		heading.textContent = row.name;
		const meta = document.createElement("div");
		meta.className = "bp-meta";
		meta.textContent = `${row.classType} ${row.nodeId}.${row.key}`;
		const value = document.createElement("div");
		value.className = "bp-value";
		value.textContent = preview(row.value);
		value.title = preview(row.value);
		body.append(heading, meta, value);
		const controls = { box };
		if (row.type === "integer" || row.type === "number") {
			const range = document.createElement("div");
			range.className = "bp-range";
			controls.min = addRange(range, "min", row.min);
			controls.max = addRange(range, "max", row.max);
			controls.step = addRange(range, "step", row.step);
			body.appendChild(range);
		}
		if (row.type === "seed") {
			const choice = document.createElement("div");
			choice.className = "bp-seed";
			const randomLabel = document.createElement("label");
			const random = document.createElement("input");
			random.type = "radio";
			random.name = `seed-${row.name}`;
			random.value = "random";
			random.checked = true;
			randomLabel.append(random, document.createTextNode(" random each file"));
			const fixedLabel = document.createElement("label");
			const fixed = document.createElement("input");
			fixed.type = "radio";
			fixed.name = `seed-${row.name}`;
			fixed.value = "fixed";
			fixedLabel.append(fixed, document.createTextNode(` keep ${row.value}`));
			choice.append(randomLabel, fixedLabel);
			body.appendChild(choice);
			controls.random = random;
		}
		wrap.append(box, body);
		list.appendChild(wrap);
		return { row, controls };
	});
	const status = document.createElement("div");
	status.className = "bp-status";
	const actions = document.createElement("div");
	actions.className = "bp-actions";
	const cancel = document.createElement("button");
	cancel.type = "button";
	cancel.textContent = "Cancel";
	const submit = document.createElement("button");
	submit.type = "submit";
	submit.textContent = "Export";
	actions.append(cancel, submit);
	panel.append(title, note, nameLabel, list, status, actions);
	back.appendChild(panel);
	document.body.appendChild(back);
	nameInput.focus();

	function close() {
		back.remove();
	}
	cancel.addEventListener("click", close);
	back.addEventListener("keydown", (event) => {
		if (event.key === "Escape") {
			close();
		}
	});

	panel.addEventListener("submit", async (event) => {
		event.preventDefault();
		const name = nameInput.value.trim();
		if (!JOB_NAME.test(name)) {
			status.textContent = "Job name must start with a letter or digit and use only letters, digits, _ and -.";
			return;
		}
		let parameters;
		try {
			parameters = fields.filter((item) => item.controls.box.checked).map((item) => readRow(item));
		} catch (error) {
			status.textContent = error.message;
			return;
		}
		submit.disabled = true;
		status.textContent = "Exporting the job. Large models are hashed before the folder is written.";
		try {
			const response = await api.fetchApi("/block_party/export", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ name, workflow, parameters }),
			});
			let body = {};
			try {
				body = await response.json();
			} catch (error) {
				body = {};
			}
			if (!response.ok || !body.ok) {
				submit.disabled = false;
				status.textContent = body.message || "Export failed.";
				return;
			}
			close();
			const ready = body.prepared ? "Prepared." : "Run prepare in the launcher before dropping files.";
			const exposed = body.parameters && body.parameters.length ? `Editable: ${body.parameters.join(", ")}.` : "";
			notify("success", [body.job, body.hint, exposed, ready].filter(Boolean).join("\n"));
		} catch (error) {
			submit.disabled = false;
			status.textContent = "Export failed.";
			console.error(error);
		}
	});
}

function readRow(item) {
	const { row, controls } = item;
	const spec = {
		name: row.name,
		node_id: row.nodeId,
		class_type: row.classType,
		key: row.key,
		type: row.type,
		default: row.value,
	};
	if (row.type === "seed") {
		spec.default = controls.random.checked ? "random" : row.value;
		return spec;
	}
	if (row.type === "integer" || row.type === "number") {
		const parse = row.type === "integer" ? parseInteger : parseDecimal;
		const min = parse(controls.min.value, `${row.name} min`);
		const max = parse(controls.max.value, `${row.name} max`);
		const step = parse(controls.step.value, `${row.name} step`);
		if (!(step > 0) || min > max) {
			throw new Error(`${row.name} has a broken range`);
		}
		if (row.value < min || row.value > max || !aligned(row.value, min, step)) {
			throw new Error(`${row.name} must be between ${min} and ${max} in steps of ${step}`);
		}
		spec.min = min;
		spec.max = max;
		spec.step = step;
	}
	return spec;
}

app.registerExtension({
	name: "BlockParty.Export",
	commands: [
		{
			id: "BlockParty.ExportJob",
			label: "Export job",
			function: async () => {
				let graph;
				try {
					graph = await app.graphToPrompt();
				} catch (error) {
					notify("error", "Could not read the graph.");
					console.error(error);
					return;
				}
				if (!graph || !graph.output) {
					notify("error", "Could not read the graph.");
					return;
				}
				let rows;
				try {
					rows = candidates(graph.output);
				} catch (error) {
					notify("error", "Could not read the values on this graph.");
					console.error(error);
					return;
				}
				openDialog(graph.output, rows);
			},
		},
	],
	menuCommands: [
		{
			path: ["Block Party"],
			commands: ["BlockParty.ExportJob"],
		},
	],
});
