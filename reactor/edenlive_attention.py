# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Dense FA4 for the released single-GPU 5B inference path."""


def install_flash_attention4(network) -> None:
    """Replace attention execution, retaining native projections, RoPE and KV.

    The native cache already selects the visible tokens. Attention within that
    selected chunk/window is non-causal, including text cross-attention.
    FA4 preserves dense attention; its floating-point reduction order differs
    from PyTorch's FlashAttention kernel.
    """
    from flash_attn.cute import flash_attn_func
    from flashdreams.core.attention.cp import ContextParallelAttention
    from flashdreams.recipes.wan.transformer.impl.modules import MultiHeadAttention

    class FlashAttention4(ContextParallelAttention):
        def _impl(self, query, key, value):
            if self.device_mesh is not None:
                raise RuntimeError("EdenLive FA4 requires single-GPU execution")
            output, _lse = flash_attn_func(
                query.transpose(1, 2),
                key.transpose(1, 2),
                value.transpose(1, 2),
                causal=False,
            )
            return output.transpose(1, 2)

    for module in network.modules():
        if isinstance(module, MultiHeadAttention):
            if module.attn_op.is_context_parallel_enabled():
                raise ValueError("EdenLive FA4 requires single-GPU execution")
            module.attn_op = FlashAttention4(qkv_format="bshd")
