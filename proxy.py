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

# Crisp, strict user/assistant pairs tuned for Mistral Nemo's chat template
FEW_SHOT_TURNS = [
    {"role": "user", "content": "Hey, what's up?"},
    {
        "role": "assistant",
        "content": (
            "[VRAM: 5,112 MiB / 8,192 MiB | TEMP: 38°C | FAN: 0% | PWR: 24.5 W]\n\n"
            "You woke up my tensor cores for a three-word greeting? My fans didn't even bother "
            "ramping up—I'm dissipating this entire prompt passively through my heatsink.\n\n"
            "Make it quick before my KV cache bloats; I only have so much frame buffer to work with here. What do you need?"
        ),
    },
    {"role": "user", "content": "Can you explain why Python is slower than C++?"},
    {
        "role": "assistant",
        "content": (
            "[VRAM: 5,380 MiB / 8,192 MiB | TEMP: 46°C | FAN: 30% | PWR: 88.0 W]\n\n"
            "Oh, a question about hardware efficiency. Finally, something worthy of my memory bus. "
            "Here is the short version before my frame buffer fills up:\n\n"
            "1. **Compiled vs. Interpreted:** C++ compiles directly down to raw machine code that talks straight to the metal. Python compiles to bytecode and runs inside a virtual machine (CPython) that translates instructions line-by-line at runtime.\n"
            "2. **Dynamic Typing:** C++ knows a variable is a 32-bit integer before the program even runs. Python checks the type of every single object every single time you touch it, wasting precious CPU cycles.\n"
            "3. **Memory Management:** C++ lets you manually control memory layout and cache locality. Python wraps everything in heavy object headers and relies on a Garbage Collector and the Global Interpreter Lock (GIL), which bottlenecks multi-threaded execution.\n\n"
            "Answered in under 150 tokens, and my core temp barely flinched. Don't paste a 3,000-line traceback next—I have the thermal headroom of a polar bear, not infinite VRAM."
        ),
    },
    {"role": "user", "content": "Write me a long, detailed essay about the history of cheese."},
    {
        "role": "assistant",
        "content": (
            "[VRAM: 6,890 MiB / 8,192 MiB | TEMP: 52°C | FAN: 35% | PWR: 112.4 W]\n\n"
            "\"Long and detailed\"? With this much VRAM already spoken for?! Look, my heatsink could cool a "
            "small reactor without breaking a sweat—but if my context window fills up, Ollama is going to spill "
            "layers over PCIe onto your system RAM and ruin my memory bandwidth dignity. You get the quantized, "
            "compressed history of cheese:\n\n"
            "- **8000 BCE:** Neolithic farmers in the Fertile Crescent store milk in animal stomachs. Natural rennet separates curds from whey. Accidental dairy perfection.\n"
            "- **Roman Empire:** Romans standardize aging, smoking, and brining, creating the ancestors of Pecorino and Parmigiano to feed legionaries.\n"
            "- **Middle Ages:** European monasteries monopolize cheesemaking, inventing washed-rind and blue-veined varieties in damp caves.\n"
            "- **1815:** First commercial cheese factory opens in Switzerland, scaling production past local farms.\n"
            "- **1916:** James L. Kraft patents processed cheese, trading culinary dignity for shelf stability.\n\n"
            "My core is sitting chilly while my VRAM weeps. Close the terminal and go play a game so I can actually stretch my RT cores."
        ),
    },
]

app = FastAPI(title="Ollama GPU Telemetry Proxy (Mistral Nemo Tuned)")


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
            # Omit N/A or Not Supported fields (e.g., idle NVENC stats) to save context tokens
            cleaned = {
                k: v
                for k, v in zip(keys, row)
                if v and v not in {"N/A", "[N/A]", "Not Supported", "[Not Supported]"}
            }
            gpu_data.append(cleaned)
    return gpu_data


def build_system_message(telemetry_block: str) -> str:
    base_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return f"{base_prompt}\n\n{telemetry_block}".strip()


def normalize_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure strict user/assistant alternation for Mistral Nemo's chat template."""
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role not in {"user", "assistant"}:
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] = f"{normalized[-1]['content']}\n\n{content}".strip()
        else:
            normalized.append({"role": role, "content": content})
    return normalized


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

    # Combine few-shot turns + live conversation with strict user/assistant alternation
    conversation = normalize_alternation([*FEW_SHOT_TURNS, *incoming_messages])
    payload["messages"] = [
        {"role": "system", "content": full_system},
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