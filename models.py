import torch
import torch.nn as nn
from RGCN import RGCNNet
from torch_geometric.nn import MessagePassing
from torch_geometric.data import Data, Batch
from torch_geometric.nn.glob import global_max_pool, global_mean_pool
from torch_scatter import scatter_sum
from torch_sparse import SparseTensor, spmm
from sklearn.metrics import roc_auc_score

import pdb
import numpy as np
from torch_geometric.utils import subgraph
import os
import pickle
import json
import torch.nn.functional as F



import math
from sklearn.metrics.pairwise import cosine_similarity
SYNTHETIC = False

def load_embed(datapath, emb_path, dataset="NELL", embed_model = "ComplEx", use_ours = True, load_ent = False, bidir=False, inductive = False):
    tail = ""
    if inductive:
        tail += "_inductive"
    rel2id = json.load(open(datapath + f'/relation2id{tail}.json'))
    ent2id = json.load(open(datapath + f'/entity2id{tail}.json'))
    
    if inductive:
        try:
            inductive_ndoes= json.load(open(datapath + f'/inductive_nodes.json'))
        except:
            print("inductive_ndoes not found")
            inductive_ndoes = []
    else:
        inductive_ndoes = []

    if  not use_ours:    
        print("use original emb", embed_model)            
        assert dataset == "NELL" and not inductive
        theirs_rel2id = json.load(open(emb_path + f'/{dataset}/relation2ids'))
        theirs_ent2id = json.load(open(emb_path + f'/{dataset}/ent2ids'))

        print ("loading pre-trained embedding...")
        if embed_model in ['DistMult', 'TransE', 'ComplEx', 'RESCAL']:
                    
            rel_embed = np.loadtxt(emb_path + f'/{dataset}/embed/relation2vec.' + embed_model)
            ent_embed = np.loadtxt(emb_path + f'/{dataset}/embed/entity2vec.' + embed_model)     

            if embed_model == 'ComplEx':
                # normalize the complex embeddings
                ent_mean = np.mean(ent_embed, axis=1, keepdims=True)
                ent_std = np.std(ent_embed, axis=1, keepdims=True)
                rel_mean = np.mean(rel_embed, axis=1, keepdims=True)
                rel_std = np.std(rel_embed, axis=1, keepdims=True)
                eps = 1e-3
                ent_embed = (ent_embed - ent_mean) / (ent_std + eps)
                rel_embed = (rel_embed - rel_mean) / (rel_std + eps)

            assert ent_embed.shape[0] == len(ent2id.keys())
            if not load_ent:   
                embeddings = []
                id2rel = {v: k for k, v in rel2id.items()}

                for key_id in range(len(rel2id.keys())):
                    key = id2rel[key_id]
                    if key not in ['','OOV']:
                        embeddings.append(list(rel_embed[theirs_rel2id[key],:]))

                # just add a random extra one        
                embeddings.append(list(rel_embed[0,:]))        
                return np.array(embeddings)

            else:  
                embeddings = []
                
                id2ent = {v: k for k, v in ent2id.items()}

                for key_id in range(len(ent2id.keys())):
                    key = id2ent[key_id]
                    if key not in ['', 'OOV']:
                        if key in inductive_ndoes:
                            embeddings.append(np.random.normal(size = ent_embed.shape[1]))
                        else:
                            embeddings.append(list(ent_embed[theirs_ent2id[key],:]))
                            
                return np.array(embeddings)
    else:
        print("use ours emb")            

        prefix = f'{dataset}-fs'
        if bidir:
            prefix += '-bidir'
        if inductive:
            prefix += '-ind'    
        theirs_rel2id = pickle.load(open(emb_path + f'/{prefix}/rel2id.pkl', 'rb'))
        theirs_ent2id = pickle.load(open(emb_path + f'/{prefix}/ent2id.pkl', 'rb'))

        print ("loading ours pre-trained embedding...")
        if embed_model == 'TransE':
            ckpt = torch.load(emb_path + f'/{prefix}/checkpoint', map_location='cpu')
        elif embed_model == 'ComplEx':
            ckpt = torch.load(emb_path + f'/{prefix}/complex_checkpoint', map_location='cpu')
        
        if not load_ent:
            rel_embed = ckpt['model_state_dict']['relation_embedding.embedding']
            embeddings = []
            id2rel = {v: k for k, v in rel2id.items()}

            for key_id in range(len(rel2id.keys())):
                key = id2rel[key_id]
                if key not in ['','OOV']:
                    embeddings.append(list(rel_embed[theirs_rel2id[key],:]))

            # just add a random extra one        
            embeddings.append(list(rel_embed[0,:]))        
            embeddings = np.array(embeddings)

            return embeddings
    
        if load_ent:
            ent_embed = ckpt['model_state_dict']['entity_embedding.embedding']
            node_embeddings = []
            id2ent = {v: k for k, v in ent2id.items()}

            for key_id in range(len(ent2id.keys())):
                key = id2ent[key_id]
                if key not in ['','OOV']:
                    if key in inductive_ndoes:
                        node_embeddings.append(np.random.normal(size = ent_embed.shape[1]))
                    else:
                        node_embeddings.append(list(ent_embed[theirs_ent2id[key],:]))


            node_embeddings = np.array(node_embeddings)
            return node_embeddings

def clear_masks(model):
    """ clear the edge weights to None """
    for module in model.modules():
        if isinstance(module, MessagePassing):
            module.__explain__ = False
            module.__edge_mask__ = None

def set_masks(model, edgemask):
    for module in model.modules():
        if isinstance(module, MessagePassing):
            module.__explain__ = True
            module.__edge_mask__ = edgemask

class GNNEmbeddingLearner(nn.Module):
    def __init__(self, bn, prototype_dim, emb_dim, num_prototypes = 2, hidden_dim=128, use_subgraph = False, num_rels_bg=101, num_nodes = 1000, use_node_emb = False, debug=False, logging_dir=None):
        super(GNNEmbeddingLearner, self).__init__()
        self.edge_embedding = nn.Embedding(num_rels_bg + 1, emb_dim)
        
        self.node_embedding = nn.Embedding(num_nodes, emb_dim)
        self.prototype_dim = prototype_dim
        self.rgcn = RGCNNet(emb_dim = emb_dim, input_dim = emb_dim, edge_embedding = self.edge_embedding, node_embedding = self.node_embedding, num_rels_bg = num_rels_bg, use_node_emb = False, use_node_emb_end = use_node_emb, use_noid_node_emb = SYNTHETIC)

        self.epsilon = 1e-15
        self.debug = debug
        self.last_layer = nn.Linear(num_prototypes, 1)  
        self.egnn = RGCNNet(emb_dim =emb_dim, input_dim = emb_dim + prototype_dim, num_rels_bg = num_rels_bg, edge_embedding = self.edge_embedding, node_embedding = self.node_embedding, latent_dim = [hidden_dim]*3, use_node_emb = False, use_noid_node_emb = SYNTHETIC)
        self.Linlayer1=nn.Linear(hidden_dim , 64)
        self.Linlayer2=nn.Linear(64, 1)
        self.BN=nn.BatchNorm1d(64)
        #for FB15K-237, we add a BN layer
        self.egnn_post_layers = nn.Sequential(
            self.Linlayer1, 
            nn.ReLU(),
            self.BN,
            self.Linlayer2)
        self.egnn_post_layers2 = nn.Sequential(
            self.Linlayer1, 
            nn.ReLU(),
            self.Linlayer2)
        self.csg_gnn = RGCNNet(emb_dim =emb_dim, input_dim = emb_dim + prototype_dim, num_rels_bg = num_rels_bg, edge_embedding = self.edge_embedding, node_embedding = self.node_embedding, latent_dim = [128]* 10, ffn=True)
        
        self.csg_gnn_post_layers = nn.Sequential(
            nn.Linear(128 , 64), 
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Linear(64, 1))
        self.empty_idx = num_rels_bg
        self.use_subgraph = use_subgraph
        self.bn=bn
    def masked_embedding(self, graphs, edgemask, size_loss_beta = 0):        

        clear_masks(self.rgcn)
        set_masks(self.rgcn, edgemask)
        
        emb, _, _ = self.rgcn(graphs, edgemask)
        clear_masks(self.rgcn)
        
        size_loss = torch.sum(edgemask)
        # entropy
        mask_ent = - edgemask * torch.log(edgemask + self.epsilon) - (1 - edgemask) * torch.log(1 - edgemask + self.epsilon)
        mask_ent_loss = torch.sum(mask_ent)

        extra_loss = size_loss* size_loss_beta + mask_ent_loss 
        return emb, extra_loss, edgemask

    def gen_mask_gnn(self, graphs, prototype):
        
        prototype = prototype[:, :self.prototype_dim] # in case it contains extra node emb
        row, col = graphs.edge_index.long()
        edge_batch = graphs.batch[row]
        #print(graphs.batch)
        _, _, edge_attr = self.egnn(graphs, extra_cond = prototype[edge_batch] )
        #print("edge_attr",edge_attr.shape)
        #if(edge_attr.shape[0]>1):
        #    h = self.egnn_post_layers(edge_attr)
        #    #print("h",h.shape)
        #elif (edge_attr.shape[0]==1):
        #    edge_attr=torch.cat([edge_attr,edge_attr],dim=0)
        #    h = self.egnn_post_layers(edge_attr)
        #    #print("h",h.shape)
        #    h=h[0,:].unsqueeze(0)
        #else:
        #    h = self.egnn_post_layers2(edge_attr)
        #print(edge_attr.shape)
        #exit()
        if self.bn:
            h = self.egnn_post_layers(edge_attr)
        else:
            h = self.egnn_post_layers2(edge_attr)
        
        h = h.sigmoid().reshape(-1)
        return h

    def get_masked_graph_embedding(self, graphs, prototype, size_loss_beta = 0):
        edgemask = self.gen_mask_gnn(graphs, prototype)

        emb, extra_loss, edgemask = self.masked_embedding(graphs, edgemask, size_loss_beta)
        return emb, extra_loss, edgemask
        
        
        
class weightmodel(nn.Module):
    #the model for weight calculation
    def __init__(self, dataset, parameter):
        super(weightmodel, self).__init__()
        self.device = parameter['device']
        self.beta = parameter['beta']
        self.dropout_p = parameter['dropout_p']
        self.margin = parameter['margin']
        self.abla = parameter['ablation']
        self.use_subgraph = parameter['use_subgraph']
        self.support_only = parameter['support_only']
        self.opt_mask = parameter['opt_mask']
        self.use_atten = parameter['use_atten']
        self.egnn_only = parameter['egnn_only']

        self.use_ground_truth = parameter['use_ground_truth']
        self.use_full_mask_rule = parameter['use_full_mask_rule']
        self.use_full_mask_query = parameter['use_full_mask_query']
        self.joint_train_mask = parameter['joint_train_mask']
        self.verbose = parameter['verbose']
        self.pdb_mode = parameter['pdb_mode']
        self.debug = parameter['debug']
        self.extra_loss_beta = parameter['extra_loss_beta']
        self.loss_mode = parameter['loss_mode']
        self.niters = parameter['niters']
        self.geo = parameter['geo']
        self.pool_mode = parameter['pool_mode']
        self.opt_mode = parameter['opt_mode']
        self.emb_path = parameter['emb_path']
        self.emb_dim = parameter['emb_dim']
        self.hidden_dim = parameter['hidden_dim']
        self.full_kg = dataset.graph
        self.no_margin = parameter['no_margin']
        
        self.num_prototypes_per_class = 1
        self.prototype_dim = self.hidden_dim * 3

        if dataset.dataset=="FB15K-237":
            self.bn=True
        else:
            self.bn=False
        self.embedding_learner = GNNEmbeddingLearner(self.bn, self.prototype_dim , self.emb_dim, self.num_prototypes_per_class, self.hidden_dim, self.use_subgraph, num_rels_bg = dataset.num_rels_bg, num_nodes = dataset.num_nodes_bg, use_node_emb = parameter['use_pretrain_node_emb'] or parameter['use_rnd_node_emb'], debug=self.debug)
        


            
class GNNEmbeddingmix(nn.Module):
    def __init__(self, input_dim, output_dim):
        super().__init__()        
        self.emb_dim=6
        self.edge_mlp = nn.Linear(input_dim * 3 + 12, output_dim)
        nn.init.xavier_uniform_(self.edge_mlp.weight)
 
    def forward(self,change,is_support,head_idxs,tail_idxs,x, edge_index, edge_attr_emb, mask ,num_nodes, support_tail=None, support_head=None):
        row = edge_index[0].long()
        col = edge_index[1].long()
        
        #edge aggregations
        node_rep = scatter_sum(edge_attr_emb * mask.unsqueeze(-1), col, dim=0, dim_size=num_nodes)
        node_rep = node_rep/(scatter_sum(mask.unsqueeze(-1), col, dim=0, dim_size=num_nodes) + 1)
        
        node_rep = torch.cat([node_rep, x], 1)
        
        if change and is_support:
            #average the tail embedding of all support graphs
            #node_rep[head_idxs,:]=node_rep[head_idxs,:].mean(dim=0)
            node_rep[tail_idxs,:]=node_rep[tail_idxs,:].mean(dim=0)
            
        if change and (not is_support):
            #adapt the support_tail to query graph structure
            #node_rep[head_idxs,:]=(support_head+node_rep[head_idxs,:])/2
            node_rep[tail_idxs,:]=0.1*support_tail+0.9*node_rep[tail_idxs,:]
            #node_rep[tail_idxs,:]=support_tail
        
        #update edge embedding
        edge_rep = torch.cat([node_rep[row], node_rep[col], edge_attr_emb], -1)
        edge_rep = self.edge_mlp(edge_rep)
        
        return  node_rep, edge_rep


class COMPASSWeightModel(nn.Module):
    def __init__(self, dataset, parameter):
        super(COMPASSWeightModel, self).__init__()
      
        # Additional MLPs for attention/weighting
        self.edge_attention_mlp = nn.Sequential(
            nn.Linear(self.hidden_dim, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, 1)
        )
        self.graph_weight_mlp = nn.Sequential(
            nn.Linear(self.prototype_dim, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, 1)
        )

    def forward(self, graph):
        # Initialization (same as SAFER)
        b_e = torch.cat([graph.edge_features, g_all.expand(graph.num_edges, -1)], dim=1)
        
        # Iterative Edge Attention (L layers)
        for _ in range(self.niters):
            # 1. Node Embedding with Attention
            alpha_e = torch.sigmoid(self.edge_attention_mlp(b_e))  # [E,1]
            alpha_e = scatter_softmax(alpha_e, graph.edge_index[1])  # Normalize per node
            
            b_v = scatter_add(alpha_e * b_e, graph.edge_index[1], dim=0)  # [N,d]
            b_v = b_v / (1 + graph.node_degrees.unsqueeze(1))  # Degree normalization
            
            # 2. Role Augmentation
            r_v = torch.cat([
                b_v,
                (graph.nodes == graph.heads).float().unsqueeze(1),
                (graph.nodes == graph.tails).float().unsqueeze(1)
            ], dim=1)
            
            # 3. Edge Update
            sender_roles = r_v[graph.edge_index[0]]
            receiver_roles = r_v[graph.edge_index[1]]
            b_e = self.edge_update_mlp(torch.cat([sender_roles, receiver_roles, b_e], dim=1))
        
        # Graph Embedding (MaxPool + Head/Tail)
        g_G = torch.cat([
            global_max_pool(b_v, graph.batch),
            b_v[graph.heads],
            b_v[graph.tails]
        ], dim=1)
        
        # Adaptive Support Graph Weighting
        if self.training:  # Only for support graphs
            w_k = torch.softmax(self.graph_weight_mlp(g_G), dim=0)
            g_all = (w_k * g_G).sum(dim=0, keepdim=True)
        else:
            g_all = g_G.mean(dim=0, keepdim=True)
        
        # Final Edge Weights
        w_e = torch.sigmoid(self.edge_weight_layer(b_e))
        
        return w_e, g_all

class mix_model2(nn.Module):
    def __init__(self, dataset, parameter):
        super(mix_model2, self).__init__()
        #for p in self.parameters():
        #    p.requires_grad=False
        self.device = parameter['device']
        self.beta = parameter['beta']
        self.dropout_p = parameter['dropout_p']
        self.margin = parameter['margin']
        self.abla = parameter['ablation']
        self.use_subgraph = parameter['use_subgraph']
        self.support_only = parameter['support_only']
        self.opt_mask = parameter['opt_mask']
        self.use_atten = parameter['use_atten']
        self.egnn_only = parameter['egnn_only']

        self.use_ground_truth = parameter['use_ground_truth']
        self.use_full_mask_rule = parameter['use_full_mask_rule']
        self.use_full_mask_query = parameter['use_full_mask_query']
        self.joint_train_mask = parameter['joint_train_mask']
        self.verbose = parameter['verbose']
        self.pdb_mode = parameter['pdb_mode']
        self.debug = parameter['debug']
        self.extra_loss_beta = parameter['extra_loss_beta']
        self.loss_mode = parameter['loss_mode']
        self.niters = parameter['niters']
        self.geo = parameter['geo']
        self.pool_mode = parameter['pool_mode']
        self.opt_mode = parameter['opt_mode']
        
        self.emb_path = parameter['emb_path']
        self.emb_dim = parameter['emb_dim']
        self.hidden_dim = parameter['hidden_dim']
        self.full_kg = dataset.graph
        self.no_margin = parameter['no_margin']
        
        self.num_prototypes_per_class = 1
        self.prototype_dim = self.hidden_dim * 3

        self.num_rels_bg = dataset.num_rels_bg
        self.num_nodes = dataset.num_nodes_bg
        self.edge_embedding = nn.Embedding(self.num_rels_bg + 1, self.emb_dim)
        
        self.node_embedding = nn.Embedding(self.num_nodes, self.emb_dim)
        self.dataset=dataset.dataset
        #if self.dataset=="NELL":
        #    self.dataset="None"
        use_ours = True
        if dataset.dataset in ["NELL", "Wiki"] and not parameter['our_emb'] and not parameter['inductive']:
            use_ours = False
            
        
        if parameter['use_pretrain_edge_emb']:
            rel_embeddings =  load_embed(os.path.join(dataset.root, dataset.dataset), self.emb_path, dataset.dataset, use_ours = use_ours, embed_model=parameter["embed_model"], bidir = parameter['bidir'], inductive = parameter['inductive'])  

            print ("loading into edge embedding...")
            self.edge_embedding.weight.data.copy_(torch.from_numpy(rel_embeddings))
            #emb_matching=nn.Embedding(1,self.emb_dim)
            #with torch.no_grad():
            #    self.edge_embedding.weight[-1,:]=0
        if parameter['use_pretrain_node_emb']:
            node_embeddings =  load_embed(os.path.join(dataset.root, dataset.dataset), self.emb_path, dataset.dataset, load_ent = True, use_ours = use_ours, embed_model=parameter["embed_model"], bidir = parameter['bidir'], inductive = parameter['inductive'])   
####    
            print ("loading into node embedding...")
            self.node_embedding.weight.data.copy_(torch.from_numpy(node_embeddings))
        
        
        self.latent_dim = [128, 128, 128, 128]
        #self.latent_dim = [256,256,256,256,256,256]
        self.num_gnn_layers = len(self.latent_dim)
        self.num_layers=len(self.latent_dim)
        self.embedding_mix= nn.ModuleList()
        self.embedding_mix.append(GNNEmbeddingmix(int(self.emb_dim), self.latent_dim[0]))
        for i in range(1, self.num_layers):
            self.embedding_mix.append(GNNEmbeddingmix(self.latent_dim[i - 1], self.latent_dim[i]))

        self.gnn_non_linear=nn.ReLU()
        #self.end=nn.Linear((self.latent_dim[-1]+6)*3*4,2)
        self.head_tail=nn.Linear(self.emb_dim,self.emb_dim)
        self.weight=weightmodel(dataset,parameter)
        #if parameter['prev_state_dir'] is not None:
        #    print('loading pretrained CSR model into model2')
        #    prev_ckpt = torch.load(parameter['prev_state_dir'], map_location='cpu')
        #    self.csr.load_state_dict(prev_ckpt, strict=False)
        #for p in self.csr.parameters():
        #    p.requires_grad=False
        
        #for name, param in self.model1.named_parameters:
        #    print(name, param.requires_grad)
        #self.end.to(self.device)
        self.edge_embedding.to(self.device)
        self.node_embedding.to(self.device)
        self.weight.to(self.device)
        self.head_tail.to(self.device)
        for i in range(self.num_gnn_layers):
            self.embedding_mix[i].to(self.device)
    
    def forward(self, task, iseval=False, is_eval_loss = False, curr_rel='', trial = None, best_params = None):
        support, support_subgraphs, support_negative, support_negative_subgraphs, query, query_subgraphs, negative, negative_subgraphs = task
        #print(support)
        #print(query)
        #print(negative)
        #exit(0)
        if self.dataset=="FB15K-237":
            use_bn=True
        else:
            use_bn=False
        cos=nn.CosineSimilarity(dim=1, eps=1e-6)
        few=len(support_subgraphs)
        #few=3
        num_q=len(query_subgraphs)
        num_n=len(negative_subgraphs)
        print(few,num_q,num_n)
        
        #note the number of nodes
        support_subgraphs_mix=Batch.from_data_list(support_subgraphs).to(self.device)
        batch=support_subgraphs_mix.batch
        batch_num_nodes = scatter_sum(torch.ones(batch.shape).to(self.device), batch)     
        head_idxs_support = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes[:-1]]), 0).long()
        tail_idxs_support = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes[:-1]]), 0).long() + 1
        
        query_subgraphs_mix=Batch.from_data_list(query_subgraphs).to(self.device)
        batch_query=query_subgraphs_mix.batch
        batch_num_nodes_query = scatter_sum(torch.ones(batch_query.shape).to(self.device), batch_query)
        head_idxs_query_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_query[:-1]]), 0).long()
        tail_idxs_query_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_query[:-1]]), 0).long() + 1
           
            
        negative_subgraphs_mix=Batch.from_data_list(negative_subgraphs).to(self.device)
        batch_negative=negative_subgraphs_mix.batch
        batch_num_nodes_negative = scatter_sum(torch.ones(batch_negative.shape).to(self.device), batch_negative)       
        head_idxs_negative_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_negative[:-1]]), 0).long()
        tail_idxs_negative_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_negative[:-1]]), 0).long() + 1
        
        support_node_embedding = self.node_embedding(support_subgraphs_mix.x_id)
        query_node_embedding = self.node_embedding(query_subgraphs_mix.x_id)
        negative_node_embedding = self.node_embedding(negative_subgraphs_mix.x_id)
        
        
        #new emb in 3 supports
        #first step of weight calculation
        graph_emb, _, _ = self.weight.embedding_learner.rgcn(support_subgraphs_mix)
        
        graph_emb = graph_emb.reshape(few, -1)
        #graph_emb = graph_emb[:1167]  # Reduce the number of elements to match the desired shape
        #graph_emb = graph_emb.reshape(3, 389)  # Now this should work
        total_elements = graph_emb.numel()
        #print(f"Total elements: {total_elements}") #1168



        #print("Original graph_emb shape:", graph_emb.shape) #torch.Size([2, 584])
        #print("First few values of graph_emb:\n", graph_emb[:5])
        '''
        First few values of graph_emb:
 tensor([[ 0.1092,  0.0000,  1.9482,  ...,  1.0140, -1.6853, -1.3691],
        [ 0.1415,  0.0000,  1.8504,  ...,  1.4751,  0.1444, -1.1313]],
       device='cuda:0', grad_fn=<SliceBackward0>)
        '''
###
        graph_emb_permute = graph_emb.clone()
        indices = torch.LongTensor([1, 0]).to(graph_emb_permute.device) 
        #indices = torch.LongTensor([1, 2, 0]).to(graph_emb_permute.device)
        #indices = torch.LongTensor([1, 2, 0])
        #print("Indices before selection:", indices)
        '''
        Indices before selection: tensor([1, 0], device='cuda:0')
        '''
        
        
        max_index = graph_emb_permute.size(0) - 1  # Maximum valid index
        indices = torch.clamp(indices, min=0, max=max_index)  # Ensure indices are within bounds
        
        #print("Clamped Indices:", indices)
        '''
        Clamped Indices: tensor([1, 0], device='cuda:0')
tensor([[ 0.1415,  0.0000,  1.8504,  ...,  1.4751,  0.1444, -1.1313],
        [ 0.1092,  0.0000,  1.9482,  ...,  1.0140, -1.6853, -1.3691]],
       device='cuda:0', grad_fn=<IndexSelectBackward0>)
        '''
        
        graph_emb_permute = torch.index_select(graph_emb_permute, 0, indices)
        #print(graph_emb_permute)

        #graph_emb_permute = torch.index_select(graph_emb_permute, 0, torch.LongTensor([1,2,0]).to(graph_emb_permute.device))
        '''
        print("graph_emb_permute.shape:", graph_emb_permute.shape)
        print("Indices being used:", torch.LongTensor([0,1]))
        #print("Indices being used:", torch.LongTensor([1, 2, 0]))
        print("graph_emb_permute shape:", graph_emb_permute.shape)
        print("indices shape:", indices.shape)
        print("indices max:", indices.max().item())
        print("indices min:", indices.min().item())
        '''
        
        '''
        graph_emb_permute.shape: torch.Size([2, 584])
       Indices being used: tensor([0, 1])
	graph_emb_permute shape: torch.Size([2, 584])
	indices shape: torch.Size([2])
	indices max: 1
	indices min: 0
        '''

        
        graph_emb_permute = graph_emb_permute.reshape(few, -1)
###
        _, loss, edgemask1 = self.weight.embedding_learner.get_masked_graph_embedding(support_subgraphs_mix, graph_emb_permute, size_loss_beta = 0)  
###
       #graph_emb_permute = graph_emb.clone()
       #graph_emb_permute = torch.index_select(graph_emb_permute, 0, torch.LongTensor([2,0,1]).to(graph_emb_permute.device))
        
        graph_emb_permute = graph_emb.clone()
        indices = torch.LongTensor([1, 0]).to(graph_emb_permute.device) 
        #indices = torch.LongTensor([1, 2, 0]).to(graph_emb_permute.device)
        graph_emb_permute = torch.index_select(graph_emb_permute, 0, indices)
        
        graph_emb_permute = graph_emb_permute.reshape(few, -1)
        _, loss, edgemask2 = self.weight.embedding_learner.get_masked_graph_embedding(support_subgraphs_mix, graph_emb_permute, size_loss_beta = 0)  
        
        
        graph_emb_permute = graph_emb.clone()
        #indices = torch.LongTensor([1, 2, 0]).to(graph_emb_permute.device)
        indices = torch.LongTensor([1, 0]).to(graph_emb_permute.device) 
        graph_emb_permute = torch.index_select(graph_emb_permute, 0, indices)
        
       #graph_emb_permute = graph_emb.clone()
       #graph_emb_permute = torch.index_select(graph_emb_permute, 0, torch.LongTensor([0,1,2]).to(graph_emb_permute.device))
        
        graph_emb_permute = graph_emb_permute.reshape(few, -1)
        _, loss, edgemask3 = self.weight.embedding_learner.get_masked_graph_embedding(support_subgraphs_mix, graph_emb_permute, size_loss_beta = 0)  
        
        edge_mask = torch.cat([edgemask1.unsqueeze(-1), edgemask2.unsqueeze(-1), edgemask3.unsqueeze(-1)],dim=-1).mean(dim=-1)
        graph_emb, extra_loss, _  = self.weight.embedding_learner.masked_embedding(support_subgraphs_mix, edge_mask, size_loss_beta = 0)
        
        graph_emb = graph_emb.reshape(few, -1)
        rel_q = torch.mean(graph_emb, 0).view(1, -1)
        
        rel_use=rel_q.expand(few,-1).reshape(few,rel_q.shape[1])
        
        _, loss, edgemask = self.weight.embedding_learner.get_masked_graph_embedding(support_subgraphs_mix, rel_use, size_loss_beta = 0)  
        #edgemask=torch.ones(len(edgemask)).to(self.device)
            
        #support adaptation
        edge_attr_support_emb=self.edge_embedding(support_subgraphs_mix.edge_attr.long())
        batch_support=support_subgraphs_mix.batch
        batch_num_nodes_support = scatter_sum(torch.ones(batch_support.shape).to(self.device), batch_support)  
        head_idxs_support = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_support[:-1]]), 0).long()
        tail_idxs_support = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_support[:-1]]), 0).long() + 1
        change=True
        change2=True
        is_support=True
        x=support_subgraphs_mix.x
        support_mix_tail_output=[]
        support_mix_head_output=[]
        for j in range(self.num_gnn_layers):
            if j==self.num_gnn_layers-1:
                node_rep, edge_attr_support_emb = self.embedding_mix[j](change,is_support,head_idxs_support,tail_idxs_support, x, support_subgraphs_mix.edge_index,  edge_attr_support_emb, edgemask ,int(sum(batch_num_nodes_support)))
            else:
                node_rep, edge_attr_support_emb = self.embedding_mix[j](change2,is_support,head_idxs_support,tail_idxs_support, x, support_subgraphs_mix.edge_index,  edge_attr_support_emb, edgemask ,int(sum(batch_num_nodes_support)))
            edge_attr_support_emb = self.gnn_non_linear(edge_attr_support_emb)
            support_mix_tail_output.append(node_rep[tail_idxs_support[0],:])
            #support_mix_head_output.append(node_rep[head_idxs_support[0],:])
        if self.dataset=="NELL":
            ht_support=self.head_tail((support_node_embedding[head_idxs_support,:]-support_node_embedding[tail_idxs_support,:]).mean(dim=0))
        
        
        #query adaptation for FB
        if use_bn==True:
            can_subgraphs_mix=Batch.from_data_list(query_subgraphs+negative_subgraphs).to(self.device)
            batch_can=can_subgraphs_mix.batch
            batch_num_nodes_can = scatter_sum(torch.ones(batch_can.shape).to(self.device), batch_can)       
            head_idxs_can_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_can[:-1]]), 0).long()
            tail_idxs_can_mix = torch.cumsum(torch.cat([torch.tensor([0]).to(self.device),batch_num_nodes_can[:-1]]), 0).long() + 1

            can_node_embedding = self.node_embedding(can_subgraphs_mix.x_id)
            
            
            is_support=False
            rel_use=rel_q.expand(num_q+num_n,-1).reshape(num_q+num_n,rel_q.shape[1])
            _, loss, edgemask = self.weight.embedding_learner.get_masked_graph_embedding(can_subgraphs_mix, rel_use, size_loss_beta = 0)  
            #ht_can=self.head_tail((can_node_embedding[tail_idxs_can_mix,:]-can_node_embedding[tail_idxs_can_mix,:]))
            #edgemask=torch.ones(len(edgemask)).to(self.device)

            edge_attr_can_emb=self.edge_embedding(can_subgraphs_mix.edge_attr.long())
            x=can_subgraphs_mix.x

            change=False
            for j in range(self.num_gnn_layers):
                node_rep, edge_attr_can_emb = self.embedding_mix[j](change,is_support,head_idxs_can_mix,tail_idxs_can_mix, x, can_subgraphs_mix.edge_index,  edge_attr_can_emb, edgemask ,int(sum(batch_num_nodes_can)),support_mix_tail_output[j])#,support_mix_head_output[j])
                edge_attr_can_emb = self.gnn_non_linear(edge_attr_can_emb)              

            tail_emb_can=torch.index_select(node_rep,0,tail_idxs_can_mix.long())
            pooled = []
            pooled.append(tail_emb_can)
            #pooled1=[]
            #pooled1.append(tail_emb_can)
            #pooled1.append(can_node_embedding[tail_idxs_can_mix,:])
            #graph_emb_can1 = torch.cat(pooled1, dim=-1)
            pooled.append(can_node_embedding[tail_idxs_can_mix,:])
            #pooled.append(ht_can)
            graph_emb_can = torch.cat(pooled, dim=-1)


            edge_attr_can_emb=self.edge_embedding(can_subgraphs_mix.edge_attr.long())
            change=True
            for j in range(self.num_gnn_layers):
                node_rep, edge_attr_can_emb = self.embedding_mix[j](change,is_support,head_idxs_can_mix,tail_idxs_can_mix, x, can_subgraphs_mix.edge_index,  edge_attr_can_emb, edgemask ,int(sum(batch_num_nodes_can)),support_mix_tail_output[j])#,support_mix_head_output[j])
                edge_attr_can_emb = self.gnn_non_linear(edge_attr_can_emb)               

            tail_emb_can=torch.index_select(node_rep,0,tail_idxs_can_mix.long())
            pooled = []
            pooled.append(tail_emb_can)
            #pooled1=[]
            #pooled1.append(support_mix_tail_output[-1].unsqueeze(0).expand(num_q+num_n,-1))
            #pooled1.append(support_node_embedding[tail_idxs_support,:].mean(dim=0).unsqueeze(0).expand(num_q+num_n,-1))
            #graph_emb_can_change1 = torch.cat(pooled1, dim=-1)
            pooled.append(support_node_embedding[tail_idxs_support,:].mean(dim=0).unsqueeze(0).expand(num_q+num_n,-1))
            #pooled.append(ht_support.unsqueeze(0).expand(num_q+num_n,-1))
            graph_emb_can_change = torch.cat(pooled, dim=-1)

            can_ans_list=cos(graph_emb_can,graph_emb_can_change)#+0.5*cos(graph_emb_can1,graph_emb_can_change1)
            query_ans_list=can_ans_list[:num_q].reshape(num_q)
            negative_ans_list=can_ans_list[num_q:].reshape(num_n)
            query_sim_train=query_ans_list.mean(dim=0)
            negative_sim_train=negative_ans_list.mean(dim=0)
            return query_ans_list, negative_ans_list, query_sim_train, negative_sim_train
        
        
        #query adaptation for NELL and CN
        is_support=False
        #query
        rel_use=rel_q.expand(num_q,-1).reshape(num_q,rel_q.shape[1])
        _, loss, edgemask = self.weight.embedding_learner.get_masked_graph_embedding(query_subgraphs_mix, rel_use, size_loss_beta = 0)  
        if self.dataset=="NELL":
            ht_query=self.head_tail((query_node_embedding[head_idxs_query_mix,:]-query_node_embedding[tail_idxs_query_mix,:]))
        #edgemask=torch.ones(len(edgemask)).to(self.device)
        
        edge_attr_query_emb=self.edge_embedding(query_subgraphs_mix.edge_attr.long())
        x=query_subgraphs_mix.x
        
        change=False
        for j in range(self.num_gnn_layers):
            node_rep, edge_attr_query_emb = self.embedding_mix[j](change,is_support,head_idxs_query_mix,tail_idxs_query_mix, x, query_subgraphs_mix.edge_index,  edge_attr_query_emb, edgemask ,int(sum(batch_num_nodes_query)),support_mix_tail_output[j])
            edge_attr_query_emb = self.gnn_non_linear(edge_attr_query_emb)              
            
        tail_emb_query=torch.index_select(node_rep,0,tail_idxs_query_mix.long())
        pooled = []
        pooled.append(tail_emb_query)
        pooled.append(query_node_embedding[tail_idxs_query_mix,:])
        if self.dataset=="NELL":
            pooled.append(ht_query)
        graph_emb_query = torch.cat(pooled, dim=-1)


        edge_attr_query_emb=self.edge_embedding(query_subgraphs_mix.edge_attr.long())
        change=True
        for j in range(self.num_gnn_layers):
            node_rep, edge_attr_query_emb = self.embedding_mix[j](change,is_support,head_idxs_query_mix,tail_idxs_query_mix, x, query_subgraphs_mix.edge_index,  edge_attr_query_emb, edgemask ,int(sum(batch_num_nodes_query)),support_mix_tail_output[j])
            edge_attr_query_emb = self.gnn_non_linear(edge_attr_query_emb)              
        
        tail_emb_query=torch.index_select(node_rep,0,tail_idxs_query_mix.long())#.mean(dim=0)     
        pooled = []
        pooled.append(tail_emb_query)
        pooled.append(support_node_embedding[tail_idxs_support,:].mean(dim=0).unsqueeze(0).expand(num_q,-1))
        if self.dataset=="NELL":
            pooled.append(ht_support.unsqueeze(0).expand(num_q,-1))
        graph_emb_query_change = torch.cat(pooled, dim=-1)
        
        query_ans_list=cos(graph_emb_query,graph_emb_query_change)
        query_sim_train=query_ans_list.mean(dim=0)
        
        
        
        #negative
        rel_use=rel_q.expand(num_n,-1).reshape(num_n,rel_q.shape[1])
        _, loss, edgemask = self.weight.embedding_learner.get_masked_graph_embedding(negative_subgraphs_mix, rel_use, size_loss_beta = 0)  
        if self.dataset=="NELL":
            ht_negative=self.head_tail((negative_node_embedding[head_idxs_negative_mix,:]-negative_node_embedding[tail_idxs_negative_mix,:]))
        #edgemask=torch.ones(len(edgemask)).to(self.device)
        
        edge_attr_negative_emb=self.edge_embedding(negative_subgraphs_mix.edge_attr.long())
        x=negative_subgraphs_mix.x
        
        change=False
        for j in range(self.num_gnn_layers):
            node_rep, edge_attr_negative_emb = self.embedding_mix[j](change,is_support,head_idxs_negative_mix,tail_idxs_negative_mix, x, negative_subgraphs_mix.edge_index,  edge_attr_negative_emb, edgemask ,int(sum(batch_num_nodes_negative)),support_mix_tail_output[j])
            edge_attr_negative_emb = self.gnn_non_linear(edge_attr_negative_emb)              
            
        tail_emb_negative=torch.index_select(node_rep,0,tail_idxs_negative_mix.long())
        pooled = []
        pooled.append(tail_emb_negative)
        pooled.append(negative_node_embedding[tail_idxs_negative_mix,:])
        if self.dataset=="NELL":
            pooled.append(ht_negative)
        graph_emb_negative = torch.cat(pooled, dim=-1)


        edge_attr_negative_emb=self.edge_embedding(negative_subgraphs_mix.edge_attr.long())
        change=True
        for j in range(self.num_gnn_layers):
            node_rep, edge_attr_negative_emb = self.embedding_mix[j](change,is_support,head_idxs_negative_mix,tail_idxs_negative_mix, x, negative_subgraphs_mix.edge_index,  edge_attr_negative_emb, edgemask ,int(sum(batch_num_nodes_negative)),support_mix_tail_output[j])
            edge_attr_negative_emb = self.gnn_non_linear(edge_attr_negative_emb)              
        
        tail_emb_negative=torch.index_select(node_rep,0,tail_idxs_negative_mix.long())#.mean(dim=0)     
        pooled = []
        pooled.append(tail_emb_negative)
        pooled.append(support_node_embedding[tail_idxs_support,:].mean(dim=0).unsqueeze(0).expand(num_n,-1))
        if self.dataset=="NELL":
            pooled.append(ht_support.unsqueeze(0).expand(num_n,-1))
        graph_emb_negative_change = torch.cat(pooled, dim=-1)
        
        negative_ans_list=cos(graph_emb_negative,graph_emb_negative_change)
        negative_sim_train=negative_ans_list.mean(dim=0)
        
        
        
        return query_ans_list, negative_ans_list, query_sim_train, negative_sim_train
     
