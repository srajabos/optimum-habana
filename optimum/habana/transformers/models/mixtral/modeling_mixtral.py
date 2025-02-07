# coding=utf-8
# Copyright 2023 Mistral AI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PyTorch Mixtral model."""

import math
import os
from typing import List, Optional, Tuple, Union

import habana_frameworks.torch.core as htcore
import torch
import torch.nn.functional as F
from transformers.cache_utils import Cache, DynamicCache, StaticCache
from transformers.integrations.deepspeed import is_deepspeed_available
from transformers.modeling_attn_mask_utils import (
    _prepare_4d_causal_attention_mask,
    _prepare_4d_causal_attention_mask_for_sdpa,
)
from transformers.modeling_outputs import (
    BaseModelOutputWithPast,
    CausalLMOutputWithPast,
    MoeCausalLMOutputWithPast,
    MoeModelOutputWithPast,
)
from transformers.models.mixtral.modeling_mixtral import (
    KwargsForCausalLM,
    MixtralAttention,
    MixtralDecoderLayer,
    MixtralForCausalLM,
    MixtralModel,
    apply_rotary_pos_emb,
    load_balancing_loss_func,
)
from transformers.processing_utils import Unpack
from transformers.utils import logging

from ....utils import get_device_name
from ..llama.modeling_llama import GaudiLlamaRotaryEmbedding
from ..modeling_all_models import KVCache, apply_customized_rope_module
from .configuration_mixtral import MixtralConfig


try:
    from habana_frameworks.torch.hpex.normalization import FusedRMSNorm
except ImportError:
    print("Not using HPU fused kernel for RMSNorm")
    FusedRMSNorm = None

try:
    from habana_frameworks.torch.hpex.kernels import FusedSDPA
except ImportError:
    print("Not using HPU fused scaled dot-product attention kernel.")
    FusedSDPA = None

try:
    from habana_frameworks.torch.hpex.kernels import RotaryPosEmbeddingHelperV2 as FusedRoPE
except ImportError:
    print("Not using HPU fused kernel for apply_rotary_pos_emb")
    FusedRoPE = None

deepspeed_available = is_deepspeed_available()
logger = logging.get_logger(__name__)


def apply_customized_rope(q, k, cos, sin, position_ids, training=True):
    if q.device.type == "hpu" and FusedRoPE is not None:
        return apply_customized_rope_module(q, k, cos, sin, position_ids, training)
    else:
        return apply_rotary_pos_emb(q, k, cos, sin, position_ids)


def gaudi_mixtral_rmsnorm_forward(self, hidden_states):
    """
    Copied from MixtralRMSNorm.forward: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py
    The only differences are:
        - override RMSNorm with Habana fused RMSNorm
    """
    if hidden_states.device.type == "hpu" and FusedRMSNorm is not None:
        # mixed dtypes are not good for FusedRMSNorm, both inputs need to have same dtype
        if hidden_states.dtype != self.weight.dtype:
            orig_dtype = hidden_states.dtype
            hidden_states = FusedRMSNorm.apply(hidden_states.to(self.weight.dtype), self.weight, self.variance_epsilon)
            return hidden_states.to(orig_dtype)
        else:
            hidden_states = FusedRMSNorm.apply(hidden_states, self.weight, self.variance_epsilon)
            return hidden_states
    else:
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


def gaudi_mixtral_repeat_kv(
    query_states: torch.Tensor,
    key_states: torch.Tensor,
    value_states: torch.Tensor,
    attention_mask: torch.Tensor,
    n_rep: int,
):
    """
    Copied from repeat_kv: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py
    The only differences are:
    - Append num_key_value_heads == 1 check as kv states can be broadcasted during matmuls so need to expand and reshape them.
    - Add new args query_states, key_states, value_states and attention_mask and update the logic for expansion.
    The query states go from (batch, num_heads, seqlen, head_dim) to (batch, num_key_value_heads, n_rep, seqlen, head_dim)
    The key/value states go from (batch, num_key_value_heads, seqlen, head_dim) to (batch, num_key_value_heads, 1, seqlen, head_dim)
    """
    batch, num_key_value_heads, kv_len, head_dim = key_states.shape
    if n_rep == 1 or num_key_value_heads == 1:
        return query_states, key_states, value_states, attention_mask

    new_kv_shape = (batch, num_key_value_heads, 1, kv_len, head_dim)
    key_states = key_states.reshape(new_kv_shape)
    value_states = value_states.reshape(new_kv_shape)

    batch, _, q_len, head_dim = query_states.shape
    new_q_shape = (batch, num_key_value_heads, n_rep, q_len, head_dim)
    query_states = query_states.reshape(new_q_shape)

    if attention_mask is not None:
        # Add groups dim and set to 1
        attention_mask = attention_mask.unsqueeze(1)

    return query_states, key_states, value_states, attention_mask


class GaudiMixtralAttentionLongSequence:
    @staticmethod
    def forward(q, k, v, mask, causal, q_block_size):
        """
        Support long sequence at prompt phase
        """
        q_len = q.size(-2)
        q_tiles = (q_len // q_block_size) if (q_len % q_block_size == 0) else math.ceil(q_len / q_block_size)
        q_padding = q_tiles * q_block_size - q_len
        q = F.pad(q, (0, 0, 0, q_padding), "constant", 0)
        if mask is not None:
            mask = F.pad(mask, (0, 0, 0, q_padding), "constant", -10000.0)
        attn_output = torch.zeros_like(q)

        for i in range(q_tiles):
            s, e = i * q_block_size, (i + 1) * q_block_size
            row_q = q[:, :, s:e, :]
            row_mask = mask[:, :, s:e, :]
            attn_output[:, :, s:e, :] = FusedSDPA.apply(row_q, k, v, row_mask, 0.0, causal, None)

        if q_padding != 0:
            attn_output = attn_output[:, :, :-q_padding, :]

        return attn_output


def gaudi_eager_attention_forward(
    module: torch.nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    scaling: float,
    dropout: float = 0.0,
    **kwargs,
):
    bsz, q_len = kwargs["input_shape"]
    query_states, key_states, value_states, attention_mask = gaudi_mixtral_repeat_kv(
        query, key, value, attention_mask, module.num_key_value_groups
    )

    attn_weights = torch.matmul(query_states, key_states.transpose(-2, -1)) * scaling
    if attention_mask is not None:
        causal_mask = attention_mask[:, :, :, : key_states.shape[-2]]
        attn_weights = attn_weights + causal_mask

    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query.dtype)
    attn_weights = torch.nn.functional.dropout(attn_weights, p=dropout, training=module.training)
    attn_output = torch.matmul(attn_weights, value_states)

    attn_output = attn_output.reshape(bsz, -1, q_len, module.head_dim).contiguous()

    return attn_output, attn_weights


class GaudiMixtralAttention(MixtralAttention):
    def __init__(self, config: MixtralConfig, layer_idx: Optional[int] = None):
        super().__init__(config, layer_idx)
        self.config = config
        self.k_cache = KVCache()
        self.v_cache = KVCache()
        self.inp_seq_len = -1
        self.rotary_emb = GaudiLlamaRotaryEmbedding(config=config)
        self.block_size = 1024
        self.num_key_value_heads = config.num_key_value_heads

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        cache_shape = (batch_size, self.num_key_value_heads, max_seq_len, self.head_dim)
        device = self.k_proj.weight.device
        dtype = self.config.torch_dtype
        self.k_cache.allocate(inp_seq_len, dtype, device, cache_shape)
        self.v_cache.allocate(inp_seq_len, dtype, device, cache_shape)

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_embeddings: Tuple[torch.Tensor, torch.Tensor],
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[Cache] = None,
        use_cache: bool = False,
        cache_position: Optional[torch.LongTensor] = None,
        token_idx: Optional[torch.Tensor] = None,
        reuse_cache: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        cache_idx: int = None,
        **kwargs,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        """
        Copied from MixtralAttention.forward: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py
        The only differences are:
        - add new args token_idx
        - optimize KV cache
        - add new args reuse_cache
        - add new args flash_attention_recompute
        - add new args cache_idx
        """
        input_shape = hidden_states.shape[:-1]
        q_len = input_shape[1]
        hidden_shape = (*input_shape, -1, self.head_dim)

        query_states = self.q_proj(hidden_states).view(hidden_shape).transpose(1, 2)
        key_states = self.k_proj(hidden_states).view(hidden_shape).transpose(1, 2)
        value_states = self.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            if self.layer_idx is None:
                raise ValueError(
                    f"The cache structure has changed since version v4.36. If you are using {self.__class__.__name__} "
                    "for auto-regressive decoding with k/v caching, please make sure to initialize the attention class "
                    "with a layer index."
                )
            if token_idx is None:
                if hasattr(past_key_value, "get_usable_length"):
                    kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)
                else:
                    kv_seq_len += past_key_value[0].shape[-2]
            else:
                if reuse_cache:
                    kv_seq_len = past_key_value[0][-2]
                else:
                    kv_seq_len = past_key_value[0].shape[-2]

        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, key_states = apply_customized_rope(
            query_states, key_states, cos, sin, kwargs["position_ids"], self.training
        )

        if use_cache:
            if reuse_cache:
                key_states = self.k_cache(key_states, 2, token_idx)
                value_states = self.v_cache(value_states, 2, token_idx)
                past_key_value = (self.k_cache.get_shape(), self.v_cache.get_shape())
            else:
                if past_key_value is None:
                    past_key = torch.zeros(key_states.shape, dtype=self.k_proj.weight.dtype, device=key_states.device)
                    past_value = torch.zeros(
                        key_states.shape, dtype=self.k_proj.weight.dtype, device=key_states.device
                    )
                    past_key_value = (past_key, past_value)
                key_states = self.k_cache.update(past_key_value[0], key_states, 2, token_idx, self.inp_seq_len)
                value_states = self.v_cache.update(past_key_value[1], value_states, 2, token_idx, self.inp_seq_len)
                if token_idx is None:
                    past_key_value = (key_states, value_states)

            if cache_idx is not None and q_len == 1:
                key_states = key_states[:, :, :cache_idx, :]
                value_states = value_states[:, :, :cache_idx, :]
                if attention_mask is not None:
                    attention_mask = attention_mask[:, :, :, :cache_idx]
                kv_seq_len = key_states.shape[-2]
        else:
            past_key_value = None

        if FusedSDPA is not None:
            attn_weights = None
            if query_states.dtype != key_states.dtype:
                key_states = key_states.type(query_states.dtype)
                value_states = value_states.type(query_states.dtype)
            # support long sequences exceeding 8192
            if not self.training and q_len == key_states.size(-2) and q_len > 8192:
                htcore.mark_step()
                attn_output = GaudiMixtralAttentionLongSequence.forward(
                    query_states,
                    key_states,
                    value_states,
                    attention_mask,
                    False,
                    self.block_size,
                )
                htcore.mark_step()
            else:
                attn_output = FusedSDPA.apply(
                    query_states,
                    key_states,
                    value_states,
                    attention_mask,
                    0.0,
                    False,
                    None,
                    "None",
                    flash_attention_recompute,
                )
        else:
            attn_output, attn_weights = gaudi_eager_attention_forward(
                self,
                query_states,
                key_states,
                value_states,
                attention_mask,
                dropout=0.0 if not self.training else self.attention_dropout,
                scaling=self.scaling,
                sliding_window=getattr(self.config, "sliding_window", None),  # main diff with Llama
                input_shape=input_shape,
            )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(*input_shape, -1).contiguous()
        attn_output = self.o_proj(attn_output)

        return attn_output, attn_weights, past_key_value


def gaudi_mixtral_block_moe_forward(self, hidden_states: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    # We need this workaround until moe op in hpu is supporting fp8
    if not self.training and not os.environ.get("QUANT_CONFIG") and not get_device_name() == "gaudi":
        # Gaudi1 is not supporting dynamic moe
        return self.dynamic_moe_forward(hidden_states)

    return self.sparse_moe_forward(hidden_states)


def gaudi_mixtral_block_sparse_moe_forward(self, hidden_states: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Copied from MixtralSparseMoeBlock.forward: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py
    The only differences are:
    - optimize expert forward, remove dynamic control and dynamic shape
    """
    batch_size, sequence_length, hidden_dim = hidden_states.shape
    if self.training and self.jitter_noise > 0:
        hidden_states *= torch.empty_like(hidden_states).uniform_(1.0 - self.jitter_noise, 1.0 + self.jitter_noise)
    hidden_states = hidden_states.view(-1, hidden_dim)
    # router_logits: (batch * sequence_length, n_experts)
    router_logits = self.gate(hidden_states)

    if deepspeed_available and (not self.training):
        from deepspeed import comm as dist

        if dist.is_initialized():
            output_tensors = [router_logits.clone() for _ in range(dist.get_world_size())]
            dist.all_gather(output_tensors, router_logits)
            router_logits = torch.cat(output_tensors, dim=1)

    routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
    routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
    routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
    # we cast back to the input dtype
    routing_weights = routing_weights.to(hidden_states.dtype)

    final_hidden_states = torch.zeros(
        (batch_size, sequence_length, hidden_dim), dtype=hidden_states.dtype, device=hidden_states.device
    )

    padded_weights = torch.zeros(
        (batch_size * sequence_length, self.num_experts), dtype=hidden_states.dtype, device=hidden_states.device
    )
    padded_weights.scatter_(-1, selected_experts, routing_weights)
    padded_weights = padded_weights.reshape(-1, sequence_length, self.num_experts)
    padded_weights = padded_weights.permute(2, 0, 1).unsqueeze(-1)

    # Loop over all available experts in the model and perform the computation on each expert
    for expert_idx in range(self.num_experts):
        expert_layer = self.experts[expert_idx]
        padded_weight = padded_weights[expert_idx]
        current_state_static = hidden_states.reshape(-1, hidden_dim)
        current_hidden_states_static = (
            expert_layer(current_state_static).reshape(-1, sequence_length, hidden_dim) * padded_weight
        )
        final_hidden_states += current_hidden_states_static
        # support long sequences exceeding 8192
        if not self.training and sequence_length > 8192:
            htcore.mark_step()

    return final_hidden_states, router_logits


def gaudi_mixtral_block_dynamic_moe_forward(self, hidden_states: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    batch_size, sequence_length, hidden_dim = hidden_states.shape
    original_shape = hidden_states.shape
    hidden_states = hidden_states.view(-1, hidden_dim)
    # router_logits: (batch * sequence_length, n_experts)
    router_logits = self.gate(hidden_states)

    if deepspeed_available and (not self.training):
        from deepspeed import comm as dist

        if dist.is_initialized():
            output_tensors = [router_logits.clone() for _ in range(dist.get_world_size())]
            dist.all_gather(output_tensors, router_logits)
            router_logits = torch.cat(output_tensors, dim=1)

    routing_weights, selected_experts = calculate_routing_tensors(router_logits, self.top_k, hidden_states.dtype)
    # pre-processing for custom op inputs
    w1_list = [expert.w1.weight for expert in self.experts]
    w2_list = [expert.w2.weight for expert in self.experts]
    w3_list = [expert.w3.weight for expert in self.experts]

    final_hidden_states = torch.ops.hpu.mixture_of_experts(
        hidden_states=hidden_states,
        expert_routing_table=selected_experts,
        router_weights=routing_weights,
        w1=w1_list,
        w3=w2_list,
        w2=w3_list,
        permuted_weights=True,
        activation="silu",
        experts_min=0,
        experts_max=7,
    )
    if deepspeed_available and (not self.training):
        from deepspeed import comm as dist

        if dist.is_initialized():
            dist.all_reduce(final_hidden_states)
    return final_hidden_states.view(original_shape), router_logits


def calculate_routing_tensors(
    score: torch.Tensor, topk: int, hidden_states_dtype: torch.dtype
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Based on https://github.com/huggingface/transformers/blob/main/src/transformers/models/mixtral/modeling_mixtral.py#L641"""
    routing_weights = F.softmax(score, dim=1, dtype=torch.float32)
    routing_weights, selected_experts = torch.topk(routing_weights, topk, dim=-1)
    routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
    routing_weights = routing_weights.to(hidden_states_dtype)
    return routing_weights, selected_experts


class GaudiMixtralDecoderLayer(MixtralDecoderLayer):
    def __init__(self, config: MixtralConfig, layer_idx: int):
        super().__init__(config, layer_idx)
        self.self_attn = GaudiMixtralAttention(config, layer_idx)

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        self.self_attn.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        output_router_logits: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # necessary, but kept here for BC
        token_idx: Optional[torch.Tensor] = None,
        reuse_cache: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        cache_idx: int = None,
        **kwargs,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Copied from MixtralDecoderLayer.forward: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py
        The only differences are:
        - add new args token_idx
        - add new args reuse_cache
        - add new args flash_attention_recompute
        - add new args cache_idx
        """
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            position_embeddings=position_embeddings,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            cache_position=cache_position,
            token_idx=token_idx,
            reuse_cache=reuse_cache,
            flash_attention_recompute=flash_attention_recompute,
            cache_idx=cache_idx,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states, router_logits = self.block_sparse_moe(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        if output_router_logits:
            outputs += (router_logits,)

        return outputs


class GaudiMixtralModel(MixtralModel):
    def __init__(self, config: MixtralConfig):
        super().__init__(config)

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        for layer in self.layers:
            layer.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        token_idx: Optional[torch.Tensor] = None,
        reuse_cache: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        cache_idx: int = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        """
        Copied from MixtralModel.forward: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py#L1069
        The only differences are:
        - add new args token_idx
        - add new args reuse_cache
        - add new args flash_attention_recompute
        - add new args cache_idx
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_router_logits = (
            output_router_logits if output_router_logits is not None else self.config.output_router_logits
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

        past_key_values_length = 0
        use_new_cache = False  # Ignoring new Cache path for HPU

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        if past_key_values is not None and use_cache:
            if reuse_cache:
                past_key_values_length = past_key_values[0][0][2]
            else:
                if use_new_cache:
                    if not isinstance(past_key_values, StaticCache):
                        past_key_values = DynamicCache.from_legacy_cache(past_key_values)
                    past_key_values_length = past_key_values.get_usable_length()
                else:
                    past_key_values_length = past_key_values[0][0].shape[2]

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        if cache_position is None:
            past_seen_tokens = 0
            if past_key_values is not None:
                if isinstance(past_key_values, Cache):
                    past_seen_tokens = past_key_values.get_seq_length()
                else:
                    past_seen_tokens = past_key_values[0][0].shape[2]

            cache_position = torch.arange(
                past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        if self.config._attn_implementation == "flash_attention_2":
            # 2d mask is passed through the layers
            attention_mask = attention_mask if (attention_mask is not None and 0 in attention_mask) else None
        elif self.config._attn_implementation == "sdpa" and not output_attentions:
            # output_attentions=True can not be supported when using SDPA, and we fall back on
            # the manual implementation that requires a 4D causal mask in all cases.
            attention_mask = _prepare_4d_causal_attention_mask_for_sdpa(
                attention_mask,
                (batch_size, seq_length),
                inputs_embeds,
                past_key_values_length,
            )
        else:
            # 4d mask is passed through the layers
            attention_mask = _prepare_4d_causal_attention_mask(
                attention_mask,
                (batch_size, seq_length),
                inputs_embeds,
                past_key_values_length,
                sliding_window=self.config.sliding_window,
            )

        hidden_states = inputs_embeds

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_router_logits = () if output_router_logits else None
        next_decoder_cache = () if not use_new_cache else None

        for layer_idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            if self.gradient_checkpointing and self.training:
                layer_outputs = self._gradient_checkpointing_func(
                    decoder_layer.__call__,
                    hidden_states,
                    attention_mask,
                    position_ids,
                    past_key_values,
                    output_attentions,
                    output_router_logits,
                    use_cache,
                    cache_position,
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=None if past_key_values is None else past_key_values[layer_idx],
                    output_attentions=output_attentions,
                    output_router_logits=output_router_logits,
                    use_cache=use_cache,
                    cache_position=cache_position,
                    token_idx=token_idx,
                    reuse_cache=reuse_cache,
                    flash_attention_recompute=flash_attention_recompute,
                    cache_idx=cache_idx,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

            if output_router_logits:
                all_router_logits += (layer_outputs[-1],)

            htcore.mark_step()

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = None
        if use_cache:
            next_cache = (
                next_decoder_cache.to_legacy_cache() if isinstance(next_decoder_cache, Cache) else next_decoder_cache
            )

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, next_cache, all_hidden_states, all_self_attns, all_router_logits]
                if v is not None
            )
        return MoeModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            router_logits=all_router_logits,
        )


class GaudiMixtralForCausalLM(MixtralForCausalLM):
    """
    Inherits from MixtralForCausalLM: https://github.com/huggingface/transformers/blob/v4.37.0/src/transformers/models/mixtral/modeling_mixtral.py#L1231
    The only differences are:
    - add new args token_idx
    - add token_idx into model_inputs
    - from step2 when enable KV cache, slice next_input_ids from input_ids base on the token_idx
    - from step2 when enable KV cache, slice next_position_ids from position_ids base on the token_idx
    """

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        self.model.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)
        self.kv_cache_len = max_seq_len

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        num_logits_to_keep: int = 0,
        token_idx: Optional[torch.Tensor] = None,
        reuse_cache: Optional[bool] = None,
        flash_attention_recompute: Optional[bool] = False,
        cache_idx: int = None,
        **kwargs: Unpack[KwargsForCausalLM],
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_router_logits = (
            output_router_logits if output_router_logits is not None else self.config.output_router_logits
        )

        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_router_logits=output_router_logits,
            return_dict=return_dict,
            cache_position=cache_position,
            token_idx=token_idx,
            reuse_cache=reuse_cache,
            flash_attention_recompute=flash_attention_recompute,
            cache_idx=cache_idx,
        )

        hidden_states = outputs[0]
        # Only compute necessary logits, and do not upcast them to float if we are not computing the loss
        logits = self.lm_head(hidden_states[:, -num_logits_to_keep:, :])

        loss = None
        if labels is not None:
            loss = self.loss_function(logits, labels, self.vocab_size, **kwargs)

        aux_loss = None
        if output_router_logits:
            aux_loss = load_balancing_loss_func(
                outputs.router_logits if return_dict else outputs[-1],
                self.num_experts,
                self.num_experts_per_tok,
                attention_mask,
            )
            if labels is not None:
                loss += self.router_aux_loss_coef * aux_loss.to(loss.device)  # make sure to reside in the same device

        if not return_dict:
            output = (logits,) + outputs[1:]
            if output_router_logits:
                output = (aux_loss,) + output
            return (loss,) + output if loss is not None else output

        return MoeCausalLMOutputWithPast(
            loss=loss,
            aux_loss=aux_loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            router_logits=outputs.router_logits,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        inputs_embeds=None,
        cache_position=None,
        output_router_logits=False,
        position_ids=None,
        use_cache=True,
        num_logits_to_keep=None,
        **kwargs,
    ):
        reuse_cache = kwargs.get("reuse_cache")
        token_idx = kwargs.get("token_idx", None)

        # Omit tokens covered by past_key_values
        if past_key_values is not None:
            if token_idx is not None:
                idx = token_idx + kwargs.get("inputs_embeds_offset", 0) - 1
                input_ids = torch.index_select(input_ids, 1, idx)
            else:
                if inputs_embeds is not None:  # Exception 1
                    input_ids = input_ids[:, -cache_position.shape[0] :]
                elif (
                    input_ids.shape[1] != cache_position.shape[0]
                ):  # Default case (the "else", a no op, is Exception 2)
                    input_ids = input_ids[:, cache_position]
        elif reuse_cache and token_idx is not None:
            # With reuse_cache, KV cache is pre allocated hence for the 1st token we can slice the inputs till token idx for the fwd pass
            input_ids = input_ids[:, :token_idx]
            attention_mask = attention_mask[:, :token_idx]

        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                if token_idx is not None:
                    position_ids = torch.index_select(position_ids, 1, token_idx - 1)
                else:
                    position_ids = position_ids[:, -input_ids.shape[1] :]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids.contiguous()}  # `contiguous()` needed for compilation use cases

        if num_logits_to_keep is not None:
            model_inputs["num_logits_to_keep"] = num_logits_to_keep

        model_inputs.update(
            {
                "position_ids": position_ids,
                "cache_position": cache_position,
                "past_key_values": past_key_values,
                "use_cache": use_cache,
                "attention_mask": attention_mask,
                "output_router_logits": output_router_logits,
                "token_idx": token_idx,
                "reuse_cache": reuse_cache,
                "flash_attention_recompute": kwargs.get("flash_attention_recompute"),
                "cache_idx": kwargs.get("cache_idx"),
            }
        )
        return model_inputs
