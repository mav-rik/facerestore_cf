import os
import sys
repo_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, repo_dir)
from comfy import model_management
import torch
import comfy.utils
import numpy as np
import cv2
import math
from facelib.utils.face_restoration_helper import FaceRestoreHelper
from facelib.detection.retinaface import retinaface
from torchvision.transforms.functional import normalize
from comfy_extras.chainner_models import model_loading
import folder_paths
from basicsr.utils.registry import ARCH_REGISTRY
# import codeformer_arch

dir_facerestore_models = os.path.join(folder_paths.models_dir, "facerestore_models")
dir_facedetection_models = os.path.join(folder_paths.models_dir, "facedetection")
os.makedirs(dir_facerestore_models, exist_ok=True)
os.makedirs(dir_facedetection_models, exist_ok=True)
folder_paths.folder_names_and_paths["facerestore_models"] = ([dir_facerestore_models], folder_paths.supported_pt_extensions)
folder_paths.folder_names_and_paths["facedetection_models"] = ([dir_facedetection_models], folder_paths.supported_pt_extensions)

def img2tensor(imgs, bgr2rgb=True, float32=True):
    """Numpy array to tensor.

    Args:
        imgs (list[ndarray] | ndarray): Input images.
        bgr2rgb (bool): Whether to change bgr to rgb.
        float32 (bool): Whether to change to float32.

    Returns:
        list[tensor] | tensor: Tensor images. If returned results only have
            one element, just return tensor.
    """

    def _totensor(img, bgr2rgb, float32):
        if img.shape[2] == 3 and bgr2rgb:
            if img.dtype == 'float64':
                img = img.astype('float32')
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = torch.from_numpy(img.transpose(2, 0, 1))
        if float32:
            img = img.float()
        return img

    if isinstance(imgs, list):
        return [_totensor(img, bgr2rgb, float32) for img in imgs]
    else:
        return _totensor(imgs, bgr2rgb, float32)


def tensor2img(tensor, rgb2bgr=True, out_type=np.uint8, min_max=(0, 1)):
    """Convert torch Tensors into image numpy arrays.

    After clamping to [min, max], values will be normalized to [0, 1].

    Args:
        tensor (Tensor or list[Tensor]): Accept shapes:
            1) 4D mini-batch Tensor of shape (B x 3/1 x H x W);
            2) 3D Tensor of shape (3/1 x H x W);
            3) 2D Tensor of shape (H x W).
            Tensor channel should be in RGB order.
        rgb2bgr (bool): Whether to change rgb to bgr.
        out_type (numpy type): output types. If ``np.uint8``, transform outputs
            to uint8 type with range [0, 255]; otherwise, float type with
            range [0, 1]. Default: ``np.uint8``.
        min_max (tuple[int]): min and max values for clamp.

    Returns:
        (Tensor or list): 3D ndarray of shape (H x W x C) OR 2D ndarray of
        shape (H x W). The channel order is BGR.
    """
    if not (torch.is_tensor(tensor) or (isinstance(tensor, list) and all(torch.is_tensor(t) for t in tensor))):
        raise TypeError(f'tensor or list of tensors expected, got {type(tensor)}')

    if torch.is_tensor(tensor):
        tensor = [tensor]
    result = []
    for _tensor in tensor:
        _tensor = _tensor.squeeze(0).float().detach().cpu().clamp_(*min_max)
        _tensor = (_tensor - min_max[0]) / (min_max[1] - min_max[0])

        n_dim = _tensor.dim()
        if n_dim == 4:
            img_np = make_grid(_tensor, nrow=int(math.sqrt(_tensor.size(0))), normalize=False).numpy()
            img_np = img_np.transpose(1, 2, 0)
            if rgb2bgr:
                img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        elif n_dim == 3:
            img_np = _tensor.numpy()
            img_np = img_np.transpose(1, 2, 0)
            if img_np.shape[2] == 1:  # gray image
                img_np = np.squeeze(img_np, axis=2)
            else:
                if rgb2bgr:
                    img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        elif n_dim == 2:
            img_np = _tensor.numpy()
        else:
            raise TypeError('Only support 4D, 3D or 2D tensor. ' f'But received with dimension: {n_dim}')
        if out_type == np.uint8:
            # Unlike MATLAB, numpy.unit8() WILL NOT round by default.
            img_np = (img_np * 255.0).round()
        img_np = img_np.astype(out_type)
        result.append(img_np)
    if len(result) == 1:
        result = result[0]
    return result

class FaceRestoreCFWithModel:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "facerestore_model": ("FACERESTORE_MODEL",),
                              "image": ("IMAGE",),
                              "facedetection": (["retinaface_resnet50", "retinaface_mobile0.25", "YOLOv5l", "YOLOv5n"],),
                              "codeformer_fidelity": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1, "step": 0.05}),
                              "blend_factor": ("FLOAT", {"default": 1, "min": 0.0, "max": 1, "step": 0.01}),
                              "blend_factor_unmasked": ("FLOAT", {"default": 0, "min": 0.0, "max": 1, "step": 0.01}),
                              "inverse_mask": ("BOOLEAN", {"default": False})
                              },
                "optional": {"mask": ("MASK",)}
                }

    RETURN_TYPES = ("IMAGE",)

    FUNCTION = "restore_face"

    CATEGORY = "facerestore_cf"

    def __init__(self):
        self.face_helper = None

    def restore_face(self, facerestore_model, image: torch.Tensor, facedetection, codeformer_fidelity: float,
                     blend_factor: float, blend_factor_unmasked: float,
                     mask: torch.Tensor = None, inverse_mask: bool = False):
        print(f'\tStarting restore_face with codeformer_fidelity: {codeformer_fidelity}')
        device = model_management.get_torch_device()
        facerestore_model.to(device)
        if self.face_helper is None:
            self.face_helper = FaceRestoreHelper(1, face_size=512, crop_ratio=(1, 1), det_model=facedetection, save_ext='png', use_parse=True, device=device)

        image_np = 255. * image.cpu().numpy()

        total_images = image_np.shape[0]
        out_images = np.ndarray(shape=image_np.shape)

        for i in range(total_images):
            cur_image_np = image_np[i,:, :, ::-1]

            original_resolution = cur_image_np.shape[0:2]

            if facerestore_model is None or self.face_helper is None:
                return image

            self.face_helper.clean_all()
            self.face_helper.read_image(cur_image_np)
            self.face_helper.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
            self.face_helper.align_warp_face()

            restored_face = None
            for idx, cropped_face in enumerate(self.face_helper.cropped_faces):
                cropped_face_t = img2tensor(cropped_face / 255., bgr2rgb=True, float32=True)
                normalize(cropped_face_t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
                cropped_face_t = cropped_face_t.unsqueeze(0).to(device)

                try:
                    with torch.no_grad():
                        #output = facerestore_model(cropped_face_t, w=strength, adain=True)[0]
                        # output = facerestore_model(cropped_face_t)[0]
                        output = facerestore_model(cropped_face_t, w=codeformer_fidelity)[0]
                        restored_face = tensor2img(output, rgb2bgr=True, min_max=(-1, 1))
                    del output
                    torch.cuda.empty_cache()
                except Exception as error:
                    print(f'\tFailed inference for CodeFormer: {error}', file=sys.stderr)
                    restored_face = tensor2img(cropped_face_t, rgb2bgr=True, min_max=(-1, 1))

                restored_face = restored_face.astype('uint8')
                self.face_helper.add_restored_face(restored_face)

            self.face_helper.get_inverse_affine(None)

            restored_img = self.face_helper.paste_faces_to_input_image()
            restored_img = restored_img[:, :, ::-1]

            if original_resolution != restored_img.shape[0:2]:
                restored_img = cv2.resize(restored_img, (0, 0),
                                          fx=original_resolution[1]/restored_img.shape[1],
                                          fy=original_resolution[0]/restored_img.shape[0],
                                          interpolation=cv2.INTER_LINEAR)

            cur_image_np = cv2.cvtColor(cur_image_np, cv2.COLOR_BGR2RGB)

            if mask is not None:
                mask_np = np.transpose(mask.cpu().numpy(), (1, 2, 0))
                mask_np = np.repeat(mask_np, 3, axis=2)

                # Masked blending
                # Blending of masked parts
                masked_image_1 = cur_image_np * (mask_np if not inverse_mask else 1 - mask_np)
                masked_image_2 = restored_img * (mask_np if not inverse_mask else 1 - mask_np)
                blended_image_1 = masked_image_1 * (1 - blend_factor) + masked_image_2 * blend_factor
                blended_image_1 = np.clip(blended_image_1, 0, 255)
                # Blending of unmasked parts
                unmasked_image_1 = cur_image_np * ((1 - mask_np) if not inverse_mask else mask_np)
                unmasked_image_2 = restored_img * ((1 - mask_np) if not inverse_mask else mask_np)
                blended_image_2 = unmasked_image_1 * (1 - blend_factor_unmasked) + unmasked_image_2 * blend_factor_unmasked
                blended_image_2 = np.clip(blended_image_2, 0, 255)

                restored_img = blended_image_1 + blended_image_2
            else:
                # Blend
                restored_img = cur_image_np * (1 - blend_factor) + restored_img * blend_factor
                restored_img = np.clip(restored_img, 0, 255)

            self.face_helper.clean_all()

            out_images[i] = restored_img

        restored_img_np = np.array(out_images).astype(np.float32) / 255.0
        restored_img_tensor = torch.from_numpy(restored_img_np)

        return (restored_img_tensor,)


class CropFace:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "image": ("IMAGE",),
                              "facedetection": (["retinaface_resnet50", "retinaface_mobile0.25", "YOLOv5l", "YOLOv5n"],)
                              }}

    RETURN_TYPES = ("IMAGE",)

    FUNCTION = "crop_face"

    CATEGORY = "facerestore_cf"

    def __init__(self):
        self.face_helper = None

    def crop_face(self, image, facedetection):
        device = model_management.get_torch_device()
        if self.face_helper is None:
            self.face_helper = FaceRestoreHelper(1, face_size=512, crop_ratio=(1, 1), det_model=facedetection, save_ext='png', use_parse=True, device=device)

        image_np = 255. * image.cpu().numpy()

        total_images = image_np.shape[0]
        out_images = np.ndarray(shape=(total_images, 512, 512, 3))
        next_idx = 0

        for i in range(total_images):

            cur_image_np = image_np[i,:, :, ::-1]

            original_resolution = cur_image_np.shape[0:2]

            if self.face_helper is None:
                return image

            self.face_helper.clean_all()
            self.face_helper.read_image(cur_image_np)
            self.face_helper.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
            self.face_helper.align_warp_face()

            faces_found = len(self.face_helper.cropped_faces)
            if faces_found == 0:
                next_idx += 1 # output black image for no face
            if out_images.shape[0] < next_idx + faces_found:
                print(out_images.shape)
                print((next_idx + faces_found, 512, 512, 3))
                print('aaaaa')
                out_images = np.resize(out_images, (next_idx + faces_found, 512, 512, 3))
                print(out_images.shape)
            for j in range(faces_found):
                cropped_face_1 = self.face_helper.cropped_faces[j]
                cropped_face_2 = img2tensor(cropped_face_1 / 255., bgr2rgb=True, float32=True)
                normalize(cropped_face_2, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
                cropped_face_3 = cropped_face_2.unsqueeze(0).to(device)
                cropped_face_4 = tensor2img(cropped_face_3, rgb2bgr=True, min_max=(-1, 1)).astype('uint8')
                cropped_face_5 = cv2.cvtColor(cropped_face_4, cv2.COLOR_BGR2RGB)
                out_images[next_idx] = cropped_face_5
                next_idx += 1

        cropped_face_6 = np.array(out_images).astype(np.float32) / 255.0
        cropped_face_7 = torch.from_numpy(cropped_face_6)
        return (cropped_face_7,)

class FaceRestoreModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "model_name": (folder_paths.get_filename_list("facerestore_models"), ),
                             }}
    RETURN_TYPES = ("FACERESTORE_MODEL",)
    FUNCTION = "load_model"

    CATEGORY = "facerestore_cf"

    # def load_model(self, model_name):
    #     model_path = folder_paths.get_full_path("facerestore_models", model_name)
    #     sd = comfy.utils.load_torch_file(model_path, safe_load=True)
    #     out = model_loading.load_state_dict(sd).eval()
    #     return (out, )

    def load_model(self, model_name):
        if "codeformer" in model_name.lower():
            print(f'\tLoading CodeFormer: {model_name}')
            model_path = folder_paths.get_full_path("facerestore_models", model_name)
            device = model_management.get_torch_device()
            codeformer_net = ARCH_REGISTRY.get("CodeFormer")(
                dim_embd=512,
                codebook_size=1024,
                n_head=8,
                n_layers=9,
                connect_list=["32", "64", "128", "256"],
            ).to(device)
            checkpoint = torch.load(model_path)["params_ema"]
            codeformer_net.load_state_dict(checkpoint)
            out = codeformer_net.eval()  
            return (out, )
        else:
            model_path = folder_paths.get_full_path("facerestore_models", model_name)
            sd = comfy.utils.load_torch_file(model_path, safe_load=True)
            out = model_loading.load_state_dict(sd).eval()
            return (out, )

NODE_CLASS_MAPPINGS = {
    "FaceRestoreCFWithModel": FaceRestoreCFWithModel,
    "CropFace": CropFace,
    "FaceRestoreModelLoader": FaceRestoreModelLoader,
}
