DEM=5
for TASK in lamp_on  reach_target  take_lid_off_saucepan press_switch  stack_wine
do
for BEF in 200 #100 50   
do
    python launch_multitask.py exclude_tasks=[${TASK}] run_name=Batch64-lr5e4-${DEM}Demo-${BEF}before \
     method.lr=5e-4 replay.batch_size=64 framework.replay_ratio=64 \
     rlbench.demo_path=/home/mandi/all_rlbench_data wandb.group='multi_task' rlbench.demos=${DEM} \
     framework.log_freq=1000 framework.training_iterations=40000

done 
done


DEM=5
for TASK in lamp_on  reach_target  take_lid_off_saucepan press_switch  stack_wine
do
for BEF in 200 #100 50   
do
    python launch_multitask.py exclude_tasks=[${TASK}] run_name=Batch128-lr3e4-${DEM}Demo-${BEF}before \
     method.lr=3e-4 replay.batch_size=128 framework.replay_ratio=128 \
     rlbench.demo_path=/home/mandi/all_rlbench_data wandb.group='multi_task' \
     framework.log_freq=2000 framework.training_iterations=40000

done 
done
    