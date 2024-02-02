from .other_samplers.refined_exp_solver import sample_refined_exp_s
import comfy.samplers
import comfy.sample
from comfy.k_diffusion import sampling as k_diffusion_sampling
import latent_preview
import torch
import numpy as np
from tqdm.auto import trange

import random
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

class SamplerRES_MOMENTUMIZED:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"noise_sampler_type": (["gaussian", "uniform", "brownian", "highres-pyramid", "perlin"], ),
                     "momentum": ("FLOAT", {"default": 0.5, "min": -1.0, "max": 1.0, "step":0.01}),
                     "denoise_to_zero": ("BOOLEAN", {"default": True}),
                     "simple_phi_calc": ("BOOLEAN", {"default": False}),
                     "ita": ("FLOAT", {"default": 0.25, "min": 0.0, "max": 100.0, "step":0.01, "round": False}),
                     "c2": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step":0.01, "round": False}),
                      }
               }
    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling"

    FUNCTION = "get_sampler"

    def get_sampler(self, noise_sampler_type, momentum, denoise_to_zero, simple_phi_calc, ita, c2):
        sampler = comfy.samplers.ksampler("res_momentumized", {"noise_sampler": noise_sampler_type, "denoise_to_zero": denoise_to_zero, "simple_phi_calc": simple_phi_calc, "c2": c2, "ita": torch.Tensor((ita,)), "momentum": momentum})
        return (sampler, )

class SamplerDPMPP_DUALSDE_MOMENTUMIZED:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"noise_sampler_type": (["gaussian", "uniform", "brownian", "perlin"], ),
                     "momentum": ("FLOAT", {"default": 0.5, "min": -1.0, "max": 1.0, "step":0.01}),
                     "eta": ("FLOAT", {"default": 1, "min": 0.0, "max": 100.0, "step":0.01}),
                     "s_noise": ("FLOAT", {"default": 1, "min": 0.0, "max": 100.0, "step":0.01}),
                     "r": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 100.0, "step":0.01}),
                      }
               }
    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling"

    FUNCTION = "get_sampler"

    def get_sampler(self, noise_sampler_type, momentum, eta, s_noise, r,):
        sampler = comfy.samplers.ksampler("dpmpp_dualsde_momentumized", {"noise_sampler": noise_sampler_type, "eta": eta, "s_noise": s_noise, "r": r, "momentum": momentum})
        return (sampler, )

class SamplerTTM:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"noise_sampler_type": (["gaussian", "uniform", "brownian"], ),
                     "eta": ("FLOAT", {"default": 1, "min": 0.0, "max": 100.0, "step":0.01}),
                     "s_noise": ("FLOAT", {"default": 1, "min": 0.0, "max": 100.0, "step":0.01}),
                      }
               }
    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling"

    FUNCTION = "get_sampler"

    def get_sampler(self, noise_sampler_type, eta, s_noise):
        sampler = comfy.samplers.ksampler("ttm", {"noise_sampler": noise_sampler_type, "eta": eta, "s_noise": s_noise})
        return (sampler, )


class SamplerLCMCustom:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"noise_sampler_type": (["gaussian", "uniform", "brownian"], ),
                      }
               }
    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling"

    FUNCTION = "get_sampler"

    def get_sampler(self, noise_sampler_type):
        sampler = comfy.samplers.ksampler("lcm_custom_noise", {"noise_sampler": noise_sampler_type})
        return (sampler, )

class SamplerCLYB_4M_SDE_MOMENTUMIZED:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"noise_sampler_type": (["gaussian", "uniform", "brownian", "highres-pyramid", "perlin"], ),
                     "momentum": ("FLOAT", {"default": 0.5, "min": -1.0, "max": 1.0, "step":0.01}),
                     "eta": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step":0.01}),
                     "s_noise": ("FLOAT", {"default": 1, "min": 0.0, "max": 100.0, "step":0.01}),
                      }
               }
    RETURN_TYPES = ("SAMPLER",)
    CATEGORY = "sampling/custom_sampling"

    FUNCTION = "get_sampler"

    def get_sampler(self, noise_sampler_type, eta, s_noise, momentum):
        sampler = comfy.samplers.ksampler("clyb_4m_sde_momentumized", {"noise_sampler": noise_sampler_type, "eta": eta, "s_noise": s_noise, "momentum": momentum})
        return (sampler, )

from comfy import model_management
import comfy.utils
import comfy.conds
from comfy.sample import prepare_sampling, cleanup_additional_models, get_models_from_cond

def mixture_sample(model, model2, noise, positive, positive2, negative, negative2, cfg, cfg2, device, device2, sampler, sampler2, sigmas, sigmas2, model_options={}, model_options2={}, latent_image=None, denoise_mask=None, denoise_mask2=None, callback=None, callback2=None, disable_pbar=False, seed=None):
    positive = positive[:]
    negative = negative[:]
    positive2 = positive2[:]
    negative2 = negative2[:]

    comfy.samplers.resolve_areas_and_cond_masks(positive, noise.shape[2], noise.shape[3], device)
    comfy.samplers.resolve_areas_and_cond_masks(negative, noise.shape[2], noise.shape[3], device)
    comfy.samplers.resolve_areas_and_cond_masks(positive2, noise.shape[2], noise.shape[3], device2)
    comfy.samplers.resolve_areas_and_cond_masks(negative2, noise.shape[2], noise.shape[3], device2)

    model_wrap = comfy.samplers.wrap_model(model)
    model_wrap2 = comfy.samplers.wrap_model(model2)

    comfy.samplers.calculate_start_end_timesteps(model, negative)
    comfy.samplers.calculate_start_end_timesteps(model, positive)
    comfy.samplers.calculate_start_end_timesteps(model2, negative2)
    comfy.samplers.calculate_start_end_timesteps(model2, positive2)

    if latent_image is not None:
        latent_image = model.process_latent_in(latent_image)

    if hasattr(model, 'extra_conds'):
        positive = comfy.samplers.encode_model_conds(model.extra_conds, positive, noise, device, "positive", latent_image=latent_image, denoise_mask=denoise_mask, seed=seed)
        negative = comfy.samplers.encode_model_conds(model.extra_conds, negative, noise, device, "negative", latent_image=latent_image, denoise_mask=denoise_mask, seed=seed)
    if hasattr(model2, 'extra_conds'):
        positive = comfy.samplers.encode_model_conds(model2.extra_conds, positive2, noise, device2, "positive", latent_image=latent_image, denoise_mask=denoise_mask2, seed=seed)
        negative = comfy.samplers.encode_model_conds(model2.extra_conds, negative2, noise, device2, "negative", latent_image=latent_image, denoise_mask=denoise_mask2, seed=seed)

    #make sure each cond area has an opposite one with the same area
    for c in positive:
        comfy.samplers.create_cond_with_same_area_if_none(negative, c)
    for c in negative:
        comfy.samplers.create_cond_with_same_area_if_none(positive, c)
    for c in positive2:
        comfy.samplers.create_cond_with_same_area_if_none(negative2, c)
    for c in negative2:
        comfy.samplers.create_cond_with_same_area_if_none(positive2, c)

    comfy.samplers.pre_run_control(model, negative + positive)
    comfy.samplers.pre_run_control(model2, negative2 + positive2)

    comfy.samplers.apply_empty_x_to_equal_area(list(filter(lambda c: c.get('control_apply_to_uncond', False) == True, positive)), negative, 'control', lambda cond_cnets, x: cond_cnets[x])
    comfy.samplers.apply_empty_x_to_equal_area(positive, negative, 'gligen', lambda cond_cnets, x: cond_cnets[x])

    comfy.samplers.apply_empty_x_to_equal_area(list(filter(lambda c: c.get('control_apply_to_uncond', False) == True, positive2)), negative2, 'control', lambda cond_cnets, x: cond_cnets[x])
    comfy.samplers.apply_empty_x_to_equal_area(positive2, negative2, 'gligen', lambda cond_cnets, x: cond_cnets[x])

    extra_args = {"cond":positive, "uncond":negative, "cond_scale": cfg, "model_options": model_options, "seed":seed}
    extra_args2 = {"cond":positive2, "uncond":negative2, "cond_scale": cfg, "model_options": model_options2, "seed":seed}
    samples = None
    temp_sigmas = sigmas
    temp_sigmas2 = sigmas2
    #samples = sampler.sample(model_wrap, sigmas, extra_args, callback, noise, latent_image, denoise_mask, True)
    for i in trange(len(sigmas) - 1, disable=disable_pbar):
        last_step = i + 1
        start_step = i
        if last_step is not None and last_step < (len(sigmas) - 1):
            temp_sigmas = sigmas[:last_step + 1]
            temp_sigmas2 = sigmas2[:last_step + 1]
        if start_step is not None:
            if start_step < (len(sigmas) - 1):
                temp_sigmas = temp_sigmas[start_step:]
                temp_sigmas2 = temp_sigmas2[start_step:]
            else:
                if latent_image is not None:
                    return latent_image
                else:
                    return torch.zeros_like(noise)
        if len(temp_sigmas) != 2:
            temp_sigmas = sigmas[-2:]
            temp_sigmas2 = sigmas2[-2:]
        if (i % 2) == 0:
            #print(temp_sigmas)
            samples = sampler.sample(model_wrap, temp_sigmas, extra_args, callback, noise.to(device) if i is 0 else torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device=device), samples if samples is not None else latent_image, denoise_mask, True)
        else:
            #print(temp_sigmas)
            samples = sampler2.sample(model_wrap2, temp_sigmas2, extra_args2, callback2, noise.to(device2) if i is 0 else torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device=device2), samples if samples is not None else latent_image, denoise_mask2, True)
    return model.process_latent_out(samples.to(torch.float32))

def sample_mixture(model, model2, noise, cfg, cfg2, sampler, sampler2, sigmas, sigmas2, positive, negative, latent_image, noise_mask=None, callback=None, callback2=None, disable_pbar=False, seed=None):
    real_model, positive_copy, negative_copy, noise_mask, models = prepare_sampling(model, noise.shape, positive, negative, noise_mask)
    real_model2, positive_copy2, negative_copy2, noise_mask2, models2 = prepare_sampling(model2, noise.shape, positive, negative, noise_mask)
    noise = noise.to(model.load_device)
    latent_image = latent_image.to(model.load_device)
    sigmas = sigmas.to(model.load_device)
    sigmas2 = sigmas2.to(model.load_device)

    samples = mixture_sample(real_model, real_model2, noise, positive_copy, positive_copy2, negative_copy, negative_copy2, cfg, cfg2, model.load_device, model2.load_device, sampler, sampler2, sigmas, sigmas2, model_options=model.model_options, model_options2=model2.model_options, latent_image=latent_image, denoise_mask=noise_mask, denoise_mask2=noise_mask2, callback=callback, callback2=callback2, disable_pbar=disable_pbar, seed=seed)

    samples = samples.to(comfy.model_management.intermediate_device())
    cleanup_additional_models(models)
    cleanup_additional_models(models2)
    cleanup_additional_models(set(get_models_from_cond(positive_copy, "control") + get_models_from_cond(negative_copy, "control")))
    cleanup_additional_models(set(get_models_from_cond(positive_copy2, "control") + get_models_from_cond(negative_copy2, "control")))
    return samples

class SamplerCustomNoise:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"model": ("MODEL",),
                    "add_noise": ("BOOLEAN", {"default": True}),
                    "noise_is_latent": ("BOOLEAN", {"default": False}),
                    "noise_type": (["gaussian", "uniform", "pyramid", "power"], ),
                    "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.5, "round": 0.01}),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "sampler": ("SAMPLER", ),
                    "sigmas": ("SIGMAS", ),
                    "latent_image": ("LATENT", ),
                     }
                }

    RETURN_TYPES = ("LATENT","LATENT")
    RETURN_NAMES = ("output", "denoised_output")

    FUNCTION = "sample"

    CATEGORY = "sampling/custom_sampling"

    def sample(self, model, add_noise, noise_is_latent, noise_type, noise_seed, cfg, positive, negative, sampler, sigmas, latent_image):
        latent = latent_image
        latent_image = latent["samples"]
        if not add_noise:
            noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
        else:
            batch_inds = latent["batch_index"] if "batch_index" in latent else None
            noise = prepare_noise(latent_image, noise_seed, noise_type, batch_inds)
        
        if noise_is_latent:
            noise += latent_image.cpu()# * noise.std()
            noise.sub_(noise.mean()).div_(noise.std())

        noise_mask = None
        if "noise_mask" in latent:
            noise_mask = latent["noise_mask"]

        x0_output = {}

        callback = latent_preview.prepare_callback(model, sigmas.shape[-1] - 1, x0_output)

        disable_pbar = False
        samples = comfy.sample.sample_custom(model, noise, cfg, sampler, sigmas, positive, negative, latent_image, noise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar, seed=noise_seed)

        out = latent.copy()
        out["samples"] = samples
        if "x0" in x0_output:
            out_denoised = latent.copy()
            out_denoised["samples"] = model.model.process_latent_out(x0_output["x0"].cpu())
        else:
            out_denoised = out
        return (out, out_denoised)

class SamplerCustomNoiseDuo:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"model": ("MODEL",),
                    "add_noise": ("BOOLEAN", {"default": True}),
                    "add_noise_pass2": ("BOOLEAN", {"default": True}),
                    "return_noisy_pass1": ("BOOLEAN", {"default": False}),
                    "noise_type": (["gaussian", "uniform", "pyramid", "power"], ),
                    "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                    "cfg2": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "sampler": ("SAMPLER", ),
                    "sampler2": ("SAMPLER", ),
                    "sigmas": ("SIGMAS", ),
                    "sigmas2": ("SIGMAS", ),
                    "hr_upscale": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 9.0, "step":0.1, "round": 0.01}),
                    "latent_image": ("LATENT", ),
                     }
                }

    RETURN_TYPES = ("LATENT","LATENT")
    RETURN_NAMES = ("output", "denoised_output")

    FUNCTION = "sample"

    CATEGORY = "sampling/custom_sampling"

    def sample(self, model, add_noise, add_noise_pass2, return_noisy_pass1, noise_type, noise_seed, cfg, cfg2, positive, negative, sampler, sampler2, sigmas, sigmas2, hr_upscale, latent_image):
        latent = latent_image
        latent_image = latent["samples"]
        if not add_noise:
            noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
        else:
            batch_inds = latent["batch_index"] if "batch_index" in latent else None
            noise = prepare_noise(latent_image, noise_seed, noise_type, batch_inds)

        noise_mask = None
        if "noise_mask" in latent:
            noise_mask = latent["noise_mask"]

        x0_output = {}

        callback = latent_preview.prepare_callback(model, sigmas.shape[-1] - 1, x0_output)

        disable_pbar = False

        samples = comfy.sample.sample_custom(model, noise, cfg, sampler, sigmas, positive, negative, latent_image, noise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar, seed=noise_seed)

        if not return_noisy_pass1:
            out_denoised = latent.copy()
            samples = model.model.process_latent_out(x0_output["x0"].cpu())
        if hr_upscale > 1.0:
            if "noise_mask" in latent:
                noise_mask = comfy.utils.common_upscale(noise_mask, (int)(noise_mask.shape[-1] * hr_upscale), (int)(noise_mask.shape[-2] * hr_upscale), "bislerp", "disabled")
            samples = comfy.utils.common_upscale(samples, (int)(samples.shape[-1] * hr_upscale), (int)(samples.shape[-2] * hr_upscale), "bislerp", "disabled")
            noise = prepare_noise(samples, noise_seed, noise_type, batch_inds)

        samples = comfy.sample.sample_custom(model, noise if add_noise_pass2 else torch.zeros(samples.size(), dtype=samples.dtype, layout=samples.layout, device="cpu"), cfg2, sampler2, sigmas2, positive, negative, samples, noise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar, seed=noise_seed)

        out = latent.copy()
        out["samples"] = samples
        if "x0" in x0_output:
            out_denoised = latent.copy()
            out_denoised["samples"] = model.model.process_latent_out(x0_output["x0"].cpu())
        else:
            out_denoised = out
        return (out, out_denoised)

class SamplerCustomModelMixtureDuo:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"model": ("MODEL",),
                    "model2": ("MODEL",),
                    "add_noise": ("BOOLEAN", {"default": True}),
                    "add_noise_pass2": ("BOOLEAN", {"default": True}),
                    "return_noisy_pass1": ("BOOLEAN", {"default": False}),
                    "noise_type": (["gaussian", "uniform", "pyramid", "power"], ),
                    "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                    "cfg2": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "sampler": ("SAMPLER", ),
                    "sampler2": ("SAMPLER", ),
                    "sigmas": ("SIGMAS", ),
                    "sigmas2": ("SIGMAS", ),
                    "hr_upscale": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 9.0, "step":0.1, "round": 0.01}),
                    "latent_image": ("LATENT", ),
                     }
                }

    RETURN_TYPES = ("LATENT","LATENT")
    RETURN_NAMES = ("output", "denoised_output")

    FUNCTION = "sample"

    CATEGORY = "sampling/custom_sampling"

    def sample(self, model, model2, add_noise, add_noise_pass2, return_noisy_pass1, noise_type, noise_seed, cfg, cfg2, positive, negative, sampler, sampler2, sigmas, sigmas2, hr_upscale, latent_image):
        latent = latent_image
        latent_image = latent["samples"]
        if not add_noise:
            noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
        else:
            batch_inds = latent["batch_index"] if "batch_index" in latent else None
            noise = prepare_noise(latent_image, noise_seed, noise_type, batch_inds)

        noise_mask = None
        if "noise_mask" in latent:
            noise_mask = latent["noise_mask"]

        x0_output = {}

        callback = latent_preview.prepare_callback(model, sigmas.shape[-1] - 1, x0_output)
        callback2 = latent_preview.prepare_callback(model2, sigmas.shape[-1] - 1, x0_output)

        disable_pbar = False

        samples = sample_mixture(model, model2, noise, cfg, cfg2, sampler, sampler2, sigmas, sigmas2, positive, negative, latent_image, noise_mask=noise_mask, callback=callback, callback2=callback2, disable_pbar=disable_pbar, seed=noise_seed)

        #if not return_noisy_pass1:
        #    out_denoised = latent.copy()
        #    samples = model.model.process_latent_out(x0_output["x0"].cpu())
        #if hr_upscale > 1.0:
        #    if "noise_mask" in latent:
        #        noise_mask = comfy.utils.common_upscale(noise_mask, (int)(noise_mask.shape[-1] * hr_upscale), (int)(noise_mask.shape[-2] * hr_upscale), "bislerp", "disabled")
        #    samples = comfy.utils.common_upscale(samples, (int)(samples.shape[-1] * hr_upscale), (int)(samples.shape[-2] * hr_upscale), "bislerp", "disabled")
        #    noise = prepare_noise(samples, noise_seed, noise_type, batch_inds)

        #samples = sample_mixture(model, model2, noise if add_noise_pass2 else torch.zeros(samples.size(), dtype=samples.dtype, layout=samples.layout, device="cpu"), cfg2, sampler2, sigmas2, positive, negative, samples, noise_mask=noise_mask, callback=callback, callback2=callback2, disable_pbar=disable_pbar, seed=noise_seed)

        out = latent.copy()
        out["samples"] = samples
        if "x0" in x0_output:
            out_denoised = latent.copy()
            out_denoised["samples"] = model.model.process_latent_out(x0_output["x0"].cpu())
        else:
            out_denoised = out
        return (out, out_denoised)