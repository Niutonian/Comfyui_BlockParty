const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

function notify(severity, detail) {
	const toast = app.extensionManager && app.extensionManager.toast;
	if (toast) {
		toast.add({ severity, summary: "Block Party", detail, life: 8000 });
		return;
	}
	window.alert(detail);
}

app.registerExtension({
	name: "BlockParty.Export",
	commands: [
		{
			id: "BlockParty.ExportJob",
			label: "Export job",
			function: async () => {
				const name = window.prompt("Job name");
				if (!name) {
					return;
				}
				notify("info", "Exporting the job. Large models are hashed before the folder is written.");
				let graph;
				try {
					graph = await app.graphToPrompt();
				} catch (error) {
					notify("error", "Could not read the graph.");
					console.error(error);
					return;
				}
				const response = await api.fetchApi("/block_party/export", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ name, workflow: graph.output }),
				});
				const body = await response.json();
				if (!response.ok || !body.ok) {
					notify("error", body.message || "Export failed.");
					return;
				}
				const ready = body.prepared ? "Prepared." : "Run prepare in the launcher before dropping files.";
				notify("success", `${body.job}\n${body.hint}\n${ready}`);
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
