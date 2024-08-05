import abc
import copy
import json
import os
import cv2
from internal import camera_utils
from internal import configs
# from internal import image as lib_image
from internal import raw_utils
from internal import utils
from internal import train_utils
import matplotlib as mpl
from collections import defaultdict
import numpy as np
from PIL import Image
import torch
from tqdm import tqdm
# This is ugly, but it works.
import sys
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp
from mpl_toolkits.mplot3d import axes3d
import matplotlib.pyplot as plt
import pickle
from scipy.spatial.transform import Rotation as R
import matplotlib.cm as cm


sys.path.insert(0, 'internal/pycolmap')
sys.path.insert(0, 'internal/pycolmap/pycolmap')
# import pycolmap

def inter_two_poses(pose_a, pose_b, alpha):
    ret = np.zeros([3, 4], dtype=np.float64)
    rot_a = R.from_matrix(pose_a[:3, :3])
    rot_b = R.from_matrix(pose_b[:3, :3])
    key_rots = R.from_matrix(np.stack([pose_a[:3, :3], pose_b[:3, :3]], 0))
    key_times = [0, 1]
    slerp = Slerp(key_times, key_rots)
    rot = slerp(1. - alpha)
    ret[:3, :3] = rot.as_matrix()
    ret[:3, 3] = (pose_a * alpha + pose_b * (1. - alpha))[:3, 3]
    return ret

def inter_poses(key_poses, n_out_poses, sigma=-1.):
    n_key_poses = len(key_poses)
    out_poses = []
    for i in range(n_out_poses):
        cur_w = i / n_out_poses
        cur_pose = inter_two_poses(key_poses[1], key_poses[0], cur_w)
        out_poses.append(cur_pose)

    return np.stack(out_poses)


def depth2distance(depth, intrinsics):
    h, w = depth.shape[-2:]
    fx, fy, cx, cy = intrinsics
    u = np.expand_dims(np.arange(w), 0).repeat(h, axis=0)
    v = np.expand_dims(np.arange(h), 1).repeat(w, axis=1)
    u_u0_by_fx = (u - cx) / fx
    v_v0_by_fy = (v - cy) / fy
    distance = depth.copy()
    distance *= np.sqrt(u_u0_by_fx**2 + v_v0_by_fy**2 + 1)
    return distance


def load_dataset(split, train_dir, config: configs.Config):
    """Loads a split of a dataset using the data_loader specified by `config`."""
    dataset_dict = {
        'waymov2': WaymoV2,
        'carlc':Carlc
    }
    return dataset_dict[config.dataset_loader](split, train_dir, config)





def load_blender_posedata(data_dir, split=None):
    """Load poses from `transforms.json` file, as used in Blender/NGP datasets."""
    suffix = '' if split is None else f'_{split}'
    pose_file = os.path.join(data_dir, f'transforms{suffix}.json')
    with utils.open_file(pose_file, 'r') as fp:
        meta = json.load(fp)
    names = []
    poses = []
    for _, frame in enumerate(meta['frames']):
        filepath = os.path.join(data_dir, frame['file_path'])
        if utils.file_exists(filepath):
            names.append(frame['file_path'].split('/')[-1])
            poses.append(np.array(frame['transform_matrix'], dtype=np.float32))
    poses = np.stack(poses, axis=0)

    w = meta['w']
    h = meta['h']
    cx = meta['cx'] if 'cx' in meta else w / 2.
    cy = meta['cy'] if 'cy' in meta else h / 2.
    if 'fl_x' in meta:
        fx = meta['fl_x']
    else:
        fx = 0.5 * w / np.tan(0.5 * float(meta['camera_angle_x']))
    if 'fl_y' in meta:
        fy = meta['fl_y']
    else:
        fy = 0.5 * h / np.tan(0.5 * float(meta['camera_angle_y']))
    pixtocam = np.linalg.inv(camera_utils.intrinsic_matrix(fx, fy, cx, cy))
    coeffs = ['k1', 'k2', 'p1', 'p2']
    if not any([c in meta for c in coeffs]):
        params = None
    else:
        params = {c: (meta[c] if c in meta else 0.) for c in coeffs}
    camtype = camera_utils.ProjectionType.PERSPECTIVE
    return names, poses, pixtocam, params, camtype


class Dataset(torch.utils.data.Dataset):
    """Dataset Base Class.

  Base class for a NeRF dataset. Creates batches of ray and color data used for
  training or rendering a NeRF model.

  Each subclass is responsible for loading images and camera poses from disk by
  implementing the _load_renderings() method. This data is used to generate
  train and test batches of ray + color data for feeding through the NeRF model.
  The ray parameters are calculated in _generate_rays().

  The public interface mimics the behavior of a standard machine learning
  pipeline dataset provider that can provide infinite batches of data to the
  training/testing pipelines without exposing any details of how the batches are
  loaded/created or how this is parallelized. Therefore, the initializer runs
  all setup, including data loading from disk using _load_renderings(), and
  begins the thread using its parent start() method. After the initializer
  returns, the caller can request batches of data straight away.

  The internal self._queue is initialized as queue.Queue(3), so the infinite
  loop in run() will block on the call self._queue.put(self._next_fn()) once
  there are 3 elements. The main thread training job runs in a loop that pops 1
  element at a time off the front of the queue. The Dataset thread's run() loop
  will populate the queue with 3 elements, then wait until a batch has been
  removed and push one more onto the end.

  This repeats indefinitely until the main thread's training loop completes
  (typically hundreds of thousands of iterations), then the main thread will
  exit and the Dataset thread will automatically be killed since it is a daemon.

  Attributes:
    alphas: np.ndarray, optional array of alpha channel data.
    cameras: tuple summarizing all camera extrinsic/intrinsic/distortion params.
    camtoworlds: np.ndarray, a list of extrinsic camera pose matrices.
    camtype: camera_utils.ProjectionType, fisheye or perspective camera.
    data_dir: str, location of the dataset on disk.
    disp_images: np.ndarray, optional array of disparity (inverse depth) data.
    distortion_params: dict, the camera distortion model parameters.
    exposures: optional per-image exposure value (shutter * ISO / 1000).
    far: float, far plane value for rays.
    focal: float, focal length from camera intrinsics.
    height: int, height of images.
    images: np.ndarray, array of RGB image data.
    metadata: dict, optional metadata for raw datasets.
    near: float, near plane value for rays.
    normal_images: np.ndarray, optional array of surface normal vector data.
    pixtocams: np.ndarray, one or a list of inverse intrinsic camera matrices.
    pixtocam_ndc: np.ndarray, the inverse intrinsic matrix used for NDC space.
    poses: np.ndarray, optional array of auxiliary camera pose data.
    rays: utils.Rays, ray data for every pixel in the dataset.
    render_exposures: optional list of exposure values for the render path.
    render_path: bool, indicates if a smooth camera path should be generated.
    size: int, number of images in the dataset.
    split: str, indicates if this is a "train" or "test" dataset.
    width: int, width of images.
  """

    def __init__(self,
                 split: str,
                 data_dir: str,
                 config: configs.Config):
        super().__init__()

        # Initialize attributes
        self._patch_size = max(config.patch_size, 1)
        self._batch_size = config.batch_size // config.world_size
        if self._patch_size ** 2 > self._batch_size:
            raise ValueError(f'Patch size {self._patch_size}^2 too large for ' +
                             f'per-process batch size {self._batch_size}')
        self._batching = utils.BatchingMethod(config.batching)
        self._use_tiffs = config.use_tiffs
        self._load_disps = config.compute_disp_metrics
        self._load_normals = config.compute_normal_metrics
        self._load_segs = config.load_sky_segments
        self._num_border_pixels_to_mask = config.num_border_pixels_to_mask
        self._apply_bayer_mask = config.apply_bayer_mask
        self._render_spherical = False

        self.config = config
        self.global_rank = config.global_rank
        self.world_size = config.world_size
        self.mode = split
        # self.split = utils.DataSplit(split)
        self.data_dir = data_dir
        self.near = config.near
        self.far = config.far
        self.render_path = config.render_path
        self.distortion_params = None
        self.disp_images = None
        self.normal_images = None
        self.alphas = None
        self.poses = None
        self.pixtocam_ndc = None
        self.metadata = None
        self.camtype = camera_utils.ProjectionType.PERSPECTIVE
        self.exposures = None
        self.render_exposures = None

        # Providing type comments for these attributes, they must be correctly
        # initialized by _load_renderings() (see docstring) in any subclass.
        self.images: np.ndarray = None
        self.camtoworlds: np.ndarray = None
        self.pixtocams: np.ndarray = None
        self.height: int = None
        self.width: int = None

        # Load data from disk using provided config parameters.
        self._load_renderings(config)

        if self.render_path:
            if config.render_path_file is not None:
                with utils.open_file(config.render_path_file, 'rb') as fp:
                    render_poses = np.load(fp)
                self.camtoworlds = render_poses
            if config.render_resolution is not None:
                self.width, self.height = config.render_resolution
            if config.render_focal is not None:
                self.focal = config.render_focal
            if config.render_camtype is not None:
                if config.render_camtype == 'pano':
                    self._render_spherical = True
                else:
                    self.camtype = camera_utils.ProjectionType(config.render_camtype)

            self.distortion_params = None
            self.pixtocams = camera_utils.get_pixtocam(self.focal, self.width,
                                                       self.height)


        if self.config.virtual_poses and self.split == utils.DataSplit.TRAIN:
            self._n_examples = self.images.shape[0]
        else:
            self._n_examples = self.camtoworlds.shape[0]

        self.cameras = (self.pixtocams,
                        self.camtoworlds,
                        self.distortion_params,
                        self.pixtocam_ndc)

        # Seed the queue with one batch to avoid race condition.
        if self.mode == 'train' and not config.compute_visibility:
            self._next_fn = self._next_train
        else:
            self._next_fn = self._next_test

    @property
    def size(self):
        return self._n_examples

    def __len__(self):
        if self.mode == 'train' and not self.config.compute_visibility:
            return 1000
        else:
            return self._n_examples

    @abc.abstractmethod
    def _load_renderings(self, config):
        """Load images and poses from disk.

    Args:
      config: utils.Config, user-specified config parameters.
    In inherited classes, this method must set the following public attributes:
      images: [N, height, width, 3] array for RGB images.
      disp_images: [N, height, width] array for depth data (optional).
      normal_images: [N, height, width, 3] array for normals (optional).
      camtoworlds: [N, 3, 4] array of extrinsic pose matrices.
      poses: [..., 3, 4] array of auxiliary pose data (optional).
      pixtocams: [N, 3, 4] array of inverse intrinsic matrices.
      distortion_params: dict, camera lens distortion model parameters.
      height: int, height of images.
      width: int, width of images.
      focal: float, focal length to use for ideal pinhole rendering.
    """

    def _make_ray_batch(self,
                        pix_x_int,
                        pix_y_int,
                        cam_idx,
                        lossmult=None,
                        cam_idx_with_src=None, cam_idx_with_ref=None,
                        pixel_x_int_with_src=None, pixel_y_int_with_src=None,
                        pixel_x_int_with_ref=None, pixel_y_int_with_ref=None
                        ):
        """Creates ray data batch from pixel coordinates and camera indices.

    All arguments must have broadcastable shapes. If the arguments together
    broadcast to a shape [a, b, c, ..., z] then the returned utils.Rays object
    will have array attributes with shape [a, b, c, ..., z, N], where N=3 for
    3D vectors and N=1 for per-ray scalar attributes.

    Args:
      pix_x_int: int array, x coordinates of image pixels.
      pix_y_int: int array, y coordinates of image pixels.
      cam_idx: int or int array, camera indices.
      lossmult: float array, weight to apply to each ray when computing loss fn.

    Returns:
      A dict mapping from strings utils.Rays or arrays of image data.
      This is the batch provided for one NeRF train or test iteration.
    """
        if not self.config.virtual_poses or self.split == utils.DataSplit.TEST:
            broadcast_scalar = lambda x: np.broadcast_to(x, pix_x_int.shape)[..., None]
            ray_kwargs = {
                'lossmult': broadcast_scalar(1.) if lossmult is None else lossmult,
                'near': broadcast_scalar(self.near),
                'far': broadcast_scalar(self.far),
                'cam_idx': broadcast_scalar(cam_idx)
            }
        else:
            broadcast_scalar = lambda x: np.broadcast_to(x, pixel_x_int_with_src.shape)[..., None]
            ray_kwargs = {
                'lossmult': broadcast_scalar(1.) if lossmult is None else lossmult,
                'near': broadcast_scalar(self.near),
                'far': broadcast_scalar(self.far),
                'cam_idx': broadcast_scalar(cam_idx_with_src),
            }
        # Collect per-camera information needed for each ray.
        # if self.metadata is not None:
        #     # Exposure index and relative shutter speed, needed for RawNeRF.
        #     for key in ['exposure_idx', 'exposure_values']:
        #         idx = 0 if self.render_path else cam_idx
        #         ray_kwargs[key] = broadcast_scalar(self.metadata[key][idx])
        # if self.exposures is not None:
        #     idx = 0 if self.render_path else cam_idx
        #     ray_kwargs['exposure_values'] = broadcast_scalar(self.exposures[idx])
        # if self.render_path and self.render_exposures is not None:
        #     ray_kwargs['exposure_values'] = broadcast_scalar(
        #         self.render_exposures[cam_idx])

        if not self.config.virtual_poses or self.split == utils.DataSplit.TEST:
            pixels = dict(pix_x_int=pix_x_int, pix_y_int=pix_y_int, **ray_kwargs)

            # Slow path, do ray computation using numpy (on CPU).
            batch = camera_utils.cast_ray_batch(self.cameras, pixels, self.camtype)
            batch['cam_dirs'] = -self.camtoworlds[ray_kwargs['cam_idx'][..., 0]][..., :3, 2]
            if not self.render_path:
                batch['rgb'] = self.images[cam_idx, pix_y_int, pix_x_int]
            if self._load_disps:
                batch['disps'] = self.disp_images[cam_idx, pix_y_int, pix_x_int]
            if self._load_normals:
                batch['normals'] = self.normal_images[cam_idx, pix_y_int, pix_x_int]
                batch['alphas'] = self.alphas[cam_idx, pix_y_int, pix_x_int]
            if self._load_segs:
                batch['sky_segs'] = self.sky_segments[cam_idx, pix_y_int, pix_x_int]
            batch['camera_id'] = ray_kwargs['cam_idx'][..., 0]
        else:
            pixels_with_src = dict(pix_x_int=pixel_x_int_with_src, pix_y_int=pixel_y_int_with_src, **ray_kwargs)

            # Slow path, do ray computation using numpy (on CPU).
            batch = camera_utils.cast_ray_batch(self.cameras, pixels_with_src, self.camtype)
            batch['cam_dirs'] = -self.camtoworlds[ray_kwargs['cam_idx'][..., 0]][..., :3, 2]
            if not self.render_path:
                batch['rgb'] = self.images[cam_idx_with_ref, pixel_y_int_with_ref, pixel_x_int_with_ref]
            if self._load_disps:
                batch['disps'] = self.disp_images[cam_idx_with_ref, pixel_y_int_with_ref, pixel_x_int_with_ref]
            if self._load_normals:
                batch['normals'] = self.normal_images[cam_idx_with_ref, pixel_y_int_with_ref, pixel_x_int_with_ref]
                batch['alphas'] = self.alphas[cam_idx_with_ref, pixel_y_int_with_ref, pixel_x_int_with_ref]
            if self._load_segs:
                batch['sky_segs'] = self.sky_segments[cam_idx_with_ref, pixel_y_int_with_ref, pixel_x_int_with_ref]
            batch['camera_id'] = broadcast_scalar(cam_idx_with_ref)[..., 0]
            batch['cam_idx'] =  broadcast_scalar(cam_idx_with_ref)[..., 0]     

        
        return {k: torch.from_numpy(v.copy()).float() if v is not None else None for k, v in batch.items()}

    def _next_train(self, item):
        """Sample next training batch (random rays)."""
        # We assume all images in the dataset are the same resolution, so we can use
        # the same width/height for sampling all pixels coordinates in the batch.
        # Batch/patch sampling parameters.
        num_patches = self._batch_size // self._patch_size ** 2
        if self.config.virtual_poses:
            num_patches_for_virtual = int(0.2 * num_patches)  

        lower_border = self._num_border_pixels_to_mask
        upper_border = self._num_border_pixels_to_mask + self._patch_size - 1
        # Random pixel patch x-coordinates.
        pix_x_int = np.random.randint(lower_border, self.width - upper_border,
                                      (num_patches, 1, 1))
        # Random pixel patch y-coordinates.
        pix_y_int = np.random.randint(lower_border, self.height - upper_border,
                                      (num_patches, 1, 1))
        # Add patch coordinate offsets.
        # Shape will broadcast to (num_patches, _patch_size, _patch_size).
        patch_dx_int, patch_dy_int = camera_utils.pixel_coordinates(
            self._patch_size, self._patch_size)
        pix_x_int = pix_x_int + patch_dx_int
        pix_y_int = pix_y_int + patch_dy_int
        # Random camera indices.
        if self._batching == utils.BatchingMethod.ALL_IMAGES:
            cam_idx = np.random.randint(0, self._n_examples, (num_patches, 1, 1))
        else:
            cam_idx = np.random.randint(0, self._n_examples, (1,))

        if self.config.virtual_poses:
            virtual_case = 9
            while True:
                cam_idx_virtual_poses_src = np.random.randint(0, self.n_examples*virtual_case)
                cam_idx_virtual_poses_src_true = cam_idx_virtual_poses_src // virtual_case
                interval = [-2*self.cam_num, -1*self.cam_num, 0, self.cam_num, 2*self.cam_num]
                random_interval = np.random.randint(0, len(interval))
                if (cam_idx_virtual_poses_src_true + interval[random_interval] >= 0) and (cam_idx_virtual_poses_src_true + interval[random_interval] < self._n_examples):
                    cam_idx_virtual_poses_ref = cam_idx_virtual_poses_src_true + interval[random_interval]
                else:
                    cam_idx_virtual_poses_ref = cam_idx_virtual_poses_src_true
                
                if cam_idx_virtual_poses_ref >= 3 * self.cam_num:
                    virtual_pose_ref_depth = self.disp_images[cam_idx_virtual_poses_ref].squeeze()
                    ref_pose = self.camtoworlds[cam_idx_virtual_poses_ref].squeeze().copy()
                    src_pose = self.virtual_poses[cam_idx_virtual_poses_src].squeeze().copy()

                    ref_pose_opencv = ref_pose @ np.diag([1., -1., -1., 1.])
                    src_pose_opencv = src_pose @ np.diag([1., -1., -1., 1.])
                    virtual_intrinsic = np.linalg.inv(self.pixtocams[cam_idx_virtual_poses_ref].squeeze())
                    pts_in_src, mask = train_utils.img_warping(ref_pose_opencv, src_pose_opencv, virtual_pose_ref_depth, virtual_intrinsic)
                    if mask.sum() >= num_patches_for_virtual:
                        break
            
            cam_idx_virtual_poses_src += self._n_examples

            ht, wd = virtual_pose_ref_depth.shape
            y, x = torch.meshgrid(torch.arange(ht).float(), torch.arange(wd).float())
            pixel_coords = torch.concat([x[:, :, None], y[:, :, None]], axis=-1)
            valid_pixel_coords_all = pixel_coords[mask.to(torch.bool)]
            random_valid_pixel_indexes = np.random.randint(0, valid_pixel_coords_all.shape[0], (num_patches_for_virtual, ))
            valid_pixel_coords = valid_pixel_coords_all[random_valid_pixel_indexes]
            ref_valid_pixel_x = valid_pixel_coords[:, 0]
            ref_valid_pixel_y = valid_pixel_coords[:, 1]
            src_valid_pixel_x = torch.round(pts_in_src[ref_valid_pixel_y.long(), ref_valid_pixel_x.long(), 0]).numpy()[:, None, None].astype(np.int32)
            src_valid_pixel_y = torch.round(pts_in_src[ref_valid_pixel_y.long(), ref_valid_pixel_x.long(), 1]).numpy()[:, None, None].astype(np.int32)

            cam_idx_virtual_poses_src = np.array([cam_idx_virtual_poses_src]).repeat(num_patches_for_virtual)[:, None, None]
            cam_idx_virtual_poses_ref = np.array([cam_idx_virtual_poses_ref]).repeat(num_patches_for_virtual)[:, None, None]

            ref_valid_pixel_x = ref_valid_pixel_x.numpy()[:, None, None].astype(np.int32)
            ref_valid_pixel_y = ref_valid_pixel_y.numpy()[:, None, None].astype(np.int32)

            cam_idx_with_src = np.concatenate((cam_idx, cam_idx_virtual_poses_src), axis=0)
            cam_idx_with_ref = np.concatenate((cam_idx, cam_idx_virtual_poses_ref), axis=0)
            pixel_x_int_with_src = np.concatenate((pix_x_int, src_valid_pixel_x), axis=0)
            pixel_y_int_with_src = np.concatenate((pix_x_int, src_valid_pixel_y), axis=0)
            pixel_x_int_with_ref = np.concatenate((pix_x_int, ref_valid_pixel_x), axis=0)
            pixel_y_int_with_ref = np.concatenate((pix_y_int, ref_valid_pixel_y), axis=0)

        if self._apply_bayer_mask:
            # Compute the Bayer mosaic mask for each pixel in the batch.
            lossmult = raw_utils.pixels_to_bayer_mask(pix_x_int, pix_y_int)
        else:
            lossmult = None

        if self.config.virtual_poses:
            return self._make_ray_batch(pix_x_int, pix_y_int, cam_idx, 
                                        lossmult=lossmult, cam_idx_with_src=cam_idx_with_src, cam_idx_with_ref=cam_idx_with_ref,
                                        pixel_x_int_with_src=pixel_x_int_with_src, pixel_y_int_with_src=pixel_y_int_with_src,
                                        pixel_x_int_with_ref=pixel_x_int_with_ref, pixel_y_int_with_ref=pixel_y_int_with_ref)

        return self._make_ray_batch(pix_x_int, pix_y_int, cam_idx,
                                    lossmult=lossmult)

    def generate_ray_batch(self, cam_idx: int):
        """Generate ray batch for a specified camera in the dataset."""
        if self._render_spherical:
            camtoworld = self.camtoworlds[cam_idx]
            rays = camera_utils.cast_spherical_rays(
                camtoworld, self.height, self.width, self.near, self.far)
            return rays
        else:
            # Generate rays for all pixels in the image.
            pix_x_int, pix_y_int = camera_utils.pixel_coordinates(
                self.width, self.height)
            return self._make_ray_batch(pix_x_int, pix_y_int, cam_idx)

    def _next_test(self, item):
        """Sample next test batch (one full image)."""
        return self.generate_ray_batch(item), torch.tensor(item)

    def collate_fn(self, item):
        return self._next_fn(item[0])

    def __getitem__(self, item):
        return self._next_fn(item)


class WaymoV2(Dataset):
    
    def _load_renderings(self, config):
        if config.factor >= 0 and not (config.rawnerf_mode and 
                                       self.split == utils.DataSplit.TRAIN):
            image_dir_suffix = f'_{config.factor}'
            factor = config.factor
        else:
            factor = 1

        images = []
        depths = []
        poses = []
        sky_segments = []
        intrinsics = []
        self.width = 1920
        self.height = 1280
        sky_seg_idx = 10

        if config.cam_type == 1:
            sensor_type = ['cam_1']
        elif config.cam_type == 2:
            sensor_type = ['cam_2']
        elif config.cam_type == 3:
            sensor_type = ['cam_3']
        elif config.cam_type == 6:
            sensor_type = ['cam_1', 'cam_2', 'cam_3']
            self.cam_num = 3
        elif config.cam_type == 7:
            sensor_type = ['cam_1', 'cam_2', 'cam_3', 'cam_4', 'cam_5']
            self.cam_num = 5

        poses_per_camera = [[] for step in range(5)]
        intrinsics_per_camera = [[] for step in range(5)]
        cam_idx_dict = {'camera_FRONT':0, 'camera_FRONT_LEFT':1, 'camera_FRONT_RIGHT':2, 'camera_SIDE_LEFT':3, 'camera_SIDE_RIGHT': 4}

        images_root = os.path.join(self.data_dir, 'images')
        sky_segments_root = os.path.join(self.data_dir, 'masks')
        depth_root = config.depth_dir
        scene_info_path = os.path.join(self.data_dir, 'scenario.pt')
        with open(scene_info_path, 'rb') as f:
            scenario = pickle.load(f)
        for oid, odict in scenario['observers'].items():
            if (o_class_name:=odict['class_name']) == 'Camera':
                intr = odict['data']['intr']
                c2w = odict['data']['c2w']
                intrinsics_per_camera[cam_idx_dict[oid]].append(intr)
                poses_per_camera[cam_idx_dict[oid]].append(c2w)

        # temp_dir = os.path.join(self.data_dir, 'cam_1')
        # pickle_idxes = sorted(os.listdir(temp_dir))
        # pickle_idxes = pickle_idxes[:80]
        # prefix_path = 'Waymo'
        # segment_path = ''
        # depth_root = ''
        # scene = self.data_dir.split("/")[-2]
        # segment_root_path = os.path.join(segment_path, scene)
        # depth_root_path = os.path.join(depth_root, scene, 'nerf_depth')
        if config.refine_name != "":
            poses_json_path = config.refine_name
            with open(poses_json_path) as jp:
                poses_json = json.load(jp)

        virtual_poses = []
        virtual_intrinsics = []
        if self.mode == 'train':
            video_lens = 150
        else:
            video_lens = 30
        for idx in range(video_lens):
            for cam_idx, cam in enumerate(sensor_type):

                if self.mode =='train':
                    rgb_path = os.path.join(images_root, 'train_'+cam+'_', str(idx).zfill(8)+'.png')
                else:
                    rgb_path = os.path.join(images_root, 'offset_left_4m','eval_'+cam+'_', str(idx).zfill(8)+'png')

                pose_cam2world = poses_per_camera[cam_idx][0][idx]
                intrinsic = intrinsics_per_camera[cam_idx][0][idx]

                image = np.array(Image.open(rgb_path), dtype=np.float32)
                ori_image_shape = image.shape
                image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
                image = image / 255.
                images.append(image)

                intrinsic[0, :] *= self.width / ori_image_shape[1]
                intrinsic[1, :] *= self.height / ori_image_shape[0]
                intrinsics.append(intrinsic)

                if config.refine_name == "":
                    poses.append(pose_cam2world)
                else:
                #cam2world
                    # pose_cam2world = pose @ cam_ex
                    # poses.append(pose_cam2world)
                    pose_key_2 = rgb_path.split("/")[-1][:-4]
                    pose_key_1 = rgb_path.split("/")[-2]
                    pose_key = pose_key_1 + "/" + pose_key_2
                    pose_attrs = poses_json[pose_key]
                    quat = [pose_attrs['q_x'], pose_attrs['q_y'], pose_attrs['q_z'], pose_attrs['q_w']]
                    pose_world2cam = np.eye(4)
                    Rot = R.from_quat(np.array(quat)).as_matrix()
                    pose_world2cam[:3, 3] = np.array([pose_attrs['p_x'], pose_attrs['p_y'], pose_attrs['p_z']])
                    pose_world2cam[:3, :3] = Rot
                    pose_cam2world = np.linalg.inv(pose_world2cam)
                    poses.append(pose_cam2world)
        poses = np.array(poses)
        images = np.array(images)
        intrinsics = np.array(intrinsics)
  

        if config.virtual_poses:
            virtual_poses = np.array(virtual_poses)
            virtual_intrinsics = np.array(virtual_intrinsics)

        center = np.mean(poses[:, :3, 3], axis=0)
        poses[:, :3, 3] -= center[None]
        scale = 1.0 / np.mean(np.linalg.norm(poses[:, :3, 3], axis=-1), axis=0)
        poses[:, :3, 3] = poses[:, :3, 3] * scale
        depths = depths * scale

        if config.virtual_poses:
            virtual_poses[:, :3, 3] -= center[None]
            virtual_poses[:, :3, 3] = virtual_poses[:, :3, 3] * scale

        all_indices = np.arange(len(images))
        train_indices = all_indices % (8 * len(sensor_type)) >= len(sensor_type)
        split_indices = {
            utils.DataSplit.TEST: all_indices[all_indices % (8 * len(sensor_type)) < len(sensor_type)],
            utils.DataSplit.TRAIN: train_indices
        }
        indices = split_indices[self.split]
        virtual_indices = np.arange(len(images)*9) % (8 * len(sensor_type) * 9) >= len(sensor_type) * 9

        self.images = images[indices]
        self.n_examples = len(self.images)

        intrinsics = intrinsics[indices]
        self.pixtocams = np.array([np.linalg.inv(intrinsic) for intrinsic in intrinsics])

        poses = poses @ np.diag([1., -1., -1., 1.]).astype(np.float32)
        if config.virtual_poses:
            virtual_intrinsics = virtual_intrinsics[virtual_indices]
            pixtocams_vir = np.array([np.linalg.inv(virtual_intrinsic) for virtual_intrinsic in virtual_intrinsics])
            virtual_poses = virtual_poses @ np.diag([1., -1., -1., 1.]).astype(np.float32)

        self.disp_images = depths[indices]
        self.camtoworlds = poses[indices]
        self.sky_segments = sky_segments[indices]

        if self.split == utils.DataSplit.TRAIN and config.virtual_poses:
            self.camtoworlds = np.concatenate((self.camtoworlds, virtual_poses[virtual_indices]), axis=0)
            self.pixtocams = np.concatenate((self.pixtocams, pixtocams_vir), axis=0)
            self.virtual_poses = virtual_poses[virtual_indices]

        if config.render_path:
            self.width = 5760
            self.height = 1000
            poses = poses[::len(sensor_type)]
            self.focal = intrinsics[0][0, 0]
            self.camtoworlds = poses
            #self.camtype = 'panoroma'


class Carlc(Dataset):
    
    def _load_renderings(self, config):
        if config.factor >= 0 and not (config.rawnerf_mode and 
                                       self.split == utils.DataSplit.TRAIN):
            image_dir_suffix = f'_{config.factor}'
            factor = config.factor
        else:
            factor = 1

        images = []
        depths = []
        poses = []
        sky_segments = []
        intrinsics = []
        self.width = 1920
        self.height = 1280
        sky_seg_idx = 10

        if config.cam_type == 1:
            sensor_type = ['camera0']
        elif config.cam_type == 2:
            sensor_type = ['camera1']
        elif config.cam_type == 3:
            sensor_type = ['camera2']
        elif config.cam_type == 6:
            sensor_type = ['camera0', 'camera1', 'camera2']
            self.cam_num = 3
        elif config.cam_type == 7:
            sensor_type = ['camera0', 'camera1', 'camera2', 'camera3', 'camera4']
            self.cam_num = 5

        poses_per_camera = [[] for step in range(5)]
        intrinsics_per_camera = [[] for step in range(5)]
        cam_idx_dict = {'camera0':0, 'camera1':1, 'camera2':2, 'camera3':3, 'camera4': 4}

        images_root = os.path.join(self.data_dir)
        if self.mode == 'train':
            images_root = os.path.join(self.data_dir,'train_pic')
            pose_ego = self.load_blender_json(images_root)
        else:
            images_root_train = os.path.join(self.data_dir,'train_pic')
            images_root = os.path.join(self.data_dir,'test_pic')
            pose_ego_train = self.load_blender_json(images_root_train)
        # self.load_json = os.path.join(self.data_dir , "transforms_ucnerf.json")
            pose_ego = self.load_blender_json(images_root,'test')
        c2w_train_list = []
        if self.mode == 'test' and config.cam_type != 1:
            sensor_type = ['camera0']
        for idx,oid in enumerate(sensor_type):
            extrinsic_root = os.path.join(self.data_dir,'extrinsics',str(idx)+'.txt')
            intrinsic_root = os.path.join(self.data_dir,'intrinsics',str(idx)+'.txt')
            extrinsics = np.loadtxt(extrinsic_root)
            extrinsics_tensor = torch.tensor(extrinsics)
            intrinsic = np.loadtxt(intrinsic_root)
            fx, fy, cx, cy = intrinsic[0], intrinsic[1], intrinsic[2], intrinsic[3]
            intrinsic = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
            intrinsics_tensor = torch.tensor(intrinsic)
            intr = intrinsics_tensor
            c2w = pose_ego@extrinsics_tensor
            c2w = c2w.numpy()
            for i in range(c2w.shape[0]):
                c2w[i] = self.pose_unreal2opencv(c2w[i])
            c2w = torch.tensor(c2w)
            if self.mode == 'test':
                c2w_train = pose_ego_train@extrinsics_tensor
                c2w_train = c2w_train.numpy()
                for i in range(c2w_train.shape[0]):
                    c2w_train[i] = self.pose_unreal2opencv(c2w_train[i])
                c2w_train_list.append(c2w_train)



            intrinsics_per_camera[cam_idx_dict[oid]].append(intr)
            poses_per_camera[cam_idx_dict[oid]].append(c2w)

        # temp_dir = os.path.join(self.data_dir, 'cam_1')
        # pickle_idxes = sorted(os.listdir(temp_dir))
        # pickle_idxes = pickle_idxes[:80]
        # prefix_path = 'Waymo'
        # segment_path = ''
        # depth_root = ''
        # scene = self.data_dir.split("/")[-2]
        # segment_root_path = os.path.join(segment_path, scene)
        # depth_root_path = os.path.join(depth_root, scene, 'nerf_depth')

        virtual_poses = []
        virtual_intrinsics = []
        if self.mode == 'train':
            video_lens = 150
        else:
            video_lens = 30
        for idx in range(video_lens):
            for cam_idx, cam in enumerate(sensor_type):
                if self.mode == 'train':
                    rgb_path = os.path.join(images_root, 'train_'+cam +'_'+str(idx).zfill(5)+'.png')
                    # sky_segments_root = os.path.join(images_root,'masks')
                    # segment_path = os.path.join(sky_segments_root,'train_'+cam +'_'+str(idx).zfill(5)+'.npz')
                else:
                    rgb_path = os.path.join(images_root,'offset_left_4m','eval_'+'camera0' +'_'+str(idx).zfill(5)+'.png')
                    # sky_segments_root = os.path.join(images_root,'masks')
                    # segment_path = os.path.join(images_root,'offset_left_1m','masks','eval_'+'camera0' +'_'+str(idx).zfill(5)+'.npz')
                pose_cam2world = poses_per_camera[cam_idx][0][idx]
                intrinsic = intrinsics_per_camera[cam_idx][0]

                image = np.array(Image.open(rgb_path))
                ori_image_shape = image.shape
                image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
                image = image / 255.
                images.append(image)

                intrinsic[0, :] *= self.width / ori_image_shape[1]
                intrinsic[1, :] *= self.height / ori_image_shape[0]
                intrinsics.append(intrinsic)
                poses.append(pose_cam2world)


                #segmentation
                #segment = np.array(Image.open(segment_path))
                # segment = np.load(segment_path)['arr_0'].astype(np.float32).squeeze()
                # sky_mask = segment == sky_seg_idx
                # segment[sky_mask] = 1
                # segment[~sky_mask] = 0
                
                # sky_segments.append(segment)
                
        poses = np.array(poses)
        images = np.array(images)
        intrinsics = np.array(intrinsics)
        # sky_segments = np.array(sky_segments)
        if config.virtual_poses:
            virtual_poses = np.array(virtual_poses)
            virtual_intrinsics = np.array(virtual_intrinsics)
        if self.mode == 'train':
            center = np.mean(poses[:, :3, 3], axis=0)
            poses[:, :3, 3] -= center[None]
            scale = 1.0 / np.mean(np.linalg.norm(poses[:, :3, 3], axis=-1), axis=0)
            poses[:, :3, 3] = poses[:, :3, 3] * scale
        else:
            center = np.mean(np.concatenate(c2w_train_list,axis=0)[:, :3, 3],axis=0)
            poses[:, :3, 3] -= center[None]
            poses_train_trans = np.concatenate(c2w_train_list,axis=0)[:, :3, 3] - center
            scale = 1.0 / np.mean(np.linalg.norm(poses_train_trans, axis=-1), axis=0)
            poses[:, :3, 3] = poses[:, :3, 3] * scale

        if config.virtual_poses:
            virtual_poses[:, :3, 3] -= center[None]
            virtual_poses[:, :3, 3] = virtual_poses[:, :3, 3] * scale

        all_indices = np.arange(len(images))

        virtual_indices = np.arange(len(images)*9) % (8 * len(sensor_type) * 9) >= len(sensor_type) * 9

        self.images = images
        self.n_examples = len(self.images)
        

        self.pixtocams = np.array([np.linalg.inv(intrinsic) for intrinsic in intrinsics])

        poses = poses @ np.diag([1., -1., -1., 1.]).astype(np.float32)
        if config.virtual_poses:
            virtual_intrinsics = virtual_intrinsics[virtual_indices]
            pixtocams_vir = np.array([np.linalg.inv(virtual_intrinsic) for virtual_intrinsic in virtual_intrinsics])
            virtual_poses = virtual_poses @ np.diag([1., -1., -1., 1.]).astype(np.float32)

  
        self.camtoworlds = poses

        # self.sky_segments = sky_segments
        if self.mode == 'train' and config.virtual_poses:
            self.camtoworlds = np.concatenate((self.camtoworlds, virtual_poses[virtual_indices]), axis=0)
            self.pixtocams = np.concatenate((self.pixtocams, pixtocams_vir), axis=0)
            self.virtual_poses = virtual_poses[virtual_indices]

        if config.render_path:
            self.width = 5760
            self.height = 1000
            poses = poses[::len(sensor_type)]
            self.focal = intrinsics[0][0, 0]
            self.camtoworlds = poses
            #self.camtype = 'panoroma'
    def pose_unreal2opencv(self,c2w_mat):

        translation = c2w_mat[:3, 3]
        rot = R.from_matrix(c2w_mat[:3, :3])
        rot_vec = rot.as_rotvec()

        rot_vec_new = rot_vec[[1, 2, 0]]
        rot_vec_new[0] *= -1
        rot_vec_new[2] *= -1

        rot = R.from_rotvec(rot_vec_new)

        
        translation_new = translation[[1, 2, 0]]
        translation_new[1] *= -1

        c2w_mat = np.eye(4)
        c2w_mat[:3, :3] = rot.as_matrix()
        c2w_mat[:3, 3] = translation_new

        return c2w_mat
    def load_blender_json(self,root_path,  mode="train"):

        pose_list = []
        if mode == 'train':
            video_len = 150
        else:
            video_len = 30
        for idx in range(video_len):
            if mode == 'train':
                json_path = os.path.join(root_path, 'train_camera_extrinsics_'+str(idx).zfill(6)+'.json')
            else:
                json_path = os.path.join(root_path,'offset_left_4m','eval_camera_extrinsics_'+str(idx).zfill(6)+'.json')
            with open(json_path,'r') as f:
                json_file = json.load(f)
            data = json_file
            # img_path = os.path.join(root_path, data["file_path"] + ".png")
            pose = np.array(data['transform_matrix'])
            pose = torch.tensor(pose)
            # pose = self.blender_pose_transform(pose) # [3, 4]
            pose_list += [pose]
            # path_list += [img_path]
        pose_list = torch.stack(pose_list, 0)
       

        return pose_list














































if __name__ == '__main__':
    from internal import configs
    import accelerate

    config = configs.Config()
    accelerator = accelerate.Accelerator()
    config.world_size = accelerator.num_processes
    config.global_rank = accelerator.process_index
    config.factor = 8
    # dataset = LLFF('test', '/SSD_DISK/datasets/360_v2/bicycle', config)
    # print(len(dataset))
    # for _ in tqdm(dataset):
    #     pass
    # print('done')
    # print(accelerator.process_index)
