import os
import imageio
import numpy as np
from PIL import Image
for exp_path in ['/root/lihao/UC-NeRF/exp/checkpoints/carla_0603_town1','exp/checkpoints/carla_0603_town1_cam3','exp/checkpoints/carla_0603_town4','exp/checkpoints/carla_0603_town4_cam3']:
    for offset in ['test_preds_offset0','test_preds_offset1','test_preds_offset2','test_preds_offset4']:
        out_dir = os.path.join(exp_path,offset)
        path_fn = lambda x: os.path.join(out_dir, x)
        video_rgb_pred = [] 
        video_depth_pred = []
        # for idx in range(30):
        #     path_fn(f'color_{idx:03d}.png')
        #     path_fn(f'distance_mean_{idx:03d}.png')
        #     image = np.array(Image.open(path_fn(f'color_{idx:03d}.png')))
        #     depth = np.array(Image.open(path_fn(f'distance_mean_{idx:03d}.png')))

        with imageio.get_writer(path_fn('video_rgb_pred.mp4'), fps=30) as writer:
            for idx in range(30):
                path_fn(f'color_{idx:03d}.png')
                image = np.array(Image.open(path_fn(f'color_{idx:03d}.png')))
                writer.append_data(image)
        with imageio.get_writer(path_fn('video_depth_pred.mp4'), fps=30) as writer:
            for idx in range(30):
                path_fn(f'distance_mean_{idx:03d}.png')
                depth = np.array(Image.open(path_fn(f'distance_mean_{idx:03d}.png')))
                writer.append_data(depth)


        #     video_rgb_pred.append(image)
        #     video_depth_pred.append(depth)

        
        # imageio.mimwrite(path_fn('video_rgb_pred.mp4'), video_rgb_pred)
        # imageio.mimwrite(path_fn('video_depth_pred.mp4'), video_rgb_pred)