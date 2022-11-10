import torch
from torch import nn
import torch.distributed as dist
import bitsandbytes as bnb


class Int8Params(torch.nn.Parameter):
    def __new__(
        cls,
        data=None,
        requires_grad=False,
        has_fp16_weights=False,
        SCB=None,
    ):
        if data is None:
            data = torch.empty(0)
        if SCB is None:
            SCB = torch.empty(0)
        cls.has_fp16_weights = has_fp16_weights
        cls.SCB = SCB
        return torch.Tensor._make_subclass(cls, data, requires_grad)

    def __init__(self, data, SCB, requires_grad=False):
        super(Int8Params, self).__init__
        self.SCB = SCB
        self.data = data

class Linear8bitTP(nn.Linear):
    def __init__(
        self,
        input_features,
        output_features,
        bias=True,
        has_fp16_weights=False,
        memory_efficient_backward=False,
        threshold=6.0,
        weight_data=None,
        index=None,
        bias_data=None
    ):
        super(Linear8bitTP, self).__init__(
            input_features, output_features, bias
        )
        self.state = bnb.MatmulLtState()
        self.index = index
        self.bias = bias_data
        self.state.threshold = threshold
        self.state.has_fp16_weights = has_fp16_weights
        self.state.memory_efficient_backward = memory_efficient_backward
        if threshold > 0.0 and not has_fp16_weights:
            self.state.use_pool = True

        self.rank = dist.get_rank()
        self.world_size = dist.get_world_size()
        weight = weight_data.data.contiguous().half().to(self.rank)

        CB, _, SCB, _, _ = bnb.functional.double_quant(weight)
        self.weight = Int8Params(data=CB, SCB=SCB)

    def forward(self, x):
        self.state.is_training = self.training
        
        # weights are cast automatically as Int8Params, but the bias has to be cast manually
        if self.bias is not None and self.bias.dtype != torch.float16:
            self.bias.data = self.bias.data.half()
        
        self.state.CB = self.weight.data
        self.state.SCB = self.weight.SCB

        out = bnb.matmul(x, self.weight, bias=self.bias, state=self.state)

        tensor_list = [torch.zeros_like(out) for _ in range(self.world_size)]
        dist.all_gather(tensor_list, out)
        out = torch.cat(tensor_list, dim=2)

        return out

class Linear8bit(nn.Linear):
    def __init__(
        self,
        input_features,
        output_features,
        bias=True,
        has_fp16_weights=False,
        memory_efficient_backward=False,
        threshold=6.0,
        weight_data=None,
        index=None,
        bias_data=None
    ):
        super(Linear8bit, self).__init__(
            input_features, output_features, bias
        )
        self.state = bnb.MatmulLtState()
        self.index = index
        self.bias = bias_data
        self.state.threshold = threshold
        self.state.has_fp16_weights = has_fp16_weights
        self.state.memory_efficient_backward = memory_efficient_backward
        if threshold > 0.0 and not has_fp16_weights:
            self.state.use_pool = True

        weight = weight_data.data.contiguous().half().to(torch.cuda.current_device())

        CB, _, SCB, _, _ = bnb.functional.double_quant(weight)
        self.weight = Int8Params(data=CB, SCB=SCB)

    def forward(self, x):
        self.state.is_training = self.training
        
        # weights are cast automatically as Int8Params, but the bias has to be cast manually
        if self.bias is not None and self.bias.dtype != torch.float16:
            self.bias.data = self.bias.data.half()
        
        self.state.CB = self.weight.data
        self.state.SCB = self.weight.SCB

        out = bnb.matmul(x, self.weight, bias=self.bias, state=self.state)

        return out

def replace_8bit_linear_tp(model, threshold=6.0, modules_to_not_convert="lm_head"):
    for name, module in model.named_children():
        if len(list(module.children())) > 0:
            replace_8bit_linear_tp(module, threshold, modules_to_not_convert)

        if isinstance(module, nn.Linear) and name not in modules_to_not_convert:
                model._modules[name] = Linear8bitTP(
                        input_features=module.in_features,
                        output_features=module.out_features,
                        threshold=6.0,
                        weight_data=module.weight,
                        bias_data=module.bias,
                )
    return model

def replace_8bit_linear(model, threshold=6.0, modules_to_not_convert="lm_head"):
    for name, module in model.named_children():
        if len(list(module.children())) > 0:
            replace_8bit_linear(module, threshold, modules_to_not_convert)

        if isinstance(module, nn.Linear) and name not in modules_to_not_convert:
                model._modules[name] = Linear8bit(
                        input_features=module.in_features,
                        output_features=module.out_features,
                        threshold=6.0,
                        weight_data=module.weight,
                        bias_data=module.bias,
                )
    return model

def get_8bit_tp_model(model, rank, world_size):
    for name, module in model.named_modules():
        if isinstance(module, Linear8bitTP):
            weight_list = list(module.weight.data.chunk(world_size, dim=0))
            weight = weight_list[rank]

            SCB_list = list(module.weight.SCB.chunk(world_size, dim=0))
            SCB = SCB_list[rank]
            module.weight = Int8Params(data=weight, SCB=SCB)

            bias_list = list(module.bias.data.chunk(world_size, dim=0))
            bias = bias_list[rank]
            module.bias = nn.Parameter(bias)
    return model
