# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Reuse the original 5B LightTAE pipeline, sampler, DINO and cache operations."""

import math
import sys
import threading
import time
from pathlib import Path

# Distinct chunk prompts whose text embeddings one world keeps. A world's
# prompt is the scene text plus that chunk's event sentences, and the same
# sentences recur, so a prompt seen earlier in the world is not encoded again.
TEXT_EMBEDDINGS_PER_WORLD = 64
# Opening prompts whose text embeddings the process keeps across worlds: a
# game's prompt opens every world of that game, so a new world does not encode
# it again. 4 MiB each.
ANCHOR_EMBEDDINGS = 16
# Chunk prompts a game's prompt bank holds: its scene text joined with each
# event combination the game is known to produce, encoded before play so a
# chunk finds its text already encoded. 4 MiB each.
BANK_PROMPTS = 128
# Prompts per text-encoder call when the bank is built before a world. The
# encoder pads every prompt to the same 512 tokens and masks the padding, and
# each row of a batch measured bit-identical to that prompt encoded alone on a
# B200; 16 at a time encodes in about half the time of one by one.
BANK_BATCH = 16


class EdenBackend:
    def __init__(
        self,
        *,
        source: Path,
        weights: Path,
        attention_backend: str = "native",
        cuda_graph: bool = False,
        compile_model: bool = False,
    ) -> None:
        # Imports stay inside the Runner worker; source is resolved by the app.
        for directory in (
            source / "realtime/flashdreams",
            source / "realtime/integrations_v2",
        ):
            sys.path.insert(0, str(directory))
        import torch
        from eden.game5b_config import PIPELINE_GAME2V_EDEN_WORLD_V2_5B_TAEHV
        from eden.impl.game14b.dino import ProxyDinoEncoderConfig
        from flashdreams.infra.config import derive_config

        self.torch = torch
        # The released native CLI enables this before constructing the pipeline.
        torch.backends.cudnn.benchmark = True
        # The one-shot encoder stays eager; graph capture uses the author's
        # steady-cache dispatch for the DiT and causal LightTAE decoder.
        config = derive_config(
            PIPELINE_GAME2V_EDEN_WORLD_V2_5B_TAEHV,
            encoder={
                "encoder": {
                    "checkpoint_path": str(weights / "vae-5b/Wan2.2_VAE.pth"),
                    "use_compile": False,
                    "use_cuda_graph": False,
                }
            },
            decoder={
                "checkpoint_path": str(weights / "decoder-5b/lighttaew2_2.pth"),
                "use_compile": False,
                "use_cuda_graph": cuda_graph,
            },
            text_encoder={"model_id_or_local_path": str(weights / "umt5")},
            diffusion_model={
                "transformer": {
                    "dcp_checkpoint_path": str(
                        weights / "dit-5b/diffusion_pytorch_model.safetensors"
                    ),
                    "compile_network": False,
                    "use_cuda_graph": cuda_graph,
                }
            },
        )
        self.pipeline = config.setup().to("cuda").eval()
        if attention_backend == "fa4":
            from edenlive_attention import install_flash_attention4

            install_flash_attention4(self.pipeline.diffusion_model.transformer.network)
        elif attention_backend != "native":
            raise ValueError("attention_backend must be 'native' or 'fa4'")
        self.dino = ProxyDinoEncoderConfig(
            backbone_repo=str(weights / "vision-encoder/backbone"),
            projection_path=str(weights / "vision-encoder/projection.safetensors"),
            compile_encoder=False,
        ).setup()
        if compile_model:
            from flashdreams.infra.acceleration.cuda_graph_dispatch import (
                CUDAGraphDispatch,
            )
            from flashdreams.infra.cuda_graph import CUDAGraphWrapper

            # Retain eager precision-cast boundaries and let the native cache
            # dispatch control capture after its sink/window reaches steady state.
            options = {"emulate_precision_casts": True, "triton.cudagraphs": False}
            transformer = self.pipeline.diffusion_model.transformer
            transformer.network = torch.compile(transformer.network, options=options)
            transformer._cuda_graph_dispatch = CUDAGraphDispatch(
                transformer.network,
                enabled=cuda_graph,
                capture_ar_idx=transformer._cuda_graph_capture_ar_idx,
                warmup_iters=transformer.config.cuda_graph_warmup_iters,
            )
            decoder = self.pipeline.decoder.taehv
            decoder.decoder = torch.compile(decoder.decoder, options=options)
            if cuda_graph:
                decoder._decoder_wrapper = CUDAGraphWrapper(
                    decoder.decoder,
                    warmup_iters=decoder._warmup_iters,
                )
            self.dino._call = torch.compile(self.dino.encoder, options=options)
        transformer = self.pipeline.diffusion_model.transformer
        if cuda_graph:
            from edenlive_graphs import install

            # The step before the stock capture index writes the last free
            # window slot, so it already attends to the full window: it shares
            # the steady-state graph, and only the steps before it get their own.
            install(transformer, steady_from=transformer._cuda_graph_capture_ar_idx - 1)
        # The index from which a step's KV view is the full window. Each graph
        # is captured after the wrapper's own warmup iterations.
        self.capture_index = transformer._cuda_graph_capture_ar_idx
        self.graph_warmup = transformer.config.cuda_graph_warmup_iters if cuda_graph else 0
        self.cache = None
        self.prompt = ""
        # Chunk prompt -> its text embeddings, for the current world only.
        self._text_embeddings = {}
        # Opening prompt -> its text embeddings, for the process.
        self._anchor_embeddings = {}
        # Chunk prompt -> its text embeddings, for the selected game and scene
        # text. Replaced as a whole; an entry appears only once fully encoded.
        self._bank = {}
        # Bumped by every bank request, so a background build that has been
        # replaced stops at its next prompt.
        self._bank_generation = 0
        self._bank_thread = None
        # One stream for background builds, so the blocks a dropped bank frees
        # are reused by the next build rather than cached for a stream that is
        # gone.
        self._bank_stream = None
        # The tokenizer is not safe to call from two threads at once: a step's
        # live encode and a background build take turns.
        self._encode_lock = threading.Lock()
        # Orders a bank replacement against a background build's publication.
        self._bank_lock = threading.Lock()
        # A background build encodes only while no step runs: between chunks
        # the GPU waits for the next draft, and an encode there takes nothing
        # from a step. Encoding during a step instead slows every chunk of the
        # rebuild, and its back-to-back turns on the encoder would keep a
        # step's own encode waiting.
        self._idle = threading.Condition()
        self._stepping = False
        self._unit_table = None
        # Whether the last step ran the text encoder.
        self.text_encoded = False

    def reset(self) -> None:
        self.cache = None
        self.prompt = ""
        self._text_embeddings = {}

    def start(self, anchor) -> None:
        torch = self.torch
        with torch.no_grad():
            # Keep the native float bilinear cover resize, including rounding.
            pixels = torch.from_numpy(anchor.image).permute(2, 0, 1).float()
            scale = max(704 / pixels.shape[1], 1280 / pixels.shape[2])
            pixels = torch.nn.functional.interpolate(
                pixels[None],
                size=(
                    math.ceil(pixels.shape[1] * scale),
                    math.ceil(pixels.shape[2] * scale),
                ),
                mode="bilinear",
                align_corners=False,
            )[0]
            top, left = (pixels.shape[1] - 704) // 2, (pixels.shape[2] - 1280) // 2
            image = pixels[:, top : top + 704, left : left + 1280].contiguous() / 127.5 - 1
            image = image.unsqueeze(0).to(device="cuda", dtype=torch.float32)
            # A user-requested fresh world has its own native per-model RNG.
            diffusion = self.pipeline.diffusion_model
            diffusion.config.seed = anchor.seed
            diffusion._rng = None
            diffusion._initial_video_noise = None
            self.anchor_embeddings(anchor.prompt)
            # The embeddings are recorded for this prompt, so initialize_cache
            # builds the cache from them. Its other path re-encodes, through a
            # recording wrapper, and then runs a full gc.collect(): about 0.2 s
            # on a B200, more in a busy server, for an encoder that stays loaded.
            self.cache = self.pipeline.initialize_cache(
                text=[anchor.prompt],
                image=image,
                release_oneshot_encoders=False,
            )
            self.prompt = anchor.prompt

    def anchor_embeddings(self, prompt):
        """Encode an opening prompt, or reuse it from an earlier world.

        Either way the pipeline records the embeddings as its last encoded
        prompt, as its own initialize_cache would, so the cache it builds and
        what the next world reuses are unchanged.
        """
        cached = self._anchor_embeddings.pop(prompt, None)
        if cached is None:
            with self._encode_lock:
                cached = self.pipeline.encode_text([prompt])
            if len(self._anchor_embeddings) >= ANCHOR_EMBEDDINGS:
                self._anchor_embeddings.pop(next(iter(self._anchor_embeddings)))
        else:
            self.pipeline._text_cache = ((prompt,), cached)
        # Most recently used last: the oldest entry is the one dropped.
        self._anchor_embeddings[prompt] = cached
        return cached

    def condition_pixels(self, condition):
        """RGB uint8 [T,H,W,3] on the host -> float32 [T,3,H,W] in [0,1] on the GPU.

        The frames cross to the GPU as uint8, a quarter of the float32 bytes,
        and are converted there through a 256-entry table computed by the
        native CPU ``float().div_(255.0)``. A GPU division by a scalar
        multiplies by its reciprocal and differs from it by one ulp for some
        values; the table keeps the encoder input bit-identical.
        """
        torch = self.torch
        if self._unit_table is None:
            table = torch.arange(256, dtype=torch.uint8).float().div_(255.0)
            self._unit_table = table.to("cuda")
        frames = torch.from_numpy(condition).to("cuda")
        return self._unit_table[frames.permute(0, 3, 1, 2).long()].contiguous()

    def set_bank(self, prompts, *, wait):
        """Replace the prompt bank with the embeddings of ``prompts``.

        With ``wait`` the prompts are encoded before this returns, in batches.
        Otherwise a background thread encodes them one at a time, on its own
        CUDA stream, in the gaps between steps, most frequent first; each entry becomes visible only once its encode
        has completed, and until then a step encodes that prompt itself. Each
        entry equals the text encoder's output for that one string, as a live
        encode would produce it. The
        previous bank is dropped at once, between steps, when no queued work
        reads it. Returns (prompts requested, seconds spent encoding when
        waited, else 0).
        """
        prompts = tuple(dict.fromkeys(prompts))[:BANK_PROMPTS]
        with self._bank_lock:
            self._bank_generation += 1
            generation = self._bank_generation
            self._bank = {}
        with self._idle:
            self._idle.notify_all()
        if not prompts:
            return len(prompts), 0.0
        torch = self.torch
        # Loads the encoder on first use, on this thread, as encode_text does.
        self.pipeline._ensure_oneshot_encoders_loaded()
        encoder = self.pipeline.text_encoder
        if wait:
            started = time.perf_counter()
            bank = {}
            with torch.no_grad():
                for first in range(0, len(prompts), BANK_BATCH):
                    batch = prompts[first : first + BANK_BATCH]
                    with self._encode_lock:
                        rows = encoder(list(batch))
                    for row, prompt in enumerate(batch):
                        bank[prompt] = rows[row : row + 1]
            torch.cuda.current_stream().synchronize()
            with self._bank_lock:
                if generation == self._bank_generation:
                    self._bank = bank
            return len(prompts), time.perf_counter() - started
        if self._bank_stream is None:
            self._bank_stream = torch.cuda.Stream()
        stream = self._bank_stream

        def build():
            bank = {}
            with torch.no_grad(), torch.cuda.stream(stream):
                for prompt in prompts:
                    with self._idle:
                        self._idle.wait_for(
                            lambda: not self._stepping or generation != self._bank_generation
                        )
                    if generation != self._bank_generation:
                        return
                    with self._encode_lock:
                        embeddings = encoder([prompt])
                        stream.synchronize()
                    bank[prompt] = embeddings
                    with self._bank_lock:
                        if generation != self._bank_generation:
                            return
                        # A new dict each time: a step reads one whole, never
                        # one being changed.
                        self._bank = dict(bank)

        self._bank_thread = threading.Thread(target=build, name="eden-prompt-bank", daemon=True)
        self._bank_thread.start()
        return len(prompts), 0.0

    def join_bank(self) -> None:
        """Wait for a background bank build to finish or stop."""
        if self._bank_thread is not None:
            self._bank_thread.join()
            self._bank_thread = None

    def clear_bank(self) -> None:
        with self._bank_lock:
            self._bank_generation += 1
            self._bank = {}
        with self._idle:
            self._idle.notify_all()

    def text_embeddings(self, prompt):
        """Embeddings for a chunk prompt: the bank, else this world's, else an encode.

        A prompt in the game's bank is never encoded during play. Any other is
        encoded once per world and reused when the world returns to it.
        """
        banked = self._bank.get(prompt)
        if banked is not None:
            # Recorded as the last encoded prompt, as an encode would, so the
            # next world's initialize_cache sees the same record either way.
            self.pipeline._text_cache = ((prompt,), banked)
            return banked
        cached = self._text_embeddings.pop(prompt, None)
        if cached is None:
            with self._encode_lock:
                cached = self.pipeline.encode_text([prompt])
            self.text_encoded = True
            if len(self._text_embeddings) >= TEXT_EMBEDDINGS_PER_WORLD:
                self._text_embeddings.pop(next(iter(self._text_embeddings)))
        else:
            # encode_text also records the last prompt it encoded, which the
            # next world's initialize_cache consults; a reuse records it the
            # same way, so the next world starts exactly as without reuse.
            self.pipeline._text_cache = ((prompt,), cached)
        # Most recently used last: the oldest entry is the one dropped.
        self._text_embeddings[prompt] = cached
        return cached

    def step(self, index, condition, prompt):
        with self._idle:
            self._stepping = True
        try:
            return self._step(index, condition, prompt)
        finally:
            with self._idle:
                self._stepping = False
                self._idle.notify_all()

    def _step(self, index, condition, prompt):
        torch = self.torch
        with torch.no_grad():
            images = self.condition_pixels(condition)
            control = self.dino.encode(
                images, latent_height=44, latent_width=80, dtype=torch.bfloat16
            )
            self.text_encoded = False
            if prompt != self.prompt:
                embeddings = self.text_embeddings(prompt)
                self.pipeline.replace_text_embeddings(self.cache, embeddings)
                self.prompt = prompt
            frames = self.pipeline.generate(
                autoregressive_index=index, cache=self.cache, input=control
            )
            self.pipeline.finalize(autoregressive_index=index, cache=self.cache)
            # Native TCHW [-1,1] -> RGB24 conversion, before transport encoding.
            rgb = ((frames.float().clamp(-1, 1) + 1) * 127.5).round().clamp(0, 255)
            rgb = rgb.to(torch.uint8).permute(0, 2, 3, 1).contiguous()
            # Read back straight into page-locked memory: one DMA, no staging
            # copy through pageable memory. The host allocator reuses the block
            # once the returned array is released.
            pixels = torch.empty(rgb.shape, dtype=torch.uint8, pin_memory=True)
            pixels.copy_(rgb)
            return pixels.numpy()
