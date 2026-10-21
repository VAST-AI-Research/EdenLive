# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Capture the DiT's CUDA graphs once per process instead of once per world.

The stock rollout fills its KV cache eagerly for the first chunks of every
world and captures its one steady-state graph only once the cache is full.
``Wan21Transformer.initialize_autoregressive_cache`` also allocates new
self-attention KV buffers and new cross-attention text K/V for every world and
resets the graph dispatch, because a graph replays against the storage
addresses it was captured with. So every world pays eager fill chunks plus a
recapture. Two changes remove both:

1. One transformer cache for the process. The first world's cache is kept. A
   later world resets only its bookkeeping (``Wan21TransformerCache.reset``
   keeps the buffers) and writes its own text K/V into the existing buffers
   in place, through ``replace_text_embeddings``: the path a mid-world prompt
   change already takes, which exists so a replayed graph reads the new text.

2. One graph per distinct step shape. While the cache fills, each step attends
   to a longer KV view, so each fill step gets its own graph; every step from
   ``steady_from`` on attends to the full window and shares one. Step 0 runs
   the network with two input signatures: its denoising passes carry a
   per-token timestep (the anchor frame's tokens at t=0), its cache-commit
   pass the scalar one. A graph wrapper drops its graph whenever its input
   shapes change, so step 0 gets one graph per timestep shape.

With stable buffers every graph stays valid across worlds, so each is captured
once, during load warm-up, and replayed in every later world. A cache the kept
one cannot stand in for (classifier-free guidance's negative prompt, image
embeddings, or a latent size other than the kept cache's) takes the stock
path and the stock dispatch, unchanged.
"""

from typing import Any


class StepGraphs:
    """Stand-in for the transformer's ``CUDAGraphDispatch``.

    ``kept`` says whether the transformer's current cache is the kept one. When
    it is, steps are dispatched to the per-step graphs; otherwise everything
    goes to the stock dispatch, which the stock cache path resets as before.
    """

    def __init__(self, stock: Any, *, steady_from: int) -> None:
        self.stock = stock
        self.steady_from = steady_from
        self.kept = False
        self._graphs: dict[tuple, Any] = {}
        self._first_steps: dict[bool, _FirstStep] = {}

    @property
    def enabled(self) -> bool:
        return self.stock.enabled

    @property
    def cond_call(self) -> Any:
        return self.stock.cond_call

    @property
    def uncond_call(self) -> Any:
        return self.stock.uncond_call

    def rebuild(self, **kwargs: Any) -> None:
        self.stock.rebuild(**kwargs)
        self._graphs.clear()

    def disable(self, **kwargs: Any) -> None:
        self.stock.disable(**kwargs)
        self._graphs.clear()

    def reset(self) -> None:
        # Called when the stock path builds a fresh cache. The per-step graphs
        # are bound to the kept cache's buffers, which stay allocated, so they
        # remain valid and are not dropped.
        self.stock.reset()

    def select(self, autoregressive_index: int, *, uncond: bool) -> Any:
        if not self.kept or not self.stock.enabled:
            return self.stock.select(autoregressive_index, uncond=uncond)
        if autoregressive_index == 0:
            return self._first_steps.setdefault(uncond, _FirstStep(self, uncond))
        return self.graph((min(autoregressive_index, self.steady_from), uncond))

    def graph(self, key: tuple) -> Any:
        graph = self._graphs.get(key)
        if graph is None:
            from flashdreams.infra.cuda_graph import CUDAGraphWrapper

            graph = CUDAGraphWrapper(self.stock.fn, warmup_iters=self.stock.warmup_iters)
            self._graphs[key] = graph
        return graph


class _FirstStep:
    """Step 0's network call: routed to one graph per timestep shape."""

    def __init__(self, dispatch: StepGraphs, uncond: bool) -> None:
        self.dispatch = dispatch
        self.uncond = uncond

    def __call__(self, **kwargs: Any) -> Any:
        key = (0, self.uncond, tuple(kwargs["timesteps"].shape))
        return self.dispatch.graph(key)(**kwargs)


def install(transformer: Any, *, steady_from: int) -> StepGraphs:
    """Keep ``transformer``'s first cache and graph every step.

    Call once, after any ``torch.compile`` swap of the network and dispatch,
    and before the first cache is built. Returns the installed dispatch.
    """
    network = getattr(transformer.network, "_orig_mod", transformer.network)
    stock_initialize = transformer.initialize_autoregressive_cache
    dispatch = StepGraphs(transformer._cuda_graph_dispatch, steady_from=steady_from)
    _, patch_h, patch_w = transformer.config.network.patch_size
    kept: dict[str, Any] = {}

    def initialize_autoregressive_cache(
        *,
        height: int,
        width: int,
        text_embeddings: Any,
        image_embeddings: Any = None,
        negative_text_embeddings: Any = None,
        **unused: Any,
    ) -> Any:
        plain = image_embeddings is None and negative_text_embeddings is None
        if not plain or kept.get("size", (height, width)) != (height, width):
            dispatch.kept = False
            return stock_initialize(
                height=height,
                width=width,
                text_embeddings=text_embeddings,
                image_embeddings=image_embeddings,
                negative_text_embeddings=negative_text_embeddings,
                **unused,
            )
        cache = kept.get("cache")
        if cache is None:
            cache = stock_initialize(
                height=height, width=width, text_embeddings=text_embeddings, **unused
            )
            kept.update(cache=cache, size=(height, width))
        else:
            # The latent layout the stock path records per rollout; another
            # size may have run through the stock path since.
            transformer._output_height, transformer._output_width = height, width
            network._patch_h, network._patch_w = height // patch_h, width // patch_w
            cache.reset()
            network.replace_text_embeddings(cache.network_cache, text_embeddings)
        dispatch.kept = True
        return cache

    transformer.initialize_autoregressive_cache = initialize_autoregressive_cache
    transformer._cuda_graph_dispatch = dispatch
    return dispatch
