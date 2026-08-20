import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {WebSocketServer} from "ws";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const web = path.join(root, "web");
const port = Number(process.env.MOCK_APP_PORT || 8871);
const token = "mock-app-token";
const commands = [];
let robotOnline = true;
let programState = "stopped";

const mapSvg = Buffer.from(`<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">
  <rect width="320" height="240" fill="#dfe4e3"/>
  <path d="M22 22H298V218H22Z" fill="#fafafa" stroke="#59656a" stroke-width="8"/>
  <path d="M160 22V86M160 150V218M22 120H104M216 120H298" stroke="#59656a" stroke-width="6"/>
  <rect x="112" y="92" width="96" height="56" rx="9" fill="#edf1ef" stroke="#c2ccca" stroke-width="3"/>
</svg>`, "utf8");

const videos = [
  {id:"mock-fitness-1", title:"俯卧撑 · 9个", category:"fitness", exercise:"push_up", exercise_label:"俯卧撑", count:9, identity:"zhangsan", created_at:1786401800, duration_sec:30},
  {id:"mock-doudou-1", title:"豆豆在书房休息", created_at:1786400000, duration_sec:5},
  {id:"mock-doudou-2", title:"豆豆刚刚吃完饭", created_at:1786396400, duration_sec:5},
];

function sendJson(response, value, status = 200) {
  response.writeHead(status, {"content-type":"application/json; charset=utf-8", "cache-control":"no-store", "access-control-allow-origin":"*"});
  response.end(JSON.stringify(value));
}

function contentType(file) {
  return ({".html":"text/html; charset=utf-8", ".js":"text/javascript; charset=utf-8", ".css":"text/css; charset=utf-8", ".png":"image/png", ".woff2":"font/woff2"})[path.extname(file)] || "application/octet-stream";
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (url.pathname === "/__commands") return sendJson(response, {commands});
  if (url.pathname === "/__reset") { commands.length = 0; return sendJson(response, {ok:true}); }
  if (url.pathname === "/__online") {
    robotOnline = url.searchParams.get("value") !== "0";
    for (const client of sockets.clients) client.send(JSON.stringify({type:"robot_status", robot:{online:robotOnline, mode:robotOnline?"ready":"offline"}}));
    return sendJson(response, {ok:true, robotOnline});
  }
  if (url.pathname === "/__pose") {
    const pose = {x:Number(url.searchParams.get("x") || 0), y:Number(url.searchParams.get("y") || 0), yaw:Number(url.searchParams.get("yaw") || 0)};
    for (const client of sockets.clients) client.send(JSON.stringify({type:"telemetry", pose, robot:{online:true, mode:"ready"}}));
    return sendJson(response, {ok:true, pose});
  }
  if (url.pathname === "/__video") {
    const video = {id:`mock-live-${Date.now()}`, title:"豆豆刚刚被发现", created_at:Date.now()/1000, duration_sec:5};
    videos.unshift(video);
    for (const client of sockets.clients) client.send(JSON.stringify({type:"video_available", video}));
    return sendJson(response, {ok:true, video});
  }
  if (url.pathname === "/__disconnect") {
    for (const client of sockets.clients) client.close(4010, "mock reconnect test");
    return sendJson(response, {ok:true});
  }
  if (url.pathname === "/config.js") {
    const mockConfig = fs.readFileSync(path.join(root, "tests", "mock_config.js"));
    response.writeHead(200, {"content-type":"text/javascript; charset=utf-8", "cache-control":"no-store"}); return response.end(mockConfig);
  }
  if (url.pathname === "/api/videos") return sendJson(response, {ok:true, videos});
  if (url.pathname === "/api/map") { response.writeHead(200, {"content-type":"image/svg+xml", "cache-control":"no-store", "access-control-allow-origin":"*"}); return response.end(mapSvg); }
  if (/^\/api\/videos\/[^/]+\/thumb$/.test(url.pathname)) {
    const image = fs.readFileSync(path.join(web, "assets", "doudou-hero.png"));
    response.writeHead(200, {"content-type":"image/png", "cache-control":"no-store", "access-control-allow-origin":"*"}); return response.end(image);
  }
  if (/^\/api\/videos\/[^/]+\/file$/.test(url.pathname)) { response.writeHead(204, {"access-control-allow-origin":"*"}); return response.end(); }
  const relative = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname).replace(/^\/+/, "");
  const file = path.resolve(web, relative);
  if (!file.startsWith(web) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { response.writeHead(404); return response.end("not found"); }
  response.writeHead(200, {"content-type":contentType(file), "cache-control":"no-store"});
  response.end(fs.readFileSync(file));
});

const sockets = new WebSocketServer({noServer:true});
server.on("upgrade", (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (url.pathname !== "/ws/app" || url.searchParams.get("token") !== token) { socket.write("HTTP/1.1 401 Unauthorized\r\n\r\n"); return socket.destroy(); }
  sockets.handleUpgrade(request, socket, head, (client) => sockets.emit("connection", client));
});

sockets.on("connection", (client) => {
  client.send(JSON.stringify({
    type:"state", robot:{online:robotOnline, mode:robotOnline?"ready":"offline"}, pose:{x:0.15,y:-0.08,yaw:0.12},
    map:{width:320,height:240,resolution:0.025,origin:{x:-4,y:-3,yaw:0}}, videos,
    program:{state:programState}, task:{active:false,planning:false,queued:0},
  }));
  client.on("message", (raw) => {
    const command = JSON.parse(String(raw));
    commands.push({...command, received_at:Date.now()});
    if (!robotOnline) {
      return setTimeout(() => client.send(JSON.stringify({type:"command_ack", id:command.id, ok:false, error:"robot_offline"})), 12);
    }
    setTimeout(() => client.send(JSON.stringify({type:"command_ack", id:command.id, ok:true, status:"forwarded"})), 12);
    setTimeout(() => {
      if (command.action === "program_start") programState = "running";
      if (command.action === "program_stop") programState = "stopped";
      const program = {state:programState};
      if (command.action === "program_start" || command.action === "program_stop") {
        client.send(JSON.stringify({type:"program_status", program}));
      }
      client.send(JSON.stringify({type:"command_result", id:command.id, ok:true, action:command.action, result:{program}}));
    }, 42);
  });
});

server.listen(port, "127.0.0.1", () => console.log(JSON.stringify({ok:true, port, token, root})));

function shutdown() { sockets.close(); server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 1000).unref(); }
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
