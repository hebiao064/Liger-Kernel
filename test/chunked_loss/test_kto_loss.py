from test.utils import HFAlignmentLoss, assert_verbose_allclose, set_seed

import pytest
import torch
import torch.nn.functional as F

from liger_kernel.chunked_loss import LigerFusedLinearKTOLoss
from liger_kernel.chunked_loss.functional import liger_fused_linear_kto
from liger_kernel.chunked_loss.kto_loss import LigerFusedLinearKTOFunction
from liger_kernel.utils import infer_device

device = infer_device()

# set random seed globally
set_seed()


class HFKTOLoss(HFAlignmentLoss):
    """
    Implementation of the Kahneman-Tversky Optimization (KTO) loss,
    adapted from Hugging Face's implementation.
    Reference: https://github.com/huggingface/trl/blob/main/trl/trainer/kto_trainer.py
    """

    def __init__(
        self, ignore_index: int = -100, beta: float = 0.1, use_ref_model: bool = True
    ):
        super().__init__(
            beta=beta, ignore_index=ignore_index, use_ref_model=use_ref_model
        )

    def alignment_loss(
        self,
        policy_chosen_logps: torch.FloatTensor,
        policy_rejected_logps: torch.FloatTensor,
        ref_chosen_logps: torch.FloatTensor,
        ref_rejected_logps: torch.FloatTensor,
    ):
        """Compute KTO loss for a batch of policy log probabilities.
        Args:
            policy_chosen_logps: Log probabilities of the policy model for the chosen responses. Shape: (batch_size,)
            policy_rejected_logps: Log probabilities of the policy model for the rejected responses. Shape: (batch_size,)

        Returns:
            The losses tensor contains the KTO loss for each example in the batch.
        """
        kl = torch.zeros_like(policy_chosen_logps)

        chosen_logratios = policy_chosen_logps - ref_chosen_logps
        chosen_losses = 1 - F.sigmoid(self.beta * (chosen_logratios - kl))
        chosen_rewards = self.beta * chosen_logratios.detach()

        rejected_logratios = policy_rejected_logps - ref_rejected_logps
        rejected_losses = 1 - F.sigmoid(self.beta * (kl - rejected_logratios))
        rejected_rewards = self.beta * rejected_logratios.detach()

        losses = torch.cat((chosen_losses, rejected_losses), 0)
        return losses, chosen_rewards, rejected_rewards


class TorchLMHeadKTO(torch.nn.Module):
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
        self.KTO_loss = HFKTOLoss(
            ignore_index=ignore_index, beta=beta, use_ref_model=True
        ).get_batch_loss_metrics

    def forward(self, x, ref_x, y):
        return self.KTO_loss(
            self.lin.weight,
            x,
            y,
            self.lin.bias,
            ref_x,
            self.ref_lin.weight,
            self.ref_lin.bias,
        )


class LigerLMHeadKTO(torch.nn.Module):
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
        self.KTO_loss = LigerFusedLinearKTOLoss(
            ignore_index=ignore_index, beta=beta, use_ref_model=True
        )

    def forward(self, x, ref_x, y):
        return self.KTO_loss(
            self.lin.weight,
            x,
            y,
            self.lin.bias,
            ref_x,
            self.ref_lin.weight,
            self.ref_lin.bias,
        )


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (3, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-2, 5e-1),
        (1.0, torch.float32, 2e-2, 5e-1),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize("ref_bias", [True, False])
@pytest.mark.parametrize("ignore_index, beta", [(-100, 0.1), (42, 0.2)])
def test_correctness(
    B, T, H, V, scalar, dtype, atol, rtol, bias, ref_bias, ignore_index, beta
):
    B = 2 * B  # KTO loss requires B to be even

    torch_lm_head_KTO = TorchLMHeadKTO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=ref_bias,
        ignore_index=ignore_index,
        beta=beta,
    )
    liger_lm_head_KTO = LigerLMHeadKTO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ref_bias=ref_bias,
        ignore_index=ignore_index,
        beta=beta,
    )

    torch_lm_head_KTO.lin.weight.data = liger_lm_head_KTO.lin.weight.data = torch.randn(
        V, H, device=device, dtype=dtype
    )
    torch_lm_head_KTO.ref_lin.weight.data = liger_lm_head_KTO.ref_lin.weight.data = (
        torch.randn(V, H, device=device, dtype=dtype)
    )

    if bias:
        torch_lm_head_KTO.lin.bias.data = liger_lm_head_KTO.lin.bias.data = torch.randn(
            V, device=device, dtype=dtype
        )
    if ref_bias:
        torch_lm_head_KTO.ref_lin.bias.data = liger_lm_head_KTO.ref_lin.bias.data = (
            torch.randn(V, device=device, dtype=dtype)
        )

    _input = torch.randn(B, T, H, device=device, dtype=dtype) * scalar
    input1 = _input.detach().clone().requires_grad_(True)
    input2 = _input.detach().clone().requires_grad_(True)

    ref_input = (
        torch.randn(B, T, H, device=device, dtype=dtype, requires_grad=False) * scalar
    )

    target = torch.randint(
        0,
        V,
        (
            B,
            T,
        ),
        device=device,
        dtype=torch.long,
    )
    # Assign some random number of elements as ignore_index
    num_elements_to_assign = torch.randint(1, B * T // 2, (1,)).item()
    indices_to_assign = torch.randperm(B * T)[:num_elements_to_assign]
    target.view(-1)[indices_to_assign] = ignore_index

    loss1, aggregated_aux_outputs1 = torch_lm_head_KTO(input1, ref_input, target)
    loss2, aggregated_aux_outputs2 = liger_lm_head_KTO(input2, ref_input, target)

    assert_verbose_allclose(loss1, loss2, atol=atol, rtol=rtol)

    assert len(aggregated_aux_outputs1) == len(aggregated_aux_outputs2)

    for i in range(len(aggregated_aux_outputs1)):
        assert_verbose_allclose(
            aggregated_aux_outputs1[i],
            aggregated_aux_outputs2[i],
            atol=atol,
            rtol=rtol,
        )

    loss1.backward()
    loss2.backward()

    assert_verbose_allclose(input1.grad, input2.grad, atol=atol, rtol=rtol)
    assert_verbose_allclose(
        torch_lm_head_KTO.lin.weight.grad,
        liger_lm_head_KTO.lin.weight.grad,
        atol=atol,
        rtol=rtol,
    )
    if bias:
        assert_verbose_allclose(
            torch_lm_head_KTO.lin.bias.grad,
            liger_lm_head_KTO.lin.bias.grad,
            atol=atol,
            rtol=rtol,
        )


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (2, 2, 8, 8),
        (3, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-2, 5e-1),
        (1.0, torch.float32, 1e-5, 5e-4),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize("ref_bias", [True, False])
def test_correctness_functional(B, T, H, V, scalar, dtype, atol, rtol, bias, ref_bias):
    B = 2 * B

    _input = torch.randn(B, T, H, device=device, dtype=dtype) * scalar
    input1 = _input.detach().clone().requires_grad_(True)
    input2 = _input.detach().clone().requires_grad_(True)

    ref_input = (
        torch.randn(B, T, H, device=device, dtype=dtype, requires_grad=False) * scalar
    )

    target = torch.randint(
        0,
        V,
        (
            B,
            T,
        ),
        device=device,
        dtype=torch.long,
    )

    _weight = torch.randn(V, H, device=device, dtype=dtype)
    weight1 = _weight.detach().clone().requires_grad_(True)
    weight2 = _weight.detach().clone().requires_grad_(True)

    _ref_weight = torch.randn(V, H, device=device, dtype=dtype)
    ref_weight1 = _ref_weight.detach().clone().requires_grad_(True)
    ref_weight2 = _ref_weight.detach().clone().requires_grad_(True)

    _bias = torch.randn(V, device=device, dtype=dtype) if bias else None
    bias1 = _bias.detach().clone().requires_grad_(True) if bias else None
    bias2 = _bias.detach().clone().requires_grad_(True) if bias else None

    _ref_bias = torch.randn(V, device=device, dtype=dtype) if ref_bias else None
    ref_bias1 = _ref_bias.detach().clone().requires_grad_(True) if ref_bias else None
    ref_bias2 = _ref_bias.detach().clone().requires_grad_(True) if ref_bias else None

    loss1, aggregated_aux_outputs1 = LigerFusedLinearKTOFunction.apply(
        input1, weight1, target, bias1, ref_input, ref_weight1, ref_bias1
    )
    loss2, aggregated_aux_outputs2 = liger_fused_linear_kto(
        input2, weight2, target, bias2, ref_input, ref_weight2, ref_bias2
    )

    assert_verbose_allclose(loss1, loss2, atol=atol, rtol=rtol)

    loss1.backward()
    loss2.backward()

    assert_verbose_allclose(input1.grad, input2.grad, atol=atol, rtol=rtol)
    assert_verbose_allclose(weight1.grad, weight2.grad, atol=atol, rtol=rtol)
    if bias:
        assert_verbose_allclose(bias1.grad, bias2.grad, atol=atol, rtol=rtol)
