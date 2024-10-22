import torch
from torch import Tensor
from torch.nn.parameter import Parameter
from torch.optim.lr_scheduler import _LRScheduler
from torch.optim.optimizer import Optimizer

import lightning as L
from typing import Optional, Mapping, Iterable, Callable

from vap.metrics import VAPMetric
from vap.modules.VAP import VAP
from vap.utils.utils import everything_deterministic

from vap.modules.encoder import EncoderCPC
from vap.modules.transformer import VapStereoTower

Batch = Mapping[str, Tensor]

everything_deterministic()


class VAPModule(L.LightningModule):
    def __init__(
        self,
        model: VAP,
        optim_fn: Optional[Callable[Iterable[Parameter], Optimizer]] = None,
        lr_scheduler: Optional[_LRScheduler] = None,
        train_metric: Optional[VAPMetric] = None,
        val_metric: Optional[VAPMetric] = None,
        test_metric: Optional[VAPMetric] = None,
        num_sink_tokens = 2,  # New parameter for attention sinks
    ):
        super().__init__()
        self.model = model
        self.optim: Optional[Optimizer] = (
            optim_fn(self.model.parameters()) if optim_fn else None
        )
        self.lr_scheduler: Optional[_LRScheduler] = (
            lr_scheduler(self.optim) if lr_scheduler else None
        )
        self.train_metric = train_metric
        self.val_metric = val_metric
        self.test_metric = test_metric
        self.num_sink_tokens = num_sink_tokens # New parameter for attention sinks
        self.save_hyperparameters()  # ignore=["model"])

    def forward(self, waveform: Tensor, *args, **kwargs) -> dict[str, Tensor]:
        return self.model(waveform, *args, **kwargs)

    @staticmethod
    def load_model(path: str, *args, **kwargs) -> VAP:
        return VAPModule.load_from_checkpoint(path, *args, **kwargs).model

    def configure_optimizers(self) -> dict:
        lr_scheduler = {
            "scheduler": self.lr_scheduler,
            "monitor": "val_loss",
        }
        return {"optimizer": self.optim, "lr_scheduler": lr_scheduler}

    def metric_update(self, logits, vad, split: str = "val"):
        m = getattr(self, f"{split}_metric", None)
        if m:
            probs = self.model.objective.get_probs(logits)
            m.update_batch(probs, vad)

    def metric_finalize(self, split: str = "val") -> None:
        m = getattr(self, f"{split}_metric", None)
        if m:
            scores = m.compute()
            m.reset()
            for event_name, score in scores.items():
                self.log(f"{split}_f1_{event_name}", score["f1"], sync_dist=True)
                self.log(f"{split}_acc_{event_name}_0", score["acc"][0], sync_dist=True)
                self.log(f"{split}_acc_{event_name}_1", score["acc"][1], sync_dist=True)

    def _step(
        self, batch: Batch, split: str = "train", reduction: str = "mean"
    ) -> Mapping[str, torch.Tensor]:
        """
        Arguments:
            batch:      dict, containing 'waveform', va, va_history

        Returns:
            out:        dict, ['logits', 'vad', 'vap_loss', 'vad_loss']
        """
        labels = self.model.extract_labels(batch["vad"])
        out = self(batch["waveform"])

        out["vap_loss"] = self.model.objective.loss_vap(
            out["logits"], labels, reduction=reduction
        )
        out["va_loss"] = self.model.objective.loss_vad(out["vad"], batch["vad"])
        self.metric_update(out["logits"], batch["vad"], split=split)

        # Log results
        batch_size = batch["waveform"].shape[0]
        self.log(
            f"{split}_loss", out["vap_loss"], batch_size=batch_size, sync_dist=True
        )
        self.log(
            f"{split}_loss_va",
            out["va_loss"],
            batch_size=batch_size,
            sync_dist=True,
        )
        return out

    def training_step(self, batch: Batch, *args, **kwargs):
        out = self._step(batch)
        loss = out["vap_loss"] + out["va_loss"]
        return {"loss": loss}

    def validation_step(self, batch: Batch, *args, **kwargs):
        _ = self._step(batch, split="val")

    def test_step(self, batch: Batch, *args, **kwargs):
        _ = self._step(batch, split="test")

    def on_train_epoch_end(self) -> None:
        self.metric_finalize(split="train")

    def on_validation_epoch_end(self) -> None:
        self.metric_finalize(split="val")

    def on_test_epoch_end(self) -> None:
        self.metric_finalize(split="test")


class VAPMonoModule(VAPModule):
    @staticmethod
    def load_model(path: str, *args, **kwargs) -> VAP:
        return VAPMonoModule.load_from_checkpoint(path, *args, **kwargs).model

    def _step(
        self, batch: Batch, split: str = "train", reduction: str = "mean"
    ) -> Mapping[str, torch.Tensor]:
        """
        Arguments:
            batch:      dict, containing 'waveform', va, va_history

        Returns:
            out:        dict, ['logits', 'vad', 'vap_loss', 'vad_loss']
        """
        labels = self.model.extract_labels(batch["vad"])
        out = self(batch["waveform"], batch["vad"][:, : labels.shape[1]])
        out["vap_loss"] = self.model.objective.loss_vap(
            out["logits"], labels, reduction=reduction
        )
        self.metric_update(out["logits"], batch["vad"], split=split)

        # Log results
        batch_size = batch["waveform"].shape[0]
        self.log(
            f"{split}_loss", out["vap_loss"], batch_size=batch_size, sync_dist=True
        )
        return out

    def training_step(self, batch: Batch, *args, **kwargs):
        out = self._step(batch)
        return {"loss": out["vap_loss"]}


if __name__ == "__main__":

    from vap.modules.encoder import EncoderCPC
    from vap.modules.modules import TransformerStereo

    encoder = EncoderCPC()
    transformer = TransformerStereo()
    model = VAP(encoder, transformer)
    module = VAPModule(model)
    print(module)
