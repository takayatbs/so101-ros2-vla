# Third-party notices

Reviewed on 2026-10-31. This file describes the source repository and its declared dependencies. Installed versions and binary-bundled components depend on the selected image, architecture and optional model environment; see [the distribution guide](docs/licenses.ja.md).

The project's [Apache-2.0 license](LICENSE) covers its original code, documentation and configuration. It does not replace third-party licenses or grant rights to downloaded weights, datasets, fonts or NVIDIA runtime components. Preserve [NOTICE](NOTICE) and the accompanying license texts when redistributing this source.

## Materials referenced or adapted in the source

| Upstream | Use in this project | License and retained text |
|---|---|---|
| [LeRobot](https://github.com/huggingface/lerobot/tree/22bd7a2f489b367d8df42de803b1e8c4ca63a3f9), commit `22bd7a2f489b367d8df42de803b1e8c4ca63a3f9` | Model integration and modified flow/denoising paths in `lerobot_flow.py`, `groot_flow.py`, `xvla_flow.py`, `diffusion_rtc.py`; motor-unit compatibility in `lerobot_compat.py`; synthetic reference-fixture generator | Apache-2.0. The [complete upstream LICENSE](LICENSES/LeRobot-LICENSE.txt), including its Diffusion Policy, FOWM, simxarm, ALOHA and DETR notices, is retained verbatim. Upstream copyright holders and project modifications are identified in the relevant headers and [NOTICE](NOTICE). |
| [FTServo_Python](https://github.com/ftservo/FTServo_Python/tree/9e6e342f6011dc727ba7144fc835bca5357b3ca9), commit `9e6e342f6011dc727ba7144fc835bca5357b3ca9` | Register/instruction reference for the project's own `feetech.py` transport. The SDK is not vendored or imported. | MIT, Copyright (c) 2024 ftservo. [Upstream text](LICENSES/FTServo-LICENSE.txt) retained as a protocol-reference acknowledgement. |

The original compact ACT and flow models, approximate robot geometry, review UI and orchestration code are project implementations. References to papers describe the methods; they do not relicense those papers or claim that the original authors supplied this project's code. The synthetic compatibility fixture contains generated test values, not an operator's calibration or recorded dataset.

## Python and machine-learning dependencies

The table gives the upstream package's principal license. Wheels may bundle additional libraries with their own notices. Read their installed license files as well; a top-level license identifier is not the entire binary's license inventory.

Some installed packages omit a standalone license text. Reviewed supplementary texts for exact package versions (colcon, vcstool, rospkg, pySerial, PyOpenGL and tokenizers) are retained under [LICENSES/runtime/](LICENSES/runtime/). The [provenance manifest](LICENSES/runtime-manifest.json) records original URLs, package versions and SHA-256 checksums; duplicate texts are stored once. The exporter uses a supplementary text only for the exact reviewed version. These texts do not supply corresponding source code or certify the license status of a whole binary.

| Dependency | Declared use/version | Primary upstream license evidence |
|---|---|---|
| PyTorch | Base `torch==2.11.0`, CPU or CUDA wheel | BSD-3-Clause and bundled notices: [LICENSE](https://github.com/pytorch/pytorch/blob/v2.11.0/LICENSE), [NOTICE](https://github.com/pytorch/pytorch/blob/v2.11.0/NOTICE) |
| torchvision | Optional environment; matched to torch (`0.26.0` for torch 2.11) | BSD-3-Clause: [LICENSE](https://github.com/pytorch/vision/blob/v0.26.0/LICENSE) |
| safetensors | Base `0.6.2`; optional environment may resolve a newer compatible version | Apache-2.0: [LICENSE](https://github.com/huggingface/safetensors/blob/v0.6.2/LICENSE) |
| MuJoCo | `3.3.7` | Apache-2.0 with third-party components: [LICENSE](https://github.com/google-deepmind/mujoco/blob/3.3.7/LICENSE). The wheel also ships `LICENSES_THIRD_PARTY.md`; retain that installed file. |
| Apache Arrow / pyarrow | `25.0.0` | Apache-2.0 with bundled component notices: [LICENSE](https://github.com/apache/arrow/blob/apache-arrow-25.0.0/LICENSE.txt), [NOTICE](https://github.com/apache/arrow/blob/apache-arrow-25.0.0/NOTICE.txt) |
| pySerial | `3.5` | BSD-3-Clause: [LICENSE](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt) |
| setuptools | `79.0.1` in the base; optional environment resolves its own dependencies | MIT with vendored components: [LICENSE](https://github.com/pypa/setuptools/blob/v79.0.1/LICENSE) |
| NumPy | Ubuntu package and optional LeRobot environment | BSD-3-Clause with bundled component notices: [license](https://numpy.org/doc/stable/license.html) |
| OpenCV | Ubuntu `python3-opencv`; LeRobot also installs `opencv-python-headless` | Apache-2.0 and retained BSD/component notices: [upstream licensing](https://opencv.org/license/). Inspect the installed Ubuntu package and wheel separately. |
| PyAV | Ubuntu `python3-av`; optional environment may install a wheel | BSD-3-Clause for PyAV: [LICENSE](https://github.com/PyAV-Org/PyAV/blob/main/LICENSE.txt). Linked/bundled FFmpeg libraries have separate conditions. |
| PyYAML | Ubuntu `python3-yaml` | MIT: [LICENSE](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| psutil | Ubuntu `python3-psutil` | BSD-3-Clause: [LICENSE](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| pytest | Ubuntu `python3-pytest`, development/test use | MIT: [LICENSE](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| LeRobot | Fixed source commit above; optional extras `diffusion`, `smolvla`, `pi`, `groot`, `xvla` | Apache-2.0 and [retained upstream notices](LICENSES/LeRobot-LICENSE.txt) |
| transformers, diffusers, accelerate, datasets, huggingface_hub, tokenizers, peft, timm | Optional LeRobot dependencies, according to the selected extras | Apache-2.0; see the respective upstream [Transformers](https://github.com/huggingface/transformers/blob/main/LICENSE), [Diffusers](https://github.com/huggingface/diffusers/blob/main/LICENSE), [Accelerate](https://github.com/huggingface/accelerate/blob/main/LICENSE), [Datasets](https://github.com/huggingface/datasets/blob/main/LICENSE), [Hub](https://github.com/huggingface/huggingface_hub/blob/main/LICENSE), [Tokenizers](https://github.com/huggingface/tokenizers/blob/main/LICENSE), [PEFT](https://github.com/huggingface/peft/blob/main/LICENSE), [timm](https://github.com/huggingface/pytorch-image-models/blob/main/LICENSE) texts. |
| scipy, einops and other transitive Python packages | Resolved by the chosen extras | SciPy: BSD-3-Clause plus bundled notices; einops: MIT. The exact complete installed inventory is exported by `scripts/export_licenses.py`, including distributions omitted from this overview. |

## ROS, desktop and operating-system dependencies

The Dockerfile installs Ubuntu Noble packages and ROS 2 Jazzy, including their transitive dependencies. These packages are installed during the build rather than copied into this source tree. Do not describe the resulting image as entirely Apache-2.0.

| Component | Principal license / distribution notes |
|---|---|
| ROS 2 client libraries, launch, ros2_control, ros2_controllers, realtime_tools, xacro, robot_state_publisher | Commonly Apache-2.0; ROS is a collection of independently licensed packages. See [ROS licensing](https://docs.ros.org/en/jazzy/The-ROS2-Project/Contributing/Developer-Guide.html#licensing) and each installed package's `package.xml` and `/usr/share/doc/ros-jazzy-*/copyright`. |
| Foxglove bridge and messages; MCAP storage | Preserve individual MIT/Apache/BSD component notices, including the [Foxglove SDK](https://github.com/foxglove/foxglove-sdk) and [MCAP](https://github.com/foxglove/mcap/blob/main/LICENSE). Foxglove's separately accessed application has its own terms and is not vendored here. |
| Fast DDS and other ROS middleware | Package-specific licenses and third-party components; [Fast DDS license](https://github.com/eProsima/Fast-DDS/blob/master/LICENSE). |
| noVNC | MPL-2.0 with additional asset/dependency licenses: [upstream license information](https://github.com/novnc/noVNC/blob/master/LICENSE.txt). The installed Ubuntu copyright file also records BSD, OFL, CC-BY-SA and other components. |
| websockify | Primarily LGPL-3.0, with file-specific licenses in the Ubuntu package: [upstream](https://github.com/novnc/websockify/blob/master/COPYING). |
| TigerVNC | GPL-2.0-or-later and component licenses: [upstream](https://github.com/TigerVNC/tigervnc/blob/master/LICENCE.TXT). |
| MATE, terminal/editor utilities, RViz/rqt/Qt, X11/Mesa | Mixed GPL, LGPL, BSD, MIT and other licenses. Preserve the installed copyright files; source/relinking requirements depend on the component and its distribution. |
| FFmpeg and codec libraries | Upstream is primarily LGPL-2.1-or-later, but enabling GPL components changes the applicable terms. The inspected Ubuntu build reports GPL-2.0-or-later (`ffmpeg -L`) and includes `--enable-gpl`. See [FFmpeg legal information](https://ffmpeg.org/legal.html). |
| Ubuntu, DejaVu and Noto fonts; themes/wallpapers | Font and asset licenses are separate. The installed Ubuntu font package includes Ubuntu Font Licence 1.0 and file-specific GPL/CC-BY-SA notices; other fonts commonly use OFL and additional notices. Preserve `/usr/share/doc/<package>/copyright`. |
| Ubuntu tools and libraries (Python, Git, OpenSSH, curl, compilers, CMake, system libraries) | Each package retains its own license. Exact binary/source package versions and copyright files are included in the generated Debian inventory. |
| NVIDIA driver/CUDA wheel dependencies and runtime integration | Separate NVIDIA terms may apply; [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html). A CUDA wheel or host driver is not relicensed under the project's Apache license. Preserve wheel-supplied LICENSE/NOTICE/EULA files. |

## Model weights

Weights and datasets are excluded from the source publication. Model licenses apply separately to local use, fine-tuning and redistribution. The default model IDs, reviewed revisions, and the GR00T model-card/LICENSE discrepancy are documented in [the model-license table](docs/licenses.ja.md#model-weights). A different `--pretrained` value requires checking that model's own terms.

## Redistributing an image or installed environment

Preserve installed notices, generate the exact inventory with `scripts/export_licenses.py`, and provide the corresponding source and any other materials required by the component licenses. A notice bundle or a URL to an upstream homepage alone does not satisfy every GPL/LGPL/MPL distribution obligation. The exporter records evidence and missing texts; it does not certify compliance or supply corresponding source. See [the distribution procedure](docs/licenses.ja.md#image-distribution).
