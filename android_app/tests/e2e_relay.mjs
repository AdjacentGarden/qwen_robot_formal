const base = process.env.RELAY_URL || "ws://100.125.188.94:8765";
const token = process.env.APP_TOKEN;
if (!token) throw new Error("APP_TOKEN required");
const ws = new WebSocket(`${base}/ws/app?token=${encodeURIComponent(token)}`);
const pending = new Map();
let robotOnline = false;
ws.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.type === "state") robotOnline = Boolean(message.robot?.online);
  if (message.type === "robot_status") robotOnline = Boolean(message.robot?.online);
  if (message.type === "command_result" && pending.has(message.id)) {
    pending.get(message.id)(message); pending.delete(message.id);
  }
});
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, {once:true});
  ws.addEventListener("error", reject, {once:true});
  setTimeout(() => reject(new Error("app websocket open timeout")), 5000);
});
for (let i=0; i<30 && !robotOnline; i++) await new Promise(r => setTimeout(r,100));
if (!robotOnline) throw new Error("dry-run robot did not connect");
async function command(payload) {
  const id = crypto.randomUUID();
  const answer = new Promise((resolve, reject) => {
    pending.set(id, resolve); setTimeout(() => reject(new Error(`result timeout: ${payload.action}`)), 5000);
  });
  ws.send(JSON.stringify({id,...payload}));
  const result = await answer;
  if (!result.ok) throw new Error(JSON.stringify(result));
  return result;
}
const cases = [
  {action:"manual_move",direction:"forward",duration:.25},
  {action:"manual_move",direction:"left",duration:.25},
  {action:"light",state:"on"},
  {action:"light",state:"off"},
  {action:"feed",grams:20},
  {action:"navigate",x:.5,y:0,yaw:0},
  {action:"stop"},
];
for (const item of cases) await command(item);
console.log(JSON.stringify({ok:true,cases:cases.length,robotOnline}));
ws.close();
