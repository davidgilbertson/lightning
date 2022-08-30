# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import io
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import torch
from torch import Tensor
from torch.nn import Module
from torch.utils.data import DataLoader

from lightning_lite.lite.accelerators import Accelerator
from lightning_lite.lite.plugins.environments import XLAEnvironment
from lightning_lite.lite.plugins.io.checkpoint_plugin import CheckpointIO
from lightning_lite.lite.plugins.io.xla_plugin import XLACheckpointIO
from lightning_lite.lite.plugins.precision import PrecisionPlugin
from lightning_lite.lite.strategies.ddp_spawn import DDPSpawnStrategy
from lightning_lite.lite.strategies.launchers.xla import _XLALauncher
from lightning_lite.lite.strategies.strategy import TBroadcast
from lightning_lite.lite.utilities import _TPU_AVAILABLE
from lightning_lite.lite.utilities.apply_func import apply_to_collection
from lightning_lite.lite.utilities.data import has_len
from lightning_lite.lite.utilities.distributed import ReduceOp
from lightning_lite.lite.utilities.rank_zero import rank_zero_only
from lightning_lite.lite.utilities.types import _PATH

if _TPU_AVAILABLE:
    import torch_xla.core.xla_env_vars as xenv
    import torch_xla.core.xla_model as xm
    from torch_xla.core.xla_model import rendezvous
    from torch_xla.distributed.parallel_loader import MpDeviceLoader
else:
    xm, xmp, MpDeviceLoader, rendezvous = [None] * 4


class TPUSpawnStrategy(DDPSpawnStrategy):
    """Strategy for training multiple TPU devices using the :func:`torch_xla.distributed.xla_multiprocessing.spawn`
    method."""

    strategy_name = "tpu_spawn"

    def __init__(
        self,
        accelerator: Optional[Accelerator] = None,
        parallel_devices: Optional[List[torch.device]] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision_plugin: Optional[PrecisionPlugin] = None,
        **_: Any,
    ) -> None:
        super().__init__(
            accelerator=accelerator,
            parallel_devices=parallel_devices,
            cluster_environment=XLAEnvironment(),
            checkpoint_io=checkpoint_io,
            precision_plugin=precision_plugin,
            start_method="fork",
        )
        self._checkpoint_io: Optional[CheckpointIO]
        self._launched = False

    @property
    def root_device(self) -> torch.device:
        if not self._launched:
            raise RuntimeError("Accessing the XLA device before processes have spawned is not allowed.")
        return xm.xla_device()

    @property
    def checkpoint_io(self) -> CheckpointIO:
        if self._checkpoint_io is None:
            self._checkpoint_io = XLACheckpointIO()
        return self._checkpoint_io

    @checkpoint_io.setter
    def checkpoint_io(self, io: Optional[CheckpointIO]) -> None:
        self._checkpoint_io = io

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, int]:
        return dict(num_replicas=self.world_size, rank=self.global_rank)

    @property
    def is_distributed(self) -> bool:
        # HOST_WORLD_SIZE is not set outside the xmp.spawn process
        return (xenv.HOST_WORLD_SIZE in os.environ) and self.world_size != 1

    def _configure_launcher(self) -> None:
        self._launcher = _XLALauncher(self)

    def setup_module(self, module: Module) -> Module:
        return module

    def module_to_device(self, module: Module) -> None:
        module.to(self.root_device)

    def process_dataloader(self, dataloader: DataLoader) -> MpDeviceLoader:
        TPUSpawnStrategy._validate_dataloader(dataloader)
        dataloader = MpDeviceLoader(dataloader, self.root_device)
        # Mimic interface to torch.utils.data.DataLoader
        dataloader.dataset = dataloader._loader.dataset
        return dataloader

    def reduce(
        self, output: Union[Tensor, Any], group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = None
    ) -> Tensor:
        if not isinstance(output, Tensor):
            output = torch.tensor(output, device=self.root_device)

        invalid_reduce_op = isinstance(reduce_op, ReduceOp) and reduce_op != ReduceOp.SUM
        invalid_reduce_op_str = isinstance(reduce_op, str) and reduce_op.lower() not in ("sum", "mean", "avg")
        if invalid_reduce_op or invalid_reduce_op_str:
            raise ValueError(
                "Currently, the TPUSpawnStrategy only supports `sum`, `mean`, `avg` for the reduce operation, got:"
                f" {reduce_op}"
            )

        output = xm.mesh_reduce("reduce", output, sum)

        if isinstance(reduce_op, str) and reduce_op.lower() in ("avg", "mean"):
            output = output / self.world_size

        return output

    def barrier(self, name: Optional[str] = None, *args: Any, **kwargs: Any) -> None:
        if self.is_distributed:
            rendezvous(name)

    def broadcast(self, obj: TBroadcast, src: int = 0) -> TBroadcast:
        if not self.is_distributed:
            return obj
        buffer = io.BytesIO()
        torch.save(obj, buffer)
        data = bytearray(buffer.getbuffer())
        data_tensor = torch.tensor(data, device=self.root_device, dtype=torch.float)
        data = xm.all_gather(data_tensor)
        buffer = io.BytesIO(data.cpu().byte().numpy())
        obj = torch.load(buffer)
        return obj

    def all_gather(self, tensor: Tensor, group: Optional[Any] = None, sync_grads: bool = False) -> Tensor:
        """
        Function to gather a tensor from several distributed processes
        Args:
            tensor: tensor of shape (batch, ...)
            group: not available with TPUs
            sync_grads: not available with TPUs
        Return:
            A tensor of shape (world_size, batch, ...)
        """
        if isinstance(tensor, Tensor) and tensor.dim() == 0:
            tensor = tensor.unsqueeze(0)
        return xm.all_gather(tensor)

    def save_checkpoint(
        self, checkpoint: Dict[str, Any], filepath: _PATH, storage_options: Optional[Any] = None
    ) -> None:
        """Save model/training states as a checkpoint file through state-dump and file-write.

        Args:
            checkpoint: dict containing model and trainer state
            filepath: write-target file's path
            storage_options: parameter for how to save to storage, passed to ``CheckpointIO`` plugin
        """
        # `xla_model.save` needs to be called on all ranks. It internally checks if the local rank is 0
        self.checkpoint_io.save_checkpoint(checkpoint, filepath, storage_options=storage_options)

    def remove_checkpoint(self, filepath: _PATH) -> None:
        """Remove checkpoint filepath from the filesystem.

        Args:
            filepath: Path to checkpoint
        """
        if self.local_rank == 0:
            self.checkpoint_io.remove_checkpoint(filepath)

    def teardown(self) -> None:
        super().teardown()
        os.environ.pop("PT_XLA_DEBUG", None)

    @classmethod
    def register_strategies(cls, strategy_registry: Dict) -> None:
        strategy_registry.register(
            cls.strategy_name,
            cls,
            description=f"{cls.__class__.__name__}",
        )

    def _worker_setup(self, process_idx: int) -> None:
        self._launched = True
        self.set_world_ranks(process_idx)
        rank_zero_only.rank = self.global_rank

    @staticmethod
    def _validate_dataloader(dataloaders: DataLoader) -> None:
        def check_has_len(dataloader: DataLoader) -> None:
            if not has_len(dataloader):
                raise TypeError(
                    "TPUs do not currently support IterableDataset objects, the dataset must implement `__len__`."
                    " HINT: You can mock the length on your dataset to bypass this MisconfigurationException."
                )

        apply_to_collection(dataloaders, dtype=object, wrong_dtype=(Sequence, Mapping), function=check_has_len)