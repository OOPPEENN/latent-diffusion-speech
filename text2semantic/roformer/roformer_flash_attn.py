from transformers.models.roformer.modeling_roformer import RoFormerSelfAttention
import warnings
import torch
import torch.utils.checkpoint
import torch.nn.functional as F
import logging

from transformers.utils import (
    is_flash_attn_2_available,
    is_flash_attn_greater_or_equal_2_10
)

if is_flash_attn_2_available():
    from flash_attn import flash_attn_func, flash_attn_varlen_func
    from flash_attn.bert_padding import index_first_axis, pad_input, unpad_input 

class RoFormerlashAttention2(RoFormerSelfAttention):
    def __init__(self, *args, **kwargs):
        super().__init__(kwargs.get("config"))
        self._flash_attn_uses_top_left_mask = not is_flash_attn_greater_or_equal_2_10()
        self.is_causal = kwargs.get("is_causal", False)
        self.has_warned = False

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        sinusoidal_pos=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
        **kwargs
    ):
        if "padding_mask" in kwargs:
            warnings.warn("Passing `padding_mask` is deprecated and will be removed in v4.37. Please make sure use `attention_mask` instead.`")
            attention_mask = kwargs.pop("padding_mask")

        mixed_query_layer = self.query(hidden_states)
        query_layer = self.transpose_for_scores(mixed_query_layer)
        is_cross_attention = encoder_hidden_states is not None
        bsz, q_len, _ = hidden_states.size()
        if is_cross_attention:
            _, k_len, _ = encoder_hidden_states.size()
        else:
            k_len = q_len

        if is_cross_attention and past_key_value is not None:
            key_layer = past_key_value[0]
            value_layer = past_key_value[1]
        elif is_cross_attention:
            key_layer = self.transpose_for_scores(self.key(encoder_hidden_states))
            value_layer = self.transpose_for_scores(self.value(encoder_hidden_states))
        else:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))
            if sinusoidal_pos is not None:
                if self.rotary_value:
                    query_layer, key_layer, value_layer = self.apply_rotary_position_embeddings(
                        sinusoidal_pos, query_layer, key_layer, value_layer
                    )
                else:
                    query_layer, key_layer = self.apply_rotary_position_embeddings(
                        sinusoidal_pos, query_layer, key_layer
                    )
            if past_key_value is not None:
                key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
                value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
            encoder_attention_mask = attention_mask

        if self.is_decoder:
            past_key_value = (key_layer, value_layer)

        query_states = query_layer.transpose(1, 2)
        key_states = key_layer.transpose(1, 2)
        value_states = value_layer.transpose(1, 2)
        dropout_rate = self.dropout.p if self.training else 0.0
        input_dtype = query_states.dtype
        if input_dtype == torch.float32:
            if torch.is_autocast_enabled():
                target_dtype = torch.get_autocast_gpu_dtype()
            else:
                target_dtype = self.query.weight.dtype

            if not self.has_warned:
                logging.warning(
                    f"The input hidden states seems to be silently casted in float32, this might be related to"
                    f" the fact you have upcasted embedding or layer norm layers in float32. We will cast back the input in"
                    f" {target_dtype}."
                )
                self.has_warned = True

            query_states = query_states.to(torch.bfloat16)
            key_states = key_states.to(torch.bfloat16)
            value_states = value_states.to(torch.bfloat16)
        attn_output = self._flash_attention_forward(query_states, key_states, value_states, encoder_attention_mask, attention_mask, q_len, k_len, dropout=dropout_rate)

        if input_dtype == torch.float32:
            attn_output = attn_output.to(target_dtype)

        if head_mask is not None:
            attn_output = attn_output * head_mask

        attn_output = attn_output.reshape(bsz, q_len, self.all_head_size).contiguous()

        attn_weights = None

        if self.is_decoder:
            return attn_output, attn_weights, past_key_value
        else:
            return attn_output, attn_weights

    def _flash_attention_forward(self, query_states, key_states, value_states, attention_mask, q_attnetion_mask, query_length, key_length, dropout=0.0, softmax_scale=None):
        if not self._flash_attn_uses_top_left_mask:
            causal = self.is_causal
        else:
            causal = self.is_causal and query_length != 1

        if attention_mask is not None:
            batch_size = query_states.shape[0]
            query_states, key_states, value_states, indices_q, cu_seq_lens, max_seq_lens = self._upad_input(query_states, key_states, value_states, attention_mask, q_attnetion_mask, query_length, key_length)

            cu_seqlens_q, cu_seqlens_k = cu_seq_lens
            max_seqlen_in_batch_q, max_seqlen_in_batch_k = max_seq_lens

            attn_output_unpad = flash_attn_varlen_func(
                query_states,
                key_states,
                value_states,
                cu_seqlens_q=cu_seqlens_q,
                cu_seqlens_k=cu_seqlens_k,
                max_seqlen_q=max_seqlen_in_batch_q,
                max_seqlen_k=max_seqlen_in_batch_k,
                dropout_p=dropout,
                softmax_scale=softmax_scale,
                causal=causal,
            )

            attn_output = pad_input(attn_output_unpad, indices_q, batch_size, query_length)
        else:
            attn_output = flash_attn_func(query_states, key_states, value_states, dropout, softmax_scale=softmax_scale, causal=causal)

        return attn_output

    def _upad_input(self, query_layer, key_layer, value_layer, attention_mask, q_attnetion_mask, query_length, key_length):
        indices_k, cu_seqlens_k, max_seqlen_in_batch_k = _get_unpad_data(attention_mask)
        batch_size, kv_seq_len, num_key_value_heads, head_dim = key_layer.shape

        key_layer = index_first_axis(key_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k)
        value_layer = index_first_axis(value_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k)
        if query_length == kv_seq_len:
            query_layer = index_first_axis(query_layer.reshape(batch_size * kv_seq_len, self.num_attention_heads, head_dim), indices_k)
            cu_seqlens_q = cu_seqlens_k
            max_seqlen_in_batch_q = max_seqlen_in_batch_k
            indices_q = indices_k
        elif query_length == 1:
            max_seqlen_in_batch_q = 1
            cu_seqlens_q = torch.arange(batch_size + 1, dtype=torch.int32, device=query_layer.device)
            indices_q = cu_seqlens_q[:-1]
            query_layer = query_layer.squeeze(1)
        else:
            q_attnetion_mask = q_attnetion_mask[:, -query_length:]
            query_layer, indices_q, cu_seqlens_q, max_seqlen_in_batch_q = unpad_input(query_layer, q_attnetion_mask)

        return (
            query_layer,
            key_layer,
            value_layer,
            indices_q,
            (cu_seqlens_q, cu_seqlens_k),
            (max_seqlen_in_batch_q, max_seqlen_in_batch_k),
        )

def _get_unpad_data(attention_mask):
    seqlens_in_batch = attention_mask.sum(dim=-1, dtype=torch.int32)
    indices = torch.nonzero(attention_mask.flatten(), as_tuple=False).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = F.pad(torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.torch.int32), (1, 0))
    return (
        indices,
        cu_seqlens,
        max_seqlen_in_batch,
    )