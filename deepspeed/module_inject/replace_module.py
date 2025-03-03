import os
import torch
import tqdm
import deepspeed
import deepspeed.ops.transformer as transformer_inference
from .replace_policy import HFBertLayerPolicy, HFGPT2LayerPolicy, BLOOMLayerPolicy
from .replace_policy import replace_policies
#from ..runtime.weight_quantizer import WeightQuantization
from deepspeed import comm as dist
from torch import nn

from ..runtime.zero import GatheredParameters
from .layers import LinearAllreduce, LinearLayer
from .load_checkpoint import load_model_with_checkpoint
import time


class ReplaceWithTensorSlicing:
    def __init__(self, mp_group=None, mp_size=1, out_dim=1, in_dim=0):
        if mp_group is not None:
            self.gpu_index = dist.get_rank(group=mp_group)
        else:
            self.gpu_index = 0
        self.out_dim = out_dim
        self.in_dim = in_dim
        self.mp_size = mp_size

    def merge_assert(self, dim1, dim2):
        assert dim1 > dim2, \
            'Merging tensors is not allowed here! Please use deepspeed load_checkpoint\
            for merging your checkpoints before replacing the transformer layer with\
            inference-kernels'

    def qkv_copy(self, dst, src):
        if src is None:
            return src
        src_shape = src.shape
        dst_shape = dst.shape
        if self.out_dim == 0:
            src_split = torch.split(src.data,
                                    src_shape[self.out_dim] // self.mp_size,
                                    dim=0)
        else:
            src_split = torch.split(src.data, src.shape[-1] // 3, dim=-1)

        if (len(src_shape) == 2 and len(dst_shape) == 2):
            if src_shape[self.out_dim] == dst_shape[self.out_dim]:
                return torch.nn.parameter.Parameter(src)
            if self.out_dim == 1:
                self.merge_assert(src_shape[self.out_dim], dst_shape[self.out_dim])
                qkv_size = dst_shape[self.out_dim] // 3
                qkv_split = [
                    torch.split(src_s,
                                qkv_size,
                                dim=self.out_dim) for src_s in src_split
                ]

                weight_split = [
                    torch.cat([qkv_s[i] for qkv_s in qkv_split],
                              axis=self.out_dim) for i in range(len(qkv_split[0]))
                ]
                dst.data.copy_(weight_split[self.gpu_index].to(
                    torch.cuda.current_device()).contiguous())
            else:
                dst.data.copy_(src_split[self.gpu_index].to(
                    torch.cuda.current_device()).contiguous())
        else:
            if src_shape[0] == dst_shape[0]:
                return torch.nn.parameter.Parameter(src)
            if self.out_dim == 1:
                qkv_size = dst_shape[0] // 3
                qkv_split = [torch.split(src_s, qkv_size, dim=0) for src_s in src_split]
                bias_split = [
                    torch.cat([qkv_s[i] for qkv_s in qkv_split],
                              axis=0) for i in range(len(qkv_split[0]))
                ]
                dst.data.copy_(bias_split[self.gpu_index].to(
                    torch.cuda.current_device()).contiguous())
            else:
                dst.data.copy_(src_split[self.gpu_index].to(
                    torch.cuda.current_device()).contiguous())

        return torch.nn.parameter.Parameter(dst)

    def copy(self, dst, src):
        if src is None:
            return src
        src_shape = src.shape
        dst_shape = dst.shape
        if (len(src_shape) == 2 and len(dst_shape) == 2):

            if src_shape[0] == dst_shape[0] and src_shape[1] == dst_shape[1]:
                dst.data.copy_(src)
            else:
                if src_shape[self.in_dim] != dst_shape[self.in_dim]:
                    self.merge_assert(src_shape[self.in_dim], dst_shape[self.in_dim])
                    weight_split = torch.split(
                        src,
                        dst_shape[self.in_dim],
                        dim=self.in_dim)[self.gpu_index].to(
                            torch.cuda.current_device()).contiguous()
                else:
                    self.merge_assert(src_shape[self.out_dim], dst_shape[self.out_dim])
                    weight_split = torch.split(
                        src.data,
                        dst_shape[self.out_dim],
                        dim=self.out_dim)[self.gpu_index].to(
                            torch.cuda.current_device()).contiguous()
                dst.data.copy_(weight_split.contiguous())
        else:
            if src_shape[0] == dst_shape[0]:
                dst.data.copy_(src)
            else:
                bias_split = torch.split(src.data,
                                         dst_shape[-1])[self.gpu_index].to(
                                             torch.cuda.current_device()).contiguous()
                dst.data.copy_(bias_split)
        dst = torch.nn.parameter.Parameter(dst, requires_grad=False)
        if hasattr(src, 'scale'):
            dst.scale = src.scale
        return dst


def get_transformer_name(replaced_module):
    from .replace_policy import supported_models
    from torch.nn import ModuleList
    transformer_name = ''
    for n, c in replaced_module.named_children():
        if c.__class__ in supported_models:
            transformer_name += n + '.'
            for name, child in c.named_children():
                if child.__class__ is ModuleList:
                    transformer_name += name
                    break
            break
    return transformer_name


class GroupQuantizer:
    def __init__(self, q_int8=True, num_groups=32, group_size=32, num_bits=8):
        self.num_groups = num_groups
        self.group_size = group_size
        self.num_bits = num_bits
        self.q_int8 = q_int8

    def quantize(self, inputs, qkv=True, count=1, parallel_dim=0):
        if not self.q_int8 or not qkv:
            inputs = torch.nn.Parameter(inputs, requires_grad=False)
            inputs.scale = torch.empty(1)
            return inputs
        q_range = 2**self.num_bits
        inputs = inputs.to(torch.cuda.current_device())
        input_flat = inputs.reshape(self.num_groups, -1).contiguous()
        input_min = torch.min(input_flat, dim=1, keepdim=True)[0].float()
        input_max = torch.max(input_flat, dim=1, keepdim=True)[0].float()
        scale = torch.max(input_min.abs(), input_max.abs()) * 2.0 / (q_range)
        input_flat = (input_flat / scale).round().clamp(-q_range // 2, q_range // 2 - 1)
        inputs_q = input_flat.reshape(inputs.shape).to(torch.int8).contiguous()
        out = torch.nn.Parameter(inputs_q, requires_grad=False)
        #print(inputs.shape)
        inputs_split = inputs.split(inputs.shape[parallel_dim] // 2, dim=parallel_dim)
        input_flat = [
            inputs_split[i].reshape(self.num_groups,
                                    -1).contiguous() for i in range(2)
        ]
        input_min = [
            torch.min(input_flat[i],
                      dim=1,
                      keepdim=True)[0].float() for i in range(2)
        ]
        input_max = [
            torch.max(input_flat[i],
                      dim=1,
                      keepdim=True)[0].float() for i in range(2)
        ]
        scale1 = [
            (torch.max(input_min[i].abs(),
                       input_max[i].abs()) * 2.0 / (q_range)).squeeze().unsqueeze(0)
            for i in range(2)
        ]

        out.scale = torch.cat([scale.squeeze().unsqueeze(0),
                               scale1[0],
                               scale1[1]],
                              dim=0).reshape(self.num_groups,
                                             -1).contiguous()
        return out


def replace_transformer_layer(orig_layer_impl,
                              model,
                              policy=None,
                              micro_batch_size=-1,
                              config=None,
                              seed=-1,
                              hidden_size=-1,
                              num_attention_heads=-1,
                              mp_size=1,
                              training_mp_size=1,
                              mp_group=None,
                              ep_group=None,
                              expert_mp_group=None,
                              fp16=True,
                              local_rank=-1,
                              stochastic_mode=True,
                              training=True,
                              quantize=False,
                              quantize_settings=None,
                              triangular_masking=False,
                              return_tuple=True,
                              replace_with_kernel_inject=False,
                              linear_layer_setting=None,
                              moe=False,
                              moe_experts=1,
                              moe_type='standard',
                              checkpoint_dict=None,
                              save_mp_checkpoint_path=None,
                              base_dir=""):
    """ Replace bert-style transformer layers with DeepSpeed's transformer layer
    Arguments:
        orig_layer_impl (torch.nn.Module): the original transformer layer implementation to look for,
            e.g., transformers.modeling_bert.BertLayer.
        model (torch.nn.Module): user's nn.module representing their model
        policy: shows the policy for mapping from the orig_layer_impl to transformer parameters when
            replace_with_kernel_inject is set, otherwise, it provides the names of two linear layers as
            a tuple: (attention_output projection, transformer output projection)
        micro_batch_size (int): micro batch size per gpu used during training/eval
        config (dict): model config containing hidden size, attention heads, etc.
        seed (int): random seed value
        max_seq_length (int): max sequence length for training
        hidden_size (int): hidden dimension
        num_attention_heads (int): number of attention heads
        mp_size (int): model_parallelism degree
        mp_group : model_parallel group initialized on the modeling side
        preln (bool): does the original layer implementation do pre or post layer norm?
        fp16 (bool): fp16 or fp32
        local_rank (int): GPU rank (optional),
        stochastic_mode (bool): whether to use stochastic mode
        training (bool): specifying whether kernel-injection is done for training/inference (set to false for inference-mode injection)
        quantize_settings (tuple): this setting shows how we can quantize a model for running it through the inference kernels.
                It includes (quantization_scales, merge_count, mlp_extra_grouping, quantize_groups).
        return_tuple (bool): if set, transformer layer returns a tuple as the output.
            Note: this flag needs to be set for huggingface models.
        replace_with_kernel_inject (bool): injection_mode, if true, kernels will be add along with configuring
            Tensor-Parallelism
        linear_layer_setting (tuple of modules) [Optional]: shows which two classes are used for linear layers
            and embedding layers
        attention_params: (list of strings) [Optional]: shows the parameters in the attention part that needs to
            be adjusted based on the model-parallelism
    Returns:
        Updated nn.module with replaced transformer layers
    """
    mp_replace = ReplaceWithTensorSlicing(mp_group=mp_group,
                                          mp_size=mp_size)  #, out_dim=0, in_dim=1)

    def replace_with_policy(child,
                            policy_cls,
                            triangular_masking,
                            inference=False,
                            layer_id=0):
        policy = policy_cls(child, inference=inference)

        if inference:
            hidden_size, num_attention_heads = policy.get_hidden_heads()
            assert num_attention_heads % mp_size == 0,\
                "To run the model parallel across the GPUs, the attention_heads require to be divisible by the world_size!" +\
                "This is because the attention computation is partitioned evenly among the parallel GPUs."
        from deepspeed.moe.layer import MoE
        moe = False
        if hasattr(child, 'mlp') and isinstance(child.mlp, MoE):
            num_experts = child.mlp.num_experts
            moe = True

        attn_linear_layer, qkvw, qkvb, dense_w, dense_b, scale_attention, megatron_v2 = policy.attention()
        if not moe or moe_type == 'standard':
            mlp_linear_layer, _h4h_w, _h4h_b, _4hh_w, _4hh_b = policy.mlp()
        else:
            mlp_linear_layer, _h4h_w, _h4h_b, _4hh_w, _4hh_b, \
                _res_h4h_w, _res_h4h_b, _res_4hh_w, _res_4hh_b, _res_coef = policy.mlp(moe_type)

        attn_nw, attn_nb, input_nw, input_nb = policy.layerNorm()
        if False:
            if policy_cls is not HFBertLayerPolicy:
                qkvw = qkvw.to(torch.int8)
            dense_w = dense_w.to(torch.int8)
            _h4h_w = [moe_w1.to(torch.int8)
                      for moe_w1 in _h4h_w] if moe else _h4h_w.to(torch.int8)
            _4hh_w = [moe_w1.to(torch.int8)
                      for moe_w1 in _4hh_w] if moe else _4hh_w.to(torch.int8)
        elif fp16:
            qkvw = qkvw.half()
            dense_w = dense_w.half()
            _h4h_w = [moe_w1.half() for moe_w1 in _h4h_w] if moe else _h4h_w.half()
            _4hh_w = [moe_w1.half() for moe_w1 in _4hh_w] if moe else _4hh_w.half()
        if quantize or fp16:
            qkvb = qkvb if qkvb is None else qkvb.half()
            dense_b = dense_b if dense_b is None else dense_b.half()
            _h4h_b = [moe_b1.half() for moe_b1 in _h4h_b] if moe else _h4h_b.half()
            _4hh_b = [moe_b1.half() for moe_b1 in _4hh_b] if moe else _4hh_b.half()
            attn_nw = attn_nw if attn_nw is None else attn_nw.half()
            attn_nb = attn_nb if attn_nb is None else attn_nb.half()
            input_nw = input_nw.half()
            input_nb = input_nb.half()

        if moe and moe_type == 'residual' and fp16:
            _res_h4h_b = _res_h4h_b.half()
            _res_4hh_b = _res_4hh_b.half()
            _res_h4h_w = _res_h4h_w.half()
            _res_4hh_w = _res_4hh_w.half()
            _res_coef = _res_coef.half()

        #expert_mp_replace = ReplaceWithTensorSlicing(mp_group=expert_mp_group)

        quantizer = GroupQuantizer(q_int8=quantize)
        if inference:
            if moe:
                ep_world_size = dist.get_world_size()
                local_ep_size = 1 if num_experts < ep_world_size else num_experts // ep_world_size

                transformer_config = transformer_inference.DeepSpeedMoEInferenceConfig(
                    hidden_size=hidden_size,
                    heads=num_attention_heads,
                    layer_norm_eps=config.layer_norm_eps if hasattr(
                        config,
                        'layer_norm_eps') else 1e-12,
                    fp16=fp16,
                    pre_layer_norm=policy.pre_attn_norm,
                    mp_size=mp_size,
                    q_int8=quantize,
                    moe_experts=local_ep_size,
                    global_experts=num_experts,
                    mlp_type=moe_type)
            else:
                rotary_dim = config.rotary_dim if hasattr(config, 'rotary_dim') else child.attention.rotary_ndims \
                                            if hasattr(child, 'attention') and hasattr(child.attention,'rotary_ndims') else -1
                bigscience_bloom = policy_cls is BLOOMLayerPolicy
                transformer_config = transformer_inference.DeepSpeedInferenceConfig(
                    hidden_size=hidden_size,
                    heads=num_attention_heads,
                    layer_norm_eps=config.layer_norm_eps if hasattr(
                        config,
                        'layer_norm_eps') else
                    (config.layer_norm_epsilon
                     if hasattr(config,
                                'layer_norm_epsilon') else config.layernorm_epsilon
                     if hasattr(config,
                                'layernorm_epsilon') else 1.0e-12),
                    fp16=fp16,
                    pre_layer_norm=policy.pre_attn_norm,
                    mp_size=mp_size,
                    q_int8=quantize,
                    return_tuple=(return_tuple or (policy_cls is HFBertLayerPolicy)),
                    triangular_masking=(policy_cls is not HFBertLayerPolicy),
                    local_attention=((config.attention_layers[layer_id] == "local")
                                     if hasattr(config,
                                                'attention_layers') else False),
                    window_size=(config.window_size if hasattr(config,
                                                               'window_size') else 1),
                    rotary_dim=rotary_dim,
                    mlp_after_attn=(rotary_dim is None or rotary_dim < 0),
                    mlp_act_func_type=policy.mlp_act_func_type,
                    training_mp_size=training_mp_size,
                    bigscience_bloom=bigscience_bloom)

            if quantize and quantize_settings is not None:
                (quantization_scales,
                 merge_count,
                 mlp_extra_grouping,
                 quantize_groups) = quantize_settings
                if moe:
                    new_module = transformer_inference.DeepSpeedMoEInference(
                        transformer_config,
                        mp_group=mp_group,
                        ep_group=None if ep_group is None else ep_group[num_experts],
                        expert_mp_group=None
                        if expert_mp_group is None else expert_mp_group[num_experts],
                        quantize_scales=quantization_scales[layer_id],
                        quantize_groups=quantize_groups,
                        merge_count=merge_count,
                        mlp_extra_grouping=mlp_extra_grouping,
                        qkv_merging=(policy_cls is HFBertLayerPolicy))

                else:
                    new_module = transformer_inference.DeepSpeedTransformerInference(
                        transformer_config,
                        mp_group=mp_group,
                        #quantize_scales=quantization_scales[layer_id],
                        quantize_groups=quantize_groups,
                        merge_count=merge_count,
                        mlp_extra_grouping=mlp_extra_grouping,
                        qkv_merging=(policy_cls is HFBertLayerPolicy))

                #if quantize and qkvw.dtype != torch.int8:
                #    quantize_bits = 8
                #    quantizer = WeightQuantization()
                #    if policy_cls is HFBertLayerPolicy:
                #        data_quantized, _ = quantizer.quantize_data(qkvw.data, quantize_bits, quantize_groups * 3)
                #    else:
                #        data_quantized, _ = quantizer.quantize_data(qkvw.data, quantize_bits, quantize_groups)
                #    qkvw.data.copy_(data_quantized)
                #    qkvw.data = qkvw.data.to(torch.int8)
            else:

                if moe:
                    new_module = transformer_inference.DeepSpeedMoEInference(
                        transformer_config,
                        mp_group=mp_group,
                        ep_group=None if ep_group is None else ep_group[num_experts],
                        expert_mp_group=None
                        if expert_mp_group is None else expert_mp_group[num_experts],
                    )

                else:
                    new_module = transformer_inference.DeepSpeedTransformerInference(
                        transformer_config,
                        mp_group=mp_group,
                    )
            new_module.config.scale_attention = scale_attention

            # we want the weights in [input, output] shape
            # linear layer is created with [input, output] shape
            # transpose it here to reduce inference cost!
            def transpose(data):
                # temp move to cpu to avoid requiring extra GPU memory during the reshape
                data = data.to('cpu')
                data.reshape(-1).copy_(data.transpose(-1, -2).contiguous().reshape(-1))
                data = data.reshape(data.shape[-1], data.shape[-2])
                data.to(torch.cuda.current_device())
                return data

            attn_block = new_module.attention
            mpl_block = new_module.mlp

            if attn_linear_layer:
                if qkvw.numel() == 0 or qkvw.is_meta:
                    if qkvw.is_meta or qkvw.ds_tensor.numel(
                    ) < attn_block.attn_qkvw.numel():
                        pass
                    else:
                        with GatheredParameters([qkvw,
                                                 dense_w,
                                                 qkvb,
                                                 dense_b],
                                                modifier_rank=0):
                            qkvw = transpose(qkvw.data)
                            dense_w = transpose(dense_w.data)
                            qkvb = qkvb.data
                            dense_b = dense_b.data
                else:
                    qkvw.data = transpose(qkvw.data)
                    dense_w.data = transpose(dense_w.data)

            def _transpose(x):
                num_attention_heads_per_partition = transformer_config.heads // transformer_config.mp_size
                attention_head_size = x.shape[-1] // num_attention_heads_per_partition
                new_x_shape = x.size()[:-1] + (num_attention_heads_per_partition,
                                               attention_head_size)
                x_1 = x.view(*new_x_shape)
                (q, k, v) = torch.split(x_1, (x_1.shape[-1] // 3), dim=(x_1.dim() - 1))
                if len(q.shape) > 2:
                    return torch.cat((q.reshape(q.shape[0],
                                                -1),
                                      k.reshape(q.shape[0],
                                                -1),
                                      v.reshape(q.shape[0],
                                                -1)),
                                     dim=-1).reshape(x.shape)
                else:
                    return torch.cat((q.reshape(-1),
                                      k.reshape(-1),
                                      v.reshape(-1)),
                                     dim=-1).reshape(x.shape)

            if megatron_v2:
                new_module.config.rotate_half = True
                new_module.config.rotate_every_two = False

                # Note: this part needs to be added for BLOOM architecture
                qkvw = torch.nn.parameter.Parameter(_transpose(qkvw).contiguous())
                qkvb = torch.nn.parameter.Parameter(_transpose(qkvb).contiguous())

            # NOTE: This part caused instability in the multi-GPU inference!
            # TODO: This needs to be incorporated in the kernels.
            #dense_b = dense_b if dense_b is None else dense_b * (
            #    transformer_config.training_mp_size / transformer_config.mp_size)
            #_4hh_b = _4hh_b * (transformer_config.training_mp_size /
            #                   transformer_config.mp_size)

            if mlp_linear_layer:
                if not moe and (_4hh_w.numel() == 0 or _4hh_w.is_meta):
                    if _4hh_w.is_meta or _4hh_w.ds_tensor.numel(
                    ) < mpl_block.inter_w.numel():
                        pass
                    else:
                        with GatheredParameters([_h4h_w,
                                                 _4hh_w,
                                                 _4hh_b,
                                                 _h4h_b],
                                                modifier_rank=0):
                            _h4h_w = transpose(_h4h_w.data)
                            _4hh_w = transpose(_4hh_w.data)
                            _h4h_b = _h4h_b.data
                            _4hh_b = _4hh_b.data
                else:
                    _h4h_w = [transpose(moe_w1.data)
                              for moe_w1 in _h4h_w] if moe else transpose(_h4h_w.data)
                    _4hh_w = [transpose(moe_w1.data)
                              for moe_w1 in _4hh_w] if moe else transpose(_4hh_w.data)

            if moe and moe_type == 'residual':
                _res_h4h_w.data = transpose(_res_h4h_w.data)
                _res_4hh_w.data = transpose(_res_4hh_w.data)
                _res_coef.data = transpose(_res_coef.data)

            if qkvw.is_meta or qkvw.numel() == 0 or qkvw.is_meta:
                if qkvw.is_meta or qkvw.ds_tensor.numel() < attn_block.attn_qkvw.numel():
                    pass
                else:
                    with GatheredParameters([
                            attn_block.attn_qkvw,
                            attn_block.attn_qkvb,
                            attn_block.attn_ow,
                            attn_block.attn_ob
                    ],
                                            modifier_rank=0):
                        attn_block.attn_qkvw = mp_replace.copy(
                            attn_block.attn_qkvw,
                            qkvw)
                        attn_block.attn_qkvb = mp_replace.copy(
                            attn_block.attn_qkvb,
                            qkvb)

                        attn_block.attn_ow = mp_replace.copy(attn_block.attn_ow, dense_w)
                        attn_block.attn_ob = mp_replace.copy(attn_block.attn_ob, dense_b)
            else:
                attn_block.attn_qkvw = quantizer.quantize(
                    mp_replace.copy(attn_block.attn_qkvw, qkvw) if bigscience_bloom else \
                    mp_replace.qkv_copy(attn_block.attn_qkvw, qkvw))
                attn_block.attn_qkvb = \
                    mp_replace.copy(attn_block.attn_qkvb, qkvb) if bigscience_bloom else \
                    mp_replace.qkv_copy(attn_block.attn_qkvb, qkvb)

                attn_block.attn_ow = quantizer.quantize(
                    mp_replace.copy(attn_block.attn_ow,
                                    dense_w))

                attn_block.attn_ob = mp_replace.copy(attn_block.attn_ob, dense_b)

            if moe:
                gpu_index = dist.get_rank()
                gpu_index = 0
                for ep_index in range(local_ep_size):
                    mpl_block[ep_index].inter_w.data = _h4h_w[
                        gpu_index * local_ep_size + ep_index].to(
                            torch.cuda.current_device())
                    mpl_block[ep_index].inter_b.data = _h4h_b[
                        gpu_index * local_ep_size + ep_index].to(
                            torch.cuda.current_device())
                    mpl_block[ep_index].output_w.data = _4hh_w[
                        gpu_index * local_ep_size + ep_index].to(
                            torch.cuda.current_device())
                    mpl_block[ep_index].output_b.data = _4hh_b[
                        gpu_index * local_ep_size + ep_index].to(
                            torch.cuda.current_device())
                new_module.attn_nw.data = attn_nw.to(torch.cuda.current_device())
                new_module.attn_nb.data = attn_nb.to(torch.cuda.current_device())
                if moe_type == 'residual':
                    new_module.res_mlp.inter_w.data = _res_h4h_w.to(
                        torch.cuda.current_device())
                    new_module.res_mlp.inter_b.data = _res_h4h_b.to(
                        torch.cuda.current_device())
                    new_module.res_mlp.output_w.data = _res_4hh_w.to(
                        torch.cuda.current_device())
                    new_module.res_mlp.output_b.data = _res_4hh_b.to(
                        torch.cuda.current_device())
                    new_module.res_coef.data = _res_coef.to(torch.cuda.current_device())
            else:

                if _4hh_w.numel() == 0 or _4hh_w.is_meta:
                    if _4hh_w.is_meta or _4hh_w.ds_tensor.numel(
                    ) < mpl_block.inter_w.numel():
                        pass
                    else:
                        with GatheredParameters([_h4h_w,
                                                 _4hh_w,
                                                 _4hh_w,
                                                 _4hh_b],
                                                modifier_rank=0):
                            mpl_block.inter_w = mp_replace.copy(
                                mpl_block.inter_w,
                                _h4h_w)
                            mpl_block.inter_b = mp_replace.copy(
                                mpl_block.inter_b,
                                _h4h_b)
                            mpl_block.output_w = mp_replace.copy(
                                mpl_block.output_w,
                                _4hh_w)
                            mpl_block.output_b = mp_replace.copy(
                                mpl_block.output_b,
                                _4hh_b)
                else:
                    mpl_block.inter_w = quantizer.quantize(
                        mp_replace.copy(mpl_block.inter_w,
                                        _h4h_w))
                    mpl_block.inter_b = mp_replace.copy(mpl_block.inter_b, _h4h_b)
                    mpl_block.output_w = quantizer.quantize(
                        mp_replace.copy(mpl_block.output_w,
                                        _4hh_w))
                    mpl_block.output_b = mp_replace.copy(mpl_block.output_b, _4hh_b)

                if attn_nw is None:
                    new_module.mlp.attn_nw = attn_nw
                    new_module.mlp.attn_nb = attn_nb
                else:
                    if attn_nw.is_meta or attn_nw.numel() == 0:
                        if attn_nw.is_meta or attn_nw.ds_tensor.numel(
                        ) < new_module.mlp.attn_nw.numel():
                            pass
                        else:
                            with GatheredParameters([attn_nw, attn_nb], modifier_rank=0):
                                new_module.mlp.attn_nw.data.copy_(
                                    attn_nw.to(torch.cuda.current_device()))
                                new_module.mlp.attn_nb.data.copy_(
                                    attn_nb.to(torch.cuda.current_device()))
                    else:
                        new_module.mlp.attn_nw.data.copy_(
                            attn_nw.to(torch.cuda.current_device()))
                        new_module.mlp.attn_nb.data.copy_(
                            attn_nb.to(torch.cuda.current_device()))

            if input_nw.is_meta or input_nw.numel() == 0:
                if input_nw.is_meta or input_nw.ds_tensor.numel(
                ) < new_module.norm_w.numel():
                    pass
                else:
                    with GatheredParameters([input_nw, input_nb], modifier_rank=0):
                        new_module.norm_w.data.copy_(
                            input_nw.to(torch.cuda.current_device()))
                        new_module.norm_b.data.copy_(
                            input_nb.to(torch.cuda.current_device()))
            else:
                new_module.norm_w.data.copy_(input_nw.to(torch.cuda.current_device()))
                new_module.norm_b.data.copy_(input_nb.to(torch.cuda.current_device()))
        else:
            transformer_config = deepspeed.DeepSpeedTransformerConfig(
                batch_size=micro_batch_size if micro_batch_size > 0 else 1,
                hidden_size=config.hidden_size,
                heads=config.num_attention_heads,
                attn_dropout_ratio=config.attention_probs_dropout_prob,
                hidden_dropout_ratio=config.hidden_dropout_prob,
                num_hidden_layers=config.num_hidden_layers,
                initializer_range=config.initializer_range,
                layer_norm_eps=config.layer_norm_eps if hasattr(
                    config,
                    'layer_norm_eps') else 1e-12,
                seed=seed,
                fp16=fp16,
                pre_layer_norm=policy.pre_attn_norm,
                return_tuple=return_tuple,
                local_rank=local_rank,
                stochastic_mode=stochastic_mode,
                normalize_invertible=True,
                training=training)
            new_module = deepspeed.DeepSpeedTransformerLayer(transformer_config)
            new_module.attn_qkvw.data = qkvw
            new_module.attn_qkvb.data = qkvb
            new_module.attn_ow.data = dense_w
            new_module.attn_ob.data = dense_b

            new_module.attn_nw.data = attn_nw
            new_module.attn_nb.data = attn_nb
            new_module.norm_w.data = input_nw
            new_module.norm_b.data = input_nb

            new_module.inter_w.data = _h4h_w
            new_module.inter_b.data = _h4h_b
            new_module.output_w.data = _4hh_w
            new_module.output_b.data = _4hh_b
        return new_module

    def replace_wo_policy(module, all_reduce_linears):
        def _replace(child, name, conv_linear_layer):
            mp_replace = ReplaceWithTensorSlicing(mp_group=mp_group)
            z_inference = (len(list(child.parameters())) > 0) and (list(
                child.parameters())[0].numel() == 0)
            if z_inference:
                weight_shape = child.weight.ds_shape
            else:
                weight_shape = child.weight.shape
            if name in all_reduce_linears:
                new_weight = torch.empty((
                    weight_shape[1] if conv_linear_layer else weight_shape[0],
                    (weight_shape[0] if conv_linear_layer else weight_shape[1]) //
                    mp_size,
                ),
                                         device=child.weight.device,
                                         dtype=child.weight.dtype)
                if z_inference:
                    with deepspeed.zero.GatheredParameters(child.weight,
                                                           modifier_rank=0):
                        data = child.weight.data.to(new_weight.device)
                        if conv_linear_layer:
                            data = data.transpose(-1, -2).contiguous()
                        data = mp_replace.copy(new_weight, data)
                    child.weight.ds_tensor = torch.empty(1)
                else:
                    if conv_linear_layer:
                        child.weight.data = child.weight.data.transpose(-1,
                                                                        -2).contiguous()
                    data = mp_replace.copy(new_weight, child.weight.data)
                new_bias = torch.empty((weight_shape[0]),
                                       device=child.weight.device,
                                       dtype=child.weight.dtype)
                if z_inference:
                    with deepspeed.zero.GatheredParameters(child.bias, modifier_rank=0):
                        new_bias.data.copy_(child.bias.data)
                elif child.bias is not None:
                    new_bias.data.copy_(child.bias.data)
                return LinearAllreduce(data, child.bias if child.bias is None else \
                            torch.nn.parameter.Parameter(new_bias.to(torch.cuda.current_device())), mp_group)
            else:
                new_weight = torch.empty((
                    (weight_shape[1] if conv_linear_layer else weight_shape[0]) //
                    mp_size,
                    weight_shape[0] // mp_size if conv_linear_layer else weight_shape[1],
                ),
                                         device=child.weight.device,
                                         dtype=child.weight.dtype)
                if z_inference:
                    with deepspeed.zero.GatheredParameters(child.weight,
                                                           modifier_rank=0):
                        data = child.weight.data.to(new_weight.device)
                        if conv_linear_layer:
                            data = data.transpose(-1, -2).contiguous()
                        data = mp_replace.copy(new_weight, data)
                    child.weight.ds_tensor = torch.empty(1)
                else:
                    if conv_linear_layer:
                        child.weight.data = child.weight.data.transpose(-1,
                                                                        -2).contiguous()
                    data = mp_replace.copy(new_weight, child.weight.data)

                new_bias = torch.empty((weight_shape[0] // mp_size),
                                       device=child.weight.device,
                                       dtype=child.weight.dtype)
                if z_inference:
                    with deepspeed.zero.GatheredParameters(child.bias, modifier_rank=0):
                        bias_data = None if child.bias is None else mp_replace.copy(
                            new_bias,
                            child.bias.data).to(torch.cuda.current_device())
                else:
                    bias_data = None if child.bias is None else mp_replace.copy(
                        new_bias,
                        child.bias.data).to(torch.cuda.current_device())
                return LinearLayer(weight=data.to(torch.cuda.current_device()),
                                   bias=bias_data)

        def _slice_embedding(child, name, conv_linear_layer):
            mp_replace = ReplaceWithTensorSlicing(mp_group=mp_group)
            new_weight = torch.empty((child.weight.shape[0],
                                      child.weight.shape[1] // mp_size),
                                     device=child.weight.device,
                                     dtype=child.weight.dtype)
            data = mp_replace.copy(new_weight,
                                   child.weight.ds_tensor.data if hasattr(child.weight, 'ds_tensor') else \
                                   child.weight.data)
            new_embedding = nn.Embedding(child.weight.shape[0],
                                         child.weight.shape[1] // mp_size)
            new_embedding.weight.data.copy_(data)
            return new_embedding

        def update_mp_params(child):
            if hasattr(child, 'n_heads'):
                child.n_heads = child.n_heads // mp_size
            if hasattr(child, 'inner_dim'):
                child.inner_dim = child.inner_dim // mp_size
            if hasattr(child, 'num_heads'):
                child.num_heads = child.num_heads // mp_size
            if hasattr(child, 'num_attention_heads'):
                child.num_attention_heads = child.num_attention_heads // mp_size
            if hasattr(child, 'all_head_size'):
                child.all_head_size = child.all_head_size // mp_size
            if hasattr(child, 'embed_dim'):
                child.embed_dim = child.embed_dim // mp_size
            if hasattr(child, 'hidden_size'):
                child.hidden_size = child.hidden_size // mp_size

        conv_linear_layer = False
        if linear_layer_setting is not None:
            linear_policies = {linear_layer_setting[0]: _replace}
            if len(linear_layer_setting) == 2:
                linear_policies.update({linear_layer_setting[1]: _slice_embedding})
        else:
            if orig_layer_impl is HFGPT2LayerPolicy._orig_layer_class:
                try:
                    import transformers
                    conv_linear_layer = True
                    linear_policies = {transformers.model_utils.Conv1D: _replace}
                except ImportError:
                    linear_policies = {nn.Linear: _replace}
            else:
                linear_policies = {nn.Linear: _replace, nn.Embedding: _slice_embedding}

        def _replace_module(r_module, prev_name=''):
            for name, child in r_module.named_children():
                if child.__class__ in linear_policies:
                    setattr(
                        r_module,
                        name,
                        linear_policies[child.__class__](child,
                                                         prev_name + '.' + name,
                                                         conv_linear_layer))
                else:
                    update_mp_params(child)
                    _replace_module(child, name)
            return r_module

        return _replace_module(module)

    def replace_fn(child, _policy, layer_id=0):
        if training:
            # copy relevant state from child -> new module
            new_module = replace_with_policy(child, _policy, triangular_masking)

        else:
            # copy relevant state from child -> new module
            if replace_with_kernel_inject:
                new_module = replace_with_policy(child,
                                                 _policy,
                                                 triangular_masking,
                                                 inference=True,
                                                 layer_id=layer_id)
            else:
                new_module = replace_wo_policy(child, _policy)

        return new_module

    replaced_module = replace_module(model=model,
                                     orig_class=orig_layer_impl,
                                     replace_fn=replace_fn,
                                     _replace_policy=policy)

    quantizer = GroupQuantizer(q_int8=quantize)
    world_size = dist.get_world_size() if dist.is_initialized() else 1
    rank = dist.get_rank() if dist.is_initialized() else 0
    if checkpoint_dict is not None:
        start_time = time.time()
        checkpoint = checkpoint_dict['checkpoints']
        ckpt_list = checkpoint["tp"] if type(checkpoint) is dict else checkpoint
        ckpt_type = checkpoint_dict.get('parallelization', 'pp')
        ckpt_mp_size = checkpoint_dict.get('tp_size', len(ckpt_list))
        ckpt_mp_size = checkpoint_dict.get('mp_size', ckpt_mp_size)
        base_dir1 = checkpoint_dict.get('base_dir', base_dir)

        if ckpt_type == 'pp' and type(checkpoint) is list:
            pbar = tqdm.tqdm(total=len(checkpoint),
                             desc=f"Loading {len(checkpoint)} checkpoint shards")

            for i in range(len(checkpoint)):

                sd = [
                    torch.load(os.path.join(base_dir1,
                                            checkpoint[i]),
                               map_location='cpu')
                ]
                load_model_with_checkpoint(
                    replaced_module,
                    sd,
                    mp_replace,
                    ckpt_type,
                    quantizer,
                )
        else:
            import gc
            num_checkpoints = len(ckpt_list) // ckpt_mp_size
            tp_split_size = (world_size / ckpt_mp_size)
            sd_offset = int(rank / tp_split_size)
            sd_count = int((rank + max(1, tp_split_size)) / tp_split_size) - sd_offset
            pbar = tqdm.tqdm(total=num_checkpoints,
                             desc=f"Loading {num_checkpoints} checkpoint shards")
            for i in range(num_checkpoints):
                pbar.update(1)
                ckpt_index = i * ckpt_mp_size + sd_offset
                ckpt_files = [
                    os.path.join(base_dir1,
                                 ckpt_list[ckpt_index +
                                           j]) if base_dir1 else ckpt_list[ckpt_index +
                                                                           j]
                    for j in range(sd_count)
                ]
                sds = [
                    torch.load(ckpt_file,
                               map_location='cpu') for ckpt_file in ckpt_files
                ]
                load_model_with_checkpoint(replaced_module,
                                           sds,
                                           mp_replace,
                                           ckpt_type,
                                           quantizer,
                                           int(rank % tp_split_size))
                sds = [None for _ in sds]
                gc.collect()

            if "non_tp" in checkpoint:
                pbar = tqdm.tqdm(
                    total=len(checkpoint["non_tp"]),
                    desc=f"Loading {len(checkpoint['non_tp'])} checkpoint shards")

                for i in range(len(checkpoint["non_tp"])):
                    pbar.update(1)
                    ckpt_file = os.path.join(base_dir1,
                                             checkpoint["non_tp"][i]
                                             ) if base_dir1 else checkpoint["non_tp"][i]
                    sds = [torch.load(ckpt_file, map_location='cpu')]
                    load_model_with_checkpoint(replaced_module,
                                               sds,
                                               mp_replace,
                                               ckpt_type,
                                               quantizer,
                                               int(rank % tp_split_size))
                    sds = [None for _ in sds]
                    gc.collect()
        print(f"checkpoint loading time at rank {rank}: {time.time()-start_time} sec")

    if save_mp_checkpoint_path is not None:
        from collections import OrderedDict
        import json
        num_partitions = 8

        if checkpoint_dict is None:
            ckpt_name = "ds_model"
            try:
                from transformers.models.bloom.modeling_bloom import BloomForCausalLM
                if isinstance(model, BloomForCausalLM):
                    ckpt_name = "bloom"
            except ImportError:
                ckpt_name = "ds_model"
        else:
            ckpt_name = checkpoint_dict['type']
        if dist.is_initialized():
            dist.barrier()
        transformer_name = get_transformer_name(replaced_module)
        non_tp_ckpt_name = f'non-tp.pt'
        ckpt_files = [non_tp_ckpt_name]
        os.makedirs(save_mp_checkpoint_path, exist_ok=True)
        if not dist.is_initialized() or dist.get_rank() == 0:
            print("Saving tp-sharded checkpoints")
            torch.save(
                OrderedDict({
                    k: v
                    for k,
                    v in dict(replaced_module.state_dict()).items()
                    if transformer_name not in k
                }),
                f'{save_mp_checkpoint_path}/{non_tp_ckpt_name}')
            config = json.dumps({
                'type':
                ckpt_name,
                'base_dir':
                f'{save_mp_checkpoint_path}',
                'checkpoints': {
                    "non_tp":
                    ckpt_files,
                    "tp": [
                        f'tp_{r:0>2d}_{m:0>2d}.pt' for m in range(num_partitions)
                        for r in range(world_size)
                    ]
                },
                'version':
                1.0,
                'parallelization':
                'tp',
                'tp_size':
                world_size,
                'dtype':
                'int8' if quantize else ('float16' if fp16 else 'float32')
            })
            with open(f"{save_mp_checkpoint_path}/ds-inference_config.json", "w") as cfg:
                cfg.write(config)

        rep_sd = replaced_module.state_dict()
        for n, p in replaced_module.named_parameters():
            if hasattr(p, 'scale'):
                rep_sd[n] = [p, p.scale]
        keys = list(rep_sd.keys())
        partition_size = (len(keys) // num_partitions + 1)
        for m in range(num_partitions):
            torch.save(
                OrderedDict({
                    k: [rep_sd[k],
                        rep_sd[k].scale] if hasattr(rep_sd[k],
                                                    'scale') else rep_sd[k]
                    for k in keys[m * partition_size:(m + 1) * partition_size]
                    if transformer_name in k
                }),
                f'{save_mp_checkpoint_path}/tp_{rank:0>2d}_{m:0>2d}.pt')

    return replaced_module


def revert_transformer_layer(orig_layer_impl, model, config, preln=False):
    """ Revert DeepSpeed's transformer layer back to original bert-style transformer layer
    Arguments:
        orig_layer_impl (torch.nn.Module): the original transformer layer implementation that was replaced,
            e.g., transformers.modeling_bert.BertLayer.
        model (torch.nn.Module): user's nn.module representing their model
        config (dict): model config containing hidden size, attention heads, etc.
    Returns:
        Updated nn.module with original bert-style transformer layers
    """
    def replace_fn(child, _replace_policy, layer_id):
        #from turing.nvidia_modelingpreln import BertLayer
        orig_module = orig_layer_impl(config)

        # copy relevant state from child -> original module
        qkvw = child.attn_qkvw.data
        qkvb = child.attn_qkvb.data

        qw, kw, vw = torch.chunk(qkvw, 3, axis=0)
        qb, kb, vb = torch.chunk(qkvb, 3, axis=0)

        orig_module.attention.self.query.weight.data = qw
        orig_module.attention.self.query.bias.data = qb
        orig_module.attention.self.key.weight.data = kw
        orig_module.attention.self.key.bias.data = kb
        orig_module.attention.self.value.weight.data = vw
        orig_module.attention.self.value.bias.data = vb

        orig_module.attention.output.dense.weight.data = child.attn_ow.data
        orig_module.attention.output.dense.bias.data = child.attn_ob.data

        attn_ln_w = child.attn_nw.data
        attn_ln_b = child.attn_nb.data
        if preln:
            orig_module.PostAttentionLayerNorm.weight.data = attn_ln_w
            orig_module.PostAttentionLayerNorm.bias.data = attn_ln_b
        else:
            orig_module.attention.output.LayerNorm.weight.data = attn_ln_w
            orig_module.attention.output.LayerNorm.bias.data = attn_ln_b

        inter_ff_w = child.inter_w.data
        inter_ff_b = child.inter_b.data
        if preln:
            orig_module.intermediate.dense_act.weight.data = inter_ff_w
            orig_module.intermediate.dense_act.bias.data = inter_ff_b
        else:
            orig_module.intermediate.dense.weight.data = inter_ff_w
            orig_module.intermediate.dense.bias.data = inter_ff_b

        orig_module.output.dense.weight.data = child.output_w.data
        orig_module.output.dense.bias.data = child.output_b.data

        transformer_ln_w = child.norm_w.data
        transformer_ln_b = child.norm_b.data
        if preln:
            orig_module.PreAttentionLayerNorm.weight.data = transformer_ln_w
            orig_module.PreAttentionLayerNorm.bias.data = transformer_ln_b
        else:
            orig_module.output.LayerNorm.weight.data = transformer_ln_w
            orig_module.output.LayerNorm.bias.data = transformer_ln_b
        return orig_module

    return replace_module(model=model,
                          orig_class=deepspeed.DeepSpeedTransformerLayer,
                          replace_fn=replace_fn,
                          _replace_policy=None)


def replace_module(model, orig_class, replace_fn, _replace_policy):
    """ Scan the model for instances of ``orig_clas:`` to replace using ``replace_fn``.
    Arguments:
        model (torch.nn.Module): the model to augment
        orig_class (torch.nn.Module): the module to search for
        replace_fn (method): a method to convert instances of ``orig_class`` to the
                             desired type and return a new instance.
    Returns:
        A modified ``model``.
    """
    policy = {}
    if orig_class is not None:
        policy.update({orig_class: (replace_fn, _replace_policy)})
    else:
        for plcy in replace_policies:
            # instantiate a throw-away policy in order to populate the _orig_layer_class
            _ = plcy(None)
            if isinstance(plcy._orig_layer_class, list):
                for orig_layer_class in plcy._orig_layer_class:
                    policy.update({orig_layer_class: (replace_fn, plcy)})
            elif plcy._orig_layer_class is not None:
                policy.update({plcy._orig_layer_class: (replace_fn, plcy)})
    assert len(policy.items()) > 0,\
        "No default policy found! Please specify your policy injection_policy (like {BertLayer:HFBEertLayerPolicy})." +\
        "You can find some samples here: https://github.com/microsoft/DeepSpeed/blob/master/deepspeed/module_inject/replace_policy.py"

    replaced_module, _ = _replace_module(model, policy)
    return replaced_module


from ..pipe import PipelineModule


def _replace_module(model, policies, layer_id=0):
    """ Traverse model's children recursively and apply any transformations in ``policies``.
    Arguments:
        model (torch.nn.Module): model to augment
        policies (dict): Mapping of source class to replacement function.
    Returns:
        Modified ``model``.
    """
    for name, child in model.named_children():
        if child.__class__ in policies:
            replaced_module = policies[child.__class__][0](child,
                                                           policies[child.__class__][-1],
                                                           layer_id)
            setattr(model, name, replaced_module)
            if isinstance(model, PipelineModule):
                assert hasattr(model, 'forward_funcs'),\
                    "we require pipe-module to have the list of fwd_functions"
                model.forward_funcs[model.fwd_map[name]] = replaced_module
            layer_id += 1
        else:
            _, layer_id = _replace_module(child, policies, layer_id=layer_id)

    return model, layer_id
