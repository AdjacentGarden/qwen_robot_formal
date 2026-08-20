import assert from "node:assert/strict";

const port = Number(process.argv[2] || 8871);
const response = await fetch(`http://127.0.0.1:${port}/__commands`);
assert.equal(response.ok, true);
const commands = (await response.json()).commands;
const actions = commands.map((item) => item.action);
for (const action of ["program_start","program_stop","manual_move","navigate","light","feed"]) assert.ok(actions.includes(action), `${action} was not emitted`);
for (const direction of ["forward","backward","left","right","stop"]) assert.ok(commands.some((item) => item.action === "manual_move" && item.direction === direction), `${direction} point-control was not emitted`);
assert.ok(commands.some((item) => item.action === "light" && item.state === "on"));
assert.ok(commands.some((item) => item.action === "light" && item.state === "off"));
assert.ok(commands.some((item) => item.action === "feed" && Number.isFinite(item.grams)));
assert.ok(commands.some((item) => item.action === "navigate" && Number.isFinite(item.x) && Number.isFinite(item.y) && Number.isFinite(item.yaw)));
console.log(JSON.stringify({ok:true, port, commandCount:commands.length, actions:[...new Set(actions)]}));
