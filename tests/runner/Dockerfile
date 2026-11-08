# The test runner: Python 3.12 with every component's declared dependencies,
# plus Helm and kubeconform for the chart suites. Built by scripts/test.sh
# from a context holding only the pyproject files and deploy/versions.env, so
# it is rebuilt only when dependencies change. The repository is mounted at
# /repo when tests run; nothing of ours is installed into the image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY . /ctx
RUN python /ctx/tests/runner/requirements.py /ctx > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# Helm (Apache-2.0) and kubeconform (Apache-2.0), pinned in deploy/versions.env, checksums verified.
ARG TARGETARCH
RUN set -eu; . /ctx/deploy/versions.env; arch="${TARGETARCH:-amd64}"; \
    apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null; \
    cd /tmp; \
    f="helm-${HELM_VERSION}-linux-${arch}.tar.gz"; \
    curl -fsSLO "https://get.helm.sh/$f"; \
    echo "$(curl -fsSL "https://get.helm.sh/$f.sha256sum" | cut -d' ' -f1)  $f" | sha256sum -c -; \
    tar -xzf "$f" && mv "linux-${arch}/helm" /usr/local/bin/helm; \
    f="kubeconform-linux-${arch}.tar.gz"; \
    base="https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}"; \
    curl -fsSLO "$base/$f" && curl -fsSL "$base/CHECKSUMS" | grep " $f\$" | sha256sum -c -; \
    tar -xzf "$f" kubeconform && mv kubeconform /usr/local/bin/; \
    rm -rf /tmp/* /ctx /var/lib/apt/lists/*; \
    helm version --short && kubeconform -v
WORKDIR /repo
