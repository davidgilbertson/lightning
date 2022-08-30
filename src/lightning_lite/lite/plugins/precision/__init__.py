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
from lightning_lite.lite.plugins.precision.deepspeed import DeepSpeedPrecisionPlugin
from lightning_lite.lite.plugins.precision.mixed import MixedPrecisionPlugin
from lightning_lite.lite.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from lightning_lite.lite.plugins.precision.precision import PrecisionPlugin
from lightning_lite.lite.plugins.precision.tpu import TPUPrecisionPlugin
from lightning_lite.lite.plugins.precision.tpu_bf16 import TPUBf16PrecisionPlugin

__all__ = [
    "DeepSpeedPrecisionPlugin",
    "MixedPrecisionPlugin",
    "NativeMixedPrecisionPlugin",
    "PrecisionPlugin",
    "TPUPrecisionPlugin",
    "TPUBf16PrecisionPlugin",
]