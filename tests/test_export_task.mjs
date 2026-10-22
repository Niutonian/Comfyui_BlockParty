import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../web/export_task.js', import.meta.url), 'utf8');
const {waitForExport} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const requestId = 'a'.repeat(32);
const options = {requestId, pause: async () => {}};
const json = (body, status = 200) => new Response(JSON.stringify(body), {status});
const running = () => json({ok: true, state: 'running', elapsed_seconds: 60}, 202);
const complete = (result = {ok: true}, result_status = 200) => json({ok: true, state: 'complete', result, result_status});

test('lost start response and polling failure reconnect with the same task ID', async () => {
    const calls = [], messages = [];
    const replies = [new Error('fetch timeout'), running(), new Error('offline'), complete({ok: true, share_zip: 'demo.zip'})];
    const api = {fetchApi: async (url, request) => {
        calls.push({url, request});
        const reply = replies.shift();
        if (reply instanceof Error) throw reply;
        return reply;
    }};
    const response = await waitForExport(api, '/block_party/export', '{"name":"demo"}', message => messages.push(message), options);
    assert.equal((await response.json()).share_zip, 'demo.zip');
    assert.deepEqual(calls.map(c => c.url), ['/block_party/export', '/block_party/export', '/block_party/export_status', '/block_party/export_status']);
    assert.ok(calls.every(c => c.request.headers['X-Block-Party-Request'] === requestId));
    assert.equal(calls[0].request.body, calls[1].request.body);
    assert.equal(JSON.parse(calls[2].request.body).task_id, requestId);
    assert.ok(messages.some(m => m.includes('60s')));
});

test('slow task stays in polling until completion without resubmission', async () => {
    let starts = 0, polls = 0;
    const api = {fetchApi: async url => {
        if (url === '/block_party/export_preview') { starts++; return running(); }
        polls++;
        return polls < 10 ? running() : complete({ok: true, readiness: {ready: true}});
    }};
    const response = await waitForExport(api, '/block_party/export_preview', '{}', () => {}, options);
    assert.equal(starts, 1);
    assert.equal(polls, 10);
    assert.equal((await response.json()).readiness.ready, true);
});

test('completed task failure is returned to existing export error handling', async () => {
    const replies = [running(), complete({ok: false, message: 'model missing'}, 400)];
    const response = await waitForExport({fetchApi: async () => replies.shift()}, '/block_party/export', '{}', () => {}, options);
    assert.equal(response.status, 400);
    assert.equal((await response.json()).message, 'model missing');
});

test('lost response body retries the original start identity', async () => {
    const calls = [];
    const replies = [{ok: true, status: 202, json: async () => {throw new Error('connection lost');}}, running(), complete()];
    await waitForExport({fetchApi: async url => {calls.push(url); return replies.shift();}}, '/block_party/export', '{}', () => {}, options);
    assert.deepEqual(calls, ['/block_party/export', '/block_party/export', '/block_party/export_status']);
});
