# Production image: stock vLLM 0.23.0 (CUDA 12.9, includes SM80 kernels) + the SKT-AI/vllm
# axk2-v0.23.0 Python sources, applied with the same hash checks the PoC jobs run at start-up.
# The fork's only CUDA change is SM100-gated, so no CUDA rebuild is needed for A100.
#
#   docker build -f aml/Dockerfile -t <registry>/axk2-vllm:0.23.0-cu129-skt023bc76 aml/src
FROM docker.io/vllm/vllm-openai:v0.23.0-cu129
COPY overlay_manifest.json apply_overlay.py make_dense_dir.py /opt/axk2/
RUN python3 /opt/axk2/apply_overlay.py --report /opt/axk2/overlay-report.json
