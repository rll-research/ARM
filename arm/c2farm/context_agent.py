import copy
import os
from typing import List
import torch
import torch.nn as nn
from yarr.agents.agent import Agent, ActResult, ScalarSummary, \
    HistogramSummary, Summary
import numpy as np
import math
from abc import ABC, abstractmethod
from arm.models.utils import make_optimizer # tie the optimizer definition closely with embedding nets 
from arm import utils
from omegaconf import DictConfig
from einops import rearrange, reduce, repeat, parse_shape
import logging
from yarr.utils.multitask_rollout_generator import TASK_ID, VAR_ID # use this to get K_action

NAME = 'ContextEmbedderAgent'
CONTEXT_KEY = 'demo_sample'


class SequenceStrategy(ABC):

    @abstractmethod
    def apply(self, x):
        pass


class StackOnChannel(SequenceStrategy):

    def apply(self, x):
        # expect (B, T, C, ...)
        self._channels = x.shape[2]
        x = torch.split(x, 1, dim=1)
        return torch.cat(x, dim=2).squeeze(1)

    def inv(self, x):
        # expect (B, T * C, ...)
        return torch.cat(x.unsqueeze(1).split(self._channels, dim=2), dim=1)


class StackOnBatch(SequenceStrategy):

    def __init__(self):
        pass
        # self._timesteps = timesteps

    def apply(self, x):
        # expect (B, T, C, ...)
        bs, t, c = x.shape[:3]
        rest = x.shape[3:]
        return x.view(bs * t, c, *rest)
        # x = torch.split(x, 1, dim=1)
        # return torch.cat(x, dim=0).squeeze(1)


class ContextAgent(Agent):
    """Merge train and test time embeddingAgent 
    (from QAttentionMultitask.EmbeddingAgent) into one"""

    def __init__(self,
                 embedding_net: nn.Module,
                 camera_names: list,
                 with_action_context: bool,
                 is_train: bool,
                 # for traintime:
                 embedding_size: int,  
                 query_ratio: float,
                 margin: float = 0.1,
                 emb_lambda: float = 1.0,
                 save_context: bool = False, 
                 loss_mode: str = 'hinge', 
                 prod_of_gaus_factors_over_batch: bool = False, # for PEARL 
                 encoder_cfg: DictConfig = None,
                 one_hot: bool = False,
                 replay_update: bool = True, 
                 single_embedding_replay: bool = True,
                 ):
        self._embedding_net = embedding_net
        self._encoder_cfg = encoder_cfg
        self._camera_names = camera_names
        self._with_action_context = with_action_context
        self._sequence_strategy = StackOnChannel() # TODO(Mandi): other options here? 
        self._is_train = is_train
        # train time:
        if with_action_context and not isinstance(self._sequence_strategy, StackOnChannel):
            raise Exception('Embedding agent action context is only valid for StackOnChannel sequence strategy')
        self._current_context = None
        self._loss_mode = loss_mode
 
        #   TecNet: 
        self._query_ratio = query_ratio 
        self._margin = margin
        self._emb_lambda = emb_lambda
        #   PEARL
        self._prod_of_gaus_factors_over_batch = prod_of_gaus_factors_over_batch
        self._name = NAME 

        self._val_loss = None 
        self._val_embedding_accuracy = None 
        self._one_hot = one_hot 
        self._replay_update = replay_update
        self._replay_summaries, self._context_summaries = {}, {}
        self.single_embedding_replay = single_embedding_replay
        logging.info(f'Creating context agent that takes {query_ratio} of the total K samples as query')

    def build(self, training: bool, device: torch.device = None):
        """Train and Test time use the same build() """
        if device is None:
            device = torch.device('cpu') 
        self._embedding_net.set_device(device)
        #self._embedding_net.build()
        #self._embedding_net.to(device).train(training)
        self._device = device
        self._zero = torch.tensor(0.0, device=device)
        # use a separate optimizer here to update the params with metric loss,
        # optionally, qattention agents also have optimizers that update the embedding params here
        if training:
            self._optimizer, self._optim_params = make_optimizer(
                self._embedding_net, self._encoder_cfg, return_params=True)
 
    def act_for_replay(self, step, replay_sample, output_loss=False):
        """Use this to embed context only for qattention agent update"""
        data = replay_sample[CONTEXT_KEY].to(self._device) 
        if self._one_hot:
            return ActResult(data) 
        # NOTE(10/08) now replay sample is also shape (bsize, num_samples, vid_len, 3, 128, 128) 
        # note shape here is (bsize, video_len, 3, 128, 128), preprocess_agent squeezed the task dimension 
        b, k, n, ch, img_h, img_w = data.shape

        task_ids = replay_sample[TASK_ID] # this should be (B, K_action, ...)
        assert task_ids.shape[0] == b, f'B dimension in replay samples should all be the same, got {task_ids.shape[0]} and {b}'
        k_action = task_ids.shape[1]

        model_inp = rearrange(data, 'b k n ch h w -> (b k) ch n h w')
        embeddings = self._embedding_net(model_inp) # shape (b, embed_dim)
        embeddings = rearrange(embeddings, '(b k) d -> b k d', b=b, k=k)
        if self.single_embedding_replay:
            action_embeddings = repeat(embeddings[:, 0, :], 'b d -> b k d', b=b, k=k_action) 
        else:
            # idxs = torch.randint( k, (k_action,) )  #select _with_ replacement 
            # action_embeddings = embeddings[:, idxs]
            # NOTE(1101): take mean emb. here instead
            action_embeddings = repeat(embeddings.mean(dim=1), 'b d -> b k d', b=b, k=k_action)
        action_embeddings = action_embeddings / action_embeddings.norm(dim=2, p=2, keepdim=True)
        # print('shapes of action embeddings vs embeddings:', action_embeddings.shape, embeddings.shape)
        act_result = ActResult(action_embeddings, info={})
        if self._replay_update and output_loss: 
            # self._optimizer.zero_grad()
            if self._loss_mode == 'hinge':
                update_dict = self._compute_hinge_loss(embeddings, val=False)  
            elif self._loss_mode == 'hinge-v2':
                update_dict = self._compute_hinge_loss_v2(embeddings, val=False)  
            else:
                raise NotImplementedError
            
            act_result = ActResult(action_embeddings, info={'emb_loss': update_dict['emb_loss']})
            self._replay_summaries = {
                    'replay_batch/'+k: torch.mean(v) for k,v in update_dict.items()}
             
        return act_result
        
    def act(self, step: int, observation: dict,
            deterministic=False) -> ActResult:
        """observation batch may require different input preprocessing, handle here """
        # print('context agent input:', observation.keys())
         
        data = observation[CONTEXT_KEY].to(self._device)
        if self._one_hot:
            return ActResult(data)
        #print(data.shape)
        model_inp = rearrange(data, 'n ch h w -> 1 ch n h w')
        embeddings = self._embedding_net(model_inp) # should be (1,d)
        embeddings = embeddings / embeddings.norm(dim=1, p=2, keepdim=True) 
        self._current_context = embeddings.detach().requires_grad_(False)
        return ActResult(self._current_context)
        
    def set_new_context(self, observation: dict):
        with torch.no_grad():
            observations = []
            for n in self._camera_names:
                ob = observation[n]
                k, t, c, h, w = ob.shape
                ob_seq = self._sequence_strategy.apply(ob)
                if self._with_action_context:
                    action = self._sequence_strategy.apply(
                        observation['action'].type(torch.float32))
                    action_tiled = torch.reshape(action, (k, -1, 1, 1)).repeat(
                        1, 1, h, w)
                    ob_seq = torch.cat((ob_seq, action_tiled), -3)
                observations.append(ob_seq)
            embeddings = self._embedding_net(observations)
            embeddings = embeddings.view(1, k, -1)
            embeddings_norm = embeddings / embeddings.norm(
                dim=2, p=2, keepdim=True)
            context = embeddings_norm.mean(1)  # (B, E)
            self._current_context = (context / context.norm(
                dim=1, p=2, keepdim=True))[0]  # gives (E,)
        return self._current_context

    def act_summaries(self) -> List[Summary]:
        return []

    def load_weights(self, savedir: str):
        device = self._device
        self._embedding_net.load_state_dict(
            torch.load(os.path.join(savedir, 'embedding_net.pt'), map_location=device))

    def save_weights(self, savedir: str):
        torch.save(
            self._embedding_net.state_dict(),
            os.path.join(savedir, 'embedding_net.pt'))

    # def update(self, step, replay_sample: dict) -> dict:
    #     if self._is_train:
    #         return self.train_update(step, replay_sample)
    #     else:
    #         return self.test_update(step, replay_sample)

    def update_summaries(self) -> List[Summary]:
        if self._is_train:
            return self.update_train_summaries() 
        else:
            return self.update_test_summaries()
    
    def _preprocess_inputs(self, batch):
        #for collate_id, d in batch.items():
            #print('context agent update:', collate_id, d.get('front_rgb').shape)

        data = torch.stack([ d.get('front_rgb') for collate_id, d in batch.items()] ) 
        #print('context agent update:', data.shape)
        assert len(data.shape) == 6, 'Must be shape (b, num_episodes, num_frames, channels, img_h, img_w)'
        b, k, n, ch, img_h, img_w = data.shape 
        model_inp = rearrange(data, 'b k n ch h w -> (b k) ch n h w').to(self._device)
        return model_inp, b, k 

    def update(self, step, context_batch, val=False):
        # this is kept separate from replay_sample batch, s.t. we can contruct the
        # batch for embedding loss with more freedom 
        # data = torch.stack([ d.get('front_rgb') for collate_id, d in context_batch.items()] ) 
        # data = rearrange(data, 'b k n ch h w -> (b k) n ch h w')
        # temp_batch = {
        #    'demo_sample' : data
        # }
        # utils.visualize_batch(temp_batch, filename='/home/mandi/ARM/debug/ctxt_batch', img_size=128)
        # raise ValueError
        if self._one_hot:
            return {}
        model_inp, b, k = self._preprocess_inputs(context_batch)
         
        embeddings = self._embedding_net(model_inp)
        self._mean_embedding = embeddings.mean()
        embeddings = rearrange(embeddings, '(b k) d -> b k d', b=b, k=k)

        if not val:
            self._optimizer.zero_grad()
         
        if self._loss_mode == 'hinge':
            update_dict = self._compute_hinge_loss(embeddings, val=val)
        elif self._loss_mode == 'hinge-v2':
            update_dict = self._compute_hinge_loss_v2(embeddings, val=val)
        else:
            raise NotImplementedError
        self._context_summaries.update({
                f"context_batch/{'val' if val else 'train'}/"+k: torch.mean(v) for k,v in update_dict.items()}) 

        if not val:
            self._optimizer.zero_grad() 
            loss = update_dict['mean_emb_loss']
            loss.backward()
            self._optimizer.step()

        return update_dict

    def validate_context(self, step, context_batch):
        with torch.no_grad():
            val_dict = self.update(step, context_batch, val=True)
        return val_dict 

    def _compute_kl_loss(self, mu, sigma_sqrd):
        # ref: https://github.com/katerakelly/oyster/blob/cd09c1ae0e69537ca83004ca569574ea80cf3b9c/rlkit/torch/sac/agent.py#L117
        guassian_prior = torch.distributions.Normal(
            torch.zeros_like(mu), torch.ones_like(sigma_sqrd))
        # ref: https://github.com/katerakelly/oyster/blob/cd09c1ae0e69537ca83004ca569574ea80cf3b9c/rlkit/torch/sac/agent.py#L116
        posterior = torch.distributions.Normal(mu, torch.sqrt(sigma_sqrd))
        self._loss = torch.distributions.kl.kl_divergence(posterior, guassian_prior).mean()
        # ToDo: add more informative accuracy measure
        self._embedding_accuracy = 0

        return {
            'context': torch.cat((mu, sigma_sqrd), -1).mean(1),
            'mean_emb_loss': self._loss
        }

    def _compute_hinge_loss(self, embeddings, val=False): 
        b, k, d = embeddings.shape 
        embeddings_norm = embeddings / embeddings.norm(dim=2, p=2, keepdim=True)

        num_query = 1 if val else max(1, int(self._query_ratio * k)) # ugly hack cuz not enough validation data 
        num_support = int(k - num_query)
 
        # support_embeddings = embeddings_norm[:, num_support:]
        # query_embeddings = embeddings_norm[:, :num_query].reshape(b * num_query, -1) 
        query_embeddings, support_embeddings = embeddings_norm.split([num_query, num_support], dim=1)
        query_embeddings = query_embeddings.reshape(b * num_query, -1) 
        # norm query too?
        # print('context agent embedding size and val?: ', embeddings_norm.shape, val )
        support_context = support_embeddings.mean(1)  # (B, E)
        support_context = support_context / support_context.norm(dim=1, p=2, keepdim=True) # B, d
        similarities = support_context.matmul(query_embeddings.transpose(0, 1))
        similarities = similarities.view(b, b, num_query)  # (B, B, queries)

        # Gets the diagonal to give (batch, query)
        diag = torch.eye(b, device=self._device)
        positives = torch.masked_select(similarities, diag.unsqueeze(-1).bool())  # (B * query)
        positives = positives.view(b, 1, num_query)  # (B, 1, query)

        negatives = torch.masked_select(similarities, diag.unsqueeze(-1) == 0)
        # (batch, batch-1, query)
        #print('shapes:', query_embeddings.shape, similarities.shape, diag.shape, )
        #print('negatives shape:', negatives.shape, positives.shape)
        negatives = negatives.view(b, b - 1, -1)

        loss = torch.max(self._zero, self._margin - positives + negatives)
        if val:
            self._val_loss = loss.mean() * self._emb_lambda
        else:
            self._loss = loss.mean() * self._emb_lambda

        # Summaries
        max_of_negs = negatives.max(1)[0]  # (batch, query)
        accuracy = positives[:, 0] > max_of_negs
        if val:
            self._val_embedding_accuracy = accuracy.float().mean() 
        else:
            self._embedding_accuracy = accuracy.float().mean()
        
        #print(support_context.shape, support_embeddings[:,0].shape)
        similarities = support_embeddings[:,0].matmul(query_embeddings.transpose(0, 1))
        similarities = similarities.view(b, b, num_query)
        positives = torch.masked_select(similarities, diag.unsqueeze(-1).bool())  # (B * query)
        positives = positives.view(b, 1, num_query)  # (B, 1, query) 
        negatives = torch.masked_select(similarities, diag.unsqueeze(-1) == 0) 
        negatives = negatives.view(b, b - 1, -1)
        max_of_negs = negatives.max(1)[0]  # (batch, query)
        single_accuracy = positives[:, 0] > max_of_negs
        return {
            # 'context': support_context,
            'emb_loss': loss * self._emb_lambda,
            'mean_emb_loss': loss.mean() * self._emb_lambda,
            'emd_acc': accuracy.float().mean(),
            'emd_single_acc':  single_accuracy.float().mean(),
        }

    def _compute_hinge_loss_v2(self, embeddings, val=False):
        # try just using all the rest embeddings as support, i.e. no torch.split()
        b, k, d = embeddings.shape 
        embeddings_norm = embeddings / embeddings.norm(dim=2, p=2, keepdim=True)

        num_query = 1 if val else self._num_query # ugly hack cuz not enough validation data 
        # num_support = int(k - num_query)
 
        
        query_embeddings = embeddings_norm[:, :num_query].reshape(b * num_query, -1)   
        support_context = embeddings_norm.mean(1)  # (B, E)
        support_context = support_context / support_context.norm(dim=1, p=2, keepdim=True) # B, d
        similarities = support_context.matmul(query_embeddings.transpose(0, 1))
        similarities = similarities.view(b, b, num_query)  # (B, B, queries)
  
    def update_train_summaries(self) -> List[Summary]:
        summaries = []
        if self._one_hot:
            return summaries

        prefix = 'ContextAgent'
        if self._replay_summaries is not None:
            summaries.extend([
                ScalarSummary(f'{prefix}/{key}', v) for key, v in self._replay_summaries.items()
                ])
        if self._context_summaries is not None:
            summaries.extend([
                ScalarSummary(f'{prefix}/{key}', v) for key, v in self._context_summaries.items()
                ])

        # if self._current_context is None:
        #     prefix = 'context/train'
        #     summaries.extend([
        #         ScalarSummary('%s_loss' % prefix, self._loss),
        #         ScalarSummary('%s_accuracy' % prefix, self._embedding_accuracy),
        #         ScalarSummary('%s_mean_embedding' % prefix, self._mean_embedding),
        #     ])
        #     if self._val_embedding_accuracy is not None:
        #         prefix = 'context/val'
        #         summaries.extend([
        #         ScalarSummary('%s_loss' % prefix, self._val_loss),
        #         ScalarSummary('%s_accuracy' % prefix, self._val_embedding_accuracy)
        #         ])
            # not logging parameters yet 
            # for tag, param in self._embedding_net.named_parameters():
            #     assert not torch.isnan(param.grad.abs() <= 1.0).all()
            #     summaries.append(
            #         HistogramSummary('%s/gradient/%s' % (prefix, tag), param.grad))
            #     summaries.append(
            #         HistogramSummary('%s/weight/%s' % (prefix, tag), param.data))
        return summaries
    
    def update_test_summaries(self) -> List[Summary]:
        return []

    # def train_update(self, step: int, replay_sample: dict) -> dict: 
    #     if self._current_context is not None:
    #         b = replay_sample['action'].shape[0]
    #         return {
    #             'context': self._current_context.unsqueeze(0).repeat(b, 1),
    #             'emb_loss': 0
    #         }
    #     observations = []
    #     for n in self._camera_names:
    #         ob = replay_sample['demo_' + n]
    #         b, k, t, c, h, w = ob.shape
    #         ob_seq = self._sequence_strategy.apply(ob.view(b * k, t, -1, h, w))
    #         if self._with_action_context:
    #             action = replay_sample['demo_action']
    #             ab, ak, at, _ = action.shape
    #             a_seq = self._sequence_strategy.apply(action.view(ab * ak, at, -1))
    #             action_tiled = a_seq.view(ab * ak, -1, 1, 1).repeat(1, 1, h, w)
    #             ob_seq = torch.cat((ob_seq, action_tiled), -3)
    #         observations.append(ob_seq)
    #     embeddings = self._embedding_net(observations)
    #     # Assume b and k constant across observations
    #     embeddings = embeddings.view(b, k, -1)
    #     if self._loss_mode == 'hinge':
    #         self._mean_embedding = embeddings.mean()
    #         return self._compute_hinge_loss(embeddings)
    #     elif self._loss_mode == 'kl':
    #         # reference: https://github.com/katerakelly/oyster/blob/cd09c1ae0e69537ca83004ca569574ea80cf3b9c/rlkit/torch/sac/agent.py#L129
    #         embedding_size = int(self._embedding_size / 2)
    #         mus = embeddings[..., :embedding_size]
    #         self._mean_embedding = mus.mean()
    #         # noinspection PyUnresolvedReferences
    #         sigmas_squared = torch.clamp(nn.functional.softplus(embeddings[..., embedding_size:]), 1e-7)
    #         if self._prod_of_gaus_factors_over_batch:
    #             # reference: https://github.com/katerakelly/oyster/blob/cd09c1ae0e69537ca83004ca569574ea80cf3b9c/rlkit/torch/sac/agent.py#L10
    #             sigma_sqrd = 1. / torch.sum(torch.reciprocal(sigmas_squared), 1, keepdim=True)
    #             mu = sigma_sqrd * torch.sum(mus / sigmas_squared, 1, keepdim=True)
    #         else:
    #             mu = mus
    #             sigma_sqrd = sigmas_squared
    #         return self._compute_kl_loss(mu, sigma_sqrd)
    #     else:
    #         raise Exception('Invalid loss mode, must be one of [ hinge | kl ], but found {}'.format(self._loss_mode))
