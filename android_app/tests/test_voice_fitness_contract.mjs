import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");
const app = read("web/app.js");
const html = read("web/index.html");
const manifest = read("android/app/src/main/AndroidManifest.xml");
const bridge = read("robot_bridge/bridge.py");
const agent = read("../realtime_chat.py");
const service = read("../resident_service.sh");
const fitness = read("../robot_skills/push_up/engine.py");

for (const id of ["voiceHoldButton", "voiceHint", "taskStatus", "fitnessList"]) {
  assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} missing or duplicated`);
}
for (const token of [
  "navigator.mediaDevices.getUserMedia", "new MediaRecorder", 'addEventListener("pointerdown"',
  'addEventListener("pointerup"', 'api("/api/app/voice")', '"X-Command-Id"',
  '"X-Audio-Duration-Ms"', 'item.label === "语音指令"',
]) assert.ok(app.includes(token), `voice contract missing: ${token}`);

assert.ok(manifest.includes("android.permission.RECORD_AUDIO"));
assert.ok(manifest.includes("android.permission.MODIFY_AUDIO_SETTINGS"));
assert.ok(app.includes('video.category === "fitness"'));
assert.ok(app.includes('video.category !== "fitness"'));
assert.ok(app.includes("原始视频"));
assert.ok(app.includes("identityLabel"));

for (const operation of ['"app_voice"', '"app_skill"', '"app_scenario"', '"status"', '"cancel_all"', '"microphone_set"']) {
  assert.ok(agent.includes(operation), `agent operation missing: ${operation}`);
}
assert.ok(agent.includes("start_control_server"));
assert.ok(agent.includes("send_external_audio"));
assert.ok(agent.includes("if not self.local_microphone_enabled"));
assert.ok(agent.includes('"app_voice_enabled": True'));
assert.ok(agent.includes("save_microphone_enabled"));
assert.ok(
  service.includes('run.sh" --execute-skills') ||
  (service.includes("run_args=(--execute-skills)") && service.includes('"${run_args[@]}"')),
  "resident service must launch the complete --execute-skills runtime",
);
assert.ok(bridge.includes('"busy_policy": busy_policy'));
assert.ok(bridge.includes('elif action == "voice_audio"'));
assert.ok(bridge.includes('message.get("task")') || bridge.includes('"task": self.task_state'));
assert.ok(agent.includes('"active_procedures"'));
assert.ok(app.includes("procedureNames"));
assert.ok(app.includes("task.active_procedures"));
for (const procedure of ["homecoming_welcome", "find_pet", "find_pet_at", "find_pet_here", "find_and_feed_doudou", "meeting_projection", "meeting_projection_stop", "rest_lighting"]) {
  assert.ok(app.includes(`${procedure}:`), `App task label missing: ${procedure}`);
}

const rawWrite = fitness.indexOf("raw_writer.write(raw_frame");
const sampling = fitness.indexOf("if frame_index % frame_step");
assert.ok(rawWrite > 0 && sampling > rawWrite, "raw video must be written before inference frame sampling");
assert.ok(fitness.includes('"category": "fitness"'));
assert.ok(fitness.includes('"status": "pending_upload"'));
assert.ok(!fitness.includes("原始视频正在同步到手机"), "fitness completion speech must not announce upload");

console.log(JSON.stringify({
  ok: true,
  appVoice: true,
  androidPermissions: true,
  unifiedScheduling: true,
  rawFitnessVideo: true,
  fitnessMetadata: true,
}));
