import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appSource = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const refinementCss = fs.readFileSync(path.join(root, "web", "app-refinement.css"), "utf8");
const match = appSource.match(/function greetingForHour\(hour\) \{[\s\S]*?\n  \}/);
assert.ok(match, "greetingForHour must exist in production app.js");
const greetingForHour = Function(`"use strict"; ${match[0]}; return greetingForHour;`)();

for (const [hour, expected] of [[0,"晚上好"],[4,"晚上好"],[5,"早上好"],[10,"早上好"],[11,"中午好"],[13,"中午好"],[14,"下午好"],[17,"下午好"],[18,"晚上好"],[23,"晚上好"]]) {
  assert.equal(greetingForHour(hour), expected, `unexpected greeting at hour ${hour}`);
}

for (const id of ["connectionBadge","homeView","mapView","mapOffline","mapOnlineContent","petView","fitnessView","controlView","mapStage","mapCanvas","mapSourceText","targetPanel","activityText","petVideo","fitnessVideo","petHeroPlay","fitnessHeroPlay","feedDialog","programBadge","programStart","programStop","programStopDialog","confirmProgramStop","microphoneBadge","microphoneEnable","microphoneDisable","microphoneHint"]) {
  assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} must exist exactly once`);
}
for (const action of ["manual_move","navigate","light","feed","stop","program_start","program_stop"]) assert.ok(appSource.includes(`action:\"${action}\"`) || html.includes(`&quot;action&quot;:${action}`) || html.includes(`\"action\":\"${action}\"`) || appSource.includes(`payload.action === \"${action}\"`), `${action} command missing`);
assert.ok(appSource.includes('duration: isLinearMove ? 0.42 : 0.25'), "linear point-control duration must remain 0.42s");
assert.ok(appSource.includes('{linear_speed: 0.18}'), "linear point-control speed must remain 0.18m/s");
assert.ok(html.includes('href="vendor/css/all.min.css"'), "icons must be bundled locally");
assert.ok(html.includes('href="connection-states.css"'), "connection-state styles must be bundled locally");
assert.ok(!/https?:\/\//.test(html), "HTML must not depend on remote visual assets");
assert.ok(html.includes('data-robot-online="false"'), "the map must start in the offline-hidden state");
assert.ok(appSource.includes('if (!state.online || !state.mapMeta)'), "offline clients must not fetch a map");
assert.ok(appSource.includes('if (!state.online) { clearMap(); return; }'), "offline clients must not draw a map");
assert.ok(appSource.includes('replaceAll("豆豆", "宠物")'), "legacy video titles must be normalized for display");
assert.ok(!html.includes("定点"), "visible app copy must not contain the old edition label");
assert.ok(!html.includes("豆豆"), "visible app copy must use the generic pet label");
assert.equal((html.match(/class="media-play"/g) || []).length, 2, "pet and fitness pages must share one play-button style");
assert.equal((html.match(/<video[^>]*\scontrols(?:\s|>)/g) || []).length, 0, "native controls must stay hidden until the single hero button is used");
assert.ok(appSource.includes('function startHeroPlayback('), "the two video pages must share one playback initializer");
assert.ok(appSource.includes('video.controls = true'), "native seek/pause controls must be enabled after playback starts");
assert.ok(refinementCss.includes('.bottom-nav { grid-template-columns: repeat(5, 1fr); }'), "five navigation entries require five columns");
const homeSection = html.match(/<section id="homeView"[\s\S]*?<\/section>/)?.[0] || "";
const controlSection = html.match(/<section id="controlView"[\s\S]*?<\/section>/)?.[0] || "";
for (const id of ["programStart", "programStop", "programBadge"]) assert.ok(homeSection.includes(`id="${id}"`), `${id} must be on the home page`);
assert.ok(homeSection.includes('id="voiceHoldButton"'), "voice control must remain on the home page");
assert.ok(homeSection.includes("千问端到端实时会话"), "home voice copy must identify the Qwen end-to-end path");
assert.ok(!homeSection.includes('class="quick-panel"'), "home quick controls must be removed");
assert.ok(!html.includes('id="quickFeed"'), "removed quick-feed button must not remain in HTML");
assert.ok(!appSource.includes('$("quickFeed")'), "removed quick-feed button must not be bound by JavaScript");
assert.ok(!controlSection.includes('class="program-card'), "program controls must be removed from the control page");
assert.ok(controlSection.includes('class="control-card"'), "direction controls must remain on the control page");
assert.ok(controlSection.includes('class="device-list"'), "furniture controls must remain on the control page");
for (const id of ["microphoneBadge", "microphoneEnable", "microphoneDisable"]) assert.ok(controlSection.includes(`id="${id}"`), `${id} must be on the control page`);
assert.ok(controlSection.includes("App 的按住说话、地图、家居和按键控制仍可正常使用"), "microphone copy must preserve all App controls");
assert.ok(appSource.includes('action: "request_state"'), "deployed-relay compatibility command must remain available");
assert.ok(appSource.includes('`mic-set-${enabled ? 1 : 0}-'), "microphone compatibility command must encode only the desired boolean state");
assert.ok(appSource.includes('state.microphone = message.microphone || (state.task && state.task.microphone)'), "microphone telemetry must support old and new relays");
assert.ok(appSource.includes('message.type === "link_heartbeat"'), "independent robot link heartbeat must update App connectivity");
assert.ok(appSource.includes('message.type === "server_heartbeat"'), "App-to-relay health must stay independent from robot telemetry");
assert.ok(appSource.includes('const CONNECT_TIMEOUT_MS = 5000'), "Tailscale connection setup must tolerate normal network latency");
assert.ok(appSource.includes('const LINK_STALE_MS = 4500'), "server heartbeat loss must still be detected promptly");
assert.ok(appSource.includes('const OFFLINE_GRACE_MS = 8000'), "brief network jitter must show reconnecting instead of false offline");
assert.ok(appSource.includes('retryLastHealthy'), "a healthy Tailscale endpoint must be retried before a slower fallback");
assert.ok(appSource.includes('endpointFailures: new Map()'), "endpoint retry state must be tracked per relay");

console.log(JSON.stringify({ok:true, greetingCases:10, requiredIds:14, actions:5, offlineMap:true, genericLabels:true}));
