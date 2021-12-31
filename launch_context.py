"""
Launch meta-rl agents 
Note "solvable" tasks with >1 variations:
- pick_up_cup: 20
- pick_and_lift: 20 
- push_button: 18 (push_buttons has 50), this one is super easy 
- reach_target: 20 
NOTE: some local task's variation number might be messed up 
"""
import os
import pickle

from arm.custom_rlbench_env_multitask import CustomMultiTaskRLBenchEnv

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["QT_LOGGING_RULES" ] = '*.debug=false;qt.qpa.*=false'

from typing import List
import torch
from pyrep.const import RenderMode
from rlbench import CameraConfig, ObservationConfig, ArmActionMode
from rlbench.action_modes import ActionMode, GripperActionMode
from rlbench.backend import task
from rlbench.backend.utils import task_file_to_task_class
from yarr.replay_buffer.wrappers.pytorch_replay_buffer import \
    PyTorchReplayBuffer
from yarr.runners.env_runner import EnvRunner
from yarr.runners.pytorch_train_context_runner import PyTorchTrainContextRunner
from yarr.runners.pytorch_train_runner import PyTorchTrainRunner
from yarr.utils.stat_accumulator import SimpleAccumulator, MultiTaskAccumulator, MultiTaskAccumulatorV2

from arm import arm
from arm import c2farm
from arm.baselines import bc, td3, dac, sac
from arm.custom_rlbench_env import CustomRLBenchEnv
import numpy as np

import hydra
import logging
from omegaconf import DictConfig, OmegaConf, ListConfig
from os.path import join 
import wandb 

from yarr.utils.multitask_rollout_generator import RolloutGeneratorWithContext
from launch_multitask import _create_obs_config, _modify_action_min_max
  
from arm.demo_dataset import MultiTaskDemoSampler, RLBenchDemoDataset, collate_by_id, PyTorchIterableDemoDataset
from arm.models.utils import make_optimizer 
from functools import partial
from torch.utils.data import DataLoader
from torch.multiprocessing import Lock, cpu_count
from collections import defaultdict

ACTION_MODE = ActionMode(
        ArmActionMode.ABS_EE_POSE_PLAN_WORLD_FRAME,
        GripperActionMode.OPEN_AMOUNT)
LOG_CONFIG_KEYS = ['rlbench', 'replay', 'framework', 'contexts', 'dataset', 'method', 'dev']
LATEST_MT = [
    'pick_up_cup',
    'phone_on_base',
    'pick_and_lift',
    'put_rubbish_in_bin',
    'reach_target',
    'stack_wine', 
    'take_lid_off_saucepan',
    'take_umbrella_out_of_umbrella_stand', # hard 
    'lamp_on',
    'lamp_off',
    'open_door', # 80%+
    'press_switch', # better w/ 2+ cameras 
    'push_button',
    'take_usb_out_of_computer',
    'close_drawer', # 60% ish, not perfect. 3var
]
# multi-tcam (left_shoulder + right_shoulder): meat_griff, put_procery, money, unplug_charger, 
# put_groceries_in_cupboard: 9 vars
# meat_off_grill and meat_on_grill: 2 vars
# put_money_in_safe, take_money_out_safe: 3 each
# unplug_charger: 1 var
def make_loader(cfg, mode, dataset):
    """ Not used lately """
    variation_idxs, task_idxs = dataset.get_idxs()
    sampler = MultiTaskDemoSampler(
        variation_idxs_list=variation_idxs, # 1-1 maps from each variation to idx in dataset e.g. [[0,1], [2,3], [4,5]] belongs to 3 variations but 2 tasks
        task_idxs_list=task_idxs,      # 1-1 maps from each task to all its variations e.g. [[0,1,2,3], [4,5]] collects variations by task
        **(cfg.val_sampler if mode == 'val' else cfg.sampler),
    )

    collate_func = partial(collate_by_id, cfg.sampler.sample_mode+'_id')
    loader = DataLoader(
            dataset, 
            batch_sampler=sampler,
            num_workers=min(11, cpu_count()),
            worker_init_fn=lambda w: np.random.seed(np.random.randint(2 ** 29) + w),
            collate_fn=collate_func,
            )
    return loader 

def run_seed(
    cfg: DictConfig, 
    env, 
    cams, 
    device, 
    seed, 
    all_tasks, # [stack_blocks, push_buttons,...]
    train_demo_dataset,
    val_demo_dataset,
    ) -> None:
    replay_ratio = cfg.framework.get("replay_ratio", None)
    replay_path = join(cfg.replay.path, cfg.tasks_name, cfg.method.name, 'seed%d' % seed)
    action_min_max = None
    task_var_to_replay_idx = defaultdict(dict)
    if cfg.method.name == 'C2FARM':
        if cfg.replay.share_across_tasks:  
            logging.info(f'Using only one replay for multiple tasks, one batch size: {cfg.replay.batch_size}')
            r = c2farm.launch_utils.create_replay(
                cfg.replay.batch_size, 
                cfg.replay.timesteps,
                cfg.replay.prioritisation,
                replay_path if cfg.replay.use_disk else None, cams, env,
                cfg.method.voxel_sizes,
                replay_size=cfg.replay.replay_size
                ) 
            for i, (one_task, its_variations) in enumerate(zip(all_tasks, cfg.rlbench.use_variations)):
                for task_var in its_variations:
                    var = int( task_var.split("_")[-1])  
                    c2farm.launch_utils.fill_replay(
                        r, one_task, env, 
                        cfg.rlbench.demos,
                        cfg.method.demo_augmentation, 
                        cfg.method.demo_augmentation_every_n,
                        cams, 
                        cfg.rlbench.scene_bounds,
                        cfg.method.voxel_sizes, 
                        cfg.method.bounds_offset,
                        cfg.method.rotation_resolution, 
                        cfg.method.crop_augmentation,
                        variation=var,
                        task_id=i,
                        )
                    task_var_to_replay_idx[i][var] = 0
                print(f"Task id {i}: {one_task}, **filled** replay for {len(its_variations)} variations")
            replays = [r]
        else:
            replays = []
            cfg.replay.replay_size = int(cfg.replay.replay_size / sum([len(_vars) for _vars in cfg.rlbench.use_variations]) )
            logging.info(f'Splitting total replay size into each buffer: {cfg.replay.replay_size}')
            for i, (one_task, its_variations) in enumerate(zip(all_tasks, cfg.rlbench.use_variations)):
                for task_var in its_variations:
                    var = int( task_var.split("_")[-1]) 
                    r = c2farm.launch_utils.create_replay(
                        cfg.replay.batch_size, 
                        cfg.replay.timesteps,
                        cfg.replay.prioritisation,
                        replay_path if cfg.replay.use_disk else None, cams, env,
                        cfg.method.voxel_sizes, 
                        replay_size=cfg.replay.replay_size)
                    c2farm.launch_utils.fill_replay(
                        r, one_task, env, cfg.rlbench.demos,
                        cfg.method.demo_augmentation, 
                        cfg.method.demo_augmentation_every_n,
                        cams, cfg.rlbench.scene_bounds,
                        cfg.method.voxel_sizes, 
                        cfg.method.bounds_offset,
                        cfg.method.rotation_resolution, 
                        cfg.method.crop_augmentation,
                        variation=var,
                        task_id=i,
                        )
                    task_var_to_replay_idx[i][var] = len(replays)
                    replays.append(r)
                # print(f"Task id {i}: {one_task}, **created** and filled replay for {len(its_variations)} variations")
            print('Created mapping from var ids to buffer ids:', task_var_to_replay_idx)
            cfg.replay.total_batch_size = int(cfg.replay.batch_size * cfg.replay.buffers_per_batch)
              

        if cfg.mt_only:
            agent = c2farm.launch_utils.create_agent(cfg, env)
        else:
            agent = c2farm.launch_utils.create_agent_with_context(cfg, env)
    else:
        raise ValueError('Method %s does not exists.' % cfg.method.name)

    wrapped_replays = [PyTorchReplayBuffer(r, num_workers=1) for r in replays]
    # NOTE: stat accumulator still using task-based logging, too many variations
    # stat_accum = MultiTaskAccumulator(cfg.tasks, eval_video_fps=30) 
    logging.info('Creating Stat Accumulator: ')
    stat_accum = MultiTaskAccumulatorV2(
        task_names=all_tasks, 
        tasks_vars=cfg.rlbench.use_variations,
        eval_video_fps=30,
        mean_only=True,
        max_len=cfg.framework.num_log_episodes,
        log_all_vars=cfg.framework.log_all_vars,
        ) 

    logdir = join(cfg.log_path, 'seed%d' % seed)
    os.makedirs(logdir, exist_ok=False)
    OmegaConf.save( config=cfg, f=join(logdir, 'config.yaml') )
    weightsdir = join(logdir, 'weights')
    

    if action_min_max is not None:
        # Needed if we want to run the agent again
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, 'action_min_max.pkl'), 'wb') as f:
            pickle.dump(action_min_max, f)

    if not (cfg.mt_only or cfg.dev.one_hot or cfg.dev.noisy_one_hot):
        logging.info('\n Making dataloaders for context batch training')
        # ctxt_train_loader = make_loader(cfg.contexts, 'train', train_demo_dataset)
        # ctxt_val_loader  = make_loader(cfg.contexts,'val', val_demo_dataset)
        train_demo_dataset = PyTorchIterableDemoDataset(
            demo_dataset=train_demo_dataset,
            batch_dim=cfg.contexts.sampler.batch_dim,
            samples_per_variation=cfg.contexts.sampler.k_dim,
            sample_mode=cfg.contexts.sampler.sample_mode,
            )
        val_demo_dataset = PyTorchIterableDemoDataset(
            demo_dataset=val_demo_dataset,
            batch_dim=cfg.contexts.sampler.val_batch_dim,
            samples_per_variation=cfg.contexts.sampler.val_k_dim,
            sample_mode=cfg.contexts.sampler.sample_mode,
            )
    else:
        logging.info('\n Starting no-context TrainRunner')


    num_all_vars = sum([len(variations) for variations in cfg.rlbench.all_variations]) 
    # if mt_only, generator doesn't sample context

    rollout_generator = RolloutGeneratorWithContext(
        train_demo_dataset, 
        one_hot=cfg.dev.one_hot, 
        noisy_one_hot=cfg.dev.noisy_one_hot, 
        num_task_vars=num_all_vars,
        task_var_to_replay_idx=task_var_to_replay_idx,
        )

    device_list = [ i for i in range(torch.cuda.device_count()) ]
    assert len(device_list) > 1, 'Must use multiple GPUs'
    env_gpus = None 
    if len(device_list) > 1:
        print('Total visible GPUs idxed: ', device_list)
        env_gpus = device_list[cfg.framework.env_runner_gpu: ]
        print('Environment runner using GPUs idxed: ', env_gpus)

    env_runner = EnvRunner(
        train_env=env, 
        agent=agent, 
        train_replay_buffer=replays,
        rollout_generator=rollout_generator,
        num_train_envs=cfg.framework.train_envs,                
        num_eval_envs=cfg.framework.eval_envs,
        episodes=99999,
        episode_length=cfg.rlbench.episode_length,
        stat_accumulator=stat_accum,
        weightsdir=weightsdir,
        max_fails=cfg.rlbench.max_fails,
        device_list=env_gpus,
        share_buffer_across_tasks=cfg.replay.share_across_tasks, 
        task_var_to_replay_idx=task_var_to_replay_idx,
        eval_only=cfg.dev.eval_only, # only run eval EnvRunners 
        iter_eval=cfg.framework.ckpt_eval, # 
        eval_episodes=cfg.framework.num_log_episodes, 
        )  

    if cfg.framework.wandb:
        run = wandb.init(**cfg.wandb)  
        run.name = "/".join( cfg.log_path.split('/')[-2:] )
        cfg_dict = {}
        for key in LOG_CONFIG_KEYS:
            for sub_key in cfg[key].keys():
                cfg_dict[key+'/'+sub_key] = cfg[key][sub_key]
        run.config.update(cfg_dict)
        run.save()
    
    resume_dir = None
    if cfg.resume:
        resume_dir = join(cfg.resume_path, cfg.resume_run, 'weights', str(cfg.resume_step))
        assert os.path.exists(resume_dir), 'Cannot find the weights saved at path: '+resume_dir
        cfg.framework.resume_dir = resume_dir 

    train_runner = PyTorchTrainContextRunner(
        agent, env_runner,
        wrapped_replays, 
        device, 
        stat_accum,
        iterations=cfg.framework.training_iterations,
        eval_episodes=cfg.framework.eval_episodes,
        save_freq=cfg.framework.save_freq, 
        log_freq=cfg.framework.log_freq, 
        logdir=logdir,
        weightsdir=weightsdir,
        replay_ratio=replay_ratio,
        transitions_before_train=cfg.framework.transitions_before_train,
        tensorboard_logging=cfg.framework.tensorboard_logging,
        csv_logging=cfg.framework.csv_logging,
        context_cfg=cfg.contexts, 
        train_demo_dataset=train_demo_dataset,
        val_demo_dataset=val_demo_dataset,
        wandb_logging=cfg.framework.wandb,
        context_device=torch.device("cuda:%d" % (cfg.framework.gpu+1)),
        no_context=cfg.mt_only,
        one_hot=cfg.dev.one_hot,
        noisy_one_hot=cfg.dev.noisy_one_hot,
        num_vars=num_all_vars,
        buffers_per_batch=cfg.replay.buffers_per_batch,
        num_tasks_per_batch=cfg.replay.num_tasks_per_batch,
        update_buffer_prio=cfg.replay.update_buffer_prio,
        offline=cfg.dev.offline,
        eval_only=cfg.dev.eval_only,  
        switch_online_tasks=cfg.framework.switch_online_tasks,
        task_var_to_replay_idx=task_var_to_replay_idx,
        dev_cfg=cfg.dev,
        )
 
    if cfg.dev.eval_only:
        train_runner.evaluate(resume_dir)
    else:
        train_runner.start(resume_dir)
    del train_runner
    del env_runner
    torch.cuda.empty_cache()


@hydra.main(config_name='config_metarl', config_path='conf')
def main(cfg: DictConfig) -> None: 
    if cfg.framework.gpu is not None and torch.cuda.is_available():
        device = torch.device("cuda:%d" % cfg.framework.gpu) 
        torch.backends.cudnn.enabled = torch.backends.cudnn.benchmark = True
    else:
        device = torch.device("cpu")
    logging.info('Using device %s.' % str(device))

    tasks = sorted([t for t in cfg.tasks.all_tasks if t != cfg.tasks.heldout])
    task_classes = [task_file_to_task_class(t) for t in tasks]

    cfg.rlbench.cameras = cfg.rlbench.cameras if isinstance(
        cfg.rlbench.cameras, ListConfig) else [cfg.rlbench.cameras]
    obs_config = _create_obs_config(cfg.rlbench.cameras,
                                    cfg.rlbench.camera_resolution) 
        
    variation_idxs = [j for j in range(cfg.rlbench.num_vars)] if cfg.rlbench.num_vars > -1 else []
    if len(cfg.dev.handpick) > 0:
        logging.info('Hand-picking limited variation ids: ')
        print(cfg.dev.handpick)
        variation_idxs = cfg.dev.handpick

    logging.info(f'Creating Env with that samples only from below variations:')
    print(variation_idxs)

    env = CustomMultiTaskRLBenchEnv(
        task_classes=task_classes, task_names=tasks, observation_config=obs_config,
        action_mode=ACTION_MODE, dataset_root=cfg.rlbench.demo_path,
        episode_length=cfg.rlbench.episode_length, headless=True, 
        use_variations=variation_idxs, # the tasks may have different num of variations, deal w that later 
        )
    print('Done creating custom env')
    
    all_tasks = []
    var_count, use_vars_count = 0, 0
    all_variations = [] 
    use_variations = []
    for name, tsk in zip(tasks, task_classes):
        task = env.get_task(tsk) 
        count = task.variation_count()
        if name == 'put_groceries_in_cupboard':
            count = 6 
            print('put_groceries_in_cupboard has bugged variation6, skipping 6-9 ') 
        use_count = cfg.rlbench.num_vars if cfg.rlbench.num_vars > -1 else count 
        all_tasks.append(name)
        var_count += count
        use_vars_count += use_count 
        all_variations.append([ f"{name}_{c}" for c in range(count) ])
        if len(cfg.dev.handpick) > 0:
            use_variations.append([ f"{name}_{c}" for c in cfg.dev.handpick ])
            logging.info('Hand-picked variation names: ') 
            print(use_variations)
        else:
            use_variations.append([ f"{name}_{c}" for c in range(use_count) ])
        #print(name, tsk ,all_variations) 
        logging.info(f"Task: {name}, a total of {count} variations avaliable, using {use_count} of them")
     
    # NOTE(Mandi) need to give a "blank" vanilla env so you don't get this error from env_runner.spinup_train_and_eval:
    # TypeError: can't pickle _thread.lock objects 
     
    env = CustomMultiTaskRLBenchEnv(
        task_classes=task_classes, task_names=tasks, observation_config=obs_config,
        action_mode=ACTION_MODE, dataset_root=cfg.rlbench.demo_path,
        episode_length=cfg.rlbench.episode_length, headless=True,
        use_variations=variation_idxs
        )
    
    cfg.rlbench.all_tasks = all_tasks
    cfg.rlbench.id_to_tasks = [(i, tsk) for i, tsk in enumerate(all_tasks)]
    cfg.rlbench.all_variations = all_variations
    cfg.rlbench.use_variations = use_variations
    all_task_ids = [ i for i in range(len(all_tasks)) ]

    tasks_name = "-".join(tasks) + f"-{var_count}var"
    if len(tasks) > 2:
        tasks_name = f'{len(tasks)}Task-{use_vars_count}var' 
        if len(cfg.tasks.heldout) > 1:
            tasks_name += f'-Heldout-{cfg.tasks.heldout}'
        logging.info(f'Got {len(tasks)} tasks, re-naming the run as: {tasks_name}')
    cfg.tasks_name = tasks_name
     
    if not cfg.mt_only and cfg.rlbench.num_vars == -1 :
        # sanity check context dataset sampler
        if cfg.contexts.sampler.sample_mode == 'variation':
            assert cfg.contexts.sampler.batch_dim <= sum([len(l) for l in all_variations]) , \
                f'Cannot construct a batch dim {cfg.contexts.sampler.batch_dim} larger than num. of {sum([len(l) for l in all_variations])} avalible variations'
        elif cfg.contexts.sampler.sample_mode == 'task':
            assert cfg.contexts.sampler.batch_dim <= sum([len(l) for l in all_tasks]), \
                f'Cannot construct a batch dim {cfg.contexts.sampler.batch_dim} larger than num. of {sum([len(l) for l in all_tasks])} avalible tasks'
        

    cwd = os.getcwd()

    cfg.run_name = cfg.run_name + f"-Replay_B{cfg.replay.batch_size}x{1 if cfg.replay.share_across_tasks else cfg.replay.buffers_per_batch}"
    if cfg.mt_only or cfg.dev.one_hot or cfg.dev.noisy_one_hot:
        logging.info('Use MT-policy or One-hot context, no context embedding, setting EnvRunner visible GPUs to 1')
        # cfg.run_name += '-NoContext' 
        cfg.framework.env_runner_gpu = 1 
    elif cfg.contexts.update_freq > cfg.framework.training_iterations:
        logging.info('Warning! Not updating context agent with context batch, hinge loss calculated from replay batch only.')
    elif cfg.dev.discrete:
        cfg.run_name += f"-Hidden{cfg.dev.encode_context_hidden}-Encode{cfg.dev.qnet_context_latent_size}"
    else:
        cfg.run_name +=  f"-Ctxt_B{cfg.contexts.sampler.batch_dim}_freq{cfg.contexts.update_freq}_" + \
                        f"iter{cfg.contexts.num_update_itrs}_embed{cfg.contexts.agent.embedding_size*4}" 
    
    log_path = join(cwd, tasks_name, cfg.run_name)
    os.makedirs(log_path, exist_ok=True) 
    existing_seeds = len(list(filter(lambda x: 'seed' in x, os.listdir(log_path))))
    logging.info('Logging to:' + log_path)
    cfg.log_path = log_path 
    # logging.info('\n' + OmegaConf.to_yaml(cfg))

    if cfg.mt_only or cfg.dev.one_hot or cfg.dev.noisy_one_hot :
        train_demo_dataset, val_demo_dataset = None, None 
    else:
        # make demo dataset and align idxs with task id in the environment 
        logging.info('Making dataset for context embedding update')
        cfg.dataset.include_tasks = all_tasks
        train_demo_dataset = RLBenchDemoDataset(obs_config=obs_config, mode='train', **cfg.dataset)
        val_demo_dataset = RLBenchDemoDataset(obs_config=obs_config, mode='val', **cfg.dataset)
        
        # some sanity check to make sure task_ids from offline dataset and env are matched
        for i, (one_task, its_variations) in enumerate(zip(all_tasks, all_variations)):
            for task_var in its_variations:
                var = int( task_var.split("_")[-1])
                data = train_demo_dataset.sample_one_variation(i, var, size=1)[0] # this only loads the first episode of each variation 
                assert one_task in data['name'], f"Task idx {i}. variation {var} \
                    should be {task_var} from environment, but got {data['name']} instead"


    for seed in range(existing_seeds, existing_seeds + cfg.framework.seeds):
        logging.info('Starting seed %d.' % seed)
        run_seed(
            cfg, 
            env, 
            cfg.rlbench.cameras, 
            device, 
            seed, 
            all_tasks, 
            train_demo_dataset,
            val_demo_dataset,
            )


if __name__ == '__main__':
    main()

