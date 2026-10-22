// The same ID is reused if the initial HTTP response is lost, so retries do
// not start another hash/export. Only short start/status requests are awaited.
export async function waitForExport(api, url, payload, progress, options = {}) {
	const requestId = options.requestId || Array.from(crypto.getRandomValues(new Uint8Array(16)),
		byte => byte.toString(16).padStart(2, "0")).join("");
	const pause = options.pause || (() => new Promise(resolve => setTimeout(resolve, 1500)));
	let started = false;
	for (;;) {
		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), 15000);
		let response;
		try {
			response = await api.fetchApi(started ? "/block_party/export_status" : url, {
				method: "POST", signal: controller.signal,
				headers: { "Content-Type": "application/json", "X-Block-Party": "export",
					"X-Block-Party-Async": "1", "X-Block-Party-Request": requestId },
				body: started ? JSON.stringify({task_id: requestId}) : payload,
			});
		} catch (error) {
			progress("Connection interrupted. Reconnecting to the same export task automatically…");
		} finally {
			clearTimeout(timer);
		}
		if (!response || [408, 429, 502, 503, 504].includes(response.status)) {
			await pause();
			continue;
		}
		if (!response.ok) return response;
		// Older backends return the final result directly. Restart ComfyUI to
		// activate background tasks after installing this update.
		if (!started && response.status !== 202) return response;
		let task;
		try {
			task = await response.json();
		} catch (error) {
			progress("Response interrupted. Reconnecting to the same export task automatically…");
			await pause();
			continue;
		}
		if (!task.ok) throw new Error(task.message || "Could not inspect export task.");
		started = true;
		if (task.state === "complete") {
			return new Response(JSON.stringify(task.result), {
				status: task.result_status, headers: {"Content-Type": "application/json"},
			});
		}
		progress(`Working (${task.elapsed_seconds}s). Large models can take several minutes to verify. You do not need to click again.`);
		await pause();
	}
}
