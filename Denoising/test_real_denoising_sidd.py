import os
import yaml
import argparse
import time

import numpy as np
from tqdm import tqdm
from skimage import img_as_ubyte
import scipy.io as sio

import torch
import torch.nn as nn
import utils_tool

from skimage.metrics import peak_signal_noise_ratio as compare_psnr
from skimage.metrics import structural_similarity as compare_ssim

from basicsr.models.archs.kbnet_s_arch import KBNet_s

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

parser = argparse.ArgumentParser(description='Real Image Denoising using Restormer')

parser.add_argument('--input_dir', default='./Datasets/test/SIDD/', type=str, help='Directory of validation images')
parser.add_argument('--result_dir', default='./results/Real_Denoising/SIDD/', type=str, help='Directory for results')
parser.add_argument('--save_images', default=True, help='Save denoised images in result directory')
parser.add_argument('--yml', default=None, type=str, help='Directory for results')

args = parser.parse_args()

yaml_file = args.yml
x = yaml.load(open(yaml_file, mode='r'), Loader=Loader)

cfg_name = os.path.basename(yaml_file).split('.')[0]

pth_path = x['path']['pretrain_network_g']
print('**', yaml_file, pth_path)

s = x['network_g'].pop('type')

model_restoration = eval(s)(**x['network_g'])

checkpoint = torch.load(pth_path)
model_restoration.load_state_dict(checkpoint['model'])
print("===>Testing using weights: ")
model_restoration.cuda()
model_restoration = nn.DataParallel(model_restoration)
model_restoration.eval()

from ptflops import get_model_complexity_info
m = model_restoration.module if hasattr(model_restoration, "module") else model_restoration
macs, params = get_model_complexity_info(
    m, (3, 256, 256),
    as_strings=True,  # so you get “X.XX GMac”, “Y.YY M”
    print_per_layer_stat=False,
    verbose=False
)
print(f"===>  MACs: {macs} | Params: {params}")

########################

result_dir_mat = os.path.join(args.result_dir, 'mat')
os.makedirs(result_dir_mat, exist_ok=True)

if args.save_images:
    result_dir_png = os.path.join(args.result_dir, 'png')
    os.makedirs(result_dir_png, exist_ok=True)

# load gt patches and init psnr ssim recorder
filepath = os.path.join(args.input_dir, 'ValidationGtBlocksSrgb.mat')
img = sio.loadmat(filepath)
gt = np.float32(np.array(img['ValidationGtBlocksSrgb']))
gt /= 255.
print('gt', gt.shape, gt.max(), gt.min())
res = {'psnr': [], 'ssim': []}

# Process data
filepath = os.path.join(args.input_dir, 'ValidationNoisyBlocksSrgb.mat')
img = sio.loadmat(filepath)
Inoisy = np.float32(np.array(img['ValidationNoisyBlocksSrgb']))
Inoisy /= 255.
restored = np.zeros_like(Inoisy)

torch.cuda.reset_peak_memory_stats()
latencies = []

with torch.no_grad():
    for i in tqdm(range(40)):
        for k in range(32):
            noisy_patch = torch.from_numpy(Inoisy[i, k, :, :, :]).unsqueeze(0).permute(0, 3, 1, 2).cuda()

            # measure start
            torch.cuda.synchronize()
            t0 = time.perf_counter()

            restored_patch = model_restoration(noisy_patch)

            torch.cuda.synchronize()
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000)   # ms

            restored_patch = torch.clamp(restored_patch, 0, 1).cpu().detach().permute(0, 2, 3, 1).squeeze(0)
            restored[i, k, :, :, :] = restored_patch

            # save psrn and ssim
            # print(type(restored_patch))  # torch.Tensor
            res['psnr'].append(compare_psnr(gt[i, k], restored_patch.numpy(), data_range=1.0))
            res['ssim'].append(compare_ssim(gt[i, k], restored_patch.numpy(), channel_axis=-1, data_range=1.0))

            if args.save_images:
                save_file = os.path.join(result_dir_png,
                                         '%04d_%02d_%.2f_%s.png' % (i + 1, k + 1, res['psnr'][-1], cfg_name))
                utils_tool.save_img(save_file, img_as_ubyte(restored_patch))

print(f'{cfg_name} psnr %.2f ssim %.3f' % (np.mean(res['psnr']), np.mean(res['ssim'])))

mean_latency = sum(latencies) / len(latencies)
peak_mem = torch.cuda.max_memory_allocated() / (1024**2)  # MiB

print(f"Mean latency: {mean_latency:.2f} ms (±{np.std(latencies):.2f})")
print(f"Peak GPU memory: {peak_mem:.1f} MiB")

# save denoised data
sio.savemat(os.path.join(result_dir_mat, 'Idenoised.mat'), {"Idenoised": restored, })
