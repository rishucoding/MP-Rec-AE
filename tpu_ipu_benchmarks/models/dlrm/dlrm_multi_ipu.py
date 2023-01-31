import numpy as np
from numpy import random as ra

import sys

import poptorch

import torch
import torch.nn as nn

class DLRM_MULTI_IPU(nn.Module):
    
    # Create MLP layers
    def create_mlp(
        self, 
        mlp_dims,
        en_sigmoid,
        precision=np.float32
    ):
        if precision == np.float32:
            bytes_per_param = 4
        elif precision == np.float16:
            bytes_per_param = 2
        else:
            sys.exit('Unsupported Datatype')
        
        layers    = nn.ModuleList()
        mlp_size  = 0
        mlp_flops = 0
        
        for i in range(len(mlp_dims) - 1):
            dim_in  = int(mlp_dims[i])
            dim_out = int(mlp_dims[i + 1])
            layer   = nn.Linear(dim_in, dim_out, bias = True)
            
            mu      = 0.0
            sigma_w = np.sqrt(2 / (dim_in + dim_out))
            sigma_b = np.sqrt(1 / dim_out)
            w       = ra.normal(mu, sigma_w, size=(dim_out, dim_in)).astype(precision)
            b       = ra.normal(mu, sigma_b, size=dim_out).astype(precision)
            
            layer.weight.data = torch.tensor(w, requires_grad = True)
            layer.bias.data   = torch.tensor(b, requires_grad = True)

            layers.append(layer)
            
            if en_sigmoid and i == len(mlp_dims) - 2:
                layers.append(nn.Sigmoid())
            else:
                layers.append(nn.ReLU())
                
            mlp_size += (dim_in + 1) * dim_out * bytes_per_param
            mlp_flops += 2 * dim_in * dim_out
                
        return nn.Sequential(*layers), mlp_size, mlp_flops
            
    # Create Embedding Tables
    def create_embedding_tables(
        self,
        emb_dim,
        table_sizes,
        precision=np.float32
    ):
        if precision == np.float32:
            bytes_per_param = 4
        elif precision == np.float16:
            bytes_per_param = 2
        else:
            sys.exit('Unsupported Datatype')
        
        embedding_tables = nn.ModuleList()
        
        for size in table_sizes:
            table = nn.Embedding(size, emb_dim)
            
            w = ra.uniform(
                low  = -np.sqrt(1/size),
                high = np.sqrt(1/size),
                size = (size, emb_dim)
            ).astype(precision)
            
            table.weight.data = torch.tensor(w, requires_grad=True)
            
            embedding_tables.append(table)
        
        tables_size = emb_dim * np.sum(table_sizes) * bytes_per_param
            
        return embedding_tables, tables_size
    
    def apply_mlp(self, inputs, mlp):
        return mlp(inputs)
    
    def apply_embedding_tables(self, offsets, indices, tables):
        embeddings = []
        for i in range(len(indices)):
            index_group_batch  = indices[i]
            offset_group_batch = offsets # offsets[i]
            table              = tables[i]
            
            # Manual 'Bag' implementation of EmbeddingBag
            embedding = table(index_group_batch)
            embedding_reduced = []
            
            # Manual implementation of 'Bag' of 'EmbeddingBag'
            for i in range(len(offset_group_batch)):
                start_idx = offset_group_batch[i]
                if i == len(offset_group_batch) - 1:
                    end_idx   = len(index_group_batch) # for last bag, reduce to end of sequence
                else:
                    end_idx   = offset_group_batch[i+1]
                embedding_reduced.append(torch.sum(embedding[start_idx:end_idx, :], dim=0))
            embedding_reduced = torch.stack(embedding_reduced)
            
            embeddings.append(embedding_reduced)
            
        return embeddings
    
    def __init__(
        self,
        dense_dim,
        mlp_bot_dims,
        mlp_top_dims,
        emb_dim,
        table_sizes,
        table_sizes_1,
        table_sizes_2,
        table_sizes_3,
        table_sizes_4,
        num_lookups,
        precision,
        constant_offsets,
    ):
        super(DLRM_MULTI_IPU, self).__init__()
        
        self.dense_dim        = dense_dim
        self.mlp_bot_dims     = mlp_bot_dims
        self.mlp_top_dims     = mlp_top_dims
        self.emb_dim          = emb_dim
        self.table_sizes      = table_sizes
        self.table_sizes_1    = table_sizes_1,
        self.table_sizes_2    = table_sizes_2,
        self.table_sizes_3    = table_sizes_3,
        self.table_sizes_4    = table_sizes_4,
        self.num_lookups      = num_lookups
        self.precision        = precision
        self.constant_offsets = constant_offsets
        
        # Modify MLP Top Architecture to account for feature interaction
        self.mlp_top_dims = [self.emb_dim * (1 + len(self.table_sizes))] + self.mlp_top_dims
        
        # Make MLPs (MLP Bot, MLP Top)
        self.mlp_bot, self.mlp_bot_size, self.mlp_bot_flops = self.create_mlp(self.mlp_bot_dims, en_sigmoid=False, precision=self.precision)
        self.mlp_top, self.mlp_top_size, self.mlp_top_flops = self.create_mlp(self.mlp_top_dims, en_sigmoid=True, precision=self.precision)
        # Loss Function
        self.loss_fn = nn.BCELoss(reduction="mean")
        
        # Make Embedding Tables
        self.embedding_tables_1, self.tables_size_1 = self.create_embedding_tables(self.emb_dim, self.table_sizes_1, self.precision)
        self.tables_flops_1 = len(self.table_sizes_1) * self.emb_dim * (self.num_lookups - 1)

        self.embedding_tables_2, self.tables_size_2 = self.create_embedding_tables(self.emb_dim, self.table_sizes_2, self.precision)
        self.tables_flops_2 = len(self.table_sizes_2) * self.emb_dim * (self.num_lookups - 1)

        self.embedding_tables_3, self.tables_size_3 = self.create_embedding_tables(self.emb_dim, self.table_sizes_3, self.precision)
        self.tables_flops_3 = len(self.table_sizes_3) * self.emb_dim * (self.num_lookups - 1)

        self.embedding_tables_4, self.tables_size_4 = self.create_embedding_tables(self.emb_dim, self.table_sizes_4, self.precision)
        self.tables_flops_4 = len(self.table_sizes_4) * self.emb_dim * (self.num_lookups - 1)

        self.tables_size = self.tables_size_1 + self.tables_size_2 + self.tables_size_3 + self.tables_size_4
        self.tables_flops = self.tables_flops_1 + self.tables_flops_2 + self.tables_flops_3 + self.tables_flops_4
        
        # Summary Print Messages
        self.model_size = self.mlp_bot_size + self.mlp_top_size + self.tables_size
        self.model_flops = self.mlp_bot_flops + self.mlp_top_flops + self.tables_flops

        print('Model Statistics')
        print('DataType: {}'.format(self.precision))

        print('========== Size ==========')
        print('- MLP Bot Size: {:.3f} MB'.format(self.mlp_bot_size/1e6))
        print('- MLP Top Size: {:.3f} MB'.format(self.mlp_top_size/1e6))
        print('- Embedding Tables Size: {:.3f} MB'.format(self.tables_size/1e6))
        print('- Model Size: {:.3f} MB'.format(self.model_size/1e6))
        print('\t({:.3f}% MLP, {:.3f}% Table)'.format(100*(1-self.tables_size/self.model_size), 100*self.tables_size/self.model_size))      
        
        print('========== FLOPs ==========')
        print('- MLP Bot FLOPs: {:.3f} MFLOPs'.format(self.mlp_bot_flops/1e6))
        print('- MLP Top FLOPs: {:.3f} MFLOPs'.format(self.mlp_top_flops/1e6))
        print('- Embeddings Reduction FLOPs: {:.3f} MFLOPs'.format(self.tables_flops/1e6))
        print('- Model FLOPs: {:.3f} MFLOPs'.format(self.model_flops/1e6))
        print('\t({:.3f}% Bot MLP, {:.3f}% Top MLP, {:.3f}% Embeddings)'.format(100*self.mlp_bot_flops/self.model_flops, 100*self.mlp_top_flops/self.model_flops, 100*self.tables_flops/self.model_flops))
        
    def forward(self, x_dense, x_indices, labels=None):        
        poptorch.Block.useAutoId()

        with poptorch.Block(ipu_id=0):
            # Preprocess Lookup IDs
            x_indices = torch.transpose(x_indices, 0, 1)
            # Custom Sharding for Criteo Kaggle
            x_indices_1 = x_indices[:-3]
            x_indices_2 = x_indices[-3].reshape(1,-1)
            x_indices_3 = x_indices[-2].reshape(1,-1)
            x_indices_4 = x_indices[-1].reshape(1,-1)

            embeddings_2 = self.apply_embedding_tables(self.constant_offsets, x_indices_2, self.embedding_tables_2)
        
        with poptorch.Block(ipu_id=1):
            embeddings_3 = self.apply_embedding_tables(self.constant_offsets, x_indices_3, self.embedding_tables_3)
        
        with poptorch.Block(ipu_id=2):
            embeddings_4 = self.apply_embedding_tables(self.constant_offsets, x_indices_4, self.embedding_tables_4)   
        
        with poptorch.Block(ipu_id=3):
            embeddings_1 = self.apply_embedding_tables(self.constant_offsets, x_indices_1, self.embedding_tables_1)
        
            mlp_bot_out = self.apply_mlp(x_dense, self.mlp_bot)
            fea_int_out = torch.cat([mlp_bot_out] + embeddings_1 + embeddings_2 + embeddings_3 + embeddings_4, dim=1)
            mlp_top_out = self.apply_mlp(fea_int_out, self.mlp_top)

            if self.training:
                return mlp_top_out, self.loss_fn(mlp_top_out, labels)
        
        return mlp_top_out