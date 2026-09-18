FROM python:3.12-slim

ARG BOTDCA_UID=10001
ARG BOTDCA_GID=10001

WORKDIR /app

COPY pyproject.toml README.md ./
COPY botdca ./botdca

RUN pip install --no-cache-dir .

RUN groupadd --gid "${BOTDCA_GID}" botdca \
    && useradd --uid "${BOTDCA_UID}" --gid botdca --no-create-home --shell /usr/sbin/nologin botdca \
    && mkdir -p /var/lib/botdca/secrets \
    && chown -R botdca:botdca /var/lib/botdca

ENV PYTHONDONTWRITEBYTECODE=1

USER botdca

EXPOSE 8000

CMD ["uvicorn", "botdca.api:app", "--host", "0.0.0.0", "--port", "8000"]
