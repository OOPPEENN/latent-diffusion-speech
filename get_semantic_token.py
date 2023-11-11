import torch
import cluster
import numpy as np 
import torch.multiprocessing as mp
import argparse
from logger import utils
import os
from logger.utils import traverse_dir
from tqdm import tqdm

@torch.no_grad()
def preprocess_utterance(rank, units_path, model,in_dir, out_dir, num_workers, units_quantize_type = "kmeans"):
    units_path = units_path[rank::num_workers]
    if units_quantize_type == "vq":
        model = model.to(f"cuda:{rank%num_workers}")
    for unit_path in tqdm(units_path,position=rank):
        if units_quantize_type == "kmeans":
            unit = np.load(os.path.join(in_dir, "units" , unit_path))
            token = cluster.get_cluster_result(model, unit)
            out_path = os.path.join(out_dir, unit_path)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            np.save(out_path, token)
        elif units_quantize_type == "vq":
            unit = torch.from_numpy(np.load(os.path.join(in_dir, "units" , unit_path))).to(f"cuda:{rank%num_workers}")[None,:]
            _, token, _ = model(unit)
            token = token[0].detach().cpu().numpy()
            out_path = os.path.join(out_dir, unit_path)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            np.save(out_path, token)

def preprocess(in_dir, units_quantize_type, model, num_workers=1):
    out_dir = os.path.join(in_dir, "semantic_token")
    os.makedirs(out_dir, exist_ok=True)
    units_dir = os.path.join(in_dir, "units")
    filelist = traverse_dir(
        units_dir,
        extensions=["npy"],
        is_pure=True,
        is_sort=True,
        is_ext=True)
    
    mp.spawn(preprocess_utterance, args=(filelist, model,in_dir, out_dir, num_workers, units_quantize_type), nprocs=num_workers, join=True)
    
def parse_args(args=None, namespace=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default='configs/config.yaml',
        help="path to the config file")
    parser.add_argument(
        "-n",
        "--num_workers",
        type=int,
        default= 10,
        help="num workers")
    return parser.parse_args(args=args, namespace=namespace)

if __name__ == "__main__":
    cmd = parse_args()
    args = utils.load_config(cmd.config)
    num_workers = cmd.num_workers
    if args.train.units_quantize_type == "kmeans":
        model = cluster.get_cluster_model(args.model.text2semantic.codebook_path)
    elif args.train.units_quantize_type == "vq":
        from vector_quantize_pytorch import VectorQuantize
        model = VectorQuantize(
                dim = args.data.encoder_out_channels,
                codebook_size = args.model.text2semantic.semantic_kmeans_num,
                decay = 0.8,             
                commitment_weight = 1. 
            )
        model_para = torch.load(args.model.text2semantic.codebook_path)
        model.load_state_dict(model_para["model"])
    else:
        raise ValueError('[x] Unknown quantize_type: ' + args.train.units_quantize_type)
    preprocess(args.data.train_path,args.train.units_quantize_type, model, num_workers=num_workers)
    preprocess(args.data.valid_path,args.train.units_quantize_type, model, num_workers=num_workers)