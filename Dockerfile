FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

# Install dependencies
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    cmake \
    wget \
    python3 \
    python3-pip \
    libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone and build llama.cpp
WORKDIR /app
RUN git clone https://github.com/ggerganov/llama.cpp.git && \
    cd llama.cpp && \
    cmake -B build -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc && \
    cmake --build build --config Release

# Expose the API port
EXPOSE 8080

# Use correct binary name (llama-server)
ENTRYPOINT ["/bin/bash", "-c", "cd /app/llama.cpp && ./build/bin/llama-server --model /app/models/llama-2-7b-chat.Q4_K_M.gguf --n-gpu-layers 35 --port 8080 --host 0.0.0.0"]
