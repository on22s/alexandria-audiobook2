FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

WORKDIR /alexandria

# Install system dependencies for audio processing
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY app/requirements.txt /alexandria/app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt && \
    pip install --no-cache-dir qwen-tts==0.1.1

# Copy application code
COPY app/ /alexandria/app/
COPY default_prompts.txt review_prompts.txt persona_prompts.txt /alexandria/
COPY gpu_stats.py alexandria_alignment.py alexandria_preparer_rocm_compatible.py llm_enricher.py /alexandria/
COPY voice_analysis.py batch_train_lora.py voice_profiler.py name_voices.py icon.png /alexandria/
COPY builtin_lora/ /alexandria/builtin_lora/

# Create directories for runtime data
RUN mkdir -p /alexandria/scripts \
    /alexandria/designed_voices \
    /alexandria/clone_voices \
    /alexandria/lora_models \
    /alexandria/lora_datasets \
    /alexandria/dataset_builder \
    /alexandria/app/uploads

RUN mkdir -p /alexandria/runtime

# Bind to 0.0.0.0 inside the container. This makes the app reachable from
# outside the container/host — if that network isn't trusted, enable the auth
# gate by passing -e ALEXANDRIA_AUTH_PASSWORD=... to `docker run` (see README).
ENV ALEXANDRIA_HOST=0.0.0.0
ENV ALEXANDRIA_DATA_DIR=/alexandria/runtime
EXPOSE 4200

CMD ["python", "app/app.py"]
