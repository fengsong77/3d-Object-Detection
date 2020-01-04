import pickle
import torch.utils.data
from LSUV_pytorch.LSUV import LSUVinit
from config import cfg
from model.model import PPModel,PPScatter
from model.loss import PPLoss
from data.dataset import PPDataset
from tqdm import tqdm
import pdb
import gc
import pathlib
import os.path as osp
from evaluate import evaluate
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import patches, patheffects
from pyquaternion import Quaternion
from utils.box_utils import boxes_to_image_space
from lyft_dataset_sdk.utils.data_classes import LidarPointCloud,Box

def get_batch(dataset,batch_size,ind):
    """
        helper method for creating a 
        batch w/o datalodaer - used
        for debugging
    """
    pil_list = []
    ind_list = []
    tar_list = []
    for i in range(ind,ind + batch_size):
        pil,ind,tar = dataset[i]
        pil_list.append(pil)
        ind_list.append(ind)
        tar_list.append(tar)
    pil_out = torch.stack(pil_list)
    ind_out = torch.stack(ind_list)
    tar_out = torch.stack(tar_list)
    return (pil_out,ind_out,tar_out)

# make filepaths
ddfp = osp.join(cfg.DATA.LIDAR_TRAIN_DIR,'data_dict.pkl')
boxfp = osp.join(cfg.DATA.ANCHOR_DIR,'anchor_boxes.pkl')
crnfp = osp.join(cfg.DATA.ANCHOR_DIR,'anchor_corners.pkl')
cenfp = osp.join(cfg.DATA.ANCHOR_DIR,'anchor_centers.pkl')
xyfp = osp.join(cfg.DATA.ANCHOR_DIR,'anchor_xy.pkl')
token_fp = osp.join(cfg.DATA.TOKEN_TRAIN_DIR,'token_list.pkl')

# load data for traning
data_dict = pickle.load(open(ddfp,'rb'))
anchor_boxes = pickle.load(open(boxfp,'rb'))
anchor_corners = pickle.load(open(crnfp,'rb'))
anchor_centers = pickle.load(open(cenfp,'rb'))
anchor_xy = pickle.load(open(xyfp,'rb'))
data_mean = pickle.load(open('pillar_means.pkl','rb'))
token_list = pickle.load(open(token_fp,'rb'))

device     = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
#device    = 'cpu'
pp_dataset = PPDataset(token_list,data_dict,anchor_boxes,
                      anchor_corners,anchor_centers,data_mean,training=True)

batch_size  = cfg.NET.BATCH_SIZE
epochs      = cfg.NET.EPOCHS
num_workers = cfg.NET.NUM_WORKERS
dataloader  = torch.utils.data.DataLoader(pp_dataset,batch_size,
                                         shuffle=False,num_workers=num_workers)

# get model configs
fn_in = cfg.NET.FEATURE_NET_IN
fn_out = cfg.NET.FEATURE_NET_OUT
cls_channels = len(cfg.DATA.ANCHOR_DIMS)*cfg.DATA.NUM_CLASSES
reg_channels = len(cfg.DATA.ANCHOR_DIMS)*cfg.DATA.REG_DIMS

pp_model = PPModel(fn_in,fn_out,cls_channels,reg_channels,device)
pp_loss  = PPLoss(cfg.NET.B_ORT,cfg.NET.B_REG,cfg.NET.B_CLS,cfg.NET.GAMMA,device)

pp_model = pp_model.to(device)
pp_loss  = pp_loss.to(device)

lr = cfg.NET.LEARNING_RATE
wd = cfg.NET.WEIGHT_DECAY
params = list(pp_model.parameters())
optim  = torch.optim.Adam(params,lr=lr,weight_decay=wd)

load_model = True
model_fp = osp.join(cfg.DATA.CKPT_DIR,'pp_checkpoint_0_2000.pth')
optim_fp = osp.join(cfg.DATA.CKPT_DIR,'optim_checkpoint_0_2000.pth')

if load_model:
    err_code = pp_model.load_state_dict(torch.load(model_fp))
    print(err_code)
    err_code = optim.load_state_dict(torch.load(optim_fp))
    print(err_code)
    pp_model.train()
    gc.collect()

else:
    #LSUV INIT
    (p,i,_,__) = next(iter(dataloader))
    p = p.to(device)
    i = i.to(device)

    pp_model.feature_net = LSUVinit(pp_model.feature_net,p,needed_std = 1.0, std_tol = 0.1, max_attempts = 10, do_orthonorm = False)
    feature_out = pp_model.feature_net(p)
    scatter_out = pp_model.scatter(feature_out,i)
    pp_model.backbone = LSUVinit(pp_model.backbone,scatter_out,needed_std = 1.0, std_tol = 0.1, max_attempts = 10, do_orthonorm = False)
    backbone_out = pp_model.backbone(scatter_out)
    pp_model.det_head = LSUVinit(pp_model.det_head,backbone_out,needed_std = 1.0, std_tol = 0.1, max_attempts = 10, do_orthonorm = False)

# set last layer bias for focal loss init
    pi = 0.01
    pp_model.det_head.cls.bias.data.fill_(-np.log((1-pi)/pi))


b_cls = cfg.NET.B_CLS
b_reg = cfg.NET.B_REG
b_ort = cfg.NET.B_ORT

print('STARTING TRAINING')

for epoch in range(epochs):
    print('EPOCH: ',epoch)
    epoch_losses = []
    progress_bar = tqdm(dataloader)
    for i,(pillar,inds,c_target,r_target) in enumerate(progress_bar):
        pillar = pillar.to(device)
        inds = inds.to(device)
        c_target = c_target.to(device)
        r_target = r_target.to(device)
        cls_tensor,reg_tensor = pp_model(pillar,inds)
        c_loss,r_loss,o_loss,batch_loss = pp_loss(cls_tensor,reg_tensor,c_target,r_target) 
        optim.zero_grad()
        batch_loss.backward()
        optim.step()
        gc.collect()
        if i % 10 == 0:
            print('tot: ',batch_loss)
            print('-------------------------------------------------')
            print('cls raw: ',c_loss)
            print('reg raw: ',r_loss)
            print('ort raw: ',o_loss)
            print('-------------------------------------------------')
            print('cls wgt: ',b_cls*c_loss)
            print('reg wgt: ',b_reg*r_loss)
            print('ort wgt: ',b_ort*o_loss)
            print('-------------------------------------------------')

        if i % 2000 == 0 and i!= 0:
            print('saving model checkpoint')
            with torch.no_grad():
                cpdir = cfg.DATA.CKPT_DIR
                cpfp  = osp.join(cpdir,'pp_checkpoint_{}_{}.pth'.format(epoch,i))
                torch.save(pp_model.state_dict(),cpfp)
                opfp  = osp.join(cpdir,'optim_checkpoint_{}_{}.pth'.format(epoch,i))
                torch.save(optim.state_dict(),opfp)
        """
        if i % 4000 == 0 and i != 0:
            with torch.no_grad():
                print('STARTING EVALUATION')
                mAP = evaluate(pp_model,anchor_boxes,data_mean,device)
                print('val mAP: ',mAP)
                gc.collect()    
        """
    epoch_losses.append(batch_loss.detach().cpu().numpy())

print('epoch losses: ',epoch_losses)
print('TRAINING FINISHED')



