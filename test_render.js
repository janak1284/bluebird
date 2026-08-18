const fs = require('fs');

const rawHtml = fs.readFileSync('/home/chorusofthesaints/Documents/parallax/bluebird/static/index.html', 'utf8');

// We just want to extract the renderCockpitNodes function and run it on a fake payload
const match = rawHtml.match(/function renderCockpitNodes\s*\([\s\S]*?container\.innerHTML = html;\s*\}/);
if (!match) {
    console.error("Function not found");
    process.exit(1);
}

let funcCode = match[0];
// Mock DOM
global.document = {
    getElementById: (id) => ({ innerHTML: '', innerText: '' })
};
// Mock functions
global.escapeHtml = (str) => str;
global.toggleNodeStatus = () => {};
global.prevPlacementMap = {};

eval(funcCode);

const payload = {
    nodes: {
        "node1": {
            tier: "core", raw_hostname: "host1", ready: true, cpu_util: 0.5, mem_util: 0.5,
            base_latency_ms: 10, power_w: 100, cost_per_hr: 5, cpu_cores: 4
        }
    },
    workloads: {}
};

try {
    renderCockpitNodes(payload);
    console.log("Success! No JS errors in renderCockpitNodes.");
} catch(e) {
    console.error("Error running renderCockpitNodes:", e);
}
