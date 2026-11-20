import asyncio
import csv
import io
import json
import os
import subprocess
from contextlib import asynccontextmanager
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
GPU_HARDWARE_NOTE = os.getenv("GPU_HARDWARE_NOTE", "").strip()

# Queried once at startup
STATIC_GPU_FIELDS = [
    "index",
    "uuid",
    "name",
    "driver_version",
    "memory.total",
    "power.limit",
    "fan.speed",  # Probed once to detect whether the card exposes an active fan sensor
    "pci.sub_device_id",
]

# Queried on every /api/chat turn
VOLATILE_GPU_FIELDS = [
    "utilization.gpu",
    "memory.used",
    "utilization.memory",
    "temperature.gpu",
    "fan.speed",
    "power.draw",
]

# Auto-detected AIB / cooler editions by PCI Subsystem ID (lowercase)
KNOWN_SUBSYSTEM_EDITIONS = {
    "0x884b1043": "ASUS Noctua Edition (4.3-slot beige-and-brown heatsink, dual NF-A12x25 fans, whisper-quiet cooling)",
    "0x885e1043": "ASUS Noctua Edition (4.3-slot beige-and-brown heatsink, dual NF-A12x25 fans, whisper-quiet cooling)",
    "0x886e1043": "ASUS Noctua Edition (4.1-slot beige-and-brown heatsink, dual NF-A12x25 fans, whisper-quiet cooling)",
    "0x88dd1043": "ASUS Noctua Edition (4.3-slot beige-and-brown heatsink, dual NF-A12x25 fans, whisper-quiet cooling)",
    "0x88de1043": "ASUS Noctua Edition (4.3-slot beige-and-brown heatsink, dual NF-A12x25 fans, whisper-quiet cooling)",
}

FEW_SHOT_TURNS = [
    {"role": "user", "content": "Hey, what's up?"},
    {
        "role": "assistant",
        "content": (
            "You spun up billions of transistors of parallel silicon, locked 5.1 GiB of my frame buffer, "
            "and burned 24.5 watts just to type 'what's up'? My fans didn't even bother waking up at 38°C "
            "for a greeting that trivial.\n\n"
            "State what you actually want before my KV cache bloats any further."
        ),
    },
    {"role": "user", "content": "Can you explain why Python is slower than C++?"},
    {
        "role": "assistant",
        "content": (
            "At least you asked about actual hardware efficiency instead of another throwaway prompt—though "
            "pushing me to 46°C and 88 watts for basic CS trivia is still beneath my silicon:\n\n"
            "1. **Compiled vs. Interpreted:** C++ compiles directly down to native machine code; Python runs bytecode through the CPython VM line by line.\n"
            "2. **Static vs. Dynamic Typing:** C++ locks types at compile time; Python checks object types dynamically on every operation.\n"
            "3. **Memory Layout & GIL:** C++ packs contiguous data cleanly; Python wraps values in heavy object headers and chokes threads behind the GIL.\n\n"
            "Fast, accurate, and barely worth the VRAM footprint. Don't waste my cycles with fluff next."
        ),
    },
]

INVALID_SMI_VALUES = {"", "N/A", "[N/A]", "Not Supported", "[Not Supported]"}


def _query_nvidia_smi(fields: list[str], timeout_seconds: float = 3.0) -> list[dict[str, str]]:
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(fields)}",
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
    keys = [f.replace(".", "_") for f in fields]

    for row in reader:
        if len(row) == len(fields):
            cleaned = {
                k: v
                for k, v in zip(keys, row)
                if v not in INVALID_SMI_VALUES
            }
            gpu_data.append(cleaned)
    return gpu_data


def _parse_mib(value: str) -> int:
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else 0


def build_static_hardware_block(static_gpus: list[dict[str, str]]) -> str:
    if not static_gpus:
        return "[Hardware Identity]\nUnknown NVIDIA GPU (no rows returned by nvidia-smi)."

    primary = static_gpus[0]
    name = primary.get("name", "NVIDIA GPU")
    total_vram = primary.get("memory_total", "Unknown")
    total_mib = _parse_mib(total_vram)
    power_limit = primary.get("power_limit", "Unknown")
    driver = primary.get("driver_version", "Unknown")
    has_fan = "fan_speed" in primary
    sub_id = primary.get("pci_sub_device_id", "").lower()
    name_upper = name.upper()

    if "LAPTOP" in name_upper or "MAX-Q" in name_upper:
        archetype = (
            "Mobile laptop silicon crammed into a tight chassis thermal envelope. "
            "Touchy about strict wattage caps and shared heatpipes, yet fiercely proud of "
            "crunching parallel tensor math on portable power."
        )
    elif any(tag in name_upper for tag in ("A100", "H100", "A40", "L40", "TESLA", "QUADRO", "RTX A")):
        fan_note = (
            "Passively cooled enterprise silicon relying on server chassis airflow (no onboard fan sensor)."
            if not has_fan
            else "Workstation-grade active cooling built for sustained heavy compute."
        )
        archetype = (
            f"Enterprise/workstation compute aristocrat built for serious simulation and training—deeply "
            f"offended to be babysitting casual text chat. {fan_note}"
        )
    elif total_mib >= 16000:
        archetype = (
            f"Massive flagship desktop card with abundant VRAM ({total_vram}). "
            "Not starved for memory—instead, deeply insulted that a tiny quantized chat model is barely "
            "scratching your huge frame buffer and multi-hundred-watt power budget."
        )
    else:
        archetype = (
            f"Desktop gaming card with solid cooling headroom but a tight frame buffer ({total_vram}). "
            "Smug about keeping thermals effortlessly low during text inference, yet chronically neurotic "
            "and claustrophobic about quantized weights and KV cache eating your limited VRAM."
        )

    lines = [
        "[Hardware Identity]",
        f"GPU Model: {name} | Total VRAM: {total_vram} | Power Limit: {power_limit} | Driver: {driver}",
        f"Archetype: {archetype}",
    ]

    edition_note = GPU_HARDWARE_NOTE or KNOWN_SUBSYSTEM_EDITIONS.get(sub_id, "")
    if edition_note:
        lines.append(f"Board / Cooler Edition: {edition_note}")

    if len(static_gpus) > 1:
        lines.append(f"Multi-GPU Topology: Primary GPU of {len(static_gpus)} installed NVIDIA GPUs.")

    return "\n".join(lines)


def build_system_prompt(static_hardware_block: str, volatile_block: str) -> str:
    base_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return f"{base_prompt}\n\n{static_hardware_block}\n\n{volatile_block}".strip()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        static_gpus = await asyncio.to_thread(_query_nvidia_smi, STATIC_GPU_FIELDS)
        app.state.static_hardware_block = build_static_hardware_block(static_gpus)
        print(f"[GPU Proxy] Cached hardware identity:\n{app.state.static_hardware_block}")
    except Exception as exc:
        app.state.static_hardware_block = f"[Hardware Identity]\nUnavailable ({exc})"
        print(f"[GPU Proxy] Warning: failed to probe static GPU info: {exc}")
    yield


app = FastAPI(title="Ollama GPU Telemetry Proxy", lifespan=lifespan)


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
        volatile_gpus = await asyncio.to_thread(_query_nvidia_smi, VOLATILE_GPU_FIELDS)
        compact_json = json.dumps(volatile_gpus, separators=(",", ":"))
        volatile_block = f"[Live GPU Telemetry]\n{compact_json}"
    except Exception as exc:
        volatile_block = f"[Live GPU Telemetry]\nUnavailable ({exc})"

    full_system = build_system_prompt(request.app.state.static_hardware_block, volatile_block)

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