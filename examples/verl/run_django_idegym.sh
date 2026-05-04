set -x

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH}"

CONFIG_PATH="${PROJECT_DIR}/examples/verl/config"

python3 -m verl.trainer.main_ppo \
    --config-path="$CONFIG_PATH" \
    --config-name='django_idegym_grpo' \
    data.train_batch_size=8 \
    data.custom_cls.path="${PROJECT_DIR}/examples/verl/hf_dataset.py" \
    actor_rollout_ref.model.path=Qwen/Qwen3-0.6B \
    actor_rollout_ref.rollout.agent.agent_loop_config_path="${PROJECT_DIR}/examples/verl/config/agent_loop_config.yaml" \
    trainer.total_epochs=1 \
    trainer.logger=['wandb'] \
    trainer.project_name=django_idegym \
    trainer.experiment_name=django_idegym_grpo \
    trainer.n_gpus_per_node=4 \
    "$@"
