import numpy as np
import torch
import os
import cv2
import math
import datetime
import glob

from scipy.spatial.distance import cdist
from torch.utils.data import Dataset

from models.superpoint import SuperPoint

import sys

def frame2tensor(frame):
    return torch.from_numpy(frame/255.).float()[None, None].to("cuda:0")

class SparseDataset(Dataset):
    """
    Sparse correspondences dataset.
    Dataset folder architecture:
    - dataset_name
        - place1
            - test
                - images
                    - *.jpg
            - train
                - images
                    - *.jpg
            -val
                - images
                    - *.jpg
        - place2
            - test
                - images
                    - *.jpg
            - train
                - images
                    - *.jpg
            -val
                - images
                    - *.jpg
        ...
    """

    config = {
        'superpoint': {
            'descriptor_dim': 256,
            'nms_radius': 1,
            'keypoint_threshold': 0.015,
            'max_keypoints': -1,
            'remove_borders': 4,
            }
        }

    def __init__(self, root_path, mode, nfeatures):
        self.files = glob.glob(os.path.join(root_path, '*', mode, 'images', '*.jpg'), recursive=True)
        self.nfeatures = nfeatures
        self.sift = cv2.SIFT_create()
        self.superpoint = SuperPoint(self.config.get('superpoint')).to('cuda:0')
        # self.superpoint = SuperPoint().to('cuda:0')

        self.sift.setNFeatures(maxFeatures=self.nfeatures)
        self.matcher = cv2.BFMatcher(cv2.NORM_L1, crossCheck=False)

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        file_name = self.files[idx]
        image = cv2.imread(file_name, cv2.IMREAD_GRAYSCALE) 
        sift = self.sift
        sp = self.superpoint
        width, height = image.shape[:2]
        corners = np.array([[0, 0], [0, height], [width, 0], [width, height]], dtype=np.float32)
        warp = np.random.randint(-224, 224, size=(4, 2)).astype(np.float32)

        # get the corresponding warped image
        M = cv2.getPerspectiveTransform(corners, corners + warp)
        warped = cv2.warpPerspective(src=image, M=M, dsize=(image.shape[1], image.shape[0])) # return an image type
        
        # # extract keypoints of the image pair using SIFT
        # kp1, descs1 = sift.detectAndCompute(image, None)
        # kp2, descs2 = sift.detectAndCompute(warped, None)

        image_tensor = frame2tensor(image)
        warped_tensor = frame2tensor(warped)

        pred1 = sp(image_tensor)
        pred2 = sp(warped_tensor)

        #keypoints,descriptors,scores
        kp1_np=pred1["keypoints"][0].cpu().detach().numpy()#(636,2)
        kp2_np=pred2["keypoints"][0].cpu().detach().numpy()#(666,2)

        descs1=pred1["descriptors"][0].cpu().detach().numpy().transpose()#(636,256)
        descs2=pred2["descriptors"][0].cpu().detach().numpy().transpose()#(666,256)


        # limit the number of keypoints
        kp1_num = max(self.nfeatures, len(kp1_np))
        kp2_num = max(self.nfeatures, len(kp2_np))
        kp1 = kp1_np[:kp1_num]
        kp2 = kp2_np[:kp2_num]

        # kp1_np = np.array([(kp.pt[0], kp.pt[1]) for kp in kp1])
        # kp2_np = np.array([(kp.pt[0], kp.pt[1]) for kp in kp2])

        # skip this image pair if no keypoints detected in image
        if len(kp1) < 1 or len(kp2) < 1:
            return{
                'keypoints0': torch.zeros([0, 0, 2], dtype=torch.double),
                'keypoints1': torch.zeros([0, 0, 2], dtype=torch.double),
                'descriptors0': torch.zeros([0, 2], dtype=torch.double),
                'descriptors1': torch.zeros([0, 2], dtype=torch.double),
                'image0': image,
                'image1': warped,
                'file_name': file_name
            } 

        # confidence of each key point
        # scores1_np = np.array([kp.response for kp in kp1]) 
        # scores2_np = np.array([kp.response for kp in kp2])

        scores1_np=pred1["scores"][0].cpu().detach().numpy()#(636,)
        scores2_np=pred2["scores"][0].cpu().detach().numpy()#(666,)


        kp1_np = kp1_np[:kp1_num, :]
        kp2_np = kp2_np[:kp2_num, :]
        descs1 = descs1[:kp1_num, :]
        descs2 = descs2[:kp2_num, :]

        # obtain the matching matrix of the image pair
        matched = self.matcher.match(descs1, descs2)
        kp1_projected = cv2.perspectiveTransform(kp1_np.reshape((1, -1, 2)), M)[0, :, :] 
        dists = cdist(kp1_projected, kp2_np)

        min1 = np.argmin(dists, axis=0)
        min2 = np.argmin(dists, axis=1)

        min1v = np.min(dists, axis=1)
        min1f = min2[min1v < 3]

        xx = np.where(min2[min1] == np.arange(min1.shape[0]))[0]
        matches = np.intersect1d(min1f, xx)

        missing1 = np.setdiff1d(np.arange(kp1_np.shape[0]), min1[matches])
        missing2 = np.setdiff1d(np.arange(kp2_np.shape[0]), matches)

        MN = np.concatenate([min1[matches][np.newaxis, :], matches[np.newaxis, :]])
        MN2 = np.concatenate([missing1[np.newaxis, :], (len(kp2)) * np.ones((1, len(missing1)), dtype=np.int64)])
        MN3 = np.concatenate([(len(kp1)) * np.ones((1, len(missing2)), dtype=np.int64), missing2[np.newaxis, :]])
        all_matches = np.concatenate([MN, MN2, MN3], axis=1)

        kp1_np = kp1_np.reshape((1, -1, 2))
        kp2_np = kp2_np.reshape((1, -1, 2))
        descs1 = np.transpose(descs1 / 256.)
        descs2 = np.transpose(descs2 / 256.)

        # image = torch.from_numpy(image/255.).double()[None].cuda()
        # warped = torch.from_numpy(warped/255.).double()[None].cuda()
        image = torch.from_numpy(image/255.).double()[None].to('cuda:0')
        warped = torch.from_numpy(warped/255.).double()[None].to('cuda:0')

        return{
            'keypoints0': list(kp1_np),
            'keypoints1': list(kp2_np),
            'descriptors0': list(descs1),
            'descriptors1': list(descs2),
            'scores0': list(scores1_np),
            'scores1': list(scores2_np),
            'image0': image,
            'image1': warped,
            'all_matches': list(all_matches),
            'file_name': file_name
        } 