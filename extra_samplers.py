import math

import torch
from torch import nn, FloatTensor
import torchsde
from tqdm.auto import trange, tqdm
import numpy as np

import comfy.sample

from comfy.k_diffusion.sampling import BrownianTreeNoiseSampler, PIDStepSizeController, get_ancestral_step, to_d, default_noise_sampler, DPMSolver

# The following function adds the samplers during initialization, in __init__.py
def add_samplers():
    from comfy.samplers import KSampler, k_diffusion_sampling
    if hasattr(KSampler, "DISCARD_PENULTIMATE_SIGMA_SAMPLERS"):
        KSampler.DISCARD_PENULTIMATE_SIGMA_SAMPLERS |= discard_penultimate_sigma_samplers
    added = 0
    for sampler in extra_samplers: #getattr(self, "sample_{}".format(extra_samplers))
        if sampler not in KSampler.SAMPLERS:
            try:
                idx = KSampler.SAMPLERS.index("uni_pc_bh2") # Last item in the samplers list
                KSampler.SAMPLERS.insert(idx+1, sampler) # Add our custom samplers
                setattr(k_diffusion_sampling, "sample_{}".format(sampler), extra_samplers[sampler])
                added += 1
            except ValueError as _err:
                pass
    if added > 0:
        import importlib
        importlib.reload(k_diffusion_sampling)

# The following function adds the samplers during initialization, in __init__.py
def add_schedulers():
    from comfy.samplers import KSampler, k_diffusion_sampling
    added = 0
    for scheduler in extra_schedulers: #getattr(self, "sample_{}".format(extra_samplers))
        if scheduler not in KSampler.SCHEDULERS:
            try:
                idx = KSampler.SCHEDULERS.index("ddim_uniform") # Last item in the samplers list
                KSampler.SCHEDULERS.insert(idx+1, scheduler) # Add our custom samplers
                setattr(k_diffusion_sampling, "get_sigmas_{}".format(scheduler), extra_schedulers[scheduler])
                added += 1
            except ValueError as err:
                pass
    if added > 0:
        import importlib
        importlib.reload(k_diffusion_sampling)


# Noise samplers
NOISE_SAMPLER_NAMES=("gaussian", "uniform", "brownian", "highres-pyramid", "pyramid", "perlin", "laplacian")

def get_noise_sampler_names(default=None):
    if not default:
        return NOISE_SAMPLER_NAMES
    return (default,) + tuple(n for n in NOISE_SAMPLER_NAMES if n != default)

def mk_noise_sampler(x, fun):
    return lambda _sigma, _sigma_next: fun(x)

def get_noise_sampler(x, sigmas, noise_sampler_type="brownian", extra_args=None, cpu=False):
    if noise_sampler_type == "brownian":
        seed = extra_args.get("seed", None) if extra_args else None
        sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
        return BrownianTreeNoiseSampler(x, sigma_min, sigma_max, seed=seed, cpu=cpu)
    return mk_noise_sampler(x, NOISE_SAMPLER_HANDLERS.get(noise_sampler_type, uniform_noise_like))

from torch import Generator, Tensor, lerp
from torch.nn.functional import unfold
from typing import Callable, Tuple
from math import pi

def uniform_noise_like(x):
    return (torch.rand_like(x) - 0.5) * 2 * 1.73

def get_positions(block_shape: Tuple[int, int]) -> Tensor:
    """
    Generate position tensor.

    Arguments:
        block_shape -- (height, width) of position tensor

    Returns:
        position vector shaped (1, height, width, 1, 1, 2)
    """
    bh, bw = block_shape
    positions = torch.stack(
        torch.meshgrid(
            [(torch.arange(b) + 0.5) / b for b in (bw, bh)],
            indexing="xy",
        ),
        -1,
    ).view(1, bh, bw, 1, 1, 2)
    return positions


def unfold_grid(vectors: Tensor) -> Tensor:
    """
    Unfold vector grid to batched vectors.

    Arguments:
        vectors -- grid vectors

    Returns:
        batched grid vectors
    """
    batch_size, _, gpy, gpx = vectors.shape
    return (
        unfold(vectors, (2, 2))
        .view(batch_size, 2, 4, -1)
        .permute(0, 2, 3, 1)
        .view(batch_size, 4, gpy - 1, gpx - 1, 2)
    )


def smooth_step(t: Tensor) -> Tensor:
    """
    Smooth step function [0, 1] -> [0, 1].

    Arguments:
        t -- input values (any shape)

    Returns:
        output values (same shape as input values)
    """
    return t * t * (3.0 - 2.0 * t)


def perlin_noise_tensor(
    vectors: Tensor, positions: Tensor, step: Callable = None
) -> Tensor:
    """
    Generate perlin noise from batched vectors and positions.

    Arguments:
        vectors -- batched grid vectors shaped (batch_size, 4, grid_height, grid_width, 2)
        positions -- batched grid positions shaped (batch_size or 1, block_height, block_width, grid_height or 1, grid_width or 1, 2)

    Keyword Arguments:
        step -- smooth step function [0, 1] -> [0, 1] (default: `smooth_step`)

    Raises:
        Exception: if position and vector shapes do not match

    Returns:
        (batch_size, block_height * grid_height, block_width * grid_width)
    """
    if step is None:
        step = smooth_step

    batch_size = vectors.shape[0]
    # grid height, grid width
    gh, gw = vectors.shape[2:4]
    # block height, block width
    bh, bw = positions.shape[1:3]

    for i in range(2):
        if positions.shape[i + 3] not in (1, vectors.shape[i + 2]):
            raise Exception(
                f"Blocks shapes do not match: vectors ({vectors.shape[1]}, {vectors.shape[2]}), positions {gh}, {gw})"
            )

    if positions.shape[0] not in (1, batch_size):
        raise Exception(
            f"Batch sizes do not match: vectors ({vectors.shape[0]}), positions ({positions.shape[0]})"
        )

    vectors = vectors.view(batch_size, 4, 1, gh * gw, 2)
    positions = positions.view(positions.shape[0], bh * bw, -1, 2)

    step_x = step(positions[..., 0])
    step_y = step(positions[..., 1])

    row0 = lerp(
        (vectors[:, 0] * positions).sum(dim=-1),
        (vectors[:, 1] * (positions - positions.new_tensor((1, 0)))).sum(dim=-1),
        step_x,
    )
    row1 = lerp(
        (vectors[:, 2] * (positions - positions.new_tensor((0, 1)))).sum(dim=-1),
        (vectors[:, 3] * (positions - positions.new_tensor((1, 1)))).sum(dim=-1),
        step_x,
    )
    noise = lerp(row0, row1, step_y)
    return (
        noise.view(
            batch_size,
            bh,
            bw,
            gh,
            gw,
        )
        .permute(0, 3, 1, 4, 2)
        .reshape(batch_size, gh * bh, gw * bw)
    )


def perlin_noise(
    grid_shape: Tuple[int, int],
    out_shape: Tuple[int, int],
    batch_size: int = 1,
    generator: Generator = None,
    *args,
    **kwargs,
) -> Tensor:
    """
    Generate perlin noise with given shape. `*args` and `**kwargs` are forwarded to `Tensor` creation.

    Arguments:
        grid_shape -- Shape of grid (height, width).
        out_shape -- Shape of output noise image (height, width).

    Keyword Arguments:
        batch_size -- (default: {1})
        generator -- random generator used for grid vectors (default: {None})

    Raises:
        Exception: if grid and out shapes do not match

    Returns:
        Noise image shaped (batch_size, height, width)
    """
    # grid height and width
    gh, gw = grid_shape
    # output height and width
    oh, ow = out_shape
    # block height and width
    bh, bw = oh // gh, ow // gw

    if oh != bh * gh:
        raise Exception(f"Output height {oh} must be divisible by grid height {gh}")
    if ow != bw * gw != 0:
        raise Exception(f"Output width {ow} must be divisible by grid width {gw}")

    angle = torch.empty(
        [batch_size] + [s + 1 for s in grid_shape], *args, **kwargs
    ).uniform_(to=2.0 * pi, generator=generator)
    # random vectors on grid points
    vectors = unfold_grid(torch.stack((torch.cos(angle), torch.sin(angle)), dim=1))
    # positions inside grid cells [0, 1)
    positions = get_positions((bh, bw)).to(vectors)
    return perlin_noise_tensor(vectors, positions).squeeze(0)

def rand_perlin_like(x):
    noise = torch.randn_like(x) / 2.0
    noise_size_H = noise.size(dim=2)
    noise_size_W = noise.size(dim=3)
    perlin = None
    for i in range(2):
        noise += perlin_noise((noise_size_H, noise_size_W), (noise_size_H, noise_size_W), batch_size=x.shape[1]).to(x.device)
    #noise += perlin
    #print(noise)
    return noise / noise.std()

def uniform_noise_sampler(x): # Even distribution, seemingly produces more information in non-subject areas than the normal (gaussian) noise sampler
    return lambda sigma, sigma_next: (torch.rand_like(x) - 0.5) * 2 * 1.73

from torch.distributions import StudentT
def studentt_noise_sampler(x): # Produces more subject-focused outputs due to distribution, unsure if this works
    noise = StudentT(loc=0, scale=0.2, df=1).rsample(x.size())
    #noise *= 2 / (torch.max(torch.abs(noise)) + 1e-8)
    s: FloatTensor = torch.quantile(
        noise.flatten(start_dim=1).abs(),
        0.75,
        dim = -1
    )
    #s.clamp_(min = 1.)
    s = s.reshape(*s.shape, 1, 1, 1)
    noise = noise.clamp(-s, s)
    noise = torch.copysign(torch.pow(torch.abs(noise), 0.5), noise)
    print(s)
    return lambda sigma, sigma_next: noise.to(x.device) / (7/3)

from torch.distributions import Laplace
def rand_laplacian_like(x):
    noise = torch.randn_like(x) / 4.0
    noise_size_H = noise.size(dim=2)
    noise_size_W = noise.size(dim=3)
    noise += Laplace(loc=0, scale=1.0).rsample(x.size()).to(noise.device)
    #noise += perlin
    #print(noise)
    return noise / noise.std()

def highres_pyramid_noise_like(x, discount=0.7):
    b, c, h, w = x.shape # EDIT: w and h get over-written, rename for a different variant!
    orig_h = h
    orig_w = w
    u = torch.nn.Upsample(size=(orig_h, orig_w), mode='bilinear')
    noise = (torch.rand_like(x) - 0.5) * 2 * 1.73 # Start with scaled uniform noise
    for i in range(4):
        r = torch.rand(1).item() * 2 + 2 # Rather than always going 2x,
        h, w = min(orig_h*15, int(h*(r**i))), min(orig_w*15, int(w*(r**i)))
        noise += u(torch.randn(b, c, h, w).to(x)) * discount**i
        if h>=orig_h*15 or w>=orig_w*15: break # Lowest resolution is 1x1
    return noise/noise.std() # Scaled back to roughly unit variance

def green_noise_like(x):
    noise = torch.randn_like(x)
    width = noise.size(dim=2)
    height = noise.size(dim=3)
    scale = 1.0 / (width * height)
    fy = torch.fft.fftfreq(width, device=x.device)[:, None] ** 2
    fx = torch.fft.fftfreq(height, device=x.device) ** 2
    f = fy + fx
    power = torch.sqrt(f)
    power[0, 0] = 1
    noise = torch.fft.ifft2(torch.fft.fft2(noise) / torch.sqrt(power))
    noise *= scale / noise.std()
    noise = torch.real(noise).to(x.device)
    return noise / noise.std()

def green_noise_sampler(x): # This doesn't work properly right now
    width = x.size(dim=2)
    height = x.size(dim=3)
    noise = torch.randn(width, height)
    #scale = 1.0 / (width * height)
    fy = torch.fft.fftfreq(width)[:, None] ** 2
    fx = torch.fft.fftfreq(height) ** 2
    f = fy + fx
    power = torch.sqrt(f)
    power[0, 0] = 1
    noise = torch.fft.ifft2(torch.fft.fft2(noise) / torch.sqrt(power))
    #noise *= scale / noise.std()
    noise = torch.real(noise).to(x.device)
    mean = torch.mean(noise)
    std = torch.std(noise)

    noise.sub_(mean).div_(std)
    print(noise)
    return lambda sigma, sigma_next: noise

# I'm not sure how this differs from the other implementation but it doesn't seem to be used at present.
def power_noise_sampler_2(tensor, alpha=2, k=1): # This doesn't work properly right now
    """Generate 1/f noise for a given tensor.

    Args:
        tensor: The tensor to add noise to.
        alpha: The parameter that determines the slope of the spectrum.
        k: A constant.

    Returns:
        A tensor with the same shape as `tensor` containing 1/f noise.
    """
    tensor = torch.randn_like(tensor)
    fft = torch.fft.fft2(tensor)
    freq = torch.arange(1, len(fft) + 1, dtype=torch.float)
    spectral_density = k / freq**alpha
    noise = torch.rand(tensor.shape) * spectral_density
    mean = torch.mean(noise, dim=(-2, -1), keepdim=True).to(tensor.device)
    std = torch.std(noise, dim=(-2, -1), keepdim=True).to(tensor.device)
    noise = noise.to(tensor.device).sub_(mean).div_(std)
    variance = torch.var(noise, dim=(-2, -1), keepdim=True)
    print(variance)
    return lambda sigma, sigma_next: noise / 3

def pyramid_noise_like(size, dtype, layout, generator, device="cpu", discount=0.8):
    b, c, h, w = size
    orig_h = h
    orig_w = w
    noise = torch.zeros(size=size, dtype=dtype, layout=layout, device=device)
    r = 1
    for i in range(5):
        r *= 2 # Rather than always going 2x,
        #w, h = max(1, int(w/(r**i))), max(1, int(h/(r**i)))
        noise += torch.nn.functional.interpolate((torch.normal(mean=0, std=0.5 ** i, size=(b, c, h * r, w * r), dtype=dtype, layout=layout, generator=generator, device=device)), size=(orig_h, orig_w), mode='nearest-exact') * discount**i
        #if w>=orig_w*16 or h>=orig_h*16: break
    return noise

def power_noise_sampler(size, dtype, layout, generator, device="cpu", alpha=2, k=1): # This doesn't work properly right now
    """Generate 1/f noise for a given tensor.

    Args:
        tensor: The tensor to add noise to.
        alpha: The parameter that determines the slope of the spectrum.
        k: A constant.

    Returns:
        A tensor with the same shape as `tensor` containing 1/f noise.
    """
    tensor = torch.randn(size=size, dtype=dtype, layout=layout, generator=generator, device=device)
    fft = torch.fft.fft2(tensor)
    freq = torch.arange(1, len(fft) + 1, dtype=torch.float)
    spectral_density = k / freq**alpha
    noise = torch.rand(size=size, dtype=dtype, layout=layout, generator=generator, device=device) * spectral_density
    mean = torch.mean(noise, dim=(-2, -1), keepdim=True).to(tensor.device)
    std = torch.std(noise, dim=(-2, -1), keepdim=True).to(tensor.device)
    noise = noise.to(tensor.device).sub_(mean).div_(std)
    return noise

def prepare_noise(latent_image, seed, noise_type, noise_inds=None): # From `sample.py`
    """
    creates random noise given a latent image and a seed.
    optional arg skip can be used to skip and discard x number of noise generations for a given seed
    """
    generator = torch.manual_seed(seed)
    match noise_type:
        case "gaussian":
            noise_func = torch.randn
        case "uniform":
            def uniform_rand(*size, **kwargs):
                return (torch.rand(*size, **kwargs) - 0.5) * 2 * 1.73
            noise_func = uniform_rand
        case "pyramid":
            noise_func = pyramid_noise_like
        case "power":
            noise_func = power_noise_sampler
        case _:
            noise_func = torch.randn
    if noise_inds is None:
        return noise_func(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, generator=generator, device="cpu")

    unique_inds, inverse = np.unique(noise_inds, return_inverse=True)
    noises = []
    for i in range(unique_inds[-1]+1):
        noise = noise_func([1] + list(latent_image.size())[1:], dtype=latent_image.dtype, layout=latent_image.layout, generator=generator, device="cpu")
        if i in unique_inds:
            noises.append(noise)
    noises = [noises[i] for i in inverse]
    noises = torch.cat(noises, axis=0)
    return noises

NOISE_SAMPLER_HANDLERS={
    # Brownian is special-cased.
    "gaussian": torch.randn_like,
    "highres-pyramid": highres_pyramid_noise_like,
    "pyramid": lambda x: pyramid_noise_like(x.size(), x.dtype, x.layout, None, device=x.device),
    "perlin": rand_perlin_like,
    "laplacian": rand_laplacian_like,
    "uniform": uniform_noise_like,
}


# Below this point are extra samplers
@torch.no_grad()
def sample_clyb_4m_sde_momentumized(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1.0, s_noise=1., noise_sampler=None, momentum=0.0):
    """DPM-Solver++(3M) SDE, modified with an extra SDE, and momentumized in both the SDE and ODE(?). 'its a first' - Clybius 2023
    The expression for d1 is derived from the extrapolation formula given in the paper “Diffusion Monte Carlo with stochastic Hamiltonians” by M. Foulkes, L. Mitas, R. Needs, and G. Rajagopal. The formula is given as follows:
    d1 = d1_0 + (d1_0 - d1_1) * r2 / (r2 + r1) + ((d1_0 - d1_1) * r2 / (r2 + r1) - (d1_1 - d1_2) * r1 / (r0 + r1)) * r2 / ((r2 + r1) * (r0 + r1))
    (if this is an incorrect citing, we blame Google's Bard and OpenAI's ChatGPT for this and NOT me :^) )

    where d1_0, d1_1, and d1_2 are defined as follows:
    d1_0 = (denoised - denoised_1) / r2
    d1_1 = (denoised_1 - denoised_2) / r1
    d1_2 = (denoised_2 - denoised_3) / r0

    The variables r0, r1, and r2 are defined as follows:
    r0 = h_3 / h_2
    r1 = h_2 / h
    r2 = h / h_1
    """

    def momentum_func(diff, velocity, timescale=1.0, offset=-momentum / 2.0): # Diff is current diff, vel is previous diff
        if velocity is None:
            momentum_vel = diff
        else:
            momentum_vel = momentum * (timescale + offset) * velocity + (1 - momentum * (timescale + offset)) * diff
        return momentum_vel

    sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
    noise_sampler = rand_perlin_like(x) if noise_sampler is None else noise_sampler

    extra_args = {} if extra_args is None else extra_args
    s_in = x.new_ones([x.shape[0]])

    denoised_1, denoised_2, denoised_3 = None, None, None
    h_1, h_2, h_3 = None, None, None
    vel, vel_sde = None, None
    for i in trange(len(sigmas) - 1, disable=disable):
        time = sigmas[i] / sigma_max
        denoised = model(x, sigmas[i] * s_in, **extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        if sigmas[i + 1] == 0:
            # Denoising step
            x = denoised
        else:
            t, s = -sigmas[i].log(), -sigmas[i + 1].log()
            h = s - t
            h_eta = h * (eta + 1)
            x_diff = momentum_func((-h_eta).expm1().neg() * denoised, vel, time)
            vel = x_diff
            x = torch.exp(-h_eta) * x + vel

            if h_3 is not None:
                r0 = h_3 / h_2
                r1 = h_2 / h
                r2 = h / h_1
                d1_0 = (denoised - denoised_1) / r2
                d1_1 = (denoised_1 - denoised_2) / r1
                d1_2 = (denoised_2 - denoised_3) / r0
                d1 = d1_0 + (d1_0 - d1_1) * r2 / (r2 + r1) + ((d1_0 - d1_1) * r2 / (r2 + r1) - (d1_1 - d1_2) * r1 / (r0 + r1)) * r2 / ((r2 + r1) * (r0 + r1))
                d2 = (d1_0 - d1_1) / (r2 + r1) + ((d1_0 - d1_1) * r2 / (r2 + r1) - (d1_1 - d1_2) * r1 / (r0 + r1)) / ((r2 + r1) * (r0 + r1))
                phi_3 = h_eta.neg().expm1() / h_eta + 1
                phi_4 = phi_3 / h_eta - 0.5
                sde_diff = momentum_func(phi_3 * d1 - phi_4 * d2, vel_sde, time)
                vel_sde = sde_diff
                x = x + vel_sde
            elif h_2 is not None:
                r0 = h_1 / h
                r1 = h_2 / h
                d1_0 = (denoised - denoised_1) / r0
                d1_1 = (denoised_1 - denoised_2) / r1
                d1 = d1_0 + (d1_0 - d1_1) * r0 / (r0 + r1)
                d2 = (d1_0 - d1_1) / (r0 + r1)
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                phi_3 = phi_2 / h_eta - 0.5
                sde_diff = momentum_func(phi_2 * d1 - phi_3 * d2, vel_sde, time)
                vel_sde = sde_diff
                x = x + vel_sde
            elif h_1 is not None:
                r = h_1 / h
                d = (denoised - denoised_1) / r
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                sde_diff = momentum_func(phi_2 * d, vel_sde, time)
                vel_sde = sde_diff
                x = x + vel_sde

            if eta:
                x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * sigmas[i + 1] * (-2 * h * eta).expm1().neg().sqrt() * s_noise

            denoised_1, denoised_2, denoised_3 = denoised, denoised_1, denoised_2
            h_1, h_2, h_3 = h, h_1, h_2
    return x

# Kat's Truncated Taylor Method sampler, by Katherine Crowson
def sample_ttm_jvp(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler=None):
    """Second order truncated Taylor method (torch.func.jvp() version)."""

    extra_args = {} if extra_args is None else extra_args
    noise_sampler = default_noise_sampler(x) if noise_sampler is None else noise_sampler
    s_in = x.new_ones([x.shape[0]])
    model_fn = lambda x, sigma: model(x, sigma * s_in, **extra_args)

    for i in trange(len(sigmas) - 1, disable=disable):
        denoised = model_fn(x, sigmas[i])

        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})

        if sigmas[i + 1] == 0:
            # Denoising step
            x = denoised
        else:
            # 2nd order truncated Taylor method
            t, s = -sigmas[i].log(), -sigmas[i + 1].log()
            h = s - t
            h_eta = h * (eta + 1)

            eps = to_d(x, sigmas[i], denoised)
            _, denoised_prime = torch.func.jvp(model_fn, (x, sigmas[i]), (eps * -sigmas[i], -sigmas[i]))

            phi_1 = -torch.expm1(-h_eta)
            #phi_2 = torch.expm1(-h_eta) + h_eta
            phi_2 = torch.expm1(-h) + h # seems to work better with eta > 0
            x = torch.exp(-h_eta) * x + phi_1 * denoised + phi_2 * denoised_prime

            if eta:
                phi_1_noise = torch.sqrt(-torch.expm1(-2 * h * eta))
                x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * sigmas[i + 1] * phi_1_noise * s_noise

    return x

# Many thanks to Kat + Birch-San for this wonderful sampler implementation! https://github.com/Birch-san/sdxl-play/commits/res/
from .other_samplers.refined_exp_solver import sample_refined_exp_s
def sample_res_solver(model, x, sigmas, extra_args=None, callback=None, disable=None, noise_sampler_type="gaussian", noise_sampler=None, denoise_to_zero=True, simple_phi_calc=False, c2=0.5, ita=torch.Tensor((0.25,)), momentum=0.0):
    return sample_refined_exp_s(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args), denoise_to_zero=denoise_to_zero, simple_phi_calc=simple_phi_calc, c2=c2, ita=ita, momentum=momentum)

@torch.no_grad()
def sample_dpmpp_dualsde_momentum(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler=None, r=1/2, momentum=0.0):
    """DPM-Solver++ (Stochastic with Momentum). Personal modified sampler by Clybius"""
    sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
    noise_sampler = rand_perlin_like(x) if noise_sampler is None else noise_sampler
    extra_args = {} if extra_args is None else extra_args
    s_in = x.new_ones([x.shape[0]])
    sigma_fn = lambda t: t.neg().exp()
    t_fn = lambda sigma: sigma.log().neg()

    denoisedsde_1, denoisedsde_2, denoisedsde_3 = None, None, None # new line
    h_1, h_2, h_3 = None, None, None # new line

    def momentum_func(diff, velocity, timescale=1.0, offset=-momentum / 2.0): # Diff is current diff, vel is previous diff
        if velocity is None:
            momentum_vel = diff
        else:
            momentum_vel = momentum * (timescale + offset) * velocity + (1 - momentum * (timescale + offset)) * diff
        return momentum_vel

    vel = None
    vel_2 = None
    vel_sde = None
    for i in trange(len(sigmas) - 1, disable=disable):
        time = sigmas[i] / sigma_max
        denoised = model(x, sigmas[i] * s_in, **extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        if sigmas[i + 1] == 0:
            # Euler method
            d = to_d(x, sigmas[i], denoised)
            dt = sigmas[i + 1] - sigmas[i]
            x = x + d * dt
        else:
            # DPM-Solver++
            t, t_next = t_fn(sigmas[i]), t_fn(sigmas[i + 1])
            h = t_next - t
            h_eta = h * (eta + 1)
            s = t + h * r
            fac = 1 / (2 * r)

            # Step 1
            sd, su = get_ancestral_step(sigma_fn(t), sigma_fn(s), eta)
            s_ = t_fn(sd)
            diff_2 = momentum_func((t - s_).expm1() * denoised, vel_2, time)
            vel_2 = diff_2
            x_2 = (sigma_fn(s_) / sigma_fn(t)) * x - diff_2
            x_2 = x_2 + noise_sampler(sigma_fn(t), sigma_fn(s)) * s_noise * su
            denoised_2 = model(x_2, sigma_fn(s) * s_in, **extra_args)

            # Step 2
            sd, su = get_ancestral_step(sigma_fn(t), sigma_fn(t_next), eta)
            t_next_ = t_fn(sd)
            denoised_d = (1 - fac) * denoised + fac * denoised_2
            diff = momentum_func((t - t_next_).expm1() * denoised_d, vel, time)
            vel = diff
            x = (sigma_fn(t_next_) / sigma_fn(t)) * x - diff

            if h_3 is not None:
                r0 = h_3 / h_2
                r1 = h_2 / h
                r2 = h / h_1
                d1_0 = (denoised_d - denoisedsde_1) / r2
                d1_1 = (denoisedsde_1 - denoisedsde_2) / r1
                d1_2 = (denoisedsde_2 - denoisedsde_3) / r0
                d1 = d1_0 + (d1_0 - d1_1) * r2 / (r2 + r1) + ((d1_0 - d1_1) * r2 / (r2 + r1) - (d1_1 - d1_2) * r1 / (r0 + r1)) * r2 / ((r2 + r1) * (r0 + r1))
                d2 = (d1_0 - d1_1) / (r2 + r1) + ((d1_0 - d1_1) * r2 / (r2 + r1) - (d1_1 - d1_2) * r1 / (r0 + r1)) / ((r2 + r1) * (r0 + r1))
                phi_3 = h_eta.neg().expm1() / h_eta + 1
                phi_4 = phi_3 / h_eta - 0.5
                diff = momentum_func(phi_3 * d1 - phi_4 * d2, vel_sde, time)
                vel_sde = diff
                x = x + diff
            elif h_2 is not None:
                r0 = h_1 / h
                r1 = h_2 / h
                d1_0 = (denoised_d - denoisedsde_1) / r0
                d1_1 = (denoisedsde_1 - denoisedsde_2) / r1
                d1 = d1_0 + (d1_0 - d1_1) * r0 / (r0 + r1)
                d2 = (d1_0 - d1_1) / (r0 + r1)
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                phi_3 = phi_2 / h_eta - 0.5
                diff = momentum_func(phi_2 * d1 - phi_3 * d2, vel_sde, time)
                vel_sde = diff
                x = x + diff
            elif h_1 is not None:
                r = h_1 / h
                d = (denoised_d - denoisedsde_1) / r
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                diff = momentum_func(phi_2 * d, vel_sde, time)
                vel_sde = diff
                x = x + diff

            if eta:
                x = x + noise_sampler(sigma_fn(t), sigma_fn(t_next)) * s_noise * su
            #if 'denoised_d' in locals():
            denoisedsde_1, denoisedsde_2, denoisedsde_3 = denoised_d, denoisedsde_1, denoisedsde_2 # new line
            #if 'h' in locals():
            h_1, h_2, h_3 = h, h_1, h_2
    return x

def sample_dpmpp_dualsdemomentum(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler_type="gaussian", noise_sampler=None, r=1/2, momentum=0.0):
    return sample_dpmpp_dualsde_momentum(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, eta=eta, s_noise=s_noise, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args), r=r, momentum=momentum)

from .other_samplers.sample_ttm import sample_ttm_jvp
def sample_ttmcustom(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler_type="gaussian",noise_sampler=None):
    return sample_ttm_jvp(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, eta=eta, s_noise=s_noise, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args))

from comfy.k_diffusion.sampling import sample_lcm
def sample_lcmcustom(model, x, sigmas, extra_args=None, callback=None, disable=None, noise_sampler_type="gaussian", noise_sampler=None):
    return sample_lcm(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args))

def sample_clyb_4m_sde(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler_type="brownian", noise_sampler=None, momentum=0.0):
    return sample_clyb_4m_sde_momentumized(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, eta=eta, s_noise=s_noise, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args), momentum=momentum)


# This code works, but I'm currently experimenting with different methods
@torch.no_grad()
def sampler_euler_ancestral_dancing(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler=None, leap=2, eta_dance=1.0):
    #Ancestral sampling with Euler method steps, dancing steps.
    extra_args = {} if extra_args is None else extra_args
    noise_sampler = default_noise_sampler(x) if noise_sampler is None else noise_sampler
    unsample_noise_sampler = lambda sigma, sigma_next: torch.randn_like(x)
    s_in = x.new_ones([x.shape[0]])
    for i in trange(len(sigmas) - 1, disable=disable):
        if i < len(sigmas) - leap:
            is_danceable = sigmas[i + leap] > 0
        else:
            is_danceable = False
        denoised = model(x, sigmas[i] * s_in, **extra_args)
        sigma_down, sigma_up = get_ancestral_step(sigmas[i], sigmas[i + leap] if is_danceable else sigmas[i + 1], eta=eta)
        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        d = to_d(x, sigmas[i], denoised)
        # Euler method
        dt = sigma_down - sigmas[i]
        x = x + d * dt
        if sigmas[i + 1] > 0:
            if is_danceable:
                x = x + noise_sampler(sigmas[i], sigmas[i + leap]) * s_noise * sigma_up
                #x = x + noise_sampler(sigmas[i + 2], sigmas[i + 1]) * s_noise * sigma_up
                #denoised2 = model(x, sigmas[i + 2] * s_in, **extra_args)
                sigma_down2, sigma_up2 = get_ancestral_step(sigmas[i + leap], sigmas[i + 1], eta=eta_dance)
                d_2 = to_d(x, sigmas[i + leap], denoised)
                dt_2 = sigma_down2 - sigmas[i + leap]
                x = x + d_2 * dt_2
                x = x + noise_sampler(sigmas[i + leap], sigmas[i + 1]) * s_noise * sigma_up2

                #sigma_down3, sigma_up3 = get_ancestral_step(sigmas[i], sigmas[i + 1], eta=eta)
                #x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up3

                #denoised2 = model(x, sigmas[i] * s_in, **extra_args)
                #d_3 = to_d(x, sigmas[i], denoised2)
                #dt_3 = sigma_down3 - sigmas[i]
                #x = x + d_3 * dt_3 + d_2 * dt_2
                #print(dt_3, dt_2)
                #x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up3
                #x = x + d * dt
            else:
                x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up

    return x

def sample_euler_ancestral_dancing(model, x, sigmas, extra_args=None, callback=None, disable=None, eta=1., s_noise=1., noise_sampler_type="gaussian", noise_sampler=None, leap=2, eta_dance=1.0):
    return sampler_euler_ancestral_dancing(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, eta=eta, s_noise=s_noise, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args), leap=leap, eta_dance=eta_dance)


@torch.no_grad()
def sampler_dpmpp_3m_sde_dynamic_eta(model, x, sigmas, extra_args=None, callback=None, disable=None, eta_max=1.0, eta_min=0.0, s_noise=1., noise_sampler=None):
    """DPM-Solver++(3M) SDE with dynamic eta."""
    def eta_schedule_cosine_annealing(i, n, eta_max=eta_max, eta_min=eta_min):
        """Cosine annealing schedule for eta."""
        progress = i / (n - 1)
        eta = eta_min + 0.5 * (eta_max - eta_min) * (1 + math.cos(math.pi * progress))
        return eta

    seed = extra_args.get("seed", None)
    sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
    noise_sampler = BrownianTreeNoiseSampler(x, sigma_min, sigma_max, seed=seed, cpu=True) if noise_sampler is None else noise_sampler
    extra_args = {} if extra_args is None else extra_args
    s_in = x.new_ones([x.shape[0]])

    denoised_1, denoised_2 = None, None
    h, h_1, h_2 = None, None, None

    for i in trange(len(sigmas) - 1, disable=disable):
        denoised = model(x, sigmas[i] * s_in, **extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        if sigmas[i + 1] == 0:
            # Denoising step
            x = denoised
        else:
            # DPM-Solver++(3M) SDE
            t, s = -sigmas[i].log(), -sigmas[i + 1].log()
            h = s - t

            # Dynamic eta
            eta = eta_schedule_cosine_annealing(i, len(sigmas))
            h_eta = h * (eta + 1)

            x = torch.exp(-h_eta) * x + (-h_eta).expm1().neg() * denoised

            if h_2 is not None:
                r0 = h_1 / h
                r1 = h_2 / h
                d1_0 = (denoised - denoised_1) / r0
                d1_1 = (denoised_1 - denoised_2) / r1
                d1 = d1_0 + (d1_0 - d1_1) * r0 / (r0 + r1)
                d2 = (d1_0 - d1_1) / (r0 + r1)
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                phi_3 = phi_2 / h_eta - 0.5
                x = x + phi_2 * d1 - phi_3 * d2
            elif h_1 is not None:
                r = h_1 / h
                d = (denoised - denoised_1) / r
                phi_2 = h_eta.neg().expm1() / h_eta + 1
                x = x + phi_2 * d

            if eta:
                x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * sigmas[i + 1] * (-2 * h * eta).expm1().neg().sqrt() * s_noise

        denoised_1, denoised_2 = denoised, denoised_1
        h_1, h_2 = h, h_1
    return x

def sample_dpmpp_3m_sde_dynamic_eta(model, x, sigmas, extra_args=None, callback=None, disable=None, eta_max=1.0, eta_min=0.0, s_noise=1., noise_sampler_type="brownian", noise_sampler=None):
    return sampler_dpmpp_3m_sde_dynamic_eta(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, eta_max=eta_max, eta_min=eta_min, s_noise=s_noise, noise_sampler=noise_sampler or get_noise_sampler(x, sigmas, noise_sampler_type, noise_sampler, extra_args))

@torch.no_grad()
def sampler_supreme(model, x, sigmas, extra_args=None, callback=None, disable=None, s_noise=1., noise_sampler=None, eta=1.0, step_method="euler", centralization=0.02, normalization=0.01, edge_enhancement=0.5, perphist=-0.15):
    """
    Supreme Sampler, Euler steps. Based on no paper, purely interesting thoughts.

    Args:
        model: Denoising model call.
        x: The initial noisy sample.
        sigmas: The noise schedule.
        extra_args: Additional arguments for the model.
        callback: A callback function for monitoring the sampling process.
        disable: Whether to disable the progress bar.
        s_noise: The noise scale factor.
        noise_sampler: A custom noise sampler function.
        eta: Ancestral-ness.
        centralization: Subtracts mean from the denoised latent, reduces edge enhancement when between 0-1.
        normalization: Divides the denoised latent by the standard deviation.
        edge_enhancement: Multiplies the edges by the mean using a laplacian kernel
        perphist: Adds previous denoised variable to the current denoised using perpendicular vector projection
    """

    extra_args = {} if extra_args is None else extra_args
    noise_sampler = default_noise_sampler(x) if noise_sampler is None else noise_sampler
    s_in = x.new_ones([x.shape[0]])

    # Centralization
    def centralize(denoised_sample, centralization):
        for b in range(len(denoised_sample)):
            for c in range(len(denoised_sample[b])):
                channel = denoised_sample[b][c]
                denoised_sample[b][c] -= channel.mean() * centralization
        return denoised_sample
    
    # Normalization
    def normalize(denoised_sample, normalization):
        for b in range(len(denoised_sample)):
            for c in range(len(denoised_sample[b])):
                channel = denoised_sample[b][c]
                denoised_sample[b][c] += ((denoised_sample[b][c] / channel.std()) - denoised_sample[b][c]) * normalization
        return denoised_sample
    
    # Perp-hist
    def perpadd(denoised_tensor, old_denoised_tensor, x, alpha):
        a_diff = x - (denoised_tensor - x)
        b_diff = x - (old_denoised_tensor - x)
        a_ortho = a_diff * (a_diff / torch.linalg.norm(a_diff) * (b_diff / torch.linalg.norm(a_diff))).sum()
        b_perp = b_diff - a_ortho
        res = denoised_tensor + alpha * b_perp
        return res
    
    # DynETA
    def eta_schedule_cosine_annealing(i, n, eta_max=eta, eta_min=0.0):
        """Cosine annealing schedule for eta."""
        progress = i / (n - 1)
        eta = eta_min + 0.5 * (eta_max - eta_min) * (1 + math.cos(math.pi * progress))
        return eta

    def f(x, sigma):
        """Function representing the Karras ODE derivative."""
        denoised = model(x, sigma * s_in, **extra_args)
        return to_d(x, sigma, denoised)

    old_denoised = None
    for i in trange(len(sigmas) - 1, disable=disable):
        # DynETA
        eta = eta_schedule_cosine_annealing(i, len(sigmas))
        eps_cache = {}
        dpm_solver = DPMSolver(model, extra_args)

        denoised = model(x, sigmas[i] * s_in, **extra_args)

        if centralization != 0:
            denoised = centralize(denoised, centralization)

        if normalization != 0:
            denoised = normalize(denoised, normalization)
        
        if old_denoised != None and perphist != 0:
            denoised = perpadd(denoised, old_denoised, x, perphist)
        
        if old_denoised != None and edge_enhancement != 0:
            lap_kern = torch.tensor([[0, -1, 0], [-1, 4, -1], [0, -1, 0]], device=denoised.device, dtype=denoised.dtype).repeat(denoised.shape[1], 1, 1, 1)
            denoised = denoised + torch.conv2d(denoised, lap_kern, groups=denoised.shape[1], padding=1) * denoised.mean(dim=(1, 2, 3), keepdim=True) * sigmas[i] * edge_enhancement

        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        
        eps = (x - denoised) / sigmas[i]
        eps_cache = {'eps': eps}

        match step_method:
            case "euler":
                sigma_down, sigma_up = get_ancestral_step(sigmas[i], sigmas[i + 1], eta=eta)
                d = to_d(x, sigmas[i], denoised)
                dt = sigma_down - sigmas[i]

                x = x + d * dt
                if sigmas[i + 1] > 0:
                    x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up
            case "dpm_1s": # DPM Family
                if callback is not None:
                    dpm_solver.info_callback = lambda info: callback({'sigma': dpm_solver.sigma(info['t']), 'sigma_hat': dpm_solver.sigma(info['t_up']), **info})
                x, eps_cache = dpm_solver.dpm_solver_1_step(x, dpm_solver.t(sigmas[i]), dpm_solver.t(sigmas[i + 1]), eps_cache=eps_cache)
            case "dpm_2s":
                if callback is not None:
                    dpm_solver.info_callback = lambda info: callback({'sigma': dpm_solver.sigma(info['t']), 'sigma_hat': dpm_solver.sigma(info['t_up']), **info})
                x, eps_cache = dpm_solver.dpm_solver_2_step(x, dpm_solver.t(sigmas[i]), dpm_solver.t(sigmas[i + 1]), eps_cache=eps_cache)
            case "dpm_3s":
                if callback is not None:
                    dpm_solver.info_callback = lambda info: callback({'sigma': dpm_solver.sigma(info['t']), 'sigma_hat': dpm_solver.sigma(info['t_up']), **info})
                x, eps_cache = dpm_solver.dpm_solver_3_step(x, dpm_solver.t(sigmas[i]), dpm_solver.t(sigmas[i + 1]), eps_cache=eps_cache)
            case "rk4": # Fourth-order Runge-Kutta method
                sigma_down, sigma_up = get_ancestral_step(sigmas[i], sigmas[i + 1], eta=eta)
                # Calculate the derivative using the model
                d = to_d(x, sigmas[i], denoised)
                dt = sigma_down - sigmas[i]

                # Runge-Kutta steps
                k1 = d * dt
                k2 = to_d(x + k1 / 2, sigmas[i] + dt / 2, model(x + k1 / 2, (sigmas[i] + dt / 2) * s_in, **extra_args)) * dt
                k3 = to_d(x + k2 / 2, sigmas[i] + dt / 2, model(x + k2 / 2, (sigmas[i] + dt / 2) * s_in, **extra_args)) * dt
                k4 = to_d(x + k3, sigmas[i] + dt, model(x + k3, (sigmas[i] + dt) * s_in, **extra_args)) * dt

                # Update the sample
                x = x + (k1 + 2 * k2 + 2 * k3 + k4) / 6
                if sigmas[i + 1] > 0:
                    x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up
            case "trapezoidal":
                if sigmas[i + 1] > 0:
                    sigma_down, sigma_up = get_ancestral_step(sigmas[i], sigmas[i + 1], eta=eta)
                    dt = sigmas[i + 1] - sigmas[i]

                    # Calculate the derivative using the model
                    d_i = to_d(x, sigmas[i], denoised)

                    # Predict the sample at the next sigma using Euler step
                    x_pred = x + d_i * dt

                    # Denoised sample at the next sigma
                    denoised_i_plus_1 = model(x_pred, sigmas[i + 1] * s_in, **extra_args)

                    # Calculate the derivative at the next sigma
                    d_i_plus_1 = to_d(x_pred, sigmas[i + 1], denoised_i_plus_1)

                    #if callback is not None:
                    #    callback({'x': x, 'i': i, 'sigma': sigmas[i + 1], 'denoised': denoised_i_plus_1})
                    dt_2 = sigma_down - sigmas[i]
                    # Update the sample using the Trapezoidal rule
                    x = x + dt_2 * (d_i + d_i_plus_1) / 2
                    x = x + noise_sampler(sigmas[i], sigmas[i + 1]) * s_noise * sigma_up
                else:
                    x = denoised

        old_denoised = denoised

    return x

def sample_supreme(model, x, sigmas, extra_args=None, callback=None, disable=None, s_noise=1., noise_sampler="gaussian", eta=1.0, step_method="euler", centralization=0.02, normalization=0.01, edge_enhancement=0.5, perphist=-0.15):
    sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
    seed = extra_args.get("seed", None)
    match noise_sampler:
        case "brownian":
            noise_sampler = BrownianTreeNoiseSampler(x, sigma_min, sigma_max, seed=seed, cpu=False)
        case "gaussian":
            noise_sampler = lambda sigma, sigma_next: torch.randn_like(x)
        case "uniform":
            noise_sampler = lambda sigma, sigma_next: (torch.rand_like(x) - 0.5) * 2 * 1.73
        case "highres-pyramid":
            noise_sampler = lambda sigma, sigma_next: highres_pyramid_noise_like(x)
        case "perlin":
            noise_sampler = lambda sigma, sigma_next: rand_perlin_like(x)
        case "laplacian":
            noise_sampler = lambda sigma, sigma_next: rand_laplacian_like(x)
        case _:
            noise_sampler = lambda sigma, sigma_next: (torch.rand_like(x) - 0.5) * 2 * 1.73
    return sampler_supreme(model, x, sigmas, extra_args=extra_args, callback=callback, disable=disable, s_noise=s_noise, noise_sampler=noise_sampler, eta=eta, step_method=step_method, centralization=centralization, normalization=normalization, edge_enhancement=edge_enhancement, perphist=perphist)

# Add your personal samplers below here, just for formatting purposes ;3

# Add any extra samplers to the following dictionary
extra_samplers = {
    "res_momentumized": sample_res_solver,
    "dpmpp_dualsde_momentumized": sample_dpmpp_dualsdemomentum,
    "clyb_4m_sde_momentumized": sample_clyb_4m_sde,
    "ttm": sample_ttmcustom,
    "lcm_custom_noise": sample_lcmcustom,
    "euler_ancestral_dancing": sample_euler_ancestral_dancing,
    "dpmpp_3m_sde_dynamic_eta": sample_dpmpp_3m_sde_dynamic_eta,
    "supreme": sample_supreme,
}

discard_penultimate_sigma_samplers = set((
    "dpmpp_dualsde_momentumized",
    "clyb_4m_sde_momentumized"
))

def get_sigmas_simple_exponential(model, steps):
    s = model.model_sampling
    sigs = []
    ss = len(s.sigmas) / steps
    for x in range(steps):
        sigs += [float(s.sigmas[-(1 + int(x * ss))])]
    sigs += [0.0]
    sigs = torch.FloatTensor(sigs)
    exp = torch.exp(torch.log(torch.linspace(1, 0, steps + 1)))
    return sigs * exp

extra_schedulers = {
    "simple_exponential": get_sigmas_simple_exponential
}
