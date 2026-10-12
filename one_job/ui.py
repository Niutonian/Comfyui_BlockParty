"""Shared local browser presentation. No external assets or scripts."""
from html import escape

STYLE = '''
:root{color-scheme:dark;--bg:#10151a;--panel:#1a222a;--text:#eef4f5;--muted:#aebec6;--accent:#5cdbc5;--line:#33424c}
*{box-sizing:border-box}body{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--text);max-width:1120px;margin:32px auto;padding:0 24px}
a{color:var(--accent)}h1,h2,h3{line-height:1.2}h1{font-size:36px}h2{margin-top:32px}p{color:var(--muted)}nav{display:flex;gap:20px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:28px}nav strong{font-size:22px;letter-spacing:2px;color:var(--accent)}
button,.button{display:inline-block;background:var(--accent);color:#092b29;border:0;border-radius:8px;padding:10px 16px;font:600 15px system-ui;cursor:pointer;text-decoration:none;margin:8px 8px 8px 0}button:disabled{opacity:.45;cursor:wait}.secondary{background:#2c3b45;color:var(--text)}
input,textarea,select{font:inherit;background:#11191f;color:var(--text);border:1px solid #4b5f6d;border-radius:6px;padding:9px;max-width:100%}input[type=text],input[type=password],input[type=number],textarea{width:100%}input[type=checkbox]{width:auto}textarea{min-height:120px}label{display:block;margin-top:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b1116;padding:16px;border-radius:8px;color:#cfdee4}.hint,.mark{color:var(--muted)}.error{color:#ffa99c}.success{color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:20px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;overflow:hidden}.card img,.card video{width:100%;max-height:300px;object-fit:contain;border-radius:8px;background:#0b1116}.card audio{width:100%}.badge{display:inline-block;background:#293b44;border-radius:20px;padding:4px 10px;font-size:13px}.drop{border:2px dashed #496775;border-radius:12px;padding:24px;background:#152129}.drop.over{border-color:var(--accent)}.hero{width:100%;border-radius:12px;margin-bottom:20px}progress{width:100%;accent-color:var(--accent)}.stats{display:flex;flex-wrap:wrap;gap:16px}.stats span{padding:8px 12px;background:var(--panel);border-radius:8px}details{margin:16px 0}summary{cursor:pointer}form{margin-bottom:12px}small{color:var(--muted)}@media(max-width:600px){body{padding:0 14px;margin-top:18px}nav{flex-wrap:wrap}h1{font-size:28px}}
/* Shared visual language: cool surfaces, warm accents, and clear control groups. */
body{max-width:1180px;background:radial-gradient(ellipse at 15% 0%,#163036 0,transparent 48%),var(--bg);background-attachment:fixed}
nav{font-size:14px}nav a{text-decoration:none;padding:6px 0}nav a:hover{color:#fff}nav strong{margin-right:auto}
.brand-header{position:relative;isolation:isolate;min-height:210px;overflow:hidden;border:1px solid #42616c;border-radius:20px;margin:0 0 28px;background:#14262e}
.brand-header img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:70% 58%;z-index:-2}
.brand-header:after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,rgba(10,24,30,.97),rgba(10,24,30,.84) 42%,rgba(10,24,30,.12));z-index:-1}
.brand-copy{padding:32px 36px;max-width:620px}.eyebrow{font-size:11px;font-weight:750;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);margin:0 0 12px}
.brand-title{font-family:Impact,'Arial Black',system-ui,sans-serif;font-weight:950;font-size:clamp(38px,6vw,64px);line-height:1;letter-spacing:2px;color:#f5fafb;text-shadow:3px 3px 0 #227e7c}.brand-title span{color:#ffbc75}.brand-copy p:last-child{font-size:14px;margin:16px 0 0;max-width:340px;color:#d3e0e5}
.panel{background:linear-gradient(135deg,#1b272f,#172129);border:1px solid var(--line);border-radius:16px;padding:24px;margin:20px 0;min-width:0}.panel>h2:first-child,.drop>h2:first-child{margin-top:0}.panel summary{font-weight:650;font-size:17px}.panel summary+p{margin-top:18px}.panel form:last-child{margin-bottom:0}
.workspace-title{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;margin:24px 0}.workspace-title h1{margin:4px 0;font-size:30px;overflow-wrap:anywhere}.workspace-title .eyebrow{margin:0}.workspace-title p{margin:4px 0}
.button-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.button-row form{margin:0}.button-row button{margin:0}.subtle{font-size:13px;color:var(--muted)}.run-actions{padding-top:20px;border-top:1px solid var(--line);margin-top:24px}.run-actions p{margin-bottom:0}
button,.button{transition:background .15s,box-shadow .15s;min-height:42px}button:hover,.button:hover{background:#86ead8;box-shadow:0 0 0 1px #8be7d4}.secondary:hover{background:#3b505d;color:#fff}.primary{box-shadow:0 3px 14px #5cdbc51c}.danger{background:#553a3c;color:#ffc4bb}
:focus-visible{outline:3px solid #ffbc75;outline-offset:3px}input:focus,textarea:focus{border-color:var(--accent)}input[type=checkbox]{accent-color:var(--accent)}input[type=file]::file-selector-button{background:#2d454f;border:0;border-radius:5px;color:var(--text);padding:7px 10px;margin-right:12px;cursor:pointer}input[type=file]{width:100%;font-size:14px}label{font-size:14px;font-weight:600}textarea{line-height:1.6;resize:vertical}
.preset-row{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.preset-row label{flex:1;min-width:160px}.preset-row button{margin-bottom:0}.notice{padding:14px 18px;border:1px solid #367b70;background:#17342f;color:#b5f7dd;border-radius:10px}.error{padding:12px 16px;background:#41262a;border-radius:10px;overflow-wrap:anywhere}
.panel>section>h2:first-child,form.panel>h2:first-of-type{margin-top:0}
#log{max-height:320px;overflow:auto;font-size:13px;min-height:56px}.support textarea{min-height:260px;font:13px/1.5 ui-monospace,monospace}.recent-jobs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:24px}.recent-jobs a{max-width:100%;overflow-wrap:anywhere}.grid{grid-template-columns:repeat(auto-fit,minmax(min(270px,100%),1fr))}
@media(max-width:600px){.brand-header{min-height:180px}.brand-copy{padding:26px 22px}.brand-copy p:last-child{max-width:230px}.panel,.drop{padding:18px}.workspace-title h1{font-size:26px}.button-row{gap:8px}.brand-header:after{background:linear-gradient(90deg,rgba(10,24,30,.95),rgba(10,24,30,.65))}nav{gap:14px}nav strong{width:100%;font-size:18px}}
'''


def brand_header(image_url):
    return ('<header class="brand-header"><img src="' + escape(image_url, quote=True) + '" alt="Robots celebrating around connected ComfyUI nodes">'
            '<div class="brand-copy"><p class="eyebrow">Create. Connect. Share.</p><div class="brand-title" aria-label="Block Party">BLOCK <span>PARTY</span></div>'
            '<p>Your workflows. Your machine. A little more possibility.</p></div></header>')


def page(title, content, script=''):
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)} | Block Party</title><style>{STYLE}</style></head><body>'
            '<nav><strong>BLOCK PARTY</strong><a href="/">Library</a><a href="/setup">Machine setup</a></nav>'
            + (brand_header('/header') if title == 'Machine setup' else '')
            + content + ('<script>' + script + '</script>' if script else '') + '</body></html>')
