<div align="center">

# EdenLive

**EdenLive: Real-Time Generative Rendering from Any Visual Reference**

[![Project Page](https://img.shields.io/badge/Project-Page-blue)](https://vast-eden.ai/?p=edenlive)
[![Model](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Models-yellow)](https://huggingface.co/VAST-AI/EdenLive)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

[Project Page](https://vast-eden.ai/?p=edenlive) · [Code](https://github.com/VAST-AI-Research/EdenLive) · [Models](https://huggingface.co/VAST-AI/EdenLive) · [Paper](https://assets.vast-eden.ai/assets/edenlive/EdenLive-tech-report.pdf)

Zehuan Huang<sup>1,\*</sup>, Tianxing Xu<sup>1,\*</sup>, Chunli Peng<sup>1,\*</sup>, Cheng Zhang<sup>1,\*</sup>, Zeyi Zheng<sup>1,†</sup>, Yifei Song<sup>1,†</sup>, Zekai Gu<sup>1,†</sup>, Zhipeng Yu<sup>1</sup>, Peng Wang<sup>1,‡</sup>, Yanpei Cao<sup>1,✉</sup>

\* Equal contribution · † Intern at VAST · ‡ Project lead · ✉ Corresponding author

<sup>1</sup>[VAST](https://www.tripo3d.ai/) · [Project Eden](https://www.tripo3d.ai/research/project-eden)

</div>

**TL;DR:**
* For interactive world models, we decouple state evolution (maintained by coding agents or engines) and appearance (rendered by EdenLive).
* EdenLive renders reference videos to enhanced videos in real time at 24 fps or higher, even on a consumer GPU (RTX 5090).

https://github.com/user-attachments/assets/243f1392-b41e-4ee6-a9c0-f5be8263bbec


## Overview

EdenLive is a real-time generative renderer that treats the
reference video as the interface between an interactive program and neural visual synthesis: the upstream program executes the rules and maintains the world state, whereas EdenLive renders the resulting reference video, guided by a target first frame and a text prompt, into detailed frames.

The [project page](https://vast-eden.ai/?p=edenlive) demonstrates over 24 FPS, minute-scale generation, and 480P/720P output. The 5B model supports single-GPU deployment at 720P.

We have open-sourced:

- [x] **5B and 14B model weights**, for both bidirectional and few-step (chunk-AR) inference.
- [x] **Real-time streaming inference code**.
- [x] **[EdenGolem（v0.1）](realtime/skills/edengolem/) skill that turns a text prompt into a coarse playable game.**
- [x] **A frontend to try the full prompt-to-game pipeline.**

## Installation

One Python environment runs both the offline inference in `edenlive/` and the
real-time service in `realtime/`.

| | Verified version |
| --- | --- |
| OS / GPU | Linux x86-64, NVIDIA GPUs with 80 GB (CUDA 13 driver) |
| Python | 3.10 |
| PyTorch | `2.14.0.dev20260717+cu130` nightly (`realtime/requirements.txt` pins `2.13.0`, cu130), torchvision 0.28.0 |
| diffusers / transformers / accelerate | 0.39.0 / 5.12.1 / 1.0.1 |
| decord / pandas / pyarrow | 0.6.0 / 2.3.3 / 25.0.1 |
| imageio / imageio-ffmpeg | 2.37.4 / 0.6.0 |
| Node.js | 22 (only to fetch three.js for the game renders) |

```bash
git clone https://github.com/VAST-AI-Research/EdenLive.git EdenLive
cd EdenLive

conda create -n edenlive python=3.10 -y
conda activate edenlive

# 1. PyTorch for a CUDA 13 driver (use .../whl/cu128 for CUDA 12.8).
pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu130

# 2. The real-time service and its pinned dependencies.
cd realtime
pip install --no-deps -e ./flashdreams -e ./apps/cam2v -e ./integrations_v2/eden
pip install -r requirements.txt
python -m playwright install chromium
npm install --prefix games
cd ..

# 3. Offline inference and video I/O dependencies.
pip install av==17.1.0 decord==0.6.0 pandas==2.3.3 pyarrow==25.0.1 imageio==2.37.4 imageio-ffmpeg==0.6.0
pip install --no-deps -e .
```

The default 5B bidirectional profile uses FA4 temporal text
attention and requires SM90, BF16, `flash-attn-4==4.0.0b31`, and
`nvidia-cutlass-dsl==4.7.1`. Use `--no-deps` when installing kernels into an
existing environment to preserve PyTorch. See [`docs/zh/wan22-fa4.md`](docs/zh/wan22-fa4.md).

## Model weights

```bash
hf download VAST-AI/EdenLive --local-dir ./models/EdenLive
```

Everything loads from one directory, `models/EdenLive` under the repository root by
default (about 210 GB in total).

```
models/EdenLive/
  dit-14b/            diffusion_pytorch_model.safetensors, config.json  14B few-step (chunk-AR) DiT
  dit-14b-bidir/      diffusion_pytorch_model.safetensors, config.json  14B bidirectional DiT
  dit-5b/             diffusion_pytorch_model.safetensors, config.json  5B few-step (chunk-AR) DiT
  dit-5b-bidir/       diffusion_pytorch_model.safetensors, config.json  5B bidirectional DiT
  vae-14b/            Wan2.1_VAE.pth, config.json
  vae-5b/             Wan2.2_VAE.pth, config.json
  decoder-14b/        lighttaew2_1.pth                    TAEHV decoder (real-time only)
  decoder-5b/         lighttaew2_2.pth                    TAEHV decoder (real-time only)
  scheduler-14b/      scheduler_config.json               offline only
  scheduler-5b/       scheduler_config.json               offline only
  umt5/               text_encoder/, tokenizer/           UMT5 text encoder
  clip/               image_encoder/, image_processor/    CLIP, 14B I2V only
  vision-encoder/     backbone/, projection.safetensors   proxy-DINO encoder
  flux-c2f/           Diffusers FLUX tree + c2f_lora.safetensors   workbench first frame (real-time only)
```

The default weight directory is `models/EdenLive`, relative to the working directory.
To specify another directory:

```bash
python -m edenlive.scripts.infer --config ... model.pretrained_dir=/data/EdenLive
```

The real-time service reads the same tree through `MODEL_ROOT` in
[`realtime/deploy.conf`](realtime/deploy.conf).

### Quick start

Two proxy recordings ship with the repo under `examples/samples/`, with a manifest that
points at them:

```
examples/samples/
  manifest.csv        id, prompt, proxy_video, first_frame
  cryo-crossing/       cover.png, first-frame.png, prompts.json, prompts.txt, recording.mp4
  pulse-skirmish/      same five files
```

`recording.mp4` is the greybox proxy (1280x704), and `first-frame.png` is its first frame.
`cover.png` is a photoreal render of that frame; the manifest's `first_frame` points to this file.
`prompts.json` contains `staticPrompt` and timestamped `dynamicPrompts`;
`prompts.txt` is the corresponding text version.

| Mode | Wan2.1-I2V-14B @ 480p | Wan2.2-TI2V-5B @ 720p |
| --- | --- | --- |
| Bidirectional | `wan21_14b_480p_bidir.yaml` | `wan22_5b_720p_bidir.yaml` |
| Few-step (chunk-AR) | `wan21_14b_480p_few_step.yaml` | `wan22_5b_720p_few_step.yaml` |

**Bidirectional inference**: all frames are denoised jointly.

```bash
torchrun --nproc_per_node=2 -m edenlive.scripts.infer \
  --config edenlive/configs/wan21_14b_480p_bidir.yaml \
  runtime.sequence_parallel.size=2
```

```bash
python -m edenlive.scripts.infer \
  --config edenlive/configs/wan22_5b_720p_bidir.yaml
```

**Few-step inference**: chunk-autoregressive sampling, 4 steps per chunk.

```bash
torchrun --nproc_per_node=2 -m edenlive.scripts.infer \
  --config edenlive/configs/wan21_14b_480p_few_step.yaml \
  runtime.sequence_parallel.size=2
```

```bash
python -m edenlive.scripts.infer \
  --config edenlive/configs/wan22_5b_720p_few_step.yaml
```

**Real-time service**: uses game frames as input and streams video over WebRTC.
Configure the weight directory, service addresses, ports, and GPU count in
[`realtime/deploy.conf`](realtime/deploy.conf).
5B uses one GPU by default. Set `NPROC` for multi-GPU inference.

Set `TURN_HOST` (the relay address reachable by the browser), `TURN_IFACE`
(the relay interface), and `TURN_SECRET` (your own password) in
`realtime/deploy.conf`. To run existing games, execute on the GPU host:

```bash
cd realtime
apt-get install coturn  # First-time installation
bash turn_server.sh    # Start TURN first
bash start.sh 14b       # Model service; or use 5b
```

The workbench — a game library, a chat that builds new games from a text description,
and a play window — needs all four services. Its first-frame renderer (the FLUX worker)
runs FLUX.2 \[klein\] 9B under the FLUX Non-Commercial License, so the workbench is
**for non-commercial use only**, and that license requires content filtering on the
images it generates (see [License](#license)). Playing registered games with `start.sh`
does not load FLUX.

The bridge host requires the `claude` CLI, `claude auth login`, and first-run trust confirmation.
Game generation uses `--dangerously-skip-permissions`.
For separate hosts, copy the bridge's `.workbench/token` to the GPU host's `realtime/`
directory and set `BRIDGE_HOST`; leave it empty for a single-host deployment.

```bash
cd realtime

# 1. TURN relay, GPU host; skip if already running
bash turn_server.sh

# 2. Workbench bridge, Claude CLI host
bash workbench_bridge.sh

# 3. FLUX first-frame service, GPU host
bash workbench_flux.sh

# 4. Model service, GPU host
bash launch.sh

# Stop services on this host
bash stop.sh
```

Open `http://<host>:8089` (`APP_PORT` in `deploy.conf`).

| Model | Application slug |
| --- | --- |
| 5B | `game2v-eden-world-v2-5b-taehv` |
| 14B | `game2v-eden-world-v2-14b-taehv` |

The real-time service uses the TAEHV decoder. See [`realtime/README.md`](realtime/README.md)
for the service topology and developer documentation.

## Inference with Reactor Runtime

The [Reactor integration](reactor/README.md) serves EdenLive's four-step 5B pipeline
with synchronized draft and generated video, keyboard controls, live prompt updates,
and a browser demo. It runs on one B200 and keeps the native inference and training
entrypoints unchanged.

Install the [Reactor CLI](https://docs.reactor.inc/deploy) and prepare
`EDEN_BUNDLE` following [the source and weights instructions](reactor/README.md#1-prepare-the-source-and-weights).
The bundle uses a pinned seven-game source snapshot; the current repository's game
registry has different entries.

```sh
cd reactor
reactor build
reactor run --gpus device=0 --shm-size 2g --weights "$EDEN_BUNDLE" --port 8080
```

Wait for `curl http://localhost:8080/health` to report `"state":"available"`.
Then, in a second terminal from the repository root:

```sh
cd reactor/demo
npm ci
VITE_REACTOR_URL=http://localhost:8080 npm run dev
```

Open the URL printed by Vite, select **Local**, click **Connect**, choose a game
and reference, then press **Start**. **Update prompt** changes upcoming appearance
while keeping the video history. See the [Reactor guide](reactor/README.md) for
playback controls and SSH/TURN networking.

## Your own data

The default profiles read a recording manifest (CSV/JSONL/JSON/parquet), one
recording per request. Each directory contains `recording.mp4`, `cover.png`,
and `prompts.json`; text comes from `staticPrompt` and `dynamicPrompts`:

```csv
id,proxy_video,first_frame
shot_001,shot_001/recording.mp4,shot_001/cover.png
```

```bash
python -m edenlive.scripts.infer --config CONFIG.yaml \
  data.dataset.params.path=/data/my_manifest.csv \
  data.dataset.params.root=/data
```

## Real-Time Inference with LightX2V on RTX 5090

[LightX2V](https://github.com/ModelTC/LightX2V) provides accelerated inference for EdenLive's 14B and 5B models in both bidirectional and four-step autoregressive modes. Optimizations include low-bit quantization, efficient kernels and operator fusion, KV cache optimizations, `torch.compile`, CUDA Graphs, and streaming LightTAE decoding.

**Real-time inference on a single RTX 5090:** EdenLive 5B achieves approximately **24.7 FPS inference throughput at 1280×704**, meeting the throughput requirement for **real-time 24 FPS playback**.

See the [EdenLive deployment guide](https://github.com/ModelTC/LightX2V/blob/main/scripts/edenlive/README.md) for environment setup, model conversion, quantization, and launch commands.

## Citation

```bibtex
@misc{edenlive2026,
  title  = {EdenLive: Real-Time Generative Rendering from Any Visual Reference},
  author = {Zehuan Huang and Tianxing Xu and Chunli Peng and Cheng Zhang and Zeyi Zheng and Yifei Song and Zekai Gu and Zhipeng Yu and Peng Wang and Yanpei Cao},
  year   = {2026},
  url    = {https://vast-eden.ai/?p=edenlive}
}
```

## Acknowledgements

Built on [Wan2.1 / Wan2.2](https://github.com/Wan-Video),
🤗 [Diffusers](https://github.com/huggingface/diffusers),
[DINOv3](https://github.com/facebookresearch/dinov3),
[FLUX.2 \[klein\]](https://bfl.ai/models/flux-2-klein) by Black Forest Labs, and the
[LightX2V](https://huggingface.co/lightx2v/Autoencoders) LightTAE decoders. The game
renders use [Three.js](https://threejs.org), and the workbench's game-generation skill
builds on [GameBlocks](https://github.com/xt4d/GameBlocks).

The real-time streaming service under `realtime/` builds on
[NVIDIA FlashDreams](https://github.com/NVIDIA/flashdreams).

## License

First-party code is licensed under Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
`realtime/` is also Apache 2.0 and keeps its own [LICENSE](realtime/LICENSE),
[NOTICE](realtime/NOTICE) and [THIRD-PARTY-NOTICES](realtime/THIRD-PARTY-NOTICES).
The vendored html-to-image bundle and GameBlocks-derived skill modules retain
their MIT licenses and original copyright notices; per-file scope is recorded
in [REUSE.toml](realtime/REUSE.toml).

The model weights are distributed separately and are **not** all Apache 2.0:

| Component | License |
| --- | --- |
| `dit-*`, `vae-*`, `umt5/`, `scheduler-*`, `decoder-*` | Apache 2.0 |
| `clip/` (LAION CLIP ViT-H/14 / OpenCLIP) | [MIT](https://huggingface.co/laion/CLIP-ViT-H-14-laion2B-s32B-b79K) |
| `vision-encoder/` (DINOv3 and derivatives) | [DINOv3 License](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md) |
| `flux-c2f/` (FLUX.2 \[klein\] 9B plus our LoRA; workbench first frame only) | [FLUX Non-Commercial License](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/blob/main/LICENSE.md) — non-commercial, non-production use only |

Each component ships its own license and notice files alongside the weights; see the model
repository's README for details.
