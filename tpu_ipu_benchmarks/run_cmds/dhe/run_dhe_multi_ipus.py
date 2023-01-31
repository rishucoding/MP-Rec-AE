import numpy as np

import sys
from time import time

import poptorch

import torch

from gen_data import GeneratedDataset
from models.dhe.dhe_multi_ipu import DHE_MULTI_IPU
from utils import fix_seed, multi_ipu_unpack_batch, parse_configurations, parse_dims

def main():
    # Ensure IPU Execution
    assert args.device == 'ipu'

    # Experiment Arguments
    args = parse_configurations()
    args.mlp_bot_dims = parse_dims(args.mlp_bot_dims)
    args.mlp_top_dims = parse_dims(args.mlp_top_dims)
    args.table_sizes  = parse_dims(args.table_sizes)
    args.precision    = eval(args.precision)

    print('-------------------- Enabling DHE --------------------')
    # DHE Parameters
    args_dhe = {}
    args_dhe['activation']  = args.dhe_activation
    args_dhe['batch_norm']  = args.dhe_batch_norm
    args_dhe['hash_fn']     = args.dhe_hash_fn
    args_dhe['k']           = args.dhe_k
    args_dhe['m']           = args.dhe_m
    args_dhe['mlp_dims']    = parse_dims(args.dhe_mlp_dims)
    args_dhe['num_lookups'] = args.num_lookups
    args_dhe['precision']   = args.precision
    args_dhe['seed']        = args.seed
    args_dhe['transform']   = args.dhe_transform
    
    print('---------- DHE Parameters: ----------')
    for key, val in args_dhe.items():
        print('{} : {}'.format(key, val))
    print('--------------------')

    # Fix Seed
    fix_seed(args.seed)

    # Data Generation
    dataset = GeneratedDataset(
        num_batches = args.num_batches,
        batch_size  = args.batch_size,
        dense_dim   = args.dense_dim,
        table_sizes = args.table_sizes,
        num_lookups = args.num_lookups,
        precision   = args.precision
    )

    # Model and Optimizer Setup
    constant_offsets = torch.tensor(np.arange(args.batch_size)*args.num_lookups)
    model = DHE_MULTI_IPU(
        dense_dim        = args.dense_dim,
        mlp_bot_dims     = args.mlp_bot_dims,
        mlp_top_dims     = args.mlp_top_dims,
        emb_dim          = args.emb_dim,
        table_sizes      = args.table_sizes,
        num_lookups      = args.num_lookups,
        precision        = args.precision,
        constant_offsets = constant_offsets,
        args_dhe         = args_dhe
    )
    # Create Optimizer
    optimizer = poptorch.optim.SGD(model.parameters(), lr=args.lr)

    print(model)

    # Training
    if args.train == True:
        # Model Compilation
        model.train()
        opts = poptorch.Options()
        poptorch_dataloader = poptorch.DataLoader(
            opts,
            dataset,
            batch_size=1,
            shuffle=False,
            num_workers=0)
        poptorch_model_training = poptorch.trainingModel(model, 
                                                        options=opts, 
                                                        optimizer=optimizer)
        # Pre-Compile Model
        x_dense_compile, _, x_indices_compile, y_compile = multi_ipu_unpack_batch(next(iter(poptorch_dataloader)))

        t0 = time()
        poptorch_model_training.compile(x_dense_compile, x_indices_compile, y_compile)
        t1 = time()
        print('<IPU> Compilation Time: {:.3f} s'.format(t1-t0))

        # Training Flow
        for epoch in range(args.num_epochs):
            print('Epoch {} ===================='.format(epoch))
            for i, inputBatch in enumerate(poptorch_dataloader):
                x_dense, _, x_indices, y = multi_ipu_unpack_batch(inputBatch)
                output, loss = poptorch_model_training(x_dense, x_indices, y)
        poptorch_model_training.detachFromDevice()

    # Inference
    if args.inference == True:
        # Model Compilation
        model = model.eval()
        opts = poptorch.Options()
        opts.deviceIterations(args.ipu_device_iterations)
        opts.replicationFactor(args.ipu_replicas)

        poptorch_dataloader = poptorch.DataLoader(
            opts,
            dataset,
            batch_size=1,
            shuffle=False,
            num_workers=0)
        poptorch_model_inference = poptorch.inferenceModel(model,
                                                        options=opts)
        # Pre-Compile Model
        x_dense_compile, _, x_indices_compile, _ = multi_ipu_unpack_batch(next(iter(poptorch_dataloader)))

        t0 = time()
        poptorch_model_inference.compile(x_dense_compile, x_indices_compile)
        t1 = time()
        print('<IPU> Compilation Time: {:.3f} s'.format(t1-t0))

        # Inference Flow
        for epoch in range(args.num_epochs):
            print('Epoch {} ===================='.format(epoch))
            for i, inputBatch in enumerate(poptorch_dataloader):
                x_dense, _, x_indices, _ = multi_ipu_unpack_batch(inputBatch)
                output = poptorch_model_inference(x_dense, x_indices)
        poptorch_model_inference.detachFromDevice()

if __name__ == '__main__':
    main()