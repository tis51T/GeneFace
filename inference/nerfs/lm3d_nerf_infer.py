import os
import numpy as np
import torch
import tqdm
import cv2
import importlib
import math
from scipy.ndimage import gaussian_filter1d

from inference.nerfs.base_nerf_infer import BaseNeRFInfer
from utils.commons.ckpt_utils import load_ckpt
from utils.commons.hparams import hparams, set_hparams
from utils.commons.tensor_utils import move_to_cuda, convert_to_tensor, convert_to_np
from utils.commons.euler2rot import euler_trans_2_c2w, c2w_to_euler_trans


class LM3dNeRFInfer(BaseNeRFInfer):

    def get_cond_from_input(self, inp):
        """
        :param inp: {'audio_source_name': (str), 'cond_name': (str, optional)}
        :return: a list that contains the condition feature of NeRF
        """
        self.save_wav16k(inp)

        # load the lm3d as the condition for lm3d head nerf
        assert inp['cond_name'].endswith('.npy')
        lm3d_arr = np.load(inp['cond_name'])[0] # [T, w=16, c=29]
        idexp_lm3d = torch.from_numpy(lm3d_arr).float()
        print(f"Loaded pre-extracted 3D landmark sequence from {inp['cond_name']}!")
        
        # load the deepspeech features as the condition for lm3d torso nerf
        wav16k_name = self.wav16k_name
        deepspeech_name = wav16k_name[:-4] + '_deepspeech.npy'
        if not os.path.exists(deepspeech_name):
            print(f"Try to extract deepspeech from {wav16k_name}...")
            # deepspeech_python = '/home/yezhenhui/anaconda3/envs/geneface/bin/python' # the path of your python interpreter that has installed DeepSpeech
            # extract_deepspeech_cmd = f'{deepspeech_python} data_util/deepspeech_features/extract_ds_features.py --input={wav16k_name} --output={deepspeech_name}'
            extract_deepspeech_cmd = f'python data_util/deepspeech_features/extract_ds_features.py --input={wav16k_name} --output={deepspeech_name}'
            os.system(extract_deepspeech_cmd)
            print(f"Saved deepspeech features of {wav16k_name} to {deepspeech_name}.")
        else:
            print(f"Try to load pre-extracted deepspeech from {deepspeech_name}...")
        deepspeech_arr = np.load(deepspeech_name) # [T, w=16, c=29]
        print(f"Loaded deepspeech features from {deepspeech_name}.")
        # get window condition of deepspeech
        from data_gen.nerf.binarizer import get_win_conds
        num_samples = min(len(lm3d_arr), len(deepspeech_arr), self.infer_max_length)
        samples = [{} for _ in range(num_samples)]
        for idx, sample in enumerate(samples):
            sample['deepspeech_win'] = torch.from_numpy(deepspeech_arr[idx]).float().unsqueeze(0) # [B=1, w=16, C=29]
            sample['deepspeech_wins'] = torch.from_numpy(get_win_conds(deepspeech_arr, idx, smo_win_size=8)).float() # [W=8, w=16, C=29]
        
        idexp_lm3d_mean = self.dataset.idexp_lm3d_mean
        idexp_lm3d_std = self.dataset.idexp_lm3d_std
        idexp_lm3d_normalized = (idexp_lm3d.reshape([-1,68,3]) - idexp_lm3d_mean)/idexp_lm3d_std
        # idexp_lm3d_normalized = idexp_lm3d_normalized.reshape([-1, 68*3])
        # torch.clamp() is adequate to ensure the stability of rendering head
        # idexp_lm3d_normalized = torch.clamp(idexp_lm3d_normalized, -2, 2)

        # fix by group-wise clamp
        idexp_lm3d_normalized[:,0:17] = torch.clamp(idexp_lm3d_normalized[:,0:17], -2, 2) # yaw_x_y_z
        idexp_lm3d_normalized[:,17:27,0:2] = torch.clamp(idexp_lm3d_normalized[:,17:27,0:2], -1, 1) # brow_x_y
        idexp_lm3d_normalized[:,17:27,2] = torch.clamp(idexp_lm3d_normalized[:,17:27,2], -2, 2) # brow_z
        idexp_lm3d_normalized[:,27:36] = torch.clamp(idexp_lm3d_normalized[:,27:36], -2, 2) # nose
        idexp_lm3d_normalized[:,36:48,0:2] = torch.clamp(idexp_lm3d_normalized[:,36:48,0:2], -1, 1) # eye_x_y
        idexp_lm3d_normalized[:,36:48,2] = torch.clamp(idexp_lm3d_normalized[:,36:48,2], -2, 2) # eye_z
        idexp_lm3d_normalized[:,48:68] = torch.clamp(idexp_lm3d_normalized[:,48:68], -2, 2) # mouth

        idexp_lm3d_normalized = idexp_lm3d_normalized.reshape([-1,68*3])
        idexp_lm3d_normalized[:, :48*3] = convert_to_tensor(gaussian_filter1d(idexp_lm3d_normalized[:, :48*3].numpy(), sigma=1.))
        # max_face = idexp_lm3d_normalized[0].unsqueeze(0).repeat([edit_mouth_mask2.sum(), 1,1])
        # max_mouth_y = max_mouth_y[:, 48:68, :]
        # max_xiaba_y = max_face[:, 0:17, :]
        # max_brow_y = max_face[:, 17:27, :]
        # max_nose_y = max_face[:, 27:36, :]
        # max_eye_y = max_face[:, 36:48, :]
        # max_mouth_y = max_face[:, 48:68, :]
        # max_mouth_y = torch.tensor([0.]*68).unsqueeze(0).repeat([edit_mouth_mask2.sum(), 1])
        # idexp_lm3d_normalized[:,48:68,1][edit_mouth_mask2] = max_mouth_y
        # idexp_lm3d_normalized[:,:,][edit_mouth_mask2] = max_mouth_y.unsqueeze(-1).repeat([1,1,3])
        # idexp_lm3d_normalized[:,0:17,][edit_mouth_mask2] = max_xiaba_y
        # idexp_lm3d_normalized[:,17:27,][edit_mouth_mask2] = max_brow_y # 影响程度一般
        # idexp_lm3d_normalized[:,27:36,][edit_mouth_mask2] = max_nose_y
        # idexp_lm3d_normalized[:,36:48,][edit_mouth_mask2] = max_eye_y # 影响程度很大
        # idexp_lm3d_normalized[:,48:68,][edit_mouth_mask2] = max_mouth_y
        # idexp_lm3d_normalized = idexp_lm3d_normalized.reshape([-1,68*3]

        # fix bad cases by using fixed landmark
        # max_face = idexp_lm3d_normalized[0].unsqueeze(0).repeat([len(idexp_lm3d_normalized), 1,1])
        # max_brow_y = max_face[:, 17:27, :]
        # max_eye_y = max_face[:, 36:48, :]
        # idexp_lm3d_normalized[:,17:27] = max_brow_y
        # idexp_lm3d_normalized[:,36:48] = max_eye_y
        # idexp_lm3d_normalized = idexp_lm3d_normalized.reshape([-1,68*3])
        
        # Find bad cases via mouth lm then fix them by setting values
        # edit_mouth_mask1 = idexp_lm3d_normalized.reshape([-1,68,3])[:,48:68,1] > 2
        # edit_mouth_mask1 = idexp_lm3d_normalized.reshape([-1,68,3])[:,62,1] >= 2 # [N]
        # edit_mouth_mask2 = idexp_lm3d_normalized.reshape([-1,68,3])[:,48:68,1] < -2
        # edit_mouth_mask2 = idexp_lm3d_normalized.reshape([-1,68,3])[:,66,1] <= -2# [N]
        # edit_mouth_mask = torch.bitwise_or(edit_mouth_mask1,edit_mouth_mask2).sum(dim=1).bool() # [N,]
        # bad_slices = [] # list of (start_idx, dur)
        # start_idx = None
        # end_idx = None
        # is_bad_indexs = np.where(edit_mouth_mask.numpy())[0]
        # for idx in is_bad_indexs:
        #     if start_idx is None:
        #         start_idx = idx
        #         end_idx = start_idx
        #     else:
        #         if idx - end_idx == 1:
        #             end_idx = idx
        #         else:
        #             bad_slices.append([start_idx, end_idx])
        #             start_idx = idx
        #             end_idx = start_idx
        # bad_slices.append([start_idx, end_idx]) # the list of start,end index of outliers
        # for (start,end) in bad_slices: 
        #     if end-start >= 5:
        #         num_pad = 10
        #         start = max(0, start - num_pad)
        #         end = min(end+10,len(idexp_lm3d_normalized))
        #         idexp_lm3d_normalized[start:end+1,48*3:68*3] = convert_to_tensor(gaussian_filter1d(idexp_lm3d_normalized[start:end+1,48*3:68*3].numpy(), sigma=1.0))
        # idexp_lm3d_normalized[:,48:68,1][edit_mouth_mask1] = 1
        # idexp_lm3d_normalized[:,48:68,1][edit_mouth_mask2] = -1
        # idexp_lm3d_normalized = idexp_lm3d_normalized.reshape([-1, 68*3])

        idexp_lm3d_normalized_numpy = idexp_lm3d_normalized.cpu().numpy()
        idexp_lm3d_normalized_win_numpy = np.stack([get_win_conds(idexp_lm3d_normalized_numpy, i, smo_win_size=hparams['cond_win_size'], pad_option='edge') for i in range(idexp_lm3d_normalized_numpy.shape[0])])
        idexp_lm3d_normalized_win = torch.from_numpy(idexp_lm3d_normalized_win_numpy)

        for idx, sample in enumerate(samples):
            sample['cond'] = idexp_lm3d_normalized[idx].unsqueeze(0)
            if hparams['use_window_cond']:
                sample['cond_win'] = idexp_lm3d_normalized_win[idx]
                sample['cond_wins'] = torch.from_numpy(get_win_conds(idexp_lm3d_normalized_win_numpy, idx, hparams['smo_win_size'], 'edge'))
        return samples


if __name__ == '__main__':
    from utils.commons.hparams import set_hparams
    from utils.commons.hparams import hparams as hp
    inp = {
            'audio_source_name': 'data/raw/val_wavs/zozo.wav',
            'cond_name': 'infer_out/May/pred_lm3d/zozo.npy',
            'out_video_name': 'infer_out/May/pred_video/zozo.mp4',
            }

    LM3dNeRFInfer.example_run(inp)