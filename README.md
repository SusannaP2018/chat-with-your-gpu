
# Chat with your GPU

**Converse with your GPU and get live metrics embedded in copious amounts of snark.**

*A lightweight FastAPI proxy that sits between **Open WebUI** (or the Ollama CLI) and **Ollama**, injecting live `nvidia-smi` telemetry and static GPU hardware identity into every chat turn—turning your graphics card into a sentient, sharp-tongued assistant that complains about running quantized LLMs in real time.*

## Prerequisites

* NVIDIA GPU with working `nvidia-smi` drivers
* [Ollama](https://ollama.com/) running locally on port `11434`
* Python 3.10+

## Quick Start

1. **Clone the repository:**
```bash
git clone [https://github.com/](https://github.com/)<your-username>/sentient-gpu-proxy.git
cd chat-with-your-gpu
```

2. **Install Python dependencies:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. **Detect GPU VRAM, generate `Modelfile`, and build the model:**
```bash
python setup.py --build
```
*(Or run `python setup.py` first if you want to inspect the generated `Modelfile` before pulling weights and running `ollama create`.)*

4. **(Optional) Configure custom hardware notes:**
```bash
cp .env.example .env
```

5. **Start the telemetry proxy:**
```bash
uvicorn proxy:app --host 0.0.0.0 --port 11435
```

## Connecting a Client

* **Open WebUI (Docker):**
Ensure your container is started with `--add-host=host.docker.internal:host-gateway`, then set your Ollama API Base URL in **Admin Settings → Connections** to:
`http://host.docker.internal:11435`


* **Ollama CLI:**
```bash
OLLAMA_HOST=http://localhost:11435 
ollama run sentient-gpu
```

## Customization

* **Custom Cooler / AIB Edition:** Set `GPU_HARDWARE_NOTE` in your environment (e.g., `GPU_HARDWARE_NOTE="EVGA FTW3 Ultra"`) to inject custom board details into the hardware profile.

## Quirks

* **The model acts weird/only gives generic responses in Open-WebUI:** This is a bug in OpenWebUI. In Admin panel > Models, uncheck *Builtin tools*.