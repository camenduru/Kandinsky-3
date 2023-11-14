from typing import Optional, Union, List
import PIL
import io
import os
import math
import random
import omegaconf 
import numpy as np
import pandas as pd
from tqdm import tqdm
from PIL import Image, ImageDraw, ImageFont

import torch
import torchvision.transforms as T
from torch import einsum
from einops import repeat

from kandinsky3.model.unet import UNet
from kandinsky3.movq import MoVQ
from kandinsky3.condition_encoders import T5TextConditionEncoder
from kandinsky3.condition_processors import T5TextConditionProcessor
from kandinsky3.model.diffusion import BaseDiffusion, get_named_beta_schedule
from kandinsky3.utils import generate_mask, resize_image_for_diffusion


class Kandinsky3InpaintingPipeline:
    
    def __init__(
        self, 
        device: Union[str, torch.device], 
        unet: UNet,
        null_embedding: torch.Tensor,
        t5_processor: T5TextConditionProcessor,
        t5_encoder: T5TextConditionEncoder,
        movq: MoVQ,
        fp16: bool = True
    ):
        self.device = device
        self.fp16 = fp16
        self.to_pil = T.ToPILImage()
        self.to_tensor = T.ToTensor()
        
        self.unet = unet
        self.null_embedding = null_embedding
        self.t5_processor = t5_processor
        self.t5_encoder = t5_encoder
        self.movq = movq
        
    def shared_step(self, batch: dict) -> dict:
        image = batch['image']
        condition_model_input = batch['text']
        
        bs = image.shape[0]
        
        masked_latent = None
        mask = batch['mask']

        if 'masked_image' in batch:
            masked_latent = batch['masked_image']
        elif self.unet.in_layer.in_channels == 9:
            masked_latent = image.masked_fill((1 - mask).bool(), 0)
        else:
            raise ValueError()
            
        masked_latent = self.movq.encode(masked_latent)
        mask = torch.nn.functional.interpolate(mask, size=(masked_latent.shape[2], masked_latent.shape[3]))
        context, context_mask = self.t5_encoder(condition_model_input)
        
        return {
            'context': context, 
            'context_mask': context_mask, 
            'image': image,
            'masked_latent': masked_latent,
            'mask': mask
        }
    
    def prepare_batch(
        self, 
        text: str, 
        image: PIL.Image.Image,
        mask: PIL.Image.Image,
    ) -> dict:
        condition_model_input = self.t5_processor.encode(text=text)
        batch = {
            'image': self.to_tensor(resize_image_for_diffusion(image.convert("RGB"))) * 2 - 1,
            'mask': 1 - self.to_tensor(resize_image_for_diffusion(mask.convert('L'))),
            'text' : condition_model_input
        }
        
        batch['image'] = batch['image'].unsqueeze(0).to(self.device)
        batch['text']['t5']['input_ids'] = batch['text']['t5']['input_ids'].unsqueeze(0).to(self.device)
        batch['text']['t5']['attention_mask'] = batch['text']['t5']['attention_mask'].unsqueeze(0).to(self.device)
        batch['mask'] = batch['mask'].unsqueeze(0).to(self.device)
        return batch
        
    def __call__(
        self, 
        text: str, 
        image: PIL.Image.Image,
        mask: PIL.Image.Image,
        images_num: int = 1, 
        bs: int = 1, 
        steps: int = 50,
        guidance_weight_text: float = 4,
    ) -> List[PIL.Image.Image]:
    
        with torch.no_grad():
            batch = self.prepare_batch(text, image, mask)
            processed = self.shared_step(batch)
        
        context_mask = processed['context_mask'].repeat_interleave(bs, dim=0)
        context = processed['context'].repeat_interleave(bs, dim=0)

        mask = processed['mask'].repeat_interleave(bs, dim=0) 
        masked_latent = processed['masked_latent'].repeat_interleave(bs, dim=0)
        
        bs = masked_latent.shape[0]
        
        betas = get_named_beta_schedule('cosine', 50)
        base_diffusion = BaseDiffusion(betas, percentile=0.95)
        base_diffusion.jump_length = 0
        
        with torch.cuda.amp.autocast(enabled=self.fp16):
            with torch.no_grad():
                images = base_diffusion.p_sample_loop(
                    self.unet, (bs, 4, masked_latent.shape[2], masked_latent.shape[3]), self.device, 
                    context, context_mask, self.null_embedding, guidance_weight_text, 
                    mask=mask, masked_latent=masked_latent, 
                )

                images = self.movq.decode(images)
            
        images = torch.clip((images + 1.) / 2., 0., 1.).cpu()
        return self.to_pil(images[0])
