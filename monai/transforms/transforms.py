# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
A collection of "vanilla" transforms
https://github.com/Project-MONAI/MONAI/wiki/MONAI_Design
"""

import numpy as np
import torch
from scipy.ndimage.filters import gaussian_filter

import monai
from monai.data.utils import get_random_patch, get_valid_patch_size
from monai.transforms.compose import Randomizable
from monai.transforms.utils import (create_control_grid, create_grid, create_rotate, create_scale, create_shear,
                                    create_translate, rescale_array)
from monai.utils.misc import ensure_tuple

export = monai.utils.export("monai.transforms")


@export
class AddChannel:
    """
    Adds a 1-length channel dimension to the input image.
    """

    def __call__(self, img):
        return img[None]


@export
class Transpose:
    """
    Transposes the input image based on the given `indices` dimension ordering.
    """

    def __init__(self, indices):
        self.indices = indices

    def __call__(self, img):
        return img.transpose(self.indices)


@export
class Rescale:
    """
    Rescales the input image to the given value range.
    """

    def __init__(self, minv=0.0, maxv=1.0, dtype=np.float32):
        self.minv = minv
        self.maxv = maxv
        self.dtype = dtype

    def __call__(self, img):
        return rescale_array(img, self.minv, self.maxv, self.dtype)


@export
class Flip:
    """Reverses the order of elements along the given axis. Preserves shape.
    Uses np.flip in practice. See numpy.flip for additional details.

    Args:
        axes (None, int or tuple of ints): Axes along which to flip over. Default is None.
    """

    def __init__(self, axis=None):
        assert axis is None or isinstance(axis, (int, list, tuple)), \
            "axis must be None, int or tuple of ints."
        self.axis = axis

    def __call__(self, img):
        return np.flip(img, self.axis)


@export
class ToTensor:
    """
    Converts the input image to a tensor without applying any other transformations.
    """

    def __call__(self, img):
        return torch.from_numpy(img)


@export
class UniformRandomPatch(Randomizable):
    """
    Selects a patch of the given size chosen at a uniformly random position in the image.
    """

    def __init__(self, patch_size):
        self.patch_size = (None,) + tuple(patch_size)

        self._slices = None

    def randomize(self, image_shape, patch_shape):
        self._slices = get_random_patch(image_shape, patch_shape, self.R)

    def __call__(self, img):
        patch_size = get_valid_patch_size(img.shape, self.patch_size)
        self.randomize(img.shape, patch_size)
        return img[self._slices]


@export
class IntensityNormalizer:
    """Normalize input based on provided args, using calculated mean and std if not provided
    (shape of subtrahend and divisor must match. if 0, entire volume uses same subtrahend and
     divisor, otherwise the shape can have dimension 1 for channels).
     Current implementation can only support 'channel_last' format data.

    Args:
        subtrahend (ndarray): the amount to subtract by (usually the mean)
        divisor (ndarray): the amount to divide by (usually the standard deviation)
        dtype: output data format
    """

    def __init__(self, subtrahend=None, divisor=None, dtype=np.float32):
        if subtrahend is not None or divisor is not None:
            assert isinstance(subtrahend, np.ndarray) and isinstance(divisor, np.ndarray), \
                'subtrahend and divisor must be set in pair and in numpy array.'
        self.subtrahend = subtrahend
        self.divisor = divisor
        self.dtype = dtype

    def __call__(self, img):
        if self.subtrahend is not None and self.divisor is not None:
            img -= self.subtrahend
            img /= self.divisor
        else:
            img -= np.mean(img)
            img /= np.std(img)

        if self.dtype != img.dtype:
            img = img.astype(self.dtype)
        return img


@export
class ImageEndPadder:
    """Performs padding by appending to the end of the data all on one side for each dimension.
     Uses np.pad so in practice, a mode needs to be provided. See numpy.lib.arraypad.pad
     for additional details.

    Args:
        out_size (list): the size of region of interest at the end of the operation.
        mode (string): a portion from numpy.lib.arraypad.pad is copied below.
        dtype: output data format.
    """

    def __init__(self, out_size, mode, dtype=np.float32):
        assert out_size is not None and isinstance(out_size, (list, tuple)), 'out_size must be list or tuple'
        self.out_size = out_size
        assert isinstance(mode, str), 'mode must be str'
        self.mode = mode
        self.dtype = dtype

    def _determine_data_pad_width(self, data_shape):
        return [(0, max(self.out_size[i] - data_shape[i], 0)) for i in range(len(self.out_size))]

    def __call__(self, img):
        data_pad_width = self._determine_data_pad_width(img.shape[2:])
        all_pad_width = [(0, 0), (0, 0)] + data_pad_width
        img = np.pad(img, all_pad_width, self.mode)
        return img


@export
class Rotate90:
    """
    Rotate an array by 90 degrees in the plane specified by `axes`.
    """

    def __init__(self, k=1, axes=(1, 2)):
        """
        Args:
            k (int): number of times to rotate by 90 degrees.
            axes (2 ints): defines the plane to rotate with 2 axes.
        """
        self.k = k
        self.plane_axes = axes

    def __call__(self, img):
        return np.rot90(img, self.k, self.plane_axes)


@export
class RandRotate90(Randomizable):
    """
    With probability `prob`, input arrays are rotated by 90 degrees
    in the plane specified by `axes`.
    """

    def __init__(self, prob=0.1, max_k=3, axes=(1, 2)):
        """
        Args:
            prob (float): probability of rotating.
                (Default 0.1, with 10% probability it returns a rotated array)
            max_k (int): number of rotations will be sampled from `np.random.randint(max_k) + 1`.
                (Default 3)
            axes (2 ints): defines the plane to rotate with 2 axes.
                (Default (1, 2))
        """
        self.prob = min(max(prob, 0.0), 1.0)
        self.max_k = max_k
        self.axes = axes

        self._do_transform = False
        self._rand_k = 0

    def randomize(self):
        self._rand_k = self.R.randint(self.max_k) + 1
        self._do_transform = self.R.random() < self.prob

    def __call__(self, img):
        self.randomize()
        if not self._do_transform:
            return img
        rotator = Rotate90(self._rand_k, self.axes)
        return rotator(img)


@export
class SpatialCrop:
    """General purpose cropper to produce sub-volume region of interest (ROI).
    It can support to crop 1, 2 or 3 dimensions spatial data.
    Either a center and size must be provided, or alternatively if center and size
    are not provided, the start and end coordinates of the ROI must be provided.
    The sub-volume must sit the within original image.

    Note: This transform will not work if the crop region is larger than the image itself.
    """

    def __init__(self, roi_center=None, roi_size=None, roi_start=None, roi_end=None):
        """
        Args:
            roi_center (list or tuple): voxel coordinates for center of the crop ROI.
            roi_size (list or tuple): size of the crop ROI.
            roi_start (list or tuple): voxel coordinates for start of the crop ROI.
            roi_end (list or tuple): voxel coordinates for end of the crop ROI.
        """
        if roi_center is not None and roi_size is not None:
            assert isinstance(roi_center, (list, tuple)), 'roi_center must be list or tuple.'
            assert isinstance(roi_size, (list, tuple)), 'roi_size must be list or tuple.'
            assert all(x > 0 for x in roi_center), 'all elements of roi_center must be positive.'
            assert all(x > 0 for x in roi_size), 'all elements of roi_size must be positive.'
            roi_center = np.asarray(roi_center, dtype=np.uint16)
            roi_size = np.asarray(roi_size, dtype=np.uint16)
            self.roi_start = np.subtract(roi_center, np.floor_divide(roi_size, 2))
            self.roi_end = np.add(self.roi_start, roi_size)
        else:
            assert roi_start is not None and roi_end is not None, 'roi_start and roi_end must be provided.'
            assert isinstance(roi_start, (list, tuple)), 'roi_start must be list or tuple.'
            assert isinstance(roi_end, (list, tuple)), 'roi_end must be list or tuple.'
            assert all(x >= 0 for x in roi_start), 'all elements of roi_start must be greater than or equal to 0.'
            assert all(x > 0 for x in roi_end), 'all elements of roi_end must be positive.'
            self.roi_start = roi_start
            self.roi_end = roi_end

    def __call__(self, img):
        max_end = img.shape[1:]
        assert (np.subtract(max_end, self.roi_start) >= 0).all(), 'roi start out of image space.'
        assert (np.subtract(max_end, self.roi_end) >= 0).all(), 'roi end out of image space.'
        assert (np.subtract(self.roi_end, self.roi_start) >= 0).all(), 'invalid roi range.'
        if len(self.roi_start) == 1:
            data = img[:, self.roi_start[0]:self.roi_end[0]].copy()
        elif len(self.roi_start) == 2:
            data = img[:, self.roi_start[0]:self.roi_end[0], self.roi_start[1]:self.roi_end[1]].copy()
        elif len(self.roi_start) == 3:
            data = img[:, self.roi_start[0]:self.roi_end[0], self.roi_start[1]:self.roi_end[1],
                       self.roi_start[2]:self.roi_end[2]].copy()
        else:
            raise ValueError('unsupported image shape.')
        return data


class AffineGrid:
    """
    Affine transforms on the coordinates.
    """

    def __init__(self,
                 rotate_params=None,
                 shear_params=None,
                 translate_params=None,
                 scale_params=None,
                 as_tensor_output=True,
                 device=None):
        self.rotate_params = rotate_params
        self.shear_params = shear_params
        self.translate_params = translate_params
        self.scale_params = scale_params

        self.as_tensor_output = as_tensor_output
        self.device = device

    def __call__(self, spatial_size=None, grid=None):
        """
        Args:
            spatial_size (list or tuple of int): output grid size.
            grid (ndarray): grid to be transformed. Shape must be (3, H, W) for 2D or (4, H, W, D) for 3D.
        """
        if grid is None:
            if spatial_size is not None:
                grid = create_grid(spatial_size)
            else:
                raise ValueError('Either specify a grid or a spatial size to create a grid from.')

        spatial_dims = len(grid.shape) - 1
        affine = np.eye(spatial_dims + 1)
        if self.rotate_params:
            affine = affine @ create_rotate(spatial_dims, self.rotate_params)
        if self.shear_params:
            affine = affine @ create_shear(spatial_dims, self.shear_params)
        if self.translate_params:
            affine = affine @ create_translate(spatial_dims, self.translate_params)
        if self.scale_params:
            affine = affine @ create_scale(spatial_dims, self.scale_params)
        affine = torch.tensor(affine, device=self.device)

        if not torch.is_tensor(grid):
            grid = torch.tensor(grid)
        if self.device:
            grid = grid.to(self.device)
        grid = (affine.float() @ grid.reshape((grid.shape[0], -1)).float()).reshape([-1] + list(grid.shape[1:]))
        if self.as_tensor_output:
            return grid
        return grid.cpu().numpy()


class RandAffineGrid(Randomizable):
    """
    generate randomised affine grid
    """

    def __init__(self,
                 rotate_range=None,
                 shear_range=None,
                 translate_range=None,
                 scale_range=None,
                 as_tensor_output=True,
                 device=None):
        """
        Args:
            rotate_range (a sequence of positive floats): rotate_range[0] with be used to generate the 1st rotation
                parameter from `uniform[-rotate_range[0], rotate_range[0])`. Similarly, `rotate_range[2]` and
                `rotate_range[3]` are used in 3D affine for the range of 2nd and 3rd axes.
            shear_range (a sequence of positive floats): shear_range[0] with be used to generate the 1st shearing
                parameter from `uniform[-shear_range[0], shear_range[0])`. Similarly, `shear_range[1]` to
                `shear_range[N]` controls the range of the uniform distribution used to generate the 2nd to
                N-th parameter.
            translate_range (a sequence of positive floats): translate_range[0] with be used to generate the 1st
                shift parameter from `uniform[-translate_range[0], translate_range[0])`. Similarly, `translate_range[1]`
                to `translate_range[N]` controls the range of the uniform distribution used to generate
                the 2nd to N-th parameter.
            scale_range (a sequence of positive floats): scaling_range[0] with be used to generate the 1st scaling
                factor from `uniform[-scale_range[0], scale_range[0]) + 1.0`. Similarly, `scale_range[1]` to
                `scale_range[N]` controls the range of the uniform distribution used to generate the 2nd to
                N-th parameter.

        See also:
            `from monai.transforms.utils import (create_rotate, create_shear, create_translate, create_scale)`
        """
        self.rotate_range = ensure_tuple(rotate_range)
        self.shear_range = ensure_tuple(shear_range)
        self.translate_range = ensure_tuple(translate_range)
        self.scale_range = ensure_tuple(scale_range)

        self.rotate_params = None
        self.shear_params = None
        self.translate_params = None
        self.scale_params = None

        self.as_tensor_output = as_tensor_output
        self.device = device

    def randomize(self):
        if self.rotate_range:
            self.rotate_params = [self.R.uniform(-f, f) for f in self.rotate_range if f is not None]
        if self.shear_range:
            self.shear_params = [self.R.uniform(-f, f) for f in self.shear_range if f is not None]
        if self.translate_range:
            self.translate_params = [self.R.uniform(-f, f) for f in self.translate_range if f is not None]
        if self.scale_range:
            self.scale_params = [self.R.uniform(-f, f) + 1.0 for f in self.scale_range if f is not None]

    def __call__(self, spatial_size=None, grid=None):
        """
        Returns:
            a 2D (3xHxW) or 3D (4xHxWxD) grid.
        """
        self.randomize()
        _affine_grid = AffineGrid(self.rotate_params, self.shear_params, self.translate_params, self.scale_params,
                                  self.as_tensor_output, self.device)
        return _affine_grid(spatial_size, grid)


class RandDeformGrid(Randomizable):
    """
    generate random deformation grid
    """

    def __init__(self, spacing, magnitude_range, as_tensor_output=True, device=None):
        """
        Args:
            spacing (2 or 3 ints): spacing of the grid in 2D or 3D.
                e.g., spacing=(1, 1) indicates pixel-wise deformation in 2D,
                      spacing=(1, 1, 1) indicates pixel-wise deformation in 2D,
                      spacing=(2, 2) indicates deformation field defined on every other pixel in 2D.
            magnitude_range (2 ints): the random offsets will be generated from
                `uniform[magnitude[0], magnitude[1])`.
            as_tensor_output (bool): whether to output tensor instead of numpy array.
                defaults to True.
            device (torch device): device to store the output grid data.
        """
        self.spacing = spacing
        self.magnitude = magnitude_range

        self.rand_mag = 1.0
        self.as_tensor_output = as_tensor_output
        self.random_offset = 0.0
        self.device = device

    def randomize(self, grid_size):
        self.random_offset = self.R.normal(size=([len(grid_size)] + list(grid_size)))
        self.rand_mag = self.R.uniform(self.magnitude[0], self.magnitude[1])

    def __call__(self, spatial_size):
        control_grid = create_control_grid(spatial_size, self.spacing)
        self.randomize(control_grid.shape[1:])
        control_grid[:len(spatial_size)] += self.rand_mag * self.random_offset
        if self.as_tensor_output:
            control_grid = torch.tensor(control_grid, device=self.device)
        return control_grid


class Resample:

    def __init__(self, padding_mode='zeros', as_tensor_output=False, device=None):
        """
        computes output image using values from `img`, locations from `grid` using pytorch.
        supports spatially 2D or 3D (num_channels, H, W[, D]).

        Args:
            padding_mode ('zeros'|'border'|'reflection'): mode of handling out of range indices. Defaults to 'zeros'.
            as_tensor_output(bool): whether to return a torch tensor. Defaults to False.
            device (string):
        """
        self.padding_mode = padding_mode
        self.as_tensor_output = as_tensor_output
        self.device = device

    def __call__(self, img, grid, mode='bilinear'):
        """
        Args:
            img (ndarray or tensor): shape must be (num_channels, H, W[, D]).
            grid (ndarray or tensor): shape must be (3, H, W) for 2D or (4, H, W, D) for 3D.
            mode ('nearest'|'bilinear'): interpolation order. Defaults to 'bilinear'.
        """
        if not torch.is_tensor(img):
            img = torch.tensor(img)
        if not torch.is_tensor(grid):
            grid = torch.tensor(grid)
        if self.device:
            img = img.to(self.device)
            grid = grid.to(self.device)

        for i, dim in enumerate(img.shape[1:]):
            grid[i] = 2. * grid[i] / (dim - 1.)
        grid = grid[:-1] / grid[-1:]
        grid = grid[range(img.ndim - 2, -1, -1)]
        grid = grid.permute(list(range(grid.ndim))[1:] + [0])
        out = torch.nn.functional.grid_sample(img[None].float(),
                                              grid[None].float(),
                                              mode=mode,
                                              padding_mode=self.padding_mode,
                                              align_corners=False)[0]
        if not self.as_tensor_output:
            return out.cpu().numpy()
        return out


@export
class Affine:
    """
    transform ``img`` given the affine parameters.
    """

    def __init__(self,
                 rotate_params=None,
                 shear_params=None,
                 translate_params=None,
                 scale_params=None,
                 padding_mode='zeros',
                 as_tensor_output=False,
                 device=None):
        """
        The affines are applied in rotate, shear, translate, scale order.

        Args:
            rotate_params (float, list of floats): a rotation angle in radians,
                a scalar for 2D image, a tuple of 2 floats for 3D. Defaults to no rotation.
            shear_params (list of floats):
                a tuple of 2 floats for 2D, a tuple of 6 floats for 3D. Defaults to no shearing.
            translate_params (list of floats):
                a tuple of 2 floats for 2D, a tuple of 3 floats for 3D. Translation is in pixel/voxel
                relative to the center of the input image. Defaults to no translation.
            scale_params (list of floats):
                a tuple of 2 floats for 2D, a tuple of 3 floats for 3D. Defaults to no scaling.
            padding_mode ('zeros'|'border'|'reflection'): mode of handling out of range indices. Defaults to 'zeros'.
            as_tensor_output (bool): the computation is implemented using pytorch tensors, this option specifies
                whether to convert it back to numpy arrays.
            device (torch.device): device on which the tensor will be allocated.
        """
        self.affine_grid = AffineGrid(rotate_params,
                                      shear_params,
                                      translate_params,
                                      scale_params,
                                      as_tensor_output=True,
                                      device=device)
        self.resampler = Resample(padding_mode, as_tensor_output=as_tensor_output, device=device)

    def __call__(self, img, spatial_size, mode='bilinear'):
        """
        Args:
            img (ndarray or tensor): shape must be (num_channels, H, W[, D]),
            spatial_size (list or tuple of int): output image spatial size.
                if `img` has two spatial dimensions, `spatial_size` should have 2 elements [h, w].
                if `img` has three spatial dimensions, `spatial_size` should have 3 elements [h, w, d].
            mode ('nearest'|'bilinear'): interpolation order. Defaults to 'bilinear'.
        """
        grid = self.affine_grid(spatial_size)
        return self.resampler(img, grid, mode)


@export
class RandAffine(Randomizable):
    """
    Random affine transform.
    """

    def __init__(self,
                 prob=0.1,
                 rotate_range=None,
                 shear_range=None,
                 translate_range=None,
                 scale_range=None,
                 padding_mode='zeros',
                 as_tensor_output=True,
                 device=None):
        """
        Args:
            prob (float): probability of returning a randomized affine grid.
                defaults to 0.1, with 10% chance returns a randomized grid.
            padding_mode ('zeros'|'border'|'reflection'): mode of handling out of range indices. Defaults to 'zeros'.
            as_tensor_output (bool): the computation is implemented using pytorch tensors, this option specifies
                whether to convert it back to numpy arrays.
            device (torch.device): device on which the tensor will be allocated.

        See also:
            RandAffineGrid for the random affine paramters configurations.
            Affine for the affine transformation parameters configurations.
        """

        self.rand_affine_grid = RandAffineGrid(rotate_range, shear_range, translate_range, scale_range, True, device)
        self.resampler = Resample(padding_mode=padding_mode, as_tensor_output=as_tensor_output, device=device)

        self.do_transform = False
        self.prob = prob

    def set_random_state(self, seed=None, state=None):
        self.rand_affine_grid.set_random_state(seed, state)
        Randomizable.set_random_state(self, seed, state)
        return self

    def randomize(self):
        self.do_transform = self.R.rand() < self.prob

    def __call__(self, img, spatial_size, mode='bilinear'):
        """
        Args:
            img (ndarray or tensor): shape must be (num_channels, H, W[, D]),
            spatial_size (list or tuple of int): output image spatial size.
                if `img` has two spatial dimensions, `spatial_size` should have 2 elements [h, w].
                if `img` has three spatial dimensions, `spatial_size` should have 3 elements [h, w, d].
            mode ('nearest'|'bilinear'): interpolation order. Defaults to 'bilinear'.
        """
        self.randomize()
        if self.do_transform:
            grid = self.rand_affine_grid(spatial_size=spatial_size)
        else:
            grid = create_grid(spatial_size)
        return self.resampler(img, grid, mode)


@export
class Rand2DElastic(Randomizable):
    """
    Random elastic deformation and affine in 2D
    """

    def __init__(self,
                 spacing,
                 magnitude_range,
                 prob=0.1,
                 rotate_range=None,
                 shear_range=None,
                 translate_range=None,
                 scale_range=None,
                 padding_mode='zeros',
                 as_tensor_output=False,
                 device=None):
        """
        Args:
            spacing (2 ints): distance in between the control points.
            magnitude_range (2 ints): the random offsets will be generated from
                `uniform[magnitude[0], magnitude[1])`.
            prob (float): probability of returning a randomized affine grid.
                defaults to 0.1, with 10% chance returns a randomized grid,
                otherwise returns a `spatial_size` centered area centered extracted from the input image.
            padding_mode ('zeros'|'border'|'reflection'): mode of handling out of range indices. Defaults to 'zeros'.
            as_tensor_output (bool): the computation is implemented using pytorch tensors, this option specifies
                whether to convert it back to numpy arrays.
            device (torch.device): device on which the tensor will be allocated.

        See also:
            RandAffineGrid for the random affine paramters configurations.
            Affine for the affine transformation parameters configurations.
        """
        self.deform_grid = RandDeformGrid(spacing, magnitude_range, as_tensor_output=True, device=device)
        self.rand_affine_grid = RandAffineGrid(rotate_range, shear_range, translate_range, scale_range, True, device)
        self.resampler = Resample(padding_mode=padding_mode, as_tensor_output=as_tensor_output, device=device)

        self.prob = prob
        self.do_transform = False

    def set_random_state(self, seed=None, state=None):
        self.deform_grid.set_random_state(seed, state)
        self.rand_affine_grid.set_random_state(seed, state)
        Randomizable.set_random_state(self, seed, state)
        return self

    def randomize(self):
        self.do_transform = self.R.rand() < self.prob

    def __call__(self, img, spatial_size, mode='bilinear'):
        """
        Args:
            img (ndarray or tensor): shape must be (num_channels, H, W),
            spatial_size (2 ints): specifying output image spatial size [h, w].
            mode ('nearest'|'bilinear'): interpolation order. Defaults to 'bilinear'.
        """
        self.randomize()
        if self.do_transform:
            grid = self.deform_grid(spatial_size)
            grid = self.rand_affine_grid(grid=grid)
            grid = torch.nn.functional.interpolate(grid[None], spatial_size, mode='bicubic', align_corners=False)[0]
        else:
            grid = create_grid(spatial_size)
        return self.resampler(img, grid, mode)


@export
class Rand3DElastic(Randomizable):
    """
    Random elastic deformation and affine in 3D
    """

    def __init__(self,
                 sigma_range,
                 magnitude_range,
                 prob=0.1,
                 rotate_range=None,
                 shear_range=None,
                 translate_range=None,
                 scale_range=None,
                 padding_mode='zeros',
                 as_tensor_output=False,
                 device=None):
        """
        Args:
            sigma_range (2 ints): a Gaussian kernel with standard deviation sampled
                 from `uniform[sigma_range[0], sigma_range[1])` will be used to smooth the random offset grid.
            magnitude_range (2 ints): the random offsets on the grid will be generated from
                `uniform[magnitude[0], magnitude[1])`.
            prob (float): probability of returning a randomized affine grid.
                defaults to 0.1, with 10% chance returns a randomized grid,
                otherwise returns a `spatial_size` centered area centered extracted from the input image.
            padding_mode ('zeros'|'border'|'reflection'): mode of handling out of range indices. Defaults to 'zeros'.
            as_tensor_output (bool): the computation is implemented using pytorch tensors, this option specifies
                whether to convert it back to numpy arrays.
            device (torch.device): device on which the tensor will be allocated.

        See also:
            RandAffineGrid for the random affine paramters configurations.
            Affine for the affine transformation parameters configurations.
        """
        self.rand_affine_grid = RandAffineGrid(rotate_range, shear_range, translate_range, scale_range, True, device)
        self.resampler = Resample(padding_mode=padding_mode, as_tensor_output=as_tensor_output, device=device)

        self.sigma_range = sigma_range
        self.magnitude_range = magnitude_range

        self.prob = prob
        self.do_transform = False
        self.rand_offset = None
        self.magnitude = 1.0
        self.sigma = 1.0

    def set_random_state(self, seed=None, state=None):
        self.rand_affine_grid.set_random_state(seed, state)
        Randomizable.set_random_state(self, seed, state)
        return self

    def randomize(self, grid_size):
        self.do_transform = self.R.rand() < self.prob
        if self.do_transform:
            self.rand_offset = self.R.uniform(-1., 1., [3] + list(grid_size))
        self.magnitude = self.R.uniform(self.magnitude_range[0], self.magnitude_range[1])
        self.sigma = self.R.uniform(self.sigma_range[0], self.sigma_range[1])

    def __call__(self, img, spatial_size, mode='bilinear'):
        """
        Args:
            img (ndarray or tensor): shape must be (num_channels, H, W, D),
            spatial_size (2 ints): specifying output image spatial size [h, w, d].
            mode ('nearest'|'bilinear'): interpolation order. Defaults to 'bilinear'.
        """
        self.randomize(spatial_size)
        grid = create_grid(spatial_size)
        if self.do_transform:
            for i in range(3):
                grid[i] += gaussian_filter(self.rand_offset[i], self.sigma, mode='constant', cval=0) * self.magnitude
            grid = self.rand_affine_grid(grid=grid)
        return self.resampler(img, grid, mode)


if __name__ == "__main__":
    # img = np.array((1, 2, 3, 4)).reshape((1, 2, 2))
    # rotator = RandRotate90(prob=0.0, max_k=3, axes=(1, 2))
    # # rotator.set_random_state(1234)
    # img_result = rotator(img)
    # print(type(img))
    # print(img_result)

    # np_im = np.zeros((3, 1201, 1601))
    # np_im = np_img
    np_im = np.zeros((3, 80, 80, 80))

    # new_img = Affine(translate_params=[-200, 300], scale_params=(1.2, 1.2))(np_img, (300, 400))
    # new_img = Rand2DElastic(prob=1.0, spacing=(20, 20), magnitude_range=(1.0, 4.0), translate_range=[400., 400.])(
    #     np_img, (300, 400))
    new_img = Rand3DElastic(prob=1.0, magnitude_range=(1.0, 4.0), sigma_range=(1., 4.),
                            translate_range=[20., 30., 10.])(np_im, (30, 40, 50))
    print(new_img.shape)
    # new_img = np.moveaxis(new_img, 0, -1).astype(int)
    # plt.imshow(new_img)
    # plt.show()
