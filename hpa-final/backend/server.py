"""
HPA Live Dashboard — FastAPI WebSocket Backend
Reads REAL metrics from minikube via kubectl subprocess calls
"""

import asyncio
import json
import subprocess
import re
from datetime import datetime
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="HPA Live Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

clients: Set[WebSocket] = set()

STATE = {
    "cpu_current":   0,
    "cpu_target":    50,
    "pods_current":  0,
    "pods_desired":  0,
    "pods_min":      2,
    "pods_max":      10,
    "hpa_status":    "Unknown",
    "events":        [],
    "pod_list":      [],
    "kubectl_raw":   "",
    "top_raw":       "",
    "load_active":   False,
    "timestamp":     "",
    "error":         None,
}


def run_kubectl(args: list, timeout: int = 8):
    try:
        result = subprocess.run(
            ["kubectl"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "kubectl timed out"
    except FileNotFoundError:
        return "", "kubectl not found — is minikube running?"


def get_hpa_metrics():
    stdout, stderr = run_kubectl([
        "get", "hpa", "webapp-hpa",
        "-o", "jsonpath={.status.currentReplicas},{.status.desiredReplicas},"
               "{.spec.minReplicas},{.spec.maxReplicas},"
               "{.status.currentMetrics[0].resource.current.averageUtilization},"
               "{.spec.metrics[0].resource.target.averageUtilization}"
    ])

    if stderr and not stdout:
        return {"error": stderr}

    parts = stdout.split(",")
    if len(parts) < 6:
        return {"error": f"Unexpected HPA output: {stdout!r}"}

    def safe_int(v, default=0):
        try: return int(v)
        except: return default

    return {
        "pods_current":  safe_int(parts[0]),
        "pods_desired":  safe_int(parts[1]),
        "pods_min":      safe_int(parts[2], 2),
        "pods_max":      safe_int(parts[3], 10),
        "cpu_current":   safe_int(parts[4]),
        "cpu_target":    safe_int(parts[5], 50),
        "error":         None,
    }


def get_pod_list():
    stdout, _ = run_kubectl([
        "get", "pods", "-l", "app=webapp",
        "-o", "jsonpath={range .items[*]}{.metadata.name},{.status.phase},"
              "{.status.conditions[?(@.type=='Ready')].status}\\n{end}"
    ])
    pods = []
    for line in stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            pods.append({
                "name":  parts[0],
                "phase": parts[1],
                "ready": parts[2] if len(parts) > 2 else "False",
            })
    return pods


def get_hpa_events():
    stdout, _ = run_kubectl([
        "get", "events",
        "--field-selector", "involvedObject.name=webapp-hpa",
        "--sort-by", ".lastTimestamp",
        "-o", "jsonpath={range .items[-5:]}{.lastTimestamp},{.reason},{.message}\\n{end}"
    ])
    events = []
    for line in stdout.splitlines():
        parts = line.strip().split(",", 2)
        if len(parts) == 3:
            events.append({
                "time":    parts[0],
                "reason":  parts[1],
                "message": parts[2],
            })
    return list(reversed(events))


def get_kubectl_raw():
    stdout, stderr = run_kubectl(["get", "hpa", "webapp-hpa", "-o", "wide"])
    return stdout or stderr


def get_top_pods():
    stdout, stderr = run_kubectl(["top", "pods", "-l", "app=webapp", "--no-headers"])
    return stdout or stderr or "(metrics-server not ready yet — wait 60s)"



_load_proc = None

def start_load():
    global _load_proc
    if _load_proc and _load_proc.poll() is None:
        return "Load generator already running"
    try:
        run_kubectl(["delete", "pod", "load-gen", "--ignore-not-found=true"])
        _load_proc = subprocess.Popen(
            ["kubectl", "run", "load-gen", "--image=busybox", "--restart=Never",
             "--", "/bin/sh", "-c",
             "while true; do wget -q -O- http://webapp-service/ 2>/dev/null; done"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        STATE["load_active"] = True
        return "Load generator pod started — CPU will rise in ~30s"
    except Exception as e:
        return f"Failed to start load: {e}"


def stop_load():
    global _load_proc
    try:
        run_kubectl(["delete", "pod", "load-gen", "--ignore-not-found=true"])
        if _load_proc:
            _load_proc.terminate()
            _load_proc = None
        STATE["load_active"] = False
        return "Load generator stopped — pods will scale down in ~30s"
    except Exception as e:
        return f"Failed to stop load: {e}"



async def poll_metrics():
    while True:
        try:
            hpa  = get_hpa_metrics()
            pods = get_pod_list()
            evts = get_hpa_events()
            raw  = get_kubectl_raw()
            top  = get_top_pods()

            STATE.update({
                "timestamp":   datetime.utcnow().isoformat() + "Z",
                "pod_list":    pods,
                "events":      evts,
                "kubectl_raw": raw,
                "top_raw":     top,
                "error":       hpa.get("error"),
            })

            if not hpa.get("error"):
                STATE.update({
                    "cpu_current":  hpa["cpu_current"],
                    "cpu_target":   hpa["cpu_target"],
                    "pods_current": hpa["pods_current"],
                    "pods_desired": hpa["pods_desired"],
                    "pods_min":     hpa["pods_min"],
                    "pods_max":     hpa["pods_max"],
                })

            payload = json.dumps({"type": "metrics", "data": STATE})
            dead = set()
            for ws in clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            clients.difference_update(dead)

        except Exception as e:
            print(f"[poll error] {e}")

        await asyncio.sleep(3)


@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_metrics())



@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "metrics", "data": STATE}))
    try:
        while True:
            msg  = await ws.receive_text()
            data = json.loads(msg)
            if data.get("action") == "start_load":
                result = start_load()
                await ws.send_text(json.dumps({"type": "action_result", "message": result}))
            elif data.get("action") == "stop_load":
                result = stop_load()
                await ws.send_text(json.dumps({"type": "action_result", "message": result}))
    except WebSocketDisconnect:
        clients.discard(ws)



@app.get("/api/metrics")
def rest_metrics():
    return STATE

@app.get("/api/pods")
def rest_pods():
    return get_pod_list()

@app.get("/api/events")
def rest_events():
    return get_hpa_events()

@app.post("/api/load/start")
def rest_load_start():
    return {"message": start_load()}

@app.post("/api/load/stop")
def rest_load_stop():
    return {"message": stop_load()}


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")
