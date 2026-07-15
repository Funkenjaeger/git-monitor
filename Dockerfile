FROM python:3.12-slim

# git: scan local/mounted repos.  openssh-client: pull from remote hosts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Config, sqlite db, and the collector's ssh key live on a mounted volume.
ENV GITMON_CONFIG=/data/config.yaml \
    GITMON_DB=/data/data.db \
    GITMON_PORT=8083

EXPOSE 8083
CMD ["python", "app.py"]
