# The test runner: Python 3.12 with every component's declared dependencies.
# Built by scripts/test.sh from a context holding only the pyproject files,
# so it is rebuilt only when dependencies change. The repository is mounted
# at /repo when tests run; nothing of ours is installed into the image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY . /ctx
RUN python /ctx/tests/runner/requirements.py /ctx > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -rf /ctx /tmp/requirements.txt
WORKDIR /repo
