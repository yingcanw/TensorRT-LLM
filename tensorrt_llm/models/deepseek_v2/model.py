# SPDX-FileCopyrightText: Copyright (c) 2022-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional

import torch

from ..._utils import pad_vocab_size, torch_dtype_to_str
from ...functional import Tensor, non_gated_version, recv, send
from ...layers import (MOE, AttentionMaskType, ColumnLinear,
                       DeepseekV2Attention, Embedding, GatedMLP,
                       PositionEmbeddingType, RmsNorm, SharedMoE)
from ...mapping import Mapping
from ...module import Module
from ...plugin import init_all_reduce_helper
from ..modeling_utils import (DecoderLayerList, DecoderModelForCausalLM,
                              PretrainedConfig, QuantConfig)
from .config import DeepSeekV2Config
from .convert import (load_hf_deepseek, load_weights_from_hf_by_shard,
                      load_weights_from_hf_model)


class DeepseekV2DecoderLayer(Module):

    def __init__(self, config: PretrainedConfig, layer_idx: int):
        super().__init__()
        self.layer_idx = layer_idx
        self.config = config

        ### Input layernorm in Deepseek v2 is same as Llama
        self.input_layernorm = RmsNorm(normalized_shape=config.hidden_size,
                                       eps=config.norm_epsilon,
                                       dtype=config.dtype)

        layers_range = config.mapping.pp_layers(config.num_hidden_layers)
        local_layer_idx = layer_idx - layers_range[0]

        self.attention = DeepseekV2Attention(
            local_layer_idx=local_layer_idx,
            hidden_size=config.hidden_size,
            num_attention_heads=config.num_attention_heads,
            q_lora_rank=config.q_lora_rank,
            kv_lora_rank=config.kv_lora_rank,
            qk_nope_head_dim=config.qk_nope_head_dim,
            qk_rope_head_dim=config.qk_rope_head_dim,
            v_head_dim=config.v_head_dim,
            max_position_embeddings=config.max_position_embeddings,
            eps=config.norm_epsilon,
            attention_mask_type=AttentionMaskType.causal,
            dtype=config.dtype,
            position_embedding_type=PositionEmbeddingType.learned_absolute,
            rotary_embedding_base=config.rotary_base,
            rotary_embedding_scaling=None,
            rotary_scaling=config.rotary_scaling,
            tp_group=config.mapping.tp_group,
            tp_size=config.mapping.tp_size,
            tp_rank=config.mapping.tp_rank)

        ### Added deepseek MoE and shared_experts
        ### First decoder layer: MLA + dense MLP + input_layernorm(RMSNorm) + post_attention_layernorm(RMSNorm)
        ### Rest decoder layer: MLA + MoE MLP + MoE Gate + shared_experts(MLP) + input_layernorm(RMSNorm) + post_attention_layernorm(RMSNorm)
        ### Added MLA in co-testing phase, use standard attention for MoE testing

        ### Distinguish dense MLP and MoE MLP
        # dense_config = DenseConfig(intermediate_size=config.intermediate_size)
        moe_config = config.moe

        # layer_config = LayerMLPConfig(config=[dense_config, moe_config], moe_layer_idx_min=0,
        #                             moe_layer_idx_max=config.num_hidden_layers,
        #                             total_num_layers=config.num_hidden_layers)
        if moe_config.num_experts > 0 and layer_idx >= config.first_k_dense_replace:
            hidden_act = config.hidden_act
            mlp_hidden_size = config.moe_inter_size
            mlp_kwargs = {'moe_config': moe_config, 'mapping': config.mapping}
            if moe_config.shared_expert_intermediate_size > 0:
                ClsMLP = SharedMoE
                mlp_kwargs['use_shared_gate'] = False
                mlp_kwargs['use_side_stream'] = False
            else:
                ClsMLP = MOE
        else:
            ClsMLP = GatedMLP
            mlp_hidden_size = config.intermediate_size
            hidden_act = non_gated_version(
                config.hidden_act)  # back to non gated for dense layers
            mlp_kwargs = {}

        self.mlp = ClsMLP(hidden_size=config.hidden_size,
                          ffn_hidden_size=mlp_hidden_size,
                          hidden_act=hidden_act,
                          dtype=config.dtype,
                          bias=False,
                          tp_group=config.mapping.tp_group,
                          tp_size=config.mapping.tp_size,
                          **mlp_kwargs)

        ### Pose layernorm in Deepseek v2 is same as Llama
        self.post_layernorm = RmsNorm(normalized_shape=config.hidden_size,
                                      eps=config.norm_epsilon,
                                      dtype=config.dtype)

    def forward(self,
                hidden_states,
                attention_mask=None,
                use_cache=False,
                spec_decoding_params=None,
                kv_cache_params=None,
                attention_params=None):

        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)

        attention_output = self.attention(
            hidden_states=hidden_states,
            use_cache=use_cache,
            spec_decoding_params=spec_decoding_params,
            kv_cache_params=kv_cache_params,
            attention_params=attention_params)
        if use_cache:
            attention_output, presents = attention_output

        hidden_states = residual + attention_output

        residual_attn = hidden_states

        hidden_states = self.post_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual_attn + hidden_states
        if use_cache:
            return (hidden_states, presents)
        return hidden_states


class DeepseekV2Model(Module):

    def __init__(self, config: PretrainedConfig) -> None:
        super().__init__()
        init_all_reduce_helper()  # enable use_customer_all_reduce
        self.dtype = config.dtype
        self.mapping = config.mapping
        if self.mapping.is_first_pp_rank():
            self.vocab_embedding = Embedding(config.vocab_size,
                                             config.hidden_size,
                                             dtype=config.dtype)
        self.layers = DecoderLayerList(DeepseekV2DecoderLayer, config)

        if self.mapping.is_last_pp_rank():
            self.ln_f = RmsNorm(normalized_shape=config.hidden_size,
                                eps=config.norm_epsilon,
                                dtype=config.dtype)

        self.head_num = config.num_attention_heads
        self.head_size = config.qk_nope_head_dim + config.qk_rope_head_dim

    def forward(self,
                input_ids,
                position_ids=None,
                use_cache=False,
                attention_mask=None,
                spec_decoding_params=None,
                kv_cache_params=None,
                attention_params=None,
                hidden_states=None,
                prompt_embedding_table: Optional[Tensor] = None,
                prompt_tasks: Optional[Tensor] = None,
                prompt_vocab_size: Optional[Tensor] = None):

        ptuning_args = [
            prompt_embedding_table, prompt_tasks, prompt_vocab_size
        ] if prompt_embedding_table is not None else []

        if self.mapping.is_first_pp_rank():
            hidden_states = self.vocab_embedding(input_ids, *ptuning_args)
        else:
            hidden_states = recv(hidden_states, self.mapping.prev_pp_rank())

        hidden_states = self.layers.forward(
            hidden_states,
            use_cache=use_cache,
            kv_cache_params=kv_cache_params,
            attention_params=attention_params,
            spec_decoding_params=spec_decoding_params)

        if use_cache:
            hidden_states, presents = hidden_states

        if self.mapping.is_last_pp_rank():
            hidden_states = self.ln_f(hidden_states)
        else:
            hidden_states = send(hidden_states, self.mapping.next_pp_rank())

        if use_cache:
            return (hidden_states, tuple(presents))
        return hidden_states


class DeepseekV2ForCausalLM(DecoderModelForCausalLM):

    def __init__(self, config: PretrainedConfig):
        transformer = DeepseekV2Model(config)
        vocab_size_padded = pad_vocab_size(config.vocab_size,
                                           config.mapping.tp_size)
        if config.mapping.is_last_pp_rank():
            lm_head = ColumnLinear(config.hidden_size,
                                   vocab_size_padded,
                                   bias=False,
                                   dtype=config.dtype,
                                   tp_group=config.mapping.tp_group,
                                   tp_size=config.mapping.tp_size,
                                   gather_output=True)
        else:
            lm_head = None
        self.mapping = config.mapping
        super().__init__(config, transformer, lm_head)

    @classmethod
    def from_hugging_face(cls,
                          model_dir,
                          dtype: str = 'auto',
                          mapping: Optional[Mapping] = None,
                          quant_config: Optional[QuantConfig] = None,
                          **kwargs):
        load_by_shard = kwargs.pop('load_by_shard', False)
        load_model_on_cpu = kwargs.pop('load_model_on_cpu', False)
        if mapping is None:
            mapping = Mapping()
        config = DeepSeekV2Config.from_hugging_face(model_dir,
                                                    dtype=dtype,
                                                    mapping=mapping,
                                                    quant_config=quant_config,
                                                    **kwargs)
        if load_by_shard:
            weights = load_weights_from_hf_by_shard(model_dir, config)
        else:
            hf_model = load_hf_deepseek(model_dir, load_model_on_cpu)
            weights = load_weights_from_hf_model(hf_model, config)
        model = cls(config)
        model.load(weights)
        return model

        if dtype == 'auto':
            dtype = getattr(config, 'torch_dtype', None)
        if dtype is None:
            dtype = 'float16'
        if isinstance(dtype, torch.dtype):
            dtype = torch_dtype_to_str(dtype)
        if dtype == 'float32':  # should remove "float32"
            dtype = 'float16'
        if dtype == 'bfloat16' and torch.cuda.get_device_properties(
                0).major < 8:
            logger.warning(
                "Pre SM 80 GPUs do not support bfloat16, fallback to float16")
            dtype = 'float16'

        deepseek = cls.from_config(pretrained_config)
        weights = convert_deepseekv2(
            hf_model,
            config,
            mapping,
            dtype=dtype,
            use_parallel_embedding=config.get('use_parallel_embedding', False),
            sharding_dim=config.get('embedding_sharding_dim', 0))
        deepseek.load(weights)

        return deepseek
