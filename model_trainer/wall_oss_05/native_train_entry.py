"""Invoke upstream training, preserving a final checkpoint at the step limit."""
import os
from pathlib import Path
import shutil


def main():
    import torch.distributed as dist
    from wall_x.trainer.fsdp_trainer import FSDPTrainer
    from wall_x.trainer.fsdp_trainer.train_fsdp import main as upstream_main
    original_loop = FSDPTrainer.train_loop
    original_save = FSDPTrainer.save_checkpoint
    original_fit = FSDPTrainer.fit

    def train_loop(self, epoch, *args, **kwargs):
        self._rebot_last_epoch = epoch
        return original_loop(self, epoch, *args, **kwargs)

    def save_checkpoint(self, epoch, step=0):
        result = original_save(self, epoch, step)
        if dist.get_rank() == 0:
            source = Path(os.environ['WALLX_REBOT_REFERENCE'])
            for config in Path(self.cfg.checkpoint.save_path).rglob('config.yml'):
                shutil.copy2(source, config.parent / 'dataset_reference.json')
        return result

    def fit(self, *args, **kwargs):
        result = original_fit(self, *args, **kwargs)
        # Upstream breaks before epoch-save when the optimizer step cap is met.
        # Every rank must participate in this final FSDP save.
        self.save_checkpoint(getattr(self, '_rebot_last_epoch', self.start_epoch), self.global_step)
        return result

    FSDPTrainer.train_loop = train_loop
    FSDPTrainer.save_checkpoint = save_checkpoint
    FSDPTrainer.fit = fit
    upstream_main()


if __name__ == '__main__':
    main()
