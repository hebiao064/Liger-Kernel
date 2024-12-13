import os
import sys

import torch
import triton
from utils import (
    QUANTILES,
    SingleBenchmarkRunInput,
    SingleBenchmarkRunOutput,
    _test_memory,
    parse_benchmark_script_args,
    run_benchmarks,
)

from liger_kernel.chunked_loss import LigerFusedLinearKTOLoss
from liger_kernel.utils import infer_device

device = infer_device()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


class TorchKTOLoss(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ref_bias: bool = False,
        ignore_index: int = -100,
        beta: float = 0.1,
    ):
        from test.chunked_loss.test_kto_loss import HFKTOLoss

        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.ref_lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=ref_bias, dtype=dtype
        )
        self.kto_loss = HFKTOLoss(
            ignore_index=ignore_index, beta=beta, use_ref_model=True
        ).get_batch_loss_metrics

    def forward(self, x, ref_x, y):
        return self.kto_loss(
            self.lin.weight,
            x,
            y,
            self.lin.bias,
            ref_x,
            self.ref_lin.weight,
            self.ref_lin.bias,
        )[0]


class LigerKTOLoss(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ref_bias: bool = False,
        ignore_index: int = -100,
        beta: float = 0.1,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.ref_lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=ref_bias, dtype=dtype
        )
        self.kto_loss = LigerFusedLinearKTOLoss(
            ignore_index=ignore_index, beta=beta, use_ref_model=True
        )

    def forward(self, x, ref_x, y):
        return self.kto_loss(
            self.lin.weight,
            x,
            y,
            self.lin.bias,
            ref_x,
            self.ref_lin.weight,
            self.ref_lin.bias,
        )[0]


def bench_memory_kto_loss(input: SingleBenchmarkRunInput) -> SingleBenchmarkRunOutput:
    B = input.x
    T = input.extra_benchmark_config["T"]
    H = input.extra_benchmark_config["H"]
    V = input.extra_benchmark_config["V"]
    dtype = input.extra_benchmark_config["dtype"]
    bias = input.extra_benchmark_config["bias"]
    beta = input.extra_benchmark_config["beta"]
    ignore_index = input.extra_benchmark_config["ignore_index"]
    provider = input.kernel_provider

    torch_kto_loss = TorchKTOLoss(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=bias,
        ignore_index=ignore_index,
        beta=beta,
    ).to(device)

    liger_kto_loss = LigerKTOLoss(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=bias,
        ignore_index=ignore_index,
        beta=beta,
    ).to(device)

    # Input shape: [B, T, H]
    _input = torch.randn(B, T, H, device=device, dtype=dtype)
    # Target shape: [B, T]
    target = torch.randint(V, (B, T), dtype=torch.long, device=device)

    # Add ignore_index tokens to simulate padding
    num_elements_to_assign = torch.randint(1, B * T // 2, (1,)).item()
    indices_to_assign = torch.randperm(B * T)[:num_elements_to_assign]
    target.view(-1)[indices_to_assign] = ignore_index

    # Add ref_x with the same shape as _input
    ref_input = torch.randn(B, T, H, device=device, dtype=dtype)

    def fwd():
        if provider == "liger":
            return liger_kto_loss(_input, ref_input, target)
        elif provider == "huggingface":
            return torch_kto_loss(_input, ref_input, target)

    def full():
        y = fwd()
        y.backward()

    mem_50, mem_20, mem_80 = _test_memory(full, _iter=10, quantiles=QUANTILES)
    return SingleBenchmarkRunOutput(
        y_20=mem_20,
        y_50=mem_50,
        y_80=mem_80,
    )


def bench_speed_kto_loss(input: SingleBenchmarkRunInput) -> SingleBenchmarkRunOutput:
    B = input.x
    T = input.extra_benchmark_config["T"]
    H = input.extra_benchmark_config["H"]
    V = input.extra_benchmark_config["V"]
    dtype = input.extra_benchmark_config["dtype"]
    bias = input.extra_benchmark_config["bias"]
    beta = input.extra_benchmark_config["beta"]
    ignore_index = input.extra_benchmark_config["ignore_index"]
    provider = input.kernel_provider
    mode = input.kernel_operation_mode

    torch_kto_loss = TorchKTOLoss(
        H=H, V=V, dtype=dtype, beta=beta, ignore_index=ignore_index, bias=bias
    ).to(device)
    liger_kto_loss = LigerKTOLoss(
        H=H, V=V, dtype=dtype, beta=beta, ignore_index=ignore_index, bias=bias
    ).to(device)

    # Input shape: [B, T, H]
    _input = torch.randn(B, T, H, device=device, dtype=dtype)

    # Target shape: [B, T]
    target = torch.randint(V, (B, T), device=device, dtype=torch.long)

    # Add ignore_index tokens
    num_elements_to_assign = torch.randint(1, B * T // 2, (1,)).item()
    indices_to_assign = torch.randperm(B * T)[:num_elements_to_assign]
    target.view(-1)[indices_to_assign] = ignore_index

    # Add ref_x with the same shape as _input
    ref_input = torch.randn(B, T, H, device=device, dtype=dtype)

    def fwd():
        if provider == "liger":
            return liger_kto_loss(_input, ref_input, target)
        elif provider == "huggingface":
            return torch_kto_loss(_input, ref_input, target)

    if mode == "forward":
        ms_50, ms_20, ms_80 = triton.testing.do_bench(
            fwd,
            rep=100,
            quantiles=QUANTILES,
        )
    elif mode == "backward":
        y = fwd()
        ms_50, ms_20, ms_80 = triton.testing.do_bench(
            lambda: y.backward(retain_graph=True),
            grad_to_none=[_input],
            rep=100,
            quantiles=QUANTILES,
        )
    elif mode == "full":

        def full():
            y = fwd()
            y.backward()

        ms_50, ms_20, ms_80 = triton.testing.do_bench(
            full,
            rep=100,
            quantiles=QUANTILES,
        )

    return SingleBenchmarkRunOutput(
        y_20=ms_20,
        y_50=ms_50,
        y_80=ms_80,
    )


if __name__ == "__main__":
    args = parse_benchmark_script_args()

    common_configs = {
        "kernel_name": "kto_loss",
        "x_name": "B",
        "x_label": "Batch Size (B)",
        "x_values": [2**i for i in range(1, 6)],
        "kernel_providers": ["liger", "huggingface"],
        "extra_benchmark_configs": [
            {
                "T": 512,
                "H": 1024,
                "V": 128256,
                "mode": "forward",
                "dtype": torch.bfloat16,
                "bias": True,
                "beta": 0.1,
                "ignore_index": 42,
            }
        ],
        "overwrite": args.overwrite,
    }

    run_benchmarks(
        bench_test_fn=bench_speed_kto_loss,
        kernel_operation_modes=["forward", "full"],
        metric_name="speed",
        metric_unit="ms",
        **common_configs
    )

    run_benchmarks(
        bench_test_fn=bench_memory_kto_loss,
        kernel_operation_modes=["full"],
        metric_name="memory",
        metric_unit="MB",
        **common_configs
    )
