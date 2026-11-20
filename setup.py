#!/usr/bin/env python3
"""Inspects NVIDIA GPU VRAM via nvidia-smi, selects a compatible roleplay-capable
Ollama model, writes a tailored Modelfile, and optionally builds the model.
"""

import argparse
import csv
import io
import shutil
import subprocess
import sys
from pathlib import Path

MODEL_NAME = "sentient-gpu"
MODELFILE_PATH = Path(__file__).resolve().parent / "Modelfile"

# Ordered from highest capability to lowest.
# All candidates are instruction-tuned models with strong system-prompt adherence
# and clean multi-turn few-shot template behavior.
MODEL_TIERS = [
    {
        "min_vram_mib": 22000,
        "tag": "qwen2.5:32b-instruct",
        "num_ctx": 8192,
        "temperature": 0.88,
        "min_p": 0.04,
        "repeat_penalty": 1.1,
        "notes": "24GB+ tier: sharp wit, strong instruction following, zero PCIe spill.",
    },
    {
        "min_vram_mib": 14000,
        "tag": "mistral-nemo:12b",
        "num_ctx": 8192,
        "temperature": 0.88,
        "min_p": 0.04,
        "repeat_penalty": 1.1,
        "notes": "16GB tier: 12B Mistral Nemo with full 8K context headroom.",
    },
    {
        "min_vram_mib": 7500,
        "tag": "mistral-nemo:12b",
        "num_ctx": 4096,
        "temperature": 0.88,
        "min_p": 0.04,
        "repeat_penalty": 1.1,
        "notes": "8–12GB tier: 12B Mistral Nemo tuned to 4K context to stay inside VRAM.",
    },
    {
        "min_vram_mib": 5500,
        "tag": "mistral:7b-instruct",
        "num_ctx": 4096,
        "temperature": 0.88,
        "min_p": 0.04,
        "repeat_penalty": 1.1,
        "notes": "6GB tier: compact 7B instruct model that respects strict roleplay prompts.",
    },
    {
        "min_vram_mib": 0,
        "tag": "llama3.2:3b",
        "num_ctx": 4096,
        "temperature": 0.88,
        "min_p": 0.04,
        "repeat_penalty": 1.1,
        "notes": "Entry/4GB tier: lightweight 3B instruct model to avoid CPU offload.",
    },
]


def query_gpus() -> list[dict[str, str]]:
    if not shutil.which("nvidia-smi"):
        raise RuntimeError("nvidia-smi not found in PATH. Ensure NVIDIA drivers are installed.")

    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.free",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {(result.stderr or result.stdout).strip()}")

    gpus = []
    reader = csv.reader(io.StringIO(result.stdout.strip()), skipinitialspace=True)
    for row in reader:
        if len(row) == 4:
            gpus.append(
                {
                    "index": row[0],
                    "name": row[1],
                    "memory_total": row[2],
                    "memory_free": row[3],
                }
            )
    if not gpus:
        raise RuntimeError("No NVIDIA GPUs detected by nvidia-smi.")
    return gpus


def parse_mib(value: str) -> int:
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else 0


def select_model(total_vram_mib: int) -> dict:
    for tier in MODEL_TIERS:
        if total_vram_mib >= tier["min_vram_mib"]:
            return tier
    return MODEL_TIERS[-1]


def write_modelfile(tier: dict, path: Path) -> None:
    content = (
        f"FROM {tier['tag']}\n\n"
        f"PARAMETER num_ctx {tier['num_ctx']}\n"
        f"PARAMETER temperature {tier['temperature']}\n"
        f"PARAMETER min_p {tier['min_p']}\n"
        f"PARAMETER repeat_penalty {tier['repeat_penalty']}\n"
    )
    path.write_text(content, encoding="utf-8")


def build_ollama_model(tag: str, model_name: str, modelfile_path: Path) -> None:
    if not shutil.which("ollama"):
        raise RuntimeError("ollama CLI not found in PATH. Install Ollama first.")

    print(f"\n[1/2] Pulling base model '{tag}'...")
    subprocess.run(["ollama", "pull", tag], check=True)

    print(f"\n[2/2] Creating '{model_name}' from {modelfile_path.name}...")
    subprocess.run(
        ["ollama", "create", model_name, "-f", str(modelfile_path)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect GPU VRAM and generate a matching Ollama Modelfile."
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Automatically run 'ollama pull' and 'ollama create sentient-gpu'.",
    )
    parser.add_argument(
        "--model-name",
        default=MODEL_NAME,
        help=f"Target Ollama model name (default: {MODEL_NAME}).",
    )
    args = parser.parse_args()

    try:
        gpus = query_gpus()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_vram_mib = sum(parse_mib(g["memory_total"]) for g in gpus)
    primary = gpus[0]

    print("Detected NVIDIA Hardware:")
    for gpu in gpus:
        print(
            f"  [GPU {gpu['index']}] {gpu['name']} — "
            f"Total VRAM: {gpu['memory_total']} (Free: {gpu['memory_free']})"
        )

    if len(gpus) > 1:
        print(f"\nCombined multi-GPU VRAM pool: {total_vram_mib} MiB")

    selected = select_model(total_vram_mib)
    write_modelfile(selected, MODELFILE_PATH)

    print("\nSelected Configuration:")
    print(f"  Base Model : {selected['tag']}")
    print(f"  Context    : {selected['num_ctx']} tokens")
    print(f"  Reason     : {selected['notes']}")
    print(f"  Written to : {MODELFILE_PATH}")

    if args.build:
        try:
            build_ollama_model(selected["tag"], args.model_name, MODELFILE_PATH)
            print(f"\nDone! Run with: OLLAMA_HOST=http://localhost:11435 ollama run {args.model_name}")
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"\nBuild failed: {exc}", file=sys.stderr)
            return 1
    else:
        print("\nNext steps:")
        print(f"  ollama pull {selected['tag']}")
        print(f"  ollama create {args.model_name} -f Modelfile")
        print("  (Or re-run with: python setup.py --build)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())