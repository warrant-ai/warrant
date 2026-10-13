// Runs the JS workflow worker for the cross-language test against a server the Python test started.
// Usage: node temporal-worker.mjs <address> <namespace> <taskQueue>. Prints "ready", stops on SIGTERM.
import { fileURLToPath } from "node:url";

import { DefaultLogger, NativeConnection, Runtime, Worker } from "@temporalio/worker";

import { Warrant } from "../../src/index.js";
import { ActivityDecision, warrantPlugin } from "../../src/adapters/temporal.js";

const [address, namespace, taskQueue] = process.argv.slice(2);
Runtime.install({ logger: new DefaultLogger("WARN") });
const quiet = { info() {}, warn() {}, error() {} };
// The activities run on the Python worker, which records them; this worker only runs the workflow.
const w = new Warrant("lending", { store: { write() {} }, logger: quiet });
const plugin = warrantPlugin(w, { disburse_loan: new ActivityDecision({ decisionClass: "credit.approve", subject: "loan_id" }) });
const connection = await NativeConnection.connect({ address });
const worker = await Worker.create({
  connection, namespace, taskQueue, plugins: [plugin],
  workflowsPath: fileURLToPath(new URL("./temporal-workflows.js", import.meta.url)),
});
process.on("SIGTERM", () => worker.shutdown());
const running = worker.run();
console.log("ready");
await running;
await connection.close();
await w.close();
