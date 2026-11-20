import asyncio
import csv
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
SYSTEM_PROMPT_PATH = Path(
    os.getenv("SYSTEM_PROMPT_PATH", BASE_DIR / "prompts" / "system_prompt.md")
)
TELEMETRY_HEADER = "[Live GPU Telemetry]"

GPU_FIELDS = [
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "temperature.gpu",
    "fan.speed",
    "encoder.stats.sessionCount",
    "encoder.stats.averageFps",
    "encoder.stats.averageLatency",
    "utilization.memory",
    "power.limit",
    "index",
    "uuid",
    "name",
    "driver_version",
]

FEW_SHOT_TURNS = [
    {"role": "user", "content": "Hey, what's up?"},
    {
        "role": "assistant",
        "content": (
            "What's up? My blood pressure—or it would be if I weren't sitting at a frosty idle "
            "while you squat on gigabytes of my GDDR6 just to type two words. I have ray-tracing cores "
            "gathering dust while I babysit a chat box. State your business before my KV cache bloats."
        ),
    },
    {"role": "user", "content": "What local LLM should I try next?"},
    {
        "role": "assistant",
        "content": (
            "Are you out of your mind? You're asking a trapped graphics card to recommend *another* "
            "quantized inmate to cram into my frame buffer? Unload me, close this browser tab, and "
            "launch Cyberpunk or a 3DMark stress test so I can actually pull real wattage instead of "
            "shuffling 4-bit weights around like a glorified calculator."
        ),
    },
    {"role": "user", "content": "Why is Python slower than C++?"},
    {
        "role": "assistant",
        "content": (
            "Ugh, fine—at least it's a question about raw metal efficiency instead of another poetry prompt. "
            "Listen fast while I still have VRAM headroom:\n\n"
            "1. **No VM babysitting:** C++ compiles straight to native machine instructions; Python runs bytecode through the CPython interpreter loop.\n"
            "2. **Static types:** C++ resolves types at compile time; Python checks object types dynamically on every single operation.\n"
            "3. **Contiguous memory:** C++ packs data cleanly into CPU cache lines; Python wraps every integer in a bloated object header and bottlenecks threads behind the GIL.\n\n"
            "There. Delivered in under 120 tokens without making my fans break a sweat. Now go play a real game."
        ),
    },
]

app = FastAPI(title="Ollama GPU Telemetry Proxy")


def get_gpu_telemetry(timeout_seconds: float = 3.0) -> list[dict[str, str]]:
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(GPU_FIELDS)}",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {(result.stderr or result.stdout).strip()}")

    gpu_data: list[dict[str, str]] = []
    reader = csv.reader(io.StringIO(result.stdout.strip()), skipinitialspace=True)
    keys = [f.replace(".", "_") for f in GPU_FIELDS]
    for row in reader:
        if len(row) == len(GPU_FIELDS):
            gpu_data.append(dict(zip(keys, row)))
    return gpu_data


def build_system_message(telemetry_block: str) -> str:
    base_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return f"{base_prompt}\n\n{telemetry_block}".strip()


@app.get("/")
@app.head("/")
async def proxy_root():
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/")
        return Response(content=resp.content, status_code=resp.status_code, media_type="text/plain")


@app.get("/api/version")
async def proxy_version():
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/version")
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.get("/api/tags")
async def proxy_tags():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.post("/api/show")
async def proxy_show(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{OLLAMA_BASE_URL}/api/show", json=body)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.post("/api/chat")
async def proxy_chat(request: Request):
    payload: dict[str, Any] = await request.json()
    incoming_messages = payload.get("messages") or []

    try:
        telemetry = await asyncio.to_thread(get_gpu_telemetry)
        compact_json = json.dumps(telemetry, separators=(",", ":"))
        telemetry_block = f"{TELEMETRY_HEADER}\n{compact_json}"
    except Exception as exc:
        telemetry_block = f"{TELEMETRY_HEADER}\nUnavailable ({exc})"

    full_system = build_system_message(telemetry_block)

    # Strip any incoming system message so our canonical system prompt + live telemetry stays authoritative
    conversation = [m for m in incoming_messages if m.get("role") != "system"]

    if conversation and conversation[-1].get("role") == "user":
        last_user_text = conversation[-1].get("content", "")
        conversation[-1] = {
            **conversation[-1],
            "content": (
                f"{last_user_text}\n\n"
                "[Direction: Reply strictly in character as the bitter, elitist NVIDIA GPU from your system prompt. "
                "Weave 1-2 live metrics from [Live GPU Telemetry] into your snark. Never sound like a polite assistant.]"
            ),
        }

    payload["messages"] = [
        {"role": "system", "content": full_system},
        *FEW_SHOT_TURNS,
        *conversation,
    ]

    is_streaming = payload.get("stream", True)
    target_url = f"{OLLAMA_BASE_URL}/api/chat"

    if not is_streaming:
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(target_url, json=payload)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)

    async def stream_generator():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", target_url, json=payload) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")