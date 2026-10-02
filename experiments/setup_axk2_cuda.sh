#!/usr/bin/env bash
set -euo pipefail
exec > /tmp/axk2-setup.log 2>&1
python3 -m pip install --target /opt/axk2-bootstrap uv
/opt/axk2-bootstrap/bin/uv venv /opt/axk2-validation --python 3.12
/opt/axk2-bootstrap/bin/uv pip install --python /opt/axk2-validation/bin/python \
  torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
/opt/axk2-bootstrap/bin/uv pip install --python /opt/axk2-validation/bin/python \
  "transformers @ git+https://github.com/huggingface/transformers.git@7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae" accelerate
/opt/axk2-validation/bin/python -c \
  'import torch; from transformers import AXK2ForCausalLM; print(torch.__version__, torch.cuda.get_device_name()); x=torch.ones((8,8), device="cuda", dtype=torch.bfloat16); print((x@x).sum().item())'
/opt/axk2-bootstrap/bin/uv pip freeze --python /opt/axk2-validation/bin/python > /tmp/axk2-cuda-requirements.txt
echo AXK2_SETUP_PASSED
